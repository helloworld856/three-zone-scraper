from __future__ import annotations

import atexit
import logging
import os
import subprocess
import time
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

logger = logging.getLogger(__name__)

DEFAULT_X_CDP_URL = "http://localhost:9222"
DEFAULT_TIKTOK_CDP_URL = "http://localhost:9222"
DEFAULT_CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

_chrome_process: subprocess.Popen | None = None


def _cleanup_chrome():
    global _chrome_process
    if _chrome_process is not None and _chrome_process.poll() is None:
        _chrome_process.terminate()
        try:
            _chrome_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _chrome_process.kill()
        _chrome_process = None


atexit.register(_cleanup_chrome)


def build_cdp_url(port_or_url: str | int) -> str:
    value = str(port_or_url).strip()
    if not value:
        raise ValueError("CDP port or URL is required.")

    if value.startswith("http://") or value.startswith("https://"):
        return value

    return f"http://localhost:{value}"


def debug_port_from_cdp_url(port_or_url: str | int) -> str:
    cdp_url = build_cdp_url(port_or_url)
    parsed = urlparse(cdp_url)
    if parsed.port is not None:
        return str(parsed.port)
    return parsed.netloc or cdp_url


def get_workspace_root():
    from src.core.output import get_workspace_root

    return get_workspace_root()


def get_chrome_user_data_dir() -> str:
    user_data_dir = get_workspace_root() / "user_data"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    return str(user_data_dir)


def find_chrome_executable() -> str:
    for path in DEFAULT_CHROME_PATHS:
        if os.path.exists(path):
            return path

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        local_chrome = os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe")
        if os.path.exists(local_chrome):
            return local_chrome

    return "chrome.exe"


def chrome_launch_hint(port_or_url: str | int) -> str:
    return (
        f'"{find_chrome_executable()}" '
        f"--remote-debugging-port={debug_port_from_cdp_url(port_or_url)} "
        "--remote-allow-origins=* "
        f'--user-data-dir="{get_chrome_user_data_dir()}"'
    )


def is_cdp_available(port_or_url: str | int, timeout: float = 1.0) -> bool:
    cdp_url = build_cdp_url(port_or_url).rstrip("/")
    try:
        with urlopen(f"{cdp_url}/json/version", timeout=timeout) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def launch_chrome_for_cdp(port_or_url: str | int) -> subprocess.Popen:
    global _chrome_process
    chrome_path = find_chrome_executable()
    port = debug_port_from_cdp_url(port_or_url)
    user_data_dir = get_chrome_user_data_dir()
    _chrome_process = subprocess.Popen(
        [
            chrome_path,
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return _chrome_process


def ensure_chrome_for_cdp(port_or_url: str | int, log_callback=None, wait_seconds: float = 12.0) -> None:
    if is_cdp_available(port_or_url):
        return

    if log_callback:
        log_callback("未检测到浏览器，正在自动启动 Chrome...")
    launch_chrome_for_cdp(port_or_url)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_cdp_available(port_or_url):
            return
        time.sleep(0.4)

    raise RuntimeError(
        f"Chrome 未能在 {wait_seconds}s 内启动在端口 {debug_port_from_cdp_url(port_or_url)}。"
        f"请检查 Chrome 是否已安装且未被阻止。"
    )


def connect_existing_chromium(
    playwright: Any,
    port_or_url: str | int,
    *,
    context_index: int = 0,
    log_callback=None,
):
    ensure_chrome_for_cdp(port_or_url, log_callback=log_callback)
    cdp_url = build_cdp_url(port_or_url)
    browser = playwright.chromium.connect_over_cdp(cdp_url)
    contexts = browser.contexts
    context = contexts[context_index] if len(contexts) > context_index else browser.new_context()
    return browser, context
