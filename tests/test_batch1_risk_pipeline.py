import json
import sqlite3
from datetime import datetime

import pandas as pd
import pytest

from backend import database, supply_chain_news as news, supply_chain_risk as risk
from backend.news_store import migrate, store_raw, store_analysis, identity_keys
from backend.region_matching import matches_location, connect_db, expanded_region_where
from backend.risk_validation import parse_news_batch, number


@pytest.fixture
def risk_db(tmp_path, monkeypatch):
    path = str(tmp_path / "batch1.db")
    monkeypatch.setattr(database, "DB_FILE", path)
    monkeypatch.setattr(risk, "DB_FILE", path)
    database.init_db()
    with sqlite3.connect(path) as conn:
        for table in ("suppliers", "purchase_orders", "purchase_order_items", "inventory", "supply_chain_events", "risk_heatmap", "esg_risk_factors"):
            conn.execute(f"DELETE FROM {table}")
        for i, (country, region) in enumerate((("台灣", "北區"), ("台灣", "南區"), ("日本", "北區"), ("阿聯酋", "杜拜"))):
            conn.execute("INSERT INTO suppliers(supplier_id,name,country,region,is_official,latitude,longitude) VALUES (?,?,?,?,1,25,121)", (f"S{i}", f"S{i}", country, region))
            conn.execute("INSERT INTO inventory(product_id,name,stock,reorder_point,daily_sales) VALUES (?,?,10,5,3)", (f"P{i}", f"P{i}"))
            conn.execute("INSERT INTO purchase_orders(po_id,supplier_id,status,total_amount) VALUES (?,?, 'pending',100)", (f"PO{i}", f"S{i}"))
            conn.execute("INSERT INTO purchase_order_items(po_id,product_id,qty,unit_price) VALUES (?,?,1,100)", (f"PO{i}", f"P{i}"))
    return path


def item(**kwargs):
    return dict(dict(country="台灣", region="北區", title="Original headline", summary="Original news body",
                url="https://example.test/news", source="fixture", published_at=datetime.now().strftime("%Y-%m-%d %H:%M")), **kwargs)


def response(idx=0, **kwargs):
    return dict(dict(news_id=idx, 相關性="YES", 國家="台灣", 地區="北區", 事件類型="交通", 預計延遲=0, 繁體中文簡要="分析摘要"), **kwargs)


def mock_llm(monkeypatch, rows):
    def complete(prompt, **kwargs):
        if kwargs.get("tag") == "analysis:heatmap":
            return '{"摘要":"有效摘要","更新":[],"事件":[]}'
        return json.dumps({"results": rows}, ensure_ascii=False)
    monkeypatch.setattr("backend.llm_client.complete_text", complete)
    monkeypatch.setattr("backend.llm_client.llm_available", lambda: True)


@pytest.mark.parametrize("bad", [True, False, "0", "55%", -1, 366, 0.5, float("nan"), float("inf"), [], {}])
def test_delay_rejects_invalid_values(bad):
    with pytest.raises(ValueError):
        number(bad, maximum=365, integer=True)


@pytest.mark.parametrize("value", [0, None, 365])
def test_zero_unknown_and_known_remain_distinct(value):
    rows = parse_news_batch(json.dumps({"results": [response(預計延遲=value)]}), 1)
    assert rows[0]["analysis_status"] == "succeeded"
    assert rows[0]["estimated_delay"] == value


@pytest.mark.parametrize("rows", [[response(news_id=True)], [response(news_id="0")], [response(news_id=2)], [response(), response()], [None]])
def test_batch_rejects_ambiguous_identifiers(rows):
    with pytest.raises(ValueError):
        parse_news_batch(json.dumps({"results": rows}), 1)


def test_partial_output_marks_missing_item_failed():
    results = parse_news_batch(json.dumps({"results": [response()]}), 2)
    assert results[0]["estimated_delay"] == 0
    assert results[1]["analysis_status"] == "failed"
    assert results[1]["estimated_delay"] is None
    assert results[1]["is_relevant"] is None


@pytest.mark.parametrize("changes", [{"相關性": "maybe"}, {"事件類型": "typo"}, {"國家": 42}, {"預計延遲": "7"}, {"繁體中文簡要": None}])
def test_bad_fields_fail_without_inventing_risk(changes):
    result = parse_news_batch(json.dumps({"results": [response(**changes)]}), 1)[0]
    assert result["analysis_status"] == "failed"
    assert result["estimated_delay"] is None and result["is_relevant"] is None


