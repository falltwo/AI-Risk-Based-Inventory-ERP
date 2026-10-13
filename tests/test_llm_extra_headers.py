"""
tests/test_llm_extra_headers.py
LLM_EXTRA_HEADERS / LLM_TIMEOUT：讓 .env 指定每次 litellm 呼叫都要附帶的 HTTP header
（OpenCode Go 要求 x-opencode-session，缺了直接回 MissingSessionID）。
"""

from types import SimpleNamespace

from backend import agent_orchestrator as orch


def _resp(text="ok"):
    msg = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _capture(monkeypatch):
    calls = []

    def fake_completion(**kw):
        calls.append(kw)
        return _resp()

    monkeypatch.setattr(orch.litellm, "completion", fake_completion)
    monkeypatch.setattr(orch, "_FALLBACK_MODELS", [])
    return calls


def test_extra_headers_forwarded_to_litellm(monkeypatch):
    monkeypatch.setattr(orch, "_EXTRA_HEADERS", {"x-opencode-session": "erp"})
    calls = _capture(monkeypatch)

    orch._llm([{"role": "user", "content": "hi"}])

    assert calls[0]["extra_headers"] == {"x-opencode-session": "erp"}


def test_no_extra_headers_when_unset(monkeypatch):
    monkeypatch.setattr(orch, "_EXTRA_HEADERS", {})
    calls = _capture(monkeypatch)

    orch._llm([{"role": "user", "content": "hi"}])

    assert "extra_headers" not in calls[0]


def test_load_extra_headers_parses_json(monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_HEADERS", '{"x-opencode-session": "erp", "x-n": 1}')
    assert orch._load_extra_headers() == {"x-opencode-session": "erp", "x-n": "1"}


def test_load_extra_headers_ignores_bad_values(monkeypatch, capsys):
    """打錯字或不是物件 → 當作未設定並印警告，不讓整個模組 import 失敗。"""
    for bad in ("not json", '["a", "b"]', "   "):
        monkeypatch.setenv("LLM_EXTRA_HEADERS", bad)
        assert orch._load_extra_headers() == {}
    assert "LLM_EXTRA_HEADERS" in capsys.readouterr().out


def test_timeout_forwarded_to_litellm(monkeypatch):
    """上游卡住時要能放棄換 fallback，所以每次呼叫都帶 timeout。"""
    monkeypatch.setattr(orch, "_EXTRA_HEADERS", {})
    monkeypatch.setattr(orch, "_LLM_TIMEOUT", 42.0)
    calls = _capture(monkeypatch)

    orch._llm([{"role": "user", "content": "hi"}])

    assert calls[0]["timeout"] == 42.0


def test_load_timeout_defaults_and_rejects_bad_values(monkeypatch):
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    assert orch._load_timeout() == 120.0
    monkeypatch.setenv("LLM_TIMEOUT", "45")
    assert orch._load_timeout() == 45.0
    for bad in ("abc", "0", "-5"):
        monkeypatch.setenv("LLM_TIMEOUT", bad)
        assert orch._load_timeout() == 120.0
