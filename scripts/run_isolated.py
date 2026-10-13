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
    parser.add_argument("--integration-demo", action="store_true", help="Seed deterministic local PO and supplier offers for L1/L2/L3 review")
    parser.add_argument("--scheduler-once", metavar="KEY")
    parser.add_argument("--port", type=int, default=8511)
    parser.add_argument("--scenario", choices=("mixed", "success"), default="mixed")
    parser.add_argument("--acceptance-dir", type=Path,
                        help="Show a real-news acceptance snapshot in its own review database")
    args = parser.parse_args()
    os.chdir(ROOT)
    # Always pick a local test DB; never inherit a user's production database setting.
    db_name = "erp-batch1.db" if args.scenario == "mixed" else "erp-batch1-success.db"
    db_path = ROOT / ".isolated" / db_name
    db_path.parent.mkdir(exist_ok=True)
    os.environ.pop("ERP_NEWS_CAPTURE", None)
    os.environ.pop("ERP_NEWS_ACCEPTANCE", None)
    if args.acceptance_dir:
        import sqlite3
        acceptance_dir = args.acceptance_dir.resolve()
        if not acceptance_dir.is_relative_to((ROOT / ".isolated").resolve()):
            parser.error("Acceptance directory must be inside this worktree's .isolated folder")
        capture_path = acceptance_dir / "news-capture.json"
        report_path = acceptance_dir / "acceptance-results.json"
        original_db = acceptance_dir / "acceptance.db"
        if not all(p.is_file() for p in (capture_path, report_path, original_db)):
            parser.error("A complete news capture, acceptance report and database are required")
        db_path = acceptance_dir / "preview.db"
        if not db_path.exists():
            with sqlite3.connect(str(original_db)) as source, sqlite3.connect(str(db_path)) as target:
                source.backup(target)
        os.environ.update(ERP_NEWS_CAPTURE=str(capture_path), ERP_NEWS_ACCEPTANCE=str(report_path))
    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GNEWS_API_KEY", "LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(key, None)
    os.environ["ERP_ISOLATED_SCENARIO"] = args.scenario
    os.environ.update(ERP_DB_PATH=str(db_path), ERP_DEMO_MODE="1", ERP_ENABLE_DEMO_SEED="0", ERP_ISOLATED_TEST="1",
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
        if args.acceptance_dir:
            print("Real news snapshot loaded; analysis remains mocked; acceptance evidence is preserved.")
        else:
            seed_preview_suppliers(conn)
            if args.integration_demo:
                seed_integration_demo(conn)
    print(f"ISOLATED ERP_DB_PATH={db_path}")
    print("News replay/LLM fixtures; external network blocked; background scheduler disabled.")
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


def seed_preview_suppliers(conn):
    conn.execute("UPDATE suppliers SET is_official=0 WHERE supplier_id NOT LIKE 'BATCH1-%'")
    for sid, country, region, lat, lon in (("BATCH1-TWN","台灣","北區",25.03,121.56),("BATCH1-TWS","台灣","南區",22.63,120.30),("BATCH1-JP","日本","東京",35.68,139.69)):
        conn.execute("INSERT OR IGNORE INTO suppliers(supplier_id,name,country,region,latitude,longitude,is_official) VALUES (?,?,?,?,?,?,1)", (sid,f"測試供應商 {country} {region}",country,region,lat,lon))
        conn.execute("UPDATE suppliers SET is_official=1 WHERE supplier_id=?", (sid,))


def seed_integration_demo(conn):
    """Only invoked by the network-blocked launcher in this worktree's DB."""
    conn.execute("INSERT OR IGNORE INTO inventory(product_id,name,stock,price,cost,reorder_point) VALUES('INTEGRATION-ITEM','整合驗收物料',100,120,100,20)")
    for sid, price in (("BATCH1-JP",100),("BATCH1-TWN",110),("BATCH1-TWS",115)):
        if not conn.execute("SELECT 1 FROM supplier_products WHERE supplier_id=? AND product_id='INTEGRATION-ITEM'",(sid,)).fetchone():
            conn.execute("INSERT INTO supplier_products(supplier_id,product_id,price,carbon_factor) VALUES(?,'INTEGRATION-ITEM',?,1)",(sid,price))
    conn.execute("INSERT OR IGNORE INTO purchase_orders(po_id,supplier_id,order_date,status,total_amount,note) VALUES('INTEGRATION-PO-JP','BATCH1-JP',date('now'),'已下單',1000,'固定隔離驗收資料')")
    if not conn.execute("SELECT 1 FROM purchase_order_items WHERE po_id='INTEGRATION-PO-JP'").fetchone():
        conn.execute("INSERT INTO purchase_order_items(po_id,product_id,qty,unit_price) VALUES('INTEGRATION-PO-JP','INTEGRATION-ITEM',10,100)")

if __name__ == "__main__":
    raise SystemExit(main())
