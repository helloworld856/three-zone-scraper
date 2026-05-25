from __future__ import annotations

import re
import time

from playwright.sync_api import sync_playwright

from src.core import (
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    interruptible_sleep,
    random_cooldown,
    sanitize_xlsx_cell,
    should_stop,
    wait_if_paused,
    XlsxRowWriter,
)

OUTPUT_FIELDS = ["推文链接", "作者主页链接", "作者的名称", "账号ID", "粉丝数"]
OUTPUT_FIELDS_PROFILE_MODE = ["作者主页链接", "作者的名称", "账号ID", "粉丝数"]
PAGE_LOAD_TIMEOUT = 45000
STATUS_RE = re.compile(r"/status/(\d+)")
TWEET_READY_TIMEOUT = 12000

def normalize_x_url(url: str) -> str:
    if not url:
        return ""
    normalized = url.strip().replace("twitter.com", "x.com")
    normalized = normalized.split("?")[0].split("#")[0]
    if normalized.startswith("//"):
        normalized = "https:" + normalized
    if normalized.startswith("/"):
        normalized = "https://x.com" + normalized
    if normalized and not normalized.startswith("http"):
        normalized = "https://" + normalized
    return normalized

def parse_tweet_links(txt_path: str) -> list[str]:
    links: list[str] = []
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            url = normalize_x_url(stripped.split()[0])
            if "/status/" in url:
                links.append(url)
    return links

def parse_profile_links(txt_path: str) -> list[str]:
    links: list[str] = []
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            url = normalize_x_url(stripped.split()[0])
            if url and "/status/" not in url:
                links.append(url)
    return links

def extract_status_id(url: str) -> str:
    match = STATUS_RE.search(url or "")
    return match.group(1) if match else ""

def parse_metric_number(text: str) -> float:
    if not text:
        return 0
    expanded = expand_compact_number(text)
    try:
        return float(expanded)
    except ValueError:
        return 0

def safe_text(locator, default: str = "") -> str:
    try:
        if locator.count() <= 0:
            return default
        return locator.first.inner_text(timeout=2000).strip() or default
    except Exception:
        return default

def safe_attr(locator, attr: str, default: str = "") -> str:
    try:
        if locator.count() <= 0:
            return default
        return locator.first.get_attribute(attr, timeout=2000) or default
    except Exception:
        return default

def find_target_article(page, target_status_id: str):
    try:
        page.wait_for_selector('article[data-testid="tweet"]', timeout=20000)
    except Exception:
        return None

    articles = page.locator('article[data-testid="tweet"]').all()
    for article in articles:
        try:
            hrefs = [
                a.get_attribute("href") or ""
                for a in article.locator('a[href*="/status/"]').all()
            ]
            if any(target_status_id in href for href in hrefs):
                return article
        except Exception:
            continue
    return articles[0] if articles else None

def load_tweet_page(page, tweet_url: str, target_status_id: str, log_callback, page_timeout=None, tweet_ready_timeout=None) -> bool:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if tweet_ready_timeout is None:
        tweet_ready_timeout = TWEET_READY_TIMEOUT
    try:
        page.goto(tweet_url, wait_until="domcontentloaded", timeout=page_timeout)
        page.wait_for_selector('article[data-testid="tweet"]', timeout=tweet_ready_timeout)
        return True
    except Exception as e:
        current_url = getattr(page, "url", "")
        title = ""
        try:
            title = page.title()
        except Exception:
            pass
        log_callback(
            f"  推文正文未在 {tweet_ready_timeout // 1000} 秒内渲染，快速跳过。当前 URL: {current_url or '未知'}，标题: {title or '未知'}，错误: {e}"
        )
    return False

def extract_author_from_article(article) -> dict:
    user_block = article.locator('div[data-testid="User-Name"]').first
    author_name = ""
    account_id = ""
    profile_url = ""

    try:
        spans = user_block.locator("span").all()
        for span in spans:
            text = span.inner_text(timeout=1000).strip()
            if not text:
                continue
            if text.startswith("@") and not account_id:
                account_id = text.lstrip("@")
            elif not author_name:
                author_name = text
    except Exception:
        pass

    try:
        links = user_block.locator('a[role="link"]').all()
        for link in links:
            href = link.get_attribute("href") or ""
            normalized = normalize_x_url(href)
            if not normalized or "/status/" in normalized:
                continue
            handle_match = re.search(r"x\.com/([^/?#]+)$", normalized)
            if handle_match:
                account_id = handle_match.group(1)
                profile_url = f"https://x.com/{account_id}"
                break
    except Exception:
        pass

    if account_id and not profile_url:
        profile_url = f"https://x.com/{account_id}"

    return {
        "author_name": author_name,
        "account_id": account_id,
        "profile_url": profile_url,
    }

