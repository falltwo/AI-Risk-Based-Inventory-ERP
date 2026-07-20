"""Contract tests for durable, idempotent PO approval requests."""

import ast
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import inspect
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from backend import database


_PENDING_PHASE3_COLUMNS = {
    "operation_id",
    "payload_digest",
    "resource_version",
    "policy_version",
    "version",
}
_RECEIPT_PHASE3_COLUMNS = {
    "operation_id",
    "approval_id",
    "payload_digest",
    "result",
    "created_at",
}


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Give every test its own real SQLite database."""
    db_path = tmp_path / "erp.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    return db_path


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _insert_pending(
    conn, *, approval_id, operation_id, tool_name="create_purchase_order"
):
    conn.execute(
        """
        INSERT INTO pending_approvals (
            approval_id, tool_name, parameters, requester, status, approver,
            created_at, updated_at, reason, checksum, operation_id,
            payload_digest, resource_version, policy_version, version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            approval_id,
            tool_name,
            "{}",
            "warehouse",
            "pending",
            None,
            "2026-07-20 12:00:00",
            "2026-07-20 12:00:00",
            None,
            f"checksum-{approval_id}",
            operation_id,
            f"digest-{approval_id}",
            "absent",
            "po-approval-v1",
            0,
        ),
    )


def _insert_receipt(conn, *, operation_id, approval_id):
    conn.execute(
        """
        INSERT INTO effect_receipts (
            operation_id, approval_id, payload_digest, result, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            operation_id,
            approval_id,
            f"digest-{operation_id}",
            json.dumps({"status": "committed"}),
            "2026-07-20 12:01:00",
        ),
    )


def _assert_phase3_schema_and_uniqueness(db_path):
    with sqlite3.connect(db_path) as conn:
        pending_columns = _columns(conn, "pending_approvals")
        receipt_columns = _columns(conn, "effect_receipts")

        assert _PENDING_PHASE3_COLUMNS <= pending_columns
        assert _RECEIPT_PHASE3_COLUMNS <= receipt_columns

        _insert_pending(conn, approval_id="approval-1", operation_id="operation-1")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_pending(conn, approval_id="approval-2", operation_id="operation-1")
        conn.rollback()

        _insert_receipt(conn, operation_id="operation-1", approval_id="approval-1")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_receipt(conn, operation_id="operation-1", approval_id="approval-2")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_receipt(conn, operation_id="operation-2", approval_id="approval-1")


def _po_args(po_id="PO-20260720-001"):
    return {
        "po_id": po_id,
        "supplier_id": "SUP01",
        "product_id": "P001",
        "qty": 2,
        "unit_price": 500.0,
        "order_date": "2026-07-20",
        "status": "pending_review",
        "note": "risk-event-42",
    }


def _submit_po(*, operation_id="po-submit-session-1", args=None):
    from backend.tool_gateway import gateway

    submitted_args = args or _po_args()
    database.run_query(
        "UPDATE suppliers SET is_official = 1 WHERE supplier_id = ?",
        (submitted_args["supplier_id"],),
        fetch=False,
    )
    return gateway.call(
        "create_purchase_order",
        submitted_args,
        role="warehouse",
        agent_name="procurement_agent",
        operation_id=operation_id,
    )


def _effect_counts(db_path):
    with sqlite3.connect(db_path) as conn:
        return {
            "purchase_orders": conn.execute(
                "SELECT COUNT(*) FROM purchase_orders"
            ).fetchone()[0],
            "purchase_order_items": conn.execute(
                "SELECT COUNT(*) FROM purchase_order_items"
            ).fetchone()[0],
            "effect_receipts": conn.execute(
                "SELECT COUNT(*) FROM effect_receipts"
            ).fetchone()[0],
        }


def _approval_state(db_path, approval_id):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT status, version, approver FROM pending_approvals "
            "WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()


def _assert_pending_without_effect(db_path, approval_id):
    assert _approval_state(db_path, approval_id) == ("pending", 0, None)
    assert _effect_counts(db_path) == {
        "purchase_orders": 0,
        "purchase_order_items": 0,
        "effect_receipts": 0,
    }


def _load_line_access_module():
    module_path = Path(__file__).resolve().parents[1] / "line bot" / "line_access.py"
    spec = importlib.util.spec_from_file_location(
        "line_access_for_po_contract", module_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fresh_schema_has_phase3_fields_and_unique_constraints(isolated_db):
    database.init_db()

    _assert_phase3_schema_and_uniqueness(isolated_db)


def test_legacy_schema_migrates_without_losing_pending_requests(isolated_db):
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            """
            CREATE TABLE pending_approvals (
                approval_id TEXT PRIMARY KEY,
                tool_name TEXT,
                parameters TEXT,
                requester TEXT,
                status TEXT DEFAULT 'pending',
                approver TEXT,
                created_at TEXT,
                updated_at TEXT,
                reason TEXT,
                checksum TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO pending_approvals (
                approval_id, tool_name, parameters, requester, status,
                created_at, updated_at, checksum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-approval",
                "update_inventory",
                '{"product_id":"P001","qty":1}',
                "warehouse",
                "pending",
                "2026-07-19 12:00:00",
                "2026-07-19 12:00:00",
                "legacy-checksum",
            ),
        )

    database.init_db()

    with sqlite3.connect(isolated_db) as conn:
        legacy = conn.execute(
            "SELECT tool_name, parameters, status FROM pending_approvals "
            "WHERE approval_id = 'legacy-approval'"
        ).fetchone()
    assert legacy == (
        "update_inventory",
        '{"product_id":"P001","qty":1}',
        "pending",
    )
    _assert_phase3_schema_and_uniqueness(isolated_db)


def test_same_operation_id_returns_the_same_pending_po_approval(isolated_db):
    from backend.agent_logger import create_pending_approval

    database.init_db()
    operation_id = "po-submit-session-1"
    first_args = {
        "po_id": "PO-20260720-001",
        "supplier_id": "SUP01",
        "product_id": "P001",
        "qty": 2,
        "unit_price": 500.0,
        "order_date": "2026-07-20",
        "status": "pending_review",
        "note": "risk-event-42",
    }
    same_args_in_different_order = dict(reversed(list(first_args.items())))

    first_id = create_pending_approval(
        "create_purchase_order",
        first_args,
        "warehouse",
        operation_id=operation_id,
        resource_version="absent",
    )
    second_id = create_pending_approval(
        "create_purchase_order",
        same_args_in_different_order,
        "warehouse",
        operation_id=operation_id,
        resource_version="absent",
    )

    assert second_id == first_id
    with sqlite3.connect(isolated_db) as conn:
        rows = conn.execute(
            """
            SELECT approval_id, operation_id, payload_digest, resource_version,
                   policy_version, version
            FROM pending_approvals
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == first_id
    assert rows[0][1] == operation_id
    assert rows[0][2]
    assert rows[0][3:] == ("absent", "po-approval-v1", 0)


def test_conditional_approval_transition_allows_only_one_winner(isolated_db):
    from backend.agent_logger import transition_approval_status

    database.init_db()
    with sqlite3.connect(isolated_db) as conn:
        _insert_pending(
            conn,
            approval_id="approval-race",
            operation_id="operation-race",
            tool_name="update_inventory",
        )

    approved = transition_approval_status(
        "approval-race",
        expected_status="pending",
        expected_version=0,
        new_status="approved",
        approver="alice",
    )
    rejected = transition_approval_status(
        "approval-race",
        expected_status="pending",
        expected_version=0,
        new_status="rejected",
        approver="bob",
        reason="stale competing decision",
    )

    assert approved is True
    assert rejected is False
    with sqlite3.connect(isolated_db) as conn:
        state = conn.execute(
            """
            SELECT status, version, approver, reason
            FROM pending_approvals
            WHERE approval_id = 'approval-race'
            """
        ).fetchone()
    assert state == ("approved", 1, "alice", None)


def test_canonical_digest_covers_every_effectful_value():
    from backend.tool_gateway import canonical_payload_digest

    base_args = {
        "po_id": "PO-20260720-001",
        "supplier_id": "SUP01",
        "product_id": "P001",
        "qty": 2,
        "unit_price": 500.0,
        "order_date": "2026-07-20",
        "status": "pending_review",
        "note": "risk-event-42",
    }
    digest_kwargs = {
        "tool_name": "create_purchase_order",
        "args": base_args,
        "resource_version": "absent",
        "policy_version": "po-approval-v1",
    }
    baseline = canonical_payload_digest(**digest_kwargs)

    reordered_args = dict(reversed(list(base_args.items())))
    assert canonical_payload_digest(**{**digest_kwargs, "args": reordered_args}) == baseline

    variants = {
        "tool_name": {**digest_kwargs, "tool_name": "create_order"},
        "po_id": {**digest_kwargs, "args": {**base_args, "po_id": "PO-20260720-002"}},
        "supplier_id": {**digest_kwargs, "args": {**base_args, "supplier_id": "SUP02"}},
        "product_id": {**digest_kwargs, "args": {**base_args, "product_id": "P002"}},
        "qty": {**digest_kwargs, "args": {**base_args, "qty": 3}},
        "unit_price": {**digest_kwargs, "args": {**base_args, "unit_price": 501.0}},
        "order_date": {**digest_kwargs, "args": {**base_args, "order_date": "2026-07-21"}},
        "status": {**digest_kwargs, "args": {**base_args, "status": "draft"}},
        "note": {**digest_kwargs, "args": {**base_args, "note": "risk-event-43"}},
        "resource_version": {**digest_kwargs, "resource_version": "present:v1"},
        "policy_version": {**digest_kwargs, "policy_version": "po-approval-v2"},
    }
    ignored = [
        name
        for name, variant in variants.items()
        if canonical_payload_digest(**variant) == baseline
    ]
    assert ignored == [], f"digest ignored effectful values: {ignored}"


def test_po_tool_is_registered_as_governed_write_and_hidden_from_line():
    from backend import ALL_TOOLS, tools_mapping
    from backend.agent_registry import AGENTS, get_tools_for_agent
    from backend.tool_registry import registry

    info = registry.get_tool_info("create_purchase_order")
    assert info is not None
    assert info == {
        "module": "procurement",
        "risk_level": "write",
        "allowed_roles": ["admin", "warehouse"],
        "description": info["description"],
    }
    assert callable(tools_mapping["create_purchase_order"])
    assert "create_purchase_order" in {
        tool.__name__ for tool in ALL_TOOLS
    }
    assert AGENTS["procurement_agent"]["can_write"] is True
    assert "create_purchase_order" in get_tools_for_agent("procurement_agent")

    line_access = _load_line_access_module()
    exposed_to_line = line_access.build_line_tools(
        ALL_TOOLS, registry, role="warehouse"
    )
    assert "create_purchase_order" not in {
        tool.__name__ for tool in exposed_to_line
    }


def test_purchase_orders_have_partial_unique_operation_link(isolated_db):
    database.init_db()

    with sqlite3.connect(isolated_db) as conn:
        assert "operation_id" in _columns(conn, "purchase_orders")

        # Legacy rows remain valid because a partial unique index permits NULL.
        conn.execute(
            """
            INSERT INTO purchase_orders (
                po_id, supplier_id, order_date, status, total_amount, note,
                operation_id
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            ("PO-LEGACY-1", "SUP01", "2026-07-19", "legacy", 1.0, "legacy"),
        )
        conn.execute(
            """
            INSERT INTO purchase_orders (
                po_id, supplier_id, order_date, status, total_amount, note,
                operation_id
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            ("PO-LEGACY-2", "SUP01", "2026-07-19", "legacy", 1.0, "legacy"),
        )
        conn.execute(
            """
            INSERT INTO purchase_orders (
                po_id, supplier_id, order_date, status, total_amount, note,
                operation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "PO-OP-1",
                "SUP01",
                "2026-07-20",
                "approved",
                1.0,
                "linked",
                "operation-unique",
            ),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO purchase_orders (
                    po_id, supplier_id, order_date, status, total_amount, note,
                    operation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "PO-OP-2",
                    "SUP01",
                    "2026-07-20",
                    "approved",
                    1.0,
                    "duplicate operation",
                    "operation-unique",
                ),
            )


def test_gateway_submission_creates_only_a_pending_request(isolated_db):
    database.init_db()

    result = _submit_po()

    assert result.status == "pending"
    assert result.approval_id
    _assert_pending_without_effect(isolated_db, result.approval_id)


def test_gateway_replay_is_stable_and_payload_tamper_fails_closed(isolated_db):
    database.init_db()
    operation_id = "stable-submit-operation"
    args = _po_args()

    first = _submit_po(operation_id=operation_id, args=args)
    replay = _submit_po(
        operation_id=operation_id,
        args=dict(reversed(list(args.items()))),
    )
    tampered = _submit_po(
        operation_id=operation_id,
        args={**args, "qty": args["qty"] + 1},
    )

    assert first.status == replay.status == "pending"
    assert replay.approval_id == first.approval_id
    assert tampered.status == "error"
    with sqlite3.connect(isolated_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()[0] == 1
    _assert_pending_without_effect(isolated_db, first.approval_id)


def test_approval_commits_po_item_receipt_and_final_state_once(isolated_db):
    from backend.tool_gateway import gateway

    database.init_db()
    operation_id = "approved-operation"
    pending = _submit_po(operation_id=operation_id)

    approved = gateway.approve_action(pending.approval_id, approver="admin")
    replay = gateway.approve_action(pending.approval_id, approver="admin")

    assert approved.status == replay.status == "ok"
    assert approved.data == replay.data
    assert approved.data["po_id"] == _po_args()["po_id"]
    assert approved.data["operation_id"] == operation_id
    with sqlite3.connect(isolated_db) as conn:
        header = conn.execute(
            """
            SELECT po_id, supplier_id, total_amount, operation_id
            FROM purchase_orders
            """
        ).fetchone()
        item = conn.execute(
            "SELECT po_id, product_id, qty, unit_price FROM purchase_order_items"
        ).fetchone()
        receipt = conn.execute(
            """
            SELECT operation_id, approval_id, payload_digest, result
            FROM effect_receipts
            """
        ).fetchone()
        approval = conn.execute(
            """
            SELECT status, approver, version, payload_digest
            FROM pending_approvals WHERE approval_id = ?
            """,
            (pending.approval_id,),
        ).fetchone()

    assert header == (
        _po_args()["po_id"],
        _po_args()["supplier_id"],
        _po_args()["qty"] * _po_args()["unit_price"],
        operation_id,
    )
    assert item == (
        _po_args()["po_id"],
        _po_args()["product_id"],
        _po_args()["qty"],
        _po_args()["unit_price"],
    )
    assert receipt[:2] == (operation_id, pending.approval_id)
    assert receipt[2] == approval[3]
    assert json.loads(receipt[3]) == approved.data
    assert approval[:2] == ("approved", "admin")
    assert approval[2] >= 1
    assert _effect_counts(isolated_db) == {
        "purchase_orders": 1,
        "purchase_order_items": 1,
        "effect_receipts": 1,
    }


def test_concurrent_approvals_share_one_committed_effect(isolated_db):
    from backend.tool_gateway import gateway

    database.init_db()
    pending = _submit_po(operation_id="approve-approve-race")
    barrier = threading.Barrier(2)

    def approve_at_once():
        barrier.wait()
        return gateway.approve_action(pending.approval_id, approver="admin")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve_at_once(), range(2)))

    assert [result.status for result in results] == ["ok", "ok"]
    assert results[0].data == results[1].data
    assert _effect_counts(isolated_db) == {
        "purchase_orders": 1,
        "purchase_order_items": 1,
        "effect_receipts": 1,
    }


@pytest.mark.parametrize(
    ("approver", "revoke_admin"),
    [("wh1", False), ("missing-user", False), ("admin", True)],
    ids=["non-admin", "missing", "role-revoked-after-submit"],
)
def test_approval_rechecks_current_admin_authorization(
    isolated_db, approver, revoke_admin
):
    from backend.tool_gateway import gateway

    database.init_db()
    pending = _submit_po(operation_id=f"auth-{approver}-{revoke_admin}")
    if revoke_admin:
        with sqlite3.connect(isolated_db) as conn:
            conn.execute(
                "UPDATE users SET role = 'warehouse' WHERE username = 'admin'"
            )

    result = gateway.approve_action(pending.approval_id, approver=approver)

    assert result.status in {"denied", "error"}
    _assert_pending_without_effect(isolated_db, pending.approval_id)


@pytest.mark.parametrize(
    "tamper_sql",
    [
        "UPDATE pending_approvals SET payload_digest = 'bad' WHERE approval_id = ?",
        "UPDATE pending_approvals SET resource_version = 'present:v9' WHERE approval_id = ?",
        "UPDATE pending_approvals SET policy_version = 'po-approval-v999' WHERE approval_id = ?",
        "UPDATE pending_approvals SET parameters = '{\"po_id\":\"PO-TAMPERED\"}' WHERE approval_id = ?",
    ],
    ids=[
        "payload-digest",
        "resource-version",
        "policy-version",
        "stored-parameters",
    ],
)
def test_approval_rejects_tampered_binding(isolated_db, tamper_sql):
    from backend.tool_gateway import gateway

    database.init_db()
    pending = _submit_po(operation_id="tamper-binding")
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(tamper_sql, (pending.approval_id,))

    result = gateway.approve_action(pending.approval_id, approver="admin")

    assert result.status == "error"
    _assert_pending_without_effect(isolated_db, pending.approval_id)


def test_absent_resource_version_rejects_a_stale_po(isolated_db):
    from backend.tool_gateway import gateway

    database.init_db()
    args = _po_args(po_id="PO-STALE")
    pending = _submit_po(operation_id="stale-operation", args=args)
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            """
            INSERT INTO purchase_orders (
                po_id, supplier_id, order_date, status, total_amount, note
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (args["po_id"], "SUP02", "2026-07-19", "existing", 7.0, "pre-existing"),
        )

    result = gateway.approve_action(pending.approval_id, approver="admin")

    assert result.status == "error"
    with sqlite3.connect(isolated_db) as conn:
        assert conn.execute(
            "SELECT supplier_id, status, total_amount, note FROM purchase_orders"
        ).fetchone() == ("SUP02", "existing", 7.0, "pre-existing")
        assert conn.execute(
            "SELECT COUNT(*) FROM purchase_order_items"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM effect_receipts"
        ).fetchone()[0] == 0
    assert _approval_state(isolated_db, pending.approval_id) == ("pending", 0, None)


@pytest.mark.parametrize(
    ("trigger_table", "trigger_timing", "trigger_event", "trigger_when"),
    [
        ("purchase_order_items", "BEFORE", "INSERT", ""),
        ("effect_receipts", "BEFORE", "INSERT", ""),
        ("agent_action_logs", "BEFORE", "INSERT", ""),
        (
            "pending_approvals",
            "BEFORE",
            "UPDATE",
            "WHEN NEW.status = 'approved'",
        ),
    ],
    ids=["item-insert", "receipt-insert", "action-log", "final-status"],
)
def test_approval_fault_rolls_back_every_effect(
    isolated_db, trigger_table, trigger_timing, trigger_event, trigger_when
):
    from backend.tool_gateway import gateway

    database.init_db()
    pending = _submit_po(operation_id=f"fault-{trigger_table}-{trigger_event}")
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER injected_failure
            {trigger_timing} {trigger_event} ON {trigger_table}
            {trigger_when}
            BEGIN
                SELECT RAISE(ABORT, 'injected approval failure');
            END
            """
        )

    result = gateway.approve_action(pending.approval_id, approver="admin")

    assert result.status == "error"
    _assert_pending_without_effect(isolated_db, pending.approval_id)


