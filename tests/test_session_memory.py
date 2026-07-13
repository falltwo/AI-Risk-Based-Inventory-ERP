"""
tests/test_session_memory.py
功能 #4：Web 助理 session 內多輪記憶（sliding window）。

驗證 _trim_history 的視窗/截斷/角色映射，以及 history 正確夾進
run_agent 與 route 的 prompt。
"""

from types import SimpleNamespace

from backend import agent_orchestrator as orch


def _llm_msg(content):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


# ── _trim_history 本體 ───────────────────────────────────────────────


def test_trim_window_and_role_mapping():
    history = [{"role": "user", "content": f"問題 {i}"} if i % 2 == 0
               else {"role": "model", "content": f"回答 {i}"}
               for i in range(12)]

    trimmed = orch._trim_history(history, max_msgs=8)

    assert len(trimmed) == 8                      # 只留最後 8 則
    assert trimmed[0]["content"] == "問題 4"       # 視窗從第 4 則開始
    assert all(m["role"] in ("user", "assistant") for m in trimmed)  # model→assistant


def test_trim_truncates_long_and_skips_junk():
    history = [
        {"role": "user", "content": "x" * 5000},          # 超長 → 截斷
        {"role": "tool", "content": "工具原文不該進來"},     # 非對話角色 → 略過
        {"role": "assistant", "content": "   "},           # 空白 → 略過
    ]

    trimmed = orch._trim_history(history, max_chars=1200)

    assert len(trimmed) == 1
    assert trimmed[0]["content"].endswith("…（截斷）")
    assert len(trimmed[0]["content"]) <= 1200 + 10


# ── history 進 run_agent prompt ─────────────────────────────────────


def test_run_agent_injects_history_between_system_and_task(monkeypatch):
    captured = {}

    def fake_llm(messages, **kw):
        captured["messages"] = messages
        return _llm_msg("好的。")

    monkeypatch.setattr(orch, "_llm", fake_llm)

    history = [{"role": "user", "content": "庫存還有多少？"},
               {"role": "model", "content": "P001 還有 150 件。"}]
    orch.run_agent("inventory_agent", "那再進 20 件", role="warehouse", history=history)

    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1] == {"role": "user", "content": "庫存還有多少？"}
    assert msgs[2] == {"role": "assistant", "content": "P001 還有 150 件。"}
    assert msgs[-1] == {"role": "user", "content": "那再進 20 件"}


def test_run_agent_no_history_unchanged(monkeypatch):
    captured = {}
    monkeypatch.setattr(orch, "_llm",
                        lambda messages, **kw: captured.update(m=messages) or _llm_msg("ok"))

    orch.run_agent("inventory_agent", "查庫存", role="warehouse")

    assert len(captured["m"]) == 2  # system + user，跟原本一樣


# ── history 進 route（解指代）────────────────────────────────────────


def test_route_includes_history_recap(monkeypatch):
    captured = {}

    def fake_llm(messages, **kw):
        captured["messages"] = messages
        return _llm_msg('{"task_type":"single","primary_agent":"inventory_agent",'
                        '"agent_chain":["inventory_agent"],"needs_approval":false,'
                        '"reason":"依前文指庫存"}')

    monkeypatch.setattr(orch, "_llm", fake_llm)

    history = [{"role": "user", "content": "哪些商品快缺貨？"},
               {"role": "model", "content": "P004 螢幕低於安全庫存。"}]
    routing = orch.route("那幫我補 20 個", history=history)

    user_content = captured["messages"][-1]["content"]
    assert "先前對話摘錄" in user_content
    assert "哪些商品快缺貨" in user_content
    assert "【目前任務】那幫我補 20 個" in user_content
    assert routing["primary_agent"] == "inventory_agent"


def test_route_without_history_plain_task(monkeypatch):
    captured = {}
    monkeypatch.setattr(orch, "_llm",
                        lambda messages, **kw: captured.update(m=messages) or _llm_msg(
                            '{"task_type":"smalltalk","primary_agent":"cs_agent",'
                            '"agent_chain":["cs_agent"],"needs_approval":false,"reason":"招呼"}'))

    orch.route("你好")

    assert captured["m"][-1]["content"] == "你好"  # 無 history 時不加前綴
