from __future__ import annotations

import html as html_lib
import json
import random
import re
import time
from datetime import datetime

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from src.core import (
    XlsxRowWriter,
    MultiSheetXlsxWriter,
    build_output_path,
    connect_existing_chromium,
    interruptible_sleep,
    sanitize_csv_row,
    should_stop,
    wait_if_paused,
)
from src.core import expand_compact_number, extract_tiktok_video_title
from src.platforms.tiktok.comments import collect_video_comments


CSV_FIELDS = ["序号", "视频链接", "发布日期", "视频简介", "点赞数", "评论数", "收藏量", "分享数"]
PAGE_LOAD_TIMEOUT = 45000
DETAIL_LOAD_TIMEOUT = 30000
SCROLL_INTERVAL_SECONDS = 2.5
DETAIL_DELAY_MIN_SECONDS = 2.0
DETAIL_DELAY_MAX_SECONDS = 5.0
LINK_BATCH_SIZE = 50
SAVE_BATCH_SIZE = 10
BATCH_WAIT_MIN_SECONDS = 10.0
BATCH_WAIT_MAX_SECONDS = 20.0
NO_NEW_SCROLL_LIMIT = 10
DEFAULT_MAX_SCROLLS = 500
SCROLL_PX = 3600


def parse_date_range(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d")
    end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError("开始日期不能晚于结束日期。")
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


def log_line(log_callback, message: str) -> None:
    if log_callback:
        log_callback(message)


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
    return value.split("?")[0].split("#")[0].rstrip("/")


def normalize_profile_url(url: str) -> str:
    cleaned = clean_url(url)
    match = re.search(r"tiktok\.com/(@[^/?#]+)", cleaned)
    return f"https://www.tiktok.com/{match.group(1)}" if match else ""


def parse_profile_urls(txt_path: str) -> list[str]:
    urls: list[str] = []
    seen = set()
    with open(txt_path, "r", encoding="utf-8-sig") as file:
        for line in file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for part in re.split(r"\s+", stripped):
                profile_url = normalize_profile_url(part)
                if profile_url and profile_url not in seen:
                    urls.append(profile_url)
                    seen.add(profile_url)
                    break
    return urls


def parse_video_id(video_url: str) -> str:
    match = re.search(r"/video/(\d+)", video_url or "")
    return match.group(1) if match else ""


def normalize_video_url(url: str) -> str:
    value = (url or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://www.tiktok.com" + value
    if not value.startswith("http"):
        return ""
    value = value.split("?")[0].split("#")[0].rstrip("/")
    if not re.search(r"tiktok\.com/@[^/?#]+/video/\d+", value):
        return ""
    return value


def trigger_profile_lazy_load(page) -> None:
    try:
        page.evaluate(
            f"""() => {{
                const scrolling = document.scrollingElement || document.documentElement || document.body;
                scrolling.scrollBy(0, {SCROLL_PX});
                const scrollable = Array.from(document.querySelectorAll('body, main, section, div'))
                    .filter(el => {{
                        const style = getComputedStyle(el);
                        return el.scrollHeight > el.clientHeight + 80 &&
                            ['auto', 'scroll', 'overlay'].includes(style.overflowY);
                    }})
                    .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                for (const el of scrollable.slice(0, 6)) {{
                    el.scrollBy(0, {SCROLL_PX});
                    el.dispatchEvent(new Event('scroll', {{ bubbles: true }}));
                }}
                window.dispatchEvent(new Event('scroll'));
            }}"""
        )
    except Exception:
        pass
    try:
        page.mouse.wheel(0, SCROLL_PX)
    except Exception:
        pass


def collect_visible_video_links(page, seen: set[str]) -> list[str]:
    links: list[str] = []
    try:
        anchors = page.locator("a[href*='/video/']").all()
    except Exception:
        anchors = []

    for anchor in anchors:
        try:
            href = normalize_video_url(anchor.get_attribute("href") or "")
        except Exception:
            href = ""
        if href and href not in seen:
            seen.add(href)
            links.append(href)
    return links


def item_detail_from_state(page, video_url: str) -> dict:
    video_id = parse_video_id(video_url)
    return find_item_in_state(page_state_sources(page), video_id)


def extract_video_detail(page, video_url: str) -> dict[str, str]:
    page.goto(video_url, wait_until="domcontentloaded", timeout=DETAIL_LOAD_TIMEOUT)
    try:
        page.wait_for_selector(
            "script#__UNIVERSAL_DATA_FOR_REHYDRATION__, script#SIGI_STATE, [data-e2e='like-count']",
            timeout=5000,
        )
    except Exception:
        pass

    item = item_detail_from_state(page, video_url)
    desc = format_plain_text(item.get("desc") or item.get("description")) if item else ""
    publish_time = format_publish_time(item.get("createTime") or item.get("create_time")) if item else ""
    likes = item_metric(item, "diggCount", "digg_count", "digg_count_str", "likeCount", "like_count", "like_count_str") if item else ""
    comments = item_metric(item, "commentCount", "comment_count", "comments") if item else ""
    collects = item_metric(
        item,
        "collectCount",
        "collect_count",
        "favoriteCount",
        "favouriteCount",
        "favorite_count",
        "favourite_count",
        "saveCount",
        "save_count",
    ) if item else ""
    shares = item_metric(
        item,
        "shareCount",
        "share_count",
        "share_count_str",
        "shares",
    ) if item else ""

    if not desc:
        desc = extract_tiktok_video_title(page)
    if not likes:
        likes = extract_metric(page, "like-count", ["Likes", "Like", "赞", " "])
    if not comments:
        comments = extract_metric(page, "comment-count", ["Comments", "Comment", "评论", " "])
    if not collects:
        collects = extract_metric(
            page,
            ["favorite-count", "undefined-count"],
            ["Favorites", "Favorite", "Favourites", "Favourite", "收藏", " "],
        )
    if not shares:
        shares = extract_metric(
            page,
            "share-count",
            ["Shares", "Share", "分享", " "],
        )

    return {
        "video_url": video_url,
        "desc": desc,
        "published_at": publish_time,
        "likes": format_count(likes),
        "comments": format_count(comments),
        "collects": format_count(collects),
        "shares": format_count(shares),
    }


def row_from_detail(index: int, detail: dict[str, str]) -> dict[str, str]:
    return {
        "序号": str(index),
        "视频链接": detail.get("video_url", ""),
        "发布日期": detail.get("published_at", ""),
        "视频简介": detail.get("desc", ""),
        "点赞数": detail.get("likes", ""),
        "评论数": detail.get("comments", ""),
        "收藏量": detail.get("collects", ""),
        "分享数": detail.get("shares", ""),
    }


def wait_after_detail(log_callback, stop_event=None, pause_event=None) -> bool:
    if wait_if_paused(pause_event, stop_event):
        return True
    seconds = random.uniform(DETAIL_DELAY_MIN_SECONDS, DETAIL_DELAY_MAX_SECONDS)
    return interruptible_sleep(seconds, stop_event)


def process_video_batch(
    detail_page,
    video_links: list[str],
    start_dt: datetime | None,
    end_dt: datetime | None,
    limit_time_bool: bool,
    get_video_info_bool: bool,
    get_comments_bool: bool,
    max_comments: int,
    writer,
    serial_number: int,
    written_count: int,
    log_callback,
    stop_event=None,
    pause_event=None,
    save_batch_size: int = SAVE_BATCH_SIZE,
    batch_wait_min: float = BATCH_WAIT_MIN_SECONDS,
    batch_wait_max: float = BATCH_WAIT_MAX_SECONDS,
) -> tuple[int, int, bool]:
    stop_profile = False
    batch_written = 0
    log_line(log_callback, f"  开始爬取本批 {len(video_links)} 条视频。")

    for batch_index, video_url in enumerate(video_links, 1):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        try:
            log_line(log_callback, f"    [{batch_index}/{len(video_links)}] 读取视频：{video_url}")

            detail = {"video_url": video_url}
            if get_video_info_bool or get_comments_bool or limit_time_bool:
                detail = extract_video_detail(detail_page, video_url)
                published_at = detail.get("published_at", "")

                if limit_time_bool and start_dt and end_dt:
                    publish_dt = parse_publish_date(published_at)
                    if publish_dt and publish_dt.date() < start_dt.date():
                        log_line(log_callback, f"      停止当前主页：视频发布时间早于开始日期（{published_at}）。")
                        stop_profile = True
                        wait_after_detail(log_callback, stop_event, pause_event=pause_event)
                        break

                    if not in_date_range(published_at, start_dt, end_dt):
                        log_line(log_callback, f"      跳过：发布时间不在范围内（{published_at or '未解析'}）。")
                        if wait_after_detail(log_callback, stop_event, pause_event=pause_event):
                            break
                        continue

            row_base = row_from_detail(serial_number, detail) if get_video_info_bool else {"序号": str(serial_number), "视频链接": video_url}

            if get_comments_bool:
                comments = collect_video_comments(detail_page, video_url, max_comments, log_callback, stop_event, pause_event=pause_event)
                writer.writerow("视频信息", sanitize_csv_row(row_base))
                for comment in comments:
                    comment_row = {
                        "序号": str(serial_number),
                        "视频链接": video_url,
                        "评论的点赞量": comment.get("like_count", ""),
                        "评论内容": comment.get("text", ""),
                        "发布时间": comment.get("create_time", "")
                    }
                    writer.writerow("评论信息", sanitize_csv_row(comment_row))

                written_count += 1
                batch_written += 1
                log_line(
                    log_callback,
                    f"      写入：点赞 {detail.get('likes') or '空'}，评论 {detail.get('comments') or '空'}，收藏 {detail.get('collects') or '空'}，分享 {detail.get('shares') or '空'}，抓取到主楼评论 {len(comments)} 条。",
                )
            else:
                writer.writerow(sanitize_csv_row(row_base))
                written_count += 1
                batch_written += 1
                if get_video_info_bool:
                    log_line(
                        log_callback,
                        f"      写入：点赞 {detail.get('likes') or '空'}，评论 {detail.get('comments') or '空'}，收藏 {detail.get('collects') or '空'}，分享 {detail.get('shares') or '空'}。",
                    )
                else:
                    log_line(log_callback, f"      写入视频链接：{video_url}")

            serial_number += 1
            if batch_written >= save_batch_size:
                if wait_if_paused(pause_event, stop_event):
                    break
                seconds = random.uniform(batch_wait_min, batch_wait_max)
                log_line(log_callback, f"    已写入 {written_count} 条，随机等待 {seconds:.1f} 秒。")
                if interruptible_sleep(seconds, stop_event):
                    break
                batch_written = 0
        except Exception as exc:
            log_line(log_callback, f"      跳过：{exc}")

        if wait_after_detail(log_callback, stop_event, pause_event=pause_event):
            break

    return serial_number, written_count, stop_profile


def run_tiktok_profile_videos_spider(
    txt_path: str,
    start_date: str,
    end_date: str,
    limit_time_str: str,
    max_scrolls: int,
    get_video_info_str: str,
    get_comments_str: str,
    max_comments: int,
    cdp_port_or_url: str,
    log_callback,
    finish_callback,
    stop_event=None,
    pause_event=None,
    config=None,
):
    if config is None:
        config = {}
    page_load_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    scroll_interval = float(config.get("scroll_interval", SCROLL_INTERVAL_SECONDS))
    no_new_scroll_limit = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
    max_scrolls = int(config.get("max_scrolls", max_scrolls))
    link_batch_size = int(config.get("link_batch_size", LINK_BATCH_SIZE))
    save_batch_size = int(config.get("save_batch_size", SAVE_BATCH_SIZE))
    batch_wait_min = float(config.get("batch_wait_min", BATCH_WAIT_MIN_SECONDS))
    batch_wait_max = float(config.get("batch_wait_max", BATCH_WAIT_MAX_SECONDS))

    output_path = None
    completed_path = None
    try:
        if sync_playwright is None:
            log_line(log_callback, "缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        profile_urls = parse_profile_urls(txt_path)
        if not profile_urls:
            log_line(log_callback, "TXT 中没有找到有效的 TikTok 博主主页链接。")
            return

        limit_time_bool = (limit_time_str == "是")
        get_video_info_bool = (get_video_info_str == "是")
        get_comments_bool = (get_comments_str == "是")

        start_dt = None
        end_dt = None
        if limit_time_bool:
            start_dt, end_dt = parse_date_range(start_date, end_date)

        video_fields = ["序号", "视频链接"]
        if get_video_info_bool:
            video_fields.extend(["发布日期", "视频简介", "点赞数", "评论数", "收藏量", "分享数"])

        output_path = build_output_path("tiktok", f"tiktok_profile_videos_{time.strftime('%Y%m%d')}.xlsx")
        if get_comments_bool:
            comment_fields = ["序号", "视频链接", "评论的点赞量", "评论内容", "发布时间"]
            writer = MultiSheetXlsxWriter(output_path, {"视频信息": video_fields, "评论信息": comment_fields})
        else:
            writer = XlsxRowWriter(output_path, video_fields)

        written_count = 0
        serial_number = 1
        
        actual_max_scrolls = max_scrolls if max_scrolls > 0 else 999999
        no_new_limit = 5 if not limit_time_bool else no_new_scroll_limit

        with sync_playwright() as playwright:
            log_line(log_callback, "正在连接本地 Chrome，请确认已登录 TikTok。")
            try:
                _, context = connect_existing_chromium(playwright, cdp_port_or_url, log_callback=log_callback)
            except Exception as exc:
                log_line(log_callback, f"连接失败：请确认 Chrome 已打开并已登录 TikTok。错误：{exc}")
                return

            profile_page = context.new_page()
            detail_page = context.new_page()

            for profile_index, raw_profile_url in enumerate(profile_urls, 1):
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                profile_url = normalize_profile_url(raw_profile_url)
                if not profile_url:
                    log_line(log_callback, f"[{profile_index}/{len(profile_urls)}] 跳过无效主页：{raw_profile_url}")
                    continue

                log_line(log_callback, f"[{profile_index}/{len(profile_urls)}] 读取主页：{profile_url}")
                try:
                    profile_page.goto(profile_url, wait_until="domcontentloaded", timeout=page_load_timeout)
                    interruptible_sleep(2.5, stop_event)
                except PlaywrightTimeoutError:
                    log_line(log_callback, "  主页加载超时，跳过。")
                    continue

                seen_links: set[str] = set()
                pending_links: list[str] = []
                no_new_count = 0
                stop_profile = False

                for scroll_index in range(actual_max_scrolls):
                    if should_stop(stop_event):
                        break
                    if wait_if_paused(pause_event, stop_event):
                        break

                    new_links = collect_visible_video_links(profile_page, seen_links)
                    if new_links:
                        no_new_count = 0
                        log_line(log_callback, f"  滚动 {scroll_index + 1}/{actual_max_scrolls}：发现 {len(new_links)} 条新视频链接。")
                        pending_links.extend(new_links)
                    else:
                        no_new_count += 1

                    while len(pending_links) >= link_batch_size and not stop_profile and not should_stop(stop_event):
                        batch = pending_links[:link_batch_size]
                        del pending_links[:link_batch_size]
                        serial_number, written_count, stop_profile = process_video_batch(
                            detail_page,
                            batch,
                            start_dt,
                            end_dt,
                            limit_time_bool,
                            get_video_info_bool,
                            get_comments_bool,
                            max_comments,
                            writer,
                            serial_number,
                            written_count,
                            log_callback,
                            stop_event,
                            pause_event=pause_event,
                            save_batch_size=save_batch_size,
                            batch_wait_min=batch_wait_min,
                            batch_wait_max=batch_wait_max,
                        )
                    if stop_profile:
                        break

                    if no_new_count >= no_new_limit:
                        if pending_links and not should_stop(stop_event):
                            serial_number, written_count, stop_profile = process_video_batch(
                                detail_page,
                                pending_links,
                                start_dt,
                                end_dt,
                                limit_time_bool,
                                get_video_info_bool,
                                get_comments_bool,
                                max_comments,
                                writer,
                                serial_number,
                                written_count,
                                log_callback,
                                stop_event,
                                pause_event=pause_event,
                                save_batch_size=save_batch_size,
                                batch_wait_min=batch_wait_min,
                                batch_wait_max=batch_wait_max,
                            )
                            pending_links = []
                        log_line(log_callback, "  连续多次没有新视频链接，结束当前主页。")
                        break

                    trigger_profile_lazy_load(profile_page)
                    if interruptible_sleep(scroll_interval, stop_event):
                        break

                if pending_links and not stop_profile and not should_stop(stop_event):
                    serial_number, written_count, stop_profile = process_video_batch(
                        detail_page,
                        pending_links,
                        start_dt,
                        end_dt,
                        limit_time_bool,
                        get_video_info_bool,
                        get_comments_bool,
                        max_comments,
                        writer,
                        serial_number,
                        written_count,
                        log_callback,
                        stop_event,
                        pause_event=pause_event,
                        save_batch_size=save_batch_size,
                        batch_wait_min=batch_wait_min,
                        batch_wait_max=batch_wait_max,
                    )

            for opened_page in (profile_page, detail_page):
                if not opened_page.is_closed():
                    opened_page.close()

        writer.save()
        completed_path = output_path
        log_line(log_callback, f"完成：写入 {written_count} 条，已保存：{output_path}")
    finally:
        finish_callback(completed_path)
