from __future__ import annotations

from datetime import datetime
import re
import time

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from src.core import (
    XlsxRowWriter,
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    interruptible_sleep,
    random_cooldown,
    sanitize_csv_cell,
    should_stop,
    wait_if_paused,
)

TOP_COMMENT_LIMIT = 100
DEFAULT_SCAN_LIMIT = 500
SCROLL_PAUSE = 4.0
PAGE_LOAD_TIMEOUT = 30000
NO_NEW_SCROLL_LIMIT = 5
CSV_FIELDS = ["编号", "帖文链接", "点赞数", "评论内容", "评论发布时间"]
RECOMMENDATION_MARKERS = (
    "discover more",
    "more posts",
    "relevant people",
    "who to follow",
    "相关推荐",
    "发现更多",
    "更多帖子",
    "相关用户",
    "推荐关注",
)
PROMOTED_MARKERS = ("promoted", "广告", "推广")

def log_line(log_callback, text: str):
    if log_callback:
        log_callback(text)

def clean_url(url: str) -> str:
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

def parse_tweet_urls(txt_path: str) -> list[str]:
    urls: list[str] = []
    seen = set()
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            url = clean_url(stripped.split()[0])
            if "/status/" in url and url not in seen:
                urls.append(url)
                seen.add(url)
    return urls

def metric_to_int(value: str) -> int:
    text = expand_compact_number(str(value or "0")).replace(",", "")
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else 0

def extract_status_id(url: str) -> str:
    match = re.search(r"/status/(\d+)", url or "")
    return match.group(1) if match else ""

def normalize_handle(value: str) -> str:
    return (value or "").strip().lstrip("@").lower()

def format_comment_time(raw_time: str) -> str:
    value = (raw_time or "").strip()
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        match = re.search(r"(\d{4}-\d{2}-\d{2})[T\s](\d{2}):(\d{2}):(\d{2})", value)
        if match:
            return f"{match.group(1)} {match.group(2)}:{match.group(3)}:{match.group(4)}"
    return value

def has_selector(node, selector: str) -> bool:
    try:
        return node.query_selector(selector) is not None
    except Exception:
        return False

def text_has_promoted_marker(text: str) -> bool:
    raw_text = text or ""
    lines = [line.strip().lower() for line in raw_text.splitlines() if line.strip()]
    if any(line in {"ad", "promoted", "广告", "推广"} for line in lines):
        return True
    normalized = re.sub(r"\s+", " ", raw_text).strip().lower()
    if not normalized:
        return False
    if any(marker in normalized for marker in PROMOTED_MARKERS):
        return True
    return False

