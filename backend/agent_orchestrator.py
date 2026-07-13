"""
backend/agent_orchestrator.py
總管 Agent（Orchestrator）— B 交付

設計：對外是 Supervisor、對內第一層是乾淨的 Router，三層架構：
    route(task)              ← 路由層：LLM 結構化 JSON 判斷派給誰（fallback：關鍵字版）
    run_agent(agent, task)   ← 執行層：recursive tool-call 迴圈（迴圈+max_turns），
                                工具一律走 A 的 tool_gateway（least-privilege + 自動 log + write 轉審批）
    orchestrate(task)        ← 總指揮：route → run_agent(s) → 彙整回覆（支援多 Agent 早報）

模型層（issue #25）：改用 **LiteLLM Provider Adapter** —— 模型 = 設定字串 `LLM_MODEL`
（預設 gemini/gemini-2.5-flash），換 Gemini / Groq / OpenRouter / Ollama / OpenCode 只要改一行。
工具用 OpenAI 格式（LiteLLM 自動翻譯成各家格式），讀 message.tool_calls。

與隊友的介面不變：
  - A（Tool Gateway）：透過 gateway.call(tool, args, role) 執行，治理鏈完全保留。
  - C（審批）：write 工具由 gateway 回 status="pending"。
  - D（Dashboard）：route() 的決策 dict 寫進派工 log。
"""

from __future__ import annotations

import os
import json
import time
import inspect

import litellm
from cachetools import TTLCache

from backend.agent_registry import (
    AGENTS, DEFAULT_AGENT, route_by_keyword, get_tools_for_agent, get_agent,
)
from backend.tool_gateway import gateway
from backend import tools_mapping

# F8：route() 決策快取（同一任務 5 分鐘內不重打路由 LLM）
_ROUTE_CACHE: TTLCache = TTLCache(maxsize=256, ttl=300)
_VALID_TASK_TYPES = {"single", "multi", "smalltalk"}

litellm.suppress_debug_info = True
litellm.drop_params = True  # 某模型不支援的參數自動丟棄，提高跨供應商相容性

# ── 模型設定：改 LLM_MODEL 即可換供應商（LiteLLM 格式 provider/model）──────────
#   gemini/gemini-2.5-flash · openai/gpt-4o · groq/llama-3.3-70b-versatile ·
#   openrouter/... · ollama/qwen2.5  …
DEFAULT_MODEL = os.getenv("LLM_MODEL", "gemini/gemini-2.5-flash")

# ── F6 供應商 fallback chain：primary（LLM_MODEL）掛掉時依序改打這些模型 ──────
#   逗號分隔、可用 LLM_FALLBACK_MODELS 覆蓋。各模型金鑰由 litellm 讀對應環境變數。
_FALLBACK_MODELS = [
    m.strip() for m in os.getenv(
        "LLM_FALLBACK_MODELS", "openai/kimi-k2.6,gemini/gemini-2.5-flash"
    ).split(",") if m.strip()
]


# ════════════════════════════════════════════════════════════════════════
# 0) 各 Agent 的 system prompt（由 registry 組出，DRY + 單一真實來源）
# ════════════════════════════════════════════════════════════════════════
from backend.prompts import PROMPT_DEFENSE_BASELINE

_COMMON_AGENT_RULES = (
    "【共同規則】\n"
    "1. 你只能使用下列「你的工具」，不可呼叫其他部門的工具。\n"
    "2. 先查資料再下結論，必要時可連續呼叫多個工具。\n"
    "3. 若工具回傳『需審批 / pending』，請告知使用者該操作已送主管審批、等待核准，不可宣稱已完成。\n"
    "4. 若工具回傳『無權限 / denied』，請委婉說明並停止，不要硬試其他工具。\n"
    "5. 列出多筆資料請用 Markdown 表格。\n"
    "6. 全程使用繁體中文。\n\n"
    f"{PROMPT_DEFENSE_BASELINE}"
)


def build_agent_prompt(agent_id: str) -> str:
    """由 registry 組出單一 Agent 的 system prompt（角色+職責+工具清單+規則）。"""
    meta = get_agent(agent_id) or {}
    tools = get_tools_for_agent(agent_id)
    tool_lines = "\n".join(f"  - {t}" for t in tools) if tools else "  （此 Agent 無專屬工具）"
    return (
        f"你是進銷存 ERP 系統中的「{meta.get('name_zh', agent_id)}」"
        f"（{meta.get('name_en', '')}）。\n"
        f"【你的職責】{meta.get('description', '')}\n"
        f"【你的工具】\n{tool_lines}\n\n"
        f"{_COMMON_AGENT_RULES}"
    )


