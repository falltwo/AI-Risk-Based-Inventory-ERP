"""Contracts for the L2 proposal -> L3 approval -> Gateway execution flow."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime as real_datetime
import json
import sqlite3

import pytest

from backend import database


@pytest.fixture
def proposal_db(tmp_path, monkeypatch):
    db_path = tmp_path / "purchase-proposals.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    database.init_db()

    with database.transaction() as conn:
        conn.execute(
            "UPDATE suppliers SET is_official = 1 WHERE supplier_id IN ('SUP01', 'SUP02')"
        )
        conn.execute(
            "DELETE FROM supplier_products WHERE supplier_id IN ('SUP01', 'SUP02') "
            "AND product_id = 'P001'"
        )
        conn.execute(
            "INSERT INTO supplier_products (supplier_id, product_id, price, carbon_factor) "
            "VALUES ('SUP01', 'P001', 500, 3.5)"
        )
        conn.execute(
            "INSERT INTO supplier_products (supplier_id, product_id, price, carbon_factor) "
            "VALUES ('SUP02', 'P001', 475, 2.1)"
        )
        conn.execute(
            """
            INSERT INTO purchase_orders (
                po_id, supplier_id, order_date, status, total_amount, note,
                estimated_delay_days, alternative_suggestion
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "PO-RISK-001",
                "SUP01",
                "2026-07-20",
                "待入庫",
                1000.0,
                "affected by port disruption",
                12,
                "switch to SUP02",
            ),
        )
        conn.execute(
            "INSERT INTO purchase_order_items (po_id, product_id, qty, unit_price) "
            "VALUES ('PO-RISK-001', 'P001', 2, 500)"
        )
    return db_path


def _prepare(proposal_id="PROP-TEST-001", *, actor="planner"):
    from backend.purchase_proposals import prepare_alternative_purchase_proposal

    return prepare_alternative_purchase_proposal(
        affected_po_id="PO-RISK-001",
        product_id="P001",
        alternative_supplier_id="SUP02",
        reason="港口中斷，改由低風險正式供應商供貨",
        estimated_delay_days=12,
        actor=actor,
        proposal_id=proposal_id,
    )


def test_fresh_schema_keeps_proposal_separate_from_approval(proposal_db):
    with sqlite3.connect(proposal_db) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(purchase_proposals)")
        }

    assert {
        "proposal_id",
        "proposal_type",
        "schema_version",
        "organization_id",
        "proposer_username",
        "proposer_role",
        "affected_po_id",
        "proposed_po_id",
        "original_supplier_id",
        "alternative_supplier_id",
        "product_id",
        "qty",
        "unit_price",
        "currency",
        "reason",
        "estimated_delay_days",
        "source_po_version",
        "proposal_digest",
        "created_at",
    } <= columns
    assert "operation_id" not in columns
    assert "approval_id" not in columns
    assert "approval_status" not in columns
    assert "tool_name" not in columns


def test_prepare_returns_immutable_domain_object_and_separate_execution_request(
    proposal_db,
):
    from backend.purchase_proposals import (
        ApprovalDecision,
        proposal_to_execution_request,
    )

    proposal = _prepare()
    assert proposal.affected_po_id == "PO-RISK-001"
    assert proposal.original_supplier_id == "SUP01"
    assert proposal.alternative_supplier_id == "SUP02"
    assert proposal.product_id == "P001"
    assert proposal.qty == 2
    assert proposal.unit_price == 475.0
    assert proposal.currency == "TWD"
    assert proposal.source_po_version.startswith("sha256:")
    assert not hasattr(proposal, "operation_id")
    assert not hasattr(proposal, "tool_name")

    with pytest.raises(FrozenInstanceError):
        proposal.qty = 99

    execution = proposal_to_execution_request(proposal)
    assert execution.tool_name == "create_purchase_order"
    assert execution.operation_id == "proposal:create-po:PROP-TEST-001:v1"
    assert execution.args["proposal_id"] == proposal.proposal_id
    assert execution.args["affected_po_id"] == proposal.affected_po_id
    assert execution.args["source_po_version"] == proposal.source_po_version
    assert execution.args["supplier_id"] == "SUP02"
    assert "approval_id" not in execution.args
    assert "approver" not in execution.args
    assert "approval_status" not in execution.args
    assert "reason" not in execution.args
    assert "estimated_delay_days" not in execution.args
    assert "source_event_id" not in execution.args
    assert set(ApprovalDecision.__dataclass_fields__) == {
        "proposal_id",
        "outcome",
        "reason",
    }