def test_concurrent_approve_and_reject_have_one_decision(isolated_db):
    from backend.tool_gateway import gateway

    database.init_db()
    pending = _submit_po(operation_id="approve-reject-race")
    barrier = threading.Barrier(2)

    def approve_at_once():
        barrier.wait()
        return gateway.approve_action(pending.approval_id, approver="admin")

    def reject_at_once():
        barrier.wait()
        return gateway.reject_action(
            pending.approval_id,
            reason="competing decision",
            approver="admin",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        approve_future = pool.submit(approve_at_once)
        reject_future = pool.submit(reject_at_once)
        approved_result = approve_future.result()
        rejected_result = reject_future.result()

    final_status = _approval_state(isolated_db, pending.approval_id)[0]
    assert final_status in {"approved", "rejected"}
    if final_status == "approved":
        assert approved_result.status == "ok"
        assert _effect_counts(isolated_db) == {
            "purchase_orders": 1,
            "purchase_order_items": 1,
            "effect_receipts": 1,
        }
    else:
        assert rejected_result.status == "denied"
        assert approved_result.status != "ok"
        assert _effect_counts(isolated_db) == {
            "purchase_orders": 0,
            "purchase_order_items": 0,
            "effect_receipts": 0,
        }


def test_protected_po_cannot_bypass_approval_context(isolated_db):
    from backend import procurement
    from backend.tool_gateway import gateway

    database.init_db()
    args = _po_args(po_id="PO-BYPASS")

    assert hasattr(procurement, "create_purchase_order")
    create_purchase_order = procurement.create_purchase_order

    bypass = gateway.execute_approved(
        "create_purchase_order", args, role="admin"
    )
    assert bypass.status in {"denied", "error"}
    with pytest.raises(PermissionError):
        create_purchase_order(**args)
    assert _effect_counts(isolated_db) == {
        "purchase_orders": 0,
        "purchase_order_items": 0,
        "effect_receipts": 0,
    }


def test_internal_sentinel_still_requires_an_executing_approval(isolated_db):
    from backend.procurement import (
        _PO_APPROVAL_CONTEXT,
        create_purchase_order,
    )

    database.init_db()
    with database.transaction(immediate=True) as conn:
        with pytest.raises(PermissionError):
            create_purchase_order(
                **_po_args(po_id="PO-NO-EXECUTING-APPROVAL"),
                _conn=conn,
                _operation_id="no-executing-approval",
                _approval_context=_PO_APPROVAL_CONTEXT,
            )


def test_concurrent_submissions_reuse_one_pending_approval(isolated_db):
    database.init_db()
    operation_id = "submit-submit-race"
    barrier = threading.Barrier(2)

    def submit_at_once():
        barrier.wait()
        return _submit_po(operation_id=operation_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit_at_once(), range(2)))

    assert [result.status for result in results] == ["pending", "pending"]
    assert results[0].approval_id == results[1].approval_id
    with sqlite3.connect(isolated_db) as conn:
        rows = conn.execute(
            "SELECT approval_id FROM pending_approvals WHERE operation_id = ?",
            (operation_id,),
        ).fetchall()
    assert rows == [(results[0].approval_id,)]
    _assert_pending_without_effect(isolated_db, results[0].approval_id)