AGENT_SYSTEM_PROMPTS: dict[str, str] = {
    agent_id: build_agent_prompt(agent_id) for agent_id in AGENTS
}


# ════════════════════════════════════════════════════════════════════════
# 1) 總管 system prompt（<agents> 標籤注入 + few-shot + 強制 JSON 輸出）
# ════════════════════════════════════════════════════════════════════════
# few-shot 路由範例（issue #47 P1-2，取自 docs/b_day1_agent_roster_routing.md §3.1；
# 含關鍵字路由會誤判的歧義案例）
_ROUTER_FEWSHOT = (
    "【範例】\n"
    '任務「哪些商品快缺貨了？」→ {"task_type":"single","primary_agent":"inventory_agent",'
    '"agent_chain":["inventory_agent"],"needs_approval":false,"reason":"庫存查詢"}\n'
    '任務「幫客戶王小明下一張 10 台筆電的訂單」→ {"task_type":"single","primary_agent":"sales_agent",'
    '"agent_chain":["sales_agent"],"needs_approval":true,"reason":"建立訂單會寫入資料"}\n'
    '任務「早報：今天整體營運狀況」→ {"task_type":"multi","primary_agent":"cs_agent",'
    '"agent_chain":["inventory_agent","sales_agent","finance_agent","risk_agent","cs_agent"],'
    '"needs_approval":false,"reason":"跨領域總覽，多 Agent 彙整"}\n'
    '任務「台灣供應商受戰爭影響的採購單有哪些」→ {"task_type":"single","primary_agent":"risk_agent",'
    '"agent_chain":["risk_agent"],"needs_approval":false,"reason":"重點是風險影響評估，非一般採購查詢"}\n'
    '任務「幫我算 3500×12」→ {"task_type":"single","primary_agent":"finance_agent",'
    '"agent_chain":["finance_agent"],"needs_approval":false,"reason":"試算意圖，財務 Agent 有 calculate 工具"}\n'
    '任務「你好」→ {"task_type":"smalltalk","primary_agent":"cs_agent",'
    '"agent_chain":["cs_agent"],"needs_approval":false,"reason":"招呼"}'
)

# 路由 prompt 快取（issue #47 P2）：Agent 名單執行期不變，每次 route 重組是浪費
_ORCH_PROMPT_CACHE: str | None = None


def build_orchestrator_prompt() -> str:
    global _ORCH_PROMPT_CACHE
    if _ORCH_PROMPT_CACHE is not None:
        return _ORCH_PROMPT_CACHE
    lines = []
    for agent_id, meta in AGENTS.items():
        tools = get_tools_for_agent(agent_id)
        lines.append(
            f'  <agent id="{agent_id}" name="{meta["name_zh"]}" '
            f'can_write="{str(meta["can_write"]).lower()}">\n'
            f'    職責：{meta["description"]}\n'
            f'    工具：{", ".join(tools) if tools else "（無，僅作 fallback / 彙整）"}\n'
            f'  </agent>'
        )
    agents_block = "<agents>\n" + "\n".join(lines) + "\n</agents>"
    prompt = (
        "你是進銷存 ERP 系統的「總管 Agent（Orchestrator）」。\n"
        "你本身不直接查資料、不呼叫工具，唯一職責是：讀懂使用者任務，"
        "判斷該交給哪一個專責 Agent，必要時規劃多個 Agent 並指定彙整。\n\n"
        "你管理的專責 Agent 名單如下：\n"
        f"{agents_block}\n\n"
        "【判斷規則】\n"
        "1. 先判斷任務領域，選出主責 Agent（primary_agent）。\n"
        "2. 若任務跨多領域，或屬『營運總覽 / 老闆早報』類，task_type 設為 multi，"
        "在 agent_chain 依執行順序列出多個 Agent，最後由 cs_agent 彙整。\n"
        "3. 若任務會動到資料（建立訂單、更新庫存等），needs_approval 設為 true；"
        "你只負責派工，實際寫入會被攔截送人工審批，不可宣稱已完成。\n"
        "4. 招呼 / 閒聊 / 無法歸類 → primary_agent 設 cs_agent，task_type 設 smalltalk。\n"
        "5. 只能從上方名單挑 agent id，絕對不可自創不存在的 Agent。\n\n"
        f"{_ROUTER_FEWSHOT}\n\n"
        f"{PROMPT_DEFENSE_BASELINE}\n\n"
        "【輸出格式】只輸出以下 JSON，不要任何多餘文字或 markdown 標記：\n"
        "{\n"
        '  "task_type": "single | multi | smalltalk",\n'
        '  "primary_agent": "<agent id>",\n'
        '  "agent_chain": ["<agent id>", ...],\n'
        '  "needs_approval": true | false,\n'
        '  "reason": "<一句話說明為何這樣派>"\n'
        "}"
    )
    _ORCH_PROMPT_CACHE = prompt
    return prompt


