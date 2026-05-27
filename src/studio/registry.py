from __future__ import annotations

from src.studio.base import ToolSpec
from src.studio.discovery import discover_tools

TOOLS: list[ToolSpec] = discover_tools()

__all__ = ["TOOLS"]
