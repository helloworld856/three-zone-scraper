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
PROMOTED_MARKERS = ("promoted", "ad", "广告", "推广", "スポンサー", "pr", "赞助")

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
    for marker in PROMOTED_MARKERS:
        if len(marker) <= 2 and marker.isascii():
            # Short ASCII markers ("ad", "pr") need word boundary to avoid false positives
            if re.search(r'\b' + re.escape(marker) + r'\b', normalized):
                return True
        elif marker in normalized:
            return True
    return False

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

def _click_show_replies_buttons(page, stop_event=None) -> int:
    """Click all visible 'show replies' buttons to reveal hidden comments."""
    SHOW_REPLIES_TEXTS = (
        "show replies", "show reply", "显示回复", "显示回复内容",
        "返信を表示", "返信をすべて表示", "답글 보기",
        "voir les réponses", "antworten anzeigen", "mostrar respuestas",
        "mostrar respostas", "показать ответы",
        "show more replies", "显示更多回复", "もっと返信を表示",
    )
    clicked = 0
    for _ in range(10):
        if should_stop(stop_event):
            break
        try:
            found = page.evaluate("""(texts) => {
                let clicked = 0;
                const allBtns = document.querySelectorAll(
                    'button, div[role="button"], [data-testid="cellInnerDiv"] button, [data-testid="cellInnerDiv"] div[role="button"]'
                );
                for (const btn of allBtns) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    if (texts.some(t => txt === t || txt.startsWith(t))) {
                        btn.scrollIntoView({block: 'center'});
                        btn.click();
                        clicked++;
                    }
                }
                return clicked;
            }""", list(SHOW_REPLIES_TEXTS))
            if found == 0:
                break
            clicked += found
            interruptible_sleep(1.5, stop_event)
        except Exception:
            break
    return clicked



