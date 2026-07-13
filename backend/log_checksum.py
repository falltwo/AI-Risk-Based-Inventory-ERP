"""
backend/log_checksum.py
日誌雜湊鏈模組：為 agent_action_logs、pending_approvals、agent_dispatch_logs
三張表提供防篡改的雜湊鏈（hash chain）機制。

原理：
  每筆記錄的 checksum = SHA256(前筆 checksum + 本筆資料內容)
  形成一條鏈，任何竄改都會導致後續所有 checksum 失效，可被偵測。
"""

import hashlib
import json
from backend.database import run_query, tx_run

GENESIS_HASH = "0000000000000000000000000000000000000000000000000000000000000000"


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _get_prev_checksum(table: str, id_col: str = "id", order: str = "DESC", conn=None) -> str:
    """
    取得指定表單中最後一筆記錄的 checksum，作為新記錄的前導雜湊。
    若表為空，回傳創世雜湊 GENESIS_HASH。
    若傳入 conn（transaction 連線），則在同交易內讀取，保證原子性。
    """
    query = f"SELECT checksum FROM {table} ORDER BY {id_col} {order} LIMIT 1"
    if conn:
        rows = tx_run(conn, query)
    else:
        rows = run_query(query)
    if rows and rows[0][0]:
        return rows[0][0]
    return GENESIS_HASH


def compute_checksum(prev_checksum: str, *fields) -> str:
    """
    計算本筆記錄的 checksum。
    prev_checksum: 前筆記錄的 checksum（或 GENESIS_HASH）
    fields: 本筆記錄的各欄位值（依序）
    
    使用 json.dumps 序列化以避免 | 分隔符邊界歧義（欄位值若含 | 會構造出相同 checksum）。
    """
    row_data = json.dumps([str(f) for f in fields], ensure_ascii=False, separators=(",", ":"))
    return _sha256(prev_checksum + row_data)


def verify_log_chain(table: str, id_col: str = "id", columns: tuple = ()) -> dict:
    """
    驗證指定表的雜湊鏈完整性。

    回傳 {"valid": bool, "tampered_rows": list, "legacy_rows": int}：
      - valid=True  表示鏈完整（含無舊列的情況）
      - valid=False 且 tampered_rows 列出被竄改的記錄 ID
      - legacy_rows 為 migration 前尚未補算 checksum 的列數（不予驗證）
    """
    query = f"SELECT {id_col}, checksum, {', '.join(columns)} FROM {table} ORDER BY {id_col} ASC"
    rows = run_query(query)
    if not rows:
        return {"valid": True, "tampered_rows": [], "legacy_rows": 0}

    tampered = []
    legacy = 0
    prev = None
    id_idx = 0
    checksum_idx = 1
    data_start_idx = 2
    chain_started = False

    for row in rows:
        stored_checksum = row[checksum_idx]

        # Migration 前舊列 checksum 為 NULL，跳過不驗證
        if stored_checksum is None:
            legacy += 1
            continue

        data_fields = row[data_start_idx:]

        if not chain_started:
            # 第一筆有 checksum 的列：此為雜湊鏈起點
            prev = stored_checksum
            chain_started = True
            continue

        computed = compute_checksum(prev, *data_fields)
        if stored_checksum != computed:
            tampered.append(row[id_idx])
        prev = stored_checksum

    return {"valid": len(tampered) == 0, "tampered_rows": tampered, "legacy_rows": legacy}


def verify_agent_action_logs() -> dict:
    """驗證 agent_action_logs 表的雜湊鏈"""
    return verify_log_chain(
        "agent_action_logs",
        id_col="id",
        columns=("tool_name", "parameters", "caller", "result", "success", "timestamp"),
    )


def verify_pending_approvals() -> dict:
    """驗證 pending_approvals 表的雜湊鏈（依 created_at 排序）"""
    return verify_log_chain(
        "pending_approvals",
        id_col="approval_id",
        columns=("tool_name", "parameters", "requester", "status", "approver", "created_at", "updated_at", "reason"),
    )


def verify_agent_dispatch_logs() -> dict:
    """驗證 agent_dispatch_logs 表的雜湊鏈"""
    return verify_log_chain(
        "agent_dispatch_logs",
        id_col="id",
        columns=("task", "task_type", "primary_agent", "agent_chain", "routed_by", "needs_approval", "reason", "caller", "timestamp"),
    )


def rebuild_chain_from(table: str, id_col: str, start_id, columns: tuple) -> None:
    """
    從指定記錄開始重新計算該表後續所有記錄的 checksum。
    用於 pending_approvals 的 UPDATE 操作後，重建鏈。
    整段包在 transaction() 內，避免中途崩潰殘留半條新鏈半條舊鏈。
    """
    from backend.database import transaction
    with transaction() as conn:
        query = f"SELECT {id_col}, {', '.join(columns)} FROM {table} ORDER BY {id_col} ASC"
        rows = tx_run(conn, query)

        prev_checksum = None
        found_start = False
        for row in rows:
            row_id = row[0]
            data_fields = row[1:]
            if row_id == start_id:
                found_start = True
                prev_query = f"SELECT checksum FROM {table} WHERE {id_col} < ? ORDER BY {id_col} DESC LIMIT 1"
                prev_rows = tx_run(conn, prev_query, (start_id,))
                prev_checksum = prev_rows[0][0] if prev_rows and prev_rows[0][0] else GENESIS_HASH

            if found_start:
                new_checksum = compute_checksum(prev_checksum, *data_fields)
                update_query = f"UPDATE {table} SET checksum = ? WHERE {id_col} = ?"
                tx_run(conn, update_query, (new_checksum, row_id), fetch=False)
                prev_checksum = new_checksum
