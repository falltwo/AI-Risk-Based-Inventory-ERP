"""Regression acceptance for the contracts joining PR16's pipeline to PR17's tiers."""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from backend import database, supply_chain_risk as risk, l1_monitoring as l1
from backend import news_store, risk_intelligence as intelligence
from backend.approval_reversal import reverse_approval
from test_l3_proposal_closure import flow_db, _mark_first_po, _first_impacted, _propose


@pytest.fixture
def integration_db(tmp_path, monkeypatch):
    path = str(tmp_path / "integration.db")
    monkeypatch.setenv("ERP_DB_PATH", path)
    monkeypatch.setattr(database, "DB_FILE", path)
    monkeypatch.setattr(risk, "DB_FILE", path)
    database.init_db()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM suppliers")
        conn.executemany("INSERT INTO suppliers(supplier_id,name,country,region,is_official,latitude,longitude) VALUES(?,?,?,?,1,25,121)",
                         [("N","North","台灣","北區"),("S","South","台灣","南區")])
    return path


def news(path, *, status="succeeded", days=5, country="台灣", region="北區", title="fixture"):
    with sqlite3.connect(path) as conn:
        nid, _ = news_store.store_raw(conn, dict(title=title, country="美國", region="北美", summary="raw body", source="fixture", published_at=datetime.now().isoformat(), url=f"https://fixture.invalid/{title}"))
        if status in {"succeeded", "failed"}:
            news_store.store_analysis(conn, nid, dict(analysis_status=status, analysis_error="provider_error",
                country=country, region=region, event_type="交通", chinese_summary="分析結果", is_relevant=True, estimated_delay=days))
        elif status == "legacy_unverified":
            conn.execute("UPDATE supply_chain_news SET analysis_status=?,is_relevant=1,estimated_delay=7 WHERE id=?", (status,nid))
        conn.row_factory=sqlite3.Row
        return dict(conn.execute("SELECT * FROM supply_chain_news WHERE id=?", (nid,)).fetchone())


def test_l1_uses_analysis_and_rejects_legacy_sources(integration_db):
    valid = news(integration_db)
    for status in ("pending","failed","legacy_unverified"):
        row=news(integration_db,status=status,title=status)
        with sqlite3.connect(integration_db) as conn:
            conn.execute("INSERT INTO supply_chain_events(event_type,country,region,impact_days,created_at,news_id) VALUES('其他','美國','北美',7,datetime('now'),?)", (row["id"],))
    feed=l1.get_latest_event_alerts(actor="viewer")
    assert feed["confirmed"] == []
    assert [(r["news_id"],r["country"],r["region"],r["summary"]) for r in feed["candidates"]] == [(valid["id"],"台灣","北區","分析結果")]
    l1.set_alert_status(l1.ALERT_KIND_CANDIDATE,valid["id"],l1.ALERT_STATUS_NOTIFIED_L2,actor="viewer",note="請 L2 確認")
    assert l1.list_l1_notifications_for_l2(actor="planner")[0]["country"] == "台灣"
    eid=risk.add_risk_event("交通","北區","台灣",5,"登錄",valid["id"],actor="planner")
    assert l1.list_l1_notifications_for_l2(actor="planner") == []
    assert l1.get_latest_event_alerts(actor="viewer")["confirmed"][0]["id"] == eid


@pytest.mark.parametrize("days,expected", [(0,0),(None,None),(5,7)])
def test_evidence_distinguishes_zero_unknown_and_known(integration_db,days,expected):
    n=news(integration_db,days=days)
    evidence=risk.build_risk_evidence([n],[])
    _,events,audit=risk.gate_by_evidence([], [dict(country="台灣",region="北區",event_type="交通",impact_days=7)],evidence)
    if expected is None:
        assert events == [] and audit
    else:
        assert events[0]["impact_days"] == expected
    if days == 0:
        assert audit[0]["action"] == "調整"


def test_empty_failed_evidence_never_authorizes_action(integration_db):
    n=news(integration_db,status="failed")
    evidence=risk.build_risk_evidence([n],[])
    u,e,a=risk.gate_by_evidence([dict(display_name="台灣 北區",risk_pct=90)], [dict(country="台灣",region="北區",event_type="交通",impact_days=7)], evidence)
    assert not evidence["locations"] and u == e == [] and len(a)==2


