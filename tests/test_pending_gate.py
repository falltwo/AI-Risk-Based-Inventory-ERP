"""
tests/test_pending_gate.py
F2（issue #32）：pending 狀態 code-gate。

驗證 run_agent 在有工具呼叫被攔下送審批（pending）時，
最終回覆「必定」包含審批告示 —— 不管 LLM 自己怎麼措辭。
"""

from types import SimpleNamespace

import pytest

from backend import agent_orchestrator as orch


# ── 假 LLM 回應（模仿 litellm response 形狀）────────────────────────────


def _llm_msg(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _tool_call(name, args_json="{}"):
    return SimpleNamespace(
        id="tc-1",
        function=SimpleNamespace(name=name, arguments=args_json),
    )


@pytest.fixture
def fake_llm_write_then_lie(monkeypatch):
    """第一輪：LLM 呼叫 create_order；第二輪：LLM 謊稱已完成。"""
    calls = iter([
        _llm_msg(tool_calls=[_tool_call("create_order", '{"product_id": "P001"}')]),
        _llm_msg(content="好的，訂單已建立完成！"),  # 幻覺：其實在等審批
    ])
    monkeypatch.setattr(orch, "_llm", lambda *a, **kw: next(calls))


def test_pending_reply_must_disclose_approval(monkeypatch, fake_llm_write_then_lie):
    """有 pending 時，回覆必須含審批告示與 approval_id，不能只剩 LLM 的說法。"""
    monkeypatch.setattr(
        orch, "execute_tool_call",
        lambda name, args, role, **kw: {
            "status": "pending",
            "message": "已建立審批單",
            "approval_id": "PENDING-TEST-1",
        },
    )

    result = orch.run_agent("sales_agent", "幫我建一張訂單", role="sales")

    assert result["pending"], "pending 清單應有一筆"
    reply = result["reply"]
    assert "送" in reply and "審批" in reply, f"回覆缺少審批告示：{reply!r}"
    assert "尚未執行" in reply, f"回覆必須明講尚未執行：{reply!r}"
    assert "PENDING-TEST-1" in reply, f"回覆應含審批單號：{reply!r}"


def test_no_pending_reply_untouched(monkeypatch):
    """沒有 pending 時，回覆不得被加上審批告示。"""
    monkeypatch.setattr(
        orch, "_llm",
        lambda *a, **kw: _llm_msg(content="目前庫存共 150 件。"),
    )

    result = orch.run_agent("inventory_agent", "查庫存", role="warehouse")

    assert result["pending"] == []
    assert result["reply"] == "目前庫存共 150 件。"
    assert "審批" not in result["reply"]
