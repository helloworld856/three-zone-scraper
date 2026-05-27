from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Sequence

from src.studio.base import ToolSpec

logger = logging.getLogger(__name__)

SCAN_DIRS = [
    "src/platforms",
    "src/processing",
]


def discover_tools(scan_dirs: Sequence[str] | None = None) -> list[ToolSpec]:
    if scan_dirs is None:
        scan_dirs = SCAN_DIRS

    project_root = Path(__file__).resolve().parents[2]
    tools: list[ToolSpec] = []

    for scan_dir in scan_dirs:
        base = project_root / scan_dir
        if not base.is_dir():
            logger.warning("Scan directory not found: %s", base)
            continue

        for manifest_path in sorted(base.rglob("*.manifest.json")):
            try:
                tool = _load_manifest(manifest_path)
                if tool:
                    tools.append(tool)
            except Exception:
                logger.exception("Failed to load manifest: %s", manifest_path)

    return tools


def _load_manifest(path: Path) -> ToolSpec | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        logger.error("Manifest %s contains invalid JSON", path)
        return None

    required = {"tool_id", "name", "category", "summary", "entrypoint"}
    missing = required - set(data)
    if missing:
        logger.error("Manifest %s missing required fields: %s", path, missing)
        return None

    tags = tuple(data.get("tags", []))

    return ToolSpec(
        tool_id=data["tool_id"],
        name=data["name"],
        category=data["category"],
        summary=data["summary"],
        entrypoint=data["entrypoint"],
        implementation_path=data.get("implementation_path", ""),
        tags=tags,
    )
