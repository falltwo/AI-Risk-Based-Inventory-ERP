"""UI contracts for the durable ERP CSV exchange page."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _call_is_guarded_by_button(tree: ast.AST, function_name: str) -> bool:
    """Return whether a named call is nested under an explicit button guard."""

    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        guarded_source = ast.dump(node.test)
        if "button" not in guarded_source and "form_submit_button" not in guarded_source:
            continue
        if any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == function_name
            for statement in node.body
            for child in ast.walk(statement)
        ):
            return True
    return False


def test_procurement_menu_routes_to_erp_csv_exchange_page():
    app_source = _source("app.py")
    procurement_source = _source("frontend/page_procurement.py")

    assert '"ERP CSV 交換"' in app_source
    assert '"ERP CSV 交換"' in procurement_source
    assert "page_erp_csv_exchange" in procurement_source


def test_logout_clears_all_erp_csv_session_keys():
    app_tree = ast.parse(_source("app.py"))
    logout = next(
        node
        for node in app_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "logout"
    )
    normalized = ast.dump(logout)

    assert "erp_csv_" in normalized
    assert "startswith" in normalized
    assert "Delete" in normalized


def test_csv_writes_and_receipt_reconciliation_require_explicit_buttons():
    tree = ast.parse(_source("frontend/page_erp_csv_exchange.py"))

    assert _call_is_guarded_by_button(tree, "stage_purchase_order_rows")
    assert _call_is_guarded_by_button(tree, "reconcile_receipt_csv")


def test_exchange_page_uses_gateway_with_version_derived_operation_id():
    source = _source("frontend/page_erp_csv_exchange.py")
    normalized = " ".join(source.split())

    assert 'gateway.call( "sync_external_purchase_order"' in normalized
    assert '"source_system": record["source_system"]' in source
    assert '"external_id": record["external_id"]' in source
    assert "build_exchange_operation_id(" in source
    assert "operation_id=operation_id" in source
    assert 'agent_name="procurement_agent"' in source
    assert "build_receipt_template_csv(" in source


def test_pending_state_is_described_as_not_synchronized():
    from frontend.page_erp_csv_exchange import describe_sync_state

    pending = describe_sync_state(
        {"sync_state": "pending", "receipt_status": None}
    )
    approved = describe_sync_state(
        {"sync_state": "approved", "receipt_status": None}
    )
    acknowledged = describe_sync_state(
        {"sync_state": "acknowledged", "receipt_status": "accepted"}
    )

    assert "尚未同步" in pending
    assert "待 ERP 回執" in approved
    assert "accepted" in acknowledged


def test_preview_rows_add_supplier_risk_without_changing_import_rows():
    from frontend.page_erp_csv_exchange import build_preview_rows

    imported = [
        {
            "external_id": "EXT-1",
            "po_id": "PO-1",
            "supplier_id": "SUP-1",
            "product_id": "P-1",
            "qty": 2,
            "unit_price": 10.0,
            "order_date": "2026-07-20",
            "status": "draft",
            "note": "keep",
        }
    ]
    original = dict(imported[0])

    preview = build_preview_rows(
        imported,
        {
            "SUP-1": {
                "country": "TW",
                "region": "East Asia",
                "risk_level": "低",
            }
        },
    )

    assert imported[0] == original
    assert preview[0]["supplier_country"] == "TW"
    assert preview[0]["supplier_region"] == "East Asia"
    assert preview[0]["supplier_risk_level"] == "低"


def test_page_does_not_store_uploaded_csv_bytes_in_session_state():
    source = _source("frontend/page_erp_csv_exchange.py")

    assert "erp_csv_raw" not in source
    assert "erp_csv_upload_bytes" not in source
    assert "session_state[uploaded.getvalue()" not in source


def test_export_caption_describes_immutable_approved_versions_truthfully():
    source = _source("frontend/page_erp_csv_exchange.py")

    assert "每次核准都固定為不可變快照" in source
    assert "最新版本會被匯出" not in source
