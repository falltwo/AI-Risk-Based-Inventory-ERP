"""L1 最新事件告警 feed：唯讀、fail-closed、資料直接來自 DB（非 session state）。"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from backend import database
from backend import l1_monitoring
from backend import supply_chain_risk as risk


NOW = datetime(2026, 9, 12, 9, 0, 0)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def alert_db(tmp_path, monkeypatch):
    db_path = tmp_path / "l1-alerts.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    monkeypatch.setattr(risk, "DB_FILE", str(db_path))
    database.init_db()

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO supply_chain_news
                (id, country, region, title, summary, url, source, published_at,
                 relevance_tag, fetched_at, category, is_relevant, estimated_delay)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                # 已登錄為事件 → 不應再出現在候選
                (1, "日本", "關東", "Registered port strike", "s", "https://n/1",
                 "test", "2026-09-10 08:00", "supply_chain", "2026-09-10 09:00",
                 "罷工", 1, 10),
                # 候選：高嚴重度、時間最新
                (2, "台灣", "北區", "Typhoon closes port", "s", "https://n/2",
                 "test", "2026-09-11 06:00", "supply_chain", "2026-09-11 07:00",
                 "氣候", 1, 21),
                # 候選：published_at 無法解析 → 退回 fetched_at
                (3, "越南", "", "Customs slowdown", "s", "https://n/3",
                 "test", "Thu, 10 Sep 2026 00:00:00 GMT", "supply_chain",
                 "2026-09-10 12:00", "政策", 1, 5),
                # 與 3 重複（同標題同網址）→ 去重
                (4, "越南", "", "Customs slowdown", "s", "https://n/3",
                 "test", "2026-09-10 12:30", "supply_chain", "2026-09-10 12:30",
                 "政策", 1, 5),
                # 延遲 0 天 → 排除
                (5, "美國", "", "General economy news", "s", "https://n/5",
                 "test", "2026-09-11 01:00", "supply_chain", "2026-09-11 01:00",
                 "其他", 1, 0),
                # 標記不相關 → 排除
                (6, "德國", "", "Irrelevant", "s", "https://n/6",
                 "test", "2026-09-11 01:00", "supply_chain", "2026-09-11 01:00",
                 "其他", 0, 9),
                # 超出 30 天視窗 → 排除
                (7, "墨西哥", "", "Old strike", "s", "https://n/7",
                 "test", "2026-07-01 01:00", "supply_chain", "2026-07-01 01:00",
                 "罷工", 1, 14),
            ],
        )
        conn.executemany(
            """
            INSERT INTO supply_chain_events
                (id, event_type, region, country, impact_days, description,
                 created_at, news_id)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                # id 最小但 created_at 最新（覆寫更新的情境）
                (1, "罷工", "關東", "日本", 10, "由新聞登錄", "2026-09-11 10:00", 1),
                (2, "地震", "關西", "日本", 3, "人工登錄", "2026-09-05 10:00", None),
                # 超出視窗
                (3, "戰爭", "", "伊朗", 45, "舊事件", "2026-06-01 10:00", None),
            ],
        )
        conn.execute("UPDATE supply_chain_news SET analysis_status='succeeded', analysis_country=country, analysis_region=region, analysis_summary=summary")
        conn.commit()
    return db_path


@pytest.mark.parametrize("actor", [None, "", "hr1", "nobody"])
def test_alert_feed_is_denied_before_any_read(alert_db, monkeypatch, actor):
    reads = []

    def forbidden_read(*args, **kwargs):
        reads.append(args)
        raise AssertionError("alert data was read before authorization")

    monkeypatch.setattr(l1_monitoring, "_load_confirmed_alerts", forbidden_read)
    monkeypatch.setattr(l1_monitoring, "_load_candidate_alerts", forbidden_read)

    with pytest.raises(PermissionError):
        l1_monitoring.get_latest_event_alerts(actor=actor, now=NOW)
    assert reads == []


@pytest.mark.parametrize("actor", ["viewer", "planner", "approver", "admin"])
def test_roles_with_overview_read_can_load_feed(alert_db, actor):
    feed = l1_monitoring.get_latest_event_alerts(actor=actor, now=NOW)
    assert feed["confirmed_count"] == 2
    assert feed["candidate_count"] == 2


def test_confirmed_alerts_are_windowed_ordered_by_time_and_linked_to_news(alert_db):
    feed = l1_monitoring.get_latest_event_alerts(actor="viewer", now=NOW)

    assert feed["since"] == "2026-08-13"
    assert [item["id"] for item in feed["confirmed"]] == [1, 2]

    newest = feed["confirmed"][0]
    assert newest["severity"] == "中"
    assert newest["source"] == l1_monitoring.ALERT_SOURCE_NEWS
    assert newest["news_title"] == "Registered port strike"
    assert newest["news_url"] == "https://n/1"

    manual = feed["confirmed"][1]
    assert manual["source"] == l1_monitoring.ALERT_SOURCE_MANUAL
    assert manual["news_title"] == ""
    assert manual["severity"] == "低"