def text_has_recommendation_marker(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    if not normalized:
        return False
    return any(marker.lower() in normalized for marker in RECOMMENDATION_MARKERS)

def is_promoted_tweet(article) -> bool:
    try:
        text = article.evaluate(
            """node => {
                const hasPromotedContainer = el => {
                    if (!el || !el.querySelectorAll) return false;
                    const dataTestId = (el.getAttribute && el.getAttribute('data-testid')) || '';
                    if (/placementTracking|promoted/i.test(dataTestId)) return true;
                    return !!el.querySelector('[data-testid*="placementTracking"], [data-testid*="promoted"], [data-testid*="Promoted"]');
                };
                if (hasPromotedContainer(node)) return 'Ad';

                const parts = Array.from(node.querySelectorAll('[data-testid="socialContext"], [aria-label], span, div'))
                    .map(el => (el.innerText || el.getAttribute('aria-label') || '').trim())
                    .filter(Boolean);
                const cell = node.closest('[data-testid="cellInnerDiv"]') || node.parentElement;
                if (cell) {
                    if (hasPromotedContainer(cell)) return 'Ad';
                    parts.push(cell.innerText || '');
                    if (cell.previousElementSibling) {
                        parts.push(cell.previousElementSibling.innerText || cell.previousElementSibling.getAttribute('aria-label') || '');
                    }
                }
                return parts.join('\\n');
            }"""
        )
    except Exception:
        try:
            text = article.inner_text()
        except Exception:
            text = ""
    return text_has_promoted_marker(str(text or ""))

def article_own_status_id(article) -> str:
    try:
        href = article.evaluate(
            """node => {
                const time = node.querySelector('time');
                const link = time ? time.closest('a[href*="/status/"]') : null;
                return link ? link.getAttribute('href') : '';
            }"""
        )
    except Exception:
        href = ""
    return extract_status_id(str(href or ""))

def article_contains_status_id(article, status_id: str) -> bool:
    if not status_id:
        return False
    try:
        hrefs = [
            anchor.get_attribute("href") or ""
            for anchor in article.query_selector_all('a[href*="/status/"]')
        ]
    except Exception:
        hrefs = []
    return any(status_id in href for href in hrefs)

def extract_article_author_handle(article) -> str:
    try:
        user_name_el = article.query_selector('div[data-testid="User-Name"]')
        if not user_name_el:
            return ""
        for span in user_name_el.query_selector_all("span"):
            span_text = span.inner_text().strip()
            if span_text.startswith("@"):
                return normalize_handle(span_text)
        for link in user_name_el.query_selector_all('a[role="link"]'):
            href = link.get_attribute("href") or ""
            handle = href.strip("/").split("/")[0]
            if handle and handle not in {"i", "home", "search"}:
                return normalize_handle(handle)
    except Exception:
        return ""
    return ""

def find_main_tweet_article(page, target_status_id: str):
    try:
        articles = page.query_selector_all('article[data-testid="tweet"]')
    except Exception:
        return None
    for article in articles:
        if article_own_status_id(article) == target_status_id:
            return article
    return None

def recommendation_boundary_visible(page) -> bool:
    markers = [marker.lower() for marker in RECOMMENDATION_MARKERS]
    try:
        return bool(
            page.evaluate(
                """markers => {
                    const firstArticle = document.querySelector('article[data-testid="tweet"]');
                    const conversation =
                        (firstArticle && firstArticle.closest('[aria-label*="Conversation"], [aria-label*="对话"], [aria-label*="会話"], [aria-label*="会话"]')) ||
                        document.querySelector('[aria-label*="Conversation"], [aria-label*="对话"], [aria-label*="会話"], [aria-label*="会话"]') ||
                        document.querySelector('main[role="main"]') ||
                        document.body;
                    const nodes = Array.from(conversation.querySelectorAll('[role="heading"], h1, h2, h3, span'));
                    return nodes.some(node => {
                        const text = (node.innerText || node.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        return text && markers.some(marker => text.includes(marker));
                    });
                }""",
                markers,
            )
        )
    except Exception:
        return False

def is_after_recommendation_boundary(article) -> bool:
    markers = [marker.lower() for marker in RECOMMENDATION_MARKERS]
    try:
        return bool(
            article.evaluate(
                """(node, markers) => {
                    const conversation =
                        node.closest('[aria-label*="Conversation"], [aria-label*="对话"], [aria-label*="会話"], [aria-label*="会话"]') ||
                        node.closest('main[role="main"]') ||
                        document.body;
                    const walker = document.createTreeWalker(conversation, NodeFilter.SHOW_ELEMENT);
                    let boundarySeen = false;
                    while (walker.nextNode()) {
                        const current = walker.currentNode;
                        if (current === node || current.contains(node)) {
                            return boundarySeen;
                        }
                        const tag = current.tagName ? current.tagName.toLowerCase() : '';
                        const role = current.getAttribute ? current.getAttribute('role') : '';
                        if (!['h1', 'h2', 'h3', 'span'].includes(tag) && role !== 'heading') {
                            continue;
                        }
                        const text = (current.innerText || current.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        if (text && markers.some(marker => text.includes(marker))) {
                            boundarySeen = true;
                        }
                    }
                    return false;
                }""",
                markers,
            )
        )
    except Exception:
        return False

def is_inside_recommendation_section(article) -> bool:
    markers = [marker.lower() for marker in RECOMMENDATION_MARKERS]
    try:
        return bool(
            article.evaluate(
                """(node, markers) => {
                    const hasMarker = el => {
                        const text = (el && (el.innerText || el.getAttribute('aria-label') || '') || '')
                            .replace(/\\s+/g, ' ')
                            .trim()
                            .toLowerCase();
                        return text && markers.some(marker => text.includes(marker));
                    };
                    const cell = node.closest('[data-testid="cellInnerDiv"]') || node.parentElement;
                    if (!cell) return false;
                    const cellText = (cell.innerText || '').replace(/\\s+/g, ' ').trim();
                    const articleText = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                    const headingText = cellText.replace(articleText, '').slice(0, 180);
                    if (headingText && markers.some(marker => headingText.toLowerCase().includes(marker))) return true;
                    if (hasMarker(cell.previousElementSibling)) return true;

                    let current = cell.parentElement;
                    while (current && current !== document.body) {
                        const aria = current.getAttribute('aria-label') || '';
                        const firstText = (current.innerText || '')
                            .replace(/\\s+/g, ' ')
                            .trim()
                            .slice(0, 160)
                            .toLowerCase();
                        if (markers.some(marker => firstText.includes(marker))) {
                            return true;
                        }
                        if (/timeline:\\s*conversation/i.test(aria) || aria.includes('对话') || aria.includes('会話') || aria.includes('会话')) {
                            break;
                        }
                        current = current.parentElement;
                    }
                    return false;
                }""",
                markers,
            )
        )
    except Exception:
        return False

def extract_reply_to_handles(article) -> set[str]:
    try:
        handles = article.evaluate(
            """node => {
                const result = new Set();
                const blocks = Array.from(node.querySelectorAll('div, span'))
                    .filter(el => {
                        const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                        return text && (
                            /replying to/i.test(text) ||
                            text.includes('正在回复') ||
                            text.includes('回复给') ||
                            text.includes('回覆')
                        );
                    });
                for (const block of blocks) {
                    const text = block.innerText || '';
                    for (const match of text.matchAll(/@([A-Za-z0-9_]+)/g)) {
                        result.add(match[1].toLowerCase());
                    }
                    for (const link of block.querySelectorAll('a[href^="/"]')) {
                        const handle = (link.getAttribute('href') || '').split('/').filter(Boolean)[0] || '';
                        if (handle && !handle.includes('status')) {
                            result.add(handle.toLowerCase());
                        }
                    }
                }
                return Array.from(result);
            }"""
        )
    except Exception:
        handles = []
    return {normalize_handle(item) for item in handles if normalize_handle(item)}

def is_nested_reply_article(article) -> bool:
    try:
        return bool(
            article.evaluate(
                """node => {
                    const cell = node.closest('[data-testid="cellInnerDiv"]') || node.parentElement;
                    if (!cell) return false;
                    const prev = cell.previousElementSibling;
                    return !!(prev && prev.querySelector('article[data-testid="tweet"]'));
                }"""
            )
        )
    except Exception:
        return False

def is_direct_reply_to_main(article, target_status_id: str, main_author_handle: str, own_status_id: str = "") -> bool:
    own_status_id = own_status_id or article_own_status_id(article)
    if target_status_id and own_status_id == target_status_id:
        return False
    if is_nested_reply_article(article):
        return False

    reply_to_handles = extract_reply_to_handles(article)
    main_handle = normalize_handle(main_author_handle)
    if reply_to_handles and main_handle and main_handle not in reply_to_handles:
        return False
    return True

def detect_non_text_content_type(article) -> str:
    if has_selector(article, '[data-testid="videoPlayer"], video'):
        return "视频"
    if has_selector(article, '[data-testid="tweetPhoto"]'):
        return "图片"
    if has_selector(article, '[aria-label="GIF"], [data-testid="gif"]'):
        return "GIF"
    if has_selector(article, '[data-testid="card.wrapper"]'):
        return "链接卡片"
    if has_selector(article, '[role="radio"], [aria-label*="poll"]'):
        return "投票"
    return "非文本"

def extract_comments(page, tweet_url: str, max_count: int = DEFAULT_SCAN_LIMIT, log_callback=None, stop_event=None, scroll_pause=None, no_new_scroll_limit=None, pause_event=None) -> list[dict[str, str]]:
    if scroll_pause is None:
        scroll_pause = SCROLL_PAUSE
    if no_new_scroll_limit is None:
        no_new_scroll_limit = NO_NEW_SCROLL_LIMIT

    comments: list[dict[str, str]] = []
    seen_ids = set()
    no_new_count = 0
    target_status_id = extract_status_id(tweet_url)
    main_article = find_main_tweet_article(page, target_status_id)
    main_author_handle = extract_article_author_handle(main_article) if main_article else ""
    passed_main_section = False

    if not main_author_handle:
        log_line(log_callback, "  未能识别主贴作者，将使用主贴之后、推荐区之前的页面顺序兜底抓取。")
    else:
        log_line(log_callback, f"  开始抓取一级评论，目标 {max_count} 条；主贴作者 @{main_author_handle}。")

    if not target_status_id:
        passed_main_section = True

    while len(comments) < max_count:
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        articles = page.query_selector_all('article[data-testid="tweet"]')
        new_found = 0
        boundary_hit = False

        for article in articles:
            if len(comments) >= max_count:
                break

            try:
                own_status_id = article_own_status_id(article)
                if target_status_id and (
                    own_status_id == target_status_id
                    or (not passed_main_section and article_contains_status_id(article, target_status_id))
                ):
                    passed_main_section = True
                    continue
                if not passed_main_section:
                    continue
                if is_after_recommendation_boundary(article):
                    boundary_hit = True
                    log_line(log_callback, "  已到达推荐区域，停止抓取当前推文。")
                    break
                if is_inside_recommendation_section(article):
                    boundary_hit = True
                    log_line(log_callback, "  已进入推荐区域，停止抓取当前推文。")
                    break
                if is_promoted_tweet(article):
                    continue
                if not is_direct_reply_to_main(article, target_status_id, main_author_handle, own_status_id):
                    continue

                # Revert auto-translation and remove CSS truncation before reading text
                try:
                    article.evaluate("""async (el) => {
                        const revertTexts = ['view original', '查看原文', '原文を表示', 'show original', '原文を見る'];
                        const allNodes = el.querySelectorAll('*');
                        for (const node of allNodes) {
                            const text = (node.textContent || '').trim().toLowerCase();
                            if (!text || node.children.length > 0) continue;
                            if (revertTexts.includes(text)) {
                                try { node.click(); } catch (_) {}
                                break;
                            }
                        }
                        const tweetText = el.querySelector('[data-testid="tweetText"]');
                        if (tweetText) {
                            tweetText.style.setProperty('max-height', 'none', 'important');
                            tweetText.style.setProperty('overflow', 'visible', 'important');
                            tweetText.style.setProperty('-webkit-line-clamp', 'unset', 'important');
                        }
                        // Wait for React to re-render with original text
                        await new Promise(r => setTimeout(r, 400));
                    }""")
                except Exception:
                    pass

                content_el = article.query_selector('div[data-testid="tweetText"]')
                content = content_el.inner_text().strip() if content_el else ""

                user_name_el = article.query_selector('div[data-testid="User-Name"]')
                user_text = user_name_el.inner_text().strip() if user_name_el else ""

                comment_time = ""
                time_el = article.query_selector("time")
                if time_el:
                    comment_time = time_el.get_attribute("datetime") or time_el.inner_text()

                if not content:
                    content = f"[{detect_non_text_content_type(article)}]"

                comment_id = own_status_id or f"{user_text[:80]}|{comment_time}|{content[:120]}"
                if not comment_id.strip("|") or comment_id in seen_ids:
                    continue
                seen_ids.add(comment_id)
                new_found += 1

                author_name = ""
                author_handle = ""
                if user_name_el:
                    links = user_name_el.query_selector_all('a[role="link"]')
                    if links:
                        name_span = links[0].query_selector("span")
                        if name_span:
                            author_name = name_span.inner_text().strip()
                        href = links[0].get_attribute("href") or ""
                        author_handle = href.strip("/")

                    for span in user_name_el.query_selector_all("span"):
                        span_text = span.inner_text().strip()
                        if span_text.startswith("@"):
                            author_handle = span_text
                            break

                like_count = "0"
                for testid in ("like", "unlike"):
                    btn = article.query_selector(f'button[data-testid="{testid}"]')
                    if not btn:
                        continue
                    raw_text = btn.inner_text().strip()
                    if raw_text and re.search(r"\d", raw_text):
                        like_count = expand_compact_number(raw_text)
                        break
                    aria = btn.get_attribute("aria-label") or ""
                    match = re.search(r"([\d,.]+(?:\.\d+)?\s*[KkMmBb]?)", aria)
                    if match:
                        like_count = expand_compact_number(match.group(1))
                        break

                reply_count = "0"
                reply_btn = article.query_selector('button[data-testid="reply"]')
                if reply_btn:
                    raw_text = reply_btn.inner_text().strip()
                    if raw_text and re.search(r"\d", raw_text):
                        reply_count = expand_compact_number(raw_text)
                    else:
                        aria = reply_btn.get_attribute("aria-label") or ""
                        match = re.search(r"([\d,.]+(?:\.\d+)?\s*[KkMmBb]?)", aria)
                        if match:
                            reply_count = expand_compact_number(match.group(1))

                comments.append(
                    {
                        "author_name": str(sanitize_csv_cell(author_name)),
                        "author_handle": str(sanitize_csv_cell(author_handle)),
                        "content": str(sanitize_csv_cell(content)),
                        "time": str(sanitize_csv_cell(format_comment_time(comment_time))),
                        "likes": str(sanitize_csv_cell(like_count)),
                        "replies": str(sanitize_csv_cell(reply_count)),
                    }
                )
                log_line(log_callback, f"    [{len(comments)}/{max_count}] {author_handle}: {content[:40]}")
            except Exception as exc:
                log_line(log_callback, f"    解析评论时出错：{exc}")

        if boundary_hit:
            break

        if new_found == 0:
            no_new_count += 1
            if no_new_count >= no_new_scroll_limit:
                log_line(log_callback, f"  连续 {no_new_scroll_limit} 次滚动没有发现新评论，停止。")
                break
        else:
            no_new_count = 0

        if recommendation_boundary_visible(page):
            log_line(log_callback, "  页面已出现推荐区域，停止继续滚动。")
            break

        if len(comments) < max_count:
            page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
            interruptible_sleep(scroll_pause, stop_event)

    log_line(log_callback, f"  评论抓取完成：{len(comments)} 条。")
    return comments

def build_comment_rows(tweet_index: int, tweet_url: str, comments: list[dict[str, str]], top_limit=None) -> list[dict[str, str]]:
    if top_limit is None:
        top_limit = TOP_COMMENT_LIMIT
    top_comments = sorted(comments, key=lambda item: metric_to_int(item.get("likes", "0")), reverse=True)
    return [
        {
            "编号": str(tweet_index),
            "帖文链接": tweet_url,
            "点赞数": comment.get("likes", "0"),
            "评论内容": comment.get("content", ""),
            "评论发布时间": comment.get("time", ""),
        }
        for comment in top_comments[:top_limit]
    ]

def run_x_top_comments_spider(
    txt_path: str,
    cdp_port_or_url: str,
    max_comments: int,
    log_callback,
    finish_callback,
    stop_event=None,
    config=None,
    pause_event=None,
):
    if config is None:
        config = {}
    tweet_comment_top_limit = int(config.get("tweet_comment_top_limit", TOP_COMMENT_LIMIT))
    page_load_timeout_val = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    scroll_pause_val = float(config.get("scroll_pause", SCROLL_PAUSE))
    no_new_scroll_limit_val = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))

    completed_path = None
    page = None
    try:
        if sync_playwright is None:
            log_callback("缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        tweet_urls = parse_tweet_urls(txt_path)
        if not tweet_urls:
            log_callback("未读取到有效的 X/Twitter 推文链接。")
            return

        max_comments = max(tweet_comment_top_limit, int(max_comments or DEFAULT_SCAN_LIMIT))
        output_path = build_output_path("x", f"x_tweet_comments_{time.strftime('%Y%m%d')}.xlsx")
        writer = XlsxRowWriter(output_path, CSV_FIELDS)

        with sync_playwright() as playwright:
            log_callback("正在连接本地 Chrome...")
            try:
                _, context = connect_existing_chromium(playwright, cdp_port_or_url)
            except Exception as exc:
                log_callback(f"无法连接浏览器：{exc}")
                log_callback("连接失败：请确认 Chrome 已自动打开并已登录 X/Twitter。")
                return

            page = context.new_page()

            for index, tweet_url in enumerate(tweet_urls, 1):
                if should_stop(stop_event):
                    log_callback("任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                log_callback(f"[{index}/{len(tweet_urls)}] 读取推文：{tweet_url}")
                try:
                    page.goto(tweet_url, wait_until="domcontentloaded", timeout=page_load_timeout_val)
                    page.wait_for_selector('article[data-testid="tweet"]', timeout=page_load_timeout_val)
                    interruptible_sleep(3, stop_event)

                    comments = extract_comments(page, tweet_url, max_comments, log_callback, stop_event, scroll_pause=scroll_pause_val, no_new_scroll_limit=no_new_scroll_limit_val, pause_event=pause_event)
                    rows = build_comment_rows(index, tweet_url, comments, top_limit=tweet_comment_top_limit)
                    writer.writerows(rows)
                    writer.save()
                    log_callback(f"  完成：扫描主楼评论 {len(comments)} 条，写入点赞量最高的 {len(rows)} 条。")
                    if index % 5 == 0:
                        if random_cooldown(log_callback, stop_event, 3.0, 8.0):
                            break
                except PlaywrightTimeoutError:
                    log_callback("  跳过：页面加载超时，请确认链接可打开且账号已登录。")
                except Exception as exc:
                    log_callback(f"  跳过：{exc}")

            if page and not page.is_closed():
                page.close()

        completed_path = output_path
        writer.save()
        log_callback(f"完成，已保存：{output_path}")
    finally:
        try:
            if page and not page.is_closed():
                page.close()
        except Exception:
            pass
        finish_callback(completed_path)