def test_geography_shared_by_cards_alerts_evidence_exposure(integration_db):
    ev=dict(country="臺灣",region="北區",event_type="交通",impact_days=5)
    assert risk.events_for_location("台灣","南區",[ev]) == []
    assert risk.events_for_location("台灣","北區",[ev]) == [ev]
    assert not l1._event_matches_supplier(ev,dict(country="台灣",region="南區"))
    assert l1._event_matches_supplier(ev,dict(country="台灣",region="北區"))
    evidence=risk.build_risk_evidence([news(integration_db)],[])
    u,e,a=risk.gate_by_evidence([dict(display_name="台灣 南區",risk_pct=99)], [{**ev,"region":"南區"}],evidence)
    assert u == e == []
    with sqlite3.connect(integration_db) as conn:
        conn.executemany("INSERT INTO purchase_orders(po_id,supplier_id,total_amount,status) VALUES(?,?,?,'已下單')",[("PN","N",100),("PS","S",900)])
    assert risk.get_region_exposure("臺灣|北區")["open_po_amount"] == 100
    assert [r["supplier_id"] for r in risk.get_affected_suppliers_by_event("北區","台灣")] == ["N"]


@pytest.mark.parametrize("value", [True,-1,1.5,"7",None])
def test_event_new_rejects_invalid_days(integration_db,value):
    with pytest.raises(ValueError):
        risk.add_risk_event("交通","北區","台灣",value,"x",actor="planner")


@pytest.mark.parametrize("value", [True,-1,1.5,"7"])
def test_event_update_validates_and_preserves_row(integration_db,value):
    eid=risk.add_risk_event("交通","北區","台灣",0,"x",actor="planner")
    with pytest.raises(ValueError):
        risk.update_risk_event(eid,impact_days=value,actor="planner")
    assert risk.get_risk_events_list().iloc[0]["impact_days"] == 0


def test_event_source_revalidated_on_update_and_types_do_not_overwrite(integration_db):
    n=news(integration_db)
    one=risk.add_risk_event("交通","北區","台灣",5,"one",n["id"],actor="planner")
    two=risk.add_risk_event("政策","北區","台灣",5,"two",n["id"],actor="planner")
    assert one != two
    with pytest.raises(ValueError):
        risk.add_risk_event("交通","南區","台灣",5,"wrong",n["id"],actor="planner")
    with sqlite3.connect(integration_db) as conn:
        conn.execute("UPDATE supply_chain_news SET analysis_status='failed' WHERE id=?",(n["id"],))
    with pytest.raises(ValueError):
        risk.update_risk_event(one,description="try",actor="planner")


