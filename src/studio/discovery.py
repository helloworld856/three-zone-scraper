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


def discover_tools(scan_dirs: Sequence[str] | None = None) -> tuple[list[ToolSpec], list[str]]:
    if scan_dirs is None:
        scan_dirs = SCAN_DIRS

    project_root = Path(__file__).resolve().parents[2]
    tools: list[ToolSpec] = []
    errors: list[str] = []
    seen_ids: set[str] = set()

    for scan_dir in scan_dirs:
        base = project_root / scan_dir
        if not base.is_dir():
            logger.warning("Scan directory not found: %s", base)
            continue

        for manifest_path in sorted(base.rglob("*.manifest.json")):
            try:
                tool, err = _load_manifest(manifest_path)
                if err:
                    errors.append(err)
                if tool:
                    if tool.tool_id in seen_ids:
                        err_msg = f"工具 ID 冲突: '{tool.tool_id}' ({manifest_path})"
                        logger.error(err_msg)
                        errors.append(err_msg)
                    else:
                        seen_ids.add(tool.tool_id)
                        tools.append(tool)
            except Exception as e:
                err_msg = f"加载 {manifest_path} 时发生未捕获异常: {e}"
                logger.exception(err_msg)
                errors.append(err_msg)

    return tools, errors


def _load_manifest(path: Path) -> tuple[ToolSpec | None, str | None]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        err = f"文件 {path.name} 包含无效的 JSON: {e}"
        logger.error(err)
        return None, err
    except Exception as e:
        err = f"无法读取文件 {path.name}: {e}"
        logger.error(err)
        return None, err

    required = {"tool_id", "name", "category", "summary", "entrypoint"}
    missing = required - set(data)
    if missing:
        err = f"文件 {path.name} 缺少必需字段: {missing}"
        logger.error(err)
        return None, err

    tags = tuple(data.get("tags", []))

    tool = ToolSpec(
        tool_id=data["tool_id"],
        name=data["name"],
        category=data["category"],
        summary=data["summary"],
        entrypoint=data["entrypoint"],
        implementation_path=data.get("implementation_path", ""),
        tags=tags,
    )
    return tool, None