def test_provider_failure_retains_raw_content_and_cannot_create_risk(risk_db, monkeypatch):
    monkeypatch.setattr(news, "fetch_country_news", lambda *a, **k: [item()])
    monkeypatch.setattr("backend.llm_client.llm_available", lambda: True)
    def fail(*a, **k):
        raise RuntimeError("paid-provider-secret must not become news")
    monkeypatch.setattr("backend.llm_client.complete_text", fail)
    result = news.refresh_news_for_countries(["台灣"], actor="planner")
    assert result["failed_count"] == 1 and result["status"] == "partial_failure"
    row = news.get_news_from_db()[0]
    assert row["summary"] == "Original news body" and row["analysis_summary"] is None
    assert row["estimated_delay"] is None and row["is_relevant"] is None
    assert row["analysis_error"] == "provider_error"
    assert news.get_news_from_db(analyzed_only=True) == []
    assert risk.get_active_risk_events().empty
    with pytest.raises(ValueError):
        risk.add_risk_event("交通", "北區", "台灣", 7, "bad", row["id"], actor="planner")
    with sqlite3.connect(risk_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM risk_heatmap").fetchone()[0] == 0


def test_dedupe_before_analysis_across_countries_and_refreshes(risk_db, monkeypatch):
    mock_llm(monkeypatch, [response()])
    observed = []
    original = risk.batch_infer_affected_region_from_news
    def infer(**kwargs):
        observed.extend(kwargs["news_texts"])
        return original(**kwargs)
    monkeypatch.setattr(risk, "batch_infer_affected_region_from_news", infer)
    monkeypatch.setattr(news, "fetch_country_news", lambda *a, **k: [item(), item(url="https://example.test/news?utm_source=test#top"), item(url="https://other.test/syndicated")])
    result = news.refresh_news_for_countries(["台灣", "Taiwan", "日本"], actor="planner")
    assert result["saved_count"] == 1 and result["duplicate_count"] == 5
    assert len(observed) == 1
    assert news.refresh_news_for_countries(["日本"], actor="planner")["saved_count"] == 0
    assert len(observed) == 1
    row = news.get_news_from_db()[0]
    assert row["summary"] == "Original news body" and row["analysis_summary"] == "分析摘要"
    assert row["estimated_delay"] == 0


def test_failed_retained_news_retries_even_if_fetch_no_longer_returns_it(risk_db, monkeypatch):
    mock_llm(monkeypatch, [response(預計延遲="invalid")])
    monkeypatch.setattr(news, "fetch_country_news", lambda *a, **k: [item()])
    assert news.refresh_news_for_countries(["台灣"], actor="planner")["failed_count"] == 1
    mock_llm(monkeypatch, [response(預計延遲=5)])
    monkeypatch.setattr(news, "fetch_country_news", lambda *a, **k: [])
    result = news.refresh_news_for_countries(["台灣"], actor="planner")
    assert result["saved_count"] == 0 and result["analyzed_count"] == 1
    assert news.get_news_from_db()[0]["estimated_delay"] == 5


def test_no_model_retains_pending_and_empty_countries_is_safe(risk_db, monkeypatch):
    monkeypatch.setattr("backend.llm_client.llm_available", lambda: False)
    monkeypatch.setattr(news, "fetch_country_news", lambda *a, **k: [item()])
    assert news.refresh_news_for_countries([], actor="planner")["fetched_count"] == 0
    assert news.refresh_news_for_countries(["台灣"], actor="planner")["pending_count"] == 1
    assert news.get_news_from_db()[0]["analysis_status"] == "pending"
    assert not news.get_news_from_db(analyzed_only=True)


def test_fetch_error_is_retryable_not_empty_success(risk_db, monkeypatch):
    monkeypatch.setattr("backend.llm_client.llm_available", lambda: False)
    def fail(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(news, "fetch_country_news", fail)
    result = news.refresh_news_for_countries(["台灣"], actor="planner")
    assert result["status"] == "partial_failure" and result["fetch_failed_count"] == 1


def test_migration_preserves_duplicates_and_ids(tmp_path):
    with sqlite3.connect(tmp_path / "old.db") as conn:
        conn.execute("CREATE TABLE supply_chain_news(id INTEGER PRIMARY KEY,title TEXT,url TEXT,source TEXT,published_at TEXT,summary TEXT)")
        conn.execute("CREATE TABLE risk_heatmap(region_key TEXT PRIMARY KEY)")
        for i in (10, 20):
            conn.execute("INSERT INTO supply_chain_news VALUES (?,'same','https://test/','test','2026-09-13','original')", (i,))
        migrate(conn)
        migrate(conn)
        assert conn.execute("SELECT id,summary,analysis_status FROM supply_chain_news ORDER BY id").fetchall() == [(10,"original","legacy_unverified"),(20,"original","legacy_unverified")]
        assert conn.execute("SELECT COUNT(url_key) FROM supply_chain_news").fetchone()[0] == 1


@pytest.mark.parametrize("region,country,expected", [("北區", "台灣", ["S0"]), ("台灣 北區", None, ["S0"]), ("台灣|北區", None, ["S0"]), ("台灣", None, ["S0","S1"]), ("東亞", None, ["S0","S1","S2"]), ("中東", None, ["S3"]), ("阿拉伯聯合大公國", None, ["S3"]), ("台灣，日本", None, ["S0","S1","S2"]), ("%", None, []), ("灣", None, []), (None,None,[])])
def test_all_risk_consumers_match_identical_geography(risk_db, region, country, expected):
    suppliers = risk.get_affected_suppliers_by_event(region, country)
    pos = risk.get_impacted_pos(region, country)
    stock = risk.get_stockout_alerts_for_event(region, country, 5)
    assert sorted(s["supplier_id"] for s in suppliers) == expected
    if region or country:
        assert sorted(p["po_id"] for p in pos) == [s.replace("S","PO") for s in expected]
    assert sorted(p["product_id"] for p in stock) == [s.replace("S","P") for s in expected]
    with connect_db(risk_db) as conn:
        where, params = expanded_region_where(region, country)
        sql_ids = [r[0] for r in conn.execute(f"SELECT supplier_id FROM suppliers WHERE {where[0]} ORDER BY supplier_id", params)]
        python_ids = [r[0] for r in conn.execute("SELECT supplier_id,country,region FROM suppliers ORDER BY supplier_id") if matches_location(r[1],r[2],region,country)]
    assert sql_ids == python_ids == expected


def test_spaces_aliases_and_country_region_intersection():
    assert matches_location("United States", "West", "United States West")
    assert not matches_location("United States", "East", "United States West")
    assert matches_location("韓國", "首爾", "南韓")
    assert not matches_location("加拿大", "北美", "美國")
    assert not matches_location("日本", "北區", "北區", "台灣")


def test_zero_risk_and_delay_persist_atomically_and_reload(risk_db):
    before = risk.get_risk_heatmap_data()
    review = risk.build_heatmap_review_rows([{"display_name":"台灣 北區","risk_pct":0}],
        [{"country":"台灣","region":"北區","impact_days":0}], before)
    assert review == [{"套用":True,"地區":"台灣 北區","預估風險 (%)":0.0,"預估延遲 (天)":0}]
    assert risk.apply_heatmap_updates([dict(display_name="台灣 北區",risk_pct=0,estimated_delay=0)], "zero", actor="planner") == 1
    reloaded = {r["region_key"]: r for r in risk.get_risk_heatmap_data()}
    assert reloaded["台灣|北區"]["risk_pct"] == 0 and reloaded["台灣|北區"]["estimated_delay"] == 0
    assert reloaded["台灣|南區"]["risk_pct"] > 0
    with pytest.raises(ValueError):
        risk.apply_heatmap_updates([dict(display_name="台灣 北區",risk_pct=55),dict(display_name="日本",risk_pct=float("nan"))], actor="planner")
    assert risk.get_risk_heatmap_data()[0]["risk_pct"] == 0
    risk.apply_heatmap_updates([dict(display_name="台灣 北區",risk_pct=10,estimated_delay=None)], actor="planner")
    assert risk.get_risk_heatmap_data()[0]["estimated_delay"] is None


def test_zero_news_can_register_and_unknown_cannot(risk_db, monkeypatch):
    mock_llm(monkeypatch, [response()])
    monkeypatch.setattr(news,"fetch_country_news",lambda *a,**k:[item()])
    news.refresh_news_for_countries(["台灣"],actor="planner")
    row = news.get_news_from_db()[0]
    event_id = risk.add_risk_event("交通","北區","台灣",0,"zero",row["id"],actor="planner")
    with sqlite3.connect(risk_db) as conn:
        assert conn.execute("SELECT impact_days FROM supply_chain_events WHERE id=?",(event_id,)).fetchone()[0] == 0
        conn.execute("UPDATE supply_chain_news SET estimated_delay=NULL WHERE id=?",(row["id"],))
    with pytest.raises(ValueError):
        risk.add_risk_event("交通","北區","台灣",0,"unknown",row["id"],actor="planner")


def test_heatmap_transaction_rolls_back_on_storage_failure(risk_db):
    risk.apply_heatmap_updates([dict(display_name="台灣 北區",risk_pct=0,estimated_delay=0)],actor="planner")
    with sqlite3.connect(risk_db) as conn:
        conn.execute("CREATE TRIGGER reject_japan BEFORE INSERT ON risk_heatmap WHEN NEW.region_key='日本|北區' BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        risk.apply_heatmap_updates([dict(display_name="台灣 北區",risk_pct=25,estimated_delay=3),dict(display_name="日本",risk_pct=80,estimated_delay=8)],actor="planner")
    with sqlite3.connect(risk_db) as conn:
        assert conn.execute("SELECT risk_pct,estimated_delay FROM risk_heatmap").fetchall() == [(0,0)]


def test_manual_refresh_obeys_shared_pipeline_lock(risk_db,monkeypatch):
    from backend.job_lock import exclusive_job_lock
    monkeypatch.setattr(news,"fetch_country_news",lambda *a,**k:pytest.fail("Duplicate fetch"))
    with exclusive_job_lock(risk_db,"news") as acquired:
        assert acquired
        assert news.refresh_news_for_countries(["台灣"],actor="planner")["status"] == "busy"


def test_legacy_unverified_source_events_are_not_risk_inputs(risk_db):
    with sqlite3.connect(risk_db) as conn:
        nid = conn.execute("INSERT INTO supply_chain_news(title,summary,analysis_status) VALUES ('old','previous content','legacy_unverified')").lastrowid
        conn.execute("INSERT INTO supply_chain_events(event_type,country,region,impact_days,news_id) VALUES ('交通','台灣','北區',7,?)",(nid,))
    assert risk.get_recent_events_for_delay().empty
    assert risk.get_active_risk_events().empty
    assert not risk.get_historical_event_precedents()
    with sqlite3.connect(risk_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM supply_chain_events").fetchone()[0] == 1


def test_heatmap_invalid_numeric_output_is_explicit_failure(risk_db,monkeypatch):
    monkeypatch.setattr("backend.llm_client.complete_text",lambda *a,**k:'{"摘要":"bad","更新":[{"地區":"台灣","風險":101}],"事件":[]}')
    result = risk.get_heatmap_ai_analysis(news_context="fixture")
    assert result["analysis_status"] == "failed"
    assert result["updates"] == [] and result["events"] == []


def test_exact_nodes_outside_country_dictionary_can_be_reviewed_and_saved(risk_db,monkeypatch):
    with sqlite3.connect(risk_db) as conn:
        conn.execute("INSERT INTO suppliers(supplier_id,name,country,region,is_official,latitude,longitude) VALUES ('BR','BR','巴西','聖保羅',1,-23,-46)")
    monkeypatch.setattr("backend.llm_client.complete_text",lambda *a,**k:'{"摘要":"test","更新":[{"地區":"巴西","風險":0}],"事件":[{"類型":"交通","國家":"巴西","地區":"聖保羅","延遲天數":0,"描述":"test"}]}')
    result = risk.get_heatmap_ai_analysis(news_context="fixture")
    assert result["updates"] == [{"display_name":"巴西 聖保羅","risk_pct":0}]
    assert len(result["events"]) == 1
    assert risk.apply_heatmap_updates(result["updates"],actor="planner") == 1
    assert matches_location("Czech Republic","Prague","Czech Republic Prague")
