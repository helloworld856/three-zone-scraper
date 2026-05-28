from __future__ import annotations

from src.studio.base import ToolSpec
from src.studio.discovery import discover_tools

_tools, _errors = discover_tools()
TOOLS: list[ToolSpec] = _tools

__all__ = ["TOOLS"]