@pytest.mark.parametrize("actor", ["viewer", "approver", None])
def test_only_l2_proposer_can_prepare_alternative_proposal(proposal_db, actor):
    with pytest.raises(PermissionError):
        _prepare(actor=actor)


def test_alternative_supplier_must_be_official_and_different(proposal_db):
    from backend.purchase_proposals import prepare_alternative_purchase_proposal

    with pytest.raises(ValueError, match="不同"):
        prepare_alternative_purchase_proposal(
            affected_po_id="PO-RISK-001",
            product_id="P001",
            alternative_supplier_id="SUP01",
            reason="same supplier",
            actor="planner",
            proposal_id="PROP-SAME-SUPPLIER",
        )

    database.run_query(
        "UPDATE suppliers SET is_official = 0 WHERE supplier_id = 'SUP02'",
        fetch=False,
    )
    with pytest.raises(PermissionError, match="正式供應商"):
        _prepare(proposal_id="PROP-INACTIVE-SUPPLIER")


def test_l2_submission_persists_proposal_but_not_live_erp_effect(proposal_db):
    from backend.purchase_proposals import submit_purchase_proposal

    proposal = _prepare()
    result = submit_purchase_proposal(proposal, actor="planner")

    assert result.status == "pending"
    assert result.approval_id
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_proposals WHERE proposal_id = ?",
        (proposal.proposal_id,),
    )[0][0] == 1
    approval = database.run_query(
        "SELECT tool_name, requester_username, parameters, status "
        "FROM pending_approvals WHERE approval_id = ?",
        (result.approval_id,),
    )[0]
    args = json.loads(approval[2])
    assert approval[:2] == ("create_purchase_order", "planner")
    assert approval[3] == "pending"
    assert args["proposal_id"] == proposal.proposal_id
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_orders WHERE po_id = ?",
        (proposal.proposed_po_id,),
    )[0][0] == 0
    assert database.run_query(
        "SELECT COUNT(*) FROM effect_receipts WHERE operation_id = ?",
        ("proposal:create-po:PROP-TEST-001:v1",),
    )[0][0] == 0


def test_submit_rejects_forged_effect_fields_even_with_a_valid_digest(proposal_db):
    from backend.purchase_proposals import _proposal_digest, submit_purchase_proposal

    proposal = _prepare(proposal_id="PROP-FORGED-QTY")
    forged = replace(proposal, qty=proposal.qty + 999, proposal_digest="")
    forged = replace(forged, proposal_digest=_proposal_digest(forged))

    with pytest.raises(PermissionError, match="數量"):
        submit_purchase_proposal(forged, actor="planner")

    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_proposals WHERE proposal_id = ?",
        (proposal.proposal_id,),
    )[0][0] == 0
    assert database.run_query(
        "SELECT COUNT(*) FROM pending_approvals WHERE operation_id = ?",
        ("proposal:create-po:PROP-FORGED-QTY:v1",),
    )[0][0] == 0


def test_same_operation_replays_one_proposal_and_one_approval(proposal_db):
    from backend.purchase_proposals import submit_purchase_proposal

    proposal = _prepare()
    first = submit_purchase_proposal(proposal, actor="planner")
    second = submit_purchase_proposal(proposal, actor="planner")

    assert second.status == "pending"
    assert second.approval_id == first.approval_id
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_proposals WHERE proposal_id = ?",
        (proposal.proposal_id,),
    )[0][0] == 1
    assert database.run_query(
        "SELECT COUNT(*) FROM pending_approvals WHERE operation_id = ?",
        ("proposal:create-po:PROP-TEST-001:v1",),
    )[0][0] == 1


def test_l3_approval_executes_once_and_builds_correlated_timeline(proposal_db):
    from backend.purchase_proposals import (
        ApprovalDecision,
        decide_purchase_proposal,
        get_purchase_operation_timeline,
        proposal_to_execution_request,
        submit_purchase_proposal,
    )

    proposal = _prepare()
    execution = proposal_to_execution_request(proposal)
    pending = submit_purchase_proposal(proposal, actor="planner")
    decision = ApprovalDecision(
        proposal_id=proposal.proposal_id, outcome="approve", reason=""
    )
    approved = decide_purchase_proposal(decision, actor="approver")
    replay = decide_purchase_proposal(decision, actor="approver")

    assert approved.status == "ok"
    assert replay.status == "ok"
    assert database.run_query(
        "SELECT supplier_id, operation_id FROM purchase_orders WHERE po_id = ?",
        (proposal.proposed_po_id,),
    ) == [("SUP02", execution.operation_id)]
    assert database.run_query(
        "SELECT COUNT(*) FROM effect_receipts WHERE operation_id = ?",
        (execution.operation_id,),
    )[0][0] == 1

    timeline = get_purchase_operation_timeline(
        execution.operation_id, actor="approver"
    )
    assert [event["kind"] for event in timeline] == [
        "proposal_created",
        "approval_submitted",
        "execution_completed",
    ]
    assert all(event["operation_id"] == execution.operation_id for event in timeline)
    assert "parameters" not in json.dumps(timeline, ensure_ascii=False)


