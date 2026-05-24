from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.core import XlsxRowWriter, build_output_path, sanitize_csv_rows, should_stop, wait_if_paused

CONTEXT_SIZE = 5
VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([0-9A-Za-z_-]{11})")

def parse_video_id(url: str) -> str:
    match = VIDEO_ID_RE.search(url or "")
    if match:
        return match.group(1)
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", (url or "").strip()):
        return url.strip()
    return ""

def parse_input_pairs(txt_path: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = [part.strip() for part in stripped.split("\t") if part.strip()] if "\t" in stripped else stripped.split()
            if len(parts) >= 2:
                pairs.append((parts[0], parts[1]))
    return pairs

def normalize_youtube_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if not url.startswith("http"):
        return "https://" + url
    return url

def relation_for_index(target_index: int, current_index: int) -> str:
    if current_index < target_index:
        return f"目标后发布第{target_index - current_index}条"
    return f"目标前发布第{current_index - target_index}条"

def extract_channel_hint(profile_url: str) -> tuple[str, str]:
    normalized = normalize_youtube_url(profile_url)
    parsed = urlparse(normalized)
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        return "", ""

    first = path_parts[0]
    if first == "channel" and len(path_parts) >= 2:
        return "id", path_parts[1]
    if first == "user" and len(path_parts) >= 2:
        return "username", path_parts[1]
    if first.startswith("@"):
        return "handle", first[1:]
    if first in {"c", "custom"} and len(path_parts) >= 2:
        return "search", path_parts[1]
    return "search", first.lstrip("@")

def resolve_channel(youtube, profile_url: str) -> dict:
    hint_type, hint_value = extract_channel_hint(profile_url)
    if not hint_value:
        return {}

    try:
        if hint_type == "id":
            res = youtube.channels().list(part="snippet,contentDetails", id=hint_value).execute()
        elif hint_type == "username":
            res = youtube.channels().list(part="snippet,contentDetails", forUsername=hint_value).execute()
        elif hint_type == "handle":
            res = {"items": []}
            handle_variants = []
            clean_handle = hint_value.lstrip("@")
            handle_variants.append(f"@{clean_handle}")
            handle_variants.append(clean_handle)
            for handle in handle_variants:
                try:
                    res = youtube.channels().list(part="snippet,contentDetails", forHandle=handle).execute()
                except TypeError:
                    res = {"items": []}
                if res.get("items"):
                    break
        else:
            res = {"items": []}
    except HttpError:
        raise
    except Exception:
        res = {"items": []}

    items = res.get("items", [])
    return items[0] if items else {}

def resolve_channel_from_video(youtube, video_id: str) -> dict:
    res = youtube.videos().list(part="snippet", id=video_id, maxResults=1).execute()
    items = res.get("items", [])
    if not items:
        return {}
    channel_id = items[0].get("snippet", {}).get("channelId", "")
    if not channel_id:
        return {}
    channel_res = youtube.channels().list(part="snippet,contentDetails", id=channel_id).execute()
    channel_items = channel_res.get("items", [])
    return channel_items[0] if channel_items else {}

def find_context_video_ids(youtube, uploads_playlist_id: str, target_video_id: str, stop_event=None, pause_event=None, max_pages: int = 200, context_size: int = CONTEXT_SIZE) -> tuple[list[str], int, list[str]]:
    video_ids: list[str] = []
    next_page_token = None
    page_count = 0

    while page_count < max_pages:
        page_count += 1
        if should_stop(stop_event):
            return [], -1, video_ids
        if wait_if_paused(pause_event, stop_event):
            return [], -1, video_ids
        res = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=next_page_token,
        ).execute()

        for item in res.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId", "")
            if vid:
                video_ids.append(vid)

        if target_video_id in video_ids:
            target_index = video_ids.index(target_video_id)
            if len(video_ids) >= target_index + context_size + 1:
                break

        next_page_token = res.get("nextPageToken")
        if not next_page_token:
            break

    if target_video_id not in video_ids:
        return [], -1, video_ids

    target_index = video_ids.index(target_video_id)
    selected_indices = list(range(max(0, target_index - context_size), target_index))
    selected_indices += list(range(target_index + 1, min(len(video_ids), target_index + context_size + 1)))
    return [video_ids[idx] for idx in selected_indices], target_index, video_ids

