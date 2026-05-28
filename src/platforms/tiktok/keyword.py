from __future__ import annotations

import html as html_lib
import json
import queue
import random
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from playwright.sync_api import sync_playwright

from src.core import (
    XlsxRowWriter,
    MultiSheetXlsxWriter,
    build_output_path,
    connect_existing_chromium,
    ensure_chrome_for_cdp,
    expand_compact_number,
    extract_tiktok_video_title,
    interruptible_sleep,
    random_cooldown,
    resolve_tiktok_card_container,
    sanitize_csv_row,
    should_stop,
    wait_if_paused,
)
from src.platforms.tiktok.comments import collect_video_comments

CSV_FIELDS = [
    "搜索词",
    "序号",
    "视频标题",
    "播放量",
    "点赞数",
    "收藏量",
    "评论数",
    "发布时间",
    "视频链接",
    "博主主页链接",
    "标签",
]

def _tiktok_media_tag(item: dict, page=None) -> str:
    """Classify TikTok post media type from JSON item data, with DOM fallback.
    0=图片+视频, 1=图片, 2=视频, 3=纯文本, 4=其它
    """
    has_image = bool(item.get("image_post_info") or item.get("imagePost"))
    has_video = bool(item.get("video") or item.get("videoInfo"))
    if has_image and has_video:
        return "0"
    if has_image:
        return "1"
    if has_video:
        return "2"
    # JSON state empty or missing media keys — fall back to DOM
    if page is not None:
        try:
            dom_has_image = page.locator('[data-e2e="browse-image-item"], [class*="DivPhoto"], swiper, [class*="Swiper"]').count() > 0
            dom_has_video = page.locator("video, [data-e2e='video-player'], [class*='VideoPlayer']").count() > 0
            if dom_has_image and dom_has_video:
                return "0"
            if dom_has_image:
                return "1"
            if dom_has_video:
                return "2"
        except Exception:
            pass
    return "3"

DEFAULT_START_DATE = "2025-05-06"
DEFAULT_END_DATE = "2026-05-06"
MIN_SEARCH_SCROLLS = 60
MAX_SEARCH_SCROLLS = 360
SEARCH_SCROLL_PAUSE = 0.7
DEFAULT_CANDIDATE_MULTIPLIER = 3

def parse_date_range(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d")
    end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError("开始日期不能晚于结束日期")
    return start_dt, end_dt

def parse_publish_date(value: str) -> datetime | None:
    text = (value or "").strip()
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None

def in_date_range(publish_time: str, start_dt: datetime, end_dt: datetime) -> bool:
    publish_dt = parse_publish_date(publish_time)
    if not publish_dt:
        return False
    return start_dt.date() <= publish_dt.date() <= end_dt.date()

def clean_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://www.tiktok.com" + value
    if not value.startswith("http"):
        value = "https://" + value
    return value.split("?")[0].split("#")[0]

def safe_filename_part(value: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", value or "").strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:80] or "keyword"

def extract_author_url(video_url: str) -> str:
    match = re.search(r"tiktok\.com/(@[^/?#]+)/video/", video_url or "")
    return f"https://www.tiktok.com/{match.group(1)}" if match else ""

def extract_tiktok_video_id(url: str) -> str:
    match = re.search(r"/video/(\d+)", url or "")
    return match.group(1) if match else ""

def format_plain_text(value) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (dict, list, tuple)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"none", "null", "undefined", "nan"} else text

def format_count(value) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list, tuple)):
        return ""
    text = str(value).strip()
    if text.lower() in {"none", "null", "undefined", "nan"}:
        return ""
    return expand_compact_number(text)

def count_to_int(value) -> int:
    text = format_count(value).replace(",", "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0

def format_publish_time(value) -> str:
    try:
        timestamp = int(value)
        if timestamp > 0:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except Exception:
        pass
    return format_plain_text(value)

def iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)

