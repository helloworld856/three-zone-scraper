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
    sanitize_csv_cell,
    should_stop,
)
from src.platforms.x_twitter.comments import extract_comments
from src.platforms.tiktok.keyword import parse_date_range


CSV_FIELDS = ["序号", "帖子ID", "发布时间", "帖子内容", "帖子链接"]
PAGE_LOAD_TIMEOUT = 30000
INITIAL_LOAD_DELAY = 2.0
SCROLL_DELAY = 1.2
SLOW_SCROLL_DELAY = 2.2
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
        "url": str(sanitize_csv_cell(tweet.get("url", ""))),
    }


def row_from_tweet(index: int, tweet: dict[str, str]) -> dict[str, str]:
    return {
        "序号": str(index),
        "帖子ID": tweet.get("post_id") or tweet.get("postId", ""),
        "发布时间": tweet.get("published_at") or tweet.get("publishedAt", ""),
        "帖子内容": tweet.get("content", ""),
        "帖子链接": tweet.get("url", ""),
    }


def cooldown_after_batch(total_written: int, log_callback, stop_event=None):
    if total_written <= 0 or total_written % SAVE_BATCH_SIZE != 0:
        return
    seconds = random.uniform(COOLDOWN_MIN_SECONDS, COOLDOWN_MAX_SECONDS)
    log_line(log_callback, f"  已保存 {total_written} 条帖子，随机等待 {seconds:.1f} 秒。")
    deadline = time.time() + seconds
    while time.time() < deadline:
        if should_stop(stop_event):
            break
        time.sleep(min(0.5, deadline - time.time()))


def extract_visible_profile_tweets(page, username: str) -> list[dict[str, str]]:
    username_lc = username.lower().lstrip("@")
    return page.evaluate(
        """({ username }) => {
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

            for (const article of document.querySelectorAll('article[data-testid="tweet"], article')) {
                try {
                    if (isPromoted(article)) continue;
                    const info = ownStatus(article);
                    if (!info.postId || normalize(info.handle) !== username) continue;

                    const textEl = article.querySelector('[data-testid="tweetText"]');
                    const text = textEl ? (textEl.innerText || textEl.textContent || '').trim() : '';
                    const timeEl = article.querySelector('time');
                    const publishedAt = timeEl ? (timeEl.getAttribute('datetime') || '') : '';
                    const href = info.href.startsWith('http') ? info.href : `https://x.com${info.href}`;

                    results.push({
                        postId: info.postId,
                        publishedAt,
                        content: text || nonTextContent(article),
                        url: href,
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
) -> list[dict[str, str]] | tuple[list[dict[str, str]], int, int]:
    username = extract_profile_username(profile_url)
    if not username:
        raise ValueError(f"无效的 X 博主主页链接：{profile_url}")

    page.goto(clean_profile_url(profile_url), wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
    page.wait_for_selector('article[data-testid="tweet"], article', timeout=PAGE_LOAD_TIMEOUT)
    time.sleep(INITIAL_LOAD_DELAY)

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
                        time.sleep(2)
                        comments = extract_comments(detail_page, normalized_tweet["url"], max_comments, log_callback, stop_event)
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
                
                if len(pending_rows) >= SAVE_BATCH_SIZE:
                    if hasattr(writer, "writerow") and hasattr(writer, "worksheets"):
                        for r in pending_rows:
                            writer.writerow("推文信息", r)
                    else:
                        writer.writerows(pending_rows)
                    writer.save()
                    written_count += len(pending_rows)
                    pending_rows.clear()
                    cooldown_after_batch(written_count, log_callback, stop_event)
                    if should_stop(stop_event):
                        break

        if added:
            log_line(log_callback, f"  滚动 {scroll_index + 1}/{max_scrolls}：新增 {added} 条，累计 {len(tweets)} 条。")
            no_new_count = 0
        else:
            no_new_count += 1
            if no_new_count >= NO_NEW_SCROLL_LIMIT:
                log_line(log_callback, f"  连续 {NO_NEW_SCROLL_LIMIT} 次没有新增帖子，停止。")
                break

        if should_stop(stop_event):
            break

        page.evaluate(f"window.scrollBy(0, {SCROLL_PX})")
        time.sleep(SLOW_SCROLL_DELAY if no_new_count else SCROLL_DELAY)

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
):
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
            start_dt, end_dt = parse_date_range(start_date, end_date)

        max_comments_val = max(10, int(max_comments))
        output_path = build_output_path("x", f"x_profile_tweets_{time.strftime('%Y%m%d')}.xlsx")
        
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
            detail_page = context.new_page()

            for profile_index, profile_url in enumerate(profile_urls, 1):
                if should_stop(stop_event):
                    log_line(log_callback, "任务已停止。")
                    break

                username = extract_profile_username(profile_url)
                log_line(log_callback, f"[{profile_index}/{len(profile_urls)}] 读取主页：{profile_url}")
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
                    )
                    log_line(log_callback, f"  完成 @{username}：写入 {written_count} 条帖子。")
                except PlaywrightTimeoutError:
                    log_line(log_callback, "  跳过：页面加载超时，请确认链接可打开且账号已登录。")
                except Exception as exc:
                    log_line(log_callback, f"  跳过：{exc}")

            for opened_page in (page, detail_page):
                if not opened_page.is_closed():
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