def fetch_video_details(youtube, video_ids: list[str], stop_event=None, pause_event=None) -> dict[str, dict]:
    details: dict[str, dict] = {}
    for start in range(0, len(video_ids), 50):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        chunk = video_ids[start:start + 50]
        if not chunk:
            continue
        res = youtube.videos().list(
            part="snippet,statistics,contentDetails",
            id=",".join(chunk),
            maxResults=50,
        ).execute()
        for item in res.get("items", []):
            stats = item.get("statistics", {})
            snippet = item.get("snippet", {})
            details[item["id"]] = {
                "title": snippet.get("title", ""),
                "published_at": snippet.get("publishedAt", ""),
                "view_count": stats.get("viewCount", ""),
                "like_count": stats.get("likeCount", ""),
                "comment_count": stats.get("commentCount", ""),
            }
    return details

OUTPUT_FIELDS = [
    "博主链接",
    "目标视频链接",
    "视频链接",
    "时间轴关系",
    "视频标题",
    "发布时间",
    "播放量",
    "点赞数",
    "评论数",
    "视频ID",
]


def build_pair_rows(youtube, target_video_url: str, profile_url: str, channel_cache: dict[str, dict], log_callback, stop_event=None, pause_event=None, context_size: int = CONTEXT_SIZE, max_upload_pages: int = 200) -> list[dict]:
    rows: list[dict] = []
    target_video_id = parse_video_id(target_video_url)
    if not target_video_id:
        log_callback("  跳过：无法解析视频 ID。")
        return rows

    channel = channel_cache.get(profile_url)
    if channel is None:
        channel = resolve_channel(youtube, profile_url)
        channel_cache[profile_url] = channel

    uploads_id = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
    if not uploads_id:
        log_callback("  博主主页解析失败，改用目标视频反查频道上传列表。")
        channel = resolve_channel_from_video(youtube, target_video_id)
        if channel:
            channel_cache[profile_url] = channel
        uploads_id = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
        if not uploads_id:
            log_callback("  跳过：无法解析上传列表。请检查博主主页链接是否为 YouTube 频道主页。")
            return rows

    selected_ids, target_index, timeline_ids = find_context_video_ids(youtube, uploads_id, target_video_id, stop_event, pause_event, max_upload_pages, context_size)
    if should_stop(stop_event):
        return rows
    if target_index < 0:
        log_callback("  跳过：目标视频不在该博主公开上传列表中。")
        return rows

    details = fetch_video_details(youtube, selected_ids, stop_event, pause_event)
    for vid in selected_ids:
        current_index = timeline_ids.index(vid)
        item = details.get(vid, {})
        rows.append({
            "博主链接": profile_url,
            "目标视频链接": target_video_url,
            "视频链接": f"https://www.youtube.com/watch?v={vid}",
            "时间轴关系": relation_for_index(target_index, current_index),
            "视频标题": item.get("title", ""),
            "发布时间": item.get("published_at", ""),
            "播放量": item.get("view_count", ""),
            "点赞数": item.get("like_count", ""),
            "评论数": item.get("comment_count", ""),
            "视频ID": vid,
        })
    return rows


def run_youtube_paired_context_spider(api_key: str, txt_path: str, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    if config is None:
        config = {}
    context_size = int(config.get("context_size", CONTEXT_SIZE))
    max_upload_pages = int(config.get("max_upload_pages", 200))

    output_path = None
    try:
        pairs = parse_input_pairs(txt_path)
        if not pairs:
            log_callback("TXT 中没有有效的'视频链接 + 博主主页链接'行。")
            return
        if should_stop(stop_event):
            log_callback("任务已停止。")
            return
        output_path = build_output_path("youtube", f"youtube_paired_context_metrics_{time.strftime('%Y%m%d')}.xlsx")
        writer = XlsxRowWriter(output_path, OUTPUT_FIELDS)
        youtube = build("youtube", "v3", developerKey=api_key)
        channel_cache: dict[str, dict] = {}
        written_count = 0
        for index, (target_video_url, profile_url) in enumerate(pairs, 1):
            if should_stop(stop_event):
                log_callback("任务已停止。")
                break
            if wait_if_paused(pause_event, stop_event):
                break
            log_callback(f"[{index}/{len(pairs)}] 定位 YouTube 目标视频: {target_video_url}")
            try:
                rows = build_pair_rows(youtube, target_video_url, profile_url, channel_cache, log_callback, stop_event, pause_event, context_size, max_upload_pages)
                if rows:
                    writer.writerows(sanitize_csv_rows(rows))
                    written_count += len(rows)
                log_callback(f"  完成：写入 {len(rows)} 条前后视频，累计 {written_count} 条。")
            except HttpError as e:
                log_callback(f"  YouTube API 错误：{e}")
            except Exception as e:
                log_callback(f"  处理失败：{e}")
        writer.save()
        if written_count <= 0:
            log_callback("没有提取到数据。")
        log_callback(f"完成，已保存：{output_path}")
    except Exception as exc:
        log_callback(f"运行失败：{exc}")
        output_path = None
    finally:
        finish_callback(output_path)