@pytest.mark.parametrize("tamper_target", ["source_po", "proposal"])
def test_approval_fails_closed_when_source_or_proposal_changes(
    proposal_db, tamper_target
):
    from backend.purchase_proposals import submit_purchase_proposal
    from backend.tool_gateway import gateway

    proposal = _prepare(proposal_id=f"PROP-TAMPER-{tamper_target.upper()}")
    pending = submit_purchase_proposal(proposal, actor="planner")
    from backend.purchase_proposals import proposal_to_execution_request

    execution = proposal_to_execution_request(proposal)

    if tamper_target == "source_po":
        database.run_query(
            "UPDATE purchase_order_items SET qty = 99 "
            "WHERE po_id = 'PO-RISK-001' AND product_id = 'P001'",
            fetch=False,
        )
    else:
        database.run_query(
            "UPDATE purchase_proposals SET alternative_supplier_id = 'SUP01' "
            "WHERE proposal_id = ?",
            (proposal.proposal_id,),
            fetch=False,
        )

    result = gateway.approve_action(pending.approval_id, approver="approver")
    assert result.status in {"denied", "error"}
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_orders WHERE po_id = ?",
        (proposal.proposed_po_id,),
    )[0][0] == 0
    assert database.run_query(
        "SELECT COUNT(*) FROM effect_receipts WHERE operation_id = ?",
        (execution.operation_id,),
    )[0][0] == 0


@pytest.mark.parametrize(
    ("proposal_id", "operation_id"),
    [
        (None, "proposal:create-po:RAW-MISSING:v1"),
        ("PROP-NOT-FOUND", "proposal:create-po:PROP-NOT-FOUND:v1"),
    ],
)
def test_l2_raw_gateway_requires_a_durable_bound_proposal(
    proposal_db, proposal_id, operation_id
):
    """A planner cannot bypass the Proposal adapter with a raw Gateway call."""
    from backend.tool_gateway import gateway

    args = {
        "po_id": "ALT-RAW-BYPASS",
        "supplier_id": "SUP02",
        "product_id": "P001",
        "qty": 2,
        "unit_price": 475.0,
        "order_date": "2026-07-21",
        "status": "待入庫",
        "note": "raw L2 bypass attempt",
    }
    if proposal_id is not None:
        args.update(
            {
                "proposal_id": proposal_id,
                "proposal_digest": "0" * 64,
                "affected_po_id": "PO-RISK-001",
                "source_po_version": "sha256:" + "0" * 64,
            }
        )

    result = gateway.call(
        "create_purchase_order",
        args,
        role="supply_planner",
        actor="planner",
        agent_name="procurement_agent",
        operation_id=operation_id,
    )

    assert result.status in {"denied", "error"}
    assert database.run_query(
        "SELECT COUNT(*) FROM pending_approvals WHERE operation_id = ?",
        (operation_id,),
    )[0][0] == 0
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_orders WHERE po_id = 'ALT-RAW-BYPASS'"
    )[0][0] == 0


def test_l2_raw_gateway_rejects_payload_mismatched_to_durable_proposal(proposal_db):
    from backend.purchase_proposals import (
        _persist_proposal,
        proposal_to_execution_request,
    )
    from backend.tool_gateway import gateway

    proposal = _persist_proposal(_prepare(proposal_id="PROP-RAW-MISMATCH"))
    execution = proposal_to_execution_request(proposal)
    tampered_args = dict(execution.args)
    tampered_args["qty"] = proposal.qty + 99

    result = gateway.call(
        execution.tool_name,
        tampered_args,
        role="supply_planner",
        actor="planner",
        agent_name="procurement_agent",
        operation_id=execution.operation_id,
    )

    assert result.status in {"denied", "error"}
    assert database.run_query(
        "SELECT COUNT(*) FROM pending_approvals WHERE operation_id = ?",
        (execution.operation_id,),
    )[0][0] == 0
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_orders WHERE po_id = ?",
        (proposal.proposed_po_id,),
    )[0][0] == 0


