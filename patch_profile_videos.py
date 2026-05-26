import sys
import re

with open('src/platforms/tiktok/profile_videos.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add MIN_GUARANTEED_VIDEOS
content = content.replace("NO_NEW_SCROLL_LIMIT = 10\nDEFAULT_MAX_SCROLLS = 500\nSCROLL_PX = 3600\n",
"NO_NEW_SCROLL_LIMIT = 10\nDEFAULT_MAX_SCROLLS = 500\nSCROLL_PX = 3600\nMIN_GUARANTEED_VIDEOS = 5\n")

# 2. Update row_from_detail
old_row = """def row_from_detail(index: int, detail: dict[str, str]) -> dict[str, str]:
    return {
        "序号": str(index),
        "视频链接": detail.get("video_url", ""),
        "发布日期": detail.get("published_at", ""),
        "视频简介": detail.get("desc", ""),
        "点赞数": detail.get("likes", ""),
        "评论数": detail.get("comments", ""),
        "收藏量": detail.get("collects", ""),
        "分享数": detail.get("shares", ""),
    }"""
new_row = """def row_from_detail(index: int, detail: dict[str, str], play_count: str = "") -> dict[str, str]:
    row = {
        "序号": str(index),
        "视频链接": detail.get("video_url", ""),
    }
    if play_count or "播放量" in detail:
        row["播放量"] = str(play_count or detail.get("播放量", ""))
    row.update({
        "发布日期": detail.get("published_at", ""),
        "视频简介": detail.get("desc", ""),
        "点赞数": detail.get("likes", ""),
        "评论数": detail.get("comments", ""),
        "收藏量": detail.get("collects", ""),
        "分享数": detail.get("shares", ""),
    })
    return row"""
content = content.replace(old_row, new_row)

# 3. Update process_video_batch signature
old_sig = """def process_video_batch(
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
) -> tuple[int, int, bool]:"""
new_sig = """def process_video_batch(
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
    processed_count: int = 0,
    play_counts_map: dict[str, int] = None,
    fetch_play_counts_bool: bool = False,
) -> tuple[int, int, bool, int]:"""
content = content.replace(old_sig, new_sig)


# 4. Update process_video_batch body (date limit)
old_body = """                if limit_time_bool and start_dt and end_dt:
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

            row_base = row_from_detail(serial_number, detail) if get_video_info_bool else {"序号": str(serial_number), "视频链接": video_url}"""
new_body = """                if limit_time_bool and start_dt and end_dt:
                    publish_dt = parse_publish_date(published_at)
                    if publish_dt and publish_dt.date() < start_dt.date():
                        if processed_count >= MIN_GUARANTEED_VIDEOS:
                            log_line(log_callback, f"      停止当前主页：视频发布时间早于开始日期（{published_at}）。")
                            stop_profile = True
                            wait_after_detail(log_callback, stop_event, pause_event=pause_event)
                            break
                        else:
                            log_line(log_callback, f"      跳过：发布时间超出范围（{published_at}），当前在保底前 {MIN_GUARANTEED_VIDEOS} 条内，不终止。")
                            processed_count += 1
                            if wait_after_detail(log_callback, stop_event, pause_event=pause_event):
                                break
                            continue

                    if not in_date_range(published_at, start_dt, end_dt):
                        log_line(log_callback, f"      跳过：发布时间不在范围内（{published_at or '未解析'}）。")
                        processed_count += 1
                        if wait_after_detail(log_callback, stop_event, pause_event=pause_event):
                            break
                        continue

            processed_count += 1
            vid = parse_video_id(video_url)
            play_count = ""
            if fetch_play_counts_bool and play_counts_map and vid in play_counts_map:
                play_count = str(play_counts_map[vid])
                
            row_base = row_from_detail(serial_number, detail, play_count) if get_video_info_bool else {"序号": str(serial_number), "视频链接": video_url}
            if fetch_play_counts_bool and not get_video_info_bool:
                row_base["播放量"] = play_count"""
content = content.replace(old_body, new_body)

# 5. return processed_count
content = content.replace("return serial_number, written_count, stop_profile", "return serial_number, written_count, stop_profile, processed_count")

# 6. run_tiktok_profile_videos_spider args
old_run_sig = """    get_comments_str: str,
    max_comments: int,
    cdp_port_or_url: str,"""
new_run_sig = """    get_comments_str: str,
    max_comments: int,
    fetch_play_counts_str: str,
    cdp_port_or_url: str,"""
content = content.replace(old_run_sig, new_run_sig)

# 7. fetch_play_counts setup
old_fields = """        video_fields = ["序号", "视频链接"]
        if get_video_info_bool:
            video_fields.extend(["发布日期", "视频简介", "点赞数", "评论数", "收藏量", "分享数"])"""
new_fields = """        fetch_play_counts_bool = (fetch_play_counts_str == "是")

        video_fields = ["序号", "视频链接"]
        if fetch_play_counts_bool:
            video_fields.append("播放量")
        if get_video_info_bool:
            video_fields.extend(["发布日期", "视频简介", "点赞数", "评论数", "收藏量", "分享数"])"""
content = content.replace(old_fields, new_fields)

# 8. intercept response
old_intercept = """                log_line(log_callback, f"[{profile_index}/{len(profile_urls)}] 读取主页：{profile_url}")
                try:
                    profile_page.goto(profile_url, wait_until="domcontentloaded", timeout=page_load_timeout)"""
new_intercept = """                log_line(log_callback, f"[{profile_index}/{len(profile_urls)}] 读取主页：{profile_url}")
                
                play_counts_map = {}
                def handle_response(response):
                    if "/api/post/item_list" in response.url and "secUid" in response.url:
                        try:
                            text = response.text()
                            if text.strip():
                                body = json.loads(text)
                                for item in body.get("itemList", []):
                                    vid = item.get("id", "")
                                    if vid:
                                        stats = item.get("stats", {})
                                        play_counts_map[vid] = stats.get("playCount", 0)
                        except Exception:
                            pass

                if fetch_play_counts_bool:
                    profile_page.on("response", handle_response)
                try:
                    profile_page.goto(profile_url, wait_until="domcontentloaded", timeout=page_load_timeout)"""
content = content.replace(old_intercept, new_intercept)

# 9. remove listener and init processed_count
old_seen = """                seen_links: set[str] = set()
                pending_links: list[str] = []
                no_new_count = 0
                stop_profile = False

                for scroll_index in range(actual_max_scrolls):"""
new_seen = """                seen_links: set[str] = set()
                pending_links: list[str] = []
                no_new_count = 0
                stop_profile = False
                processed_count = 0

                for scroll_index in range(actual_max_scrolls):"""
content = content.replace(old_seen, new_seen)

# 10. remove listener
old_close = """            for opened_page in (profile_page, detail_page):"""
new_close = """            if fetch_play_counts_bool:
                try:
                    profile_page.remove_listener("response", handle_response)
                except Exception:
                    pass

            for opened_page in (profile_page, detail_page):"""
content = content.replace(old_close, new_close)

# 11. update batch calls
# We'll use regex for this
batch_call_pattern = r'serial_number, written_count, stop_profile = process_video_batch\((.*?)\s*\)'
new_batch_call = r'''serial_number, written_count, stop_profile, processed_count = process_video_batch(\1,
                            processed_count=processed_count,
                            play_counts_map=play_counts_map,
                            fetch_play_counts_bool=fetch_play_counts_bool,
                        )'''
content = re.sub(batch_call_pattern, new_batch_call, content, flags=re.DOTALL)

with open('src/platforms/tiktok/profile_videos.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Patch applied successfully.")
