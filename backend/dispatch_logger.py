"""
backend/dispatch_logger.py
派工紀錄（Dispatch Log）讀寫 — 記錄「哪個任務被派給哪個 Agent」

這是 B（總管 Agent）的派工『決策』紀錄，與 C 的兩張表互補、不重複：
  - C 的 agent_action_logs  ：哪個『工具』被呼叫（tool 層）
  - C 的 pending_approvals  ：寫入操作的審批
  - 本檔 agent_dispatch_logs：哪個『任務』被派給哪個 Agent（orchestrator 層）

給 D 的 Dashboard「派工結果」區塊用：
    from backend.dispatch_logger import get_recent_dispatches
    rows = get_recent_dispatches(limit=50)   # list[dict]

風格對齊 C 的 backend/agent_logger.py（run_query + json 參數 + dict 回傳）。
"""

import json
from datetime import datetime
from backend.database import run_query, tx_run
from backend.log_checksum import _get_prev_checksum, compute_checksum

_TABLE = "agent_dispatch_logs"


def ensure_table():
    """建立派工紀錄表（若不存在）。採 IF NOT EXISTS，免另跑 migration。"""
    run_query(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            task           TEXT,
            task_type      TEXT,
            primary_agent  TEXT,
            agent_chain    TEXT,
            routed_by      TEXT,
            needs_approval INTEGER,
            reason         TEXT,
            caller         TEXT,
            timestamp      TEXT,
            checksum       TEXT
        )
        """,
        fetch=False,
    )
    # 向後相容：為舊版資料表加入 checksum 欄位
    try:
        run_query(f"ALTER TABLE {_TABLE} ADD COLUMN checksum TEXT", fetch=False)
    except Exception:
        pass


def write_dispatch_log(routing: dict, task: str, caller: str = "system"):
    """
    寫入一筆派工紀錄，含雜湊鏈 checksum。
    routing 為 orchestrator.route() 的輸出 dict。
    讀 prev_hash → 算 row_hash → 寫入 在同一 transaction 內原子完成。
    """
    ensure_table()
    task_type = routing.get("task_type")
    primary_agent = routing.get("primary_agent")
    agent_chain_str = json.dumps(routing.get("agent_chain") or [], ensure_ascii=False)
    routed_by = routing.get("routed_by")
    needs_approval_int = 1 if routing.get("needs_approval") else 0
    reason = routing.get("reason")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    from backend.database import transaction
    with transaction() as conn:
        prev_checksum = _get_prev_checksum(_TABLE, id_col="id", order="DESC", conn=conn)
        checksum = compute_checksum(prev_checksum, task, task_type, primary_agent, agent_chain_str, routed_by, needs_approval_int, reason, caller, timestamp)

        query = f"""
            INSERT INTO {_TABLE}
                (task, task_type, primary_agent, agent_chain, routed_by, needs_approval, reason, caller, timestamp, checksum)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        tx_run(conn, query, (task, task_type, primary_agent, agent_chain_str, routed_by, needs_approval_int, reason, caller, timestamp, checksum), fetch=False)


def get_recent_dispatches(limit: int = 100) -> list[dict]:
    """
    查詢近期派工紀錄（給 Dashboard 顯示「哪個任務派給哪個 Agent」）。
    回傳 dict 列表，欄位：id, task, task_type, primary_agent, agent_chain(list),
                          routed_by, needs_approval(bool), reason, caller, timestamp
    """
    ensure_table()
    rows = run_query(
        f"""
        SELECT id, task, task_type, primary_agent, agent_chain,
               routed_by, needs_approval, reason, caller, timestamp, checksum
        FROM {_TABLE}
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    out = []
    for r in rows:
        try:
            chain = json.loads(r[4])
        except Exception:
            chain = r[4]
        out.append({
            "id": r[0],
            "task": r[1],
            "task_type": r[2],
            "primary_agent": r[3],
            "agent_chain": chain,
            "routed_by": r[5],
            "needs_approval": bool(r[6]),
            "reason": r[7],
            "caller": r[8],
            "timestamp": r[9],
            "checksum": r[10],
        })
    return out