def test_other_l2_actor_cannot_submit_someone_elses_durable_proposal(proposal_db):
    from backend.purchase_proposals import (
        _persist_proposal,
        proposal_to_execution_request,
    )
    from backend.tool_gateway import gateway

    database.run_query(
        "INSERT INTO users (username, password, role, name) "
        "VALUES ('planner2', 'unused', 'supply_planner', '第二位規劃員')",
        fetch=False,
    )
    database.run_query(
        "INSERT INTO user_organizations (username, organization_id) "
        "VALUES ('planner2', 'demo-org')",
        fetch=False,
    )
    proposal = _persist_proposal(_prepare(proposal_id="PROP-OWNER-BOUND"))
    execution = proposal_to_execution_request(proposal)

    result = gateway.call(
        execution.tool_name,
        dict(execution.args),
        role="supply_planner",
        actor="planner2",
        agent_name="procurement_agent",
        operation_id=execution.operation_id,
    )

    assert result.status == "denied"
    assert database.run_query(
        "SELECT COUNT(*) FROM pending_approvals WHERE operation_id = ?",
        (execution.operation_id,),
    )[0][0] == 0


def test_approval_rechecks_proposal_owner_for_preexisting_pending_request(proposal_db):
    from backend.purchase_proposals import (
        _persist_proposal,
        proposal_to_execution_request,
    )
    from backend.tool_gateway import (
        PO_APPROVAL_POLICY_VERSION,
        _create_pending_approval,
        gateway,
    )

    database.run_query(
        "INSERT INTO users (username, password, role, name) "
        "VALUES ('planner2', 'unused', 'supply_planner', '第二位規劃員')",
        fetch=False,
    )
    database.run_query(
        "INSERT INTO user_organizations (username, organization_id) "
        "VALUES ('planner2', 'demo-org')",
        fetch=False,
    )
    proposal = _persist_proposal(_prepare(proposal_id="PROP-OWNER-EXECUTE"))
    execution = proposal_to_execution_request(proposal)
    approval_id = _create_pending_approval(
        execution.tool_name,
        dict(execution.args),
        "supply_planner",
        requester_username="planner2",
        operation_id=execution.operation_id,
        resource_version="absent",
        policy_version=PO_APPROVAL_POLICY_VERSION,
    )

    result = gateway.approve_action(approval_id, approver="approver")

    assert result.status in {"denied", "error"}
    assert database.run_query(
        "SELECT status FROM pending_approvals WHERE approval_id = ?",
        (approval_id,),
    )[0][0] == "pending"
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_orders WHERE po_id = ?",
        (proposal.proposed_po_id,),
    )[0][0] == 0
    assert database.run_query(
        "SELECT COUNT(*) FROM effect_receipts WHERE operation_id = ?",
        (execution.operation_id,),
    )[0][0] == 0


def test_same_actor_cannot_self_approve_after_switching_to_l3_role(proposal_db):
    from backend.purchase_proposals import (
        ApprovalDecision,
        decide_purchase_proposal,
        submit_purchase_proposal,
    )

    proposal = _prepare(proposal_id="PROP-SELF-SWITCH")
    pending = submit_purchase_proposal(proposal, actor="planner")
    database.run_query(
        "UPDATE users SET role = 'procurement_approver' WHERE username = 'planner'",
        fetch=False,
    )

    result = decide_purchase_proposal(
        ApprovalDecision(proposal_id=proposal.proposal_id, outcome="approve"),
        actor="planner",
    )

    assert result.status == "denied"
    assert database.run_query(
        "SELECT status FROM pending_approvals WHERE approval_id = ?",
        (pending.approval_id,),
    )[0][0] == "pending"
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_orders WHERE po_id = ?",
        (proposal.proposed_po_id,),
    )[0][0] == 0


def test_l3_decision_uses_live_role_not_stale_session_claim(proposal_db):
    from backend.purchase_proposals import (
        ApprovalDecision,
        decide_purchase_proposal,
        submit_purchase_proposal,
    )

    proposal = _prepare(proposal_id="PROP-LIVE-ROLE")
    submit_purchase_proposal(proposal, actor="planner")
    database.run_query(
        "UPDATE users SET role = 'read_only_viewer' WHERE username = 'approver'",
        fetch=False,
    )

    with pytest.raises(PermissionError):
        decide_purchase_proposal(
            ApprovalDecision(proposal_id=proposal.proposal_id, outcome="approve"),
            actor="approver",
        )

    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_orders WHERE po_id = ?",
        (proposal.proposed_po_id,),
    )[0][0] == 0