def parse_script_json(html: str, script_id: str):
    pattern = rf'<script[^>]+id=["\']{re.escape(script_id)}["\'][^>]*>(.*?)</script>'
    match = re.search(pattern, html, re.S)
    if not match:
        return None
    try:
        return json.loads(html_lib.unescape(match.group(1)).strip())
    except Exception:
        return None

def page_state_sources(page) -> list[dict]:
    sources: list[dict] = []
    try:
        raw = page.evaluate(
            """() => JSON.stringify({
                sigi: window.SIGI_STATE || null,
                universal: window.__UNIVERSAL_DATA_FOR_REHYDRATION__ || null
            })"""
        )
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                sources.append(data)
    except Exception:
        pass

    try:
        html = page.content()
        for script_id in ("SIGI_STATE", "__UNIVERSAL_DATA_FOR_REHYDRATION__"):
            data = parse_script_json(html, script_id)
            if isinstance(data, dict):
                sources.append(data)
    except Exception:
        pass
    return sources

def find_item_in_state(sources: list[dict], video_id: str) -> dict:
    if not video_id:
        return {}
    for source in sources:
        for item_module_key in ("ItemModule", "itemModule"):
            item_module = source.get(item_module_key)
            if isinstance(item_module, dict):
                item = item_module.get(video_id)
                if isinstance(item, dict):
                    return item
        for node in iter_dicts(source):
            item_struct = node.get("itemStruct")
            if isinstance(item_struct, dict) and str(item_struct.get("id", "")) == video_id:
                return item_struct
            if str(node.get("id", "")) == video_id and ("stats" in node or "createTime" in node or "desc" in node):
                return node
    return {}

def item_metric(item: dict, *keys: str) -> str:
    stats_sources = []
    for key in ("stats", "statsV2", "stats_v2", "statistics"):
        value = item.get(key)
        if isinstance(value, dict):
            stats_sources.append(value)
    stats_sources.append(item)
    for source in stats_sources:
        for key in keys:
            if key in source:
                value = format_count(source.get(key))
                if value:
                    return value
    return ""

def item_metrics(item: dict) -> dict[str, str]:
    if not item:
        return {}
    return {
        "视频标题": format_plain_text(item.get("desc") or item.get("description")),
        "播放量": item_metric(item, "playCount", "play_count", "viewCount", "view_count", "play_count_str"),
        "点赞数": item_metric(item, "diggCount", "digg_count", "digg_count_str", "likeCount", "like_count", "like_count_str"),
        "收藏量": item_metric(item, "collectCount", "collect_count", "favoriteCount", "favouriteCount", "favorite_count", "favourite_count", "saveCount", "save_count"),
        "评论数": item_metric(item, "commentCount", "comment_count", "comments"),
        "发布时间": format_publish_time(item.get("createTime") or item.get("create_time")),
    }

def extract_metric(page, data_e2e_candidates, removable_words=(), default=""):
    candidates = data_e2e_candidates if isinstance(data_e2e_candidates, (list, tuple)) else [data_e2e_candidates]
    for data_e2e in candidates:
        try:
            loc = page.locator(f"[data-e2e='{data_e2e}']").first
            if loc.count() <= 0:
                continue
            text = loc.inner_text(timeout=2500).strip()
            for word in removable_words:
                text = text.replace(word, "")
            text = text.strip()
            if text:
                return expand_compact_number(text)
        except Exception:
            continue
    return default

def extract_publish_time(page) -> str:
    try:
        html = page.content()
        match = re.search(r'"createTime":"?(\d{10})"?', html)
        if match:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(match.group(1))))
    except Exception:
        pass

    for selector in [
        "span[data-e2e='browser-nickname'] + span + span",
        "span[data-e2e='video-create-time']",
        "time",
    ]:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0:
                text = loc.inner_text(timeout=1500).strip()
                if text:
                    return text
        except Exception:
            continue
    return ""

def extract_card_play_count(anchor) -> str:
    try:
        container = resolve_tiktok_card_container(anchor)
        for selector in [
            "[data-e2e='video-views']",
            "strong[data-e2e='video-views']",
            "span[data-e2e='video-views']",
        ]:
            node = container.query_selector(selector)
            if node:
                text = node.inner_text().strip()
                if text:
                    return expand_compact_number(text)
    except Exception:
        pass
    return ""

