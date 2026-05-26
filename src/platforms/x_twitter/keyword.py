from __future__ import annotations

import queue
import random
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

from src.core import (
    XlsxRowWriter,
    MultiSheetXlsxWriter,
    build_output_path,
    connect_existing_chromium,
    ensure_chrome_for_cdp,
    expand_compact_number,
    interruptible_sleep,
    random_cooldown,
    sanitize_csv_row,
    sanitize_csv_rows,
    should_stop,
    wait_if_paused,
)
from src.platforms.x_twitter.comments import extract_comments

MAX_SEARCH_SCROLLS = 200
STATUS_PATH_RE = re.compile(r"(/[^/]+/status/\d+)")

CSV_FIELDS = [
    "原始搜索词",
    "完整搜索语法",
    "序号",
    "推文内容",
    "浏览量",
    "点赞量",
    "转发量",
    "评论数",
    "发帖时间",
    "推文链接",
    "标签",
]

def normalize_status_url(url: str) -> str:
    if not url:
        return ""
    normalized = url.strip().replace("twitter.com", "x.com")
    normalized = normalized.split("?")[0].split("#")[0]
    if normalized.startswith("//"):
        normalized = "https:" + normalized
    if normalized.startswith("/"):
        normalized = "https://x.com" + normalized
    if normalized and not normalized.startswith("http"):
        normalized = "https://" + normalized
    return normalized

def safe_text(locator, default: str = "") -> str:
    try:
        if locator.count() <= 0:
            return default
        return locator.first.inner_text(timeout=1500).strip() or default
    except Exception:
        return default

def safe_attr(locator, attr: str, default: str = "") -> str:
    try:
        if locator.count() <= 0:
            return default
        return locator.first.get_attribute(attr, timeout=1500) or default
    except Exception:
        return default

def collect_status_urls(article) -> list[str]:
    urls: list[str] = []
    seen = set()
    try:
        anchors = article.locator('a[href*="/status/"]').all()
    except Exception:
        return urls

    for anchor in anchors:
        try:
            href = anchor.get_attribute("href") or ""
        except Exception:
            continue
        match = STATUS_PATH_RE.search(href)
        if not match:
            continue
        normalized = normalize_status_url(match.group(1))
        if normalized and normalized not in seen:
            urls.append(normalized)
            seen.add(normalized)
    return urls

def is_repost_context(text: str) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in ["reposted", "repost", "retweeted", "转推", "转发", "リポスト"])

def get_social_context(article) -> str:
    return safe_text(article.locator('[data-testid="socialContext"]'))

def article_contains_nested_tweet(article) -> bool:
    status_urls = collect_status_urls(article)
    if len(status_urls) > 1:
        return True
    try:
        nested_articles = article.locator('article[data-testid="tweet"]').count()
        return nested_articles > 1
    except Exception:
        return False

def get_tweet_url(article) -> str:
    status_urls = collect_status_urls(article)
    return status_urls[0] if status_urls else ""

def get_tweet_text(article, stop_event=None) -> str:
    try:
        article.evaluate("""el => {
            // Step 1: Revert auto-translation — click "View original" / "查看原文" / "原文を表示"
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

            // Step 2: Remove CSS truncation to reveal full text
            const tweetText = el.querySelector('[data-testid="tweetText"]');
            if (!tweetText) return;
            tweetText.style.setProperty('max-height', 'none', 'important');
            tweetText.style.setProperty('overflow', 'visible', 'important');
            tweetText.style.setProperty('-webkit-line-clamp', 'unset', 'important');
            tweetText.style.setProperty('display', 'block', 'important');
            tweetText.style.setProperty('white-space', 'normal', 'important');

            // Step 3: Click "Show more" if present (for dynamic-load cases)
            const expandTexts = ['show more', 'show more...', 'もっと見る', '더 보기'];
            for (const node of allNodes) {
                const text = (node.textContent || '').trim().toLowerCase();
                if (!text || node.children.length > 0) continue;
                if (!expandTexts.includes(text)) continue;
                try { node.click(); } catch (_) {}
                break;
            }
        }""")
        interruptible_sleep(0.3, stop_event)
    except Exception:
        pass
    return safe_text(article.locator('[data-testid="tweetText"]'), default="无文字内容")

