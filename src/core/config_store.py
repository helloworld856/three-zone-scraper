"""JSON 持久化配置管理。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from src.core.output import get_workspace_root

_CONFIG_DIR_NAME = "config"

DEFAULT_CONFIGS: dict[str, dict] = {
    "youtube_keyword_mining": {
        "max_results": 5000,
        "youtube_search_batch_size": 50,
        "youtube_video_batch_size": 50,
        "comment_top_limit": 100,
    },
    "youtube_paired_context_metrics": {
        "context_size": 5,
        "max_upload_pages": 200,
    },
    "youtube_channel_works": {
        "max_video_items": 5000,
        "page_load_timeout": 45000,
        "scroll_delay": 0.8,
        "no_new_scroll_limit": 6,
        "scroll_px": 2800,
        "max_post_scrolls": 200,
        "save_batch_size": 10,
    },
    "youtube_top_comments": {
        "max_scan_comments": 500,
        "comment_top_limit": 100,
        "youtube_api_page_size": 100,
    },
    "tiktok_keyword_metrics": {
        "max_videos": 1000,
        "max_candidates": 3000,
        "scroll_interval": 0.7,
        "max_search_scrolls": 360,
        "no_new_scroll_limit": 12,
        "comment_top_limit": 100,
    },
    "tiktok_profile_directory": {
        "page_load_timeout": 35000,
        "captcha_wait": 12,
    },
    "tiktok_profile_videos": {
        "page_load_timeout": 45000,
        "scroll_interval": 2.5,
        "no_new_scroll_limit": 10,
        "max_scrolls": 200,
        "link_batch_size": 50,
        "save_batch_size": 10,
        "cooldown_min": 10.0,
        "cooldown_max": 20.0,
    },
    "tiktok_profile_play_counts": {
        "page_load_timeout": 45000,
        "scroll_interval": 2.5,
        "no_new_scroll_limit": 10,
        "max_scrolls": 200,
    },
    "tiktok_paired_context_metrics": {
        "context_size": 5,
        "api_page_size": 35,
        "max_api_pages": 10,
        "max_profile_scrolls": 80,
        "scroll_interval": 0.8,
    },
    "tiktok_top_comments": {
        "comment_top_limit": 100,
        "page_load_timeout": 45000,
        "scroll_interval": 1.4,
        "max_scroll_rounds": 80,
    },
    "x_keyword_video_search": {
        "slice_days": 7,
        "search_page_timeout": 40000,
        "cooldown_min": 5.0,
        "cooldown_max": 7.0,
        "no_new_scroll_limit": 5,
        "max_scrolls": 200,
    },
    "x_tweet_author_profiles": {
        "page_load_timeout": 45000,
        "tweet_ready_timeout": 12000,
    },
    "x_paired_context_metrics": {
        "context_size": 5,
        "max_profile_scrolls": 45,
        "scroll_interval": 3.8,
        "page_load_timeout": 45000,
    },
    "x_tweet_metrics": {
        "page_load_timeout": 30000,
        "comment_top_limit": 100,
    },
    "x_profile_tweets": {
        "page_load_timeout": 30000,
        "scroll_interval": 3.2,
        "no_new_scroll_limit": 10,
        "max_scrolls": 200,
        "save_batch_size": 10,
        "cooldown_min": 6.0,
        "cooldown_max": 15.0,
    },
    "x_top_comments": {
        "comment_top_limit": 100,
        "page_load_timeout": 30000,
        "scroll_interval": 4.0,
        "no_new_scroll_limit": 5,
    },
    "instagram_profile_works": {
        "max_works": 5000,
        "page_load_timeout": 45000,
        "scroll_interval": 3.0,
        "scroll_px": 2600,
        "no_new_scroll_limit": 8,
        "max_scrolls": 200,
        "save_batch_size": 10,
        "cooldown_min": 10.0,
        "cooldown_max": 25.0,
        "detail_delay_min": 3.0,
        "detail_delay_max": 7.0,
    },
    "judge_aigc": {
        "temperature": 0.1,
        "sleep_seconds": 0.5,
        "trust_local_negative_aigc": False,
    },
}


def get_config_dir() -> Path:
    return get_workspace_root() / _CONFIG_DIR_NAME


def get_config_path(tool_id: str) -> Path:
    return get_config_dir() / f"{tool_id}.json"


def get_config_path_for_profile(tool_id: str, profile: str | None) -> Path:
    """获取指定方案的配置文件路径。profile 为 None 时使用默认文件。"""
    if not profile:
        return get_config_path(tool_id)
    safe_name = profile.strip().translate(str.maketrans({c: "_" for c in r'\/:*?"<>|'}))
    return get_config_dir() / f"{tool_id}_{safe_name}.json"


def list_profiles(tool_id: str) -> list[tuple[str, str | None]]:
    """列出某个工具的所有配置方案。返回 [(显示名, profile_key), ...]，profile_key 为 None 表示默认。"""
    config_dir = get_config_dir()
    if not config_dir.exists():
        return [("默认配置", None)]
    profiles: list[tuple[str, str | None]] = [("默认配置", None)]
    prefix = f"{tool_id}_"
    for f in sorted(config_dir.glob(f"{tool_id}_*.json")):
        stem = f.stem
        if stem.startswith(prefix):
            name = stem[len(prefix):]
            if name:
                profiles.append((name, name))
    return profiles


def _coerce_value(value, default):
    """将加载的 JSON 值强制转换为与默认值相同的类型。"""
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return value


def load_config(tool_id: str, defaults: dict, profile: str | None = None) -> dict:
    """加载配置，缺失字段用 defaults 补齐。profile 为 None 时加载默认配置。"""
    result = dict(defaults)
    path = get_config_path_for_profile(tool_id, profile)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in defaults:
                    if key in data:
                        result[key] = _coerce_value(data[key], defaults[key])
        except (json.JSONDecodeError, OSError):
            pass
    return result


def save_config(tool_id: str, values: dict, defaults: dict | None = None, profile: str | None = None) -> None:
    """保存配置到 JSON 文件，只保留 defaults 中存在的 key。profile 为 None 时保存到默认配置。"""
    if defaults is None:
        defaults = DEFAULT_CONFIGS.get(tool_id, {})
    if not defaults:
        return
    filtered = {k: v for k, v in values.items() if k in defaults}
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    path = get_config_path_for_profile(tool_id, profile)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(filtered, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def generate_all_defaults() -> None:
    """为所有工具生成默认配置 JSON（已存在的文件不会被覆盖）。"""
    for tool_id, defaults in DEFAULT_CONFIGS.items():
        path = get_config_path(tool_id)
        if not path.exists():
            save_config(tool_id, defaults)


def delete_profile(tool_id: str, profile: str) -> bool:
    """删除指定方案。profile 不能为空。返回是否删除成功。"""
    if not profile:
        return False
    path = get_config_path_for_profile(tool_id, profile)
    if path.exists():
        path.unlink()
        return True
    return False