# ════════════════════════════════════════════════════════════════════════
# 2) 統一 LLM 呼叫層（LiteLLM）+ 429 重試
# ════════════════════════════════════════════════════════════════════════
def _parse_retry_delay(err: Exception) -> float | None:
    import re
    m = re.search(r"ret ?ryDelay['\":\s]+(\d+(?:\.\d+)?)s", str(err))
    return float(m.group(1)) if m else None


def _is_transient(e: Exception) -> bool:
    s = str(e).lower()
    return ("429" in s or "rate limit" in s or "ratelimit" in s
            or "resource_exhausted" in s or "quota" in s or "overloaded" in s)


def _completion_with_retry(kw: dict, max_retries: int):
    """單一模型呼叫 + 429/額度重試（暫時性錯誤才重試，其他直接拋出）。"""
    for attempt in range(max_retries):
        try:
            return litellm.completion(**kw)
        except Exception as e:
            if _is_transient(e) and attempt < max_retries - 1:
                time.sleep((_parse_retry_delay(e) or (5 * (2 ** attempt))) + 1.0)
            else:
                raise


def _llm(messages, model=None, tools=None, temperature=0.2, json_mode=False,
         api_key=None, api_base=None, max_retries: int = 5, usage_tag: str = ""):
    """
    統一 LLM 呼叫。回傳 litellm response。

    F6 供應商 fallback：預設路徑（未指定 model/api_key/api_base）下，
    primary（LLM_MODEL）失敗會依序改打 _FALLBACK_MODELS，避免單一供應商
    掉線導致 Demo 中斷。呼叫端明確指定模型時（如側邊欄 Gemini key 路徑）
    維持單一模型行為、不偷換模型。

    用量記帳：每次成功呼叫記一列 llm_usage_logs（tokens + 成本），
    usage_tag 標記用途（route / agent:<id> / aggregate / smalltalk）供歸因。
    """
    kw = {"messages": messages, "temperature": temperature}
    if tools:
        kw["tools"] = tools
        kw["tool_choice"] = "auto"
    if json_mode:
        kw["response_format"] = {"type": "json_object"}
    if api_key:
        kw["api_key"] = api_key
    if api_base:
        kw["api_base"] = api_base

    explicit = bool(model or api_key or api_base)
    chain = [model or DEFAULT_MODEL]
    if not explicit:
        chain += [m for m in _FALLBACK_MODELS if m not in chain]

    # 多模型時每個模型少重試幾次，讓 fallback 快點接手（現場等 80 秒不如換一家）
    per_model_retries = max_retries if len(chain) == 1 else min(max_retries, 2)

    last_err: Exception | None = None
    for mdl in chain:
        try:
            resp = _completion_with_retry({**kw, "model": mdl}, per_model_retries)
            # 用量記帳（fire-and-forget，失敗不影響主流程）
            from backend.usage_logger import log_usage
            log_usage(usage_tag or "untagged", mdl, resp)
            return resp
        except Exception as e:
            last_err = e
            continue  # 換下一家供應商
    raise last_err


def _content(resp) -> str:
    try:
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


# ════════════════════════════════════════════════════════════════════════
# 2.5) 對話記憶（session 內 sliding window）
# ════════════════════════════════════════════════════════════════════════
def _trim_history(history, max_msgs: int = 8, max_chars: int = 1200) -> list[dict]:
    """
    把前端對話紀錄整理成可餵給 LLM 的 history：
      - 只留最近 max_msgs 則（sliding window；研究顯示優於摘要式壓縮）
      - 單則超過 max_chars 截斷（防止表格類長回覆吃光 context）
      - 只帶 user / assistant 文本（Streamlit 的 "model" 角色映射為 assistant），
        不帶工具 payload —— 訊號乾淨、也避免 log/PII 進 context
    """
    out = []
    for m in (history or [])[-max_msgs:]:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if role == "model":
            role = "assistant"
        if role not in ("user", "assistant") or not content:
            continue
        if len(content) > max_chars:
            content = content[:max_chars] + "…（截斷）"
        out.append({"role": role, "content": content})
    return out