def test_po_operation_id_stays_stable_across_ui_reruns():
    from frontend.page_procurement import ensure_po_operation_id

    state = {}
    first = ensure_po_operation_id(state)
    state_after_first_render = dict(state)
    second = ensure_po_operation_id(state)

    assert first
    assert second == first
    assert state == state_after_first_render


def test_new_po_action_rotates_operation_id_and_clears_last_approval():
    from frontend.page_procurement import (
        ensure_po_operation_id,
        start_new_po_operation,
    )

    state = {}
    previous = ensure_po_operation_id(state)
    state["po_last_approval_id"] = "PENDING-previous"

    current = start_new_po_operation(state)

    assert current
    assert current != previous
    assert ensure_po_operation_id(state) == current
    assert state.get("po_last_approval_id") is None


def test_ui_sources_use_gateway_operation_id_and_current_approver():
    procurement_source = (
        Path(__file__).resolve().parents[1] / "frontend" / "page_procurement.py"
    ).read_text(encoding="utf-8")
    normalized = " ".join(procurement_source.lower().split())

    assert "insert into purchase_orders" not in normalized
    assert "insert into purchase_order_items" not in normalized
    assert "gateway.call(" in procurement_source
    assert "operation_id=" in procurement_source

    dashboard_source = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "page_agent_dashboard.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(dashboard_source)

    decision_calls = {"approve_action": [], "reject_action": []}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in decision_calls:
            continue
        decision_calls[node.func.id].append(node)

    for function_name, calls in decision_calls.items():
        assert calls, f"dashboard does not call {function_name}"
        for call in calls:
            approver_values = [
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "approver"
            ]
            assert approver_values, f"{function_name} omits the current username"
            assert all(
                not (
                    isinstance(value, ast.Constant)
                    and value.value == "admin"
                )
                for value in approver_values
            ), f"{function_name} hard-codes admin"

    assert "session_state" in dashboard_source
    assert "username" in dashboard_source


