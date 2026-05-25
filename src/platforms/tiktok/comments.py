from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlencode, urlparse

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
    expand_compact_number,
    interruptible_sleep,
    random_cooldown,
    sanitize_csv_row,
    sanitize_csv_rows,
    should_stop,
    wait_if_paused,
)


CSV_FIELDS = ["编号", "视频链接", "评论的点赞量", "评论内容", "发布时间"]
TOP_COMMENT_LIMIT = 100
DEFAULT_SCAN_LIMIT = 500
PAGE_LOAD_TIMEOUT = 45000
COMMENT_WAIT_TIMEOUT = 12000
SCROLL_PAUSE = 1.4
NO_NEW_SCROLL_LIMIT = 8
MAX_SCROLL_ROUNDS = 80
VIDEO_BATCH_COOLDOWN_EVERY = 3
VIDEO_BATCH_COOLDOWN_MIN = 4.0
VIDEO_BATCH_COOLDOWN_MAX = 9.0


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
    return value.split("#")[0].rstrip("/")


def extract_video_id(url: str) -> str:
    match = re.search(r"/video/(\d+)", url or "")
    return match.group(1) if match else ""


def parse_video_entries(txt_path: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen_video_ids: set[str] = set()
    with open(txt_path, "r", encoding="utf-8-sig") as file:
        for line in file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            video_url = clean_url(stripped.split()[0])
            video_id = extract_video_id(video_url)
            if not video_id or video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
            entries.append({"编号": str(len(entries) + 1), "视频链接": video_url, "视频ID": video_id})
    return entries


def count_to_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = expand_compact_number(str(value)).replace(",", "").strip()
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else 0


def detect_non_text_type(comment: dict[str, Any]) -> str:
    try:
        blob = json.dumps(comment, ensure_ascii=False).lower()
    except Exception:
        blob = " ".join(str(key).lower() for key in comment.keys())
    if "sticker" in blob or "sticker_text" in blob:
        return "贴纸"
    if "gif" in blob:
        return "GIF"
    if "image" in blob or "photo" in blob or "picture" in blob:
        return "图片"
    if "video" in blob:
        return "视频"
    return "非文本"


def normalize_comment_text(comment: dict[str, Any]) -> str:
    text = str(comment.get("text") or comment.get("comment") or comment.get("content") or "").replace("\r", " ").replace("\n", " ").strip()
    if text:
        return text
    return f"[{detect_non_text_type(comment)}]"


def _format_timestamp(value) -> str:
    if value is None:
        return ""
    try:
        ts = int(value)
        if ts > 0:
            if ts > 10_000_000_000:
                ts = ts // 1000
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        pass
    text = str(value).strip()
    if text and text not in {"0", "None", "null", "undefined"}:
        return text
    return ""


def comment_like_count(comment: dict[str, Any]) -> int:
    for key in ("digg_count", "diggCount", "like_count", "likeCount"):
        if key in comment:
            return count_to_int(comment.get(key))
    return 0


def is_comment_list_response(url: str) -> bool:
    url_lower = url.lower()
    if "reply" in url_lower:
        return False
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.rstrip("/") == "/api/comment/list"


def has_more_comments(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true"}


class CommentCollector:
    def __init__(self, max_scan_comments: int, log_callback, comment_top_limit: int | None = None) -> None:
        self.comment_top_limit = comment_top_limit if comment_top_limit is not None else TOP_COMMENT_LIMIT
        self.max_scan_comments = max(self.comment_top_limit, int(max_scan_comments or DEFAULT_SCAN_LIMIT))
        self.log_callback = log_callback
        self.comments: list[dict[str, Any]] = []
        self.seen_ids: set[str] = set()
        self.seen_dom_fingerprints: set[str] = set()
        self.last_has_more: int | None = None
        self.response_count = 0

    def _text_fingerprint(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _has_existing_non_dom_text(self, text: str) -> bool:
        fingerprint = self._text_fingerprint(text)
        return any(
            item.get("source") != "dom"
            and self._text_fingerprint(item.get("text", "")) == fingerprint
            for item in self.comments
        )

    def _dom_fingerprint(self, comment_id: str, text: str, like_count: Any) -> str:
        parts = str(comment_id or "").split("|", 3)
        if len(parts) >= 4 and parts[0] == "dom":
            author_key = parts[1].strip()
            if author_key:
                return f"{author_key}|{self._text_fingerprint(text)}"
        return ""

    def add_comment(self, comment_id: str, like_count: Any, text: str, source: str, create_time: str = "") -> bool:
        comment_id = str(comment_id or "").strip()
        text = str(text or "").strip()
        like_val = count_to_int(like_count)

        # Tier 1: Primary dedup by ID
        if comment_id and comment_id in self.seen_ids:
            for c in self.comments:
                if c["id"] == comment_id and like_val > c["like_count"]:
                    c["like_count"] = like_val
                    c["text"] = text or c["text"]
            return False

        if not comment_id:
            comment_id = f"{source}|{text[:120]}|{like_val}"

        is_dom_comment = source == "dom" or comment_id.startswith("dom|")
        if is_dom_comment:
            fingerprint = self._dom_fingerprint(comment_id, text, like_val)
            if (fingerprint and fingerprint in self.seen_dom_fingerprints) or self._has_existing_non_dom_text(text):
                return False
            if fingerprint:
                self.seen_dom_fingerprints.add(fingerprint)

        self.seen_ids.add(comment_id)
        self.comments.append(
            {
                "id": comment_id,
                "like_count": like_val,
                "text": text or "[非文本]",
                "order": len(self.comments),
                "source": source,
                "create_time": str(create_time or "").strip(),
            }
        )
        return True

    @staticmethod
    def _is_reply_comment(comment: dict[str, Any]) -> bool:
        """Detect if a comment is a reply to another comment (not top-level)."""
        # Direct reply indicators in TikTok API — broad coverage of possible field names.
        # root_comment_id can be present on top-level comments, so handle it below.
        for key in ("reply_comment_id", "parent_comment_id", "reply_to_comment_cid",
                     "reply_to_comment_id", "reply_id",
                     "reply_to_user_id", "reply_to_username", "reply_to_user_name",
                     "reply_owner_id", "is_reply"):
            val = comment.get(key)
            if val is not None and str(val) not in ("", "0", "false", "False"):
                return True
        root_comment_id = str(comment.get("root_comment_id") or "").strip()
        own_comment_id = str(comment.get("cid") or comment.get("id") or "").strip()
        if root_comment_id and root_comment_id not in ("0", "false", "False") and root_comment_id != own_comment_id:
            return True
        # comment_type / comment_role / comment_source: 1 = top-level, 2+ = reply
        ctype = comment.get("comment_type") or comment.get("comment_role") or comment.get("comment_source")
        if ctype is not None and str(ctype) not in ("", "0", "1"):
            return True
        # Text-based fallback: replies often start with @mention or 回复
        text = str(comment.get("text") or comment.get("comment") or "")
        if text.strip():
            stripped = text.strip()
            if stripped.startswith("@") or stripped.startswith("回复") or stripped.startswith("Replying to"):
                return True
        return False

    def add_comments_from_payload(self, data: dict[str, Any], source: str) -> int:
        if len(self.comments) >= self.max_scan_comments or not isinstance(data, dict):
            return 0
        self.response_count += 1
        self.last_has_more = data.get("has_more")
        comments = data.get("comments") or []
        if not isinstance(comments, list):
            return 0

        added = 0
        skipped_non_top = 0
        for comment in comments:
            if len(self.comments) >= self.max_scan_comments:
                break
            if not isinstance(comment, dict):
                continue
            if self._is_reply_comment(comment):
                skipped_non_top += 1
                continue
            comment_text = normalize_comment_text(comment)
            comment_id = str(comment.get("cid") or comment.get("id") or "").strip()
            if not comment_id:
                comment_id = f"{comment.get('create_time', '')}|{comment_text[:120]}"
            create_time = _format_timestamp(comment.get("create_time") or comment.get("createTime"))
            if self.add_comment(comment_id, comment_like_count(comment), comment_text, source, create_time):
                added += 1
        if skipped_non_top:
            self.log_callback(f"  接口返回 {skipped_non_top} 条非主楼评论对象，已忽略。")
        return added

    def handle_response(self, response) -> None:
        if len(self.comments) >= self.max_scan_comments:
            return
        try:
            if not is_comment_list_response(response.url):
                return
            data = response.json()
            added = self.add_comments_from_payload(data, "api")
            if added:
                self.log_callback(f"  接口返回新增主楼评论 {added} 条，累计 {len(self.comments)} 条。")
        except Exception:
            import logging
            _logger = logging.getLogger(__name__)
            _logger.warning("handle_response failed for %s (will continue silently)", response.url)
            _logger.debug("handle_response failed", exc_info=True)
            return


def looks_blocked_or_captcha(page) -> bool:
    try:
        current_url = page.url.lower()
        if "captcha" in current_url or "verify" in current_url:
            return True
        if page.locator("div[id^='captcha'], iframe[src*='captcha']").count() > 0:
            return True
        return page.get_by_text(re.compile(r"verify|verification|验证码", re.I)).count() > 0
    except Exception:
        return False


def open_comment_panel(page) -> bool:
    selectors = [
        "[data-e2e='comment-icon']",
        "[data-e2e='comment-count']",
        "button[aria-label*='comment' i]",
        "button[aria-label*='评论']",
        "[aria-label*='评论']",
        "button:has-text('评论')",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                locator.click(timeout=2500)
                time.sleep(1.2)
                return True
        except Exception:
            continue
    try:
        clicked = page.evaluate(
            """() => {
                const visible = el => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const score = el => {
                    const text = `${el.getAttribute('aria-label') || ''} ${el.innerText || ''}`.toLowerCase();
                    if (!/(comment|评论)/i.test(text)) return -1;
                    const rect = el.getBoundingClientRect();
                    let value = 0;
                    if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') value += 10;
                    if (rect.left > window.innerWidth * 0.45) value += 5;
                    if (/\\d/.test(text)) value += 2;
                    return value;
                };
                const candidates = Array.from(document.querySelectorAll('button,[role="button"],a,div,span'))
                    .filter(visible)
                    .map(el => ({el, score: score(el)}))
                    .filter(item => item.score >= 0)
                    .sort((a, b) => b.score - a.score);
                if (!candidates.length) return false;
                candidates[0].el.click();
                return true;
            }"""
        )
        if clicked:
            time.sleep(1.2)
            return True
    except Exception:
        pass
    return False


def scroll_comments(page) -> None:
    try:
        page.evaluate(
            """() => {
                const isScrollable = el => {
                    if (!el) return false;
                    const style = getComputedStyle(el);
                    return el.scrollHeight > el.clientHeight + 80 &&
                        ['auto', 'scroll', 'overlay'].includes(style.overflowY);
                };
                const rightSide = el => {
                    const rect = el.getBoundingClientRect();
                    return rect.left > window.innerWidth * 0.45 && rect.width > 180 && rect.height > 180;
                };
                const commentNodes = Array.from(document.querySelectorAll(
                    '[data-e2e="comment-level-1"], [data-e2e="browse-comment-list"], [data-e2e="comment-list"], [class*="CommentList"], [class*="comment-list"]'
                ));
                const candidates = [];
                for (const commentNode of commentNodes) {
                    let current = commentNode;
                    while (current && current !== document.body && current !== document.documentElement) {
                        if (isScrollable(current) && rightSide(current)) {
                            candidates.push(current);
                            break;
                        }
                        current = current.parentElement;
                    }
                }
                const target = candidates
                    .sort((a, b) => (b.clientHeight * b.clientWidth) - (a.clientHeight * a.clientWidth))[0];
                if (!target) {
                    return false;
                }
                target.scrollTop = Math.min(target.scrollHeight, target.scrollTop + Math.max(500, Math.floor(target.clientHeight * 0.85)));
                target.dispatchEvent(new Event('scroll', {bubbles: true}));
                return true;
            }"""
        )
    except Exception:
        pass


def collect_visible_dom_comments(page, collector: CommentCollector, log_callback) -> int:
    try:
        items = page.evaluate(
            """() => {
                const normalize = value => (value || '').replace(/[\\r\\n\\u2028\\u2029]+/g, ' ').replace(/\\s+/g, ' ').trim();
                const unique = nodes => Array.from(new Set(nodes.filter(Boolean)));
                const rawNodes = unique([
                    ...document.querySelectorAll('[data-e2e="comment-level-1"]'),
                    ...document.querySelectorAll('[class*="CommentItemContainer"]'),
                    ...document.querySelectorAll('[class*="DivCommentItemContainer"]')
	                ]);
	                const replySelector = [
	                    '[data-e2e="comment-level-2"]',
	                    '[data-e2e*="comment-level-2"]',
	                    '[data-e2e*="reply"]',
	                    '[class*="ReplyComment"]',
	                    '[class*="reply-comment"]',
	                    '[class*="SubComment"]',
	                    '[class*="sub-comment"]',
	                    '[class*="ChildComment"]',
	                    '[class*="ReplyItem"]',
	                    '[class*="reply-item"]'
	                ].join(', ');
	                const containerSelector = '[class*="CommentItemContainer"], [class*="DivCommentItemContainer"]';
	                const mainTextSelector = [
	                    '[data-e2e="comment-level-1"]',
	                    '[data-e2e="comment-text"]',
	                    'p[data-e2e="comment-level-1"]',
	                    '[class*="CommentText"]',
	                    '[class*="comment-text"]'
	                ].join(', ');
	                const result = [];
	                for (const rawNode of rawNodes) {
	                    // Skip reply comments — check multiple indicators
	                    if (!rawNode) continue;
	                    if (rawNode.matches(replySelector) || rawNode.closest(replySelector)) {
	                        continue;
	                    }
                    // Skip nodes whose text starts with reply pattern (e.g. "回复 @user")
                    const quickText = normalize(rawNode.innerText || rawNode.textContent || '').substring(0, 30);
                    if (/^(回复\\s*@|Reply\\s*@|Replying to)/i.test(quickText)) {
                        continue;
                    }
	                    const node = rawNode.closest(containerSelector) || rawNode;
	                    const rawTextNodes = rawNode.matches('[data-e2e="comment-level-1"]')
	                        ? [rawNode]
	                        : unique(Array.from(node.querySelectorAll(mainTextSelector)).filter(el => {
	                            if (!el || el.closest(replySelector)) {
	                                return false;
	                            }
	                            const closestContainer = el.closest(containerSelector);
	                            if (closestContainer && closestContainer !== node) {
	                                return false;
	                            }
	                            const text = normalize(el.innerText || el.textContent || '');
	                            if (/^(回复|Reply|查看\\s*\\d+\\s*条回复|View\\s+\\d+\\s+repl)/i.test(text)) {
	                                return false;
	                            }
	                            return true;
	                        })).slice(0, 1);
	                    const textParts = [];
	                    for (const textNode of rawTextNodes) {
	                        if (!textNode) {
	                            continue;
	                        }
	                        if (textNode !== rawNode && textNode.closest(replySelector)) {
	                            continue;
	                        }
	                        const nestedTopComment = rawNode.matches('[data-e2e="comment-level-1"]')
	                            ? null
	                            : textNode.closest('[data-e2e="comment-level-1"]');
	                        if (nestedTopComment && nestedTopComment !== rawNode) {
	                            continue;
	                        }
	                        const text = normalize(textNode.innerText || textNode.textContent || '');
	                        if (!text) {
	                            continue;
                        }
                        if (/^(回复|Reply|查看\\s*\\d+\\s*条回复|View\\s+\\d+\\s+repl)/i.test(text)) {
                            continue;
                        }
                        if (/^\\d{1,2}-\\d{1,2}$/.test(text)) {
                            continue;
                        }
                        if (textParts.includes(text)) {
                            continue;
                        }
                        textParts.push(text);
                    }
                    let text = textParts.join(' ').trim();
                    let type = '';
                    if (!text) {
                        if (node.querySelector('img[src*="sticker"], [class*="Sticker"]')) {
                            type = '贴纸';
                        } else if (node.querySelector('img[src*="gif"], [class*="Gif"], [aria-label*="GIF" i]')) {
                            type = 'GIF';
                        } else if (node.querySelector('img, picture')) {
                            type = '图片';
                        } else if (node.querySelector('video')) {
                            type = '视频';
                        } else {
                            type = '非文本';
                        }
                        text = `[${type}]`;
                    }
                    let likeText = '';
                    // Broad selector search — TikTok changes class names frequently
                    const likeNodes = unique([
                        ...node.querySelectorAll('[data-e2e*="like"]'),
                        ...node.querySelectorAll('button[aria-label*="like" i]'),
                        ...node.querySelectorAll('button[aria-label*="赞" i]'),
                        ...node.querySelectorAll('[aria-label*="like" i]'),
                        ...node.querySelectorAll('[aria-label*="赞" i]'),
                        ...node.querySelectorAll('span[data-e2e*="like"]'),
                        ...node.querySelectorAll('[class*="Like"] strong, [class*="Like"] span'),
                        ...node.querySelectorAll('[class*="like"] strong, [class*="like"] span'),
	                    ]);
	                    for (const likeNode of likeNodes) {
	                        if (likeNode.closest(replySelector)) {
	                            continue;
	                        }
	                        const candidate = normalize(likeNode.innerText || likeNode.textContent || likeNode.getAttribute('aria-label') || '');
	                        if (candidate && /\\d/.test(candidate) && candidate.length < 30) {
	                            likeText = candidate;
                            break;
                        }
                    }
	                    if (!likeText) {
	                        // Scan all leaf elements for number candidates near like/heart icons
	                        const allLeaves = Array.from(node.querySelectorAll('span, strong, p, button, time, small, b, i, em'))
	                            .filter(el => !el.closest(replySelector))
	                            .filter(el => !el.children.length || el.querySelector('svg, img, [data-e2e*="like"]'));
                        const numberCandidates = [];
                        for (const leaf of allLeaves) {
                            const txt = normalize(leaf.innerText || leaf.textContent || '');
                            if (/^\\d+(?:[,.]\\d+)?\\s*(?:K|M|B|万|萬|亿|億)?$/i.test(txt)) {
                                const rect = leaf.getBoundingClientRect();
                                numberCandidates.push({text: txt, x: rect.left + rect.width / 2});
                            }
                        }
                        if (numberCandidates.length > 0) {
                            // Pick the rightmost number (likes are on the right side of comment rows)
                            numberCandidates.sort((a, b) => b.x - a.x);
                            likeText = numberCandidates[0].text;
                        } else {
                            likeText = '0';
                        }
                    }
	                    const authorLink = node.querySelector('a[href^="/@"], a[href*="tiktok.com/@"]');
	                    const authorKey = normalize(authorLink?.getAttribute('href') || authorLink?.textContent || '')
	                        .replace(/\\|/g, ' ')
	                        .slice(0, 80) || `row-${result.length}`;
	                    let timeText = '';
                    const timeEl = node.querySelector('time, [datetime], [data-e2e*="time"], span:last-child');
                    if (timeEl) {
                        timeText = normalize(timeEl.getAttribute('datetime') || timeEl.getAttribute('title') || timeEl.innerText || timeEl.textContent || '');
                    }
                    if (!timeText) {
                        const timeMatch = (node.innerText || node.textContent || '').match(/(\\d{1,2}-\\d{1,2}|\\d+\\s*(?:天|d|h|m|s|小时|分钟|秒|ago|前))/i);
                        timeText = timeMatch ? timeMatch[1] : '';
                    }
                    const cid = node.getAttribute('data-id') ||
	                        node.getAttribute('id') ||
	                        node.querySelector('a[href*="/comment/"]')?.getAttribute('href') ||
	                        `dom|${authorKey}|${text.slice(0, 120).replace(/\\|/g, ' ')}|${likeText}`;
                    result.push({id: cid, text, like_count: likeText, create_time: timeText});
                }
                return result;
            }"""
        )
    except Exception:
        return 0

    added = 0
    for item in items if isinstance(items, list) else []:
        if len(collector.comments) >= collector.max_scan_comments:
            break
        if not isinstance(item, dict):
            continue
        if collector.add_comment(item.get("id", ""), item.get("like_count", 0), item.get("text", ""), "dom", item.get("create_time", "")):
            added += 1
    if added:
        log_callback(f"  页面可见评论新增主楼评论 {added} 条，累计 {len(collector.comments)} 条。")
    return added


def build_comment_api_url(video_id: str, cursor: Any, count: int) -> str:
    params = {
        "aweme_id": video_id,
        "item_id": video_id,
        "count": str(count),
        "cursor": str(cursor or 0),
        "from_page": "video",
        "aid": "1988",
        "app_name": "tiktok_web",
        "device_platform": "web_pc",
        "channel": "tiktok_web",
        "browser_platform": "Win32",
        "browser_language": "zh-CN",
        "is_page_visible": "true",
        "focus_state": "true",
        "root_referer": "https://www.tiktok.com/",
    }
    return "https://www.tiktok.com/api/comment/list/?" + urlencode(params)


def build_comment_api_candidates(video_id: str, cursor: Any, count: int) -> list[str]:
    compact_params = {
        "aweme_id": video_id,
        "count": str(min(20, count)),
        "cursor": str(cursor or 0),
        "from_page": "video",
    }
    browser_params = {
        "aweme_id": video_id,
        "count": str(min(20, count)),
        "cursor": str(cursor or 0),
        "aid": "1988",
        "app_language": "zh-Hans",
        "app_name": "tiktok_web",
        "browser_language": "zh-CN",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": "5.0 (Windows)",
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "video",
        "history_len": "2",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "os": "windows",
        "priority_region": "",
        "referer": "",
        "region": "SG",
        "screen_height": "1080",
        "screen_width": "1920",
        "tz_name": "Asia/Shanghai",
        "webcast_language": "zh-Hans",
    }
    urls = [
        build_comment_api_url(video_id, cursor, count),
        "https://www.tiktok.com/api/comment/list/?" + urlencode(compact_params),
        "https://www.tiktok.com/api/comment/list/?" + urlencode(browser_params),
        "/api/comment/list/?" + urlencode(compact_params),
        "/api/comment/list/?" + urlencode(browser_params),
    ]
    return list(dict.fromkeys(urls))


def wait_for_tiktok_runtime(page) -> None:
    try:
        page.wait_for_function(
            "() => window.byted_acrawler && (typeof window.byted_acrawler.frontierSign === 'function' || typeof window.byted_acrawler.sign === 'function')",
            timeout=5000,
        )
    except Exception:
        pass


def fetch_comments_via_page_api(page, video_id: str, collector: CommentCollector, log_callback, stop_event=None, pause_event=None, max_scroll_rounds: int | None = None) -> int:
    if not video_id:
        return 0

    wait_for_tiktok_runtime(page)
    cursor: Any = 0
    total_added = 0
    _max_rounds = max_scroll_rounds if max_scroll_rounds is not None else MAX_SCROLL_ROUNDS
    for _ in range(_max_rounds):
        if should_stop(stop_event) or len(collector.comments) >= collector.max_scan_comments:
            break
        if wait_if_paused(pause_event, stop_event):
            break

        count = min(50, collector.max_scan_comments - len(collector.comments))
        data = None
        last_error = ""
        for url in build_comment_api_candidates(video_id, cursor, count):
            try:
                result = page.evaluate(
                    """async (url) => {
                        const absoluteUrl = url.startsWith('/') ? `${location.origin}${url}` : url;
                        const getCookie = name => {
                            const item = document.cookie.split('; ').find(row => row.startsWith(`${name}=`));
                            return item ? decodeURIComponent(item.split('=').slice(1).join('=')) : '';
                        };
                        const urlObj = new URL(absoluteUrl);
                        const msToken = getCookie('msToken');
                        if (msToken && !urlObj.searchParams.has('msToken')) {
                            urlObj.searchParams.set('msToken', msToken);
                        }
                        try {
                            const node = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
                            const data = node ? JSON.parse(node.textContent || '{}') : {};
                            const ctx = data?.__DEFAULT_SCOPE__?.['webapp.app-context'] || {};
                            if (ctx.wid && !urlObj.searchParams.has('web_id')) urlObj.searchParams.set('web_id', ctx.wid);
                            if (ctx.appId && !urlObj.searchParams.has('aid')) urlObj.searchParams.set('aid', String(ctx.appId));
                            if (ctx.region && !urlObj.searchParams.has('region')) urlObj.searchParams.set('region', ctx.region);
                            if (ctx.language && !urlObj.searchParams.has('app_language')) urlObj.searchParams.set('app_language', ctx.language);
                            if (ctx.language && !urlObj.searchParams.has('webcast_language')) urlObj.searchParams.set('webcast_language', ctx.language);
                        } catch (_) {}
                        const urlToSign = urlObj.toString();
                        let requestUrl = urlToSign;
                        try {
                            if (window.byted_acrawler && typeof window.byted_acrawler.frontierSign === 'function') {
                                const signResult = await window.byted_acrawler.frontierSign(urlToSign);
                                if (signResult && signResult['X-Bogus']) {
                                    const joiner = urlToSign.includes('?') ? '&' : '?';
                                    requestUrl = `${urlToSign}${joiner}X-Bogus=${encodeURIComponent(signResult['X-Bogus'])}`;
                                    if (signResult['X-Gnarly']) {
                                        requestUrl += `&X-Gnarly=${encodeURIComponent(signResult['X-Gnarly'])}`;
                                    }
                                }
                            } else if (window.byted_acrawler && typeof window.byted_acrawler.sign === 'function') {
                                const signed = await window.byted_acrawler.sign({url: urlToSign});
                                if (signed) requestUrl = signed;
                            }
                        } catch (_) {}
                        const response = await fetch(requestUrl, {
                            credentials: 'include',
                            headers: {
                                accept: 'application/json, text/plain, */*',
                                'x-secsdk-csrf-request': '1',
                                'x-secsdk-csrf-version': '1.2.22'
                            },
                            referrer: location.href,
                            referrerPolicy: 'strict-origin-when-cross-origin'
                        });
                        return {
                            ok: response.ok,
                            status: response.status,
                            text: await response.text()
                        };
                    }""",
                    url,
                )
            except Exception as exc:
                last_error = f"request error: {exc}"
                continue

            if not isinstance(result, dict):
                last_error = "empty result"
                continue
            if not result.get("ok"):
                last_error = f"HTTP {result.get('status', 'unknown')}"
                continue

            try:
                parsed = json.loads(result.get("text") or "{}")
            except Exception:
                last_error = "not JSON"
                continue

            if not isinstance(parsed, dict):
                last_error = "JSON is not object"
                continue

            has_comments_key = isinstance(parsed.get("comments"), list)
            comments = parsed.get("comments") or []
            status_code = parsed.get("status_code")
            status_msg = parsed.get("status_msg") or parsed.get("statusMsg") or ""
            if comments or (has_comments_key and status_code in (0, "0")):
                data = parsed
                break
            last_error = f"status_code={status_code}, status_msg={status_msg}"

        if data is None:
            log_callback(f"  主动评论接口失败：{last_error or '所有候选接口均失败'}")
            break

        added = collector.add_comments_from_payload(data, "api-fetch")
        total_added += added
        if added:
            log_callback(f"  主动评论接口新增主楼评论 {added} 条，累计 {len(collector.comments)} 条。")
        else:
            status_code = data.get("status_code")
            status_msg = data.get("status_msg") or data.get("statusMsg") or ""
            log_callback(f"  主动评论接口未获得主楼评论：status_code={status_code} status_msg={status_msg}")

        next_cursor = data.get("cursor") or data.get("nextCursor") or data.get("next_cursor")
        if not has_more_comments(data.get("has_more")) or not next_cursor or str(next_cursor) == str(cursor):
            break
        cursor = next_cursor
        interruptible_sleep(0.4, stop_event)

    return total_added


def collect_video_comments(page, video_url: str, max_scan_comments: int, log_callback, stop_event=None, pause_event=None, comment_top_limit: int | None = None, page_load_timeout: int | None = None, scroll_pause: float | None = None, max_scroll_rounds: int | None = None) -> list[dict[str, Any]]:
    collector = CommentCollector(max_scan_comments, log_callback, comment_top_limit=comment_top_limit)
    video_id = extract_video_id(video_url)
    _page_timeout = page_load_timeout if page_load_timeout is not None else PAGE_LOAD_TIMEOUT
    _scroll_pause = scroll_pause if scroll_pause is not None else SCROLL_PAUSE
    _max_scroll_rounds = max_scroll_rounds if max_scroll_rounds is not None else MAX_SCROLL_ROUNDS
    page.on("response", collector.handle_response)
    try:
        page.goto(video_url, wait_until="domcontentloaded", timeout=_page_timeout)
        time.sleep(2.5)
        if looks_blocked_or_captcha(page):
            log_callback("  跳过：疑似验证码或风控页面。")
            return []

        api_added = fetch_comments_via_page_api(page, video_id, collector, log_callback, stop_event=stop_event, pause_event=pause_event, max_scroll_rounds=_max_scroll_rounds)
        opened = False
        if len(collector.comments) == 0:
            opened = open_comment_panel(page)
        if not opened and api_added == 0 and len(collector.comments) < collector.max_scan_comments:
            log_callback("  评论入口未能打开，改用页面上下文评论接口。")
            fetch_comments_via_page_api(page, video_id, collector, log_callback, stop_event=stop_event, pause_event=pause_event, max_scroll_rounds=_max_scroll_rounds)
        if opened:
            log_callback("  已点击评论入口。")

        try:
            if opened:
                page.wait_for_selector("[data-e2e='comment-level-1'], [data-e2e='browse-comment-list']", timeout=COMMENT_WAIT_TIMEOUT)
        except PlaywrightTimeoutError:
            log_callback("  未等到评论 DOM，继续通过接口响应和滚动尝试。")

        use_dom_fallback = len(collector.comments) == 0
        if use_dom_fallback:
            collect_visible_dom_comments(page, collector, log_callback)
        if opened and api_added == 0 and len(collector.comments) < collector.max_scan_comments:
            fetch_comments_via_page_api(page, video_id, collector, log_callback, stop_event=stop_event, pause_event=pause_event, max_scroll_rounds=_max_scroll_rounds)
            use_dom_fallback = len(collector.comments) == 0

        no_new_rounds = 0
        last_count = len(collector.comments)
        for round_index in range(_max_scroll_rounds):
            if should_stop(stop_event):
                log_callback("  任务已停止。")
                break
            if wait_if_paused(pause_event, stop_event):
                break
            if len(collector.comments) >= collector.max_scan_comments:
                break
            if collector.last_has_more is not None and not has_more_comments(collector.last_has_more) and collector.response_count > 0:
                break
            if not use_dom_fallback:
                break

            scroll_comments(page)
            interruptible_sleep(_scroll_pause, stop_event)
            collect_visible_dom_comments(page, collector, log_callback)

            current_count = len(collector.comments)
            if current_count == last_count:
                no_new_rounds += 1
                if no_new_rounds >= NO_NEW_SCROLL_LIMIT:
                    log_callback(f"  连续 {NO_NEW_SCROLL_LIMIT} 次滚动没有新增主楼评论，停止当前视频。")
                    break
            else:
                no_new_rounds = 0
                last_count = current_count

            if round_index and round_index % 10 == 0:
                log_callback(f"  已滚动 {round_index} 轮，累计主楼评论 {len(collector.comments)} 条。")

        return collector.comments
    finally:
        try:
            page.remove_listener("response", collector.handle_response)
        except Exception:
            pass


def build_top_rows(video_index: str, video_url: str, comments: list[dict[str, Any]], comment_top_limit: int | None = None) -> list[dict[str, str]]:
    top_limit = comment_top_limit if comment_top_limit is not None else TOP_COMMENT_LIMIT
    top_comments = sorted(comments, key=lambda item: (-int(item.get("like_count", 0) or 0), int(item.get("order", 0) or 0)))
    return [
        {
            "编号": video_index,
            "视频链接": video_url,
            "评论的点赞量": str(comment.get("like_count", 0)),
            "评论内容": str(comment.get("text") or ""),
            "发布时间": str(comment.get("create_time") or ""),
        }
        for comment in top_comments[:top_limit]
    ]


def empty_video_row(video_index: str, video_url: str) -> dict[str, str]:
    return {"编号": video_index, "视频链接": video_url, "评论的点赞量": "", "评论内容": "该视频无评论", "发布时间": ""}


def run_tiktok_top_comments_spider(
    txt_path: str,
    cdp_port_or_url: str,
    max_scan_comments: int,
    log_callback,
    finish_callback,
    stop_event=None,
    pause_event=None,
    config=None,
):
    if config is None:
        config = {}
    comment_top_limit = int(config.get("comment_top_limit", TOP_COMMENT_LIMIT))
    config_page_load_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    config_scroll_pause = float(config.get("scroll_interval", SCROLL_PAUSE))
    config_max_scroll_rounds = int(config.get("max_scroll_rounds", MAX_SCROLL_ROUNDS))

    completed_path = None
    page = None
    try:
        if sync_playwright is None:
            log_callback("缺少依赖：playwright。请先在当前运行环境执行 pip install -r requirements.txt，并执行 python -m playwright install chromium。")
            return

        entries = parse_video_entries(txt_path)
        if not entries:
            log_callback("TXT 中没有找到有效的 TikTok 视频链接。")
            return

        max_scan_comments = max(comment_top_limit, int(max_scan_comments or DEFAULT_SCAN_LIMIT))
        output_path = build_output_path("tiktok", f"tiktok_top_comments_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        writer = XlsxRowWriter(output_path, CSV_FIELDS)
        log_callback(f"输出文件：{output_path}")
        log_callback(f"最多扫描主楼评论数：{max_scan_comments}，每个视频输出点赞量前 {comment_top_limit} 条。")

        with sync_playwright() as playwright:
            log_callback("正在连接本地 Chrome...")
            try:
                _, context = connect_existing_chromium(playwright, cdp_port_or_url, log_callback=log_callback)
            except Exception as exc:
                log_callback(f"连接失败：请确认 Chrome 已自动打开并已登录 TikTok。错误：{exc}")
                return

            page = context.new_page()
            for progress_index, entry in enumerate(entries, 1):
                if should_stop(stop_event):
                    log_callback("任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break

                video_index = entry["编号"]
                video_url = entry["视频链接"]
                log_callback(f"[{progress_index}/{len(entries)}] 读取评论：{video_url}")
                try:
                    comments = collect_video_comments(page, video_url, max_scan_comments, log_callback, stop_event, pause_event=pause_event, comment_top_limit=comment_top_limit, page_load_timeout=config_page_load_timeout, scroll_pause=config_scroll_pause, max_scroll_rounds=config_max_scroll_rounds)
                    rows = build_top_rows(video_index, video_url, comments, comment_top_limit=comment_top_limit)
                    if not rows:
                        rows = [empty_video_row(video_index, video_url)]
                    writer.writerows(sanitize_csv_rows(rows))
                    writer.save()
                    written_count = len([row for row in rows if row.get("评论内容") and row.get("评论内容") != "该视频无评论"])
                    log_callback(f"  完成：扫描主楼评论 {len(comments)} 条，写入 {written_count} 条并已保存。")
                except PlaywrightTimeoutError:
                    writer.writerow(sanitize_csv_row(empty_video_row(video_index, video_url)))
                    writer.save()
                    log_callback("  跳过：页面加载超时，已写入空评论占位行并保存。")
                except Exception as exc:
                    writer.writerow(sanitize_csv_row(empty_video_row(video_index, video_url)))
                    writer.save()
                    log_callback(f"  跳过：{exc}，已写入空评论占位行并保存。")

                if (
                    progress_index < len(entries)
                    and progress_index % VIDEO_BATCH_COOLDOWN_EVERY == 0
                    and random_cooldown(
                        log_callback=log_callback,
                        stop_event=stop_event,
                        min_seconds=VIDEO_BATCH_COOLDOWN_MIN,
                        max_seconds=VIDEO_BATCH_COOLDOWN_MAX,
                        reason=f"已连续处理 {VIDEO_BATCH_COOLDOWN_EVERY} 个视频，降低 TikTok 访问频率",
                    )
                ):
                    log_callback("任务已停止。")
                    break

            if page and not page.is_closed():
                page.close()

        completed_path = output_path
        writer.save()
        log_callback(f"完成，已保存：{output_path}")
    finally:
        try:
            if page and not page.is_closed():
                page.close()
        except Exception:
            pass
        finish_callback(completed_path)