def test_timeline_is_an_allowlist_and_does_not_echo_rejection_reason(proposal_db):
    from backend.purchase_proposals import (
        ApprovalDecision,
        decide_purchase_proposal,
        get_purchase_operation_timeline,
        proposal_to_execution_request,
        submit_purchase_proposal,
    )

    proposal = _prepare(proposal_id="PROP-TIMELINE-REDACT")
    execution = proposal_to_execution_request(proposal)
    submit_purchase_proposal(proposal, actor="planner")
    sensitive_reason = (
        "API_TOKEN=timeline-secret; C:\\private\\erp.env; "
        "SELECT * FROM credentials; <script>alert(1)</script>"
    )
    decide_purchase_proposal(
        ApprovalDecision(
            proposal_id=proposal.proposal_id,
            outcome="reject",
            reason=sensitive_reason,
        ),
        actor="approver",
    )

    timeline = get_purchase_operation_timeline(
        execution.operation_id, actor="approver"
    )
    allowed_fields = {
        "kind",
        "operation_id",
        "time",
        "actor",
        "proposal_id",
        "approval_id",
        "receipt_id",
        "summary",
    }
    assert all(set(event) <= allowed_fields for event in timeline)
    serialized = json.dumps(timeline, ensure_ascii=False)
    assert "timeline-secret" not in serialized
    assert "private\\erp.env" not in serialized
    assert "credentials" not in serialized
    assert "<script>" not in serialized


def test_retry_rebuild_with_same_id_reuses_the_durable_proposal(
    proposal_db, monkeypatch
):
    import backend.purchase_proposals as purchase_proposals

    first = _prepare(proposal_id="PROP-LOST-RESPONSE")
    submitted = purchase_proposals.submit_purchase_proposal(first, actor="planner")

    class LaterDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 21, 23, 59, 59, tzinfo=tz)

    monkeypatch.setattr(purchase_proposals, "datetime", LaterDateTime)
    rebuilt = _prepare(proposal_id="PROP-LOST-RESPONSE")
    replay = purchase_proposals.submit_purchase_proposal(rebuilt, actor="planner")

    assert rebuilt == first
    assert replay.status == "pending"
    assert replay.approval_id == submitted.approval_id
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_proposals WHERE proposal_id = ?",
        (first.proposal_id,),
    )[0][0] == 1


def test_selected_source_line_and_supplier_price_keep_exact_row_identity(proposal_db):
    from backend.purchase_proposals import (
        list_alternative_suppliers,
        list_impacted_purchase_options,
        prepare_alternative_purchase_proposal,
    )

    with database.transaction() as conn:
        source_line_id = conn.execute(
            "INSERT INTO purchase_order_items (po_id, product_id, qty, unit_price) "
            "VALUES ('PO-RISK-001', 'P001', 7, 900)"
        ).lastrowid
        supplier_price_id = conn.execute(
            "INSERT INTO supplier_products "
            "(supplier_id, product_id, price, carbon_factor) "
            "VALUES ('SUP02', 'P001', 999, 2.1)"
        ).lastrowid

    impacted = list_impacted_purchase_options(actor="planner")
    selected = next(
        item for item in impacted if item["source_po_item_id"] == source_line_id
    )
    alternatives = list_alternative_suppliers(
        affected_po_id=selected["po_id"],
        product_id=selected["product_id"],
        source_po_item_id=selected["source_po_item_id"],
        actor="planner",
    )
    alternative = next(
        item
        for item in alternatives
        if item["supplier_product_id"] == supplier_price_id
    )

    proposal = prepare_alternative_purchase_proposal(
        proposal_id="PROP-EXACT-LINE",
        affected_po_id=selected["po_id"],
        product_id=selected["product_id"],
        source_po_item_id=selected["source_po_item_id"],
        alternative_supplier_id=alternative["supplier_id"],
        alternative_supplier_product_id=alternative["supplier_product_id"],
        reason="使用畫面所選的精確明細與報價列",
        estimated_delay_days=12,
        actor="planner",
    )

    assert proposal.source_po_item_id == source_line_id
    assert proposal.alternative_supplier_product_id == supplier_price_id
    assert proposal.qty == 7
    assert proposal.unit_price == 999.0


