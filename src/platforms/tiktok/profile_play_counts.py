from __future__ import annotations

import json
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
    interruptible_sleep,
    sanitize_csv_row,
    should_stop,
    wait_if_paused,
)
from src.platforms.tiktok.profile_videos import (
    normalize_profile_url,
    parse_profile_urls,
    trigger_profile_lazy_load,
    log_line,
)

CSV_FIELDS = ["序号", "视频链接", "播放量"]
PAGE_LOAD_TIMEOUT = 45000
SCROLL_INTERVAL_SECONDS = 2.5
NO_NEW_SCROLL_LIMIT = 10


def run_tiktok_profile_play_counts_spider(
    txt_path: str,
    cdp_port_or_url: str,
    max_scrolls: int,
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

        output_path = build_output_path("tiktok", f"tiktok_profile_play_counts_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        writer = XlsxRowWriter(output_path, CSV_FIELDS)

        written_count = 0
        serial_number = 1
        
        with sync_playwright() as playwright:
            log_line(log_callback, "正在连接本地 Chrome，请确认已登录 TikTok。")
            try:
                _, context = connect_existing_chromium(playwright, cdp_port_or_url, log_callback=log_callback)
            except Exception as exc:
                log_line(log_callback, f"连接失败：请确认 Chrome 已打开并已登录 TikTok。错误：{exc}")
                return

            profile_page = context.new_page()

            for profile_index, raw_profile_url in enumerate(profile_urls, 1):
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                profile_url = normalize_profile_url(raw_profile_url)
                if not profile_url:
                    log_line(log_callback, f"[{profile_index}/{len(profile_urls)}] 跳过无效主页：{raw_profile_url}")
                    continue
                
                match = re.search(r"tiktok\.com/(@[^/?#]+)", profile_url)
                username = match.group(1) if match else ""
                if not username:
                    continue

                log_line(log_callback, f"[{profile_index}/{len(profile_urls)}] 读取主页：{profile_url}")
                
                api_data = {"items": []}
                seen_video_ids = set()

                def handle_response(response):
                    if "/api/post/item_list" in response.url and "secUid" in response.url:
                        try:
                            text = response.text()
                            if text.strip():
                                body = json.loads(text)
                                for item in body.get("itemList", []):
                                    vid = item.get("id", "")
                                    if vid and vid not in seen_video_ids:
                                        seen_video_ids.add(vid)
                                        stats = item.get("stats", {})
                                        api_data["items"].append({
                                            "video_id": vid,
                                            "play_count": stats.get("playCount", 0)
                                        })
                        except Exception:
                            pass

                profile_page.on("response", handle_response)
                
                try:
                    profile_page.goto(profile_url, wait_until="domcontentloaded", timeout=page_load_timeout)
                    interruptible_sleep(2.5, stop_event)
                except PlaywrightTimeoutError:
                    log_line(log_callback, "  主页加载超时，跳过。")
                    profile_page.remove_listener("response", handle_response)
                    continue

                no_new_count = 0

                for scroll_index in range(max_scrolls):
                    if should_stop(stop_event):
                        break
                    if wait_if_paused(pause_event, stop_event):
                        break

                    new_items = api_data["items"]
                    api_data["items"] = []
                    
                    if new_items:
                        no_new_count = 0
                        log_line(log_callback, f"  滚动 {scroll_index + 1}/{max_scrolls}：拦截到 {len(new_items)} 条视频数据。")
                        
                        for item in new_items:
                            video_link = f"https://www.tiktok.com/{username}/video/{item['video_id']}"
                            play_count = item["play_count"]
                            
                            row = {
                                "序号": str(serial_number),
                                "视频链接": video_link,
                                "播放量": str(play_count),
                            }
                            writer.writerow(sanitize_csv_row(row))
                            written_count += 1
                            serial_number += 1
                    else:
                        no_new_count += 1

                    if no_new_count >= no_new_scroll_limit:
                        log_line(log_callback, "  连续多次未拦截到新数据，结束当前主页。")
                        break

                    trigger_profile_lazy_load(profile_page)
                    if interruptible_sleep(scroll_interval, stop_event):
                        break

                profile_page.remove_listener("response", handle_response)

            if not profile_page.is_closed():
                profile_page.close()

        writer.save()
        completed_path = output_path
        log_line(log_callback, f"完成：写入 {written_count} 条，已保存：{output_path}")
    except Exception as e:
        import traceback
        log_line(log_callback, f"发生异常: {e}\n{traceback.format_exc()}")
    finally:
        finish_callback(completed_path)
