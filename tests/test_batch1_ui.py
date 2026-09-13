import sqlite3

from streamlit.testing.v1 import AppTest

from backend import database, supply_chain_risk as risk


def prepare(tmp_path, monkeypatch):
    path = str(tmp_path / "ui.db")
    monkeypatch.setattr(database, "DB_FILE", path)
    monkeypatch.setattr(risk, "DB_FILE", path)
    database.init_db()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM suppliers")
        conn.execute("INSERT INTO suppliers(supplier_id,name,country,region,latitude,longitude,is_official) VALUES ('UI','UI','台灣','北區',25,121,1)")
    monkeypatch.setattr("backend.llm_client.complete_text", lambda *a, **k:
        '{"摘要":"測試摘要","更新":[{"地區":"台灣 北區","風險":0}],'
        '"事件":[{"類型":"交通","國家":"台灣","地區":"北區","延遲天數":0,"描述":"零延遲"}]}')
    return path


def test_streamlit_generate_apply_and_new_session_reload(tmp_path, monkeypatch):
    path = prepare(tmp_path, monkeypatch)
    script = "from frontend.components.supply_map import render_supply_chain_map\nrender_supply_chain_map('', '', actor='planner')"
    at = AppTest.from_string(script, default_timeout=20).run()
    assert not at.exception
    at.button(key="heatmap_ai_btn").click().run()
    assert not at.exception
    assert not at.button(key="apply_ai_risk_btn").disabled
    at.button(key="apply_ai_risk_btn").click().run()
    assert not at.exception
    assert any("至資料庫" in s.value for s in at.success)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT risk_pct,estimated_delay FROM risk_heatmap WHERE region_key='台灣|北區'").fetchone() == (0,0)
    fresh = AppTest.from_string(script, default_timeout=20).run()
    assert not fresh.exception
    rows = risk.get_risk_heatmap_data()
    assert rows[0]["risk_pct"] == 0 and rows[0]["estimated_delay"] == 0


def test_streamlit_failed_news_shows_original_and_disables_registration(tmp_path, monkeypatch):
    path = prepare(tmp_path, monkeypatch)
    with sqlite3.connect(path) as conn:
        conn.execute("""INSERT INTO supply_chain_news(title,summary,country,source,published_at,
            analysis_status,analysis_error,is_relevant,estimated_delay)
            VALUES ('Failed fixture','Original body retained','台灣','fixture',date('now'),
                    'failed','provider_error',NULL,NULL)""")
    at = AppTest.from_string("from frontend.components.risk_dashboard import render_intelligence_gathering\nrender_intelligence_gathering(actor='planner')", default_timeout=20).run()
    assert not at.exception
    buttons = [b for b in at.button if "登錄" in b.label]
    assert len(buttons) == 2 and all(b.disabled for b in buttons)
    assert any("未知" in c.value and "failed" in c.value for c in at.caption)
    assert any("Original body retained" in m.value for m in at.markdown)


def test_real_news_snapshot_is_visible_and_replay_uses_captured_items(tmp_path, monkeypatch):
    import json
    from backend.isolated_runtime import fixture_news
    path = prepare(tmp_path, monkeypatch)
    articles = [dict(country="美國", title=f"Captured article {i}", summary=f"Source description {i}",
                     url=f"https://publisher.test/article-{i}", source="Publisher", published_at="2026-09-13 10:00") for i in range(6)]
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(json.dumps(dict(source="gnews", captured_at="2026-09-13T10:01:00Z", articles=articles)), encoding="utf-8")
    report_path = tmp_path / "results.json"
    report_path.write_text(json.dumps(dict(checks=[str(i) for i in range(14)], phases={})), encoding="utf-8")
    monkeypatch.setenv("ERP_ISOLATED_TEST", "1")
    monkeypatch.setenv("ERP_NEWS_CAPTURE", str(capture_path))
    monkeypatch.setenv("ERP_NEWS_ACCEPTANCE", str(report_path))
    from backend.supply_chain_news import save_news_to_db
    assert save_news_to_db(articles) == 6
    assert fixture_news("美國") == articles
    assert fixture_news("日本") == []
    at = AppTest.from_string("from frontend.components.news_acceptance import render_news_acceptance\nrender_news_acceptance()", default_timeout=20).run()
    assert not at.exception
    assert any("GNews API" in item.value and "模擬" in item.value for item in at.info)
    assert [m.value for m in at.metric] == ["6", "6", "14 項通過"]
    assert len(at.dataframe[0].value) == 6
    assert at.dataframe[0].value["新聞標題"].str.startswith("Captured article").all()
    at = AppTest.from_string("from frontend.components.risk_dashboard import render_intelligence_gathering\nrender_intelligence_gathering(actor='planner')", default_timeout=20).run()
    assert not at.exception
    assert at.button(key="refresh_news_btn").label == "🔁 重播本批真實新聞"
