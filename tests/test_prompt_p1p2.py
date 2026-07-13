"""
tests/test_prompt_p1p2.py
issue #47 P1/P2：熱圖結構化輸出 + code-side 區域閘 + router few-shot +
PO 建議 JSON 化 + 死 prompt 清理 + 路由 prompt 快取。
兼作 prompt 格式回歸測試（P2）。
"""

from types import SimpleNamespace

from backend import agent_orchestrator as orch
from backend import prompts
from backend.supply_chain_risk import _gate_heatmap_updates, _coerce_heatmap_events


# ── P2：死 prompt 已清、規則與現實一致 ─────────────────────────────


def test_dead_prompts_removed():
    for name in ("HEATMAP_SUMMARY_PROMPT", "TRANSLATE_PROMPT",
                 "INFER_REGION_PROMPT", "BATCH_INFER_REGION_PROMPT"):
        assert not hasattr(prompts, name), f"{name} 應已刪除"


def test_rule5_no_calculate_mismatch():
    assert "calculate" not in orch._COMMON_AGENT_RULES  # 只有 finance 有此工具


# ── P1-2 / P2：router few-shot 與 prompt 快取 ─────────────────────


def test_router_fewshot_present_and_ordered():
    p = orch.build_orchestrator_prompt()
    assert "【範例】" in p
    assert "幫我算 3500×12" in p            # 關鍵字路由會誤判的歧義案例
    assert "受戰爭影響的採購單" in p
    assert p.index("【範例】") < p.index("安全基線") < p.index("輸出格式")


def test_router_prompt_cached():
    a = orch.build_orchestrator_prompt()
    b = orch.build_orchestrator_prompt()
    assert a is b  # 同一個字串物件 = 快取生效


# ── P1-1 / P1-3：熱圖結構化 + code-side gate ──────────────────────


VALID = ["台灣 北區", "台灣 中區", "日本 亞洲"]
EXP = {"台灣": ["台灣 北區", "台灣 中區"], "日本": ["日本 亞洲"], "亞洲": ["日本 亞洲"],
       "北區": ["台灣 北區"], "中區": ["台灣 中區"]}


def test_gate_passes_valid_and_expands_country():
    raw = [{"地區": "台灣 北區", "風險": 80},   # 完整名稱 → 直接收
           {"地區": "台灣", "風險": 60}]        # 總稱 → 展開兩筆
    out = _gate_heatmap_updates(raw, VALID, EXP)
    names = [u["display_name"] for u in out]
    assert names == ["台灣 北區", "台灣 北區", "台灣 中區"]
    assert all(isinstance(u["risk_pct"], float) for u in out)


def test_gate_drops_unknown_region():
    out = _gate_heatmap_updates([{"地區": "烏克蘭", "風險": 90}], VALID, EXP)
    assert out == []  # code-side gate：不在清單也不可展開 → 丟棄


def test_gate_soft_mode_when_no_suppliers():
    fallback = ["（目前無正式供應商據點資料，請跳過風險建議清單）"]
    out = _gate_heatmap_updates([{"地區": "任何地方", "風險": "55%"}], fallback, {})
    assert out == [{"display_name": "任何地方", "risk_pct": 55.0}]  # 寬鬆模式全收


def test_coerce_events_types_and_defaults():
    out = _coerce_heatmap_events([
        {"類型": "罷工", "地區": "台灣 北區", "國家": "台灣", "延遲天數": "14", "描述": "港口罷工"},
        {"類型": None, "延遲天數": "not-a-number"},
    ])
    assert out[0] == {"event_type": "罷工", "region": "台灣 北區", "country": "台灣",
                      "impact_days": 14, "description": "港口罷工"}
    assert out[1]["event_type"] == "其他" and out[1]["impact_days"] == 14


def test_heatmap_flow_end_to_end(monkeypatch):
    """整條 get_heatmap_ai_summary：假 JSON 回應 → 三元組契約不變。"""
    import backend.llm_client as lc
    monkeypatch.setattr(lc, "complete_text", lambda *a, **kw:
                        '{"摘要": "### 摘要\\n台灣風險升高。", '
                        '"更新": [{"地區": "台灣", "風險": 75}], '
                        '"事件": [{"類型": "政策", "地區": "台灣", "國家": "台灣", '
                        '"延遲天數": 12, "描述": "出口管制"}]}')
    from backend.supply_chain_risk import get_heatmap_ai_summary
    summary, updates, events = get_heatmap_ai_summary(news_context="測試新聞")

    assert "台灣風險升高" in summary
    assert isinstance(updates, list) and isinstance(events, list)
    assert events[0]["impact_days"] == 12  # 契約鍵名 = 舊版（呼叫端不用改）


def test_heatmap_failsoft_on_bad_json(monkeypatch):
    import backend.llm_client as lc
    monkeypatch.setattr(lc, "complete_text", lambda *a, **kw: "這不是 JSON")
    from backend.supply_chain_risk import get_heatmap_ai_summary
    summary, updates, events = get_heatmap_ai_summary(news_context="x")
    assert "解析失敗" in summary and updates == [] and events == []


def test_v2_prompt_object_root_no_dead_directives():
    p = prompts.HEATMAP_AI_SUMMARY_PROMPT_V2
    assert "頂層必須是物件" in p
    assert "UPDATE_EVENT_DELAY" not in p   # 從沒人解析的死指令已移除
    assert "！！！" not in p                # Draconian 吼叫改為 code-side gate


# ── PO 建議 JSON 化 ───────────────────────────────────────────────


def test_po_suggestions_json_parse(monkeypatch):
    import backend.llm_client as lc
    monkeypatch.setattr(lc, "complete_text", lambda *a, **kw:
                        '{"results": [{"po_id": "PO-1", "延遲天數": 14, "建議": "調用越南庫存"},'
                        '{"po_id": "PO-陌生", "延遲天數": 7, "建議": "不在清單，應被過濾"}]}')
    from backend.supply_chain_risk import get_ai_alternative_suggestions
    impacted = [{"po_id": "PO-1", "supplier_name": "測試供應商",
                 "key_materials": "螢幕", "estimated_delay": "+14 天"}]
    out = get_ai_alternative_suggestions(impacted_list=impacted, hotspot_name="台灣")

    assert out == [{"po_id": "PO-1", "estimated_delay_days": 14,
                    "alternative_suggestion": "調用越南庫存"}]
