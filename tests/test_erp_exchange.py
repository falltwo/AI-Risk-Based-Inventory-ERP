"""Contracts for the L3 CSV ERP batch exchange bridge."""

import csv
from concurrent.futures import ThreadPoolExecutor
import io
import json
import sqlite3
import threading

import pytest

from backend import database


IMPORT_COLUMNS = (
    "external_id",
    "po_id",
    "supplier_id",
    "product_id",
    "qty",
    "unit_price",
    "order_date",
    "status",
    "note",
)


@pytest.fixture
def exchange_db(tmp_path, monkeypatch):
    """Use a real isolated SQLite database for every exchange contract."""
    db_path = tmp_path / "erp-exchange.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    database.init_db()
    database.run_query(
        "UPDATE suppliers SET is_official = 1, country = ?, region = ?, "
        "risk_level = ? WHERE supplier_id = ?",
        ("日本", "東亞", "高", "SUP01"),
        fetch=False,
    )
    return db_path


def _exchange_module():
    from backend import erp_exchange

    return erp_exchange


def _csv_bytes(rows, columns=IMPORT_COLUMNS):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    writer.writerows(
        [{column: row.get(column, "") for column in columns} for row in rows]
    )
    return output.getvalue().encode("utf-8")


def _row(**overrides):
    row = {
        "external_id": "odoo.purchase_order_1001",
        "po_id": "EXT-PO-1001",
        "supplier_id": "SUP01",
        "product_id": "P001",
        "qty": "2",
        "unit_price": "500.50",
        "order_date": "2026-07-20",
        "status": "待入庫",
        "note": "imported from Odoo",
    }
    row.update(overrides)
    return row


def _stage_one(source_system="odoo-demo", *, actor="wh1", **overrides):
    exchange = _exchange_module()
    rows = exchange.parse_purchase_order_csv(_csv_bytes([_row(**overrides)]))
    return exchange.stage_purchase_order_rows(source_system, rows, actor=actor)


def _submit_one(source_system="odoo-demo", external_id="odoo.purchase_order_1001"):
    exchange = _exchange_module()
    from backend.tool_gateway import gateway

    record = exchange.get_exchange_record(source_system, external_id)
    operation_id = exchange.build_exchange_operation_id(
        source_system, external_id, record["version"]
    )
    result = gateway.call(
        "sync_external_purchase_order",
        {"source_system": source_system, "external_id": external_id},
        role="warehouse",
        actor="wh1",
        agent_name="procurement_agent",
        operation_id=operation_id,
    )
    return result, operation_id


def test_csv_parser_normalizes_types_and_rejects_schema_drift(exchange_db):
    exchange = _exchange_module()

    parsed = exchange.parse_purchase_order_csv(_csv_bytes([_row()]))

    assert parsed == [
        {
            "external_id": "odoo.purchase_order_1001",
            "po_id": "EXT-PO-1001",
            "supplier_id": "SUP01",
            "product_id": "P001",
            "qty": 2,
            "unit_price": 500.5,
            "order_date": "2026-07-20",
            "status": "待入庫",
            "note": "imported from Odoo",
        }
    ]

    missing = tuple(column for column in IMPORT_COLUMNS if column != "note")
    with pytest.raises(ValueError, match="欄位"):
        exchange.parse_purchase_order_csv(_csv_bytes([_row()], columns=missing))

    extra = IMPORT_COLUMNS + ("unexpected",)
    with pytest.raises(ValueError, match="欄位"):
        exchange.parse_purchase_order_csv(
            _csv_bytes([{**_row(), "unexpected": "x"}], columns=extra)
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"external_id": "=CMD()"}, "external_id"),
        ({"qty": "1.5"}, "qty"),
        ({"qty": "0"}, "qty"),
        ({"unit_price": "NaN"}, "unit_price"),
        ({"order_date": "20/07/2026"}, "order_date"),
    ],
)
def test_csv_parser_rejects_dangerous_or_invalid_values(
    exchange_db, overrides, message
):
    exchange = _exchange_module()

    with pytest.raises(ValueError, match=message):
        exchange.parse_purchase_order_csv(_csv_bytes([_row(**overrides)]))


