"""LINE-specific access-control helpers.

This module intentionally has no LINE SDK or database imports so its security
rules can be tested without starting the bot.
"""

from __future__ import annotations

from collections.abc import Iterable


_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_SAFE_RISK_LEVELS = {"read_only", "suggestion"}
_BLOCKED_MODULES = {"hr", "finance"}


def is_line_tool_allowed(tool_name: str, registry, role: str | None) -> bool:
    """Apply the LINE boundary again at execution time, not only schema build."""
    info = registry.get_tool_info(str(tool_name or ""))
    if not info:
        return False
    if info.get("module") in _BLOCKED_MODULES:
        return False
    if info.get("risk_level") not in _SAFE_RISK_LEVELS:
        return False
    return bool(registry.is_allowed(tool_name, role))


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
        if not is_line_tool_allowed(tool_name, registry, role):
            continue
        allowed.append(tool)
    return allowed


_LINE_IDENTITY_DENIED_ERROR = (
    "LINE 使用者身分或角色無法解析，已拒絕本次操作；"
    "請先完成 LINE 帳號與 ERP 角色綁定，或改由已登入的 Web 介面操作。"
)


def resolve_line_role(line_user_id: str | None, resolver=None) -> str | None:
    """Resolve the LINE user's ERP role, fail-closed.

    Returns the stripped role string, or ``None`` when the identity is
    missing/blank, the resolver cannot be imported, the lookup raises, or
    the resolved role is missing/blank. Callers MUST deny on ``None`` —
    there is no default-role fallback at the LINE boundary.
    """
    if line_user_id is None or not str(line_user_id).strip():
        return None
    if resolver is None:
        try:
            from backend.database import get_line_user_role

            resolver = get_line_user_role
        except Exception:
            return None
    try:
        role = resolver(str(line_user_id).strip())
    except Exception:
        return None
    if role is None or not str(role).strip():
        return None
    return str(role).strip()


def build_line_gateway_response(
    tool_name: str, args: dict, registry, gateway, role: str | None
) -> tuple[dict, bool]:
    """Execute a LINE tool call through the Gateway, fail-closed on identity.

    ``role=None`` (or blank) means identity resolution failed: deny with a
    consistent payload and never call the Gateway. Otherwise keep the
    existing boundary — non-sensitive read-only/suggestion tools only, with
    the Gateway as the final authorization check.
    """
    if role is None or not str(role).strip():
        return (
            {"status": "denied", "error": _LINE_IDENTITY_DENIED_ERROR},
            False,
        )
    if not is_line_tool_allowed(tool_name, registry, role):
        return (
            {
                "status": "denied",
                "error": "LINE 入口僅允許唯讀或建議工具；寫入操作請由已登入的 Web 介面送審。",
            },
            False,
        )
    gw_result = gateway.call(tool_name, args or {}, role=role)
    payload = gw_result.to_dict()

    if gw_result.is_ok():
        payload["result"] = gw_result.data
        return payload, True

    if gw_result.status == "pending":
        payload["result"] = f"已送審批：{gw_result.message}"
        return payload, False

    payload["error"] = gw_result.message or f"Gateway returned status: {gw_result.status}"
    return payload, False