def test_only_one_full_replacement_can_execute_for_the_same_source_line(proposal_db):
    from backend.purchase_proposals import (
        ApprovalDecision,
        decide_purchase_proposal,
        submit_purchase_proposal,
    )

    first = _prepare(proposal_id="PROP-MITIGATION-A")
    second = _prepare(proposal_id="PROP-MITIGATION-B")
    first_pending = submit_purchase_proposal(first, actor="planner")
    second_pending = submit_purchase_proposal(second, actor="planner")

    first_result = decide_purchase_proposal(
        ApprovalDecision(proposal_id=first.proposal_id, outcome="approve"),
        actor="approver",
    )
    second_result = decide_purchase_proposal(
        ApprovalDecision(proposal_id=second.proposal_id, outcome="approve"),
        actor="approver",
    )

    assert first_result.status == "ok"
    assert second_result.status == "error"
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_orders WHERE po_id IN (?, ?)",
        (first.proposed_po_id, second.proposed_po_id),
    )[0][0] == 1
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_proposal_effects"
    )[0][0] == 1
    assert database.run_query(
        "SELECT status FROM pending_approvals WHERE approval_id = ?",
        (first_pending.approval_id,),
    )[0][0] == "approved"
    assert database.run_query(
        "SELECT status FROM pending_approvals WHERE approval_id = ?",
        (second_pending.approval_id,),
    )[0][0] == "pending"


def test_other_organization_cannot_read_or_decide_proposal(proposal_db):
    from backend.access_control import load_principal
    from backend.purchase_proposals import (
        ApprovalDecision,
        decide_purchase_proposal,
        get_purchase_proposal_evidence,
        submit_purchase_proposal,
    )

    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO users (username, password, role, name) "
            "VALUES ('other-approver', 'unused', 'procurement_approver', '跨組織主管')"
        )
        conn.execute(
            "INSERT INTO user_organizations (username, organization_id) "
            "VALUES ('other-approver', 'other-org')"
        )
        conn.execute(
            "INSERT INTO organization_entitlements "
            "(organization_id, entitlement_key, enabled) "
            "VALUES ('other-org', 'l3_governed_action', 1)"
        )

    assert load_principal("other-approver") is None
    proposal = _prepare(proposal_id="PROP-ORG-SCOPE")
    submit_purchase_proposal(proposal, actor="planner")

    with pytest.raises(PermissionError):
        get_purchase_proposal_evidence(
            proposal.proposal_id, actor="other-approver"
        )
    with pytest.raises(PermissionError):
        decide_purchase_proposal(
            ApprovalDecision(proposal_id=proposal.proposal_id, outcome="approve"),
            actor="other-approver",
        )
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_orders WHERE po_id = ?",
        (proposal.proposed_po_id,),
    )[0][0] == 0


@pytest.mark.parametrize("decision_kind", ["approve", "reject"])
def test_gateway_direct_cross_organization_decision_fails_closed(
    proposal_db, decision_kind
):
    from backend.purchase_proposals import submit_purchase_proposal
    from backend.tool_gateway import gateway

    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO users (username, password, role, name) "
            "VALUES ('other-approver', 'unused', 'procurement_approver', '跨組織主管')"
        )
        conn.execute(
            "INSERT INTO user_organizations (username, organization_id) "
            "VALUES ('other-approver', 'other-org')"
        )
        conn.execute(
            "INSERT INTO organization_entitlements "
            "(organization_id, entitlement_key, enabled) "
            "VALUES ('other-org', 'l3_governed_action', 1)"
        )

    proposal = _prepare(proposal_id=f"PROP-CROSS-ORG-{decision_kind.upper()}")
    pending = submit_purchase_proposal(proposal, actor="planner")
    if decision_kind == "approve":
        result = gateway.approve_action(
            pending.approval_id, approver="other-approver"
        )
    else:
        result = gateway.reject_action(
            pending.approval_id,
            "cross organization rejection",
            approver="other-approver",
        )

    assert result.status == "denied"
    assert database.run_query(
        "SELECT status FROM pending_approvals WHERE approval_id = ?",
        (pending.approval_id,),
    )[0][0] == "pending"
    assert database.run_query(
        "SELECT COUNT(*) FROM purchase_orders WHERE po_id = ?",
        (proposal.proposed_po_id,),
    )[0][0] == 0
