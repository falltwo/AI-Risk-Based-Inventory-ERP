"""
backend/tool_registry.py
工具登記表 — 提供 Gateway 查詢每個工具的風險等級與允許角色

使用方式：
    from backend.tool_registry import registry
    info = registry.get_tool_info("create_order")
    ok   = registry.is_allowed("create_order", "sales")
"""

from backend.tool_classification import TOOL_CLASSIFICATION


class ToolRegistry:
    """工具登記表，供 Tool Gateway 查詢用"""

    def __init__(self, classification: dict):
        self._tools = classification

    # ── 基本查詢 ──────────────────────────────────────────────

    def get_tool_info(self, tool_name: str) -> dict | None:
        """回傳工具完整 metadata，工具不存在時回傳 None"""
        return self._tools.get(tool_name)

    def get_risk_level(self, tool_name: str) -> str | None:
        """回傳風險等級字串，工具不存在時回傳 None"""
        info = self._tools.get(tool_name)
        return info["risk_level"] if info else None

    def is_allowed(self, tool_name: str, role: str) -> bool:
        """檢查指定角色是否有權限使用此工具"""
        info = self._tools.get(tool_name)
        if not info:
            return False
        return role in info["allowed_roles"]

    def tool_exists(self, tool_name: str) -> bool:
        """檢查工具是否在登記表內"""
        return tool_name in self._tools

    # ── 批次查詢 ──────────────────────────────────────────────

    def get_tools_for_role(self, role: str) -> list[str]:
        """回傳某角色可使用的所有工具名稱"""
        return [
            name for name, info in self._tools.items()
            if role in info["allowed_roles"]
        ]

    def get_tools_by_risk_level(self, risk_level: str) -> list[str]:
        """回傳特定風險等級的所有工具名稱"""
        return [
            name for name, info in self._tools.items()
            if info["risk_level"] == risk_level
        ]

    def get_tools_by_module(self, module: str) -> list[str]:
        """回傳特定模組的所有工具名稱"""
        return [
            name for name, info in self._tools.items()
            if info["module"] == module
        ]

    # ── 摘要 ──────────────────────────────────────────────────

    def summary(self) -> dict:
        """回傳工具數量統計"""
        from collections import Counter
        levels = Counter(info["risk_level"] for info in self._tools.values())
        return {
            "total": len(self._tools),
            "read_only":  levels.get("read_only", 0),
            "suggestion": levels.get("suggestion", 0),
            "write":      levels.get("write", 0),
            "dangerous":  levels.get("dangerous", 0),
        }


# 全域單例，直接 import 使用
registry = ToolRegistry(TOOL_CLASSIFICATION)