def test_csv_parser_enforces_file_row_and_duplicate_limits(exchange_db):
    exchange = _exchange_module()

    duplicate = [_row(), _row(po_id="EXT-PO-1002")]
    with pytest.raises(ValueError, match="重複.*external_id"):
        exchange.parse_purchase_order_csv(_csv_bytes(duplicate))

    too_many = [
        _row(external_id=f"ext-{index}", po_id=f"PO-{index}")
        for index in range(exchange.MAX_IMPORT_ROWS + 1)
    ]
    with pytest.raises(ValueError, match="筆數"):
        exchange.parse_purchase_order_csv(_csv_bytes(too_many))

    with pytest.raises(ValueError, match="大小"):
        exchange.parse_purchase_order_csv(b"x" * (exchange.MAX_IMPORT_BYTES + 1))


def test_stage_reimport_is_idempotent_and_changed_content_increments_version(
    exchange_db,
):
    exchange = _exchange_module()

    first = _stage_one()
    same = _stage_one()
    changed = _stage_one(qty="7", note="revised")

    assert first["inserted"] == 1
    assert same["unchanged"] == 1
    assert changed["updated"] == 1
    record = exchange.get_exchange_record(
        "odoo-demo", "odoo.purchase_order_1001"
    )
    assert record["version"] == 2
    assert record["qty"] == 7
    assert record["note"] == "revised"
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM erp_exchange_records"
        ).fetchone()[0] == 1


def test_stage_preview_joins_supplier_risk_without_inventing_ai_result(exchange_db):
    exchange = _exchange_module()
    _stage_one()

    records = exchange.list_exchange_records("odoo-demo", actor="wh1")

    assert len(records) == 1
    assert records[0]["supplier_country"] == "日本"
    assert records[0]["supplier_region"] == "東亞"
    assert records[0]["supplier_risk_level"] == "高"
    assert records[0]["sync_state"] == "staged"
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0


def test_stage_rejects_unknown_product_and_non_official_supplier(exchange_db):
    exchange = _exchange_module()
    database.run_query(
        "UPDATE suppliers SET is_official = 0 WHERE supplier_id = ?",
        ("SUP02",),
        fetch=False,
    )

    non_official = exchange.parse_purchase_order_csv(
        _csv_bytes([_row(supplier_id="SUP02")])
    )
    with pytest.raises(ValueError, match="正式供應商"):
        exchange.stage_purchase_order_rows("odoo-demo", non_official, actor="wh1")

    unknown_product = exchange.parse_purchase_order_csv(
        _csv_bytes([_row(product_id="MISSING")])
    )
    with pytest.raises(ValueError, match="product_id"):
        exchange.stage_purchase_order_rows("odoo-demo", unknown_product, actor="wh1")


def test_stage_rejects_duplicate_po_identity_without_partial_write(exchange_db):
    exchange = _exchange_module()
    rows = exchange.parse_purchase_order_csv(
        _csv_bytes(
            [
                _row(),
                _row(external_id="odoo.purchase_order_1002"),
            ]
        )
    )

    with pytest.raises(ValueError, match="重複 po_id"):
        exchange.stage_purchase_order_rows("odoo-demo", rows, actor="wh1")

    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM erp_exchange_records"
        ).fetchone()[0] == 0