def get_tweet_time(article) -> str:
    raw_time = safe_attr(article.locator("time"), "datetime", default="")
    if not raw_time:
        return ""
    try:
        dt_obj = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        return dt_obj.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return raw_time

def extract_metric_value(locator, default: str = "未知") -> str:
    try:
        if locator.count() <= 0:
            return default
        node = locator.first
        raw_text = node.inner_text(timeout=1500).strip()
        if raw_text:
            return expand_compact_number(raw_text)
        aria = node.get_attribute("aria-label", timeout=1500) or ""
        match = re.search(r"(\d[\d,.]*[KMBkmb千万萬亿億]?)", aria)
        if match:
            return expand_compact_number(match.group(1))
        if aria:
            return "0"
    except Exception:
        pass
    return default

def extract_metric_from_article(article, selectors, default: str = "未知") -> str:
    if isinstance(selectors, str):
        selectors = [selectors]
    for selector in selectors:
        value = extract_metric_value(article.locator(selector), default="")
        if value:
            return value
    return default

def _x_media_tag(media_label: str) -> str:
    """Convert get_media_label output to numeric tag.
    0=图片+视频, 1=图片, 2=视频, 3=纯文本, 4=其它
    """
    has_video = "视频" in media_label
    has_photo = "图片" in media_label
    if has_video and has_photo:
        return "0"
    if has_photo:
        return "1"
    if has_video:
        return "2"
    if media_label:
        return "4"
    return "3"


def get_media_label(article) -> str:
    labels: list[str] = []
    video_selectors = [
        "video",
        '[data-testid="videoPlayer"]',
        '[aria-label*="Play"]',
        '[aria-label*="play"]',
        '[aria-label*="播放"]',
        '[aria-label*="再生"]',
    ]
    photo_selectors = [
        '[data-testid="tweetPhoto"]',
        'a[href*="/photo/"]',
        'img[src*="twimg.com/media"]',
        'div[aria-label*="Image"]',
        'div[aria-label*="图片"]',
        'div[aria-label*="画像"]',
    ]
    for selector in video_selectors:
        try:
            if article.locator(selector).count() > 0:
                labels.append("视频")
                break
        except Exception:
            continue
    for selector in photo_selectors:
        try:
            if article.locator(selector).count() > 0:
                labels.append("图片")
                break
        except Exception:
            continue
    if (article.inner_text() or "").split('\n')[0].strip().lower() == "gif":
        labels.append("GIF")
    return f"[{' + '.join(labels)}]" if labels else ""


def should_keep_article(article) -> bool:
    if is_repost_context(get_social_context(article)):
        return False
    if article_contains_nested_tweet(article):
        return False
    return True

def append_rows(writer, rows: list[dict], sheet_name: str = "推文信息"):
    if not rows:
        return
    sanitized = sanitize_csv_rows(rows)
    if hasattr(writer, "writerow") and hasattr(writer, "worksheets"):
        for row in sanitized:
            writer.writerow(sheet_name, row)
    else:
        writer.writerows(sanitized)

def build_search_query(base_keyword: str, adv_params: dict, since: str, until: str) -> str:
    query_parts = [base_keyword]
    if adv_params.get("lang", "any") != "any":
        query_parts.append(f"lang:{adv_params['lang']}")
    if since and until:
        query_parts.append(f"since:{since}")
        query_parts.append(f"until:{until}")
    return " ".join(query_parts)

def _make_keyword_log_callback(base_log_callback, keyword: str):
    """Wrap log_callback to prefix messages with [keyword] for disambiguation."""
    def log(msg: str) -> None:
        base_log_callback(f"[{keyword}] {msg}")
    return log


