import re
import random
from datetime import datetime, timedelta
from typing import Any

from src.core import (
    MultiSheetXlsxWriter,
    connect_existing_chromium,
    should_stop,
    wait_if_paused,
    DEFAULT_X_CDP_URL,
    build_output_path,
)
from playwright.sync_api import sync_playwright

IGNORE_WORDS = [
    "赞", "评论", "分享", "发送", "留言", "回复", "隐藏", "Like", "Comment", "Share", "Send",
    "Reply", "Hide"
]

PAGE_TIMEOUT_MS = 60000
SCROLL_DELAY_MS = 2000
NO_NEW_LIMIT = 5
SAVE_BATCH_SIZE = 10

def log_line(log_callback, message: str) -> None:
    if log_callback:
        log_callback(message)

def parse_date_range(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d")
    end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError("开始日期不能晚于结束日期。")
    return start_dt, end_dt

def parse_fb_time_string(time_str: str) -> datetime | None:
    text = (time_str or "").strip().lower()
    if not text:
        return None
    now = datetime.now()
    
    match = re.search(r'(\d+)\s*(小?时|分钟|天|周|min|hr|hour|day|week|month|year)s?\s*(前|ago)?', text)
    if match:
        val = int(match.group(1))
        unit = match.group(2)
        if unit in ('分钟', 'min'): 
            return now - timedelta(minutes=val)
        elif unit in ('时', '小时', 'hr', 'hour'): 
            return now - timedelta(hours=val)
        elif unit in ('天', 'day'): 
            return now - timedelta(days=val)
        elif unit in ('周', 'week'): 
            return now - timedelta(weeks=val)
        elif unit in ('month',): 
            return now - timedelta(days=val*30)
        elif unit in ('year',): 
            return now - timedelta(days=val*365)
    
    if "昨天" in text or "yesterday" in text:
        return now - timedelta(days=1)
        
    match = re.search(r'(?:(?:20)?(\d{2})年)?\s*(\d{1,2})月(\d{1,2})日', text)
    if match:
        year_str, month_str, day_str = match.groups()
        year = int("20" + year_str) if year_str else now.year
        try:
            return datetime(year, int(month_str), int(day_str))
        except ValueError:
            pass
            
    match = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
            
    return None

def in_date_range(publish_dt: datetime | None, start_dt: datetime, end_dt: datetime) -> bool:
    if not publish_dt:
        return False
    return start_dt.date() <= publish_dt.date() <= end_dt.date()

def _get_output_path(profile_url: str) -> str:
    username = profile_url.rstrip("/").split("/")[-1].split("?")[0]
    if not username:
        username = "profile"
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return build_output_path("facebook", f"facebook_{username}_{date_str}.xlsx")

def clean_fb_url(url: str) -> str:
    """清洗 URL，剥离用于追踪的冗余参数"""
    if not url: 
        return ""
    if url.startswith("/"):
        url = "https://www.facebook.com" + url

    if "?" in url:
        base, params = url.split("?", 1)
        keep_params = []
        for p in params.split("&"):
            if p.startswith("fbid=") or p.startswith("story_fbid=") or p.startswith("v=") or p.startswith("id="):
                keep_params.append(p)
        if keep_params:
            url = base + "?" + "&".join(keep_params)
        else:
            url = base

    return url.rstrip('/')

def row_from_post(index: int, post: dict[str, Any], profile_url: str) -> dict[str, Any]:
    return {
        "序号": str(index),
        "主页链接": profile_url,
        "帖子链接": post.get("url", ""),
        "发布时间": post.get("published_at", ""),
        "帖子内容": post.get("content", ""),
        "类型": post.get("type", ""),
        "点赞数": post.get("reactions", "0"),
        "评论数": post.get("comment_count", "0"),
        "分享数": post.get("shares", "0"),
        "播放量": post.get("views", "0")
    }

def extract_post_metrics(container, page) -> dict[str, str]:
    """提取点赞、评论、转发、播放量数据"""
    def extract_count_by_role(role_name):
        try:
            node = container.locator(f'[data-ad-rendering-role="{role_name}"]')
            if node.count() > 0:
                num_span = node.first.locator('xpath=../..').locator('span[dir="auto"]')
                if num_span.count() > 0:
                    return num_span.first.inner_text().strip()
        except Exception:
            pass
        return "0"

    stats_likes = extract_count_by_role("like_button")
    stats_comments = extract_count_by_role("comment_button")
    stats_shares = extract_count_by_role("share_button")

    # 提取视频播放量，使用 xpath/文本正则
    views = "0"
    try:
        all_text = container.inner_text() or ""
        view_match = re.search(r'([\d\.,wW万kK]+)\s*(views|次播放|播放|次观看|观看)', all_text, re.IGNORECASE)
        if view_match:
            views = view_match.group(1).strip()
    except Exception:
        pass

    return {
        "reactions": stats_likes,
        "comments": stats_comments,
        "shares": stats_shares,
        "views": views
    }

def extract_publish_time(main_article, page) -> str:
    """通过 hover 悬停提取精确发布时间"""
    publish_time = "未获取"
    try:
        time_links = main_article.locator('a[role="link"]').all()
        for link in time_links:
            href_val = link.get_attribute("href") or ""
            text_val = link.inner_text().strip()
            # 过滤发布时间链接的特征：href 包含 posts, fbid, story_fbid, watch, 且文本长度在 1-20 之间
            is_time_link = ("/posts/" in href_val or "fbid=" in href_val or "story_fbid=" in href_val or "/watch" in href_val) and 1 <= len(text_val) < 20
            if is_time_link:
                try:
                    link.hover()
                    page.wait_for_timeout(1000)
                    tooltip = page.locator('div[role="tooltip"]')
                    if tooltip.count() > 0:
                        publish_time = tooltip.last.inner_text().strip()
                    else:
                        publish_time = link.get_attribute('aria-label') or text_val
                    break
                except Exception:
                    pass
    except Exception:
        pass
    return publish_time

def extract_post_content(main_article, page, ignore_words: list[str]) -> str:
    """提取帖子正文内容"""
    # 尝试点击“展开”或“查看更多”按钮
    try:
        buttons = main_article.locator('div[role="button"]').all()
        for b in buttons:
            t = b.inner_text() or ""
            if any(word in t for word in ["展开", "See more", "查看更多"]):
                try:
                    b.click(timeout=1000)
                    page.wait_for_timeout(500)
                except Exception:
                    pass
    except Exception:
        pass

    text_blocks = []
    try:
        paragraphs = main_article.locator('div[dir="auto"]').all()
        for p_elem in paragraphs:
            p_text = p_elem.inner_text().strip()
            if p_text and p_text not in ignore_words:
                if not any(p_text in existing for existing in text_blocks):
                    text_blocks.append(p_text)
    except Exception:
        pass
    return " | ".join(text_blocks)

def extract_comments(active_container, page, post_url: str, ignore_words: list[str]) -> list[dict[str, Any]]:
    """提取帖子下方的评论"""
    comments_data_list = []
    try:
        # 模拟滚动以便加载评论
        try:
            active_container.evaluate("node => { node.setAttribute('tabindex', '-1'); node.focus(); }")
            page.wait_for_timeout(500)
            box = active_container.bounding_box()
            if box:
                page.mouse.move(box["x"] + 2, box["y"] + 2)
            page.keyboard.press("PageDown")
            page.wait_for_timeout(1000)
            page.keyboard.press("PageDown")
            page.wait_for_timeout(1500)
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1000)
        except Exception:
            pass

        updated_articles = active_container.locator('div[role="article"]').all()
        if len(updated_articles) > 1:
            comments_elements = updated_articles[1:]
            for comment in comments_elements:
                c_text_blocks = []
                c_paragraphs = comment.locator('div[dir="auto"]').all()
                for cp_elem in c_paragraphs:
                    cp_text = cp_elem.inner_text().strip()
                    if cp_text and cp_text not in ignore_words:
                        if not any(cp_text in existing for existing in c_text_blocks):
                            c_text_blocks.append(cp_text)
                c_clean = " | ".join(c_text_blocks)
                if c_clean:
                    comments_data_list.append({
                        "原帖链接": post_url,
                        "评论内容": c_clean,
                        "抓取时间": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        "是否主楼": "否"
                    })
    except Exception:
        pass
    return comments_data_list

