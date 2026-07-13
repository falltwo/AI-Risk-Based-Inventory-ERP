"""
backend/usage_logger.py
LLM 用量/成本記帳 — 每次 LLM 呼叫記一列（tokens + 估算成本 USD）。

設計：
  - 資料進獨立表 llm_usage_logs（不動 dispatch/action log，與稽核鏈解耦）。
  - 由 agent_orchestrator._llm 單一漏斗呼叫，tag 標記用途
    （route / agent:<id> / aggregate / smalltalk），供成本歸因。
  - 記帳失敗絕不影響主流程（全程 try/except 吞掉）。
  - 成本用 litellm.completion_cost() 查內建價目表；查不到（自訂端點
    如 OpenCode 模型）記 NULL，tokens 仍照記。
"""

from datetime import datetime

from backend.database import run_query

_TABLE = "llm_usage_logs"


def ensure_table():
    run_query(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            tag               TEXT,
            model             TEXT,
            prompt_tokens     INTEGER,
            completion_tokens INTEGER,
            total_tokens      INTEGER,
            cost_usd          REAL,
            timestamp         TEXT
        )
        """,
        fetch=False,
    )


def _usage_field(usage, key):
    """usage 可能是物件或 dict，兩者都取得到。"""
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


def log_usage(tag: str, model: str, resp) -> None:
    """從 litellm response 抽 usage 記一列。任何失敗都靜默（不影響主流程）。"""
    try:
        usage = getattr(resp, "usage", None)
        prompt_tokens = _usage_field(usage, "prompt_tokens")
        completion_tokens = _usage_field(usage, "completion_tokens")
        total_tokens = _usage_field(usage, "total_tokens")
        if total_tokens is None and prompt_tokens is None:
            return  # 沒有 usage 資訊（如測試假回應），不記

        cost_usd = None
        try:
            import litellm
            cost_usd = litellm.completion_cost(completion_response=resp)
        except Exception:
            pass  # 價目表查不到（自訂端點模型），cost 留 NULL

        ensure_table()
        run_query(
            f"""
            INSERT INTO {_TABLE}
                (tag, model, prompt_tokens, completion_tokens, total_tokens, cost_usd, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (tag, model, prompt_tokens, completion_tokens, total_tokens, cost_usd,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            fetch=False,
        )
    except Exception:
        pass  # ponytail: 記帳失敗不值得讓任務失敗


def get_usage_summary(days: int = 7) -> list[dict]:
    """近 N 天依 tag 彙總（給 Dashboard 治理效益 tab 用）。"""
    ensure_table()
    rows = run_query(
        f"""
        SELECT tag, model, COUNT(*) AS calls,
               SUM(COALESCE(total_tokens, 0)) AS tokens,
               SUM(COALESCE(cost_usd, 0.0)) AS cost
        FROM {_TABLE}
        WHERE timestamp >= datetime('now', ?)
        GROUP BY tag, model
        ORDER BY tokens DESC
        """,
        (f"-{int(days)} days",),
    )
    return [
        {"tag": r[0], "model": r[1], "calls": r[2], "tokens": r[3], "cost_usd": r[4]}
        for r in rows
    ]