def _x_comment_consumer(keyword, queue_obj, cdp_port_or_url, writer, writer_lock,
                       log_callback, stop_event, pause_event, max_comments,
                       consumers_ready=None):
    """Consumer thread: creates its own Playwright connection + page, pops from queue."""
    log = _make_keyword_log_callback(log_callback, keyword)
    try:
        with sync_playwright() as p:
            _, context = connect_existing_chromium(p, cdp_port_or_url)
            comments_page = context.new_page()
            if consumers_ready is not None:
                consumers_ready.set()
            while True:
                item = queue_obj.get()
                if item is None:
                    break
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                serial_number, tweet_url, max_scan = item
                try:
                    comments_page.goto(tweet_url, wait_until="domcontentloaded", timeout=30000)
                    comments_page.wait_for_selector('article[data-testid="tweet"]', timeout=30000)
                    interruptible_sleep(2, stop_event)
                    comments = extract_comments(comments_page, tweet_url, max_scan, log,
                                                stop_event, pause_event=pause_event)
                    with writer_lock:
                        comment_count = 0
                        for comment in comments:
                            comment_row = {
                                "序号": str(serial_number),
                                "推文链接": tweet_url,
                                "评论的点赞量": comment.get("likes", ""),
                                "评论内容": comment.get("content", ""),
                                "评论发布时间": comment.get("time", ""),
                            }
                            writer.writerow("评论信息", sanitize_csv_row(comment_row))
                            comment_count += 1
                            if comment_count % 20 == 0:
                                writer.save()
                except Exception as exc:
                    log(f"    提取评论失败：{exc}")
    except Exception as exc:
        log(f"评论线程异常: {exc}")


