"""LINE-specific access-control helpers.

This module intentionally has no LINE SDK or database imports so its security
rules can be tested without starting the bot.
"""

from __future__ import annotations

from collections.abc import Iterable


_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_SAFE_RISK_LEVELS = {"read_only", "suggestion"}
_BLOCKED_MODULES = {"hr", "finance"}


def env_flag(value: str | None, default: bool = False) -> bool:
    """Parse an opt-in environment flag; unknown values use ``default``."""
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in _TRUTHY_VALUES:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def parse_line_user_ids(raw: str | None) -> tuple[str, ...]:
    """Return a stable, de-duplicated tuple of configured LINE user IDs."""
    seen: set[str] = set()
    user_ids: list[str] = []
    for value in (raw or "").split(","):
        user_id = value.strip()
        if user_id and user_id not in seen:
            seen.add(user_id)
            user_ids.append(user_id)
    return tuple(user_ids)


def build_line_tools(all_tools: Iterable, registry, role: str) -> list:
    """Expose only non-sensitive, non-writing tools allowed for LINE's role.

    The Gateway remains the final authorization boundary. Filtering tool
    schemas here prevents the public LINE model from seeing or selecting HR,
    finance, write, or dangerous tools in the first place.
    """
    allowed = []
    for tool in all_tools:
        tool_name = getattr(tool, "__name__", "")
        info = registry.get_tool_info(tool_name) if tool_name else None
        if not info:
            continue
        if info.get("module") in _BLOCKED_MODULES:
            continue
        if info.get("risk_level") not in _SAFE_RISK_LEVELS:
            continue
        if not registry.is_allowed(tool_name, role):
            continue
        allowed.append(tool)
    return allowed
