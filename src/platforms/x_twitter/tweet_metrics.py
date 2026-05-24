from __future__ import annotations

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
    random_cooldown,
    should_stop,
    wait_if_paused,
)
from src.platforms.x_twitter.comments import extract_comments


CSV_FIELDS = ["序号", "推文链接", "推文的内容", "浏览量", "评论数", "点赞量", "转发量"]
PAGE_LOAD_TIMEOUT = 30000
COOLDOWN_EVERY = 3
COOLDOWN_MIN_SECONDS = 3.0
COOLDOWN_MAX_SECONDS = 8.0
STATUS_RE = re.compile(r"/[^/?#]+/status/(\d+)")
NUMBER_RE = re.compile(r"(\d[\d,.]*(?:\.\d+)?\s*(?:[KkMmBb]|千|万|萬|亿|億)?)")


def log_line(log_callback, text: str):
    if log_callback:
        log_callback(text)


def clean_tweet_url(url: str) -> str:
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
            url = clean_tweet_url(stripped.split()[0])
            if "/status/" in url and url not in seen:
                urls.append(url)
                seen.add(url)
    return urls


def extract_status_id(url: str) -> str:
    match = STATUS_RE.search(clean_tweet_url(url))
    return match.group(1) if match else ""


def normalize_metric_text(text: str, default: str = "") -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        return default
    match = NUMBER_RE.search(value)
    return expand_compact_number(match.group(1).strip(), default=default) if match else default


def normalize_interaction_metric(text: str) -> str:
    return normalize_metric_text(text, default="0")


def article_has_status_id(article, status_id: str) -> bool:
    if not status_id:
        return False
    try:
        return bool(
            article.evaluate(
                """(article, statusId) => {
                    return Array.from(article.querySelectorAll('time')).some(time => {
                        const link = time.closest('a[href*="/status/"]');
                        return Boolean(link && link.href && link.href.includes(`/status/${statusId}`));
                    });
                }""",
                status_id,
            )
        )
    except Exception:
        return False


def find_target_article(page, status_id: str, page_timeout=None):
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    try:
        page.wait_for_selector('article[data-testid="tweet"], article', timeout=page_timeout)
    except Exception:
        return None

    try:
        articles = page.locator('article[data-testid="tweet"], article').all()
    except Exception:
        return None

    for article in articles:
        if article_has_status_id(article, status_id):
            return article
    return None


def extract_article_payload(article) -> dict[str, str]:
    return article.evaluate(
        """async (article) => {
            const firstText = selector => {
                const node = article.querySelector(selector);
                return node ? (node.innerText || node.textContent || '').trim() : '';
            };
            const firstMetric = selectors => {
                for (const selector of selectors) {
                    for (const node of article.querySelectorAll(selector)) {
                        const rawText = (node.innerText || node.textContent || '').trim();
                        const aria = (node.getAttribute('aria-label') || '').trim();
                        if (/\\d/.test(rawText)) return rawText;
                        if (/\\d/.test(aria)) return aria;
                    }
                }
                return '';
            };
            const nonTextContent = () => {
                const types = [];
                if (article.querySelector('[data-testid="tweetPhoto"], img[src*="/media/"]')) types.push('图片');
                if (article.querySelector('video')) types.push('视频');
                if ((article.innerText || '').split('\\n').some(line => line.trim().toLowerCase() === 'gif')) types.push('GIF');
                if (article.querySelector('[data-testid="card.wrapper"], [data-testid="card.layoutLarge.media"], [data-testid="card.layoutSmall.media"]')) types.push('卡片');
                return types.length ? `[${types.join('+')}]` : '[非文本]';
            };

            const tweetTextEl = article.querySelector('[data-testid="tweetText"]');
            if (tweetTextEl) {
                // Step 1: Revert auto-translation
                const revertTexts = ['view original', '查看原文', '原文を表示', 'show original', '原文を見る'];
                const allNodes = article.querySelectorAll('*');
                for (const node of allNodes) {
                    const nodeText = (node.textContent || '').trim().toLowerCase();
                    if (!nodeText || node.children.length > 0) continue;
                    if (revertTexts.includes(nodeText)) {
                        try { node.click(); } catch (_) {}
                        break;
                    }
                }
                // Step 2: Remove CSS truncation
                tweetTextEl.style.setProperty('max-height', 'none', 'important');
                tweetTextEl.style.setProperty('overflow', 'visible', 'important');
                tweetTextEl.style.setProperty('-webkit-line-clamp', 'unset', 'important');
                tweetTextEl.style.setProperty('display', 'block', 'important');
                tweetTextEl.style.setProperty('white-space', 'normal', 'important');
                // Step 3: Click "Show more" if present
                const expandTexts = ['show more', 'show more...', 'もっと見る', '더 보기'];
                for (const node of allNodes) {
                    const nodeText = (node.textContent || '').trim().toLowerCase();
                    if (!nodeText || node.children.length > 0) continue;
                    if (!expandTexts.includes(nodeText)) continue;
                    try { node.click(); } catch (_) {}
                    break;
                }
                // Wait for React to re-render with original text
                await new Promise(r => setTimeout(r, 400));
            }
            const content = firstText('[data-testid="tweetText"]') || nonTextContent();
            return {
                content,
                views: firstMetric([
                    'a[href*="/analytics"]',
                    'div[data-testid="postViewCount"]',
                    '[aria-label*="Views"]',
                    '[aria-label*="views"]',
                    '[aria-label*="浏览"]',
                ]),
                replies: firstMetric(['[data-testid="reply"]']),
                likes: firstMetric(['[data-testid="like"]', '[data-testid="unlike"]']),
                reposts: firstMetric(['[data-testid="retweet"]', '[data-testid="unretweet"]']),
            };
        }"""
    )


