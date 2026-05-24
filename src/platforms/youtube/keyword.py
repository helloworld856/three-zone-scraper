from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

from src.core import XlsxRowWriter, MultiSheetXlsxWriter, build_output_path, sanitize_csv_rows, should_stop, wait_if_paused
from src.platforms.youtube.comments import fetch_top_level_comments

CSV_FIELDS = [
    "搜索词",
    "序号",
    "视频标题",
    "视频时长",
    "播放量",
    "点赞数",
    "发布时间",
    "视频链接",
    "作者主页链接",
]

DEFAULT_START_DATE = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
DEFAULT_END_DATE = datetime.now().strftime("%Y-%m-%d")

def parse_date_range(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if start_dt > end_dt:
        raise ValueError("开始日期不能晚于结束日期")
    return start_dt, end_dt

def youtube_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def format_youtube_duration(iso_duration: str) -> str:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        iso_duration or "",
    )
    if not match:
        return ""

    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0) + days * 24
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]

def safe_filename_part(value: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", value or "").strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:80] or "keyword"

def iter_search_video_id_batches(youtube, keyword: str, max_results: int, limit_time_bool: bool, start_dt: datetime | None, end_dt: datetime | None, log_callback, stop_event=None, pause_event=None, batch_size: int = 50):
    seen_video_ids: set[str] = set()
    next_page_token = None

    while len(seen_video_ids) < max_results:
        if should_stop(stop_event):
            log_callback("任务已停止。")
            break
        if wait_if_paused(pause_event, stop_event):
            break

        params = {
            "part": "id",
            "q": keyword,
            "type": "video",
            "order": "relevance",
            "maxResults": min(batch_size, max_results - len(seen_video_ids)),
            "pageToken": next_page_token,
        }
        if limit_time_bool and start_dt and end_dt:
            params["publishedAfter"] = youtube_rfc3339(start_dt)
            params["publishedBefore"] = youtube_rfc3339(end_dt + timedelta(days=1))
            
        response = youtube.search().list(**params).execute()

        batch_ids: list[str] = []
        for item in response.get("items", []):
            if should_stop(stop_event):
                break
            video_id = item.get("id", {}).get("videoId", "")
            if video_id and video_id not in seen_video_ids:
                batch_ids.append(video_id)
                seen_video_ids.add(video_id)

        if batch_ids:
            log_callback(f"  {keyword}: 已找到 {len(seen_video_ids)} 个日期范围内的视频")
            yield batch_ids

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

def fetch_video_rows(youtube, keyword: str, video_ids: list[str], stop_event=None, pause_event=None, batch_size: int = 50) -> list[dict]:
    rows: list[dict] = []
    for ids in chunked(video_ids, batch_size):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        response = youtube.videos().list(
            part="snippet,contentDetails,statistics",
            id=",".join(ids),
            maxResults=50,
        ).execute()

        for item in response.get("items", []):
            if should_stop(stop_event):
                break
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            video_id = item.get("id", "")
            channel_id = snippet.get("channelId", "")
            rows.append(
                {
                    "搜索词": keyword,
                    "序号": "",
                    "视频标题": snippet.get("title", ""),
                    "视频时长": format_youtube_duration(content.get("duration", "")),
                    "播放量": stats.get("viewCount", ""),
                    "点赞数": stats.get("likeCount", ""),
                    "发布时间": snippet.get("publishedAt", ""),
                    "视频链接": f"https://www.youtube.com/watch?v={video_id}",
                    "作者主页链接": f"https://www.youtube.com/channel/{channel_id}" if channel_id else "",
                }
            )
    return rows

def run_youtube_spider(api_key, keywords_list, max_results, limit_time_str, start_date, end_date, get_comments_str, max_comments, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    if config is None:
        config = {}
    search_batch_size = int(config.get("youtube_search_batch_size", 50))
    video_batch_size = int(config.get("youtube_video_batch_size", 50))
    comment_top_limit = int(config.get("youtube_comment_top_limit", 100))
    output_path = None
    output_paths: list[str] = []
    try:
        limit_time_bool = limit_time_str == "是"
        get_comments_bool = get_comments_str == "是"
        start_dt, end_dt = None, None
        if limit_time_bool:
            start_dt, end_dt = parse_date_range(start_date, end_date)

        youtube = build("youtube", "v3", developerKey=api_key)
        run_stamp = time.strftime("%Y%m%d")

        for index, keyword in enumerate(keywords_list, 1):
            if should_stop(stop_event):
                log_callback("任务已停止。")
                break
            if wait_if_paused(pause_event, stop_event):
                break
            output_path = build_output_path(
                "youtube",
                f"youtube_keyword_videos_{safe_filename_part(keyword)}_{run_stamp}.xlsx",
            )
            output_paths.append(output_path)

            if get_comments_bool:
                comment_fields = ["序号", "视频链接", "评论的点赞量", "评论内容", "评论发布时间"]
                writer = MultiSheetXlsxWriter(output_path, {"视频信息": CSV_FIELDS, "评论信息": comment_fields})
            else:
                writer = XlsxRowWriter(output_path, CSV_FIELDS)
            serial_number = 1
            log_callback(f"[{index}/{len(keywords_list)}] 搜索关键词：{keyword}")
            log_callback(f"  输出文件：{output_path}")
            if limit_time_bool:
                log_callback(f"  日期范围：{start_date} 至 {end_date}")
            else:
                log_callback("  日期范围：不限时间")
            written_count = 0
            for video_ids in iter_search_video_id_batches(youtube, keyword, max_results, limit_time_bool, start_dt, end_dt, log_callback, stop_event, pause_event, search_batch_size):
                if should_stop(stop_event):
                    break
                rows = fetch_video_rows(youtube, keyword, video_ids, stop_event, pause_event, video_batch_size)
                for row in rows:
                    row["序号"] = str(serial_number)
                    
                    if get_comments_bool:
                        try:
                            video_id = (row["视频链接"].split("v=")[1] if "v=" in row["视频链接"] else "").split("&")[0]
                            comments = fetch_top_level_comments(youtube, video_id, max_comments, log_callback, stop_event, pause_event)
                            comments.sort(key=lambda item: item["like_count"], reverse=True)
                            for comment in comments[:comment_top_limit]:
                                comment_row = {
                                    "序号": row["序号"],
                                    "视频链接": row["视频链接"],
                                    "评论的点赞量": str(comment["like_count"]),
                                    "评论内容": comment["text"],
                                    "评论发布时间": comment.get("published_at", "")
                                }
                                writer.writerow("评论信息", comment_row)
                        except Exception as exc:
                            log_callback(f"    提取评论失败：{exc}")
                            
                    serial_number += 1
                    
                if get_comments_bool:
                    for r in rows:
                        writer.writerow("视频信息", r)
                else:
                    writer.writerows(sanitize_csv_rows(rows))
                written_count += len(rows)
                log_callback(f"  已写入 {written_count} 条视频")
            writer.save()
            log_callback(f"  写入 {written_count} 条视频")

        log_callback("完成，已按关键词分别保存：")
        for path in output_paths:
            log_callback(f"  {path}")
    except Exception as exc:
        log_callback(f"运行失败：{exc}")
        output_path = None
    finally:
        finish_callback(output_paths[-1] if output_paths else output_path)
