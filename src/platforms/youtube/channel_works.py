from __future__ import annotations

import re
import time
from urllib.parse import urlparse

try:
    from googleapiclient.discovery import build
except ModuleNotFoundError:
    build = None

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from src.core import DEFAULT_X_CDP_URL, MultiSheetXlsxWriter, XlsxRowWriter, build_output_path, connect_existing_chromium, interruptible_sleep, sanitize_csv_cell, should_stop, wait_if_paused
from src.platforms.youtube.comments import fetch_top_level_comments
from src.platforms.youtube.keyword import parse_date_range


CSV_FIELDS = ["序号", "作者主页链接", "作品链接", "作品内容", "浏览量", "评论数", "点赞数"]
PAGE_LOAD_TIMEOUT = 45000
INITIAL_LOAD_DELAY = 1.8
POST_SCROLL_DELAY = 0.8
POST_SCROLL_PX = 2800
NO_NEW_POST_LIMIT = 6
DEFAULT_MAX_POST_SCROLLS = 120
DEFAULT_MAX_VIDEO_ITEMS = 500
SAVE_BATCH_SIZE = 10


def log_line(log_callback, text: str):
    if log_callback:
        log_callback(text)


def clean_channel_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://www.youtube.com" + value
    if not value.startswith("http"):
        value = "https://" + value
    value = value.split("?")[0].split("#")[0].rstrip("/")
    value = re.sub(r"/(videos|shorts|posts|community|featured)$", "", value, flags=re.I)
    return value


def parse_channel_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        url = clean_channel_url(stripped.split()[0])
        if "youtube.com/" in url and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def parse_channel_url(url: str) -> tuple[str, str]:
    normalized = clean_channel_url(url)
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


def posts_url(channel_url: str) -> str:
    return f"{clean_channel_url(channel_url)}/posts"


def normalize_youtube_href(href: str) -> str:
    value = (href or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://www.youtube.com" + value
    value = value.split("&pp=")[0].split("?pp=")[0]
    watch_match = re.search(r"(https://www\.youtube\.com/watch\?v=[\w-]+)", value)
    if watch_match:
        return watch_match.group(1)
    shorts_match = re.search(r"(https://www\.youtube\.com/shorts/[\w-]+)", value)
    if shorts_match:
        return shorts_match.group(1)
    return value


def normalize_metric_text(text: str) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        return ""
    match = re.search(r"(\d[\d,.]*(?:\.\d+)?\s*(?:K|M|B|万|萬|亿|億)?)", value, flags=re.I)
    return match.group(1).strip() if match else ""


def tab_url(channel_url: str, tab: str) -> str:
    return f"{clean_channel_url(channel_url)}/{tab}"


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def fetch_channel_item(youtube, channel_url: str) -> dict:
    hint_type, hint_value = parse_channel_url(channel_url)
    if not hint_value:
        return {}

    if hint_type == "id":
        response = youtube.channels().list(part="snippet,contentDetails", id=hint_value).execute()
    elif hint_type == "username":
        response = youtube.channels().list(part="snippet,contentDetails", forUsername=hint_value).execute()
    elif hint_type == "handle":
        response = youtube.channels().list(part="snippet,contentDetails", forHandle=hint_value).execute()
    else:
        search_response = youtube.search().list(part="id", q=hint_value, type="channel", maxResults=1).execute()
        items = search_response.get("items", [])
        channel_id = items[0].get("id", {}).get("channelId", "") if items else ""
        if not channel_id:
            return {}
        response = youtube.channels().list(part="snippet,contentDetails", id=channel_id).execute()

    items = response.get("items", [])
    return items[0] if items else {}


def collect_upload_video_ids(youtube, uploads_playlist_id: str, max_video_items: int, limit_time_bool: bool, start_dt, end_dt, log_callback, stop_event=None, pause_event=None) -> list[str]:
    video_ids: list[str] = []
    seen = set()
    page_token = None
    max_video_items = max(1, int(max_video_items if max_video_items is not None else DEFAULT_MAX_VIDEO_ITEMS))

    while len(video_ids) < max_video_items:
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        response = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=min(50, max_video_items - len(video_ids)),
            pageToken=page_token,
        ).execute()
        
        stopped_by_date = False
        for item in response.get("items", []):
            pub_time = item.get("contentDetails", {}).get("videoPublishedAt", "")
            if limit_time_bool and pub_time:
                from datetime import datetime
                try:
                    pub_dt = datetime.strptime(pub_time.split("T")[0], "%Y-%m-%d")
                    if pub_dt.date() < start_dt.date():
                        stopped_by_date = True
                        break
                    if pub_dt.date() > end_dt.date():
                        continue
                except Exception:
                    pass
                    
            video_id = item.get("contentDetails", {}).get("videoId", "")
            if video_id and video_id not in seen:
                seen.add(video_id)
                video_ids.append(video_id)

        log_line(log_callback, f"  API 已读取视频类作品 {len(video_ids)} 条。")
        if stopped_by_date:
            log_line(log_callback, "  API 已读取到早于开始日期的视频，停止加载更多。")
            break
            
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return video_ids


