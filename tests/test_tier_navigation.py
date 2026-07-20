from backend.access_control import AccessContext, capabilities_for_role
from frontend.access_navigation import (
    build_menu_structure,
    clear_identity_session_state,
    dashboard_mode,
    effective_product_levels,
    exchange_sections,
    normalize_navigation_state,
    risk_sections,
)
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _principal(username: str, role: str) -> AccessContext:
    return AccessContext(
        username=username,
        role=role,
        name=username,
        organization_id="demo-org",
        entitlements=frozenset({"l1_monitor", "l2_decision", "l3_governed_action"}),
        capabilities=frozenset(capabilities_for_role(role)),
    )


def test_viewer_only_sees_l1_risk_overview():
    principal = _principal("viewer", "risk_viewer")

    assert build_menu_structure(principal) == {"🌱 供應鏈與風險": []}
    assert risk_sections(principal) == ("overview",)
    assert exchange_sections(principal) == ()
    assert dashboard_mode(principal) == "none"
    assert effective_product_levels(principal) == ("L1",)


def test_planner_sees_l1_l2_and_proposal_only():
    principal = _principal("planner", "supply_planner")

    assert build_menu_structure(principal) == {
        "🌱 供應鏈與風險": [],
        "🛒 採購管理": ["ERP CSV 交換"],
    }
    assert risk_sections(principal) == ("overview", "analysis", "what_if")
    assert exchange_sections(principal) == ("proposal",)
    assert dashboard_mode(principal) == "none"
    assert effective_product_levels(principal) == ("L1", "L2")


def test_approver_sees_l1_approval_evidence_and_l3_execution():
    principal = _principal("approver", "procurement_approver")

    assert build_menu_structure(principal) == {
        "🌱 供應鏈與風險": [],
        "🤖 AI 智能助理": ["Agent Dashboard"],
        "🛒 採購管理": ["ERP CSV 交換"],
    }
    assert risk_sections(principal) == ("overview",)
    assert exchange_sections(principal) == ("export", "reconcile")
    assert dashboard_mode(principal) == "approvals"
    assert effective_product_levels(principal) == ("L1", "L3")


def test_admin_keeps_full_existing_navigation_and_dashboard():
    principal = _principal("admin", "admin")

    menu = build_menu_structure(principal)
    assert "📊 營運分析看板" in menu
    assert menu["🤖 AI 智能助理"] == [
        "對話介面",
        "LINE 客服記錄",
        "Agent Dashboard",
    ]
    assert menu["🛒 採購管理"][-1] == "ERP CSV 交換"
    assert dashboard_mode(principal) == "full"


def test_warehouse_keeps_monitor_only_agent_dashboard():
    principal = _principal("warehouse", "warehouse")

    menu = build_menu_structure(principal)
    assert "Agent Dashboard" in menu["🤖 AI 智能助理"]
    assert dashboard_mode(principal) == "full"


def test_legacy_navigation_removes_tier_surfaces_after_live_entitlement_loss():
    principal = AccessContext(
        username="admin",
        role="admin",
        name="admin",
        organization_id="demo-org",
        entitlements=frozenset(),
        capabilities=frozenset(),
    )

    menu = build_menu_structure(principal)

    assert "🌱 供應鏈與風險" not in menu
    assert "ERP CSV 交換" not in menu["🛒 採購管理"]
    assert "Agent Dashboard" not in menu["🤖 AI 智能助理"]


def test_logout_clears_identity_and_tier_page_state():
    state = {
        "logged_in": True,
        "username": "planner",
        "role": "supply_planner",
        "name": "供應鏈規劃員",
        "menu_selection": "🛒 採購管理",
        "sub_menu": "ERP CSV 交換",
        "erp_csv_notice": "sent",
        "po_operation_id": "old-user-operation",
        "po_last_approval_id": "old-user-approval",
        "messages": ["hello"],
        "gemini_key": "keep",
    }

    clear_identity_session_state(state)

    assert state["logged_in"] is False
    assert state["menu_selection"] is None
    assert state["sub_menu"] is None
    assert state["messages"] == []
    assert state["gemini_key"] == "keep"
    assert "username" not in state
    assert "role" not in state
    assert "name" not in state
    assert "erp_csv_notice" not in state
    assert "po_operation_id" not in state
    assert "po_last_approval_id" not in state


def test_live_role_change_replaces_stale_submenu_state():
    state = {
        "menu_selection": "🛒 採購管理",
        "sub_menu": "採購單",
        "radio_🛒 採購管理": "採購單",
    }
    planner_menu = {
        "🌱 供應鏈與風險": [],
        "🛒 採購管理": ["ERP CSV 交換"],
    }

    normalize_navigation_state(state, planner_menu)

    assert state["menu_selection"] == "🛒 採購管理"
    assert state["sub_menu"] == "ERP CSV 交換"
    assert state["radio_🛒 採購管理"] == "ERP CSV 交換"


def test_app_reloads_live_principal_and_passes_username_to_tier_pages():
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "load_principal(" in source
    assert "build_menu_structure(principal)" in source
    assert "normalize_navigation_state(st.session_state, MENU_STRUCTURE)" in source
    assert "clear_identity_session_state(st.session_state)" in source
    assert "viewer / viewer" in source
    assert "planner / planner" in source
    assert "approver / approver" in source
    assert "render_agent_dashboard(username=principal.username)" in source
    assert "render_procurement(sub_menu=sub_menu, username=principal.username)" in source
    assert "render_supply_chain_risk(" in source
    assert "username=principal.username" in source


def test_tier_pages_derive_sections_from_live_principal():
    risk_source = (ROOT / "frontend/page_supply_chain_risk.py").read_text(
        encoding="utf-8"
    )
    exchange_source = (ROOT / "frontend/page_erp_csv_exchange.py").read_text(
        encoding="utf-8"
    )
    procurement_source = (ROOT / "frontend/page_procurement.py").read_text(
        encoding="utf-8"
    )

    assert "principal = load_principal(username)" in risk_source
    assert "sections = risk_sections(principal)" in risk_source
    assert risk_source.count("actor=principal.username") == 4
    assert "principal = load_principal(username)" in exchange_source
    assert "sections = exchange_sections(principal)" in exchange_source
    assert "actor=current_actor" in exchange_source
    assert "principal = load_principal(username)" in procurement_source
    assert "role=principal.role" in procurement_source
    assert "actor=principal.username" in procurement_source
