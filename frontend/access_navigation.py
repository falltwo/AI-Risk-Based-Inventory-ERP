"""Pure navigation contracts derived from a live authorization principal."""

from __future__ import annotations

from collections.abc import MutableMapping

from backend.access_control import (
    APPROVAL_QUEUE_READ,
    ERP_EXCHANGE_EXPORT,
    ERP_EXCHANGE_PROPOSE,
    ERP_EXCHANGE_RECONCILE,
    PROPOSAL_EVIDENCE_READ,
    RISK_ANALYSIS_READ,
    RISK_OVERVIEW_READ,
    RISK_WHAT_IF_RUN,
    AccessContext,
)


FULL_MENU = {
    "📊 營運分析看板": [],
    "🤖 AI 智能助理": ["對話介面", "LINE 客服記錄", "Agent Dashboard"],
    "📦 進銷存": ["商品管理", "庫存數量", "入庫/出庫", "條碼掃描", "倉庫管理"],
    "🛒 採購管理": ["採購單", "供應商管理", "進貨成本", "採購歷史", "ERP CSV 交換"],
    "💰 銷售管理": ["報價單", "銷售單", "客戶消費視覺化", "客戶個人消費分析", "收款管理"],
    "📒 財務會計": ["應收/應付", "總帳", "成本分析", "財報"],
    "👥 人資": ["員工資料", "薪資", "出勤"],
    "🌿 碳排放管理": ["碳排放總覽", "碳足跡追蹤", "減量目標", "年度碳目標分析", "ESG 報告", "供應商風險與碳排"],
    "🌱 供應鏈與風險": [],
}

_LEGACY_ROLE_MENUS = {
    "admin": tuple(FULL_MENU),
    "warehouse": (
        "📊 營運分析看板",
        "🤖 AI 智能助理",
        "📦 進銷存",
        "🛒 採購管理",
        "🌱 供應鏈與風險",
    ),
    "sales": ("📊 營運分析看板", "🤖 AI 智能助理", "💰 銷售管理", "🌿 碳排放管理"),
    "hr": ("📊 營運分析看板", "🤖 AI 智能助理", "👥 人資"),
}

ROLE_NAMES = {
    "admin": "系統管理員",
    "warehouse": "倉管部",
    "hr": "人資部",
    "sales": "業務部",
    "risk_viewer": "風險觀測員",
    "supply_planner": "供應鏈規劃員",
    "procurement_approver": "採購核准主管",
}


def risk_sections(principal: AccessContext) -> tuple[str, ...]:
    sections: list[str] = []
    if principal.can(RISK_OVERVIEW_READ):
        sections.append("overview")
    if principal.can(RISK_ANALYSIS_READ):
        sections.append("analysis")
    if principal.can(RISK_WHAT_IF_RUN):
        sections.append("what_if")
    return tuple(sections)


def exchange_sections(principal: AccessContext) -> tuple[str, ...]:
    sections: list[str] = []
    if principal.can(ERP_EXCHANGE_PROPOSE):
        sections.append("proposal")
    if principal.can(ERP_EXCHANGE_EXPORT):
        sections.append("export")
    if principal.can(ERP_EXCHANGE_RECONCILE):
        sections.append("reconcile")
    return tuple(sections)


def dashboard_mode(principal: AccessContext) -> str:
    if not principal.can(APPROVAL_QUEUE_READ):
        return "none"
    if principal.role in {"admin", "warehouse"}:
        return "full"
    if principal.can(PROPOSAL_EVIDENCE_READ):
        return "approvals"
    return "none"


def effective_product_levels(principal: AccessContext) -> tuple[str, ...]:
    levels: list[str] = []
    if principal.can(RISK_OVERVIEW_READ):
        levels.append("L1")
    if principal.can(RISK_ANALYSIS_READ) or principal.can(ERP_EXCHANGE_PROPOSE):
        levels.append("L2")
    if (
        principal.can(APPROVAL_QUEUE_READ)
        or principal.can(ERP_EXCHANGE_EXPORT)
        or principal.can(ERP_EXCHANGE_RECONCILE)
    ):
        levels.append("L3")
    return tuple(levels)


def build_menu_structure(principal: AccessContext) -> dict[str, list[str]]:
    if principal.role == "risk_viewer":
        return {"🌱 供應鏈與風險": []} if risk_sections(principal) else {}
    if principal.role == "supply_planner":
        menu: dict[str, list[str]] = {}
        if risk_sections(principal):
            menu["🌱 供應鏈與風險"] = []
        if exchange_sections(principal):
            menu["🛒 採購管理"] = ["ERP CSV 交換"]
        return menu
    if principal.role == "procurement_approver":
        menu = {}
        if risk_sections(principal):
            menu["🌱 供應鏈與風險"] = []
        if dashboard_mode(principal) == "approvals":
            menu["🤖 AI 智能助理"] = ["Agent Dashboard"]
        if exchange_sections(principal):
            menu["🛒 採購管理"] = ["ERP CSV 交換"]
        return menu

    allowed = _LEGACY_ROLE_MENUS.get(principal.role, ())
    menu = {item: list(FULL_MENU[item]) for item in allowed}
    if not risk_sections(principal):
        menu.pop("🌱 供應鏈與風險", None)
    if not exchange_sections(principal) and "🛒 採購管理" in menu:
        menu["🛒 採購管理"] = [
            item for item in menu["🛒 採購管理"] if item != "ERP CSV 交換"
        ]
    if dashboard_mode(principal) == "none" and "🤖 AI 智能助理" in menu:
        menu["🤖 AI 智能助理"] = [
            item for item in menu["🤖 AI 智能助理"] if item != "Agent Dashboard"
        ]
    return menu


def clear_identity_session_state(state: MutableMapping[str, object]) -> None:
    """Remove authentication identity and page state without touching API config."""
    for key in ("username", "role", "name"):
        state.pop(key, None)
    for key in list(state):
        if (
            key.startswith("erp_csv_")
            or key.startswith("po_")
            or key.startswith("radio_")
        ):
            state.pop(key, None)
    state["logged_in"] = False
    state["menu_selection"] = None
    state["sub_menu"] = None
    if "messages" in state:
        state["messages"] = []


def normalize_navigation_state(
    state: MutableMapping[str, object], menu: dict[str, list[str]]
) -> None:
    """Replace stale main/submenu values after a live role or entitlement change."""
    if not menu:
        state["menu_selection"] = None
        state["sub_menu"] = None
        return

    selected = state.get("menu_selection")
    if selected not in menu:
        selected = next(iter(menu))
        state["menu_selection"] = selected

    submenus = menu[selected]
    selected_submenu = state.get("sub_menu")
    if selected_submenu not in submenus:
        selected_submenu = submenus[0] if submenus else None
        state["sub_menu"] = selected_submenu
    if submenus:
        state[f"radio_{selected}"] = selected_submenu
