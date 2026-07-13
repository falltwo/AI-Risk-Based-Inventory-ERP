"""
tests/test_aggregate_governance.py
F3（issue #33）：多 Agent 彙整不得洗掉 pending / denied 治理訊號。

驗證 _aggregate 的最終輸出「必定」含系統自動附註的治理狀態區塊，
即使彙整 LLM 的摘要隻字未提審批。
"""

from types import SimpleNamespace

from backend import agent_orchestrator as orch


def _llm_msg(content):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _results_ok_and_pending():
    """模擬兩個 Agent 的 run_agent 輸出：一個純查詢 ok、一個有 pending + denied。"""
    return [
        {
            "agent": "inventory_agent",
            "reply": "庫存共 150 件，無缺貨。",
            "tool_calls": [{"tool": "get_all_inventory", "args": {}, "outcome": "ok"}],
            "pending": [],
        },
        {
            "agent": "sales_agent",
            "reply": "已為您處理訂單事宜。",
            "tool_calls": [
                {"tool": "create_order", "args": {}, "outcome": "pending"},
                {"tool": "get_payroll_summary", "args": {}, "outcome": "denied"},
            ],
            "pending": [
                {"tool": "create_order", "args": {}, "approval_id": "PENDING-AGG-1"},
            ],
        },
    ]


def test_aggregate_keeps_pending_and_denied(monkeypatch):
    """彙整 LLM 摘要沒提審批 → 最終回覆仍必須揭露 pending 與 denied。"""
    # 彙整 LLM 幻覺：把治理訊號全部洗掉
    monkeypatch.setattr(orch, "_llm", lambda *a, **kw: _llm_msg("今日營運一切正常，訂單皆已妥善處理。"))

    reply = orch._aggregate("老闆早報", _results_ok_and_pending(), None, None, None)

    assert "審批" in reply and "尚未執行" in reply, f"缺 pending 告示：{reply!r}"
    assert "PENDING-AGG-1" in reply, f"缺審批單號：{reply!r}"
    assert "拒絕" in reply, f"缺 denied 告示：{reply!r}"


def test_aggregate_llm_failure_still_discloses(monkeypatch):
    """彙整 LLM 掛掉（fallback 回原始 blocks）時，治理訊號同樣要在。"""
    def _boom(*a, **kw):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(orch, "_llm", _boom)

    reply = orch._aggregate("老闆早報", _results_ok_and_pending(), None, None, None)

    assert "PENDING-AGG-1" in reply
    assert "尚未執行" in reply


def test_aggregate_clean_results_no_footer(monkeypatch):
    """全部 ok、無 pending/denied → 不得出現治理附註（不誤報）。"""
    monkeypatch.setattr(orch, "_llm", lambda *a, **kw: _llm_msg("彙整摘要。"))
    results = [
        {"agent": "inventory_agent", "reply": "庫存正常。",
         "tool_calls": [{"tool": "get_all_inventory", "args": {}, "outcome": "ok"}],
         "pending": []},
        {"agent": "finance_agent", "reply": "財務正常。",
         "tool_calls": [], "pending": []},
    ]

    reply = orch._aggregate("早報", results, None, None, None)

    assert "尚未執行" not in reply
    assert "拒絕" not in reply