# ════════════════════════════════════════════════════════════════════════
# 3) 路由層 route()
# ════════════════════════════════════════════════════════════════════════
def _route_fallback(task: str) -> dict:
    agent_id = route_by_keyword(task)
    meta = get_agent(agent_id) or {}
    return {
        "task_type": "smalltalk" if agent_id == DEFAULT_AGENT else "single",
        "primary_agent": agent_id,
        "agent_chain": [agent_id],
        "needs_approval": bool(meta.get("can_write", False)),
        "reason": "關鍵字路由 fallback（未使用 LLM）",
        "routed_by": "keyword",
    }


def _coerce_route(raw: dict) -> dict:
    """F9：對 LLM 路由 JSON 做完整 schema 驗證，不合法值退回安全預設。"""
    primary = raw.get("primary_agent")
    if primary not in AGENTS:
        primary = DEFAULT_AGENT

    chain = raw.get("agent_chain") or [primary]
    if not isinstance(chain, list):
        chain = [primary]
    chain = [a for a in chain if a in AGENTS] or [primary]

    task_type = raw.get("task_type", "single")
    if task_type not in _VALID_TASK_TYPES:
        task_type = "single"
    if task_type == "smalltalk":
        primary = DEFAULT_AGENT
        chain = [DEFAULT_AGENT]

    needs_approval = raw.get("needs_approval", False)
    if not isinstance(needs_approval, bool):
        needs_approval = bool(needs_approval)

    return {
        "task_type": task_type,
        "primary_agent": primary,
        "agent_chain": chain,
        "needs_approval": needs_approval,
        "reason": str(raw.get("reason", ""))[:200],
        "routed_by": "llm",
    }


def route(task: str, model=None, api_key=None, api_base=None, use_llm: bool = True,
          history: list | None = None) -> dict:
    """
    判斷任務該派給誰。use_llm=False 或呼叫失敗時退回關鍵字版。
    history：近期對話（供理解「再多 20 個」這類指代型後續任務）。
    回傳：{task_type, primary_agent, agent_chain, needs_approval, reason, routed_by}

    F8：use_llm=True 且無 history 的路由決策加 5 分鐘 TTL cache（同一任務不重打 LLM）。
    """
    if not use_llm:
        return _route_fallback(task)

    cache_key = (task or "").strip()
    if cache_key and not history:
        cached = _ROUTE_CACHE.get(cache_key)
        if cached:
            return cached

    user_content = task
    trimmed = _trim_history(history)
    if trimmed:
        recap = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in trimmed[-4:])
        user_content = (
            f"（以下為先前對話摘錄，僅供理解目前任務的指代，不需回應）\n{recap}\n\n"
            f"【目前任務】{task}"
        )

    try:
        resp = _llm(
            [{"role": "system", "content": build_orchestrator_prompt()},
             {"role": "user", "content": user_content}],
            model=model, temperature=0.0, json_mode=True,
            api_key=api_key, api_base=api_base, usage_tag="route",
        )
        result = _coerce_route(json.loads(_content(resp)))
        if cache_key and not history:
            _ROUTE_CACHE[cache_key] = result
        return result
    except Exception:
        return _route_fallback(task)


# ════════════════════════════════════════════════════════════════════════
# 4) 工具 schema（OpenAI 格式，由函式簽名 + A 的分級描述自動生成）
# ════════════════════════════════════════════════════════════════════════
_PYTYPE = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _tool_to_schema(name: str) -> dict | None:
    func = tools_mapping.get(name)
    if func is None:
        return None
    props, required = {}, []
    for pn, p in inspect.signature(func).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        ann = p.annotation if p.annotation is not inspect.Parameter.empty else str
        props[pn] = {"type": _PYTYPE.get(ann, "string"), "description": pn}
        if p.default is inspect.Parameter.empty:
            required.append(pn)
    desc = name
    try:
        from backend.tool_registry import registry
        info = registry.get_tool_info(name)
        if info:
            desc = info.get("description", name)
    except Exception:
        pass
    params = {"type": "object", "properties": props}
    if required:
        params["required"] = required
    return {"type": "function",
            "function": {"name": name, "description": desc, "parameters": params}}


def _agent_tools(agent_id: str) -> list:
    out = []
    for name in get_tools_for_agent(agent_id):
        s = _tool_to_schema(name)
        if s:
            out.append(s)
    return out