def test_summary_provenance_failure_and_atomic_apply(integration_db,monkeypatch):
    n=news(integration_db,days=0)
    payload={"摘要":"確認零延遲","更新":[{"地區":"台灣 北區","風險":0}],"事件":[{"類型":"交通","國家":"台灣","地區":"北區","延遲天數":0,"描述":"無延遲"}]}
    monkeypatch.setattr("backend.llm_client.complete_text",lambda *a,**kw:json.dumps(payload))
    result=risk.analyze_heatmap_risk([n],actor="planner")
    assert result["analysis_status"] == "succeeded"
    latest=intelligence.get_latest_ai_risk_summary()
    assert latest["sources"][0]["id"] == n["id"] and latest["sources"][0]["country"] == "台灣"
    monkeypatch.setattr("backend.llm_client.complete_text",lambda *a,**kw:"not JSON")
    failed=risk.analyze_heatmap_risk([n],actor="planner")
    assert failed["analysis_status"] == "failed" and failed["error"]
    intelligence.save_ai_risk_summary(failed,actor="planner")
    assert intelligence.get_latest_ai_risk_summary()["summary_id"] == latest["summary_id"]
    with sqlite3.connect(integration_db) as conn:
        conn.execute("CREATE TRIGGER fail_summary BEFORE INSERT ON risk_ai_summaries BEGIN SELECT RAISE(ABORT,'test rollback'); END")
    with pytest.raises(sqlite3.IntegrityError):
        risk.apply_heatmap_updates([dict(display_name="台灣 北區",risk_pct=0,estimated_delay=0)],"x",actor="planner",summary_result=result)
    with sqlite3.connect(integration_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risk_heatmap").fetchone()[0] == 0


def test_empty_and_legacy_database_migrations_preserve_ids(tmp_path,monkeypatch):
    path=str(tmp_path/"legacy.db")
    monkeypatch.setattr(database,"DB_FILE",path)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE supply_chain_news(id INTEGER PRIMARY KEY,country TEXT,region TEXT,title TEXT,summary TEXT,url TEXT,source TEXT,published_at TEXT,relevance_tag TEXT,fetched_at TEXT,category TEXT,is_relevant INTEGER,estimated_delay INTEGER);
        CREATE TABLE risk_heatmap(region_key TEXT PRIMARY KEY,display_name TEXT,latitude REAL,longitude REAL,risk_pct REAL,ai_summary TEXT,updated_at TEXT);
        INSERT INTO supply_chain_news VALUES(42,'美國','','old','raw','https://example.invalid/old','fixture','2026-09-13','','','其他',1,7);
        """)
    database.init_db();database.init_db()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT id,summary,analysis_status FROM supply_chain_news").fetchone() == (42,"raw","legacy_unverified")
        assert "estimated_delay" in {r[1] for r in conn.execute("PRAGMA table_info(risk_heatmap)")}
        assert "sources_json" in {r[1] for r in conn.execute("PRAGMA table_info(risk_ai_summaries)")}
        assert conn.execute("SELECT COUNT(*) FROM approval_reversals").fetchone()[0] == 0


def seed_reversal(path,kind="update_inventory",receipt=True):
    args={"product_id":"REV","quantity_change":5} if kind=="update_inventory" else {"product_id":"REV","quantity":5}
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO inventory(product_id,name,stock,warehouse_id) VALUES('REV','fixture',20,'WH01')")
        conn.execute("INSERT INTO pending_approvals(approval_id,tool_name,parameters,status) VALUES('REV-A',?,?,'approved')",(kind,json.dumps(args)))
        if receipt:
            conn.execute("INSERT INTO effect_receipts(operation_id,approval_id,payload_digest,result,created_at) VALUES('REV-OP','REV-A','fixture','成功 單號: ORD-20260914-010101',datetime('now'))")
        if kind=="create_order":
            conn.execute("INSERT INTO orders(order_id,product_id,quantity,status) VALUES('ORD-20260914-010101','REV',5,'處理中')")


def test_reversal_cross_process_exactly_once(integration_db):
    seed_reversal(integration_db)
    code="from backend.isolated_runtime import block_external_network; block_external_network(); from backend.approval_reversal import reverse_approval; print(reverse_approval('REV-A',actor='admin')['status'])"
    env=dict(os.environ,ERP_DB_PATH=integration_db,PYTHONIOENCODING="utf-8")
    procs=[subprocess.Popen([sys.executable,"-c",code],cwd=Path(__file__).resolve().parents[1],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8") for _ in range(2)]
    outputs=[p.communicate(timeout=30) for p in procs]
    assert all(p.returncode==0 for p in procs),outputs
    assert sorted(o[0].strip() for o in outputs)==["already_reversed","ok"]
    with sqlite3.connect(integration_db) as conn:
        assert conn.execute("SELECT stock FROM inventory WHERE product_id='REV'").fetchone()[0] == 15
        assert conn.execute("SELECT COUNT(*) FROM approval_reversals").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM stock_moves WHERE ref_no='REV-A'").fetchone()[0] == 1


def test_reversal_rolls_back_when_audit_fails(integration_db):
    seed_reversal(integration_db,kind="create_order")
    with sqlite3.connect(integration_db) as conn:
        conn.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON agent_action_logs BEGIN SELECT RAISE(ABORT,'fail'); END")
    with pytest.raises(sqlite3.IntegrityError):
        reverse_approval("REV-A",actor="admin")
    with sqlite3.connect(integration_db) as conn:
        assert conn.execute("SELECT stock FROM inventory WHERE product_id='REV'").fetchone()[0] == 20
        assert conn.execute("SELECT status FROM orders WHERE order_id='ORD-20260914-010101'").fetchone()[0] == "處理中"
        assert conn.execute("SELECT COUNT(*) FROM approval_reversals").fetchone()[0] == 0


def test_reversal_denies_planner_and_receiptless_legacy(integration_db):
    seed_reversal(integration_db,receipt=False)
    with pytest.raises(PermissionError):
        reverse_approval("REV-A",actor="planner")
    with pytest.raises(ValueError,match="收據"):
        reverse_approval("REV-A",actor="admin")


def test_zero_day_shortcut_and_fresh_summary_ui(integration_db):
    n=news(integration_db,days=0)
    risk.add_risk_event("交通","北區","台灣",0,"新聞",n["id"],actor="planner")
    risk.upsert_risk_heatmap("台灣|北區","台灣 北區",25,121,60,"fixture",actor="planner")
    script="from frontend.components.supply_map import render_risk_shortcuts\nrender_risk_shortcuts('integration',actor='planner')"
    at=AppTest.from_string(script,default_timeout=20).run()
    assert not at.exception
    button=next(b for b in at.button if "建立應變計畫" in b.label)
    assert "0 天" in button.label
    button.click().run()
    assert not at.exception
    events=risk.get_risk_events_list()
    assert events["impact_days"].tolist()==[0,0]
    fresh=AppTest.from_string(script,default_timeout=20).run()
    assert not fresh.exception and any("查看分析" in b.label for b in fresh.button)


def test_l1_to_l2_to_l3_and_fresh_views(flow_db,monkeypatch):
    from backend import purchase_proposals as pp
    po_id,country,region=_mark_first_po(flow_db)
    n=news(flow_db,country=country,region=region,title="L1-L2-L3")
    l1.set_alert_status(l1.ALERT_KIND_CANDIDATE,n["id"],l1.ALERT_STATUS_NOTIFIED_L2,actor="viewer",note="檢查供貨")
    assert l1.list_l1_notifications_for_l2(actor="planner")[0]["news_id"] == n["id"]
    eid=risk.add_risk_event("交通",region,country,5,"L2 確認",n["id"],actor="planner")
    l1.set_alert_status(l1.ALERT_KIND_CONFIRMED,eid,"處理中",actor="viewer")
    payload={"摘要":"本次有效分析摘要","更新":[],"事件":[]}
    monkeypatch.setattr("backend.llm_client.complete_text",lambda *a,**k:json.dumps(payload))
    summary=risk.analyze_heatmap_risk([n],actor="planner")
    assert summary["analysis_status"]=="succeeded"
    proposal=_propose(_first_impacted(),"integrated-flow",eid)
    # Planner cannot bypass the reviewer even after being allowed to annotate POs.
    with pytest.raises(PermissionError):
        pp.decide_purchase_proposal(pp.ApprovalDecision(proposal_id=proposal.proposal_id,outcome="approve"),actor="planner")
    ctx=pp.get_purchase_proposal_context(proposal,actor="approver")
    assert ctx["event"]["analysis_summary"]=="分析結果"
    l3script=("from backend.purchase_proposals import get_purchase_proposal_for_operation\n"
              "from backend.access_control import load_principal\n"
              "from frontend.page_agent_dashboard import _render_domain_proposal_evidence\n"
              f"proposal=get_purchase_proposal_for_operation({pp.proposal_operation_id(proposal.proposal_id)!r},actor='approver')\n"
              "_render_domain_proposal_evidence(proposal,load_principal('approver'))")
    l3=AppTest.from_string(l3script,default_timeout=20).run()
    assert not l3.exception
    assert any("分析結果" in m.value for m in l3.markdown)
    pp.decide_purchase_proposal(pp.ApprovalDecision(proposal_id=proposal.proposal_id,outcome="approve"),actor="approver")
    feed=l1.get_latest_event_alerts(actor="viewer")
    confirmed=next(e for e in feed["confirmed"] if e["id"]==eid)
    assert confirmed["ack_status"]=="處理中" and confirmed["proposals"]["approved"]==1
    assert not l1.list_l1_notifications_for_l2(actor="planner")
    assert intelligence.get_latest_ai_risk_summary()["summary_id"]==summary["summary_id"]
    for script in (
        "from frontend.components.risk_overview import _render_latest_event_alerts,_render_latest_ai_summary\n_render_latest_event_alerts(actor='viewer')\n_render_latest_ai_summary(actor='viewer')",
        "from frontend.components.purchase_proposal_workbench import render_purchase_proposal_workbench\nrender_purchase_proposal_workbench(actor='planner')",
    ):
        fresh=AppTest.from_string(script,default_timeout=20).run()
        assert not fresh.exception
    with sqlite3.connect(flow_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE po_id=?",(proposal.proposed_po_id,)).fetchone()[0]==1


def test_empty_database_repeated_init_without_demo(tmp_path,monkeypatch):
    path=str(tmp_path/"empty.db")
    monkeypatch.setattr(database,"DB_FILE",path)
    monkeypatch.setenv("ERP_DEMO_MODE","0")
    database.init_db();database.init_db()
    with sqlite3.connect(path) as conn:
        for table in ("supply_chain_news","supply_chain_events","purchase_orders","risk_ai_summaries","risk_alert_states","approval_reversals"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]==0


@pytest.mark.parametrize("days", [None,-1,1.5,"unknown",366])
def test_legacy_manual_unknown_or_invalid_days_are_not_confirmed_zero(integration_db,days):
    with sqlite3.connect(integration_db) as conn:
        conn.execute("INSERT INTO supply_chain_events(event_type,country,region,impact_days,created_at) VALUES('其他','台灣','北區',?,datetime('now'))",(days,))
    assert l1.get_latest_event_alerts(actor="viewer")["confirmed"]==[]
    assert risk.get_active_risk_events().empty
