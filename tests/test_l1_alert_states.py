"""
tests/test_l1_alert_states.py
L1 風險總覽完善：
  - 告警「已讀／處理中／已通知L2」狀態落地（RISK_ALERT_ACK，fail-closed）
  - 告警 feed 併入狀態與 L3 提案計數
  - L1 通知 L2：待確認情報在 L2 頁列出，登錄成事件後自動消失
  - 系統內未結採購單可直接對映事件（不必上傳 CSV）
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from backend import database
from backend import l1_monitoring as l1
from backend import supply_chain_risk as risk
from backend.access_control import RISK_ALERT_ACK, capabilities_for_role


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 14, 9, 0, 0)


@pytest.fixture
def l1_db(tmp_path, monkeypatch):
    db_path = tmp_path / "l1-states.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    monkeypatch.setattr(risk, "DB_FILE", str(db_path))
    monkeypatch.delenv("ERP_ENABLE_DEMO_SEED", raising=False)
    database.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM suppliers")
        conn.execute("DELETE FROM purchase_orders")
        conn.execute("DELETE FROM supply_chain_events")
        conn.execute("DELETE FROM supply_chain_news")
        conn.execute(
            "INSERT INTO suppliers (supplier_id, name, country, region, latitude, longitude, is_official) "
            "VALUES ('S-TW', '台北供應商', '台灣', '亞洲', 25.0, 121.5, 1)"
        )
        conn.execute(
            "INSERT INTO suppliers (supplier_id, name, country, region, latitude, longitude, is_official) "
            "VALUES ('S-DE', '柏林供應商', '德國', '歐洲', 52.5, 13.4, 1)"
        )
        conn.executemany(
            "INSERT INTO purchase_orders (po_id, supplier_id, status, total_amount) VALUES (?,?,?,?)",
            [("PO-TW", "S-TW", "已下單", 100.0), ("PO-DE", "S-DE", "運送中", 200.0), ("PO-DONE", "S-TW", "已完成", 5.0)],
        )
        conn.executemany(
            "INSERT INTO purchase_order_items (po_id, product_id, qty, unit_price) VALUES (?,?,?,?)",
            [("PO-TW", "P1", 1, 100.0), ("PO-DE", "P2", 2, 100.0), ("PO-DONE", "P1", 1, 5.0)],
        )
        conn.execute(
            """INSERT INTO supply_chain_news (id, country, region, title, summary, url, source, published_at,
               relevance_tag, fetched_at, category, is_relevant, estimated_delay)
               VALUES (7, '台灣', '北區', 'Typhoon closes port', 's', 'https://n/7', 't', '2026-09-13 06:00',
                       'supply_chain', '2026-09-13 07:00', '氣候', 1, 21)"""
        )
        conn.execute("UPDATE supply_chain_news SET analysis_status='succeeded', analysis_country=country, analysis_region=region, analysis_summary=summary")
        conn.commit()
    return db_path


def test_alert_ack_capability_is_l1_only():
    assert RISK_ALERT_ACK in capabilities_for_role("risk_viewer")
    assert RISK_ALERT_ACK in capabilities_for_role("supply_planner")
    assert RISK_ALERT_ACK in capabilities_for_role("procurement_approver")
    assert RISK_ALERT_ACK not in capabilities_for_role("hr")


def test_confirmed_alert_status_persists_and_shows_in_feed(l1_db):
    event_id = risk.add_risk_event("罷工", "亞洲", "台灣", 14, "港口罷工", actor="planner")
    feed = l1.get_latest_event_alerts(actor="viewer", now=NOW)
    assert feed["confirmed"][0]["ack_status"] == "未讀"
    assert feed["confirmed"][0]["proposals"] == {"pending": 0, "approved": 0, "rejected": 0, "unsubmitted": 0}

    record = l1.set_alert_status("confirmed", event_id, "處理中", actor="viewer", note="已通知採購", now=NOW)
    assert record["alert_key"] == f"confirmed:{event_id}"

    feed = l1.get_latest_event_alerts(actor="viewer", now=NOW)
    item = feed["confirmed"][0]
    assert item["ack_status"] == "處理中" and item["ack_note"] == "已通知採購" and item["ack_by"] == "viewer"

    # 同一鍵再標記 → 覆寫，不新增
    l1.set_alert_status("confirmed", event_id, "已讀", actor="viewer", now=NOW)
    assert l1.get_alert_states("confirmed", [event_id])[event_id]["status"] == "已讀"
    with sqlite3.connect(l1_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risk_alert_states").fetchone()[0] == 1


@pytest.mark.parametrize("actor", [None, "", "hr1", "nobody"])
def test_set_alert_status_fails_closed(l1_db, actor):
    event_id = risk.add_risk_event("罷工", "亞洲", "台灣", 14, "港口罷工", actor="planner")
    with pytest.raises(PermissionError):
        l1.set_alert_status("confirmed", event_id, "已讀", actor=actor)
    with sqlite3.connect(l1_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risk_alert_states").fetchone()[0] == 0


def test_status_values_are_validated_per_kind(l1_db):
    event_id = risk.add_risk_event("罷工", "亞洲", "台灣", 14, "港口罷工", actor="planner")
    with pytest.raises(ValueError):
        l1.set_alert_status("confirmed", event_id, "已通知L2", actor="viewer")   # 已確認事件不能「通知 L2」
    with pytest.raises(ValueError):
        l1.set_alert_status("candidate", 7, "處理中", actor="viewer")           # 候選只有 未讀/已讀/已通知L2
    with pytest.raises(ValueError):
        l1.set_alert_status("unknown", 7, "已讀", actor="viewer")


def test_l1_notify_l2_round_trip(l1_db):
    feed = l1.get_latest_event_alerts(actor="viewer", now=NOW)
    assert [c["news_id"] for c in feed["candidates"]] == [7]
    assert feed["candidates"][0]["ack_status"] == "未讀"
    assert l1.list_l1_notifications_for_l2(actor="planner") == []

    l1.set_alert_status("candidate", 7, "已通知L2", actor="viewer", note="請優先確認", now=NOW)
    notices = l1.list_l1_notifications_for_l2(actor="planner")
    assert len(notices) == 1
    assert notices[0]["news_id"] == 7 and notices[0]["title"] == "Typhoon closes port"
    assert notices[0]["notified_by"] == "viewer" and notices[0]["note"] == "請優先確認"
    assert notices[0]["event_type"] == "氣候" and notices[0]["impact_days"] == 21
    assert l1.get_latest_event_alerts(actor="viewer", now=NOW)["candidates"][0]["ack_status"] == "已通知L2"

    # L2 登錄成事件 → 通知自動結案、候選消失
    risk.add_risk_event("氣候", "北區", "台灣", 21, "颱風", news_id=7, actor="planner")
    assert l1.list_l1_notifications_for_l2(actor="planner") == []
    assert l1.get_latest_event_alerts(actor="viewer", now=NOW)["candidates"] == []


@pytest.mark.parametrize("actor", [None, "viewer", "approver", "nobody"])
def test_l2_notification_list_requires_analysis_read(l1_db, actor):
    with pytest.raises(PermissionError):
        l1.list_l1_notifications_for_l2(actor=actor)


def test_open_purchase_rows_map_to_events_without_csv(l1_db):
    risk.add_risk_event("罷工", "亞洲", "台灣", 14, "港口罷工", actor="planner")
    rows = l1.load_open_purchase_rows(actor="viewer")
    assert [r["po_id"] for r in rows] == ["PO-DE", "PO-TW"]          # 已完成不算
    assert rows[1] == {
        "external_id": "PO-TW", "po_id": "PO-TW", "supplier_id": "S-TW", "product_id": "P1",
        "qty": 1, "status": "已下單", "order_date": "", "total_amount": 100.0,
    }
    with sqlite3.connect(l1_db) as conn:
        conn.row_factory = sqlite3.Row
        suppliers = {r["supplier_id"]: dict(r) for r in conn.execute(
            "SELECT supplier_id, country, region, risk_level FROM suppliers")}
    events = risk.get_risk_events_list(limit=30).to_dict("records")
    mapped = {m["po_id"]: m for m in l1.map_purchase_rows_to_events(rows, supplier_context=suppliers, events=events)}
    assert mapped["PO-TW"]["match_status"] == "需關注" and mapped["PO-TW"]["impact_days"] == 14
    assert mapped["PO-DE"]["match_status"] == "正常"


@pytest.mark.parametrize("actor", [None, "", "hr1", "nobody"])
def test_open_purchase_rows_fail_closed(l1_db, actor):
    with pytest.raises(PermissionError):
        l1.load_open_purchase_rows(actor=actor)


def test_confirmed_alert_shows_l3_proposal_counts(l1_db, monkeypatch):
    from backend import purchase_proposals as pp

    event_id = risk.add_risk_event("罷工", "亞洲", "台灣", 14, "港口罷工", actor="planner")
    monkeypatch.setattr(pp, "proposal_status_summary_by_event",
                        lambda ids, conn=None: {event_id: {"pending": 1, "approved": 2, "rejected": 0, "unsubmitted": 0}})
    feed = l1.get_latest_event_alerts(actor="viewer", now=NOW)
    assert feed["confirmed"][0]["proposals"]["approved"] == 2


def _calls(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == name)
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]


def test_frontend_forwards_actor_for_l1_writes_and_l2_notices():
    overview = ast.parse((ROOT / "frontend/components/risk_overview.py").read_text(encoding="utf-8"))
    for name in ("set_alert_status", "load_open_purchase_rows", "get_latest_event_alerts"):
        calls = _calls(overview, name)
        assert calls, name
        assert all(any(k.arg == "actor" for k in c.keywords) for c in calls), name
    dashboard = ast.parse((ROOT / "frontend/components/risk_dashboard.py").read_text(encoding="utf-8"))
    calls = _calls(dashboard, "list_l1_notifications_for_l2")
    assert calls and all(any(k.arg == "actor" for k in c.keywords) for c in calls)
