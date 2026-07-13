"""
tests/test_llm_fallback.py
F6（issue #34）：LLM 供應商 fallback chain。

驗證預設路徑下 primary 模型掛掉時，_llm 自動依序改打 fallback 模型；
呼叫端明確指定 model（如側邊欄 Gemini key 路徑）時不亂 fallback。
"""

from types import SimpleNamespace

import pytest

from backend import agent_orchestrator as orch


def _resp(text="ok"):
    msg = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.fixture
def chain(monkeypatch):
    """固定 fallback chain，測試不依賴 .env。"""
    monkeypatch.setattr(orch, "DEFAULT_MODEL", "openai/primary-model")
    monkeypatch.setattr(orch, "_FALLBACK_MODELS",
                        ["openai/backup-model", "gemini/last-resort"])


def test_fallback_walks_chain_in_order(monkeypatch, chain):
    """primary 與第一 fallback 都掛 → 第二 fallback 成功，順序正確。"""
    attempted = []

    def fake_completion(**kw):
        attempted.append(kw["model"])
        if kw["model"] != "gemini/last-resort":
            raise RuntimeError("provider down")  # 非暫時性錯誤 → 立即換供應商
        return _resp("survived")

    monkeypatch.setattr(orch.litellm, "completion", fake_completion)

    resp = orch._llm([{"role": "user", "content": "hi"}])

    assert attempted == ["openai/primary-model", "openai/backup-model", "gemini/last-resort"]
    assert resp.choices[0].message.content == "survived"


def test_primary_ok_no_fallback(monkeypatch, chain):
    """primary 正常 → 不碰 fallback。"""
    attempted = []

    def fake_completion(**kw):
        attempted.append(kw["model"])
        return _resp()

    monkeypatch.setattr(orch.litellm, "completion", fake_completion)
    orch._llm([{"role": "user", "content": "hi"}])

    assert attempted == ["openai/primary-model"]


def test_explicit_model_never_falls_back(monkeypatch, chain):
    """呼叫端明確指定 model（側邊欄 Gemini 路徑）→ 失敗就拋錯，不偷換模型。"""
    attempted = []

    def fake_completion(**kw):
        attempted.append(kw["model"])
        raise RuntimeError("provider down")

    monkeypatch.setattr(orch.litellm, "completion", fake_completion)

    with pytest.raises(RuntimeError):
        orch._llm([{"role": "user", "content": "hi"}],
                  model="gemini/gemini-2.5-flash", api_key="user-key")

    assert attempted == ["gemini/gemini-2.5-flash"]


def test_all_providers_down_raises(monkeypatch, chain):
    """整條 chain 全掛 → 拋出最後一個錯誤，不吞掉。"""
    def fake_completion(**kw):
        raise RuntimeError(f"down: {kw['model']}")

    monkeypatch.setattr(orch.litellm, "completion", fake_completion)

    with pytest.raises(RuntimeError, match="gemini/last-resort"):
        orch._llm([{"role": "user", "content": "hi"}])