def test_protected_decisions_require_an_explicit_current_actor(isolated_db):
    from backend import agent_logger
    from backend.tool_gateway import gateway

    database.init_db()
    pending = _submit_po(operation_id="explicit-actor")

    assert (
        inspect.signature(gateway.approve_action)
        .parameters["approver"]
        .default
        is inspect.Parameter.empty
    )
    assert (
        inspect.signature(gateway.reject_action)
        .parameters["approver"]
        .default
        is inspect.Parameter.empty
    )
    assert (
        inspect.signature(agent_logger.approve_action)
        .parameters["approver"]
        .default
        is inspect.Parameter.empty
    )
    assert (
        inspect.signature(agent_logger.reject_action)
        .parameters["approver"]
        .default
        is inspect.Parameter.empty
    )

    assert gateway.approve_action(
        pending.approval_id, approver=""
    ).status == "denied"
    assert gateway.reject_action(
        pending.approval_id, "blank actor", approver=""
    ).status == "denied"
    _assert_pending_without_effect(isolated_db, pending.approval_id)


def test_legacy_status_helper_cannot_mutate_a_protected_po_approval(isolated_db):
    from backend.agent_logger import (
        transition_approval_status,
        update_approval_status,
    )

    database.init_db()
    pending = _submit_po(operation_id="legacy-helper-bypass")

    assert update_approval_status(
        pending.approval_id, "admin", "approved"
    ) is False
    assert transition_approval_status(
        pending.approval_id,
        expected_status="pending",
        expected_version=0,
        new_status="approved",
        approver="admin",
    ) is False
    _assert_pending_without_effect(isolated_db, pending.approval_id)


