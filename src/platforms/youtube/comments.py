from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlparse

from googleapiclient.discovery import build

from src.core import XlsxRowWriter, build_output_path, sanitize_csv_row, sanitize_csv_rows, should_stop, wait_if_paused

CSV_FIELDS = ["编号", "视频链接", "评论的点赞量", "评论内容", "发布时间"]
TOP_COMMENT_LIMIT = 100
DEFAULT_SCAN_LIMIT = 500

def normalize_youtube_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if not value.startswith("http"):
        value = "https://" + value
    return value.split("#")[0].strip()

def extract_video_id(url: str) -> str:
    normalized = normalize_youtube_url(url)
    parsed = urlparse(normalized)
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    if "youtu.be" in host and path_parts:
        return path_parts[0]
    if "youtube.com" in host:
        query_id = parse_qs(parsed.query).get("v", [""])[0]
        if query_id:
            return query_id
        if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            return path_parts[1]

    match = re.search(r"(?:v=|/video/|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{6,})", normalized)
    return match.group(1) if match else ""

def canonical_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""

def parse_video_entries(txt_path: str) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen_video_ids: set[str] = set()
    valid_line_count = 0
    duplicate_count = 0
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            raw_url = normalize_youtube_url(stripped.split()[0])
            video_id = extract_video_id(raw_url)
            if not video_id:
                continue
            valid_line_count += 1
            if video_id in seen_video_ids:
                duplicate_count += 1
                continue
            seen_video_ids.add(video_id)
            entries.append(
                {
                    "编号": len(entries) + 1,
                    "视频链接": canonical_video_url(video_id),
                    "视频ID": video_id,
                }
            )
    for entry in entries:
        entry["有效行数"] = valid_line_count
        entry["重复行数"] = duplicate_count
    return entries

def clean_comment_text(text: str) -> str:
    return (text or "").replace("\r", "").replace("\n", " | ").strip()

def non_text_placeholder(snippet: dict) -> str:
    keys = " ".join(str(key).lower() for key in snippet.keys())
    if "image" in keys or "photo" in keys:
        return "[图片]"
    if "video" in keys:
        return "[视频]"
    if "sticker" in keys:
        return "[贴纸]"
    return "[非文本]"

def fetch_top_level_comments(youtube, video_id: str, max_scan_comments: int, log_callback, stop_event=None, pause_event=None, api_page_size: int = 100) -> list[dict]:
    comments: list[dict] = []
    next_page_token = None
    page_size = max(1, min(api_page_size, 100))

    while len(comments) < max_scan_comments:
        if should_stop(stop_event):
            if log_callback:
                log_callback("  任务已停止。")
            break
        if wait_if_paused(pause_event, stop_event):
            break
        response = youtube.commentThreads().list(
            part="snippet",
            videoId=video_id,
            maxResults=min(page_size, max_scan_comments - len(comments)),
            pageToken=next_page_token,
            order="relevance",
            textFormat="plainText",
        ).execute()

        for item in response.get("items", []):
            if should_stop(stop_event):
                break
            top_comment = item.get("snippet", {}).get("topLevelComment", {})
            snippet = top_comment.get("snippet", {})
            text = clean_comment_text(snippet.get("textDisplay") or snippet.get("textOriginal") or "")
            if not text:
                text = non_text_placeholder(snippet)
            published_at = str(snippet.get("publishedAt") or "")
            if published_at:
                published_at = published_at.replace("T", " ").replace("Z", "").strip()
                if "." in published_at:
                    published_at = published_at.split(".")[0]
            comments.append(
                {
                    "like_count": int(snippet.get("likeCount", 0) or 0),
                    "text": text,
                    "published_at": published_at,
                }
            )
            if len(comments) >= max_scan_comments:
                break

        if len(comments) % 200 == 0 or len(comments) < 100:
            log_callback(f"  已扫描主楼评论 {len(comments)} 条。")

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    return comments

def top_comment_rows(youtube, video_index: int, video_url: str, video_id: str, max_scan_comments: int, log_callback, stop_event=None, pause_event=None, top_comment_limit: int = TOP_COMMENT_LIMIT, api_page_size: int = 100) -> list[dict[str, str]]:
    comments = fetch_top_level_comments(youtube, video_id, max_scan_comments, log_callback, stop_event, pause_event, api_page_size=api_page_size)
    comments.sort(key=lambda item: item["like_count"], reverse=True)

    rows: list[dict[str, str]] = []
    for comment in comments[:top_comment_limit]:
        rows.append(
            {
                "编号": str(video_index),
                "视频链接": video_url,
                "评论的点赞量": str(comment["like_count"]),
                "评论内容": comment["text"],
                "发布时间": comment.get("published_at", ""),
            }
        )
    return rows

def empty_video_row(video_index: int, video_url: str) -> dict[str, str]:
    return {
        "编号": str(video_index),
        "视频链接": video_url,
        "评论的点赞量": "",
        "评论内容": "",
        "发布时间": "",
    }

def run_youtube_top_comments_spider(api_key: str, txt_path: str, max_scan_comments: int, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    if config is None:
        config = {}
    top_comment_limit = int(config.get("comment_top_limit", TOP_COMMENT_LIMIT))
    api_page_size = int(config.get("youtube_api_page_size", 100))

    output_path = None
    completed_path = None
    try:
        entries = parse_video_entries(txt_path)
        if not entries:
            log_callback("TXT 中没有找到有效的 YouTube 视频链接。")
            return
        valid_line_count = int(entries[0].get("有效行数", len(entries)))
        duplicate_count = int(entries[0].get("重复行数", 0))
        log_callback(f"读取到 {valid_line_count} 行有效视频链接，去重后唯一视频 {len(entries)} 个，重复链接 {duplicate_count} 行。")

        youtube = build("youtube", "v3", developerKey=api_key)
        output_path = build_output_path("youtube", f"youtube_top_comments_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        writer = XlsxRowWriter(output_path, CSV_FIELDS)

        for progress_index, entry in enumerate(entries, 1):
            if should_stop(stop_event):
                log_callback("任务已停止。")
                break
            if wait_if_paused(pause_event, stop_event):
                break
            video_index = int(entry["编号"])
            video_url = str(entry["视频链接"])
            video_id = str(entry["视频ID"])

            log_callback(f"[{progress_index}/{len(entries)}] 读取评论，编号 {video_index}：{video_url}")
            try:
                rows = top_comment_rows(youtube, video_index, video_url, video_id, max_scan_comments, log_callback, stop_event, pause_event, top_comment_limit, api_page_size)
                if not rows:
                    rows = [empty_video_row(video_index, video_url)]
                writer.writerows(sanitize_csv_rows(rows))
                written_comments = len([row for row in rows if row["评论内容"]])
                log_callback(f"  完成：写入点赞量最高的主楼评论 {written_comments} 条。")
            except Exception as exc:
                writer.writerow(sanitize_csv_row(empty_video_row(video_index, video_url)))
                log_callback(f"  失败：{exc}，已写入空评论占位行。")

        writer.save()

        log_callback(f"完成，已保存：{output_path}")
        completed_path = output_path
    except Exception as exc:
        log_callback(f"运行失败：{exc}")
        output_path = None
        completed_path = None
    finally:
        finish_callback(completed_path)