def extract_view_count(article) -> tuple[str, float]:
    selectors = [
        'a[href*="/analytics"]',
        'div[data-testid="postViewCount"]',
        'span[aria-label*="Views"]',
        'span[aria-label*="浏览"]',
        'span[aria-label*="表示"]',
    ]
    for selector in selectors:
        try:
            locator = article.locator(selector)
            if locator.count() <= 0:
                continue
            text = locator.first.inner_text(timeout=1500).strip()
            aria = locator.first.get_attribute("aria-label", timeout=1500) or ""
            raw = text or aria
            if raw:
                return raw, parse_metric_number(raw)
        except Exception:
            continue
    return "", 0

def extract_followers_count(page, profile_url: str, page_timeout=None, stop_event=None) -> str:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    try:
        page.goto(profile_url, wait_until="domcontentloaded", timeout=page_timeout)
        interruptible_sleep(3, stop_event)
    except Exception:
        return ""

    selectors = [
        'a[href$="/followers"]',
        'a[href*="/followers"]',
        'a[href*="/verified_followers"]',
    ]
    for selector in selectors:
        try:
            for node in page.locator(selector).all():
                text = node.inner_text(timeout=1500).strip()
                aria = node.get_attribute("aria-label", timeout=1500) or ""
                raw = text or aria
                if raw and re.search(r"follower|粉丝|フォロワー|followers", raw, re.IGNORECASE):
                    match = re.search(r"([\d,.]+(?:\.\d+)?\s*(?:[KkMmBb]|千|万|萬|亿|億)?)", raw)
                    if match:
                        return expand_compact_number(match.group(1).strip())
        except Exception:
            continue
    return ""

def extract_tweet_author_record(tweet_page, profile_page, tweet_url: str, log_callback, page_timeout=None, tweet_ready_timeout=None, stop_event=None) -> dict | None:
    target_status_id = extract_status_id(tweet_url)
    if not target_status_id:
        log_callback(f"跳过：无法解析推文 ID：{tweet_url}")
        return None

    if not load_tweet_page(tweet_page, tweet_url, target_status_id, log_callback, page_timeout=page_timeout, tweet_ready_timeout=tweet_ready_timeout):
        log_callback(f"跳过：推文页面一直卡在 X 启动页或未渲染正文：{tweet_url}")
        return None

    article = find_target_article(tweet_page, target_status_id)
    if article is None:
        log_callback(f"跳过：未找到推文正文：{tweet_url}")
        return None

    author = extract_author_from_article(article)
    if not author["account_id"] or not author["profile_url"]:
        log_callback(f"跳过：无法提取作者信息：{tweet_url}")
        return None

    view_text, view_value = extract_view_count(article)
    followers = extract_followers_count(profile_page, author["profile_url"], page_timeout=page_timeout, stop_event=stop_event)

    return {
        "推文链接": normalize_x_url(tweet_url),
        "作者主页链接": author["profile_url"],
        "作者的名称": author["author_name"],
        "账号ID": author["account_id"],
        "粉丝数": followers,
        "_view_text": view_text,
        "_view_value": view_value,
    }

def extract_profile_record(profile_page, profile_url: str, log_callback, page_timeout=None, stop_event=None) -> dict | None:
    """Extract profile info directly from profile URL."""
    profile_url = normalize_x_url(profile_url)
    try:
        profile_page.goto(profile_url, wait_until="domcontentloaded", timeout=page_timeout if page_timeout is not None else PAGE_LOAD_TIMEOUT)
        interruptible_sleep(3, stop_event)
    except Exception as e:
        log_callback(f"跳过：无法加载主页：{profile_url}，错误：{e}")
        return None

    # Extract account ID from URL
    account_match = re.search(r"x\.com/([^/?#]+)/?$", profile_url)
    if not account_match:
        log_callback(f"跳过：无法解析账号 ID：{profile_url}")
        return None
    account_id = account_match.group(1)

    # Extract author name from profile header
    author_name = ""
    try:
        name_selector = 'div[data-testid="profile_header_0"] div[dir="auto"] span'
        name_locator = profile_page.locator(name_selector)
        if name_locator.count() > 0:
            author_name = name_locator.first.inner_text(timeout=2000).strip() or ""
    except Exception:
        pass

    # Extract followers count
    followers = extract_followers_count(profile_page, profile_url, page_timeout=page_timeout, stop_event=stop_event)

    return {
        "作者主页链接": profile_url,
        "作者的名称": author_name,
        "账号ID": account_id,
        "粉丝数": followers,
    }