def test_terminal_submission_replay_reports_the_committed_state(isolated_db):
    from backend.tool_gateway import gateway

    database.init_db()

    rejected = _submit_po(
        operation_id="terminal-rejected",
        args=_po_args(po_id="PO-REJECTED"),
    )
    gateway.reject_action(
        rejected.approval_id, "not needed", approver="admin"
    )
    rejected_replay = _submit_po(
        operation_id="terminal-rejected",
        args=_po_args(po_id="PO-REJECTED"),
    )
    assert rejected_replay.status == "denied"
    assert rejected_replay.approval_id == rejected.approval_id

    approved = _submit_po(
        operation_id="terminal-approved",
        args=_po_args(po_id="PO-APPROVED"),
    )
    committed = gateway.approve_action(
        approved.approval_id, approver="admin"
    )
    approved_replay = _submit_po(
        operation_id="terminal-approved",
        args=_po_args(po_id="PO-APPROVED"),
    )
    assert approved_replay.status == "ok"
    assert approved_replay.approval_id == approved.approval_id
    assert approved_replay.data == committed.data


def test_agent_tool_call_propagates_a_stable_operation_id(monkeypatch):
    from backend import agent_orchestrator

    captured = []

    def fake_execute(tool_name, args, role, agent_id="", *, operation_id=None):
        captured.append((tool_name, operation_id))
        return {
            "status": "pending",
            "message": "pending",
            "approval_id": "PENDING-agent",
        }

    class FakeMessage:
        def __init__(self, tool_calls=None, content=""):
            self.tool_calls = tool_calls
            self.content = content

    class FakeResponse:
        def __init__(self, message):
            self.choices = [type("Choice", (), {"message": message})()]

    tool_call = type(
        "ToolCall",
        (),
        {
            "id": "call-stable-123",
            "function": type(
                "Function",
                (),
                {
                    "name": "create_purchase_order",
                    "arguments": json.dumps(_po_args()),
                },
            )(),
        },
    )()
    responses = iter(
        [
            FakeResponse(FakeMessage([tool_call])),
            FakeResponse(FakeMessage(None, "done")),
        ]
    )
    monkeypatch.setattr(agent_orchestrator, "_llm", lambda *a, **k: next(responses))
    monkeypatch.setattr(agent_orchestrator, "execute_tool_call", fake_execute)

    agent_orchestrator.run_agent(
        "procurement_agent", "create a PO", "warehouse", max_turns=2
    )

    assert captured == [("create_purchase_order", "agent:call-stable-123")]