def test_candidates_exclude_registered_zero_delay_irrelevant_old_and_duplicates(alert_db):
    feed = l1_monitoring.get_latest_event_alerts(actor="viewer", now=NOW)

    assert feed["candidate_count"] == 2
    typhoon, customs = feed["candidates"]
    assert typhoon["news_id"] == 2
    assert typhoon["severity"] == "高"
    assert typhoon["status"] == l1_monitoring.CANDIDATE_STATUS
    assert typhoon["url"] == "https://n/2"
    # 3 與 4 是同一則新聞的重複列，只能出現一次
    assert customs["news_id"] in {3, 4}
    assert customs["title"] == "Customs slowdown"
    assert customs["impact_days"] == 5
    assert feed["highest_severity"] == "高"


def test_feed_reflects_new_news_and_new_registration_without_session_state(alert_db):
    before = l1_monitoring.get_latest_event_alerts(actor="viewer", now=NOW)
    assert before["candidate_count"] == 2

    # 排程／L2 寫入一則新新聞 → 下一次讀取立即成為候選
    with sqlite3.connect(alert_db) as conn:
        conn.execute(
            """
            INSERT INTO supply_chain_news
                (id, country, region, title, summary, url, source, published_at,
                 relevance_tag, fetched_at, category, is_relevant, estimated_delay)
            VALUES (8, '南韓', '', 'New rail strike', 's', 'https://n/8', 'test',
                    '2026-09-12 08:00', 'supply_chain', '2026-09-12 08:30',
                    '罷工', 1, 12)
            """
        )
        conn.execute("UPDATE supply_chain_news SET analysis_status='succeeded', analysis_country=country, analysis_region=region, analysis_summary=summary")
        conn.commit()
    after_news = l1_monitoring.get_latest_event_alerts(actor="viewer", now=NOW)
    assert after_news["candidate_count"] == 3
    assert after_news["candidates"][0]["news_id"] == 8

    # L2 登錄該新聞 → 候選消失、已確認增加，並帶著來源新聞
    risk.add_risk_event("罷工", "", "南韓", 12, "登錄", news_id=8, actor="planner")
    after_register = l1_monitoring.get_latest_event_alerts(actor="viewer", now=NOW)
    assert after_register["candidate_count"] == 2
    assert after_register["confirmed"][0]["news_id"] == 8
    assert after_register["confirmed"][0]["news_title"] == "New rail strike"


def test_window_and_limit_are_respected(alert_db):
    # 2026-09-12 往前 5 天 = 2026-09-07；事件 2（09-05）落在視窗外
    recent = l1_monitoring.get_latest_event_alerts(actor="viewer", since_days=5, now=NOW)
    assert [item["id"] for item in recent["confirmed"]] == [1]
    # 視窗邊界含當日：往前 7 天 = 2026-09-05，事件 2 剛好納入
    week = l1_monitoring.get_latest_event_alerts(actor="viewer", since_days=7, now=NOW)
    assert [item["id"] for item in week["confirmed"]] == [1, 2]

    quarter = l1_monitoring.get_latest_event_alerts(actor="viewer", since_days=120, now=NOW)
    assert [item["id"] for item in quarter["confirmed"]] == [1, 2, 3]
    assert 7 in {item["news_id"] for item in quarter["candidates"]}

    capped = l1_monitoring.get_latest_event_alerts(actor="viewer", limit=1, now=NOW)
    assert capped["confirmed_count"] == 1
    assert capped["candidate_count"] == 1


def test_severity_classification_thresholds():
    assert l1_monitoring.classify_alert_severity(14) == "高"
    assert l1_monitoring.classify_alert_severity(7) == "中"
    assert l1_monitoring.classify_alert_severity(1) == "低"
    assert l1_monitoring.classify_alert_severity(0) == "無"
    assert l1_monitoring.classify_alert_severity(None) == "無"
    assert l1_monitoring.classify_alert_severity("bad") == "無"


def test_risk_events_list_orders_by_created_at_not_id(alert_db):
    frame = risk.get_risk_events_list(limit=10)
    assert frame["id"].tolist() == [1, 2, 3]


def test_overview_component_forwards_live_actor_to_alert_feed():
    path = ROOT / "frontend/components/risk_overview.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    render_signature = None
    feed_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "render_risk_overview":
            render_signature = {a.arg for a in node.args.args + node.args.kwonlyargs}
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_latest_event_alerts"
        ):
            feed_calls.append(node)
            assert any(kw.arg == "actor" for kw in node.keywords)

    assert render_signature is not None and "actor" in render_signature
    assert feed_calls

    page = (ROOT / "frontend/page_supply_chain_risk.py").read_text(encoding="utf-8")
    assert "render_risk_overview(actor=principal.username)" in page
