"""python scripts/run_isolated.py [--seed-only | --scheduler-once KEY] [--port 8511]"""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-only", action="store_true")
    parser.add_argument("--scheduler-once", metavar="KEY")
    parser.add_argument("--port", type=int, default=8511)
    parser.add_argument("--scenario", choices=("mixed", "success"), default="mixed")
    args = parser.parse_args()
    os.chdir(ROOT)
    # Always pick a local test DB; never inherit a user's production database setting.
    db_name = "erp-batch1.db" if args.scenario == "mixed" else "erp-batch1-success.db"
    db_path = ROOT / ".isolated" / db_name
    db_path.parent.mkdir(exist_ok=True)
    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GNEWS_API_KEY", "LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(key, None)
    os.environ["ERP_ISOLATED_SCENARIO"] = args.scenario
    os.environ.update(ERP_DB_PATH=str(db_path), ERP_DEMO_MODE="1", ERP_ISOLATED_TEST="1",
                      ERP_SCHEDULER_ENABLED="0", ERP_SCHEDULER_ACTOR="planner",
                      STREAMLIT_BROWSER_GATHER_USAGE_STATS="false", LITELLM_LOCAL_MODEL_COST_MAP="True",
                      OTEL_SDK_DISABLED="true")
    from backend.isolated_runtime import block_external_network
    block_external_network()
    from backend.database import init_db
    init_db()
    # Stable official nodes for review, independent of the legacy random demo seed.
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE suppliers SET is_official=0 WHERE supplier_id NOT LIKE 'BATCH1-%'")
        for sid, country, region, lat, lon in (("BATCH1-TWN","台灣","北區",25.03,121.56),("BATCH1-TWS","台灣","南區",22.63,120.30),("BATCH1-JP","日本","東京",35.68,139.69)):
            conn.execute("INSERT OR IGNORE INTO suppliers(supplier_id,name,country,region,latitude,longitude,is_official) VALUES (?,?,?,?,?,?,1)", (sid,f"測試供應商 {country} {region}",country,region,lat,lon))
    print(f"ISOLATED ERP_DB_PATH={db_path}")
    print("Fixed news/LLM fixtures; external network blocked; background scheduler disabled.")
    if args.scheduler_once:
        from backend.scheduler import SchedulerConfig, run_scheduled_refresh
        result = run_scheduled_refresh(SchedulerConfig(actor="planner", max_attempts=2, retry_seconds=0), job_key=args.scheduler_once)
        print(result)
        return 1 if result["status"] in {"failed", "busy", "cancelled"} else 0
    elif not args.seed_only:
        from streamlit.web import cli
        sys.argv = ["streamlit", "run", str(ROOT / "app.py"), "--server.address=127.0.0.1",
                    f"--server.port={args.port}", "--server.headless=true", "--browser.gatherUsageStats=false"]
        cli.main()


if __name__ == "__main__":
    raise SystemExit(main())
