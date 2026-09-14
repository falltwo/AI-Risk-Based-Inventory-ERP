"""
backend/scheduler.py
背景任務排程器：定時自動抓取最新供應鏈新聞。
"""

import os
import json
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
from backend.access_control import RISK_WORKSPACE_WRITE, require_capability
from backend.supply_chain_news import refresh_news_for_countries
from backend.supply_chain_risk import get_suppliers_for_map

# 記錄排程器是否已啟動，避免重複執行
_scheduler_started = False


def refresh_supply_chain_news_once(*, actor: str) -> dict:
    """Run one authorized refresh; the actor is checked live by the service."""
    actor = str(actor or "").strip()
    if not actor:
        raise PermissionError("背景新聞刷新需要 ERP_SCHEDULER_ACTOR")
    require_capability(actor, RISK_WORKSPACE_WRITE)

    suppliers = get_suppliers_for_map()
    countries = []
    if suppliers is not None and not suppliers.empty and "country" in suppliers.columns:
        countries = suppliers["country"].dropna().unique().tolist()
        countries = [str(country).strip() for country in countries if str(country).strip()]
    if not countries:
        countries = ["台灣", "日本", "美國", "南韓", "中國", "越南", "墨西哥"]

    return refresh_news_for_countries(
        countries,
        max_per_country=5,
        actor=actor,
    )

@dataclass(frozen=True)
class SchedulerConfig:
    actor: str
    enabled: bool = False
    interval_seconds: int = 86400
    initial_delay_seconds: int = 10
    max_attempts: int = 3
    retry_seconds: int = 30

    def __post_init__(self):
        if self.interval_seconds < 1 or self.initial_delay_seconds < 0 or self.max_attempts < 1 or self.retry_seconds < 0:
            raise ValueError("Invalid scheduler timing or retry configuration")

    @classmethod
    def from_env(cls):
        return cls(actor=os.getenv("ERP_SCHEDULER_ACTOR", "").strip(),
                   enabled=os.getenv("ERP_SCHEDULER_ENABLED", "0") == "1",
                   interval_seconds=int(os.getenv("ERP_SCHEDULER_INTERVAL_SECONDS", "86400")),
                   initial_delay_seconds=int(os.getenv("ERP_SCHEDULER_INITIAL_DELAY_SECONDS", "10")),
                   max_attempts=int(os.getenv("ERP_SCHEDULER_MAX_ATTEMPTS", "3")),
                   retry_seconds=int(os.getenv("ERP_SCHEDULER_RETRY_SECONDS", "30")))


def run_scheduled_refresh(config=None, *, job_key=None, wait=None):
    """Explicit one-shot entry, with cross-process locking and durable idempotency.

    Same key + success => skip. Failures retry up to max_attempts per invocation;
    an explicit rerun of a failed key is allowed. OS lock permits crash recovery.
    """
    from .database import DB_FILE
    from .job_lock import exclusive_job_lock
    config = config or SchedulerConfig.from_env()
    require_capability(config.actor, RISK_WORKSPACE_WRITE)
    job_key = job_key or f"news:{int(time.time()) // config.interval_seconds}"
    wait = wait or time.sleep
    with exclusive_job_lock(DB_FILE, "scheduler") as acquired:
        if not acquired:
            return {"status": "busy", "job_key": job_key}
        with sqlite3.connect(DB_FILE) as conn:
            row = conn.execute("SELECT status FROM scheduled_jobs WHERE job_key=?", (job_key,)).fetchone()
            if row and row[0] == "succeeded":
                return {"status": "skipped", "job_key": job_key}
            conn.execute("INSERT OR IGNORE INTO scheduled_jobs(job_key,status) VALUES (?,'pending')", (job_key,))
        for attempt in range(config.max_attempts):
            with sqlite3.connect(DB_FILE) as conn:
                conn.execute("UPDATE scheduled_jobs SET status='running',attempts=attempts+1,started_at=?,finished_at=NULL,error=NULL WHERE job_key=?", (_utcnow(), job_key))
            result = None
            try:
                result = refresh_supply_chain_news_once(actor=config.actor)
                if result.get("status") in {"busy", "partial_failure", "pending_analysis"}:
                    raise RuntimeError(result["status"])
            except Exception as exc:
                error = type(exc).__name__  # Do not persist credentials/provider payloads.
                with sqlite3.connect(DB_FILE) as conn:
                    conn.execute("UPDATE scheduled_jobs SET status='failed',finished_at=?,error=?,result_json=? WHERE job_key=?", (_utcnow(), error, json.dumps(result, ensure_ascii=False), job_key))
                if isinstance(exc, PermissionError) or attempt + 1 >= config.max_attempts:
                    return {"status": "failed", "job_key": job_key, "error": error}
                if wait(config.retry_seconds):
                    return {"status": "cancelled", "job_key": job_key}
            else:
                with sqlite3.connect(DB_FILE) as conn:
                    conn.execute("UPDATE scheduled_jobs SET status='succeeded',finished_at=?,error=NULL,result_json=? WHERE job_key=?", (_utcnow(), json.dumps(result, ensure_ascii=False), job_key))
                return {"status": "succeeded", "job_key": job_key, "result": result}


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


_start_lock = threading.Lock()
_stop_event = threading.Event()
_job_thread = None


def start_background_jobs():
    global _scheduler_started, _job_thread
    config = SchedulerConfig.from_env()
    if not config.enabled or not config.actor or os.getenv("ERP_ISOLATED_TEST") == "1":
        return False
    require_capability(config.actor, RISK_WORKSPACE_WRITE)
    with _start_lock:
        if _scheduler_started:
            return True
        _stop_event.clear()

        def run_jobs():
            global _scheduler_started
            try:
                if _stop_event.wait(config.initial_delay_seconds):
                    return
                while not _stop_event.is_set():
                    try:
                        run_scheduled_refresh(config, wait=_stop_event.wait)
                    except Exception:
                        logging.exception("Scheduled news refresh failed")
                    if _stop_event.wait(config.interval_seconds):
                        return
            finally:
                _scheduler_started = False

        _job_thread = threading.Thread(target=run_jobs, name="erp-news-scheduler", daemon=True)
        _scheduler_started = True
        _job_thread.start()
        return True


def stop_background_jobs():
    _stop_event.set()
    if _job_thread:
        _job_thread.join(timeout=5)


def main():
    import argparse
    from .database import init_db
    parser = argparse.ArgumentParser(description="Explicit supply-chain news refresh")
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--job-key", help="Stable idempotency key; defaults to UTC interval bucket")
    args = parser.parse_args()
    if not os.getenv("ERP_DB_PATH"):
        parser.error("Explicit ERP_DB_PATH is required")
    if os.getenv("ERP_ISOLATED_TEST") == "1":
        from .isolated_runtime import block_external_network
        block_external_network()
    init_db()
    result = run_scheduled_refresh(job_key=args.job_key)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] in {"failed", "busy", "cancelled"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