def execute_tool_call(tool_name: str, args: dict, role: str, agent_id: str = "") -> dict:
    """經 A 的 Tool Gateway 執行單一工具呼叫（治理鏈在此）。可離線測。
    F1：agent_id 會傳入 gateway.call(agent_name=)，讓 Agent 白名單檢查真正生效。"""
    res = gateway.call(tool_name, args or {}, role, agent_name=agent_id)
    if res.status == "ok":
        return {"status": "ok", "result": res.data}
    if res.status == "pending":
        return {"status": "pending", "message": res.message, "approval_id": res.approval_id}
    if res.status == "denied":
        return {"status": "denied", "message": res.message}
    return {"status": "error", "message": res.message}


# ════════════════════════════════════════════════════════════════════════
# 5) 執行層 run_agent() — LiteLLM 迴圈，recursive tool-call 形狀
# ════════════════════════════════════════════════════════════════════════
def run_agent(agent_id: str, task: str, role: str,
              model=None, api_key=None, api_base=None, max_turns: int = 6,
              history: list | None = None) -> dict:
    """
    讓指定專責 Agent 處理任務：LLM 推理 → 讀 tool_calls → 交 gateway 執行 → 回灌 → 再推理。
    history：近期對話（sliding window，_trim_history 整理後夾在 system 與本輪任務之間）。
    回傳：{agent, reply, tool_calls:[...], pending:[...]}
    """
    sys_prompt = AGENT_SYSTEM_PROMPTS.get(agent_id, "")
    tools = _agent_tools(agent_id)
    messages = ([{"role": "system", "content": sys_prompt}]
                + _trim_history(history)
                + [{"role": "user", "content": task}])

    tool_calls_made: list[dict] = []
    pending: list[dict] = []
    reply_text = ""

    for _turn in range(max_turns):
        resp = _llm(messages, model=model, tools=tools or None, temperature=0.2,
                    api_key=api_key, api_base=api_base, usage_tag=f"agent:{agent_id}")
        msg = resp.choices[0].message
        tcs = getattr(msg, "tool_calls", None)

        if not tcs:
            reply_text = (msg.content or "").strip()
            break

        # 回灌 assistant 訊息（含 tool_calls），格式 portable 不依賴 litellm 版本
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tcs
            ],
        })

        for tc in tcs:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            outcome = execute_tool_call(tc.function.name, args, role, agent_id=agent_id)
            tool_calls_made.append({"tool": tc.function.name, "args": args,
                                    "outcome": outcome["status"]})
            if outcome["status"] == "pending":
                pending.append({"tool": tc.function.name, "args": args,
                                "approval_id": outcome.get("approval_id")})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": tc.function.name,
                "content": json.dumps(outcome, ensure_ascii=False),
            })
    else:
        reply_text = reply_text or "（已達思考輪數上限）"

    # ── F2 治理 code-gate：有操作被攔下送審批時，回覆「必定」如實揭露，
    #    不交給 LLM 自由措辭（防止幻覺宣稱已完成）。與 LINE 端行為一致。
    if pending:
        ids = ", ".join(str(p.get("approval_id") or "?") for p in pending)
        reply_text = (
            f"⚠️ 已送 {len(pending)} 筆審批（{ids}），**尚未執行**，"
            f"請至 Dashboard 由管理者核准後才會生效。\n\n" + (reply_text or "")
        ).strip()

    return {
        "agent": agent_id,
        "reply": reply_text or "（無回覆內容）",
        "tool_calls": tool_calls_made,
        "pending": pending,
    }


