from __future__ import annotations

import importlib
from dataclasses import dataclass, field


ALL_CATEGORY = "全部"


def load_object(dotted_path: str):
    module_path, object_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, object_name)


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    name: str
    category: str
    summary: str
    entrypoint: str
    implementation_path: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, query: str, category: str) -> bool:
        if category != ALL_CATEGORY and self.category != category:
            return False
        if not query:
            return True
        haystack = " ".join([self.name, self.category, self.summary, " ".join(self.tags)]).lower()
        return query.lower() in haystack