def test_gateway_binds_revision_then_approval_inserts_one_local_po(exchange_db):
    exchange = _exchange_module()
    from backend.agent_logger import get_pending_approval_by_id
    from backend.tool_gateway import gateway

    _stage_one(note="=HYPERLINK(\"https://bad.example\")")
    pending, operation_id = _submit_one()

    assert pending.status == "pending"
    approval = get_pending_approval_by_id(pending.approval_id)
    record = exchange.get_exchange_record(
        "odoo-demo", "odoo.purchase_order_1001"
    )
    assert approval["resource_version"] == exchange.exchange_resource_version(record)
    assert approval["operation_id"] == operation_id
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0

    approved = gateway.approve_action(pending.approval_id, approver="admin")

    assert approved.status == "ok"
    assert approved.data["effect"] == "inserted"
    with sqlite3.connect(exchange_db) as conn:
        po = conn.execute(
            "SELECT po_id, supplier_id, total_amount, external_source_system, "
            "external_id, external_version FROM purchase_orders"
        ).fetchone()
        item = conn.execute(
            "SELECT product_id, qty, unit_price FROM purchase_order_items"
        ).fetchone()
        assert po == (
            "EXT-PO-1001",
            "SUP01",
            1001.0,
            "odoo-demo",
            "odoo.purchase_order_1001",
            1,
        )
        assert item == ("P001", 2, 500.5)
        assert conn.execute(
            "SELECT COUNT(*) FROM effect_receipts"
        ).fetchone()[0] == 1


def test_revised_external_id_updates_existing_po_and_never_duplicates(exchange_db):
    from backend.tool_gateway import gateway

    _stage_one()
    first, first_operation = _submit_one()
    assert gateway.approve_action(first.approval_id, approver="admin").status == "ok"

    _stage_one(qty="9", unit_price="700", status="已確認")
    second, second_operation = _submit_one()
    second_result = gateway.approve_action(second.approval_id, approver="admin")

    assert first_operation != second_operation
    assert second.status == "pending"
    assert second_result.status == "ok"
    assert second_result.data["effect"] == "updated"
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM purchase_order_items"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT total_amount, status, external_version FROM purchase_orders"
        ).fetchone() == (6300.0, "已確認", 2)
        assert conn.execute(
            "SELECT qty, unit_price FROM purchase_order_items"
        ).fetchone() == (9, 700.0)


def test_pending_revision_fails_closed_if_staging_changes_before_approval(exchange_db):
    from backend.tool_gateway import gateway

    _stage_one()
    pending, _ = _submit_one()
    _stage_one(qty="99")

    result = gateway.approve_action(pending.approval_id, approver="admin")

    assert result.status == "error"
    assert "版本" in result.message or "失效" in result.message
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM pending_approvals WHERE approval_id = ?",
            (pending.approval_id,),
        ).fetchone()[0] == "pending"


def test_pending_revision_fails_closed_on_in_place_staging_tamper(exchange_db):
    from backend.tool_gateway import gateway

    _stage_one(unit_price="10")
    pending, _ = _submit_one()
    database.run_query(
        "UPDATE erp_exchange_records SET qty = 999, note = ? "
        "WHERE source_system = ? AND external_id = ?",
        ("tampered", "odoo-demo", "odoo.purchase_order_1001"),
        fetch=False,
    )

    result = gateway.approve_action(pending.approval_id, approver="admin")

    assert result.status == "error"
    assert "完整性" in result.message or "竄改" in result.message
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM pending_approvals WHERE approval_id = ?",
            (pending.approval_id,),
        ).fetchone()[0] == "pending"


def test_external_po_version_tamper_blocks_revised_sync(exchange_db):
    from backend.tool_gateway import gateway

    _stage_one()
    first, _ = _submit_one()
    assert gateway.approve_action(first.approval_id, approver="admin").status == "ok"
    _stage_one(qty="8")
    revised, _ = _submit_one()
    database.run_query(
        "UPDATE purchase_orders SET external_version = 999 WHERE po_id = ?",
        ("EXT-PO-1001",),
        fetch=False,
    )

    result = gateway.approve_action(revised.approval_id, approver="admin")

    assert result.status == "error"
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute(
            "SELECT external_version, total_amount FROM purchase_orders"
        ).fetchone() == (999, 1001.0)
        assert conn.execute(
            "SELECT COUNT(*) FROM effect_receipts"
        ).fetchone()[0] == 1


