import os
import sqlite3
import subprocess
import sys

import pytest
from backend import database, scheduler
from backend.job_lock import exclusive_job_lock


@pytest.fixture
def job_db(tmp_path, monkeypatch):
    path = str(tmp_path / "scheduler.db")
    monkeypatch.setattr(database, "DB_FILE", path)
    database.init_db()
    return path


def test_retry_then_success_and_skip_same_key(job_db, monkeypatch):
    calls, waits = [], []
    def refresh(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("first attempt")
        return {"saved_count":1,"status":"succeeded"}
    monkeypatch.setattr(scheduler,"refresh_supply_chain_news_once",refresh)
    cfg = scheduler.SchedulerConfig(actor="planner",max_attempts=3,retry_seconds=2)
    assert scheduler.run_scheduled_refresh(cfg,job_key="fixed",wait=waits.append)["status"] == "succeeded"
    assert scheduler.run_scheduled_refresh(cfg,job_key="fixed")["status"] == "skipped"
    assert len(calls) == 2 and waits == [2]
    with sqlite3.connect(job_db) as conn:
        assert conn.execute("SELECT status,attempts,error FROM scheduled_jobs").fetchone() == ("succeeded",2,None)


def test_partial_failure_can_retry_same_key_later(job_db, monkeypatch):
    monkeypatch.setattr(scheduler,"refresh_supply_chain_news_once",lambda **k:{"status":"partial_failure"})
    cfg = scheduler.SchedulerConfig(actor="planner",max_attempts=2,retry_seconds=0)
    assert scheduler.run_scheduled_refresh(cfg,job_key="retry")["status"] == "failed"
    monkeypatch.setattr(scheduler,"refresh_supply_chain_news_once",lambda **k:{"status":"succeeded"})
    assert scheduler.run_scheduled_refresh(cfg,job_key="retry")["status"] == "succeeded"
    with sqlite3.connect(job_db) as conn:
        assert conn.execute("SELECT attempts FROM scheduled_jobs").fetchone()[0] == 3


def test_permission_revocation_does_not_retry(job_db, monkeypatch):
    def revoked(**kwargs):
        raise PermissionError("revoked")
    monkeypatch.setattr(scheduler,"refresh_supply_chain_news_once",revoked)
    assert scheduler.run_scheduled_refresh(scheduler.SchedulerConfig(actor="planner"),job_key="revoked")["status"] == "failed"
    with sqlite3.connect(job_db) as conn:
        assert conn.execute("SELECT attempts FROM scheduled_jobs").fetchone()[0] == 1


def test_cross_process_lock_and_crash_release(job_db):
    code = "from backend.job_lock import exclusive_job_lock; import sys;\nwith exclusive_job_lock(sys.argv[1], 'scheduler') as acquired: print(acquired)"
    with exclusive_job_lock(job_db,"scheduler") as acquired:
        assert acquired
        result = subprocess.run([sys.executable,"-c",code,job_db],capture_output=True,text=True,timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"
    result = subprocess.run([sys.executable,"-c",code,job_db],capture_output=True,text=True,timeout=30)
    assert result.returncode == 0 and result.stdout.strip() == "True"
    crash = "from backend.job_lock import exclusive_job_lock; import sys,os;\nwith exclusive_job_lock(sys.argv[1], 'scheduler') as acquired: os._exit(17 if acquired else 18)"
    assert subprocess.run([sys.executable,"-c",crash,job_db],timeout=30).returncode == 17
    with exclusive_job_lock(job_db,"scheduler") as acquired:
        assert acquired


def test_abandoned_running_record_recovered(job_db,monkeypatch):
    with sqlite3.connect(job_db) as conn:
        conn.execute("INSERT INTO scheduled_jobs(job_key,status,attempts) VALUES ('crashed','running',1)")
    monkeypatch.setattr(scheduler,"refresh_supply_chain_news_once",lambda **k:{"status":"succeeded"})
    assert scheduler.run_scheduled_refresh(scheduler.SchedulerConfig(actor="planner"),job_key="crashed")["status"] == "succeeded"


def test_background_is_opt_in_and_isolation_overrides_enable(monkeypatch):
    monkeypatch.setenv("ERP_SCHEDULER_ACTOR","planner")
    monkeypatch.setenv("ERP_SCHEDULER_ENABLED","0")
    assert scheduler.start_background_jobs() is False
    monkeypatch.setenv("ERP_SCHEDULER_ENABLED","1")
    monkeypatch.setenv("ERP_ISOLATED_TEST","1")
    assert scheduler.start_background_jobs() is False


def test_configuration_rejects_invalid_interval(monkeypatch):
    monkeypatch.setenv("ERP_SCHEDULER_INTERVAL_SECONDS","0")
    with pytest.raises(ValueError):
        scheduler.SchedulerConfig.from_env()


def test_scheduler_busy_does_not_start_another_job(job_db,monkeypatch):
    monkeypatch.setattr(scheduler,"refresh_supply_chain_news_once",lambda **k:pytest.fail("Duplicate job"))
    with exclusive_job_lock(job_db,"scheduler") as acquired:
        assert acquired
        assert scheduler.run_scheduled_refresh(scheduler.SchedulerConfig(actor="planner"),job_key="busy")["status"] == "busy"
    with sqlite3.connect(job_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduled_jobs").fetchone()[0] == 0
