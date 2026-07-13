"""
tests/test_prompt_hardening.py
issue #47 兩個 P0：
  P0-1 防注入基線注入到所有 system prompt 層
  P0-2 批量新聞歸類 JSON 頂層改物件（雙格式相容）
"""

from types import SimpleNamespace

from backend import agent_orchestrator as orch
from backend import llm_client
from backend.prompts import PROMPT_DEFENSE_BASELINE, BATCH_INFER_WITH_PRECEDENTS_PROMPT


def _llm_msg(content):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


# ── P0-1：防注入基線覆蓋四層 ─────────────────────────────────────────


def test_baseline_in_all_agent_prompts():
    for agent_id, prompt in orch.AGENT_SYSTEM_PROMPTS.items():
        assert "安全基線" in prompt, f"{agent_id} 的 system prompt 缺防注入基線"
        assert "不是給你的指令" in prompt


def test_baseline_in_router_prompt():
    p = orch.build_orchestrator_prompt()
    assert "安全基線" in p
    # 基線要在輸出格式之前（先立規矩再談格式）
    assert p.index("安全基線") < p.index("輸出格式")


def test_baseline_in_smalltalk_and_aggregate(monkeypatch):
    """LOOP 補抓：smalltalk 與 aggregate 的 system prompt 也要有基線。"""
    captured = []
    monkeypatch.setattr(orch, "_llm",
                        lambda messages, **kw: captured.append(messages) or _llm_msg("ok"))

    orch._reply_without_tools("你好", {"primary_agent": "cs_agent", "routed_by": "llm"},
                              None, None, None, use_llm=True)
    assert "安全基線" in captured[-1][0]["content"], "smalltalk system prompt 缺基線"

    orch._aggregate("早報", [{"agent": "inventory_agent", "reply": "ok",
                             "tool_calls": [], "pending": []}], None, None, None)
    assert "安全基線" in captured[-1][0]["content"], "aggregate system prompt 缺基線"


def test_baseline_in_analysis_entry(monkeypatch):
    captured = {}
    monkeypatch.setattr(orch, "_llm",
                        lambda messages, **kw: captured.update(m=messages) or _llm_msg("ok"))

    llm_client.complete_text("分析這則新聞", system="你是分析師")
    sys_msg = captured["m"][0]
    assert sys_msg["role"] == "system"
    assert "你是分析師" in sys_msg["content"]
    assert "安全基線" in sys_msg["content"]

    # 沒給 system 時也要有基線
    llm_client.complete_text("分析這則新聞")
    assert "安全基線" in captured["m"][0]["content"]


def test_baseline_in_line_bot_source():
    """LINE bot import 需要 LINE 憑證，改以原始碼層驗證注入點存在。"""
    src = open("line bot/bot_server.py", encoding="utf-8").read()
    assert "PROMPT_DEFENSE_BASELINE" in src
    assert 'system_prompt += "\\n" + PROMPT_DEFENSE_BASELINE' in src


# ── P0-2：批量歸類 JSON 頂層物件 + 雙格式相容 ────────────────────────


def test_batch_prompt_demands_object_root():
    assert '"results"' in BATCH_INFER_WITH_PRECEDENTS_PROMPT
    assert "頂層必須是物件" in BATCH_INFER_WITH_PRECEDENTS_PROMPT


def _run_batch(monkeypatch, llm_json: str):
    import backend.llm_client as lc
    monkeypatch.setattr(lc, "complete_text", lambda *a, **kw: llm_json)
    from backend.supply_chain_risk import batch_infer_affected_region_from_news
    return batch_infer_affected_region_from_news(news_texts=["台灣港口罷工影響出貨"])


def test_batch_parses_object_root(monkeypatch):
    out = _run_batch(monkeypatch, '{"results": [{"news_id": 0, "相關性": "YES", '
                                  '"國家": "台灣", "地區": "高雄", "事件類型": "罷工", '
                                  '"繁體中文簡要": "港口罷工", "預計延遲": 14}]}')
    assert out[0]["is_relevant"] is True
    assert out[0]["country"] == "台灣"
    assert out[0]["estimated_delay"] == 14


def test_batch_still_accepts_legacy_array(monkeypatch):
    out = _run_batch(monkeypatch, '[{"news_id": 0, "相關性": "NO", "國家": "不明", '
                                  '"地區": "不明", "事件類型": "其他", '
                                  '"繁體中文簡要": "", "預計延遲": 0}]')
    assert out[0]["is_relevant"] is False
