"""UI contracts for the L2 proposal workbench and L3 evidence surface."""

import ast
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _call_is_guarded_by_submit(tree: ast.AST, function_name: str) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if "form_submit_button" not in ast.dump(node.test):
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


def test_proposal_id_is_stable_until_user_starts_another_proposal():
    from frontend.components.purchase_proposal_workbench import (
        ensure_purchase_proposal_id,
        start_new_purchase_proposal,
    )

    state = {}
    first = ensure_purchase_proposal_id(state)
    assert ensure_purchase_proposal_id(state) == first

    state["purchase_proposal_last_id"] = "PROP-OLD"
    second = start_new_purchase_proposal(state)
    assert second != first
    assert "purchase_proposal_last_id" not in state


def test_l2_page_renders_proposal_workbench_after_what_if():
    source = _source("frontend/page_supply_chain_risk.py")

    assert "render_purchase_proposal_workbench" in source
    assert source.rindex("render_what_if_analysis(") < source.rindex(
        "render_purchase_proposal_workbench("
    )
    assert "actor=principal.username" in source


def test_workbench_submits_domain_proposal_only_after_explicit_form_submit():
    source = _source("frontend/components/purchase_proposal_workbench.py")
    tree = ast.parse(source)

    assert "list_impacted_purchase_options" in source
    assert "list_alternative_suppliers" in source
    assert "prepare_alternative_purchase_proposal" in source
    assert _call_is_guarded_by_submit(tree, "submit_purchase_proposal")
    assert "gateway.call" not in source
    assert "create_purchase_order(" not in source
    assert "尚未寫入 ERP" in source


def test_l3_dashboard_uses_domain_decision_and_redacted_timeline_for_new_proposals():
    source = _source("frontend/page_agent_dashboard.py")

    assert "ApprovalDecision" in source
    assert "decide_purchase_proposal" in source
    assert "get_purchase_proposal_for_operation" in source
    assert "get_purchase_operation_timeline" in source
    assert "受影響採購單" in source
    assert "替代供應商" in source


def test_logout_clears_purchase_proposal_session_state():
    source = _source("frontend/access_navigation.py")

    assert 'key.startswith("purchase_proposal_")' in source


def test_successful_replay_restores_durable_submission_tracking():
    from frontend.components.purchase_proposal_workbench import (
        remember_purchase_proposal_submission,
    )

    state = {}
    replay = SimpleNamespace(status="ok", approval_id="APPROVAL-REPLAY")

    assert remember_purchase_proposal_submission(state, "PROP-REPLAY", replay)
    assert state["purchase_proposal_last_id"] == "PROP-REPLAY"
    assert state["purchase_proposal_last_approval_id"] == "APPROVAL-REPLAY"


def test_failed_submission_does_not_create_false_session_tracking():
    from frontend.components.purchase_proposal_workbench import (
        remember_purchase_proposal_submission,
    )

    state = {}
    failed = SimpleNamespace(status="error", approval_id=None)

    assert not remember_purchase_proposal_submission(state, "PROP-FAILED", failed)
    assert state == {}


def test_rejected_replay_also_restores_durable_submission_tracking():
    from frontend.components.purchase_proposal_workbench import (
        remember_purchase_proposal_submission,
    )

    state = {}
    rejected = SimpleNamespace(status="denied", approval_id="APPROVAL-REJECTED")

    assert remember_purchase_proposal_submission(state, "PROP-REJECTED", rejected)
    assert state["purchase_proposal_last_id"] == "PROP-REJECTED"
    assert state["purchase_proposal_last_approval_id"] == "APPROVAL-REJECTED"


def test_l3_records_are_scoped_before_rendering_proposal_details(monkeypatch):
    import frontend.page_agent_dashboard as dashboard

    records = [
        {"id": "same-org", "operation_id": "proposal:create-po:SAME:v1"},
        {"id": "other-org", "operation_id": "proposal:create-po:OTHER:v1"},
        {"id": "legacy", "operation_id": "legacy-operation"},
    ]

    def fake_lookup(operation_id, *, actor):
        if "OTHER" in operation_id:
            raise PermissionError("cross organization")
        if "SAME" in operation_id:
            return SimpleNamespace(proposal_id="SAME")
        return None

    monkeypatch.setattr(
        dashboard, "get_purchase_proposal_for_operation", fake_lookup
    )
    principal = SimpleNamespace(username="approver")

    visible = dashboard._scope_purchase_records(records, principal)

    assert [item["id"] for item in visible] == ["same-org", "legacy"]


def test_l3_expected_domain_error_is_renderable_instead_of_crashing(monkeypatch):
    import frontend.page_agent_dashboard as dashboard

    def revoked(*args, **kwargs):
        raise PermissionError("approval role revoked")

    monkeypatch.setattr(dashboard, "decide_purchase_proposal", revoked)

    status, message = dashboard._safe_purchase_proposal_decision(
        SimpleNamespace(), actor="approver"
    )

    assert status == "error"
    assert message == "approval role revoked"


def test_l3_refreshes_only_after_a_successful_decision():
    from frontend.page_agent_dashboard import _should_refresh_after_decision

    assert _should_refresh_after_decision("approve", "ok")
    assert _should_refresh_after_decision("approve", "pending")
    assert _should_refresh_after_decision("reject", "denied")
    assert not _should_refresh_after_decision("approve", "error")
    assert not _should_refresh_after_decision("approve", "denied")
    assert not _should_refresh_after_decision("reject", "error")