def test_procurement_ui_resolves_terminal_approval_state():
    from frontend.page_procurement import resolve_po_approval_state

    state = {"po_last_approval_id": "PENDING-1"}
    assert resolve_po_approval_state(
        state, lambda approval_id: {"status": "approved"}
    ) == ("PENDING-1", "approved")
    assert resolve_po_approval_state(
        state, lambda approval_id: {"status": "rejected"}
    ) == ("PENDING-1", "rejected")
    assert resolve_po_approval_state({}, lambda approval_id: None) == (None, None)


def test_reject_rechecks_current_admin_authorization(isolated_db):
    from backend.tool_gateway import gateway

    database.init_db()
    pending = _submit_po(operation_id="reject-auth")

    result = gateway.reject_action(
        pending.approval_id, "not authorized", approver="wh1"
    )

    assert result.status == "denied"
    _assert_pending_without_effect(isolated_db, pending.approval_id)


def test_purchase_order_migration_preserves_legacy_rows(isolated_db):
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            """
            CREATE TABLE purchase_orders (
                po_id TEXT PRIMARY KEY,
                supplier_id TEXT,
                order_date TEXT,
                status TEXT,
                total_amount REAL,
                note TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?, ?)",
            ("PO-LEGACY", "SUP01", "2026-07-19", "legacy", 10.0, "keep"),
        )

    database.init_db()

    with sqlite3.connect(isolated_db) as conn:
        assert "operation_id" in _columns(conn, "purchase_orders")
        assert conn.execute(
            """
            SELECT po_id, supplier_id, order_date, status, total_amount, note,
                   operation_id
            FROM purchase_orders WHERE po_id = 'PO-LEGACY'
            """
        ).fetchone() == (
            "PO-LEGACY",
            "SUP01",
            "2026-07-19",
            "legacy",
            10.0,
            "keep",
            None,
        )


def test_execution_rechecks_supplier_approval(isolated_db):
    from backend.tool_gateway import gateway

    database.init_db()
    pending = _submit_po(operation_id="supplier-revoked")
    database.run_query(
        "UPDATE suppliers SET is_official = 0 WHERE supplier_id = 'SUP01'",
        fetch=False,
    )

    result = gateway.approve_action(pending.approval_id, approver="admin")

    assert result.status == "error"
    _assert_pending_without_effect(isolated_db, pending.approval_id)


def test_receipt_replay_fails_closed_on_terminal_state_mismatch(isolated_db):
    from backend.tool_gateway import gateway

    database.init_db()
    pending = _submit_po(operation_id="receipt-state-mismatch")
    assert gateway.approve_action(
        pending.approval_id, approver="admin"
    ).status == "ok"
    database.run_query(
        "UPDATE pending_approvals SET status = 'rejected' WHERE approval_id = ?",
        (pending.approval_id,),
        fetch=False,
    )

    replay = gateway.approve_action(pending.approval_id, approver="admin")

    assert replay.status == "error"