def collect_profile_urls(
    page,
    profile_url: str,
    max_scrolls: int,
    limit_time_bool: bool,
    start_dt,
    end_dt,
    log_callback,
    stop_event,
    pause_event,
    scroll_delay_val: int,
    no_new_limit: int,
    max_posts: int,
    skip_navigation: bool = False,
) -> list[str]:
    log_line(log_callback, f"阶段一：开始收集帖子链接 - {profile_url}")
    if not skip_navigation:
        page.goto(profile_url, timeout=PAGE_TIMEOUT_MS)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(3000)
    
    try:
        page.wait_for_selector('div[role="article"]', timeout=15000)
    except Exception:
        log_line(log_callback, "  [!] 警告：15秒内未检测到标准文章区块加载。")
        
    viewport = page.viewport_size
    if viewport:
        page.mouse.move(viewport['width'] / 2, viewport['height'] / 2)
        
    post_urls_ordered = []
    seen_urls = set()
    no_new_count = 0
    
    for scroll_idx in range(max_scrolls):
        if should_stop(stop_event): 
            break
        if wait_if_paused(pause_event, stop_event): 
            break
        if len(post_urls_ordered) >= max_posts:
            log_line(log_callback, f"已收集到目标数量的帖子链接 ({len(post_urls_ordered)}/{max_posts})，停止收集。")
            break
            
        log_line(log_callback, f"滚动收集第 {scroll_idx + 1}/{max_scrolls} 次...")
        
        articles = page.locator('div[role="article"]').all()
        added = 0
        
        for article in articles:
            href_found = False
            
            # 策略一：提取评论按钮的 parent <a>
            try:
                comment_btn = article.locator('[data-ad-rendering-role="comment_button"]')
                if comment_btn.count() > 0:
                    href = comment_btn.first.evaluate(
                        "node => { let a = node.closest('a'); return a ? a.href : null; }"
                    )
                    if href:
                        clean_url = clean_fb_url(href)
                        if clean_url and clean_url not in seen_urls:
                            seen_urls.add(clean_url)
                            post_urls_ordered.append(clean_url)
                            added += 1
                            href_found = True
            except Exception:
                pass
                
            # 策略二：扫描所有 <a> 标签
            if not href_found:
                try:
                    links = article.locator('a').all()
                    for link in links:
                        href = link.get_attribute("href")
                        if not href: 
                            continue
                        href_lower = href.lower()
                        
                        is_post_link = (
                            "/posts/" in href_lower or 
                            "/permalink/" in href_lower or 
                            "/videos/" in href_lower or 
                            "/watch" in href_lower or 
                            "/reel/" in href_lower or 
                            "story_fbid=" in href_lower or 
                            "fbid=" in href_lower
                        )
                        is_bad_link = (
                            "/photo" in href_lower or 
                            "set=" in href_lower or 
                            "type=3" in href_lower or 
                            "profile.php" in href_lower
                        )
                        
                        if is_post_link and not is_bad_link:
                            clean_url = clean_fb_url(href)
                            if clean_url and clean_url not in seen_urls:
                                seen_urls.add(clean_url)
                                post_urls_ordered.append(clean_url)
                                added += 1
                                break
                except Exception:
                    pass
        
        if added == 0:
            no_new_count += 1
            if no_new_count >= no_new_limit:
                log_line(log_callback, f"连续 {no_new_limit} 次滚动未发现新帖子，结束收集。")
                break
        else:
            no_new_count = 0
            
        page.keyboard.press("PageDown")
        page.wait_for_timeout(1000)
        page.keyboard.press("PageDown")
        page.wait_for_timeout(scroll_delay_val)
        
    return post_urls_ordered[:max_posts]