def output_row(record: dict, fields: list[str]) -> dict:
    return {field: record.get(field, "") for field in fields}


def update_writer_row(writer: XlsxRowWriter, row_number: int, record: dict, fields: list[str]) -> None:
    row = output_row(record, fields)
    for column_number, field in enumerate(fields, start=1):
        writer.worksheet.cell(row=row_number, column=column_number).value = sanitize_xlsx_cell(row.get(field, ""))
    writer.save()

def run_scraper(txt_path: str, input_mode: str, cdp_port_or_url: str, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    if config is None:
        config = {}
    page_load_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    tweet_ready_timeout = int(config.get("tweet_ready_timeout", TWEET_READY_TIMEOUT))

    output_path = None
    try:
        is_profile_mode = input_mode == "博主链接"
        
        if is_profile_mode:
            links = parse_profile_links(txt_path)
            output_fields = OUTPUT_FIELDS_PROFILE_MODE
            if not links:
                log_callback("TXT 中没有有效的博主链接。")
                return
        else:
            links = parse_tweet_links(txt_path)
            output_fields = OUTPUT_FIELDS
            if not links:
                log_callback("TXT 中没有有效的推文链接。")
                return

        with sync_playwright() as p:
            log_callback("正在连接本地 Chrome...")
            try:
                _, context = connect_existing_chromium(p, cdp_port_or_url)
            except Exception as e:
                log_callback(f"连接失败：请确认 Chrome 已自动打开并已登录 X/Twitter。错误：{e}")
                return

            tweet_page = context.new_page() if not is_profile_mode else None
            profile_page = context.new_page()
            output_path = build_output_path("x", f"x_profiles_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
            writer = XlsxRowWriter(output_path, output_fields)
            best_by_author: dict[str, dict] = {}
            row_by_author: dict[str, int] = {}
            written_count = 0

            for index, link in enumerate(links, 1):
                if should_stop(stop_event):
                    log_callback("任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                
                if is_profile_mode:
                    log_callback(f"[{index}/{len(links)}] 处理博主链接：{link}")
                    record = extract_profile_record(profile_page, link, log_callback, page_timeout=page_load_timeout, stop_event=stop_event)
                else:
                    log_callback(f"[{index}/{len(links)}] 处理推文：{link}")
                    record = extract_tweet_author_record(tweet_page, profile_page, link, log_callback, page_timeout=page_load_timeout, tweet_ready_timeout=tweet_ready_timeout, stop_event=stop_event)
                
                if not record:
                    continue

                account_key = record["账号ID"].lower()
                old_record = best_by_author.get(account_key)
                if old_record is None:
                    writer.writerow(output_row(record, output_fields))
                    best_by_author[account_key] = record
                    row_by_author[account_key] = writer.worksheet.max_row
                    written_count += 1
                    if is_profile_mode:
                        log_callback(f"  写入作者 {account_key or '未知'}。")
                    else:
                        log_callback(f"  写入作者 {account_key or '未知'}，当前推文浏览量 {record.get('_view_text') or '未知'}。")
                elif not is_profile_mode and record["_view_value"] > old_record.get("_view_value", 0):
                    best_by_author[account_key] = record
                    update_writer_row(writer, row_by_author[account_key], record, output_fields)
                    log_callback(
                        f"  更新作者 {record['账号ID']}：更高浏览量 {record.get('_view_text') or '未知'}。"
                    )
                else:
                    if is_profile_mode:
                        log_callback(f"  跳过：作者 {record['账号ID']} 已处理过。")
                    else:
                        log_callback(f"  跳过：作者 {record['账号ID']} 已有更高浏览量推文。")
                
                if index % 10 == 0:
                    if random_cooldown(log_callback, stop_event, 3.0, 8.0):
                        break

            for opened_page in (tweet_page, profile_page):
                if opened_page is not None and not opened_page.is_closed():
                    opened_page.close()

        if not output_path:
            log_callback("没有提取到可输出的数据。")
            return
        writer.save()
        log_callback(f"完成，已保存：{output_path}")
    finally:
        finish_callback(output_path)
