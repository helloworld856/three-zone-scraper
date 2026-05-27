from __future__ import annotations

from datetime import datetime
import random
import re
import time

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from src.core import (
    DEFAULT_X_CDP_URL,
    XlsxRowWriter,
    MultiSheetXlsxWriter,
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    interruptible_sleep,
    sanitize_csv_cell,
    should_stop,
    wait_if_paused,
)
from src.platforms.x_twitter.comments import extract_comments


def _parse_date_range(start_str: str, end_str: str):
    from datetime import datetime
    start_dt = datetime.strptime(start_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_str, "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError(f"开始日期 {start_str} 晚于结束日期 {end_str}")
    return start_dt, end_dt


CSV_FIELDS = ["序号", "帖子ID", "发布时间", "帖子内容", "浏览量", "点赞量", "转发量", "评论数", "帖子链接", "博主链接"]
PAGE_LOAD_TIMEOUT = 30000
INITIAL_LOAD_DELAY = 2.0
SCROLL_DELAY = 3.2
SCROLL_PX = 2800
NO_NEW_SCROLL_LIMIT = 10
DEFAULT_MAX_SCROLLS = 300
SAVE_BATCH_SIZE = 10
COOLDOWN_MIN_SECONDS = 6.0
COOLDOWN_MAX_SECONDS = 15.0

BLOCKED_PROFILE_NAMES = {
    "home",
    "explore",
    "notifications",
    "messages",
    "i",
    "search",
    "settings",
    "signup",
    "login",
}


def log_line(log_callback, text: str):
    if log_callback:
        log_callback(text)


def clean_profile_url(url: str) -> str:
    value = (url or "").strip().replace("twitter.com", "x.com")
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://x.com" + value
    if not value.startswith("http"):
        value = "https://" + value
    return value.split("?")[0].split("#")[0].rstrip("/")


def extract_profile_username(profile_url: str) -> str:
    match = re.match(r"https?://(?:www\.)?x\.com/([^/?#]+)", clean_profile_url(profile_url), re.I)
    if not match:
        return ""
    username = match.group(1).strip().strip("@")
    if username.lower() in BLOCKED_PROFILE_NAMES:
        return ""
    return username


def parse_profile_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        url = clean_profile_url(stripped.split()[0])
        username = extract_profile_username(url)
        if username and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def format_tweet_time(raw_time: str) -> str:
    value = (raw_time or "").strip()
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def normalize_tweet(tweet: dict[str, str]) -> dict[str, str]:
    post_id = str(tweet.get("postId") or tweet.get("post_id") or "")
    return {
        "post_id": str(sanitize_csv_cell(post_id)),
        "published_at": str(sanitize_csv_cell(format_tweet_time(tweet.get("publishedAt", tweet.get("published_at", ""))))),
        "content": str(sanitize_csv_cell(tweet.get("content", ""))),
        "views": str(sanitize_csv_cell(expand_compact_number(tweet.get("views", "")))),
        "likes": str(sanitize_csv_cell(expand_compact_number(tweet.get("likes", "")))),
        "retweets": str(sanitize_csv_cell(expand_compact_number(tweet.get("retweets", "")))),
        "replies": str(sanitize_csv_cell(expand_compact_number(tweet.get("replies", "")))),
        "url": str(sanitize_csv_cell(tweet.get("url", ""))),
    }


def row_from_tweet(index: int, tweet: dict[str, str]) -> dict[str, str]:
    return {
        "序号": str(index),
        "帖子ID": tweet.get("post_id") or tweet.get("postId", ""),
        "发布时间": tweet.get("published_at") or tweet.get("publishedAt", ""),
        "帖子内容": tweet.get("content", ""),
        "浏览量": tweet.get("views", ""),
        "点赞量": tweet.get("likes", ""),
        "转发量": tweet.get("retweets", ""),
        "评论数": tweet.get("replies", ""),
        "帖子链接": tweet.get("url", ""),
        "博主链接": tweet.get("profile_url", ""),
    }


def cooldown_after_batch(total_written: int, log_callback, stop_event=None, pause_event=None, save_batch_size=None, cooldown_min=None, cooldown_max=None):
    if save_batch_size is None:
        save_batch_size = SAVE_BATCH_SIZE
    if cooldown_min is None:
        cooldown_min = COOLDOWN_MIN_SECONDS
    if cooldown_max is None:
        cooldown_max = COOLDOWN_MAX_SECONDS
    if total_written <= 0 or total_written % save_batch_size != 0:
        return
    seconds = random.uniform(cooldown_min, cooldown_max)
    log_line(log_callback, f"  已保存 {total_written} 条帖子，随机等待 {seconds:.1f} 秒。")
    deadline = time.time() + seconds
    while time.time() < deadline:
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        time.sleep(min(0.5, deadline - time.time()))


def extract_visible_profile_tweets(page, username: str) -> list[dict[str, str]]:
    username_lc = username.lower().lstrip("@")
    return page.evaluate(
        """async ({ username }) => {
            const results = [];
            const normalize = value => (value || '').trim().replace(/^@/, '').toLowerCase();
            const ownStatus = article => {
                const time = article.querySelector('time');
                const link = time ? time.closest('a[href*="/status/"]') : null;
                const href = link ? link.getAttribute('href') : '';
                const match = href.match(/\\/status\\/(\\d+)/);
                let handle = '';
                try {
                    const url = new URL(href, location.origin);
                    handle = (url.pathname.split('/').filter(Boolean)[0] || '').trim();
                } catch (error) {}
                return { href, postId: match ? match[1] : '', handle };
            };
            const isPromoted = article => {
                const text = (article.innerText || '').split('\\n').map(x => x.trim().toLowerCase());
                return text.some(line => ['ad', 'promoted', '广告', '推广'].includes(line));
            };
            const nonTextContent = article => {
                const types = [];
                if (article.querySelector('[data-testid="tweetPhoto"], img[src*="/media/"]')) types.push('图片');
                if (article.querySelector('video')) types.push('视频');
                if ((article.innerText || '').split('\\n').some(line => line.trim().toLowerCase() === 'gif')) types.push('GIF');
                if (article.querySelector('[data-testid="card.wrapper"], [data-testid="card.layoutLarge.media"], [data-testid="card.layoutSmall.media"]')) types.push('卡片');
                return types.length ? `[${types.join('+')}]` : '[非文本]';
            };
            const ariaMetric = (root, testIds) => {
                for (const id of testIds) {
                    const el = root.querySelector(`[data-testid="${id}"]`);
                    if (!el) continue;
                    const rawText = (el.innerText || el.textContent || '').trim();
                    if (rawText && /\\d/.test(rawText)) return rawText;
                    const aria = el.getAttribute('aria-label') || '';
                    const match = aria ? aria.match(/([\\d,]+(\\.\\d+)?\\s*[KkMmBb]?)/) : null;
                    if (match) return match[1].replace(/,/g, '');
                }
                return '';
            };
            const firstMetric = (root, selectors) => {
                for (const selector of selectors) {
                    const el = root.querySelector(selector);
                    if (!el) continue;
                    const rawText = (el.innerText || el.textContent || '').trim();
                    if (rawText && /\\d/.test(rawText)) return rawText;
                    const aria = el.getAttribute('aria-label') || '';
                    const match = aria ? aria.match(/([\\d,]+(\\.\\d+)?\\s*[KkMmBb]?)/) : null;
                    if (match) return match[1].replace(/,/g, '');
                }
                return '';
            };

            // Phase 1: collect matching articles and apply mutations
            const revertTexts = ['view original', '查看原文', '原文を表示', 'show original', '原文を見る'];
            const expandTexts = ['show more', 'show more...', 'もっと見る', '더 보기'];
            const articles = [];
            for (const article of document.querySelectorAll('article[data-testid="tweet"], article')) {
                try {
                    if (isPromoted(article)) continue;
                    const info = ownStatus(article);
                    if (!info.postId || normalize(info.handle) !== username) continue;

                    const textEl = article.querySelector('[data-testid="tweetText"]');
                    if (textEl) {
                        const allNodes = article.querySelectorAll('*');
                        for (const node of allNodes) {
                            const nodeText = (node.textContent || '').trim().toLowerCase();
                            if (!nodeText || node.children.length > 0) continue;
                            if (revertTexts.includes(nodeText)) {
                                try { node.click(); } catch (_) {}
                                break;
                            }
                        }
                        textEl.style.setProperty('max-height', 'none', 'important');
                        textEl.style.setProperty('overflow', 'visible', 'important');
                        textEl.style.setProperty('-webkit-line-clamp', 'unset', 'important');
                        textEl.style.setProperty('display', 'block', 'important');
                        textEl.style.setProperty('white-space', 'normal', 'important');
                        for (const node of allNodes) {
                            const nodeText = (node.textContent || '').trim().toLowerCase();
                            if (!nodeText || node.children.length > 0) continue;
                            if (!expandTexts.includes(nodeText)) continue;
                            try { node.click(); } catch (_) {}
                            break;
                        }
                    }
                    const timeEl = article.querySelector('time');
                    const publishedAt = timeEl ? (timeEl.getAttribute('datetime') || '') : '';
                    const href = info.href.startsWith('http') ? info.href : `https://x.com${info.href}`;
                    articles.push({ article, postId: info.postId, publishedAt, href });
                } catch (error) {}
            }

            // Wait for React to re-render with original text
            if (articles.length > 0) {
                await new Promise(r => setTimeout(r, 500));
            }

            // Phase 2: read text and metrics
            for (const { article, postId, publishedAt, href } of articles) {
                try {
                    const textEl = article.querySelector('[data-testid="tweetText"]');
                    const text = textEl ? (textEl.innerText || textEl.textContent || '').trim() : '';
                    results.push({
                        postId,
                        publishedAt,
                        content: text || nonTextContent(article),
                        url: href,
                        views: firstMetric(article, [
                            'a[href*="/analytics"]',
                            'div[data-testid="postViewCount"]',
                            '[aria-label*="Views"]',
                            '[aria-label*="views"]',
                            '[aria-label*="浏览"]',
                            '[aria-label*="表示"]',
                        ]) || '',
                        likes: ariaMetric(article, ['like', 'unlike']) || '',
                        retweets: ariaMetric(article, ['retweet', 'unretweet']) || '',
                        replies: ariaMetric(article, ['reply']) || '',
                    });
                } catch (error) {}
            }
            return results;
        }""",
        {"username": username_lc},
    )


def collect_profile_tweets(
    page,
    detail_page,
    profile_url: str,
    max_scrolls: int,
    limit_time_bool: bool,
    start_dt,
    end_dt,
    get_comments_bool: bool,
    max_comments: int,
    log_callback,
    stop_event=None,
    writer=None,
    row_offset: int = 0,
    page_timeout=None,
    scroll_delay=None,
    no_new_scroll_limit=None,
    save_batch_size=None,
    cooldown_min=None,
    cooldown_max=None,
    pause_event=None,
    keyword: str | None = None,
) -> list[dict[str, str]] | tuple[list[dict[str, str]], int, int]:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if scroll_delay is None:
        scroll_delay = SCROLL_DELAY
    if no_new_scroll_limit is None:
        no_new_scroll_limit = NO_NEW_SCROLL_LIMIT
    if save_batch_size is None:
        save_batch_size = SAVE_BATCH_SIZE
    if cooldown_min is None:
        cooldown_min = COOLDOWN_MIN_SECONDS
    if cooldown_max is None:
        cooldown_max = COOLDOWN_MAX_SECONDS

    username = extract_profile_username(profile_url)
    if not username:
        raise ValueError(f"无效的 X 博主主页链接：{profile_url}")

    if keyword:
        import urllib.parse
        search_query = f"from:{username} {keyword}"
        target_url = f"https://x.com/search?q={urllib.parse.quote(search_query)}&src=typed_query&f=live"
    else:
        target_url = clean_profile_url(profile_url)

    page.goto(target_url, wait_until="domcontentloaded", timeout=page_timeout)
    page.wait_for_selector('article[data-testid="tweet"], article', timeout=page_timeout)
    interruptible_sleep(INITIAL_LOAD_DELAY, stop_event)

    tweets: list[dict[str, str]] = []
    pending_rows: list[dict[str, str]] = []
    written_count = 0
    seen_ids = set()
    no_new_count = 0
    max_scrolls = max(1, int(max_scrolls or DEFAULT_MAX_SCROLLS))
    log_line(log_callback, f"  开始采集 @{username} 主页帖子，最多滚动 {max_scrolls} 次。")

    for scroll_index in range(max_scrolls):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        visible_tweets = extract_visible_profile_tweets(page, username)
        added = 0
        for tweet in visible_tweets:
            post_id = str(tweet.get("postId") or "")
            if not post_id or post_id in seen_ids:
                continue
            seen_ids.add(post_id)
            normalized_tweet = normalize_tweet(tweet)
            
            if limit_time_bool:
                pub_time = normalized_tweet.get("published_at")
                if not pub_time:
                    continue
                try:
                    pub_dt = datetime.strptime(pub_time, "%Y-%m-%d %H:%M:%S")
                    if not (start_dt.date() <= pub_dt.date() <= end_dt.date()):
                        continue
                except Exception:
                    continue
                    
            normalized_tweet["profile_url"] = profile_url
            tweets.append(normalized_tweet)
            added += 1
            if writer:
                row_offset += 1
                row = row_from_tweet(row_offset, normalized_tweet)
                pending_rows.append(row)
                
                if get_comments_bool:
                    try:
                        detail_page.goto(normalized_tweet["url"], wait_until="domcontentloaded", timeout=30000)
                        detail_page.wait_for_selector('article[data-testid="tweet"]', timeout=30000)
                        interruptible_sleep(2, stop_event)
                        comments = extract_comments(detail_page, normalized_tweet["url"], max_comments, log_callback, stop_event, pause_event=pause_event)
                        for comment in comments:
                            comment_row = {
                                "序号": str(row_offset),
                                "推文链接": normalized_tweet["url"],
                                "评论的点赞量": comment.get("likes", ""),
                                "评论内容": comment.get("content", ""),
                                "评论发布时间": comment.get("time", "")
                            }
                            writer.writerow("评论信息", comment_row)
                    except Exception as exc:
                        log_line(log_callback, f"    提取评论失败：{exc}")
                
                if len(pending_rows) >= save_batch_size:
                    if hasattr(writer, "writerow") and hasattr(writer, "worksheets"):
                        for r in pending_rows:
                            writer.writerow("推文信息", r)
                    else:
                        writer.writerows(pending_rows)
                    writer.save()
                    written_count += len(pending_rows)
                    pending_rows.clear()
                    cooldown_after_batch(written_count, log_callback, stop_event, pause_event=pause_event, save_batch_size=save_batch_size, cooldown_min=cooldown_min, cooldown_max=cooldown_max)
                    if should_stop(stop_event):
                        break

        if added:
            log_line(log_callback, f"  滚动 {scroll_index + 1}/{max_scrolls}：新增 {added} 条，累计 {len(tweets)} 条。")
            no_new_count = 0
        else:
            no_new_count += 1
            if no_new_count >= no_new_scroll_limit:
                log_line(log_callback, f"  连续 {no_new_scroll_limit} 次没有新增帖子，停止。")
                break

        if should_stop(stop_event):
            break

        page.evaluate(f"window.scrollBy(0, {SCROLL_PX})")
        interruptible_sleep(scroll_delay + 1.0 if no_new_count else scroll_delay, stop_event)

    if writer and pending_rows:
        if hasattr(writer, "writerow") and hasattr(writer, "worksheets"):
            for r in pending_rows:
                writer.writerow("推文信息", r)
        else:
            writer.writerows(pending_rows)
        writer.save()
        written_count += len(pending_rows)
        pending_rows.clear()

    if writer:
        return tweets, row_offset, written_count
    return tweets


def build_rows(tweets: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = []
    for index, tweet in enumerate(tweets, 1):
        rows.append(row_from_tweet(index, tweet))
    return rows


def run_x_profile_tweets_spider(
    profile_urls_text: str,
    use_keywords_str: str,
    keywords_text: str,
    limit_time_str: str,
    start_date: str,
    end_date: str,
    get_comments_str: str,
    max_comments: int,
    cdp_port_or_url: str = DEFAULT_X_CDP_URL,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    log_callback=None,
    finish_callback=None,
    stop_event=None,
    config=None,
    pause_event=None,
):
    if config is None:
        config = {}
    page_load_timeout_val = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    scroll_delay_val = float(config.get("scroll_interval", SCROLL_DELAY))
    no_new_scroll_limit_val = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
    save_batch_size_val = int(config.get("save_batch_size", SAVE_BATCH_SIZE))
    cooldown_min_val = float(config.get("cooldown_min", COOLDOWN_MIN_SECONDS))
    cooldown_max_val = float(config.get("cooldown_max", COOLDOWN_MAX_SECONDS))
    max_scrolls = int(config.get("max_scrolls", max_scrolls))

    completed_path = None
    page = None
    try:
        if sync_playwright is None:
            log_line(log_callback, "缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        profile_urls = parse_profile_urls(profile_urls_text)
        if not profile_urls:
            log_line(log_callback, "未读取到有效的 X 博主主页链接。")
            return

        limit_time_bool = limit_time_str == "是"
        get_comments_bool = get_comments_str == "是"
        start_dt, end_dt = None, None
        if limit_time_bool:
            start_dt, end_dt = _parse_date_range(start_date, end_date)

        max_comments_val = max(10, int(max_comments))
        output_path = build_output_path("x", f"x_profile_tweets_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        
        if get_comments_bool:
            comment_fields = ["序号", "推文链接", "评论的点赞量", "评论内容", "评论发布时间"]
            writer = MultiSheetXlsxWriter(output_path, {"推文信息": CSV_FIELDS, "评论信息": comment_fields})
        else:
            writer = XlsxRowWriter(output_path, CSV_FIELDS)
            
        row_offset = 0

        with sync_playwright() as playwright:
            log_line(log_callback, "正在连接本地 Chrome...")
            try:
                _, context = connect_existing_chromium(playwright, cdp_port_or_url)
            except Exception as exc:
                log_line(log_callback, f"无法连接浏览器：{exc}")
                log_line(log_callback, "连接失败：请确认 Chrome 已自动打开并已登录 X/Twitter。")
                return

            page = context.new_page()
            detail_page = context.new_page() if get_comments_bool else None

            use_keywords_bool = use_keywords_str == "是"
            keyword_list = [None]
            if use_keywords_bool:
                parsed_kws = [k.strip() for k in keywords_text.splitlines() if k.strip()]
                if parsed_kws:
                    keyword_list = parsed_kws
                else:
                    log_line(log_callback, "启用了关键词搜索但未提供关键词，将按无关键词采集。")

            total_tasks = len(profile_urls) * len(keyword_list)
            task_index = 0

            for profile_url in profile_urls:
                if should_stop(stop_event):
                    log_line(log_callback, "任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break

                for kw in keyword_list:
                    if should_stop(stop_event):
                        break
                    if wait_if_paused(pause_event, stop_event):
                        break

                    task_index += 1
                    username = extract_profile_username(profile_url)
                    kw_info = f" (关键词: {kw})" if kw else ""
                    log_line(log_callback, f"[{task_index}/{total_tasks}] 读取主页：{profile_url}{kw_info}")
                    try:
                        _, row_offset, written_count = collect_profile_tweets(
                            page,
                            detail_page,
                            profile_url,
                            max_scrolls,
                            limit_time_bool,
                            start_dt,
                            end_dt,
                            get_comments_bool,
                            max_comments_val,
                            log_callback,
                            stop_event,
                            writer=writer,
                            row_offset=row_offset,
                            page_timeout=page_load_timeout_val,
                            scroll_delay=scroll_delay_val,
                            no_new_scroll_limit=no_new_scroll_limit_val,
                            save_batch_size=save_batch_size_val,
                            cooldown_min=cooldown_min_val,
                            cooldown_max=cooldown_max_val,
                            pause_event=pause_event,
                            keyword=kw,
                        )
                        log_line(log_callback, f"  完成 @{username}{kw_info}：写入 {written_count} 条帖子。")
                    except PlaywrightTimeoutError:
                        log_line(log_callback, "  跳过：页面加载超时，请确认链接可打开且账号已登录。")
                    except Exception as exc:
                        log_line(log_callback, f"  跳过：{exc}")

            for opened_page in (page, detail_page):
                if opened_page is not None and not opened_page.is_closed():
                    opened_page.close()

        completed_path = output_path
        writer.save()
        log_line(log_callback, f"完成，已保存：{output_path}")
    finally:
        try:
            if page and not page.is_closed():
                page.close()
        except Exception:
            pass
        if finish_callback:
            finish_callback(completed_path)