def parse_deep_post(page, url: str, collect_comments: bool = False, ignore_words: list[str] | None = None) -> dict[str, Any]:
    page.goto(url, timeout=PAGE_TIMEOUT_MS)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(3500)

    dialog_locator = page.locator('div[role="dialog"]')
    if dialog_locator.count() > 0:
        active_container = dialog_locator.last
    else:
        active_container = page.locator('div[role="main"]')
        if active_container.count() == 0:
            active_container = page.locator('body')

    all_articles = active_container.locator('div[role="article"]').all()
    if not all_articles:
        raise ValueError("未发现标准帖子区块")

    main_article = all_articles[0]

    # 1. 提取正文
    content = extract_post_content(main_article, page, IGNORE_WORDS)

    # 2. 提取发布时间
    published_at = extract_publish_time(main_article, page)

    # 3. 确定帖子类型
    url_lower = url.lower()
    is_pure_video = ("/videos/" in url_lower or "/watch" in url_lower or "/reel/" in url_lower or "v=" in url_lower)

    if is_pure_video:
        post_type = "video"
    else:
        img_count = 0
        try:
            imgs = main_article.locator('img').all()
            for img in imgs:
                src = img.get_attribute("src") or ""
                if src and "emoji" not in src and "spacer" not in src and not src.startswith("data:image/svg+xml"):
                    img_count += 1
        except Exception:
            pass
        post_type = "image" if img_count > 0 else "text"

    # 4. 提取热度指标
    metrics = extract_post_metrics(active_container, page)

    # 5. 提取评论（可选，在页面跳转前完成）
    comment_list = []
    if collect_comments:
        comment_list = extract_comments(active_container, page, url, ignore_words or IGNORE_WORDS)

    return {
        "url": url,
        "published_at": published_at,
        "content": content[:5000],
        "type": post_type,
        "reactions": metrics["reactions"],
        "comment_count": metrics["comments"],
        "shares": metrics["shares"],
        "views": metrics["views"],
        "comment_list": comment_list,
    }