def collect_tweet_metrics(page, tweet_url: str, page_timeout=None, stop_event=None) -> dict[str, str]:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    normalized_url = clean_tweet_url(tweet_url)
    status_id = extract_status_id(normalized_url)
    if not status_id:
        raise ValueError("无法解析推文 ID")

    page.goto(normalized_url, wait_until="domcontentloaded", timeout=page_timeout)
    interruptible_sleep(2.5, stop_event)

    article = find_target_article(page, status_id, page_timeout=page_timeout)
    if article is None:
        raise RuntimeError("未找到目标推文 DOM")

    payload = extract_article_payload(article)
    return {
        "推文链接": normalized_url,
        "推文的内容": payload.get("content", ""),
        "浏览量": normalize_metric_text(payload.get("views", "")),
        "评论数": normalize_interaction_metric(payload.get("replies", "")),
        "点赞量": normalize_interaction_metric(payload.get("likes", "")),
        "转发量": normalize_interaction_metric(payload.get("reposts", "")),
    }


def run_x_tweet_metrics_spider(
    txt_path: str,
    get_comments_str: str,
    max_comments: int,
    cdp_port_or_url: str = DEFAULT_X_CDP_URL,
    log_callback=None,
    finish_callback=None,
    stop_event=None,
    config=None,
    pause_event=None,
):
    if config is None:
        config = {}
    page_load_timeout_val = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    tweet_comment_top_limit = int(config.get("tweet_comment_top_limit", 100))

    completed_path = None
    page = None
    try:
        if sync_playwright is None:
            log_line(log_callback, "缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        tweet_urls = parse_tweet_urls(txt_path)
        if not tweet_urls:
            log_line(log_callback, "TXT 中没有有效的推文链接。")
            return

        get_comments_bool = get_comments_str == "是"
        scan_limit = max(int(max_comments), tweet_comment_top_limit)

        output_path = build_output_path("x", f"x_tweet_metrics_{time.strftime('%Y%m%d')}.xlsx")
        if get_comments_bool:
            comment_fields = ["序号", "推文链接", "评论的点赞量", "评论内容", "评论发布时间"]
            writer = MultiSheetXlsxWriter(output_path, {"推文信息": CSV_FIELDS, "评论信息": comment_fields})
        else:
            writer = XlsxRowWriter(output_path, CSV_FIELDS)

        with sync_playwright() as playwright:
            log_line(log_callback, "正在连接本地 Chrome...")
            try:
                _, context = connect_existing_chromium(playwright, cdp_port_or_url)
            except Exception as exc:
                log_line(log_callback, f"无法连接浏览器：{exc}")
                log_line(log_callback, "连接失败：请确认 Chrome 已自动打开并已登录 X/Twitter。")
                return

            page = context.new_page()
            for index, tweet_url in enumerate(tweet_urls, 1):
                if should_stop(stop_event):
                    log_line(log_callback, "任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break

                normalized_url = clean_tweet_url(tweet_url)
                row = {
                    "序号": str(index),
                    "推文链接": normalized_url,
                    "推文的内容": "",
                    "浏览量": "",
                    "评论数": "",
                    "点赞量": "",
                    "转发量": "",
                }
                log_line(log_callback, f"[{index}/{len(tweet_urls)}] 读取推文：{normalized_url}")
                try:
                    row.update(collect_tweet_metrics(page, normalized_url, page_timeout=page_load_timeout_val, stop_event=stop_event))
                    
                    if get_comments_bool:
                        try:
                            comments = extract_comments(page, normalized_url, scan_limit, log_callback, stop_event, pause_event=pause_event)
                            comments.sort(key=lambda item: int(item.get("likes", "0") or 0), reverse=True)
                            for comment in comments[:tweet_comment_top_limit]:
                                comment_row = {
                                    "序号": row["序号"],
                                    "推文链接": normalized_url,
                                    "评论的点赞量": comment.get("likes", ""),
                                    "评论内容": comment.get("content", ""),
                                    "评论发布时间": comment.get("time", "")
                                }
                                writer.writerow("评论信息", comment_row)
                        except Exception as exc:
                            log_line(log_callback, f"  提取评论失败：{exc}")
                            
                except PlaywrightTimeoutError:
                    log_line(log_callback, "  页面加载超时，写入空指标行。")
                except Exception as exc:
                    log_line(log_callback, f"  处理失败，写入空指标行：{exc}")

                if get_comments_bool:
                    writer.writerow("推文信息", row)
                else:
                    writer.writerow(row)
                writer.save()
                log_line(log_callback, "  完成：已写入并保存。")
                if index < len(tweet_urls) and index % COOLDOWN_EVERY == 0:
                    if random_cooldown(log_callback, stop_event, COOLDOWN_MIN_SECONDS, COOLDOWN_MAX_SECONDS):
                        break

            if page and not page.is_closed():
                page.close()

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