def video_rows_from_api(youtube, video_ids: list[str], stop_event=None, pause_event=None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for batch in chunked(video_ids, 50):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        response = youtube.videos().list(part="snippet,statistics", id=",".join(batch), maxResults=50).execute()
        for item in response.get("items", []):
            if should_stop(stop_event):
                break
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            video_id = item.get("id", "")
            title = (snippet.get("title") or "").strip()
            if not video_id or not title:
                continue
            rows.append(
                {
                    "link": f"https://www.youtube.com/watch?v={video_id}",
                    "content": f"{title}[视频]",
                    "views": stats.get("viewCount", ""),
                    "comments": stats.get("commentCount", ""),
                    "likes": stats.get("likeCount", ""),
                }
            )
    return rows


def collect_video_works_with_api(youtube, channel_url: str, max_video_items: int, limit_time_bool: bool, start_dt, end_dt, log_callback, stop_event=None, pause_event=None) -> list[dict[str, str]]:
    channel_item = fetch_channel_item(youtube, channel_url)
    if not channel_item:
        log_line(log_callback, "  API 未找到频道信息。")
        return []

    uploads_playlist_id = (
        channel_item.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads", "")
    )
    if not uploads_playlist_id:
        log_line(log_callback, "  API 未找到 uploads 播放列表。")
        return []

    title = channel_item.get("snippet", {}).get("title", "")
    if title:
        log_line(log_callback, f"  API 识别频道：{title}")
    video_ids = collect_upload_video_ids(youtube, uploads_playlist_id, max_video_items, limit_time_bool, start_dt, end_dt, log_callback, stop_event, pause_event)
    rows = video_rows_from_api(youtube, video_ids, stop_event, pause_event)
    log_line(log_callback, f"  API 视频类作品完成：{len(rows)} 条。")
    return rows


def extract_visible_video_cards(page, tab: str) -> list[dict[str, str]]:
    return page.evaluate(
        """({ tab }) => {
            const absUrl = href => {
                if (!href) return '';
                try {
                    const value = new URL(href, location.origin).href.split('&pp=')[0].split('?pp=')[0];
                    const watchMatch = value.match(/https:\\/\\/www\\.youtube\\.com\\/watch\\?v=[\\w-]+/);
                    if (watchMatch) return watchMatch[0];
                    const shortsMatch = value.match(/https:\\/\\/www\\.youtube\\.com\\/shorts\\/[\\w-]+/);
                    if (shortsMatch) return shortsMatch[0];
                    return value;
                }
                catch (error) { return ''; }
            };
            const cleanTitle = text => {
                let value = (text || '').replace(/\\s+/g, ' ').trim();
                if (!value) return '';
                value = value.replace(/\\s+-\\s+play short$/i, '').replace(/\\s+-\\s+播放 Shorts?$/i, '').trim();
                value = value.replace(/\\s+by\\s+.+?\\s+\\d[\\d,.]*\\s+views?.*$/i, '').trim();
                value = value.replace(/\\s+作者：.+?\\s+\\d[\\d,.]*\\s*次观看.*$/i, '').trim();
                value = value.replace(/\\s+作成者:.+?\\s+\\d[\\d,.]*\\s*回視聴.*$/i, '').trim();
                return value;
            };
            const nodeText = node => (node ? (node.innerText || node.textContent || '').trim() : '');
            const titleFrom = (card, link) => {
                const candidates = [
                    link.getAttribute('title'),
                    nodeText(link),
                    nodeText(card.querySelector('#video-title')),
                    nodeText(card.querySelector('a#video-title-link')),
                    nodeText(card.querySelector('yt-lockup-metadata-view-model h3')),
                    nodeText(card.querySelector('h3')),
                    link.getAttribute('aria-label'),
                ];
                for (const candidate of candidates) {
                    const title = cleanTitle(candidate);
                    if (title) return title;
                }
                return '';
            };
            const metricLine = root => {
                const lines = (root.innerText || '').split('\\n').map(line => line.trim()).filter(Boolean);
                return lines.find(line => /views|观看|次观看|回視聴|再生/i.test(line)) || '';
            };

            const linkSelector = tab === 'videos'
                ? 'a[href*="/watch?v="]'
                : 'a[href*="/shorts/"]';
            const cardSelector = [
                'ytd-rich-item-renderer',
                'ytd-video-renderer',
                'ytd-grid-video-renderer',
                'ytd-reel-item-renderer',
                'ytd-rich-grid-media',
                'yt-lockup-view-model',
                'ytm-shorts-lockup-view-model',
                'ytm-video-with-context-renderer',
            ].join(',');

            const results = [];
            const seen = new Set();
            for (const link of document.querySelectorAll(linkSelector)) {
                const href = absUrl(link.getAttribute('href') || link.href || '');
                if (!href) continue;
                if (seen.has(href)) continue;
                seen.add(href);
                const card = link.closest(cardSelector) || link;
                const title = titleFrom(card, link);
                if (!title) continue;
                results.push({
                    link: href,
                    content: `${title}[视频]`,
                    views: metricLine(card),
                    comments: '',
                    likes: '',
                });
            }
            return results;
        }""",
        {"tab": tab},
    )


def collect_video_tab_with_playwright(page, channel_url: str, tab: str, max_scrolls: int, log_callback, stop_event=None, pause_event=None,
                                      page_timeout=None, scroll_delay=None, no_new_limit=None, scroll_px=None) -> list[dict[str, str]]:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if scroll_delay is None:
        scroll_delay = POST_SCROLL_DELAY
    if no_new_limit is None:
        no_new_limit = NO_NEW_POST_LIMIT
    if scroll_px is None:
        scroll_px = POST_SCROLL_PX

    url = tab_url(channel_url, tab)
    label = "Videos" if tab == "videos" else "Shorts"
    log_line(log_callback, f"  Playwright 读取 {label}：{url}")
    page.goto(url, wait_until="domcontentloaded", timeout=page_timeout)
    if interruptible_sleep(INITIAL_LOAD_DELAY, stop_event):
        return []
    wait_selector = 'a[href*="/watch?v="]' if tab == "videos" else 'a[href*="/shorts/"]'
    try:
        page.wait_for_selector(wait_selector, timeout=12000)
    except PlaywrightTimeoutError:
        log_line(log_callback, f"  未等到 {label} 链接，继续滚动尝试。")

    works: list[dict[str, str]] = []
    seen_links = set()
    no_new_count = 0
    max_scrolls = max(1, int(max_scrolls if max_scrolls is not None else DEFAULT_MAX_POST_SCROLLS))

    for scroll_index in range(max_scrolls):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        added = 0
        for item in extract_visible_video_cards(page, tab):
            link = normalize_youtube_href(item.get("link", ""))
            content = str(item.get("content") or "").strip()
            if not link or not content or link in seen_links:
                continue
            seen_links.add(link)
            works.append(
                {
                    "link": sanitize_csv_cell(link),
                    "content": sanitize_csv_cell(content),
                    "views": sanitize_csv_cell(normalize_metric_text(item.get("views", ""))),
                    "comments": "",
                    "likes": "",
                }
            )
            added += 1

        if added:
            log_line(log_callback, f"    {label} 滚动 {scroll_index + 1}/{max_scrolls}：新增 {added} 条，累计 {len(works)} 条。")
            no_new_count = 0
        else:
            no_new_count += 1
            if no_new_count >= no_new_limit:
                log_line(log_callback, f"    连续 {no_new_limit} 次没有新增，停止 {label}。")
                break

        page.evaluate(f"window.scrollBy(0, {scroll_px})")
        if interruptible_sleep(scroll_delay, stop_event):
            break

    return works


def extract_visible_posts(page) -> list[dict[str, str]]:
    return page.evaluate(
        """() => {
            const absUrl = href => {
                if (!href) return '';
                try { return new URL(href, location.origin).href.split('&pp=')[0].split('?pp=')[0]; }
                catch (error) { return ''; }
            };
            const uniqueTextLines = text => {
                const seen = new Set();
                const lines = [];
                for (const line of (text || '').split('\\n')) {
                    const clean = line.trim();
                    if (!clean || seen.has(clean)) continue;
                    seen.add(clean);
                    lines.push(clean);
                }
                return lines;
            };
            const textFrom = (root, selectors) => {
                for (const selector of selectors) {
                    const node = root.querySelector(selector);
                    const text = node ? (node.innerText || node.textContent || '').trim() : '';
                    if (text) return text;
                }
                return '';
            };
            const metricLine = (root, patterns) => {
                const lines = [
                    ...uniqueTextLines(root.innerText || ''),
                    ...Array.from(root.querySelectorAll('[aria-label], [title]')).flatMap(node => [
                        node.getAttribute('aria-label') || '',
                        node.getAttribute('title') || '',
                    ]).map(line => line.trim()).filter(Boolean),
                ];
                return lines.find(line => patterns.some(pattern => pattern.test(line))) || '';
            };
            const countFromEndpoint = (root, names) => {
                const html = root.outerHTML || '';
                for (const name of names) {
                    const patterns = [
                        new RegExp(`"${name}"\\\\s*:\\\\s*"?(\\\\d[\\\\d,]*)"?`, 'i'),
                        new RegExp(`"${name}"\\\\s*:\\\\s*\\\\{[^{}]*"simpleText"\\\\s*:\\\\s*"([^"]+)"`, 'i'),
                        new RegExp(`"${name}"\\\\s*:\\\\s*\\\\{[^{}]*"text"\\\\s*:\\\\s*"([^"]+)"`, 'i'),
                    ];
                    for (const pattern of patterns) {
                        const match = html.match(pattern);
                        if (match && match[1]) return match[1];
                    }
                }
                return '';
            };
            const extractMetric = (root, type) => {
                if (type === 'views') {
                    return (
                        metricLine(root, [/views?/i, /观看/, /次观看/, /回視聴/, /再生/]) ||
                        countFromEndpoint(root, ['viewCount', 'views'])
                    );
                }
                if (type === 'comments') {
                    return (
                        metricLine(root, [/comments?/i, /评论/, /留言/, /則留言/, /件のコメント/]) ||
                        countFromEndpoint(root, ['commentCount', 'commentsCount'])
                    );
                }
                return (
                    metricLine(root, [/likes?/i, /赞/, /讚/, /高評価/, /件の高評価/]) ||
                    countFromEndpoint(root, ['likeCount', 'likesCount'])
                );
            };
            const findPostLink = root => {
                for (const node of root.querySelectorAll('a[href*="/post/"], a[href*="/channel/"][href*="/community?lb="]')) {
                    const href = absUrl(node.getAttribute('href') || node.href || '');
                    if (href) return href;
                }
                return location.href;
            };
            const nonAvatarImages = root => {
                return Array.from(root.querySelectorAll('img')).filter(img => {
                    const src = img.getAttribute('src') || '';
                    const width = Number(img.naturalWidth || img.width || 0);
                    const height = Number(img.naturalHeight || img.height || 0);
                    if (src.includes('yt3.ggpht.com') && width <= 160 && height <= 160) return false;
                    return width > 80 || height > 80 || src.includes('ytimg.com');
                });
            };

            const results = [];
            const postSelectors = [
                'ytd-backstage-post-thread-renderer',
                'ytd-post-renderer',
                'ytd-rich-item-renderer:has(ytd-backstage-post-thread-renderer)',
            ].join(',');
            for (const post of document.querySelectorAll(postSelectors)) {
                const text = textFrom(post, [
                    '#content-text',
                    'yt-formatted-string#content-text',
                    'yt-attributed-string#content-text',
                    '[id="content-text"]',
                ]);
                const parts = [];
                if (text) parts.push(text);
                if (nonAvatarImages(post).length) parts.push('[图片]');
                const content = parts.join('\\n').trim();
                if (!content) continue;
                results.push({
                    link: findPostLink(post),
                    content,
                    views: extractMetric(post, 'views'),
                    comments: extractMetric(post, 'comments'),
                    likes: extractMetric(post, 'likes'),
                });
            }
            return results;
        }"""
    )


def collect_posts_with_playwright(page, channel_url: str, max_post_scrolls: int, log_callback, stop_event=None, pause_event=None,
                                   page_timeout=None, scroll_delay=None, no_new_limit=None, scroll_px=None) -> list[dict[str, str]]:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if scroll_delay is None:
        scroll_delay = POST_SCROLL_DELAY
    if no_new_limit is None:
        no_new_limit = NO_NEW_POST_LIMIT
    if scroll_px is None:
        scroll_px = POST_SCROLL_PX

    url = posts_url(channel_url)
    page.goto(url, wait_until="domcontentloaded", timeout=page_timeout)
    if interruptible_sleep(INITIAL_LOAD_DELAY, stop_event):
        return []

    posts: list[dict[str, str]] = []
    seen_links = set()
    no_new_count = 0
    max_post_scrolls = max(1, int(max_post_scrolls if max_post_scrolls is not None else DEFAULT_MAX_POST_SCROLLS))
    log_line(log_callback, f"  Playwright 读取 Posts：{url}")

    for scroll_index in range(max_post_scrolls):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        added = 0
        for item in extract_visible_posts(page):
            link = normalize_youtube_href(item.get("link", ""))
            content = str(item.get("content") or "").strip()
            if not link or not content or link in seen_links:
                continue
            seen_links.add(link)
            posts.append(
                {
                    "link": sanitize_csv_cell(link),
                    "content": sanitize_csv_cell(content),
                    "views": sanitize_csv_cell(normalize_metric_text(item.get("views", ""))),
                    "comments": sanitize_csv_cell(normalize_metric_text(item.get("comments", ""))),
                    "likes": sanitize_csv_cell(normalize_metric_text(item.get("likes", ""))),
                }
            )
            added += 1

        if added:
            log_line(log_callback, f"    Posts 滚动 {scroll_index + 1}/{max_post_scrolls}：新增 {added} 条，累计 {len(posts)} 条。")
            no_new_count = 0
        else:
            no_new_count += 1
            if no_new_count >= no_new_limit:
                log_line(log_callback, f"    连续 {no_new_limit} 次没有新增，停止 Posts。")
                break

        page.evaluate(f"window.scrollBy(0, {scroll_px})")
        if interruptible_sleep(scroll_delay, stop_event):
            break

    return posts


def row_from_work(index: int, work: dict[str, str], channel_url: str = "") -> dict[str, str]:
    return {
        "序号": str(index),
        "作者主页链接": channel_url,
        "作品链接": work.get("link", ""),
        "作品内容": work.get("content", ""),
        "浏览量": work.get("views", ""),
        "评论数": work.get("comments", ""),
        "点赞数": work.get("likes", ""),
    }


def run_youtube_channel_works_spider(
    api_key: str,
    channel_urls_text: str,
    max_video_items: int = DEFAULT_MAX_VIDEO_ITEMS,
    max_post_scrolls: int = DEFAULT_MAX_POST_SCROLLS,
    limit_time_str: str = "否",
    start_date: str = "",
    end_date: str = "",
    get_comments_str: str = "否",
    max_comments: int = 100,
    log_callback=None,
    finish_callback=None,
    stop_event=None,
    config: dict | None = None,
    pause_event=None,
):
    if config is None:
        config = {}
    page_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    scroll_delay_val = float(config.get("scroll_delay", POST_SCROLL_DELAY))
    no_new_limit = int(config.get("no_new_post_limit", NO_NEW_POST_LIMIT))
    scroll_px_val = int(config.get("scroll_px", POST_SCROLL_PX))
    max_post_scrolls = int(config.get("max_post_scrolls", max_post_scrolls))

    completed_path = None
    browser = None
    page = None
    playwright_context = None
    try:
        channel_urls = parse_channel_urls(channel_urls_text)
        if not channel_urls:
            log_line(log_callback, "未读取到有效的 YouTube 作者主页链接。")
            return

        youtube = None
        if build is None:
            log_line(log_callback, "缺少依赖：google-api-python-client。Videos/Shorts 将尝试浏览器 fallback。")
        else:
            try:
                youtube = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
            except Exception as exc:
                log_line(log_callback, f"YouTube API 初始化失败，Videos/Shorts 将尝试浏览器 fallback：{exc}")

        limit_time_bool = limit_time_str == "是"
        get_comments_bool = get_comments_str == "是"
        start_dt, end_dt = None, None
        if limit_time_bool:
            start_dt, end_dt = parse_date_range(start_date, end_date)
            
        output_path = build_output_path("youtube", f"youtube_channel_works_{time.strftime('%Y%m%d')}.xlsx")
        if get_comments_bool:
            comment_fields = ["序号", "作品链接", "评论的点赞量", "评论内容", "评论发布时间"]
            writer = MultiSheetXlsxWriter(output_path, {"作品信息": CSV_FIELDS, "评论信息": comment_fields})
        else:
            writer = XlsxRowWriter(output_path, CSV_FIELDS)
        serial_number = 1

        def ensure_page():
            nonlocal browser, page, playwright_context
            if sync_playwright is None:
                return None
            if playwright_context is None:
                log_line(log_callback, "  开始接管本地 Chrome 读取页面...")
                playwright_context = sync_playwright().start()
                browser, context = connect_existing_chromium(playwright_context, DEFAULT_X_CDP_URL, log_callback=log_callback)
                page = context.new_page()
            return page

        for channel_index, channel_url in enumerate(channel_urls, 1):
            if should_stop(stop_event):
                log_line(log_callback, "任务已停止。")
                break
            if wait_if_paused(pause_event, stop_event):
                break

            log_line(log_callback, f"[{channel_index}/{len(channel_urls)}] 读取作者主页：{channel_url}")
            works: list[dict[str, str]] = []
            should_fallback_video_tabs = False
            if youtube is None:
                should_fallback_video_tabs = True
                log_line(log_callback, "  YouTube API 不可用，尝试用浏览器读取 Videos/Shorts。")
            else:
                try:
                    works = collect_video_works_with_api(youtube, channel_url, max_video_items, limit_time_bool, start_dt, end_dt, log_callback, stop_event, pause_event)
                    if not works:
                        should_fallback_video_tabs = True
                        log_line(log_callback, "  API 未返回 Videos/Shorts，尝试用浏览器读取。")
                except Exception as exc:
                    should_fallback_video_tabs = True
                    log_line(log_callback, f"  YouTube API 读取失败，尝试用浏览器读取 Videos/Shorts：{exc}")

            if sync_playwright is None:
                if should_fallback_video_tabs:
                    log_line(log_callback, "  缺少依赖：playwright。无法浏览器 fallback Videos/Shorts。")
                log_line(log_callback, "  缺少依赖：playwright。跳过 Posts。")
            elif should_fallback_video_tabs and not should_stop(stop_event):
                try:
                    active_page = ensure_page()
                    if active_page is not None:
                        works.extend(collect_video_tab_with_playwright(active_page, channel_url, "videos", max_post_scrolls, log_callback, stop_event, pause_event, page_timeout, scroll_delay_val, no_new_limit, scroll_px_val))
                        if not should_stop(stop_event):
                            works.extend(collect_video_tab_with_playwright(active_page, channel_url, "shorts", max_post_scrolls, log_callback, stop_event, pause_event, page_timeout, scroll_delay_val, no_new_limit, scroll_px_val))
                except PlaywrightTimeoutError:
                    log_line(log_callback, "  跳过浏览器 Videos/Shorts：页面加载超时。")
                except Exception as exc:
                    log_line(log_callback, f"  跳过浏览器 Videos/Shorts：{exc}")

            if sync_playwright is not None and not should_stop(stop_event):
                try:
                    active_page = ensure_page()
                    if active_page is not None:
                        works.extend(collect_posts_with_playwright(active_page, channel_url, max_post_scrolls, log_callback, stop_event, pause_event, page_timeout, scroll_delay_val, no_new_limit, scroll_px_val))
                except PlaywrightTimeoutError:
                    log_line(log_callback, "  跳过 Posts：页面加载超时。")
                except Exception as exc:
                    log_line(log_callback, f"  跳过 Posts：{exc}")

            save_batch_size = int(config.get("save_batch_size", SAVE_BATCH_SIZE))
            channel_written = 0
            rows_buffer: list[dict[str, str]] = []

            def _flush_rows():
                nonlocal channel_written
                if not rows_buffer:
                    return
                if get_comments_bool:
                    for r in rows_buffer:
                        writer.writerow("作品信息", r)
                else:
                    writer.writerows(rows_buffer)
                writer.save()
                channel_written += len(rows_buffer)
                rows_buffer.clear()

            for work in works:
                if should_stop(stop_event):
                    break
                rows_buffer.append(row_from_work(serial_number, work, channel_url))

                if get_comments_bool and youtube is not None:
                    try:
                        work_link = work.get("link", "")
                        video_id = ""
                        if "watch?v=" in work_link:
                            video_id = work_link.split("v=")[1].split("&")[0]
                        elif "shorts/" in work_link:
                            video_id = work_link.split("shorts/")[1].split("?")[0]

                        if video_id:
                            comments = fetch_top_level_comments(youtube, video_id, max_comments, log_callback, stop_event, pause_event)
                            comments.sort(key=lambda item: item["like_count"], reverse=True)
                            for comment in comments[:max_comments]:
                                comment_row = {
                                    "序号": str(serial_number),
                                    "作品链接": work_link,
                                    "评论的点赞量": str(comment["like_count"]),
                                    "评论内容": comment["text"],
                                    "评论发布时间": comment.get("published_at", "")
                                }
                                writer.writerow("评论信息", comment_row)
                    except Exception as exc:
                        log_line(log_callback, f"    提取评论失败：{exc}")

                serial_number += 1

                if len(rows_buffer) >= save_batch_size:
                    _flush_rows()

            _flush_rows()
            log_line(log_callback, f"  作者主页完成：写入 {channel_written} 条。")

        if page and not page.is_closed():
            page.close()
        if browser and browser.is_connected():
            browser.close()
        if playwright_context is not None:
            playwright_context.stop()

        completed_path = output_path
        writer.save()
        log_line(log_callback, f"完成，已保存：{output_path}")
    except Exception as exc:
        log_line(log_callback, f"运行失败：{exc}")
    finally:
        try:
            if page and not page.is_closed():
                page.close()
        except Exception:
            pass
        try:
            if browser and browser.is_connected():
                browser.close()
        except Exception:
            pass
        try:
            if playwright_context is not None:
                playwright_context.stop()
        except Exception:
            pass
        if finish_callback:
            finish_callback(completed_path)
