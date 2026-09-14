"""
tests/test_l3_proposal_closure.py
L3 受治理行動閉環：
  - 步驟 5 清單附帶每條明細的提案狀態（pending / approved / rejected）
  - 提案綁定風險事件，審批頁可讀事件依據與採購單註記（PROPOSAL_EVIDENCE_READ）
  - L1 可取得各事件的提案狀態計數（不含提案內容）
  - 沖銷紀錄可查、同一審批單只算一次
"""

from __future__ import annotations

import ast
from pathlib import Path
import sqlite3

import pytest

from backend import agent_logger
from backend import database
from backend import purchase_proposals as pp
from backend import supply_chain_risk as risk


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def flow_db(tmp_path, monkeypatch):
    db_path = tmp_path / "l3-flow.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    monkeypatch.setattr(risk, "DB_FILE", str(db_path))
    monkeypatch.setenv("ERP_ENABLE_DEMO_SEED", "1")   # 正式供應商各一張採購單
    database.init_db()
    return db_path


def _first_impacted(actor="planner"):
    opts = pp.list_impacted_purchase_options(actor=actor)
    assert opts, "demo seed 應該讓至少一條明細被標記"
    return opts[0]


def _propose(sel, proposal_id, event_id=None, actor="planner"):
    alts = pp.list_alternative_suppliers(
        affected_po_id=sel["po_id"], product_id=sel["product_id"],
        source_po_item_id=sel["source_po_item_id"], actor=actor,
    )
    assert alts
    proposal = pp.prepare_alternative_purchase_proposal(
        proposal_id=proposal_id, affected_po_id=sel["po_id"], product_id=sel["product_id"],
        source_po_item_id=sel["source_po_item_id"], alternative_supplier_id=alts[0]["supplier_id"],
        alternative_supplier_product_id=alts[0]["supplier_product_id"], reason="改由備援供貨",
        estimated_delay_days=14, source_event_id=event_id, actor=actor,
    )
    result = pp.submit_purchase_proposal(proposal, actor=actor)
    assert result.status == "pending"
    return proposal


def _mark_first_po(flow_db):
    with sqlite3.connect(flow_db) as conn:
        po_id, supplier = conn.execute(
            "SELECT p.po_id, p.supplier_id FROM purchase_orders p ORDER BY p.po_id LIMIT 1"
        ).fetchone()
        country, region = conn.execute(
            "SELECT country, region FROM suppliers WHERE supplier_id=?", (supplier,)
        ).fetchone()
    risk.update_po_impact(po_id, estimated_delay_days=14, alternative_suggestion="改由備援", actor="planner")
    return po_id, country, region


def test_step5_reports_proposal_status_through_the_whole_flow(flow_db):
    po_id, country, region = _mark_first_po(flow_db)
    event_id = risk.add_risk_event("戰爭", region, country, 30, "港口攻擊", actor="planner")

    sel = _first_impacted()
    assert sel["po_id"] == po_id and sel["proposal"] is None

    proposal = _propose(sel, "closure-001", event_id)
    sel = _first_impacted()
    assert sel["proposal"]["status"] == "pending" and sel["proposal"]["label"] == "待 L3 核准"
    assert sel["proposal"]["source_event_id"] == event_id

    pp.decide_purchase_proposal(pp.ApprovalDecision(proposal_id="closure-001", outcome="approve"), actor="approver")
    sel = _first_impacted()
    assert sel["proposal"]["status"] == "approved"
    assert sel["proposal"]["approver"] == "approver" and sel["proposal"]["decided_at"]
    assert sel["proposal"]["proposed_po_id"] == proposal.proposed_po_id
    with sqlite3.connect(flow_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE po_id=?", (proposal.proposed_po_id,)).fetchone()[0] == 1

    # L1 只拿計數
    summary = pp.proposal_status_summary_by_event([event_id, 999999])
    assert summary == {event_id: {"pending": 0, "approved": 1, "rejected": 0, "unsubmitted": 0}}


def test_rejected_proposal_keeps_reason_and_latest_wins(flow_db):
    po_id, country, region = _mark_first_po(flow_db)
    sel = _first_impacted()
    _propose(sel, "closure-r1")
    pp.decide_purchase_proposal(
        pp.ApprovalDecision(proposal_id="closure-r1", outcome="reject", reason="價格過高"), actor="approver"
    )
    sel = _first_impacted()
    assert sel["proposal"]["status"] == "rejected" and sel["proposal"]["reason"] == "價格過高"
    # 重新提案 → 最新一筆為準
    _propose(sel, "closure-r2")
    assert _first_impacted()["proposal"]["proposal_id"] == "closure-r2"


def test_proposal_context_exposes_event_and_po_annotation(flow_db):
    po_id, country, region = _mark_first_po(flow_db)
    event_id = risk.add_risk_event("罷工", region, country, 21, "碼頭罷工", actor="planner")
    proposal = _propose(_first_impacted(), "closure-ctx", event_id)

    context = pp.get_purchase_proposal_context(proposal, actor="approver")
    assert context["event"]["id"] == event_id and context["event"]["event_type"] == "罷工"
    assert context["affected_po"]["po_id"] == po_id
    assert context["affected_po"]["estimated_delay_days"] == 14
    assert context["affected_po"]["alternative_suggestion"] == "改由備援"

    unbound = _propose(_first_impacted(), "closure-ctx-2")
    assert pp.get_purchase_proposal_context(unbound, actor="approver")["event"] is None


@pytest.mark.parametrize("actor", [None, "", "viewer", "planner", "nobody"])
def test_proposal_context_fails_closed(flow_db, actor):
    _mark_first_po(flow_db)
    proposal = _propose(_first_impacted(), "closure-deny")
    with pytest.raises(PermissionError):
        pp.get_purchase_proposal_context(proposal, actor=actor)


def test_reversal_record_is_found_only_after_success(flow_db):
    assert agent_logger.get_reversal_record("PENDING-X") is None
    agent_logger.write_action_log("retry_approval", {"approval_id": "PENDING-X"}, "admin", "沖銷失敗", False)
    assert agent_logger.get_reversal_record("PENDING-X") is None
    agent_logger.write_action_log("retry_approval", {"approval_id": "PENDING-X"}, "admin", "已沖銷", True)
    record = agent_logger.get_reversal_record("PENDING-X")
    assert record and record["result"] == "已沖銷" and record["caller"] == "admin"
    assert agent_logger.get_reversal_record("PENDING-Y") is None   # 不會誤配其他單


def _calls(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == name)
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]


def test_workbench_binds_source_event_and_dashboard_guards_reversal():
    wb = ast.parse((ROOT / "frontend/components/purchase_proposal_workbench.py").read_text(encoding="utf-8"))
    prepares = _calls(wb, "prepare_alternative_purchase_proposal")
    assert prepares and all(any(k.arg == "source_event_id" for k in c.keywords) for c in prepares)

    dash_src = (ROOT / "frontend/page_agent_dashboard.py").read_text(encoding="utf-8")
    dash = ast.parse(dash_src)
    assert _calls(dash, "get_reversal_record"), "沖銷前必須查是否已沖銷"
    contexts = _calls(dash, "get_purchase_proposal_context")
    assert contexts and all(any(k.arg == "actor" for k in c.keywords) for c in contexts)
