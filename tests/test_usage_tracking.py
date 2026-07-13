"""
tests/test_usage_tracking.py
功能 #2：LLM 用量/成本記帳。

驗證 _llm 每次成功呼叫會在 llm_usage_logs 記一列（tag/model/tokens），
usage 缺失或成本查價失敗時不炸也不污染主流程。
"""

from types import SimpleNamespace

from backend import agent_orchestrator as orch
from backend.database import run_query
from backend.usage_logger import ensure_table, get_usage_summary


def _resp_with_usage(pt=100, ct=50):
    msg = SimpleNamespace(content="ok", tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct,
                              total_tokens=pt + ct),
    )


def _rows():
    ensure_table()
    return run_query("SELECT tag, model, prompt_tokens, completion_tokens, total_tokens FROM llm_usage_logs ORDER BY id")


def test_llm_call_writes_usage_row(monkeypatch):
    monkeypatch.setattr(orch, "DEFAULT_MODEL", "openai/test-model")
    monkeypatch.setattr(orch, "_FALLBACK_MODELS", [])
    monkeypatch.setattr(orch.litellm, "completion", lambda **kw: _resp_with_usage(120, 30))

    before = len(_rows())
    orch._llm([{"role": "user", "content": "hi"}], usage_tag="agent:inventory_agent")
    rows = _rows()

    assert len(rows) == before + 1
    tag, model, pt, ct, tt = rows[-1]
    assert tag == "agent:inventory_agent"
    assert model == "openai/test-model"
    assert (pt, ct, tt) == (120, 30, 150)


def test_no_usage_no_row(monkeypatch):
    """假回應沒有 usage（如其他測試的 mock）→ 不記、不炸。"""
    monkeypatch.setattr(orch, "DEFAULT_MODEL", "openai/test-model")
    monkeypatch.setattr(orch, "_FALLBACK_MODELS", [])
    msg = SimpleNamespace(content="ok", tool_calls=None)
    bare = SimpleNamespace(choices=[SimpleNamespace(message=msg)])  # 無 usage
    monkeypatch.setattr(orch.litellm, "completion", lambda **kw: bare)

    before = len(_rows())
    orch._llm([{"role": "user", "content": "hi"}], usage_tag="route")
    assert len(_rows()) == before


def test_cost_lookup_failure_still_logs_tokens(monkeypatch):
    """completion_cost 對未知模型丟錯 → cost NULL，tokens 照記。"""
    import litellm as _litellm
    monkeypatch.setattr(orch, "DEFAULT_MODEL", "openai/unknown-custom")
    monkeypatch.setattr(orch, "_FALLBACK_MODELS", [])
    monkeypatch.setattr(orch.litellm, "completion", lambda **kw: _resp_with_usage(10, 5))
    monkeypatch.setattr(_litellm, "completion_cost",
                        lambda **kw: (_ for _ in ()).throw(ValueError("no pricing")))

    before = len(_rows())
    orch._llm([{"role": "user", "content": "hi"}], usage_tag="aggregate")
    rows = _rows()
    assert len(rows) == before + 1

    cost = run_query("SELECT cost_usd FROM llm_usage_logs ORDER BY id DESC LIMIT 1")[0][0]
    assert cost is None


def test_summary_groups_by_tag(monkeypatch):
    monkeypatch.setattr(orch, "DEFAULT_MODEL", "openai/test-model")
    monkeypatch.setattr(orch, "_FALLBACK_MODELS", [])
    monkeypatch.setattr(orch.litellm, "completion", lambda **kw: _resp_with_usage(100, 100))

    orch._llm([{"role": "user", "content": "a"}], usage_tag="route")
    orch._llm([{"role": "user", "content": "b"}], usage_tag="route")

    summary = get_usage_summary(days=1)
    route_row = next(r for r in summary if r["tag"] == "route" and r["model"] == "openai/test-model")
    assert route_row["calls"] >= 2
    assert route_row["tokens"] >= 400