def _scrape_single_x_keyword(base_keyword, adv_params, port,
                             log_callback, stop_event, pause_event,
                             search_page_timeout, scroll_cooldown_min, scroll_cooldown_max,
                             no_change_threshold, max_search_scrolls, slice_days,
                             max_comment_tabs, max_queue_size):
    """Scrape a single X keyword in this thread. Spawns comment consumer threads if needed."""
    log = _make_keyword_log_callback(log_callback, base_keyword)
    output_path = None
    writer = None
    writer_lock = None
    comment_queue = None
    comment_threads: list[threading.Thread] = []
    search_page = None
    try:
        if should_stop(stop_event):
            log("任务已停止。")
            return None
        if wait_if_paused(pause_event, stop_event):
            log("任务已停止。")
            return None

        limit_time_bool = adv_params.get("limit_time") == "是"
        get_comments_bool = adv_params.get("get_comments") == "是"
        max_comments = int(adv_params.get("max_comments", 500))

        safe_fn = re.sub(r'[\\/*?:"<>|]', "", base_keyword)
        output_path = build_output_path("x", f"x_keyword_{safe_fn}_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")

        log(f"\n{'=' * 50}")
        log(f"开始关键词：{base_keyword}")
        log(f"输出文件：{output_path}")

        if limit_time_bool:
            try:
                start_dt = datetime.strptime(adv_params["start_date"], "%Y-%m-%d")
                end_dt = datetime.strptime(adv_params["end_date"], "%Y-%m-%d") + timedelta(days=1)
            except ValueError:
                log("日期或切片格式错误：日期必须是 YYYY-MM-DD，切片天数必须是整数。")
                return None
            if start_dt >= end_dt:
                log("起始日期必须早于结束日期。")
                return None
        else:
            start_dt = datetime.now()
            end_dt = datetime.now()

        with sync_playwright() as p:
            _, context = connect_existing_chromium(p, port)
            search_page = context.new_page()

            if get_comments_bool:
                comment_fields = ["序号", "推文链接", "评论的点赞量", "评论内容", "评论发布时间"]
                writer = MultiSheetXlsxWriter(output_path, {"推文信息": CSV_FIELDS, "评论信息": comment_fields}, autosave_every=10)
                writer_lock = threading.Lock()
                comment_queue = queue.Queue(maxsize=max_queue_size)
                consumers_ready = threading.Event()
                for _ in range(max_comment_tabs):
                    t = threading.Thread(
                        target=_x_comment_consumer,
                        args=(base_keyword, comment_queue, port, writer, writer_lock,
                              log_callback, stop_event, pause_event, max_comments,
                              consumers_ready),
                        daemon=True,
                    )
                    t.start()
                    comment_threads.append(t)
            else:
                writer = XlsxRowWriter(output_path, CSV_FIELDS, autosave_every=10)

            seen_urls = set()
            total_count = 0
            current_end_dt = end_dt
            slice_index = 1

            while (limit_time_bool and current_end_dt > start_dt) or (not limit_time_bool and slice_index == 1):
                if should_stop(stop_event):
                    log("已请求停止，结束当前关键词。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break

                if limit_time_bool:
                    current_start_dt = max(start_dt, current_end_dt - timedelta(days=slice_days))
                    since = current_start_dt.strftime("%Y-%m-%d")
                    until = current_end_dt.strftime("%Y-%m-%d")
                    log(f"\n[切片 {slice_index}] {since} 至 {until}")
                else:
                    since = ""
                    until = ""
                    log("\n[搜索] 不限时间")

                final_query = build_search_query(base_keyword, adv_params, since, until)
                search_url = f"https://x.com/search?q={urllib.parse.quote(final_query)}&src=typed_query&f=top"
                log(f"搜索语法：{final_query}")

                try:
                    search_page.goto(search_url, wait_until="domcontentloaded", timeout=search_page_timeout)
                except Exception:
                    log("页面加载超时，继续尝试提取当前已加载内容。")

                if interruptible_sleep(random.uniform(4, 6), stop_event):
                    break
                slice_count = 0
                previous_count = -1
                no_change_strikes = 0
                buffer_rows: list[dict] = []

                for _ in range(max_search_scrolls):
                    if should_stop(stop_event):
                        break
                    if wait_if_paused(pause_event, stop_event):
                        break
                    try:
                        retry_btn = search_page.locator(
                            "button:has-text('Retry'), button:has-text('重试'), button:has-text('再試行')"
                        ).first
                        if retry_btn.count() > 0:
                            retry_btn.click(force=True)
                            if interruptible_sleep(3, stop_event):
                                break
                    except Exception:
                        pass

                    stop_outer = False
                    for article in search_page.locator('article[data-testid="tweet"]').all():
                        if should_stop(stop_event):
                            break
                        if wait_if_paused(pause_event, stop_event):
                            break
                        try:
                            if not should_keep_article(article):
                                continue

                            tweet_url = get_tweet_url(article)
                            if not tweet_url or tweet_url in seen_urls:
                                continue
                            seen_urls.add(tweet_url)

                            media_label = get_media_label(article)
                            row = {
                                "原始搜索词": base_keyword,
                                "完整搜索语法": final_query,
                                "序号": str(total_count + 1),
                                "推文内容": get_tweet_text(article, stop_event=stop_event) + media_label,
                                "浏览量": extract_metric_from_article(article, [
                                    'a[href*="/analytics"]',
                                    'div[data-testid="postViewCount"]',
                                    '[aria-label*="Views"]',
                                    '[aria-label*="views"]',
                                    '[aria-label*="浏览"]',
                                ]),
                                "点赞量": extract_metric_from_article(article, '[data-testid="like"], [data-testid="unlike"]'),
                                "转发量": extract_metric_from_article(article, '[data-testid="retweet"], [data-testid="unretweet"]'),
                                "评论数": extract_metric_from_article(article, '[data-testid="reply"]'),
                                "发帖时间": get_tweet_time(article),
                                "推文链接": tweet_url,
                                "标签": _x_media_tag(media_label),
                            }
                            buffer_rows.append(row)
                            total_count += 1
                            slice_count += 1

                            if get_comments_bool:
                                comment_str = row.get("评论数", "0")
                                if comment_str not in ("0", "未知", ""):
                                    if consumers_ready.wait(timeout=0.5):
                                        comment_queue.put((row["序号"], tweet_url, max_comments))
                                    else:
                                        log("    跳过评论采集：评论消费线程连接失败。")

                            if len(buffer_rows) >= 10:
                                if writer_lock:
                                    with writer_lock:
                                        append_rows(writer, buffer_rows)
                                else:
                                    append_rows(writer, buffer_rows)
                                log(f"  自动保存：累计 {total_count} 条含媒体原创推文。")
                                buffer_rows.clear()
                                if total_count and total_count % 20 == 0:
                                    if random_cooldown(log, stop_event, 3.0, 8.0):
                                        stop_outer = True
                                        break
                        except Exception as e:
                            log(f"  单条推文提取失败，已跳过：{e}")

                    if buffer_rows:
                        if writer_lock:
                            with writer_lock:
                                append_rows(writer, buffer_rows)
                        else:
                            append_rows(writer, buffer_rows)
                        buffer_rows.clear()

                    if slice_count == previous_count:
                        no_change_strikes += 1
                        if no_change_strikes >= no_change_threshold:
                            break
                    else:
                        no_change_strikes = 0
                    previous_count = slice_count

                    if not stop_outer:
                        search_page.mouse.wheel(delta_x=0, delta_y=random.randint(900, 1400))
                    if stop_outer or interruptible_sleep(random.uniform(scroll_cooldown_min, scroll_cooldown_max), stop_event):
                        break

                log(f"当前切片捕获 {slice_count} 条含媒体原创推文。")
                if limit_time_bool:
                    current_end_dt = current_start_dt
                slice_index += 1

            log(f"关键词完成：{base_keyword}，累计 {total_count} 条。")
            if comment_threads and comment_queue is not None:
                for _ in comment_threads:
                    comment_queue.put(None)
                for t in comment_threads:
                    t.join(timeout=120)

            writer.save()
            return output_path

    except Exception as exc:
        log(f"发生致命错误：{exc}")
        if writer is not None:
            try:
                writer.save()
            except Exception:
                pass
        return None
    finally:
        if comment_threads and comment_queue is not None:
            try:
                for _ in comment_threads:
                    comment_queue.put(None)
            except Exception:
                pass
            for t in comment_threads:
                if t.is_alive():
                    t.join(timeout=10)
        if search_page is not None and not search_page.is_closed():
            try:
                search_page.close()
            except Exception:
                pass


def run_x_spider(keywords_list, adv_params, port, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    if config is None:
        config = {}
    search_page_timeout = int(config.get("search_page_timeout", 40000))
    scroll_cooldown_min = float(config.get("cooldown_min", 5.0))
    scroll_cooldown_max = float(config.get("cooldown_max", 7.0))
    no_change_threshold = int(config.get("no_new_scroll_limit", 5))
    max_search_scrolls = int(config.get("max_scrolls", MAX_SEARCH_SCROLLS))
    slice_days = int(config.get("slice_days", 7))
    max_parallel_tabs = max(1, min(3, int(config.get("max_parallel_tabs", 1))))
    max_comment_tabs = max(1, min(3, int(config.get("max_comment_tabs", 1))))
    max_queue_size = max(10, min(10000, int(config.get("max_queue_size", 5000))))

    try:
        # pre-launch Chrome once before fanning out to threads
        ensure_chrome_for_cdp(port, log_callback=log_callback)

        get_comments_bool = adv_params.get("get_comments") == "是"
        if not get_comments_bool:
            log_callback("过滤规则：跳过转推、跳过引用/嵌套推文。\n")

        # --- sequential path ---
        output_path = None
        if max_parallel_tabs <= 1 or len(keywords_list) <= 1:
            for base_keyword in keywords_list:
                if should_stop(stop_event):
                    log_callback("任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                path = _scrape_single_x_keyword(
                    base_keyword, adv_params, port,
                    log_callback, stop_event, pause_event,
                    search_page_timeout, scroll_cooldown_min, scroll_cooldown_max,
                    no_change_threshold, max_search_scrolls, slice_days,
                    max_comment_tabs, max_queue_size,
                )
                if path:
                    output_path = path
            log_callback("\nX 关键词媒体推文搜索任务结束。")
            finish_callback(output_path)
            return

        # --- parallel path ---
        output_paths: list[str] = []
        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            future_to_keyword = {}
            for base_keyword in keywords_list:
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                future = executor.submit(
                    _scrape_single_x_keyword,
                    base_keyword, adv_params, port,
                    log_callback, stop_event, pause_event,
                    search_page_timeout, scroll_cooldown_min, scroll_cooldown_max,
                    no_change_threshold, max_search_scrolls, slice_days,
                    max_comment_tabs, max_queue_size,
                )
                future_to_keyword[future] = base_keyword

            for future in as_completed(future_to_keyword):
                keyword = future_to_keyword[future]
                try:
                    path = future.result()
                    if path:
                        output_paths.append(path)
                except Exception as exc:
                    log_callback(f"[{keyword}] 线程异常: {exc}")

        log_callback(f"\nX 关键词媒体推文搜索任务结束。{len(output_paths)}/{len(keywords_list)} 个成功。")
        for p in output_paths:
            log_callback(f"  {p}")
        finish_callback(output_paths[-1] if output_paths else None)

    except Exception as e:
        log_callback(f"发生致命错误：{e}")
        finish_callback()
