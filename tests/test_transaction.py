"""
tests/test_transaction.py
驗證 transaction() 邊界的原子性：中途拋例外時，前半寫入被 rollback。
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import init_db, run_query, transaction, tx_run


@pytest.fixture(autouse=True)
def setup_db():
    """每個測試前重建乾淨的資料庫。"""
    init_db()


def test_transaction_commit():
    """正常 commit：寫入應持久化。"""
    with transaction() as conn:
        tx_run(conn,
            "INSERT INTO agent_action_logs (tool_name, parameters, caller, result, success, timestamp, checksum) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("tool_a", "{}", "admin", "ok", 1, "2026-01-01 00:00:00", "test_hash_commit"),
            fetch=False)

    rows = run_query("SELECT COUNT(*) FROM agent_action_logs WHERE checksum = 'test_hash_commit'")
    assert rows[0][0] == 1, "commit 後應持久化寫入"


def test_transaction_rollback_on_exception():
    """模擬中途 raise → 斷言前半寫入被 rollback。"""
    initial_count = run_query("SELECT COUNT(*) FROM agent_action_logs")[0][0]

    try:
        with transaction() as conn:
            tx_run(conn,
                "INSERT INTO agent_action_logs (tool_name, parameters, caller, result, success, timestamp, checksum) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("tool_b", "{}", "admin", "should_rollback", 0, "2026-01-01 00:00:00", "test_hash_rollback"),
                fetch=False)
            raise RuntimeError("模擬中途崩潰")
    except RuntimeError:
        pass

    after_count = run_query("SELECT COUNT(*) FROM agent_action_logs")[0][0]
    assert after_count == initial_count, f"中途 raise 後應 rollback，expect {initial_count} rows, got {after_count}"


def test_transaction_atomic_hash_chain_write():
    """
    驗證「讀 prev_hash → 算 hash → 寫入」三步在 transaction 內原子完成。
    若寫入失敗，不應留下中間狀態。
    """
    from backend.agent_logger import write_action_log

    write_action_log("tool_x", {"k": "v"}, "admin", "first log", True)
    initial_count = run_query("SELECT COUNT(*) FROM agent_action_logs")[0][0]

    try:
        with transaction() as conn:
            from backend.log_checksum import _get_prev_checksum, compute_checksum
            prev = _get_prev_checksum("agent_action_logs", conn=conn)
            checksum = compute_checksum(prev, "tool_y", "{}", "admin", "fail", 0, "dummy")
            tx_run(conn,
                "INSERT INTO agent_action_logs (tool_name, parameters, caller, result, success, timestamp, checksum) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("tool_y", "{}", "admin", "fail", 0, "dummy", checksum),
                fetch=False)
            raise RuntimeError("模擬 hash chain 寫入中崩潰")
    except RuntimeError:
        pass

    after_count = run_query("SELECT COUNT(*) FROM agent_action_logs")[0][0]
    assert after_count == initial_count, f"hash chain 寫入失敗須 rollback，expect {initial_count}, got {after_count}"