# ════════════════════════════════════════════════════════════════════════
# 6) 總指揮 orchestrate()
# ════════════════════════════════════════════════════════════════════════
def orchestrate(task: str, role: str = "admin",
                model=None, api_key=None, api_base=None, use_llm: bool = True,
                history: list | None = None) -> dict:
    """
    端到端入口：判斷派工 → 執行 → 彙整。
    模型由 model（LiteLLM 格式字串）決定，預設 LLM_MODEL；api_key/api_base 可覆蓋。
    use_llm=False → 純關鍵字路由、不實際執行（離線/無金鑰測試）。
    history：近期對話（前端原始格式即可，內部會 _trim_history 整理）。
    回傳：{routing, results:[...], reply, pending:[...]}
    """
    hist = _trim_history(history)
    routing = route(task, model=model, api_key=api_key, api_base=api_base,
                    use_llm=use_llm, history=hist)

    try:
        from backend.dispatch_logger import write_dispatch_log
        write_dispatch_log(routing, task, caller=role)
    except Exception:
        pass

    chain = routing["agent_chain"] if routing["task_type"] == "multi" else [routing["primary_agent"]]

    if routing["task_type"] == "smalltalk" or not use_llm:
        return {"routing": routing, "results": [], "pending": [],
                "reply": _reply_without_tools(task, routing, model, api_key, api_base,
                                              use_llm, history=hist)}

    try:
        results = [run_agent(aid, task, role, model=model, api_key=api_key, api_base=api_base,
                             history=hist)
                   for aid in chain]
    except Exception as e:
        name = (get_agent(routing["primary_agent"]) or {}).get("name_zh", routing["primary_agent"])
        return {"routing": routing, "results": [], "pending": [],
                "reply": f"[模型呼叫失敗] 已路由給「{name}」（{routing['routed_by']}）。錯誤：{e}"}

    pending = [p for r in results for p in r["pending"]]
    reply = results[0]["reply"] if len(results) == 1 else _aggregate(task, results, model, api_key, api_base)
    return {"routing": routing, "results": results, "pending": pending, "reply": reply}


def _reply_without_tools(task, routing, model, api_key, api_base, use_llm,
                         history: list | None = None) -> str:
    if not use_llm:
        name = (get_agent(routing["primary_agent"]) or {}).get("name_zh", routing["primary_agent"])
        return f"[離線模式] 已將任務路由給「{name}」（{routing['routed_by']}）。設定模型/金鑰後即可實際執行。"
    try:
        resp = _llm(
            [{"role": "system", "content": "你是進銷存 ERP 的客服助理，請用繁體中文友善簡短回覆。\n\n"
              + PROMPT_DEFENSE_BASELINE}]
            + _trim_history(history)
            + [{"role": "user", "content": task}],
            model=model, temperature=0.5, api_key=api_key, api_base=api_base,
            usage_tag="smalltalk")
        return _content(resp) or "您好，請問需要什麼協助？"
    except Exception:
        return "您好，請問需要什麼協助？"


def _governance_footer(results) -> str:
    """F3：由 code 組出治理狀態附註（pending / denied），不經 LLM、不會被彙整洗掉。"""
    pending = [(r["agent"], p) for r in results for p in r.get("pending", [])]
    denied = [(r["agent"], tc) for r in results
              for tc in r.get("tool_calls", []) if tc.get("outcome") == "denied"]
    if not pending and not denied:
        return ""
    lines = ["", "---", "**📋 治理狀態（系統自動附註）**"]
    if pending:
        lines.append(f"- ⚠️ {len(pending)} 筆操作已送審批、**尚未執行**，請至 Dashboard 核准：")
        for agent_id, p in pending:
            name = (get_agent(agent_id) or {}).get("name_zh", agent_id)
            lines.append(f"  - {name}｜{p.get('tool', '?')}（{p.get('approval_id', '?')}）")
    if denied:
        lines.append(f"- ⛔ {len(denied)} 筆工具呼叫因權限不足被**拒絕**：")
        for agent_id, tc in denied:
            name = (get_agent(agent_id) or {}).get("name_zh", agent_id)
            lines.append(f"  - {name}｜{tc.get('tool', '?')}")
    return "\n".join(lines)


def _aggregate(task, results, model, api_key, api_base) -> str:
    blocks = "\n\n".join(
        f"【{get_agent(r['agent'])['name_zh']}】\n{r['reply']}" for r in results
    )
    # F3 治理 code-gate：治理狀態由 code 附註在彙整結果之後，
    # 彙整 LLM 無論怎麼摘要都無法把 pending / denied 訊號洗掉。
    footer = _governance_footer(results)
    try:
        resp = _llm(
            [{"role": "system", "content": "你是進銷存 ERP 的總管彙整助理，"
              "請把各部門回報整合成一份精簡、好讀的營運摘要（可用 Markdown）。\n\n"
              + PROMPT_DEFENSE_BASELINE},
             {"role": "user", "content": f"使用者任務：{task}\n\n各部門回報：\n{blocks}\n\n請彙整成一份條理清楚的繁體中文摘要回覆。"}],
            model=model, temperature=0.3, api_key=api_key, api_base=api_base,
            usage_tag="aggregate")
        return ((_content(resp) or blocks) + footer).strip()
    except Exception:
        return (blocks + footer).strip()
