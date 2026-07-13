"""
backend/agent_logger.py
Agent 動作日誌與審批日誌讀寫模組
"""

import json
from datetime import datetime
from backend.database import run_query, transaction, tx_run
from backend.log_checksum import _get_prev_checksum, compute_checksum


def write_action_log(tool_name: str, args: dict, role: str, result: str, success: bool):
    """
    寫入工具呼叫紀錄至 agent_action_logs 表中，含雜湊鏈 checksum。
    讀 prev_hash → 算 row_hash → 寫入 在同一 transaction 內原子完成。
    """
    parameters_str = json.dumps(args or {}, ensure_ascii=False)
    success_int = 1 if success else 0
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with transaction() as conn:
        prev_checksum = _get_prev_checksum("agent_action_logs", id_col="id", order="DESC", conn=conn)
        checksum = compute_checksum(prev_checksum, tool_name, parameters_str, role, str(result), success_int, timestamp)

        query = """
            INSERT INTO agent_action_logs (tool_name, parameters, caller, result, success, timestamp, checksum)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        tx_run(conn, query, (tool_name, parameters_str, role, str(result), success_int, timestamp, checksum), fetch=False)


def create_pending_approval(tool_name: str, args: dict, role: str) -> str:
    """
    建立待審批項目至 pending_approvals 表中，含雜湊鏈 checksum，回傳 approval_id。
    讀 prev_hash → 算 row_hash → 寫入 在同一 transaction 內原子完成。
    """
    timestamp_str = datetime.now().strftime("%Y%m%d%H%M%S%f")
    approval_id = f"PENDING-{timestamp_str}-{tool_name}"
    parameters_str = json.dumps(args or {}, ensure_ascii=False)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with transaction() as conn:
        prev_checksum = _get_prev_checksum("pending_approvals", id_col="approval_id", order="DESC", conn=conn)
        checksum = compute_checksum(prev_checksum, tool_name, parameters_str, role, "pending", None, created_at, created_at, None)

        query = """
            INSERT INTO pending_approvals (approval_id, tool_name, parameters, requester, status, approver, created_at, updated_at, reason, checksum)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        tx_run(conn, query, (approval_id, tool_name, parameters_str, role, 'pending', None, created_at, created_at, None, checksum), fetch=False)
    return approval_id


def get_action_logs(limit: int = 100) -> list[dict]:
    """
    查詢近期工具呼叫日誌，回傳 dict 列表（給前端/控制台介面）。
    """
    query = """
        SELECT id, tool_name, parameters, caller, result, success, timestamp, checksum
        FROM agent_action_logs
        ORDER BY id DESC
        LIMIT ?
    """
    rows = run_query(query, (limit,))
    logs = []
    for row in rows:
        try:
            params = json.loads(row[2])
        except Exception:
            params = row[2]
            
        logs.append({
            "id": row[0],
            "tool_name": row[1],
            "parameters": params,
            "caller": row[3],
            "result": row[4],
            "success": bool(row[5]),
            "timestamp": row[6],
            "checksum": row[7],
        })
    return logs


def get_pending_approvals(status_filter: str = None) -> list[dict]:
    """
    查詢審批項目列表。若指定 status_filter (例如 'pending', 'approved', 'rejected') 則進行過濾。
    """
    if status_filter:
        query = """
            SELECT approval_id, tool_name, parameters, requester, status, approver, created_at, updated_at, reason, checksum
            FROM pending_approvals
            WHERE status = ?
            ORDER BY created_at DESC
        """
        rows = run_query(query, (status_filter,))
    else:
        query = """
            SELECT approval_id, tool_name, parameters, requester, status, approver, created_at, updated_at, reason, checksum
            FROM pending_approvals
            ORDER BY created_at DESC
        """
        rows = run_query(query)
        
    approvals = []
    for row in rows:
        try:
            params = json.loads(row[2])
        except Exception:
            params = row[2]
            
        approvals.append({
            "approval_id": row[0],
            "tool_name": row[1],
            "parameters": params,
            "requester": row[3],
            "status": row[4],
            "approver": row[5],
            "created_at": row[6],
            "updated_at": row[7],
            "reason": row[8],
            "checksum": row[9],
        })
    return approvals


def update_approval_status(approval_id: str, approver: str, status: str, reason: str = None) -> bool:
    """
    更新待審批項目的狀態（例如核准/拒絕），並寫入核准者、更新時間與拒絕原因。
    更新後重新計算該筆及其之後所有記錄的 checksum 鏈。
    """
    from backend.log_checksum import rebuild_chain_from

    query = """
        UPDATE pending_approvals
        SET status = ?, approver = ?, updated_at = ?, reason = ?
        WHERE approval_id = ?
    """
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_query(query, (status, approver, updated_at, reason, approval_id), fetch=False)

    rebuild_chain_from(
        "pending_approvals",
        id_col="approval_id",
        start_id=approval_id,
        columns=("tool_name", "parameters", "requester", "status", "approver", "created_at", "updated_at", "reason"),
    )
    return True


def get_pending_approval_by_id(approval_id: str) -> dict | None:
    """
    根據審批單 ID 查詢特定審批項目。
    """
    query = """
        SELECT approval_id, tool_name, parameters, requester, status, approver, created_at, updated_at, reason, checksum
        FROM pending_approvals
        WHERE approval_id = ?
    """
    rows = run_query(query, (approval_id,))
    if not rows:
        return None
    row = rows[0]
    try:
        params = json.loads(row[2])
    except Exception:
        params = row[2]
        
    return {
        "approval_id": row[0],
        "tool_name": row[1],
        "parameters": params,
        "requester": row[3],
        "status": row[4],
        "approver": row[5],
        "created_at": row[6],
        "updated_at": row[7],
        "reason": row[8],
        "checksum": row[9],
    }


def get_pending_list() -> list[dict]:
    """
    查詢待審批清單，格式化為 D (Agent Dashboard) 所需之欄位結構。
    """
    from backend.agent_registry import get_agent_for_tool
    from backend.tool_registry import registry
    
    rows = get_pending_approvals(status_filter="pending")
    result = []
    for r in rows:
        tool_name = r["tool_name"]
        agent_id = get_agent_for_tool(tool_name) or "unknown_agent"
        risk_level = registry.get_risk_level(tool_name) or "write"
        
        result.append({
            "id": r["approval_id"],
            "time": r["created_at"],
            "agent": agent_id,
            "tool": tool_name,
            "args": r["parameters"],
            "role": r["requester"],
            "risk": risk_level
        })
    return result


def approve_action(approval_id: str) -> dict:
    """
    提供給 A (Gateway) 與 D (Dashboard) 的核准操作介面。
    核准特定審批項目，並真正執行該工具操作。
    """
    from backend.tool_gateway import gateway
    res = gateway.approve_action(approval_id, approver="admin")
    return res.to_dict()


def reject_action(approval_id: str, reason: str) -> dict:
    """
    提供給 A (Gateway) 與 D (Dashboard) 的拒絕操作介面。
    拒絕特定審批項目，操作作廢並記錄原因。
    """
    from backend.tool_gateway import gateway
    res = gateway.reject_action(approval_id, reason, approver="admin")
    return res.to_dict()
