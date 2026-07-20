import pytest

from frontend.page_agent_dashboard import (
    _demo_seed_enabled,
    _history_action_kind,
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
