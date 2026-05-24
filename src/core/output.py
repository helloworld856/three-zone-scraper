from __future__ import annotations

from pathlib import Path


PLATFORM_DIRS = {
    "youtube": "youtube",
    "tiktok": "tiktok",
    "x": "x",
    "x_platform": "x",
    "twitter": "x",
    "data": "data",
}


def get_workspace_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    if not (root / "requirements.txt").exists() and not (root / "main.py").exists():
        raise RuntimeError(f"Workspace root not found at {root}")
    return root


def get_output_root() -> Path:
    output_root = get_workspace_root() / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    return output_root


def get_platform_output_dir(platform: str) -> Path:
    if ".." in platform or "/" in platform or "\\" in platform:
        raise ValueError(f"Invalid platform name: {platform}")
    folder_name = PLATFORM_DIRS.get(platform, platform)
    output_dir = get_output_root() / folder_name
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def build_output_path(platform: str, filename: str) -> str:
    return str(get_platform_output_dir(platform) / filename)