def dynamic_search_scroll_limit(max_videos: int, max_search_scrolls: int = MAX_SEARCH_SCROLLS) -> int:
    return min(max_search_scrolls, max(MIN_SEARCH_SCROLLS, max_videos // 8 + 40))

def default_candidate_scan_limit(max_videos: int) -> int:
    return max(max_videos, min(max_videos * DEFAULT_CANDIDATE_MULTIPLIER, max_videos + 3000))

def trigger_search_lazy_load(page):
    try:
        page.evaluate(
            """() => {
                const scrolling = document.scrollingElement || document.documentElement || document.body;
                scrolling.scrollTop = scrolling.scrollHeight;
                const scrollable = Array.from(document.querySelectorAll('body, main, section, div'))
                    .filter(el => {
                        const style = getComputedStyle(el);
                        return el.scrollHeight > el.clientHeight + 80 &&
                            ['auto', 'scroll', 'overlay'].includes(style.overflowY);
                    })
                    .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                for (const el of scrollable.slice(0, 6)) {
                    el.scrollTop = el.scrollHeight;
                    el.dispatchEvent(new Event('scroll', {bubbles: true}));
                }
                window.dispatchEvent(new Event('scroll'));
            }"""
        )
    except Exception:
        pass
    try:
        page.mouse.wheel(0, 4200)
    except Exception:
        pass
    try:
        page.keyboard.press("End")
    except Exception:
        pass

def collect_visible_video_items(page, seen_links: set[str]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    try:
        anchors = page.locator("a[href*='/video/'], a[href*='video/']").all()
    except Exception:
        anchors = []

    for anchor in anchors:
        try:
            href = clean_url(anchor.get_attribute("href") or "")
        except Exception:
            href = ""
        if href and "/video/" in href and href not in seen_links:
            items.append({"视频链接": href, "播放量": extract_card_play_count(anchor)})
            seen_links.add(href)
    return items

def open_search_page(page, keyword: str, stop_event=None):
    search_url = f"https://www.tiktok.com/search/video?q={urllib.parse.quote(keyword)}"
    page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
    interruptible_sleep(random.uniform(1.8, 2.8), stop_event)

def extract_video_row(page, keyword: str, video_url: str, play_count: str = "", stop_event=None) -> dict:
    page.goto(video_url, wait_until="domcontentloaded", timeout=25000)
    try:
        page.wait_for_selector("script#__UNIVERSAL_DATA_FOR_REHYDRATION__, script#SIGI_STATE, [data-e2e='like-count']", timeout=3500)
    except Exception:
        pass
    interruptible_sleep(random.uniform(0.25, 0.55), stop_event)
    item = find_item_in_state(page_state_sources(page), extract_tiktok_video_id(video_url))
    json_metrics = item_metrics(item)
    publish_time = json_metrics.get("发布时间") or extract_publish_time(page)
    play_value = json_metrics.get("播放量") or play_count
    dom_like_value = extract_metric(page, "like-count", ["Likes", "Like", "赞", " "])
    like_value = json_metrics.get("点赞数") or dom_like_value
    if play_value and like_value and count_to_int(play_value) == count_to_int(like_value):
        if dom_like_value and count_to_int(dom_like_value) != count_to_int(play_value):
            like_value = dom_like_value
    return {
        "搜索词": keyword,
        "序号": "",
        "视频标题": json_metrics.get("视频标题") or extract_tiktok_video_title(page),
        "播放量": play_value,
        "点赞数": like_value,
        "收藏量": json_metrics.get("收藏量") or extract_metric(page, ["favorite-count", "undefined-count"], ["Favorites", "Favorite", "Favourites", "Favourite", "收藏", " "]),
        "评论数": json_metrics.get("评论数") or extract_metric(page, "comment-count", ["Comments", "Comment", "评论", "評論", " "]),
        "发布时间": publish_time,
        "视频链接": video_url,
        "博主主页链接": extract_author_url(video_url),
        "标签": _tiktok_media_tag(item, page=page),
    }

def _make_keyword_log_callback(base_log_callback, keyword: str):
    """Wrap log_callback to prefix messages with [keyword] for disambiguation."""
    def log(msg: str) -> None:
        base_log_callback(f"[{keyword}] {msg}")
    return log


def _tiktok_comment_consumer(keyword, queue_obj, cdp_port_or_url, writer, writer_lock,
                             log_callback, stop_event, pause_event, comment_top_limit,
                             consumers_ready=None):
    """Consumer thread: creates its own Playwright connection and page, pops from queue."""
    log = _make_keyword_log_callback(log_callback, keyword)
    comments_page = None
    try:
        with sync_playwright() as p:
            try:
                _, context = connect_existing_chromium(p, cdp_port_or_url)
                comments_page = context.new_page()
            except Exception as exc:
                log(f"    评论线程连接浏览器失败: {exc}")
                return
            if consumers_ready is not None:
                consumers_ready.set()
            while True:
                try:
                    item = queue_obj.get(timeout=3)
                except Exception:
                    if should_stop(stop_event):
                        break
                    if wait_if_paused(pause_event, stop_event):
                        break
                    continue
                if item is None:
                    break
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                serial_number, video_url, max_scan = item
                try:
                    comments = collect_video_comments(
                        comments_page, video_url, max_scan, log,
                        stop_event, pause_event=pause_event,
                        comment_top_limit=comment_top_limit,
                    )
                    with writer_lock:
                        comment_count = 0
                        for comment in comments:
                            comment_row = {
                                "序号": str(serial_number),
                                "视频链接": video_url,
                                "评论的点赞量": comment.get("like_count", ""),
                                "评论内容": comment.get("text", ""),
                                "发布时间": comment.get("create_time", ""),
                            }
                            writer.writerow("评论信息", sanitize_csv_row(comment_row))
                            comment_count += 1
                            if comment_count % 20 == 0:
                                writer.save()
                except Exception as exc:
                    log(f"评论采集异常: {exc}")
    except Exception as exc:
        log(f"评论线程异常: {exc}")
    finally:
        if comments_page is not None:
            try:
                if not comments_page.is_closed():
                    comments_page.close()
            except Exception:
                pass


def _scrape_single_tiktok_keyword(keyword, keyword_index, total_keywords,
                                  max_videos, max_candidates, start_dt, end_dt,
                                  get_comments_bool, max_comments, max_comment_tabs,
                                  max_queue_size,
                                  cdp_port_or_url, log_callback, stop_event, pause_event,
                                  search_scroll_pause, config_max_search_scrolls,
                                  no_new_scroll_limit, comment_top_limit, run_stamp):
    """Scrape a single keyword in this thread. Spawns comment consumer threads if needed."""
    log = _make_keyword_log_callback(log_callback, keyword)
    output_path = None
    writer = None
    writer_lock = None
    comment_queue = None
    comment_threads: list[threading.Thread] = []
    search_page = metrics_page = None
    try:
        if should_stop(stop_event):
            log("任务已停止。")
            return None
        if wait_if_paused(pause_event, stop_event):
            log("任务已停止。")
            return None

        log(f"[{keyword_index}/{total_keywords}] 搜索关键词：{keyword}")
        output_path = build_output_path(
            "tiktok", f"tiktok_keyword_{safe_filename_part(keyword)}_{run_stamp}.xlsx",
        )
        log(f"  输出文件：{output_path}")
        if start_dt is not None:
            log(f"  日期范围：{start_dt.strftime('%Y-%m-%d')} 至 {end_dt.strftime('%Y-%m-%d')}")

        with sync_playwright() as p:
            _, context = connect_existing_chromium(p, cdp_port_or_url)
            search_page = context.new_page()
            metrics_page = context.new_page()

            if get_comments_bool:
                comment_fields = ["序号", "视频链接", "评论的点赞量", "评论内容", "发布时间"]
                writer = MultiSheetXlsxWriter(output_path, {"视频信息": CSV_FIELDS, "评论信息": comment_fields}, autosave_every=10)
                writer_lock = threading.Lock()
                comment_queue = queue.Queue(maxsize=max_queue_size)
                consumers_ready = threading.Event()
                for _ in range(max_comment_tabs):
                    t = threading.Thread(
                        target=_tiktok_comment_consumer,
                        args=(keyword, comment_queue, cdp_port_or_url, writer, writer_lock,
                              log_callback, stop_event, pause_event, comment_top_limit,
                              consumers_ready),
                        daemon=True,
                    )
                    t.start()
                    comment_threads.append(t)
            else:
                writer = XlsxRowWriter(output_path, CSV_FIELDS, autosave_every=10)

            serial_number = 1
            open_search_page(search_page, keyword, stop_event=stop_event)
            scroll_limit = dynamic_search_scroll_limit(max_videos, config_max_search_scrolls)
            seen_links: set[str] = set()
            scanned_count = 0
            no_new_visible_rounds = 0
            log("  开始边滚动边提取详情并按日期过滤")

            written_count = 0
            for scroll_index in range(scroll_limit):
                if should_stop(stop_event):
                    log("  已请求停止，结束当前关键词。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                new_items = collect_visible_video_items(search_page, seen_links)
                if not new_items:
                    no_new_visible_rounds += 1
                else:
                    no_new_visible_rounds = 0

                for video_item in new_items:
                    if should_stop(stop_event):
                        break
                    if wait_if_paused(pause_event, stop_event):
                        break
                    if written_count >= max_videos:
                        break
                    if scanned_count >= max_candidates:
                        break
                    scanned_count += 1
                    try:
                        video_url = video_item["视频链接"]
                        log(f"  [候选{scanned_count}/已写{written_count}] {video_url}")
                        row = extract_video_row(metrics_page, keyword, video_url, video_item.get("播放量", ""), stop_event=stop_event)

                        if start_dt is not None:
                            if not in_date_range(row["发布时间"], start_dt, end_dt):
                                log(f"    跳过：发布时间不在范围内（{row['发布时间'] or '未解析'}）")
                                continue

                        row["序号"] = str(serial_number)

                        if get_comments_bool:
                            with writer_lock:
                                writer.writerow("视频信息", sanitize_csv_row(row))
                            if count_to_int(row.get("评论数", "0")) > 0:
                                if consumers_ready.wait(timeout=0.5):
                                    try:
                                        comment_queue.put(
                                            (serial_number, video_url, max_comments),
                                            block=True,
                                            timeout=15,
                                        )
                                    except Exception:
                                        log("    评论队列已满或消费线程异常，跳过本条评论采集。")
                                else:
                                    log("    跳过评论采集：评论消费线程连接失败。")
                        else:
                            writer.writerow(sanitize_csv_row(row))

                        serial_number += 1
                        written_count += 1
                    except Exception as exc:
                        log(f"    跳过：{exc}")
                    if scanned_count and scanned_count % 20 == 0:
                        if random_cooldown(log, stop_event, 3.0, 8.0):
                            break

                if written_count >= max_videos:
                    break
                if scanned_count >= max_candidates:
                    log(f"  已检查 {scanned_count} 个候选，达到候选检查上限，停止当前关键词。")
                    break
                if no_new_visible_rounds >= no_new_scroll_limit and scroll_index >= 20:
                    log("  连续多轮没有新视频链接，停止当前关键词。")
                    break
                if scroll_index and scroll_index % 10 == 0:
                    log(f"  已滚动 {scroll_index}/{scroll_limit} 轮，已扫描 {scanned_count} 个候选，写入 {written_count} 条")

                trigger_search_lazy_load(search_page)
                interruptible_sleep(search_scroll_pause, stop_event)

            log(f"  写入 {written_count} 条日期范围内的视频")
            if comment_threads and comment_queue is not None:
                for _ in comment_threads:
                    comment_queue.put(None)
                for t in comment_threads:
                    t.join(timeout=120)

            writer.save()
            return output_path

    except Exception as exc:
        log(f"运行失败：{exc}")
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
        for pg in (search_page, metrics_page):
            if pg is not None and not pg.is_closed():
                try:
                    pg.close()
                except Exception:
                    pass


def run_tiktok_spider(keywords_list, max_videos, max_candidates, limit_time_str, start_date, end_date, get_comments_str, max_comments, cdp_port_or_url, log_callback, finish_callback, stop_event=None, pause_event=None, config=None):
    if config is None:
        config = {}
    search_scroll_pause = float(config.get("scroll_interval", SEARCH_SCROLL_PAUSE))
    config_max_search_scrolls = int(config.get("max_search_scrolls", MAX_SEARCH_SCROLLS))
    no_new_scroll_limit = int(config.get("no_new_scroll_limit", 12))
    comment_top_limit = int(config.get("comment_top_limit", 100))
    max_parallel_tabs = max(1, min(3, int(config.get("max_parallel_tabs", 1))))
    max_comment_tabs = max(1, min(3, int(config.get("max_comment_tabs", 1))))
    max_queue_size = max(10, min(10000, int(config.get("max_queue_size", 5000))))

    output_paths: list[str] = []
    try:
        limit_time_bool = limit_time_str == "是"
        get_comments_bool = get_comments_str == "是"
        start_dt, end_dt = None, None
        if limit_time_bool:
            start_dt, end_dt = parse_date_range(start_date, end_date)

        run_stamp = time.strftime("%Y%m%d_%H%M%S")

        # pre-launch Chrome once before fanning out to threads
        ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)

        # --- sequential path (1 keyword or max_parallel_tabs == 1) ---
        if max_parallel_tabs <= 1 or len(keywords_list) <= 1:
            for idx, keyword in enumerate(keywords_list, 1):
                if should_stop(stop_event):
                    log_callback("任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                path = _scrape_single_tiktok_keyword(
                    keyword, idx, len(keywords_list),
                    max_videos, max_candidates,
                    start_dt, end_dt,
                    get_comments_bool, max_comments, max_comment_tabs,
                    max_queue_size,
                    cdp_port_or_url,
                    log_callback, stop_event, pause_event,
                    search_scroll_pause, config_max_search_scrolls,
                    no_new_scroll_limit, comment_top_limit,
                    run_stamp,
                )
                if path:
                    output_paths.append(path)

            log_callback("完成，已按关键词分别保存：")
            for p in output_paths:
                log_callback(f"  {p}")
            finish_callback(output_paths[-1] if output_paths else None)
            return

        # --- parallel path ---
        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            future_to_keyword = {}
            for idx, keyword in enumerate(keywords_list, 1):
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                future = executor.submit(
                    _scrape_single_tiktok_keyword,
                    keyword, idx, len(keywords_list),
                    max_videos, max_candidates,
                    start_dt, end_dt,
                    get_comments_bool, max_comments, max_comment_tabs,
                    max_queue_size,
                    cdp_port_or_url,
                    log_callback, stop_event, pause_event,
                    search_scroll_pause, config_max_search_scrolls,
                    no_new_scroll_limit, comment_top_limit,
                    run_stamp,
                )
                future_to_keyword[future] = keyword

            for future in as_completed(future_to_keyword):
                keyword = future_to_keyword[future]
                try:
                    path = future.result()
                    if path:
                        output_paths.append(path)
                except Exception as exc:
                    log_callback(f"[{keyword}] 线程异常: {exc}")

        log_callback(f"全部关键词处理完毕。{len(output_paths)}/{len(keywords_list)} 个成功。")
        for p in output_paths:
            log_callback(f"  {p}")
        finish_callback(output_paths[-1] if output_paths else None)

    except Exception as exc:
        log_callback(f"运行失败：{exc}")
        finish_callback(None)
