from __future__ import annotations

import random
import re
import time

from playwright.sync_api import sync_playwright

from src.core import (
    XlsxRowWriter,
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    random_cooldown,
    sanitize_csv_row,
    should_stop,
    wait_if_paused,
)

CSV_FIELDS = ["博主主页链接", "博主名称", "博主ID", "粉丝量", "作者简介"]

def clean_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/"):
        url = "https://www.tiktok.com" + url
    if not url.startswith("http"):
        url = "https://" + url
    return url.split("?")[0].split("#")[0].rstrip("/")

def normalize_profile_url(url: str) -> str:
    cleaned = clean_url(url)
    match = re.search(r"tiktok\.com/(@[^/?#]+)", cleaned)
    return f"https://www.tiktok.com/{match.group(1)}" if match else ""

def profile_id_from_url(profile_url: str) -> str:
    match = re.search(r"tiktok\.com/@([^/?#]+)", profile_url)
    return f"@{match.group(1)}" if match else ""

def parse_profile_urls(txt_path: str) -> list[str]:
    urls: list[str] = []
    seen = set()
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for part in re.split(r"\s+", stripped):
                profile_url = normalize_profile_url(part)
                if profile_url and profile_url not in seen:
                    urls.append(profile_url)
                    seen.add(profile_url)
                    break
    return urls

def get_first_text(page, selectors: list[str], timeout: int = 2500) -> str:
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() <= 0:
                continue
            text = loc.inner_text(timeout=timeout).strip()
            if text:
                return text
        except Exception:
            continue
    return ""

def extract_profile_row(page, profile_url: str, page_load_timeout: int = 35000, captcha_wait: int = 12) -> dict[str, str]:
    page.goto(profile_url, wait_until="domcontentloaded", timeout=page_load_timeout)
    time.sleep(random.uniform(1.4, 2.2))

    try:
        if "captcha" in page.url or page.locator("div[id^='captcha']").count() > 0:
            time.sleep(captcha_wait)
    except Exception:
        pass

    missing_text = page.locator("text=/Couldn't find this account|无法找到此账号|账号不存在/i")
    if missing_text.count() > 0:
        return {
            "博主主页链接": profile_url,
            "博主名称": "账号不可用",
            "博主ID": profile_id_from_url(profile_url),
            "粉丝量": "",
            "作者简介": "账号不存在、已注销或当前不可见",
        }

    user_title = get_first_text(page, ["[data-e2e='user-title']", "h1"])
    user_subtitle = get_first_text(page, ["[data-e2e='user-subtitle']", "h2"])
    followers = expand_compact_number(get_first_text(page, ["[data-e2e='followers-count']"]))
    bio = get_first_text(page, ["[data-e2e='user-bio']"])

    author_id = user_title or profile_id_from_url(profile_url)
    author_name = user_subtitle or user_title or author_id
    bio = bio.replace("\r", "").replace("\n", " | ")

    return {
        "博主主页链接": profile_url,
        "博主名称": author_name,
        "博主ID": author_id,
        "粉丝量": followers,
        "作者简介": bio,
    }

def run_tiktok_profile_spider(txt_path: str, cdp_port_or_url: str, log_callback, finish_callback, stop_event=None, pause_event=None, config=None):
    if config is None:
        config = {}
    page_load_timeout = int(config.get("page_load_timeout", 35000))
    captcha_wait = int(config.get("captcha_wait", 12))

    output_path = None
    completed_path = None
    try:
        profile_urls = parse_profile_urls(txt_path)
        if not profile_urls:
            log_callback("TXT 中没有找到有效的 TikTok 博主主页链接。")
            return

        output_path = build_output_path("tiktok", f"tiktok_profiles_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        writer = XlsxRowWriter(output_path, CSV_FIELDS)

        with sync_playwright() as p:
            log_callback("正在连接本地 Chrome...")
            try:
                _, context = connect_existing_chromium(p, cdp_port_or_url)
            except Exception as exc:
                log_callback(f"连接失败：请确认 Chrome 已自动打开并已登录 TikTok。错误：{exc}")
                return

            page = context.new_page()
            for index, profile_url in enumerate(profile_urls, 1):
                if should_stop(stop_event):
                    log_callback("任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                log_callback(f"[{index}/{len(profile_urls)}] 提取博主信息：{profile_url}")
                try:
                    row = extract_profile_row(page, profile_url, page_load_timeout=page_load_timeout, captcha_wait=captcha_wait)
                    log_callback(f"  完成：{row['博主名称']} | {row['博主ID']} | 粉丝 {row['粉丝量'] or '未提取'}")
                except Exception as exc:
                    row = {
                        "博主主页链接": profile_url,
                        "博主名称": "抓取失败",
                        "博主ID": profile_id_from_url(profile_url),
                        "粉丝量": "",
                        "作者简介": str(exc),
                    }
                    log_callback(f"  失败：{exc}")

                writer.writerow(sanitize_csv_row(row))
                if index % 5 == 0:
                    if random_cooldown(log_callback, stop_event, 3.0, 8.0):
                        break

            if not page.is_closed():
                page.close()

        writer.save()
        log_callback(f"完成，已保存：{output_path}")
        completed_path = output_path
    finally:
        finish_callback(completed_path)
