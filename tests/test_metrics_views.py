# tests/test_metrics_views.py
import pytest
from backend.database import init_db, run_query

@pytest.fixture(autouse=True)
def setup_db():
    """每個測試前重建乾淨的資料庫，以防表格與 View 不存在"""
    init_db()


def test_metrics_views_return_null_without_observations():
    """空資料不是零風險或滿分，Dashboard 應能辨識為資料不足。"""
    run_query("DELETE FROM agent_action_logs", fetch=False)
    run_query("DELETE FROM agent_dispatch_logs", fetch=False)

    assert run_query("SELECT avg_decision_time FROM view_decision_time")[0][0] is None
    assert run_query("SELECT intercept_ratio FROM view_pending_intercept_ratio")[0][0] is None
    assert run_query("SELECT traceability_rate FROM view_traceability_rate")[0][0] is None
    assert run_query("SELECT avg_tools_per_turn FROM view_avg_tools_per_turn")[0][0] is None

def test_metrics_views_exist():
    # Clean up or prepare some mock dispatch and action logs
    run_query("INSERT INTO agent_dispatch_logs (task, task_type, primary_agent, needs_approval, caller, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
              ("Test task for metrics", "single", "inventory_agent", 1, "admin", "2026-07-03 12:00:00"), fetch=False)
              
    run_query("INSERT INTO agent_action_logs (tool_name, parameters, caller, result, success, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
              ("update_inventory", "{}", "admin", "Success", 1, "2026-07-03 12:00:05"), fetch=False)
              
    # 1. 測試平均決策時間
    dt = run_query("SELECT avg_decision_time FROM view_decision_time")
    assert len(dt) > 0
    assert dt[0][0] >= 0
    print(f"avg_decision_time: {dt[0][0]}")
    
    # 2. 測試 pending 攔截比例
    ratio = run_query("SELECT intercept_ratio FROM view_pending_intercept_ratio")
    assert len(ratio) > 0
    assert 0.0 <= ratio[0][0] <= 1.0
    print(f"intercept_ratio: {ratio[0][0]}")
    
    # 3. 測試可追溯率
    trace = run_query("SELECT traceability_rate FROM view_traceability_rate")
    assert len(trace) > 0
    assert 0.0 <= trace[0][0] <= 1.0
    print(f"traceability_rate: {trace[0][0]}")
    
    # 4. 測試平均帶工具數
    avg_tools = run_query("SELECT avg_tools_per_turn FROM view_avg_tools_per_turn")
    assert len(avg_tools) > 0
    assert avg_tools[0][0] >= 0
    print(f"avg_tools_per_turn: {avg_tools[0][0]}")


def test_direct_approval_request_is_counted_without_agent_dispatch():
    """審批帳本是所有入口的權威資料源，不能只統計 Agent 派工。"""
    run_query("DELETE FROM pending_approvals", fetch=False)
    run_query("DELETE FROM agent_dispatch_logs", fetch=False)
    run_query(
        """
        INSERT INTO pending_approvals (
            approval_id, tool_name, parameters, requester, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "approval-direct-1",
            "create_purchase_order",
            "{}",
            "admin",
            "approved",
            "2026-07-20 09:00:00",
            "2026-07-20 09:01:00",
        ),
        fetch=False,
    )

    summary = run_query(
        """
        SELECT total_requests, pending_requests, approved_requests, rejected_requests
        FROM view_approval_summary
        """
    )[0]
    assert summary == (1, 0, 1, 0)

    trend = run_query(
        """
        SELECT date, total_dispatches, approval_requests
        FROM view_governance_daily_trend
        """
    )
    assert trend == [("2026-07-20", 0, 1)]