def run_facebook_profile_works_spider(
    profile_urls_text: str, 
    limit_time_str: str, 
    start_date_str: str, 
    end_date_str: str, 
    force_exact_time_str: str,
    log_callback, 
    stop_event, 
    pause_event, 
    **config
) -> str:
    urls = [u.strip() for u in profile_urls_text.splitlines() if u.strip()]
    if not urls:
        return "未提供任何主页链接"
    
    limit_time_bool = (limit_time_str == "是")
    start_dt = None
    end_dt = None
    if limit_time_bool:
        start_dt, end_dt = parse_date_range(start_date_str, end_date_str)
    
    page_timeout = int(config.get("page_load_timeout", PAGE_TIMEOUT_MS))
    scroll_delay_val = int(config.get("scroll_delay", SCROLL_DELAY_MS))
    no_new_limit = int(config.get("no_new_scroll_limit", NO_NEW_LIMIT))
    max_scrolls = int(config.get("max_scrolls", 200))
    save_batch_size = int(config.get("save_batch_size", SAVE_BATCH_SIZE))
    
    # 获取新增的用户配置
    max_posts = int(config.get("max_posts", 100))
    collect_comments_bool = (config.get("collect_comments", "否") == "是")
    
    try:
        with sync_playwright() as p:
            browser, playwright_context = connect_existing_chromium(p, DEFAULT_X_CDP_URL, log_callback=log_callback)
            if not browser:
                log_line(log_callback, "无法连接到本地浏览器，请确保以调试模式启动 Chrome。")
                return "浏览器连接失败"
                
            page = playwright_context.new_page()
            
            for profile_url in urls:
                if should_stop(stop_event):
                    break
                
                collected_urls = collect_profile_urls(
                    page, 
                    profile_url, 
                    max_scrolls, 
                    limit_time_bool, 
                    start_dt, 
                    end_dt, 
                    log_callback, 
                    stop_event, 
                    pause_event, 
                    scroll_delay_val, 
                    no_new_limit, 
                    max_posts
                )
                
                log_line(log_callback, f"收集完毕，共抓取到 {len(collected_urls)} 个帖子链接。准备进入详情页深度解析...")
                
                output_path = _get_output_path(profile_url)
                fieldnames = ["序号", "主页链接", "帖子链接", "发布时间", "帖子内容", "类型", "点赞数", "评论数", "分享数", "播放量"]
                
                # 配置 Sheets
                sheets_fields = {"帖子内容": fieldnames}
                if collect_comments_bool:
                    comment_fieldnames = ["原帖链接", "评论内容", "抓取时间", "是否主楼"]
                    sheets_fields["评论详情"] = comment_fieldnames
                    
                writer = MultiSheetXlsxWriter(output_path, sheets_fields)
                
                total_written = 0
                comments_written = 0
                
                for idx, post_url in enumerate(collected_urls):
                    if should_stop(stop_event): 
                        break
                    if wait_if_paused(pause_event, stop_event): 
                        break
                    
                    try:
                        log_line(log_callback, f"抓取详情 [{idx+1}/{len(collected_urls)}]: {post_url}")
                        post_data = parse_deep_post(page, post_url, collect_comments=collect_comments_bool)

                        # 精确时间过滤
                        if limit_time_bool and start_dt and end_dt:
                            pub_dt = parse_fb_time_string(post_data.get("published_at", ""))
                            if pub_dt and not in_date_range(pub_dt, start_dt, end_dt):
                                log_line(log_callback, f"  剔除: 精确时间 {pub_dt.date()} 不在范围内")
                                continue
                            if pub_dt:
                                post_data["published_at"] = pub_dt.strftime("%Y-%m-%d %H:%M:%S")

                        row = row_from_post(total_written + 1, post_data, profile_url)
                        writer.writerow("帖子内容", row)
                        total_written += 1

                        # 评论已在 parse_deep_post 内部提取
                        if collect_comments_bool:
                            for c_row in post_data.get("comment_list", []):
                                writer.writerow("评论详情", c_row)
                                comments_written += 1
                            if post_data.get("comment_list"):
                                log_line(log_callback, f"  成功提取到 {len(post_data['comment_list'])} 条评论。")
                                    
                        if total_written % save_batch_size == 0:
                            writer.save()
                            
                        # 冷却时间
                        cooldown = random.uniform(1.0, 3.0)
                        page.wait_for_timeout(cooldown * 1000)
                        
                    except Exception as e:
                        log_line(log_callback, f"  详情解析失败: {e}")
                        
                writer.save()
                msg = f"完成 {profile_url}，有效导出 {total_written} 条帖子数据。"
                if collect_comments_bool:
                    msg += f" 导出评论 {comments_written} 条。"
                log_line(log_callback, msg)
                
            page.close()
            playwright_context.close()
            browser.close()
            
            return "采集全部完成"
    except Exception as e:
        import traceback
        return f"运行异常: {e}\n{traceback.format_exc()}"
