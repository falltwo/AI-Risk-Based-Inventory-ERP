"""Capture six real articles once; replay batch-one acceptance offline with mocked AI.

Never loads provider credentials other than GNEWS_API_KEY, never starts a server
or background scheduler, and never uses the main workspace database.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("gnews", "rss"), default="gnews")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--country", default="美國")
    parser.add_argument("--snapshot", type=Path, help="Replay an earlier six-article capture without network")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = ROOT / ".isolated" / f"live-news-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    db_path = output / "acceptance.db"
    key = os.getenv("GNEWS_API_KEY", "").strip()
    if args.env_file:
        from dotenv import dotenv_values
        key = (dotenv_values(args.env_file).get("GNEWS_API_KEY") or "").strip()
    # All other inherited credentials and paid model configuration are ignored.
    for name in ("GNEWS_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
                 "LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "LLM_MODEL",
                 "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(name, None)
    os.environ.update(ERP_DB_PATH=str(db_path), ERP_DEMO_MODE="1", ERP_ISOLATED_TEST="1",
                      ERP_SCHEDULER_ENABLED="0", ERP_SCHEDULER_ACTOR="planner",
                      LITELLM_LOCAL_MODEL_COST_MAP="True", OTEL_SDK_DISABLED="true")
    from backend import database, supply_chain_news as news, supply_chain_risk as risk, scheduler
    from backend.isolated_runtime import block_external_network
    from backend.news_store import identity_keys
    from backend.job_lock import exclusive_job_lock

    # The only external operation is this explicit news capture. No automatic RSS
    # fallback here: the report must identify which provider actually succeeded.
    try:
        if args.snapshot:
            capture = json.loads(args.snapshot.read_text(encoding="utf-8"))
            articles = capture["articles"]
        else:
            if args.source == "gnews":
                if not key or key.startswith("replace_"):
                    raise ValueError("GNEWS_API_KEY is not configured in the selected file")
                articles = news._fetch_via_gnews_api(args.country, key, max_results=6, within_days=7)
            else:
                articles = news._fetch_via_rss(args.country, max_results=6, within_days=7)
            capture = dict(source=args.source, search_country=args.country,
                           captured_at=datetime.now(timezone.utc).isoformat(), articles=articles)
    except Exception as exc:
        # requests exceptions may contain the API key in their URL; never print them.
        cause = exc.__cause__
        response = getattr(cause, "response", None)
        error = dict(status="capture_failed", source=args.source, error_type=type(exc).__name__,
                     http_status=getattr(response, "status_code", None))
        write_json(output / "capture-error.json", error)
        print(json.dumps(error))
        print(f"Output: {output}")
        return 1
    finally:
        key = ""
        block_external_network()

    assert len(articles) == 6, f"Expected six articles; provider returned {len(articles)}"
    assert len({identity_keys(a)[0] for a in articles}) == 6, "Provider returned duplicate URLs"
    assert all(a.get("title") and a.get("url") and a.get("published_at") for a in articles)
    write_json(output / "news-capture.json", capture)
    database.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE suppliers SET is_official=0")
        for sid, country, region in (("LIVE-N", "台灣", "北區"), ("LIVE-S", "台灣", "南區"), ("LIVE-J", "日本", "北區")):
            conn.execute("INSERT INTO suppliers(supplier_id,name,country,region,latitude,longitude,is_official) VALUES (?,?,?,?,25,121,1)", (sid, sid, country, region))
            conn.execute("INSERT INTO inventory(product_id,name,stock,reorder_point,daily_sales) VALUES (?,?,10,5,3)", (sid, sid))
            conn.execute("INSERT INTO purchase_orders(po_id,supplier_id,status,total_amount) VALUES (?,?,'pending',100)", (sid, sid))
            conn.execute("INSERT INTO purchase_order_items(po_id,product_id,qty,unit_price) VALUES (?,?,1,100)", (sid, sid))

    checks = []
    phases = {}
    def checked(name, condition):
        assert condition, name
        checks.append(name)

    def rows():
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM supply_chain_news ORDER BY id")]

    simulated = {article["title"]: i for i, article in enumerate(articles)}
    mode = "failure"
    analyzed_titles = []
    def mock_complete(prompt, **kwargs):
        if kwargs.get("tag") == "analysis:heatmap":
            return json.dumps({"摘要": "驗收模擬摘要，不代表真實新聞風險。", "更新": [], "事件": []}, ensure_ascii=False)
        if mode == "failure":
            raise RuntimeError("Simulated provider outage")
        result = []
        for news_id, body in re.findall(r"【新聞編號 (\d+)】\n(.*?)(?=【新聞編號|$)", prompt, re.S):
            title = body.splitlines()[0]
            index = simulated[title]
            analyzed_titles.append(title)
            delay = [0, None, 5, 0, "7", -1][index] if mode == "mixed" else 0
            result.append({"news_id": int(news_id), "相關性": "NO" if index == 3 else "YES",
                "國家": "台灣", "地區": "北區", "事件類型": "交通", "預計延遲": delay,
                "繁體中文簡要": f"[驗收模擬，非新聞風險判斷] 測試案例 {index + 1}"})
        return json.dumps({"results": result}, ensure_ascii=False)

    country = capture.get("search_country", args.country)
    with patch.object(news, "fetch_country_news", return_value=deepcopy(articles)), \
         patch("backend.llm_client.complete_text", side_effect=mock_complete):
        with patch("backend.llm_client.llm_available", return_value=False):
            phases["pending"] = news.refresh_news_for_countries([country], actor="planner")
        checked("Six real articles saved as pending with unknown delay", len(rows()) == 6 and all(r["analysis_status"] == "pending" and r["estimated_delay"] is None for r in rows()))
        phases["outage"] = news.refresh_news_for_countries([country], actor="planner")
        checked("Provider failure cannot become a seven-day risk", all(r["analysis_status"] == "failed" and r["is_relevant"] is None and r["estimated_delay"] is None for r in rows()))
        checked("Failed news excluded from risk inputs", not news.get_news_from_db(analyzed_only=True) and risk.get_active_risk_events().empty)
        mode = "mixed"
        with patch.object(news, "fetch_country_news", return_value=deepcopy(articles + articles)):
            phases["strict_validation"] = news.refresh_news_for_countries([country], actor="planner")
        mixed_rows = rows()
        phases["strict_validation_states"] = [dict(id=r["id"], analysis_status=r["analysis_status"], is_relevant=r["is_relevant"], estimated_delay=r["estimated_delay"], analysis_error=r["analysis_error"]) for r in mixed_rows]
        checked("Twelve replayed entries deduplicated before six analyses", phases["strict_validation"]["duplicate_count"] == 12 and len(analyzed_titles) == 6 and len(mixed_rows) == 6)
        checked("Zero, unknown, delay, irrelevant, malformed remain distinct", [r["estimated_delay"] for r in mixed_rows] == [0, None, 5, 0, None, None] and [r["analysis_status"] for r in mixed_rows] == ["succeeded"] * 4 + ["failed"] * 2)
        for source in (mixed_rows[1], mixed_rows[4], mixed_rows[5]):
            try:
                risk.add_risk_event("交通", "北區", "台灣", 7, "Must reject", source["id"], actor="planner")
            except ValueError:
                continue
            raise AssertionError("Unknown or failed news was registered as risk")
        checked("Unknown and failed news cannot register an event", True)
        mode = "recovery"
        analyzed_titles.clear()
        phases["retry"] = news.refresh_news_for_countries([country], actor="planner")
        checked("Only the two failed analyses retry", len(analyzed_titles) == 2 and all(r["analysis_status"] == "succeeded" for r in rows()))
        analyzed_titles.clear()
        phases["deduped_replay"] = news.refresh_news_for_countries([country], actor="planner")
        checked("Successful news is not reanalyzed or inserted twice", not analyzed_titles and phases["deduped_replay"]["saved_count"] == 0 and len(rows()) == 6)
        for raw, stored in zip(articles, rows()):
            assert all(raw.get(k) == stored.get(k) for k in ("title", "summary", "url", "source", "published_at", "country", "region"))
        checked("Original news content preserved through all failures and retries", True)

        cfg = scheduler.SchedulerConfig(actor="planner", max_attempts=2, retry_seconds=0)
        scheduled_calls = []
        def scheduled_refresh(**kwargs):
            scheduled_calls.append(1)
            if len(scheduled_calls) == 1:
                raise RuntimeError("Simulated retry")
            return news.refresh_news_for_countries([country], actor=kwargs["actor"])
        with patch.object(scheduler, "refresh_supply_chain_news_once", side_effect=scheduled_refresh):
            phases["scheduler"] = scheduler.run_scheduled_refresh(cfg, job_key="live-acceptance")
            phases["scheduler_replay"] = scheduler.run_scheduled_refresh(cfg, job_key="live-acceptance")
        checked("One-shot scheduler retries and skips the completed key", len(scheduled_calls) == 2 and phases["scheduler"]["status"] == "succeeded" and phases["scheduler_replay"]["status"] == "skipped")
        with exclusive_job_lock(db_path, "news") as locked:
            checked("Overlapping refresh is blocked", locked and news.refresh_news_for_countries([country], actor="planner")["status"] == "busy")

    checked("Supplier, PO and stockout scope agree on Taiwan north only",
        [r["supplier_id"] for r in risk.get_affected_suppliers_by_event("北區", "台灣")] == ["LIVE-N"]
        and [r["po_id"] for r in risk.get_impacted_pos("北區", "台灣")] == ["LIVE-N"]
        and [r["product_id"] for r in risk.get_stockout_alerts_for_event("北區", "台灣", 5)] == ["LIVE-N"])
    risk.apply_heatmap_updates([dict(display_name="台灣 北區", risk_pct=0, estimated_delay=0)], "[驗收模擬] 零值保存", actor="planner")
    with sqlite3.connect(db_path) as conn:
        checked("Zero percent and zero days survive database reconnect", conn.execute("SELECT risk_pct,estimated_delay FROM risk_heatmap WHERE region_key='台灣|北區'").fetchone() == (0, 0))
    checked("Background scheduling remains disabled", scheduler.start_background_jobs() is False)
    report = dict(status="passed", checked_at=datetime.now(timezone.utc).isoformat(), source=capture["source"],
                  capture_sha256=hashlib.sha256((output / "news-capture.json").read_bytes()).hexdigest(),
                  article_count=6, analysis_mode="mocked; not real news risk assessment", database=str(db_path),
                  checks=checks, phases=phases, articles=[{k:a.get(k) for k in ("title", "url", "source", "published_at")} for a in articles])
    write_json(output / "acceptance-results.json", report)
    print(json.dumps(dict(status="passed", article_count=6, checks_passed=len(checks), source=capture["source"], output=str(output)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
