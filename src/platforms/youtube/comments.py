from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlparse
import urllib.request
import urllib.error
import concurrent.futures

from googleapiclient.discovery import build

from src.core import MultiSheetXlsxWriter, XlsxRowWriter, build_output_path, sanitize_csv_row, sanitize_csv_rows, should_stop, wait_if_paused

VIDEO_FIELDS = ["编号", "视频链接", "标题", "频道名称", "发布日期", "视频类型", "视频时长", "视频简介", "播放量", "点赞数", "评论数"]
COMMENT_FIELDS = ["编号", "视频链接", "评论的点赞量", "评论内容", "发布时间"]

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

def format_youtube_duration(duration: str) -> str:
    # duration is ISO 8601, e.g. PT1H2M10S, PT4M1S, PT3S
    if not duration or not duration.startswith("PT"):
        return ""
    duration = duration[2:]
    hours, minutes, seconds = 0, 0, 0
    h_match = re.search(r"(\d+)H", duration)
    if h_match:
        hours = int(h_match.group(1))
    m_match = re.search(r"(\d+)M", duration)
    if m_match:
        minutes = int(m_match.group(1))
    s_match = re.search(r"(\d+)S", duration)
    if s_match:
        seconds = int(s_match.group(1))
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def check_video_type_bulk(video_ids: list[str]) -> dict[str, str]:
    opener = urllib.request.build_opener(NoRedirectHandler)
    
    def check_one(vid: str) -> tuple[str, str]:
        req = urllib.request.Request(f"https://www.youtube.com/shorts/{vid}", method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        try:
            resp = opener.open(req, timeout=5)
            if resp.status == 200:
                return vid, "Shorts"
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return vid, "普通视频"
        except Exception:
            pass
        return vid, "未知"

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_vid = {executor.submit(check_one, vid): vid for vid in video_ids}
        for future in concurrent.futures.as_completed(future_to_vid):
            vid, vtype = future.result()
            results[vid] = vtype
    return results

def fetch_video_metrics(youtube, video_ids: list[str]) -> dict[str, dict]:
    result = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        response = youtube.videos().list(
            part="snippet,statistics,contentDetails",
            id=",".join(batch)
        ).execute()
        for item in response.get("items", []):
            vid = item.get("id")
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            pub_date = str(snippet.get("publishedAt", "")).replace("T", " ").replace("Z", "")
            if "." in pub_date:
                pub_date = pub_date.split(".")[0]
            desc = snippet.get("description", "").replace("\n", " | ").replace("\r", "")
            if len(desc) > 300:
                desc = desc[:300] + "..."
            
            result[vid] = {
                "标题": snippet.get("title", ""),
                "频道名称": snippet.get("channelTitle", ""),
                "发布日期": pub_date,
                "视频时长": format_youtube_duration(content.get("duration", "")),
                "视频简介": desc,
                "播放量": stats.get("viewCount", ""),
                "点赞数": stats.get("likeCount", ""),
                "评论数": stats.get("commentCount", "")
            }
    return result

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
            if log_callback:
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

def run_youtube_video_metrics_spider(api_key: str, txt_path: str, get_comments: str, check_type: str, max_scan_comments: int, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    if config is None:
        config = {}
    get_comments_bool = (get_comments == "是")
    check_type_bool = (check_type == "是")
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
        output_path = build_output_path("youtube", f"youtube_video_metrics_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        
        if get_comments_bool:
            writer = MultiSheetXlsxWriter(output_path, {"视频信息": VIDEO_FIELDS, "评论信息": COMMENT_FIELDS})
        else:
            writer = XlsxRowWriter(output_path, VIDEO_FIELDS)
        
        video_ids = [str(e["视频ID"]) for e in entries]
        
        try:
            log_callback(f"正在批量获取 {len(video_ids)} 个视频的热度数据...")
            metrics_map = fetch_video_metrics(youtube, video_ids)
        except Exception as exc:
            import googleapiclient.errors
            if isinstance(exc, googleapiclient.errors.HttpError) and exc.resp.status in [403]:
                log_callback("API 配额已耗尽，或无权访问，请更换 API Key。")
                return
            else:
                log_callback(f"获取视频热度失败: {exc}")
                return
                
        type_map = {}
        if check_type_bool:
            log_callback(f"正在精确检测 {len(video_ids)} 个视频的长短类型 (网络请求可能较慢)...")
            type_map = check_video_type_bulk(video_ids)

        for progress_index, entry in enumerate(entries, 1):
            if should_stop(stop_event):
                log_callback("任务已停止。")
                break
            if wait_if_paused(pause_event, stop_event):
                break
            video_index = int(entry["编号"])
            video_url = str(entry["视频链接"])
            video_id = str(entry["视频ID"])

            log_callback(f"[{progress_index}/{len(entries)}] 处理编号 {video_index}：{video_url}")
            
            v_info = metrics_map.get(video_id, {})
            row_video = {
                "编号": str(video_index),
                "视频链接": video_url,
                "标题": v_info.get("标题", ""),
                "频道名称": v_info.get("频道名称", ""),
                "发布日期": v_info.get("发布日期", ""),
                "视频类型": type_map.get(video_id, "") if check_type_bool else "",
                "视频时长": v_info.get("视频时长", ""),
                "视频简介": v_info.get("视频简介", ""),
                "播放量": v_info.get("播放量", ""),
                "点赞数": v_info.get("点赞数", ""),
                "评论数": v_info.get("评论数", ""),
            }

            if get_comments_bool:
                writer.writerow("视频信息", sanitize_csv_row(row_video))
                
                try:
                    rows = top_comment_rows(youtube, video_index, video_url, video_id, max_scan_comments, log_callback, stop_event, pause_event, top_comment_limit, api_page_size)
                    if not rows:
                        rows = [empty_video_row(video_index, video_url)]
                    for r in sanitize_csv_rows(rows):
                        writer.writerow("评论信息", r)
                    written_comments = len([row for row in rows if row["评论内容"]])
                    log_callback(f"  完成：播放 {v_info.get('播放量')}，点赞 {v_info.get('点赞数')}，评论 {v_info.get('评论数')}。写入热评 {written_comments} 条。")
                except Exception as exc:
                    if isinstance(exc, googleapiclient.errors.HttpError) and exc.resp.status in [403]:
                        log_callback(f"  停止任务：API 配额耗尽 ({exc})，请更换 API Key。")
                        break
                    else:
                        writer.writerow("评论信息", sanitize_csv_row(empty_video_row(video_index, video_url)))
                        log_callback(f"  抓取评论失败：{exc}，已写入空评论占位行。")
            else:
                writer.writerow(sanitize_csv_row(row_video))
                log_callback(f"  完成：播放 {v_info.get('播放量')}，点赞 {v_info.get('点赞数')}，评论 {v_info.get('评论数')}。")

        writer.save()

        log_callback(f"完成，已保存：{output_path}")
        completed_path = output_path
    except Exception as exc:
        log_callback(f"运行失败：{exc}")
        output_path = None
        completed_path = None
    finally:
        finish_callback(completed_path)
