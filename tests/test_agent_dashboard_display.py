import pytest
from pathlib import Path

from frontend.page_agent_dashboard import (
    _demo_seed_enabled,
    _filter_purchase_proposals,
    _history_action_kind,
    _initialize_demo_data_if_empty,
    format_parameters_to_chinese,
)


def test_purchase_order_approval_summary_includes_unit_price():
    summary = format_parameters_to_chinese(
        "create_purchase_order",
        {
            "po_id": "PO-1",
            "supplier_id": "SUP01",
            "product_id": "P001",
            "qty": 2,
            "unit_price": 500.0,
        },
    )

    assert "單價 NT$500.00" in summary


@pytest.mark.parametrize(
    ("status", "tool_name", "role", "expected"),
    [
        ("rejected", "create_purchase_order", "guest", "rejected"),
        ("approved", "create_purchase_order", "admin", "not_rollbackable"),
        ("approved", "update_inventory", "guest", "admin_required"),
        ("approved", "update_inventory", "admin", "rollback"),
    ],
)
def test_history_action_kind_distinguishes_status_and_rollback_capability(
    status, tool_name, role, expected
):
    assert _history_action_kind(status, tool_name, role) == expected


def test_demo_seed_is_opt_in(monkeypatch):
    monkeypatch.delenv("ERP_ENABLE_DEMO_SEED", raising=False)
    assert _demo_seed_enabled() is False

    monkeypatch.setenv("ERP_ENABLE_DEMO_SEED", "true")
    assert _demo_seed_enabled() is True


def test_demo_pending_records_have_verifiable_originators(tmp_path, monkeypatch):
    from backend import database
    from backend.tool_gateway import gateway

    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "dashboard-demo.db"))
    database.init_db()

    _initialize_demo_data_if_empty()

    rows = database.run_query(
        "SELECT approval_id, tool_name, requester_username "
        "FROM pending_approvals ORDER BY tool_name"
    )
    assert [(row[1], row[2]) for row in rows] == [
        ("create_order", "sales1"),
        ("update_inventory", "wh1"),
    ]
    update_approval_id = next(
        row[0] for row in rows if row[1] == "update_inventory"
    )
    assert gateway.approve_action(
        update_approval_id, approver="admin"
    ).status == "ok"


def test_approver_queue_only_contains_governed_purchase_proposals():
    records = [
        {"id": "po", "tool": "create_purchase_order"},
        {"id": "csv", "tool": "sync_external_purchase_order"},
        {"id": "stock", "tool": "update_inventory"},
        {"id": "sale", "tool": "create_order"},
    ]

    assert [item["id"] for item in _filter_purchase_proposals(records)] == [
        "po",
        "csv",
    ]


def test_approver_history_filter_accepts_logger_field_name():
    records = [
        {"approval_id": "csv", "tool_name": "sync_external_purchase_order"},
        {"approval_id": "other", "tool_name": "update_inventory"},
    ]

    assert [
        item["approval_id"] for item in _filter_purchase_proposals(records)
    ] == ["csv"]


def test_dashboard_uses_live_principal_and_separate_approver_surface():
    source = (
        Path(__file__).resolve().parents[1] / "frontend/page_agent_dashboard.py"
    ).read_text(encoding="utf-8")

    assert "principal = load_principal(username)" in source
    assert "mode = dashboard_mode(principal)" in source
    assert "_render_purchase_approval_dashboard(" in source
    assert "current_role = principal.role" in source
    assert "current_username = principal.username" in source
    assert 'if mode == "full":\n        st.markdown("<div class=\'premium-title\'>🕵️ Agent Dashboard' in source