def test_revised_sync_fails_if_previously_linked_local_po_disappears(exchange_db):
    from backend.tool_gateway import gateway

    _stage_one()
    first, _ = _submit_one()
    assert gateway.approve_action(first.approval_id, approver="admin").status == "ok"
    _stage_one(qty="8")
    revised, _ = _submit_one()
    database.run_query(
        "DELETE FROM purchase_order_items WHERE po_id = ?",
        ("EXT-PO-1001",),
        fetch=False,
    )
    database.run_query(
        "DELETE FROM purchase_orders WHERE po_id = ?",
        ("EXT-PO-1001",),
        fetch=False,
    )

    result = gateway.approve_action(revised.approval_id, approver="admin")

    assert result.status == "error"
    assert "不存在" in result.message or "失效" in result.message
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 1


def test_repeated_submission_and_approval_replay_existing_receipt(exchange_db):
    from backend.tool_gateway import gateway

    _stage_one()
    first, _ = _submit_one()
    approved = gateway.approve_action(first.approval_id, approver="admin")
    replay_submission, _ = _submit_one()
    replay_approval = gateway.approve_action(first.approval_id, approver="admin")

    assert approved.status == "ok"
    assert replay_submission.status == "ok"
    assert replay_approval.status == "ok"
    assert replay_submission.data == approved.data == replay_approval.data
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 1


def test_concurrent_external_approvals_commit_one_effect_and_replay(exchange_db):
    from backend.tool_gateway import gateway

    _stage_one()
    pending, _ = _submit_one()
    barrier = threading.Barrier(2)

    def approve_at_once():
        barrier.wait()
        return gateway.approve_action(pending.approval_id, approver="admin")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve_at_once(), range(2)))

    assert [result.status for result in results] == ["ok", "ok"]
    assert results[0].data == results[1].data
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM purchase_order_items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 1


