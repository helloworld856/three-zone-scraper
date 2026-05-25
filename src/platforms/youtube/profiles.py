from __future__ import annotations

import time
from urllib.parse import urlparse

from googleapiclient.discovery import build

from src.core import XlsxRowWriter, build_output_path, sanitize_csv_row, should_stop, wait_if_paused

CSV_FIELDS = ["作者主页链接", "作者名称", "作者ID", "粉丝量", "作者简介"]

def normalize_youtube_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        return "https:" + value
    if not value.startswith("http"):
        return "https://" + value
    return value.split("?")[0].split("#")[0].rstrip("/")

def parse_channel_url(url: str) -> tuple[str, str]:
    normalized = normalize_youtube_url(url)
    parsed = urlparse(normalized)
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return "", ""

    first = parts[0]
    if first == "channel" and len(parts) >= 2:
        return "id", parts[1]
    if first == "user" and len(parts) >= 2:
        return "username", parts[1]
    if first.startswith("@"):
        return "handle", first
    if first in {"c", "custom"} and len(parts) >= 2:
        return "search", parts[1]
    return "search", first.lstrip("@")

def resolve_channel(youtube, profile_url: str) -> dict:
    hint_type, hint_value = parse_channel_url(profile_url)
    if not hint_value:
        return {}

    if hint_type == "id":
        response = youtube.channels().list(part="snippet,statistics", id=hint_value).execute()
    elif hint_type == "username":
        response = youtube.channels().list(part="snippet,statistics", forUsername=hint_value).execute()
    elif hint_type == "handle":
        response = youtube.channels().list(part="snippet,statistics", forHandle=hint_value).execute()
    else:
        search_response = youtube.search().list(
            part="id",
            q=hint_value,
            type="channel",
            maxResults=1,
        ).execute()
        items = search_response.get("items", [])
        if not items:
            return {}
        channel_id = items[0].get("id", {}).get("channelId", "")
        if not channel_id:
            return {}
        response = youtube.channels().list(part="snippet,statistics", id=channel_id).execute()

    items = response.get("items", [])
    return items[0] if items else {}

def channel_row(profile_url: str, item: dict) -> dict:
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    channel_id = item.get("id", "")
    description = (snippet.get("description") or "").replace("\n", " | ").replace("\r", "").strip()
    return {
        "作者主页链接": normalize_youtube_url(profile_url),
        "作者名称": snippet.get("title", ""),
        "作者ID": channel_id,
        "粉丝量": stats.get("subscriberCount", "已隐藏"),
        "作者简介": description,
    }

def run_channel_spider(api_key, txt_file_path, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    output_path = None
    try:
        with open(txt_file_path, "r", encoding="utf-8-sig") as f:
            profile_urls = [normalize_youtube_url(line.strip()) for line in f if line.strip() and not line.strip().startswith("#")]

        profile_urls = [url for url in profile_urls if "youtube.com" in url or "youtu.be" in url]
        if not profile_urls:
            log_callback("TXT 中没有有效的 YouTube 作者主页链接。")
            return

        youtube = build("youtube", "v3", developerKey=api_key)
        output_path = build_output_path("youtube", f"youtube_profiles_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")

        writer = XlsxRowWriter(output_path, CSV_FIELDS)
        for index, profile_url in enumerate(profile_urls, 1):
            if should_stop(stop_event):
                log_callback("任务已停止。")
                break
            if wait_if_paused(pause_event, stop_event):
                break
            log_callback(f"[{index}/{len(profile_urls)}] 解析作者：{profile_url}")
            try:
                item = resolve_channel(youtube, profile_url)
                if not item:
                    log_callback("  未找到作者信息")
                    writer.writerow(
                        sanitize_csv_row({
                            "作者主页链接": profile_url,
                            "作者名称": "未找到",
                            "作者ID": "",
                            "粉丝量": "",
                            "作者简介": "",
                        })
                    )
                    continue

                row = channel_row(profile_url, item)
                writer.writerow(sanitize_csv_row(row))
                log_callback(f"  成功：{row['作者名称']} | 粉丝量：{row['粉丝量']}")
            except Exception as exc:
                log_callback(f"  解析失败：{exc}")

        writer.save()

        log_callback(f"完成，已保存：{output_path}")
    except Exception as exc:
        log_callback(f"运行失败：{exc}")
        output_path = None
    finally:
        finish_callback(output_path)