def extract_comments(page, tweet_url: str, max_count: int = DEFAULT_SCAN_LIMIT, log_callback=None, stop_event=None, scroll_pause=None, no_new_scroll_limit=None, pause_event=None) -> list[dict[str, str]]:
    if scroll_pause is None:
        scroll_pause = SCROLL_PAUSE
    if no_new_scroll_limit is None:
        no_new_scroll_limit = NO_NEW_SCROLL_LIMIT

    comments: list[dict[str, str]] = []
    seen_ids = set()
    no_new_count = 0
    show_replies_tried = 0

    log_line(log_callback, f"  开始抓取评论，目标 {max_count} 条。")

    # Wait for first-level comments (tabindex="0") to appear.
    try:
        page.wait_for_selector('article[data-testid="tweet"][tabindex="0"]', timeout=8000)
    except Exception:
        pass

    while len(comments) < max_count:
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        # Stop if a recommendation divider ("Discover more" etc.) has appeared.
        try:
            boundary_hit = page.evaluate("""() => {
                const markers = ['discover more', 'find more', '发现更多', '更多了解',
                    'もっと見る', '더 보기', 'encontrar más', 'descubre más'];
                const cells = document.querySelectorAll('[data-testid="cellInnerDiv"]');
                for (const cell of cells) {
                    if (cell.querySelector('article[data-testid="tweet"]')) continue;
                    const text = (cell.textContent || '').trim().toLowerCase();
                    if (markers.some(m => text.includes(m))) return true;
                }
                return false;
            }""")
            if boundary_hit:
                log_line(log_callback, "  已到达推荐区域，停止抓取。")
                break
        except Exception:
            pass

        # Identify first-level comments:
        # 1. tabindex="0" → actual comment (tabindex="-1" is the main tweet)
        # 2. Walk backward through cellInnerDiv siblings:
        #    - Empty cellInnerDiv → separator → first-level
        #    - Ad cell (article with tabindex≠0) → skip, keep walking
        #    - tabindex=0 article before separator → could be nested reply, or
        #      first-level comment with intervening ads. Use left indentation to decide:
        #      nested replies have ≥40px left padding; first-level comments have ~0.
        is_first_level = page.evaluate(
            """() => {
                const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
                const SEPARATOR_INDENT = 30;
                return articles.map(el => {
                    if (el.getAttribute('tabindex') !== '0') return false;
                    const cell = el.closest('[data-testid="cellInnerDiv"]');
                    if (!cell) return false;

                    let sibling = cell.previousElementSibling;
                    while (sibling && sibling.getAttribute('data-testid') === 'cellInnerDiv') {
                        const siblingArticle = sibling.querySelector('article[data-testid="tweet"]');
                        if (!siblingArticle) return true;  // empty cell → separator → first-level
                        const sibTab = siblingArticle.getAttribute('tabindex');
                        if (sibTab === '0') {
                            // Found another comment before finding separator.
                            // Check indentation: nested replies have left padding ≥ 40px.
                            try {
                                const cs = window.getComputedStyle(cell);
                                const leftIndent = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.marginLeft) || 0);
                                return leftIndent < SEPARATOR_INDENT;
                            } catch (_) { return false; }
                        }
                        // sibTab is '-1' or null → ad/promoted cell, skip and continue
                        sibling = sibling.previousElementSibling;
                    }
                    return false;
                });
            }"""
        )

        all_articles = page.query_selector_all('article[data-testid="tweet"]')

        new_found = 0

        for i, article in enumerate(all_articles):
            try:
                if i >= len(is_first_level) or not is_first_level[i]:
                    continue

                if is_promoted_tweet(article):
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

                comment_id = f"{user_text[:80]}|{comment_time}|{content[:120]}"
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
                    container = article.query_selector(f'[data-testid="{testid}"]')
                    if not container:
                        continue
                    # Try aria-label on the inner button/div first
                    inner_btn = container.query_selector('button, div[role="button"]')
                    if inner_btn:
                        aria = inner_btn.get_attribute("aria-label") or ""
                        match = re.search(r"([\d,.]+(?:\.\d+)?\s*[KkMmBb]?)", aria)
                        if match:
                            like_count = expand_compact_number(match.group(1))
                            break
                    # Fallback: get count from container text (the sibling span next to the icon)
                    raw_text = container.inner_text().strip()
                    if raw_text and re.search(r"\d", raw_text):
                        like_count = expand_compact_number(raw_text)
                        break

                reply_count = "0"
                for sel in ('[data-testid="reply"]',):
                    reply_container = article.query_selector(sel)
                    if not reply_container:
                        continue
                    inner_btn = reply_container.query_selector('button, div[role="button"]')
                    if inner_btn:
                        aria = inner_btn.get_attribute("aria-label") or ""
                        match = re.search(r"([\d,.]+(?:\.\d+)?\s*[KkMmBb]?)", aria)
                        if match:
                            reply_count = expand_compact_number(match.group(1))
                            break
                    raw_text = reply_container.inner_text().strip()
                    if raw_text and re.search(r"\d", raw_text):
                        reply_count = expand_compact_number(raw_text)
                        break

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

        if new_found == 0:
            if show_replies_tried < 3:
                extra = _click_show_replies_buttons(page, stop_event)
                show_replies_tried += 1
                if extra > 0:
                    log_line(log_callback, f"  点击了 {extra} 个「显示回复」按钮。")
                    interruptible_sleep(2.0, stop_event)
                    # Scroll down so revealed replies enter the viewport
                    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                    interruptible_sleep(scroll_pause, stop_event)
                    continue
            no_new_count += 1
            if no_new_count >= no_new_scroll_limit:
                log_line(log_callback, f"  连续 {no_new_scroll_limit} 次滚动没有发现新评论，停止。")
                break
        else:
            no_new_count = 0

        if len(comments) < max_count:
            # Scroll to bottom of page to trigger lazy loading of more comments
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            interruptible_sleep(scroll_pause, stop_event)

    log_line(log_callback, f"  评论抓取完成：{len(comments)} 条。")
    return comments

def build_comment_rows(tweet_index: int, tweet_url: str, comments: list[dict[str, str]], top_limit=None) -> list[dict[str, str]]:
    top_comments = sorted(comments, key=lambda item: metric_to_int(item.get("likes", "0")), reverse=True)
    if top_limit is not None:
        top_comments = top_comments[:top_limit]
    return [
        {
            "编号": str(tweet_index),
            "帖文链接": tweet_url,
            "点赞数": comment.get("likes", "0"),
            "评论内容": comment.get("content", ""),
            "评论发布时间": comment.get("time", ""),
        }
        for comment in top_comments
    ]

def run_x_top_comments_spider(
    txt_path: str,
    cdp_port_or_url: str,
    log_callback,
    finish_callback,
    stop_event=None,
    config=None,
    pause_event=None,
):
    if config is None:
        config = {}
    max_comments = int(config.get("comment_top_limit", TOP_COMMENT_LIMIT))
    page_load_timeout_val = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    scroll_pause_val = float(config.get("scroll_interval", SCROLL_PAUSE))
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

        output_path = build_output_path("x", f"x_top_comments_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
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
                    interruptible_sleep(2, stop_event)
                    page.wait_for_selector('article[data-testid="tweet"]', state="attached", timeout=page_load_timeout_val)
                    interruptible_sleep(3, stop_event)

                    comments = extract_comments(page, tweet_url, max_comments, log_callback, stop_event, scroll_pause=scroll_pause_val, no_new_scroll_limit=no_new_scroll_limit_val, pause_event=pause_event)
                    rows = build_comment_rows(index, tweet_url, comments)
                    writer.writerows(rows)
                    writer.save()
                    log_callback(f"  完成：抓取评论 {len(rows)} 条，按点赞量排序输出。")
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