def test_external_sync_fault_rolls_back_po_receipt_and_approval_state(exchange_db):
    from backend.tool_gateway import gateway

    _stage_one()
    pending, _ = _submit_one()
    with sqlite3.connect(exchange_db) as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_external_receipt
            BEFORE INSERT ON effect_receipts
            BEGIN
                SELECT RAISE(ABORT, 'injected external receipt failure');
            END
            """
        )

    result = gateway.approve_action(pending.approval_id, approver="admin")

    assert result.status == "error"
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM purchase_order_items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM pending_approvals WHERE approval_id = ?",
            (pending.approval_id,),
        ).fetchone()[0] == "pending"
        assert conn.execute(
            "SELECT last_synced_version FROM erp_exchange_records"
        ).fetchone()[0] is None


def test_external_sync_cannot_bypass_approval_or_be_approved_by_non_admin(exchange_db):
    exchange = _exchange_module()
    from backend.tool_gateway import gateway

    _stage_one()
    pending, operation_id = _submit_one()
    bypass = gateway.execute_approved(
        "sync_external_purchase_order",
        {"source_system": "odoo-demo", "external_id": "odoo.purchase_order_1001"},
        role="admin",
    )
    denied = gateway.approve_action(pending.approval_id, approver="warehouse")

    assert bypass.status == "denied"
    assert denied.status == "denied"
    with pytest.raises(PermissionError):
        exchange.sync_external_purchase_order(
            "odoo-demo", "odoo.purchase_order_1001"
        )
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM pending_approvals WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()[0] == "pending"


def test_export_and_receipt_reconciliation_complete_round_trip_safely(
    exchange_db, monkeypatch
):
    exchange = _exchange_module()
    from backend.tool_gateway import gateway

    receipt_secret = "test-only-receipt-secret-with-at-least-32-bytes"
    monkeypatch.setenv("ERP_EXCHANGE_RECEIPT_HMAC_SECRET", receipt_secret)
    monkeypatch.setenv("ERP_EXCHANGE_RECEIPT_KEY_ID", "test-key-v1")
    dangerous_note = "=HYPERLINK(\"https://bad.example\")"
    _stage_one(note=dangerous_note)
    pending, operation_id = _submit_one()
    assert gateway.approve_action(pending.approval_id, approver="admin").status == "ok"

    exported = exchange.export_approved_actions_csv("odoo-demo", actor="admin")
    export_text = exported.decode("utf-8-sig")
    exported_rows = list(csv.DictReader(io.StringIO(export_text)))
    assert len(exported_rows) == 1
    assert exported_rows[0]["operation_id"] == operation_id
    assert exported_rows[0]["note"].startswith("'=HYPERLINK")
    assert exported_rows[0]["approval_id"] == pending.approval_id
    assert exported_rows[0]["payload_digest"]

    receipt_template = exchange.build_receipt_template_csv(
        "odoo-demo", actor="admin"
    )
    template_rows = list(
        csv.DictReader(io.StringIO(receipt_template.decode("utf-8-sig")))
    )
    assert template_rows == [
        {
            "source_system": "odoo-demo",
            "external_id": "odoo.purchase_order_1001",
            "operation_id": operation_id,
            "approval_id": pending.approval_id,
            "payload_digest": exported_rows[0]["payload_digest"],
            "receipt_attempt_id": "",
            "receipt_status": "",
            "message": "",
            "key_id": "test-key-v1",
            "signature": "",
        }
    ]

    receipt_row = {
        **template_rows[0],
        "receipt_attempt_id": "erp-attempt-001",
        "receipt_status": "accepted",
        "message": "ERP import OK",
    }
    receipt_row["signature"] = exchange.compute_receipt_signature(
        receipt_row, receipt_secret
    )
    receipt_bytes = _csv_bytes([receipt_row], columns=tuple(receipt_row))
    first = exchange.reconcile_receipt_csv(receipt_bytes, actor="admin")
    repeated = exchange.reconcile_receipt_csv(receipt_bytes, actor="admin")

    assert first["inserted"] == 1
    assert repeated["unchanged"] == 1
    record = exchange.get_exchange_record(
        "odoo-demo", "odoo.purchase_order_1001"
    )
    assert record["sync_state"] == "acknowledged"
    assert record["receipt_status"] == "accepted"
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute(
            "SELECT received_by FROM erp_exchange_receipts"
        ).fetchone()[0] == "admin"
        audit_rows = conn.execute(
            "SELECT tool_name, parameters, caller FROM agent_action_logs "
            "WHERE tool_name LIKE 'erp_exchange_%' ORDER BY id"
        ).fetchall()
        assert [row[0] for row in audit_rows] == [
            "erp_exchange_export_actions",
            "erp_exchange_export_receipt_template",
            "erp_exchange_reconcile_receipts",
            "erp_exchange_reconcile_receipts",
        ]
        assert all(row[2] == "admin" for row in audit_rows)
        assert all(
            len(json.loads(row[1])["operation_set_digest"]) == 64
            for row in audit_rows
        )

    conflicting_row = {**receipt_row, "message": "different msg"}
    conflicting_row["signature"] = exchange.compute_receipt_signature(
        conflicting_row, receipt_secret
    )
    conflicting = _csv_bytes([conflicting_row], columns=tuple(conflicting_row))
    with pytest.raises(ValueError, match="衝突"):
        exchange.reconcile_receipt_csv(conflicting, actor="admin")

    wrong_digest_row = {**receipt_row, "payload_digest": "0" * 64}
    wrong_digest_row["signature"] = exchange.compute_receipt_signature(
        wrong_digest_row, receipt_secret
    )
    wrong_digest = _csv_bytes([wrong_digest_row], columns=tuple(wrong_digest_row))
    with pytest.raises(ValueError, match="不相符"):
        exchange.reconcile_receipt_csv(wrong_digest, actor="admin")


def test_unsigned_receipt_is_rejected_without_acknowledgement(exchange_db, monkeypatch):
    exchange = _exchange_module()
    from backend.tool_gateway import gateway

    monkeypatch.setenv(
        "ERP_EXCHANGE_RECEIPT_HMAC_SECRET",
        "test-only-receipt-secret-with-at-least-32-bytes",
    )
    monkeypatch.setenv("ERP_EXCHANGE_RECEIPT_KEY_ID", "test-key-v1")
    _stage_one()
    pending, _ = _submit_one()
    assert gateway.approve_action(pending.approval_id, approver="admin").status == "ok"
    template = exchange.build_receipt_template_csv("odoo-demo", actor="admin")
    row = list(csv.DictReader(io.StringIO(template.decode("utf-8-sig"))))[0]
    row["receipt_attempt_id"] = "unsigned-attempt-001"
    row["receipt_status"] = "accepted"
    unsigned = _csv_bytes([row], columns=tuple(row))

    with pytest.raises(ValueError, match="signature|簽章"):
        exchange.reconcile_receipt_csv(unsigned, actor="admin")

    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM erp_exchange_receipts"
        ).fetchone()[0] == 0


def test_approved_export_uses_immutable_snapshot_if_staging_is_modified(exchange_db):
    exchange = _exchange_module()
    from backend.tool_gateway import gateway

    _stage_one()
    pending, _ = _submit_one()
    assert gateway.approve_action(pending.approval_id, approver="admin").status == "ok"
    database.run_query(
        "UPDATE erp_exchange_records SET qty = 999 WHERE source_system = ? AND external_id = ?",
        ("odoo-demo", "odoo.purchase_order_1001"),
        fetch=False,
    )

    rows = list(
        csv.DictReader(
            io.StringIO(
                exchange.export_approved_actions_csv(
                    "odoo-demo", actor="admin"
                ).decode("utf-8-sig")
            )
        )
    )
    assert len(rows) == 1
    assert rows[0]["qty"] == "2"


def test_approved_export_rejects_effect_receipt_identity_tamper(exchange_db):
    exchange = _exchange_module()
    from backend.tool_gateway import gateway

    _stage_one()
    pending, operation_id = _submit_one()
    assert gateway.approve_action(pending.approval_id, approver="admin").status == "ok"
    with sqlite3.connect(exchange_db) as conn:
        raw_result = conn.execute(
            "SELECT result FROM effect_receipts WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()[0]
        tampered = json.loads(raw_result)
        tampered["source_system"] = "evil-source"
        conn.execute(
            "UPDATE effect_receipts SET result = ? WHERE operation_id = ?",
            (json.dumps(tampered), operation_id),
        )

    with pytest.raises(ValueError, match="外部身分"):
        exchange.export_approved_actions_csv("odoo-demo", actor="admin")


def test_export_and_reconcile_require_live_authorized_actor(exchange_db, monkeypatch):
    exchange = _exchange_module()
    monkeypatch.setenv(
        "ERP_EXCHANGE_RECEIPT_HMAC_SECRET",
        "test-only-receipt-secret-with-at-least-32-bytes",
    )
    monkeypatch.setenv("ERP_EXCHANGE_RECEIPT_KEY_ID", "test-key-v1")
    _stage_one()

    with pytest.raises(PermissionError, match="權限"):
        exchange.export_approved_actions_csv("odoo-demo", actor="missing-user")
    with pytest.raises(PermissionError, match="權限"):
        exchange.reconcile_receipt_csv(b"not-a-csv", actor="missing-user")
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_action_logs "
            "WHERE tool_name LIKE 'erp_exchange_%'"
        ).fetchone()[0] == 0


def test_approved_snapshot_remains_exportable_after_new_revision_is_staged(exchange_db):
    exchange = _exchange_module()
    from backend.tool_gateway import gateway

    _stage_one(qty="2", unit_price="10")
    first, _ = _submit_one()
    assert gateway.approve_action(first.approval_id, approver="admin").status == "ok"
    _stage_one(qty="7", unit_price="20")

    rows = list(
        csv.DictReader(
            io.StringIO(
                exchange.export_approved_actions_csv(
                    "odoo-demo", actor="admin"
                ).decode("utf-8-sig")
            )
        )
    )

    assert len(rows) == 1
    assert rows[0]["version"] == "1"
    assert rows[0]["qty"] == "2"
    assert rows[0]["unit_price"] == "10.0"


def test_delayed_receipt_and_new_attempt_can_recover_from_verified_error(
    exchange_db, monkeypatch
):
    exchange = _exchange_module()
    from backend.tool_gateway import gateway

    secret = "test-only-receipt-secret-with-at-least-32-bytes"
    monkeypatch.setenv("ERP_EXCHANGE_RECEIPT_HMAC_SECRET", secret)
    monkeypatch.setenv("ERP_EXCHANGE_RECEIPT_KEY_ID", "test-key-v1")
    _stage_one(qty="2")
    first, _ = _submit_one()
    assert gateway.approve_action(first.approval_id, approver="admin").status == "ok"
    first_template = list(
        csv.DictReader(
            io.StringIO(
                exchange.build_receipt_template_csv(
                    "odoo-demo", actor="admin"
                ).decode("utf-8-sig")
            )
        )
    )[0]

    _stage_one(qty="4")
    second, _ = _submit_one()
    assert gateway.approve_action(second.approval_id, approver="admin").status == "ok"

    failed_row = {
        **first_template,
        "receipt_attempt_id": "erp-attempt-error",
        "receipt_status": "error",
        "message": "temporary ERP import error",
    }
    failed_row["signature"] = exchange.compute_receipt_signature(failed_row, secret)
    failed_csv = _csv_bytes([failed_row], columns=tuple(failed_row))
    assert exchange.reconcile_receipt_csv(failed_csv, actor="admin")["inserted"] == 1

    accepted_row = {
        **first_template,
        "receipt_attempt_id": "erp-attempt-retry",
        "receipt_status": "accepted",
        "message": "retry imported",
    }
    accepted_row["signature"] = exchange.compute_receipt_signature(
        accepted_row, secret
    )
    accepted_csv = _csv_bytes([accepted_row], columns=tuple(accepted_row))
    assert exchange.reconcile_receipt_csv(accepted_csv, actor="admin")["inserted"] == 1

    receipts = exchange.list_exchange_receipts("odoo-demo", actor="admin")
    first_receipt = next(
        item for item in receipts if item["operation_id"] == first_template["operation_id"]
    )
    assert first_receipt["receipt_status"] == "accepted"
    assert first_receipt["attempt_count"] == 2
    with sqlite3.connect(exchange_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM erp_exchange_receipt_events"
        ).fetchone()[0] == 2


def test_read_path_rejects_joint_summary_and_event_status_tamper(
    exchange_db, monkeypatch
):
    exchange = _exchange_module()
    from backend.tool_gateway import gateway

    secret = "test-only-receipt-secret-with-at-least-32-bytes"
    monkeypatch.setenv("ERP_EXCHANGE_RECEIPT_HMAC_SECRET", secret)
    monkeypatch.setenv("ERP_EXCHANGE_RECEIPT_KEY_ID", "test-key-v1")
    _stage_one()
    pending, _ = _submit_one()
    assert gateway.approve_action(pending.approval_id, approver="admin").status == "ok"
    template = list(
        csv.DictReader(
            io.StringIO(
                exchange.build_receipt_template_csv(
                    "odoo-demo", actor="admin"
                ).decode("utf-8-sig")
            )
        )
    )[0]
    receipt = {
        **template,
        "receipt_attempt_id": "tamper-status-attempt",
        "receipt_status": "error",
        "message": "external import failed",
    }
    receipt["signature"] = exchange.compute_receipt_signature(receipt, secret)
    exchange.reconcile_receipt_csv(
        _csv_bytes([receipt], columns=tuple(receipt)), actor="admin"
    )

    with sqlite3.connect(exchange_db) as conn:
        conn.execute(
            "UPDATE erp_exchange_receipts SET receipt_status = 'accepted'"
        )
        conn.execute(
            "UPDATE erp_exchange_receipt_events SET receipt_status = 'accepted'"
        )

    record = exchange.get_exchange_record(
        "odoo-demo", "odoo.purchase_order_1001"
    )
    assert record["receipt_status"] == "accepted"
    assert record["receipt_verified"] is False
    assert record["sync_state"] == "unverified_legacy"
    history = exchange.list_exchange_receipts("odoo-demo", actor="admin")
    assert history[0]["receipt_verified"] is False


def test_legacy_unsigned_accepted_receipt_is_quarantined(
    tmp_path, monkeypatch
):
    """A migrated pre-HMAC receipt must never become a verified acknowledgement."""
    db_path = tmp_path / "legacy-erp-exchange.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE erp_exchange_receipts (
                operation_id TEXT PRIMARY KEY,
                source_system TEXT NOT NULL,
                external_id TEXT NOT NULL,
                approval_id TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                receipt_status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                received_at TEXT NOT NULL
            )
            """
        )

    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    secret = "test-only-receipt-secret-with-at-least-32-bytes"
    monkeypatch.setenv("ERP_EXCHANGE_RECEIPT_HMAC_SECRET", secret)
    monkeypatch.setenv("ERP_EXCHANGE_RECEIPT_KEY_ID", "test-key-v1")
    database.init_db()
    database.run_query(
        "UPDATE suppliers SET is_official = 1 WHERE supplier_id = ?",
        ("SUP01",),
        fetch=False,
    )
    exchange = _exchange_module()
    from backend.tool_gateway import gateway

    _stage_one()
    pending, operation_id = _submit_one()
    assert gateway.approve_action(pending.approval_id, approver="admin").status == "ok"
    with sqlite3.connect(db_path) as conn:
        approval = conn.execute(
            "SELECT approval_id, payload_digest FROM pending_approvals "
            "WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO erp_exchange_receipts (
                operation_id, source_system, external_id, approval_id,
                payload_digest, receipt_status, message, received_at
            ) VALUES (?, ?, ?, ?, ?, 'accepted', 'legacy unsigned row', ?)
            """,
            (
                operation_id,
                "odoo-demo",
                "odoo.purchase_order_1001",
                approval[0],
                approval[1],
                "2026-07-20 12:00:00",
            ),
        )

    record = exchange.get_exchange_record(
        "odoo-demo", "odoo.purchase_order_1001"
    )
    assert record["receipt_status"] == "accepted"
    assert record["receipt_verified"] is False
    assert record["sync_state"] == "unverified_legacy"

    history = exchange.list_exchange_receipts("odoo-demo", actor="admin")
    assert len(history) == 1
    assert history[0]["receipt_verified"] is False
    assert history[0]["trust_state"] == "unverified_legacy"

    template = list(
        csv.DictReader(
            io.StringIO(
                exchange.build_receipt_template_csv(
                    "odoo-demo", actor="admin"
                ).decode("utf-8-sig")
            )
        )
    )[0]
    verified = {
        **template,
        "receipt_attempt_id": "legacy-recovery-attempt",
        "receipt_status": "accepted",
        "message": "verified after migration",
    }
    verified["signature"] = exchange.compute_receipt_signature(verified, secret)
    result = exchange.reconcile_receipt_csv(
        _csv_bytes([verified], columns=tuple(verified)), actor="admin"
    )
    assert result["inserted"] == 1

    recovered = exchange.get_exchange_record(
        "odoo-demo", "odoo.purchase_order_1001"
    )
    assert recovered["receipt_verified"] is True
    assert recovered["sync_state"] == "acknowledged"
