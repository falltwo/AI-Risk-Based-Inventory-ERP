"""Authorization contracts for the L1/L2/L3 demo accounts."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from backend import database

from backend.access_control import (
    APPROVAL_DECIDE,
    APPROVAL_QUEUE_READ,
    ERP_EXCHANGE_EXPORT,
    ERP_EXCHANGE_PROPOSE,
    ERP_EXCHANGE_RECONCILE,
    PROPOSAL_EVIDENCE_READ,
    RISK_ANALYSIS_READ,
    RISK_OVERVIEW_READ,
    RISK_WHAT_IF_RUN,
    capabilities_for_role,
    has_capability,
    load_principal,
    require_capability,
)


@pytest.fixture
def tier_db(tmp_path, monkeypatch):
    db_path = tmp_path / "tier-access.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    database.init_db()
    return db_path


def test_known_demo_accounts_require_explicit_demo_mode(tmp_path, monkeypatch):
    from backend.auth import check_login

    db_path = tmp_path / "production-default.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    monkeypatch.delenv("ERP_DEMO_MODE", raising=False)

    database.init_db()

    assert database.run_query("SELECT COUNT(*) FROM users")[0][0] == 0
    assert database.run_query("SELECT COUNT(*) FROM user_organizations")[0][0] == 0
    assert database.run_query("SELECT COUNT(*) FROM organization_entitlements")[0][0] == 0
    assert check_login("viewer", "viewer") is None
    assert check_login("admin", "admin") is None


def test_runtime_init_does_not_restore_revoked_access(tier_db):
    database.run_query(
        "DELETE FROM user_organizations WHERE username = 'planner'", fetch=False
    )
    database.run_query(
        "DELETE FROM organization_entitlements "
        "WHERE organization_id = 'demo-org' AND entitlement_key = 'l3_governed_action'",
        fetch=False,
    )

    database.init_db()

    assert load_principal("planner") is None
    assert database.run_query(
        "SELECT COUNT(*) FROM organization_entitlements "
        "WHERE organization_id = 'demo-org' AND entitlement_key = 'l3_governed_action'"
    )[0][0] == 0


def test_demo_seed_does_not_grant_existing_unrelated_users(tier_db, monkeypatch):
    from backend.passwords import hash_password

    database.run_query(
        "INSERT INTO users (username, password, role, name) VALUES (?, ?, ?, ?)",
        ("existing-user", hash_password("private"), "sales", "既有使用者"),
        fetch=False,
    )
    database.run_query(
        "DELETE FROM app_metadata WHERE key = 'tier_demo_seed_v1'", fetch=False
    )
    database.run_query("DELETE FROM user_organizations", fetch=False)
    monkeypatch.setenv("ERP_DEMO_MODE", "1")

    database.init_db()

    assert database.run_query(
        "SELECT COUNT(*) FROM user_organizations WHERE username = 'existing-user'"
    )[0][0] == 0


def test_demo_roles_have_context_visibility_without_inheriting_actions():
    viewer = capabilities_for_role("risk_viewer")
    planner = capabilities_for_role("supply_planner")
    approver = capabilities_for_role("procurement_approver")

    assert viewer == {RISK_OVERVIEW_READ}

    assert {RISK_OVERVIEW_READ, RISK_ANALYSIS_READ, RISK_WHAT_IF_RUN} <= planner
    assert ERP_EXCHANGE_PROPOSE in planner
    assert APPROVAL_DECIDE not in planner
    assert ERP_EXCHANGE_EXPORT not in planner

    assert RISK_OVERVIEW_READ in approver
    assert PROPOSAL_EVIDENCE_READ in approver
    assert APPROVAL_QUEUE_READ in approver
    assert APPROVAL_DECIDE in approver
    assert ERP_EXCHANGE_EXPORT in approver
    assert ERP_EXCHANGE_RECONCILE in approver
    assert RISK_ANALYSIS_READ not in approver
    assert RISK_WHAT_IF_RUN not in approver
    assert ERP_EXCHANGE_PROPOSE not in approver

    warehouse = capabilities_for_role("warehouse")
    assert APPROVAL_QUEUE_READ in warehouse
    assert APPROVAL_DECIDE not in warehouse


def test_demo_accounts_are_seeded_with_live_entitlements(tier_db):
    from backend.auth import check_login

    viewer = load_principal("viewer")
    planner = load_principal("planner")
    approver = load_principal("approver")

    assert viewer is not None and viewer.role == "risk_viewer"
    assert planner is not None and planner.role == "supply_planner"
    assert approver is not None and approver.role == "procurement_approver"

    assert viewer.can(RISK_OVERVIEW_READ)
    assert not viewer.can(RISK_ANALYSIS_READ)
    assert planner.can(RISK_ANALYSIS_READ)
    assert planner.can(ERP_EXCHANGE_PROPOSE)
    assert approver.can(APPROVAL_DECIDE)
    assert approver.can(ERP_EXCHANGE_EXPORT)

    with sqlite3.connect(tier_db) as conn:
        memberships = dict(
            conn.execute(
                "SELECT username, organization_id FROM user_organizations "
                "WHERE username IN ('viewer', 'planner', 'approver')"
            )
        )
        entitlements = {
            row[0]
            for row in conn.execute(
                "SELECT entitlement_key FROM organization_entitlements "
                "WHERE organization_id = 'demo-org' AND enabled = 1"
            )
        }
    assert memberships == {
        "viewer": "demo-org",
        "planner": "demo-org",
        "approver": "demo-org",
    }
    assert entitlements == {"l1_monitor", "l2_decision", "l3_governed_action"}
    assert check_login("viewer", "viewer")["role"] == "risk_viewer"
    assert check_login("planner", "planner")["role"] == "supply_planner"
    assert check_login("approver", "approver")["role"] == "procurement_approver"

    stored_passwords = {
        row[0]: row[1]
        for row in database.run_query(
            "SELECT username, password FROM users "
            "WHERE username IN ('viewer', 'planner', 'approver')"
        )
    }
    assert all(stored_passwords[user] != user for user in stored_passwords)


def test_live_entitlement_and_role_changes_fail_closed(tier_db):
    assert has_capability("planner", RISK_WHAT_IF_RUN)

    database.run_query(
        "UPDATE organization_entitlements SET enabled = 0 "
        "WHERE organization_id = 'demo-org' AND entitlement_key = 'l2_decision'",
        fetch=False,
    )
    assert not has_capability("planner", RISK_WHAT_IF_RUN)
    with pytest.raises(PermissionError, match="權限"):
        require_capability("planner", RISK_WHAT_IF_RUN)

    database.run_query(
        "UPDATE users SET role = 'unknown_role' WHERE username = 'approver'",
        fetch=False,
    )
    assert not has_capability("approver", APPROVAL_DECIDE)
    assert not has_capability("missing-user", RISK_OVERVIEW_READ)


@pytest.mark.parametrize("actor", [None, "viewer"])
def test_protected_write_requires_live_actor_matching_claimed_role(
    tier_db, actor
):
    from backend.tool_gateway import gateway

    result = gateway.call(
        "create_purchase_order",
        {
            "po_id": f"PO-ACTOR-{actor or 'missing'}",
            "supplier_id": "SUP01",
            "product_id": "P001",
            "qty": 2,
            "unit_price": 500.0,
            "order_date": "2026-07-20",
            "status": "pending_review",
            "note": "authorization test",
        },
        role="admin",
        actor=actor,
        agent_name="procurement_agent",
        operation_id=f"actor-check-{actor or 'missing'}",
    )

    assert result.status == "denied"
    assert database.run_query("SELECT COUNT(*) FROM pending_approvals")[0][0] == 0


@pytest.mark.parametrize(
    ("actor", "role"),
    [
        (None, "warehouse"),
        ("missing-user", "warehouse"),
        ("wh1", "admin"),
    ],
)
def test_generic_write_requires_live_actor_matching_claimed_role(
    tier_db, actor, role
):
    from backend.tool_gateway import gateway

    result = gateway.call(
        "update_inventory",
        {"product_id": "P001", "quantity_change": 1},
        role=role,
        actor=actor,
        agent_name="inventory_agent",
        operation_id=f"generic-actor-check-{actor or 'missing'}-{role}",
    )

    assert result.status == "denied"
    assert database.run_query("SELECT COUNT(*) FROM pending_approvals")[0][0] == 0


def test_generic_write_persists_canonical_actor(tier_db):
    from backend.tool_gateway import gateway

    pending = gateway.call(
        "update_inventory",
        {"product_id": "P001", "quantity_change": 1},
        role="warehouse",
        actor=" wh1 ",
        agent_name="inventory_agent",
        operation_id="generic-canonical-actor",
    )

    assert pending.status == "pending"
    assert database.run_query(
        "SELECT requester_username FROM pending_approvals WHERE approval_id = ?",
        (pending.approval_id,),
    )[0][0] == "wh1"


def test_originator_is_recorded_and_cannot_approve_own_proposal(tier_db):
    from backend.tool_gateway import gateway

    database.run_query(
        "UPDATE suppliers SET is_official = 1 WHERE supplier_id = 'SUP01'",
        fetch=False,
    )
    pending = gateway.call(
        "create_purchase_order",
        {
            "po_id": "PO-SELF-APPROVAL",
            "supplier_id": "SUP01",
            "product_id": "P001",
            "qty": 2,
            "unit_price": 500.0,
            "order_date": "2026-07-20",
            "status": "pending_review",
            "note": "self approval test",
        },
        role="admin",
        actor="admin",
        agent_name="procurement_agent",
        operation_id="self-approval-operation",
    )
    assert pending.status == "pending"
    requester = database.run_query(
        "SELECT requester_username FROM pending_approvals WHERE approval_id = ?",
        (pending.approval_id,),
    )[0][0]
    assert requester == "admin"

    for submitted_identity in ("admin", " admin "):
        denied = gateway.approve_action(
            pending.approval_id, approver=submitted_identity
        )
        assert denied.status == "denied"
    self_reject = gateway.reject_action(
        pending.approval_id, "self reject bypass", approver=" admin "
    )
    assert self_reject.status == "denied"
    assert database.run_query("SELECT status FROM pending_approvals")[0][0] == "pending"
    assert database.run_query("SELECT COUNT(*) FROM purchase_orders")[0][0] == 0

    approved = gateway.approve_action(
        pending.approval_id, approver=" approver "
    )
    assert approved.status == "ok"
    assert database.run_query("SELECT COUNT(*) FROM purchase_orders")[0][0] == 1
    assert database.run_query(
        "SELECT approver FROM pending_approvals WHERE approval_id = ?",
        (pending.approval_id,),
    )[0][0] == "approver"


def test_protected_operation_replay_is_bound_to_original_requester(tier_db):
    from backend.passwords import hash_password
    from backend.tool_gateway import gateway

    database.run_query(
        "UPDATE suppliers SET is_official = 1 WHERE supplier_id = 'SUP01'",
        fetch=False,
    )
    args = {
        "po_id": "PO-ORIGIN-BOUND",
        "supplier_id": "SUP01",
        "product_id": "P001",
        "qty": 2,
        "unit_price": 500.0,
        "order_date": "2026-07-20",
        "status": "pending_review",
        "note": "origin binding test",
    }
    first = gateway.call(
        "create_purchase_order",
        args,
        role="admin",
        actor="admin",
        agent_name="procurement_agent",
        operation_id="origin-bound-operation",
    )
    assert first.status == "pending"

    database.run_query(
        "INSERT INTO users (username, password, role, name) VALUES (?, ?, ?, ?)",
        ("admin2", hash_password("admin2"), "admin", "第二管理員"),
        fetch=False,
    )
    database.run_query(
        "INSERT INTO user_organizations (username, organization_id) "
        "VALUES ('admin2', 'demo-org')",
        fetch=False,
    )

    replay = gateway.call(
        "create_purchase_order",
        args,
        role="admin",
        actor="admin2",
        agent_name="procurement_agent",
        operation_id="origin-bound-operation",
    )

    assert replay.status == "denied"
    assert database.run_query(
        "SELECT requester_username FROM pending_approvals WHERE approval_id = ?",
        (first.approval_id,),
    )[0][0] == "admin"


def _exchange_row(**overrides):
    row = {
        "external_id": "odoo.purchase_order_tier_1",
        "po_id": "EXT-TIER-PO-1",
        "supplier_id": "SUP01",
        "product_id": "P001",
        "qty": 2,
        "unit_price": 500.0,
        "order_date": "2026-07-20",
        "status": "pending_review",
        "note": "tier authorization contract",
    }
    row.update(overrides)
    return row


def test_exchange_staging_is_planner_only_and_denies_before_writing(tier_db):
    from backend import erp_exchange

    database.run_query(
        "UPDATE suppliers SET is_official = 1 WHERE supplier_id = 'SUP01'",
        fetch=False,
    )

    with pytest.raises(PermissionError):
        erp_exchange.stage_purchase_order_rows(
            "odoo-demo", [_exchange_row()], actor="viewer"
        )
    assert database.run_query("SELECT COUNT(*) FROM erp_exchange_records")[0][0] == 0

    summary = erp_exchange.stage_purchase_order_rows(
        "odoo-demo", [_exchange_row()], actor="planner"
    )
    assert summary["inserted"] == 1

    with pytest.raises(PermissionError):
        erp_exchange.stage_purchase_order_rows(
            "odoo-demo",
            [_exchange_row(external_id="second", po_id="EXT-TIER-PO-2")],
            actor="approver",
        )
    assert database.run_query("SELECT COUNT(*) FROM erp_exchange_records")[0][0] == 1


def test_exchange_read_and_l3_actions_use_separate_capabilities(tier_db):
    from backend import erp_exchange

    database.run_query(
        "UPDATE suppliers SET is_official = 1 WHERE supplier_id = 'SUP01'",
        fetch=False,
    )
    erp_exchange.stage_purchase_order_rows(
        "odoo-demo", [_exchange_row()], actor="planner"
    )

    assert len(erp_exchange.list_exchange_records("odoo-demo", actor="planner")) == 1
    assert len(erp_exchange.list_exchange_records("odoo-demo", actor="approver")) == 1
    with pytest.raises(PermissionError):
        erp_exchange.list_exchange_records("odoo-demo", actor="viewer")

    with pytest.raises(PermissionError):
        erp_exchange.export_approved_actions_csv("odoo-demo", actor="planner")
    exported = erp_exchange.export_approved_actions_csv(
        "odoo-demo", actor="approver"
    )
    assert exported.decode("utf-8-sig").startswith("source_system,")

    with pytest.raises(PermissionError):
        erp_exchange.reconcile_receipt_csv(b"not-a-csv", actor="planner")
    assert erp_exchange.list_exchange_receipts(
        "odoo-demo", actor="approver"
    ) == []


def test_exchange_entitlement_revocation_is_effective_immediately(tier_db):
    from backend import erp_exchange

    database.run_query(
        "UPDATE suppliers SET is_official = 1 WHERE supplier_id = 'SUP01'",
        fetch=False,
    )
    database.run_query(
        "UPDATE organization_entitlements SET enabled = 0 "
        "WHERE organization_id = 'demo-org' AND entitlement_key = 'l2_decision'",
        fetch=False,
    )

    with pytest.raises(PermissionError):
        erp_exchange.stage_purchase_order_rows(
            "odoo-demo", [_exchange_row()], actor="planner"
        )
    assert database.run_query("SELECT COUNT(*) FROM erp_exchange_records")[0][0] == 0


@pytest.mark.parametrize("approver", ["viewer", "planner", "approver"])
def test_tier_accounts_cannot_decide_unrelated_global_approvals(
    tier_db, approver
):
    from backend.tool_gateway import gateway

    before = database.run_query(
        "SELECT stock FROM inventory WHERE product_id = 'P001'"
    )[0][0]
    pending = gateway.call(
        "update_inventory",
        {"product_id": "P001", "quantity_change": 1},
        role="warehouse",
        actor="wh1",
        agent_name="inventory_agent",
        operation_id=f"global-approval-{approver}",
    )
    assert pending.status == "pending"

    denied = gateway.approve_action(pending.approval_id, approver=approver)

    assert denied.status == "denied"
    assert database.run_query(
        "SELECT stock FROM inventory WHERE product_id = 'P001'"
    )[0][0] == before


def test_generic_approval_rejects_known_self_approval(tier_db):
    from backend.tool_gateway import gateway

    before = database.run_query(
        "SELECT stock FROM inventory WHERE product_id = 'P001'"
    )[0][0]
    pending = gateway.call(
        "update_inventory",
        {"product_id": "P001", "quantity_change": 1},
        role="admin",
        actor="admin",
        agent_name="inventory_agent",
        operation_id="generic-self-approval",
    )
    assert pending.status == "pending"

    denied = gateway.approve_action(pending.approval_id, approver=" admin ")

    assert denied.status == "denied"
    rejected = gateway.reject_action(
        pending.approval_id, "self reject", approver=" admin "
    )
    assert rejected.status == "denied"
    assert database.run_query(
        "SELECT status FROM pending_approvals WHERE approval_id = ?",
        (pending.approval_id,),
    )[0][0] == "pending"
    assert database.run_query(
        "SELECT stock FROM inventory WHERE product_id = 'P001'"
    )[0][0] == before


def test_legacy_generic_approval_is_reject_only(tier_db):
    from backend.agent_logger import create_pending_approval
    from backend.tool_gateway import gateway

    approval_id = create_pending_approval(
        "update_inventory",
        {"product_id": "P001", "quantity_change": 1},
        "warehouse",
        requester_username=None,
        operation_id="legacy-generic-no-originator",
    )

    denied = gateway.approve_action(approval_id, approver="admin")
    assert denied.status == "denied"

    rejected = gateway.reject_action(
        approval_id, "重新送審", approver="admin"
    )
    assert rejected.status == "denied"
    row = database.run_query(
        "SELECT status, reason FROM pending_approvals WHERE approval_id = ?",
        (approval_id,),
    )[0]
    assert row[0] == "rejected"
    assert "legacy originator unavailable" in row[1]


def test_generic_approval_claim_allows_only_one_execution(
    tier_db, monkeypatch
):
    from backend import agent_logger
    from backend.tool_gateway import gateway

    before = database.run_query(
        "SELECT stock FROM inventory WHERE product_id = 'P001'"
    )[0][0]
    pending = gateway.call(
        "update_inventory",
        {"product_id": "P001", "quantity_change": 1},
        role="warehouse",
        actor="wh1",
        agent_name="inventory_agent",
        operation_id="generic-concurrent-approval",
    )
    assert pending.status == "pending"

    original_get = agent_logger.get_pending_approval_by_id
    both_loaded_pending = Barrier(2)

    def synchronized_get(approval_id):
        item = original_get(approval_id)
        both_loaded_pending.wait(timeout=5)
        return item

    monkeypatch.setattr(
        agent_logger, "get_pending_approval_by_id", synchronized_get
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: gateway.approve_action(
                    pending.approval_id, approver="admin"
                ),
                range(2),
            )
        )

    assert [result.status for result in results].count("ok") == 1
    assert database.run_query(
        "SELECT stock FROM inventory WHERE product_id = 'P001'"
    )[0][0] == before + 1
    row = database.run_query(
        "SELECT status, version FROM pending_approvals WHERE approval_id = ?",
        (pending.approval_id,),
    )[0]
    assert row == ("approved", 2)
    assert database.run_query(
        "SELECT COUNT(*) FROM effect_receipts WHERE approval_id = ?",
        (pending.approval_id,),
    )[0][0] == 1
