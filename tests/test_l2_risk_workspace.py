"""
tests/test_l2_risk_workspace.py
L2 供應鏈風險頁完善：
  1. 熱圖預設值依事件嚴重度／筆數／時效差異化（不再全部 60%）
  2. 曝險金額改回傳完整資訊（未結採購單 + 供應商數），demo 可 opt-in 種採購單
  3. 卡片判定納入新聞登錄事件（events_for_location / is_news_event）
  4. 同區域不同類型事件不再互相覆蓋；update_risk_event 就地修改
  5. AI 摘要落地 risk_ai_summaries，L1 唯讀可讀、L2 重開頁面可載回
  6. 證據閘門：AI 提到但資料裡沒有的地區略過、天數／類型超出證據則調整
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from backend import database
from backend import l1_monitoring
from backend import supply_chain_risk as risk


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 13, 12, 0, 0)


@pytest.fixture
def l2_db(tmp_path, monkeypatch):
    db_path = tmp_path / "l2-workspace.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    monkeypatch.setattr(risk, "DB_FILE", str(db_path))
    monkeypatch.delenv("ERP_ENABLE_DEMO_SEED", raising=False)
    database.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM suppliers")
        conn.execute("DELETE FROM purchase_orders")
        conn.execute("DELETE FROM supply_chain_events")
        conn.executemany(
            "INSERT INTO suppliers (supplier_id, name, country, region, latitude, longitude, is_official) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                ("S-TW1", "台北供應商", "台灣", "亞洲", 25.0, 121.5, 1),
                ("S-TW2", "新竹供應商", "台灣", "亞洲", 24.8, 121.0, 0),
                ("S-DE1", "柏林供應商", "德國", "歐洲", 52.5, 13.4, 1),
                ("S-US1", "加州供應商", "美國", "北美洲", 37.7, -122.4, 1),
            ],
        )
        conn.commit()
    return db_path


def _events(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT event_type, region, country, impact_days, news_id FROM supply_chain_events ORDER BY id"
        ).fetchall()


def _heatmap_by_name():
    return {row["display_name"]: row for row in risk.get_risk_heatmap_data()}


# ── 1. 熱圖預設值差異化 ──────────────────────────────────────────────


def test_score_region_events_weights_by_severity_count_and_age():
    def ev(days, created="2026-09-12", country="台灣", region="亞洲"):
        return {"country": country, "region": region, "impact_days": days, "created_at": created}

    assert risk.score_region_events("台灣", "亞洲", [], now=NOW)["points"] == 0
    assert risk.score_region_events("台灣", "亞洲", [ev(7)], now=NOW)["points"] == 30
    assert risk.score_region_events("台灣", "亞洲", [ev(30)], now=NOW)["points"] == 50
    # 兩筆事件：最嚴重者 + 每多一筆 +5
    assert risk.score_region_events("台灣", "亞洲", [ev(7), ev(14)], now=NOW)["points"] == 45
    # 逾 30 天的事件加權減半
    stale = risk.score_region_events("台灣", "亞洲", [ev(30, created="2026-07-01")], now=NOW)
    assert stale["points"] == 25 and "已逾" in stale["reason"]
    # 其他地區的事件不算
    assert risk.score_region_events("德國", "歐洲", [ev(30)], now=NOW)["count"] == 0


def test_heatmap_defaults_no_longer_flat_60(l2_db):
    risk.add_risk_event("罷工", "亞洲", "台灣", 7, "港口罷工", actor="planner")
    risk.add_risk_event("氣候", "亞洲", "台灣", 14, "颱風", actor="planner")
    risk.add_risk_event("戰爭", "歐洲", "德國", 30, "衝突", actor="planner")

    rows = _heatmap_by_name()
    assert rows["台灣 亞洲"]["risk_pct"] == 65      # 20 + 40（14 天）+ 5（第二筆）
    assert rows["德國 歐洲"]["risk_pct"] == 70      # 20 + 50（30 天）
    assert rows["美國 北美洲"]["risk_pct"] == 20    # 沒事件
    assert rows["台灣 亞洲"]["event_count"] == 2
    assert "2 則事件" in rows["台灣 亞洲"]["risk_reason"]
    assert rows["美國 北美洲"]["risk_reason"] == "近期無登錄事件"


def test_heatmap_override_keeps_reason_of_override(l2_db):
    risk.upsert_risk_heatmap("台灣|亞洲", "台灣 亞洲", 25.0, 121.5, 88, "AI", actor="planner")
    row = _heatmap_by_name()["台灣 亞洲"]
    assert row["risk_pct"] == 88 and row["risk_reason"].startswith("AI／人工設定")


# ── 2. 曝險資訊 ───────────────────────────────────────────────────────


def test_region_exposure_explains_zero_amount(l2_db):
    exposure = risk.get_region_exposure("台灣|亞洲")
    assert exposure == {
        "supplier_count": 2, "official_supplier_count": 1,
        "open_po_count": 0, "open_po_amount": 0.0,
    }
    with sqlite3.connect(l2_db) as conn:
        conn.executemany(
            "INSERT INTO purchase_orders (po_id, supplier_id, status, total_amount) VALUES (?,?,?,?)",
            [("PO-1", "S-TW1", "已下單", 1500.0), ("PO-2", "S-TW2", "已完成", 999.0), ("PO-3", "S-DE1", None, 40.0)],
        )
        conn.commit()
    exposure = risk.get_region_exposure("台灣|亞洲")
    assert exposure["open_po_count"] == 1 and exposure["open_po_amount"] == 1500.0  # 已完成不算


def test_demo_purchase_orders_seed_is_opt_in_and_idempotent(l2_db, monkeypatch):
    with sqlite3.connect(l2_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0
    monkeypatch.setenv("ERP_ENABLE_DEMO_SEED", "1")
    database.init_db()
    with sqlite3.connect(l2_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0]
        official = conn.execute("SELECT COUNT(*) FROM suppliers WHERE is_official=1").fetchone()[0]
    assert count == official > 0
    database.init_db()   # 第二次不重複種
    with sqlite3.connect(l2_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == count
    assert risk.get_region_exposure("台灣|亞洲")["open_po_amount"] > 0


# ── 3. 卡片判定納入新聞登錄事件 ───────────────────────────────────────


def test_events_for_location_separates_news_and_formal(l2_db):
    risk.add_risk_event("其他", "", "台灣", 7, "新聞 A", news_id=11, actor="planner")
    risk.add_risk_event("氣候", "", "台灣", 21, "新聞 B", news_id=12, actor="planner")
    risk.add_risk_event("罷工", "亞洲", "台灣", 10, "人工", actor="planner")

    matched = risk.events_for_location("台灣", "亞洲")
    assert [e["impact_days"] for e in matched] == [21, 10, 7]   # 依天數排序
    assert [risk.is_news_event(e) for e in matched] == [True, False, True]
    assert risk.events_for_location("美國", "北美洲") == []


# ── 4. 事件不再互相覆蓋 ───────────────────────────────────────────────


def test_add_risk_event_keeps_different_types_separate(l2_db):
    first = risk.add_risk_event("罷工", "亞洲", "台灣", 7, "罷工", actor="planner")
    second = risk.add_risk_event("氣候", "亞洲", "台灣", 14, "颱風", actor="planner")
    assert first != second
    assert len(_events(l2_db)) == 2
    # 同類型再登錄 → 更新同一筆（天數／說明），不新增
    again = risk.add_risk_event("罷工", "亞洲", "台灣", 9, "罷工延長", actor="planner")
    assert again == first
    assert [(t, d) for t, _, _, d, _ in _events(l2_db)] == [("罷工", 9), ("氣候", 14)]


def test_update_risk_event_changes_only_given_fields(l2_db):
    event_id = risk.add_risk_event("罷工", "亞洲", "台灣", 7, "原始", actor="planner")
    assert risk.update_risk_event(event_id, impact_days=21, actor="planner") is True
    assert _events(l2_db)[0][:4] == ("罷工", "亞洲", "台灣", 21)
    assert risk.update_risk_event(event_id, event_type="戰爭", description="升級", actor="planner") is True
    assert _events(l2_db)[0][0] == "戰爭"
    assert risk.update_risk_event(999999, impact_days=1, actor="planner") is False


@pytest.mark.parametrize("actor", [None, "", "viewer", "nobody"])
def test_update_risk_event_fails_closed(l2_db, actor):
    event_id = risk.add_risk_event("罷工", "亞洲", "台灣", 7, "原始", actor="planner")
    with pytest.raises(PermissionError):
        risk.update_risk_event(event_id, impact_days=99, actor=actor)
    assert _events(l2_db)[0][3] == 7


# ── 5. 摘要落地 ───────────────────────────────────────────────────────


def _fake_llm(monkeypatch, payload: str):
    import backend.llm_client as lc
    monkeypatch.setattr(lc, "complete_text", lambda *a, **kw: payload)


NEWS = [
    {"country": "台灣", "region": "亞洲", "category": "氣候", "estimated_delay": 5,
     "title": "Typhoon", "summary": "port closed", "published_at": "2026-09-12"},
]
PAYLOAD = (
    '{"摘要": "### 現況\\n台灣颱風影響港口。美國戰爭風險上升。", '
    '"更新": [{"地區": "台灣 亞洲", "風險": 70}, {"地區": "美國 北美洲", "風險": 75}], '
    '"事件": [{"類型": "氣候", "地區": "亞洲", "國家": "台灣", "延遲天數": 30, "描述": "颱風"}, '
    '{"類型": "戰爭", "地區": "北美洲", "國家": "美國", "延遲天數": 30, "描述": "戰爭"}]}'
)


def test_analyze_persists_when_actor_given_and_l1_can_read(l2_db, monkeypatch):
    _fake_llm(monkeypatch, PAYLOAD)
    assert risk.get_latest_ai_risk_summary() is None

    result = risk.analyze_heatmap_risk(NEWS, reference_date="2026-09-13", actor="planner")
    assert result["error"] is False and result["summary_id"] is not None
    assert result["news_count"] == 1

    latest = risk.get_latest_ai_risk_summary()
    assert latest["summary_id"] == result["summary_id"]
    assert "台灣颱風" in latest["summary"]
    assert latest["updates"] == result["updates"]
    assert latest["events"] == result["events"]
    assert latest["audit"] == result["audit"]
    assert latest["actor"] == "planner"

    # L1 唯讀入口：viewer 可讀，同一筆
    assert l1_monitoring.get_latest_risk_summary(actor="viewer")["summary_id"] == result["summary_id"]


@pytest.mark.parametrize("actor", [None, "", "hr1", "nobody"])
def test_l1_latest_summary_fails_closed(l2_db, actor):
    with pytest.raises(PermissionError):
        l1_monitoring.get_latest_risk_summary(actor=actor)


def test_analyze_without_actor_does_not_persist_and_viewer_cannot_persist(l2_db, monkeypatch):
    _fake_llm(monkeypatch, PAYLOAD)
    result = risk.analyze_heatmap_risk(NEWS, reference_date="2026-09-13")
    assert result["summary_id"] is None and risk.get_latest_ai_risk_summary() is None
    with pytest.raises(PermissionError):
        risk.save_ai_risk_summary(result, actor="viewer")
    assert risk.get_latest_ai_risk_summary() is None


def test_legacy_tuple_interface_still_works(l2_db, monkeypatch):
    _fake_llm(monkeypatch, PAYLOAD)
    summary, updates, events = risk.get_heatmap_ai_summary(news_context="x", news_items=NEWS)
    assert "台灣颱風" in summary and isinstance(updates, list) and isinstance(events, list)


def test_scheduler_refresh_persists_summary(l2_db, monkeypatch):
    """排程／L2「更新即時新聞」路徑：摘要落地、建議事件不再被丟掉。"""
    from backend import supply_chain_news as news

    _fake_llm(monkeypatch, PAYLOAD)
    monkeypatch.setattr("backend.llm_client.llm_available", lambda: True)
    monkeypatch.setattr(news, "fetch_country_news", lambda *a, **kw: [
        {"country": "台灣", "region": "亞洲", "title": "Typhoon", "summary": "port closed",
         "url": "https://example.test/n", "source": "t", "published_at": "2026-09-12 00:00",
         "relevance_tag": "supply_chain"},
    ])
    monkeypatch.setattr(risk, "batch_infer_affected_region_from_news", lambda **kw: [
        {"is_relevant": True, "estimated_delay": 5, "event_type": "氣候",
         "country": "台灣", "region": "亞洲", "chinese_summary": "颱風"},
    ])
    news.refresh_news_for_countries(["台灣"], actor="planner")
    latest = risk.get_latest_ai_risk_summary()
    assert latest is not None and latest["actor"] == "planner"
    assert latest["events"] and latest["events"][0]["country"] == "台灣"


# ── 6. 證據閘門 ───────────────────────────────────────────────────────


def test_gate_by_evidence_drops_unsupported_and_caps_days(l2_db, monkeypatch):
    _fake_llm(monkeypatch, PAYLOAD)
    # 已登錄事件：台灣 7 天罷工；新聞：台灣 5 天氣候。美國完全沒有依據。
    risk.add_risk_event("罷工", "亞洲", "台灣", 7, "罷工", actor="planner")
    result = risk.analyze_heatmap_risk(NEWS, reference_date="2026-09-13")

    assert [u["display_name"] for u in result["updates"]] == ["台灣 亞洲"]
    assert len(result["events"]) == 1
    tw = result["events"][0]
    assert tw["country"] == "台灣" and tw["event_type"] == "氣候"
    assert tw["impact_days"] == 14   # 證據最長 7 天 × 2

    audit = {(a["kind"], a["name"], a["action"]) for a in result["audit"]}
    assert ("更新", "美國 北美洲", "略過") in audit
    assert ("事件", "美國 北美洲", "略過") in audit
    assert ("事件", "台灣 亞洲", "調整") in audit
    assert "台灣" in result["evidence_locations"] and "美國" not in result["evidence_locations"]


def test_gate_by_evidence_unit_rules():
    evidence = risk.build_risk_evidence(
        [{"country": "越南", "region": "", "category": "政策", "estimated_delay": 3}],
        [{"country": "台灣", "region": "北區", "event_type": "罷工", "impact_days": 10}],
    )
    updates = [{"display_name": "台灣 北區", "risk_pct": 60}, {"display_name": "巴西", "risk_pct": 50}]
    events = [
        {"event_type": "戰爭", "country": "越南", "region": "", "impact_days": 6, "description": ""},
        {"event_type": "罷工", "country": "台灣", "region": "北區", "impact_days": 12, "description": ""},
    ]
    kept_u, kept_e, audit = risk.gate_by_evidence(updates, events, evidence)
    assert [u["display_name"] for u in kept_u] == ["台灣 北區"]
    # 越南：類型「戰爭」沒依據 → 其他；6 天 ≤ max(7, 3×2) 不動
    assert kept_e[0]["event_type"] == "其他" and kept_e[0]["impact_days"] == 6
    # 台灣：12 ≤ 10×2 不動，類型有依據
    assert kept_e[1]["event_type"] == "罷工" and kept_e[1]["impact_days"] == 12
    assert any(a["name"] == "巴西" and a["action"] == "略過" for a in audit)
    assert any(a["name"] == "越南" and a["action"] == "調整" for a in audit)


def test_gate_flags_but_keeps_type_when_evidence_is_unclassified():
    """證據只有「其他」類新聞時不改寫類型（分類品質未知），改成提醒人工確認。"""
    evidence = risk.build_risk_evidence(
        [{"country": "美國", "region": "北美洲", "category": "其他", "estimated_delay": 7}], [],
    )
    events = [{"event_type": "戰爭", "country": "美國", "region": "美國 北美洲", "impact_days": 30, "description": ""}]
    _, kept, audit = risk.gate_by_evidence([], events, evidence)
    assert kept[0]["event_type"] == "戰爭" and kept[0]["impact_days"] == 14   # 7×2 上限
    names = {(a["name"], a["action"]) for a in audit}
    assert ("美國 北美洲", "提醒") in names and ("美國 北美洲", "調整") in names   # 名稱不重複國家


def test_gate_by_evidence_passthrough_without_evidence():
    updates = [{"display_name": "火星", "risk_pct": 99}]
    events = [{"event_type": "戰爭", "country": "火星", "region": "", "impact_days": 90}]
    assert risk.gate_by_evidence(updates, events, risk.build_risk_evidence([], [])) == (updates, events, [])


def test_prompt_tells_model_to_stay_within_evidence():
    from backend import prompts
    assert "不要自行推測" in prompts.HEATMAP_AI_SUMMARY_PROMPT_V2


# ── 前端契約（AST） ───────────────────────────────────────────────────


def _calls(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == name)
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]


def test_supply_map_uses_structured_summary_and_forwards_actor():
    tree = ast.parse((ROOT / "frontend/components/supply_map.py").read_text(encoding="utf-8"))
    analyze = _calls(tree, "analyze_heatmap_risk")
    assert analyze, "L2 應改用 analyze_heatmap_risk 取得 audit 與落地"
    assert all(any(k.arg == "actor" for k in c.keywords) for c in analyze)
    update = _calls(tree, "update_risk_event")
    assert update and all(any(k.arg == "actor" for k in c.keywords) for c in update)
    assert _calls(tree, "get_latest_ai_risk_summary"), "重開頁面要載回最近一次摘要"
    assert not _calls(tree, "get_total_impact_amount"), "卡片改用 get_region_exposure"
    for c in _calls(tree, "add_risk_event"):
        assert any(k.arg == "actor" for k in c.keywords)


def test_risk_overview_shows_persisted_summary_read_only():
    src = (ROOT / "frontend/components/risk_overview.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = _calls(tree, "get_latest_risk_summary")
    assert calls and all(any(k.arg == "actor" for k in c.keywords) for c in calls)
    assert "analyze_heatmap_risk" not in src   # L1 不觸發模型


def test_country_event_does_not_light_up_whole_region(l2_db):
    risk.add_risk_event("罷工", "亞洲", "台灣", 7, "台灣罷工", actor="planner")
    assert risk.events_for_location("台灣", "亞洲")
    assert risk.events_for_location("越南", "亞洲") == []          # 同大區域、不同國家
    # 純大區域事件（沒有國家）才會擴散到該區所有據點
    risk.add_risk_event("戰爭", "亞洲", "", 30, "區域衝突", actor="planner")
    assert len(risk.events_for_location("越南", "亞洲")) == 1
    assert len(risk.events_for_location("台灣", "亞洲")) == 2
