"""
tests/test_llm_client.py
issue #27：非 Agent 程式的統一 LLM 入口。
"""

from types import SimpleNamespace

from backend import llm_client
from backend import agent_orchestrator as orch


def _llm_msg(content):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_complete_text_builds_messages_and_returns_text(monkeypatch):
    captured = {}

    def fake_llm(messages, **kw):
        captured["messages"] = messages
        captured["kw"] = kw
        return _llm_msg("分析結果")

    monkeypatch.setattr(orch, "_llm", fake_llm)
    monkeypatch.delenv("LLM_ANALYSIS_MODEL", raising=False)

    out = llm_client.complete_text("分析這則新聞", system="你是分析師",
                                   temperature=0.3, json_mode=True, tag="analysis:news")

    assert out == "分析結果"
    # system 內容 = 呼叫端 system + 防注入基線（issue #47）
    assert captured["messages"][0]["role"] == "system"
    assert "你是分析師" in captured["messages"][0]["content"]
    assert captured["messages"][1] == {"role": "user", "content": "分析這則新聞"}
    assert captured["kw"]["json_mode"] is True
    assert captured["kw"]["temperature"] == 0.3
    assert captured["kw"]["usage_tag"] == "analysis:news"
    assert captured["kw"]["model"] is None  # 未設別名 → 走主鏈（含 fallback）


def test_analysis_model_alias_resolved(monkeypatch):
    captured = {}
    monkeypatch.setattr(orch, "_llm",
                        lambda messages, **kw: captured.update(kw=kw) or _llm_msg("ok"))
    monkeypatch.setenv("LLM_ANALYSIS_MODEL", "openai/cheap-model")

    llm_client.complete_text("hi")

    assert captured["kw"]["model"] == "openai/cheap-model"


def test_llm_available_env_driven(monkeypatch):
    for var in ("LLM_MODEL", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert llm_client.llm_available() is False

    monkeypatch.setenv("LLM_MODEL", "openai/x")
    assert llm_client.llm_available() is True
