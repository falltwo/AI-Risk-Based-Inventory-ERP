"""
backend/llm_client.py
非 Agent 程式（分析頁、新聞歸類、翻譯、草稿生成）的統一 LLM 入口 — issue #27。

設計（參考 Hermes Agent 的「設定檔單一真實來源」+ 2026 角色別名模式）：
  - 模型設定全在 .env，不再有「側邊欄貼 key」的啟動方式：
      LLM_MODEL           主模型（agent 與分析共用預設，含 fallback chain）
      LLM_ANALYSIS_MODEL  分析副任務別名（選填；translate/新聞歸類這類量大task
                          可指到更便宜的模型。設定後為單一模型、不走 fallback）
  - 底層委派 agent_orchestrator._llm：LiteLLM 100+ 供應商、
    F6 fallback chain、用量記帳（usage_tag）全部共用，不重複實作。
  - 舊介面的 api_key 參數一律棄用忽略（保留簽名以免呼叫端漣漪）。
"""

from __future__ import annotations

import os


def llm_available() -> bool:
    """是否已設定任何可用的模型供應商（.env 驅動）。"""
    return bool(os.getenv("LLM_MODEL") or os.getenv("OPENAI_API_KEY")
                or os.getenv("GEMINI_API_KEY"))


def complete_text(prompt, system: str | None = None, temperature: float = 0.2,
                  json_mode: bool = False, tag: str = "analysis",
                  model: str | None = None) -> str:
    """
    單次文字補全。prompt 可為字串或 messages list。
    回傳純文字（失敗拋例外，由呼叫端決定 fallback 行為）。
    """
    from backend.agent_orchestrator import _llm, _content
    from backend.prompts import PROMPT_DEFENSE_BASELINE

    # 防注入基線（issue #47）：分析入口統一注入 —— 分析 prompt 常內嵌
    # 外部新聞等未信任內容，必須明確界定「資料 ≠ 指令」。
    sys_content = (system + "\n\n" + PROMPT_DEFENSE_BASELINE) if system else PROMPT_DEFENSE_BASELINE
    messages: list[dict] = [{"role": "system", "content": sys_content}]
    if isinstance(prompt, str):
        messages.append({"role": "user", "content": prompt})
    else:
        messages.extend(prompt)

    resolved = model or os.getenv("LLM_ANALYSIS_MODEL") or None
    resp = _llm(messages, model=resolved, temperature=temperature,
                json_mode=json_mode, usage_tag=tag)
    return _content(resp)
