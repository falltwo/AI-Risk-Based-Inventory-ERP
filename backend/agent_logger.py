"""
backend/agent_logger.py
Agent 動作日誌與審批日誌讀寫模組
"""

import hmac
import json
from datetime import datetime
from backend.database import run_query, transaction, tx_run
from backend.log_checksum import _get_prev_checksum, compute_checksum


_PROTECTED_APPROVAL_CONTEXT = object()


def write_action_log(
    tool_name: str,
    args: dict,
    role: str,
    result: str,
    success: bool,
    *,
    conn=None,
):
    """
    寫入工具呼叫紀錄至 agent_action_logs 表中，含雜湊鏈 checksum。
    讀 prev_hash → 算 row_hash → 寫入 在同一 transaction 內原子完成。
    """
    parameters_str = json.dumps(args or {}, ensure_ascii=False)
    success_int = 1 if success else 0
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _write(active_conn):
        prev_checksum = _get_prev_checksum(
            "agent_action_logs", id_col="id", order="DESC", conn=active_conn
        )
        checksum = compute_checksum(prev_checksum, tool_name, parameters_str, role, str(result), success_int, timestamp)

        query = """
            INSERT INTO agent_action_logs (tool_name, parameters, caller, result, success, timestamp, checksum)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        tx_run(active_conn, query, (tool_name, parameters_str, role, str(result), success_int, timestamp, checksum), fetch=False)

    if conn is not None:
        _write(conn)
        return
    with transaction() as owned_conn:
        _write(owned_conn)


def create_pending_approval(
    tool_name: str,
    args: dict,
    role: str,
    *,
    requester_username: str | None = None,
    operation_id: str | None = None,
    resource_version: str = "unspecified",
    policy_version: str = "po-approval-v2",
) -> str:
    """
    建立待審批項目至 pending_approvals 表中，含雜湊鏈 checksum，回傳 approval_id。
    讀 prev_hash → 算 row_hash → 寫入 在同一 transaction 內原子完成。
    """
    timestamp_str = datetime.now().strftime("%Y%m%d%H%M%S%f")
    approval_id = f"PENDING-{timestamp_str}-{tool_name}"
    parameters_str = json.dumps(
        args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    payload_digest = None
    if operation_id is not None:
        operation_id = str(operation_id).strip()
        if not operation_id:
            raise ValueError("operation_id must not be blank")
        from backend.tool_gateway import canonical_payload_digest

        payload_digest = canonical_payload_digest(
            tool_name=tool_name,
            args=args or {},
            resource_version=resource_version,
            policy_version=policy_version,
            requester_username=requester_username,
        )

    with transaction(immediate=operation_id is not None) as conn:
        if operation_id is not None:
            existing = tx_run(
                conn,
                """
                SELECT approval_id, payload_digest
                FROM pending_approvals
                WHERE operation_id = ?
                """,
                (operation_id,),
            )
            if existing:
                existing_id, existing_digest = existing[0]
                if not existing_digest or not hmac.compare_digest(
                    existing_digest, payload_digest
                ):
                    raise ValueError(
                        "operation_id is already bound to a different approval payload"
                    )
                return existing_id

        prev_checksum = _get_prev_checksum("pending_approvals", id_col="approval_id", order="DESC", conn=conn)
        checksum = compute_checksum(prev_checksum, tool_name, parameters_str, role, "pending", None, created_at, created_at, None)

        query = """
            INSERT INTO pending_approvals (
                approval_id, tool_name, parameters, requester,
                requester_username, status, approver,
                created_at, updated_at, reason, checksum, operation_id,
                payload_digest, resource_version, policy_version, version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        tx_run(
            conn,
            query,
            (
                approval_id,
                tool_name,
                parameters_str,
                role,
                requester_username,
                "pending",
                None,
                created_at,
                created_at,
                None,
                checksum,
                operation_id,
                payload_digest,
                resource_version if operation_id is not None else None,
                policy_version if operation_id is not None else None,
                0,
            ),
            fetch=False,
        )
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
            SELECT approval_id, tool_name, parameters, requester, status, approver,
                   created_at, updated_at, reason, checksum, operation_id,
                   payload_digest, resource_version, policy_version, version,
                   requester_username
            FROM pending_approvals
            WHERE status = ?
            ORDER BY created_at DESC
        """
        rows = run_query(query, (status_filter,))
    else:
        query = """
            SELECT approval_id, tool_name, parameters, requester, status, approver,
                   created_at, updated_at, reason, checksum, operation_id,
                   payload_digest, resource_version, policy_version, version,
                   requester_username
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
            "operation_id": row[10],
            "payload_digest": row[11],
            "resource_version": row[12],
            "policy_version": row[13],
            "version": row[14],
            "requester_username": row[15],
        })
    return approvals


def transition_approval_status(
    approval_id: str,
    *,
    expected_status: str,
    expected_version: int,
    new_status: str,
    approver: str | None,
    reason: str | None = None,
    conn=None,
    approval_context=None,
) -> bool:
    """Atomically move one approval only when both state and version still match."""
    from backend.log_checksum import rebuild_chain_from

    def _transition(active_conn) -> bool:
        tool_row = active_conn.execute(
            "SELECT tool_name FROM pending_approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if tool_row is None:
            return False
        if (
            tool_row[0]
            in {"create_purchase_order", "sync_external_purchase_order"}
            and approval_context is not _PROTECTED_APPROVAL_CONTEXT
        ):
            return False

        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = active_conn.execute(
            """
            UPDATE pending_approvals
            SET status = ?, approver = ?, updated_at = ?, reason = ?,
                version = version + 1
            WHERE approval_id = ? AND status = ? AND version = ?
            """,
            (
                new_status,
                approver,
                updated_at,
                reason,
                approval_id,
                expected_status,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            return False

        rebuild_chain_from(
            "pending_approvals",
            id_col="approval_id",
            start_id=approval_id,
            columns=(
                "tool_name",
                "parameters",
                "requester",
                "status",
                "approver",
                "created_at",
                "updated_at",
                "reason",
            ),
            conn=active_conn,
        )
        return True

    if conn is not None:
        return _transition(conn)
    with transaction(immediate=True) as owned_conn:
        return _transition(owned_conn)


def update_approval_status(approval_id: str, approver: str, status: str, reason: str = None) -> bool:
    """
    更新待審批項目的狀態（例如核准/拒絕），並寫入核准者、更新時間與拒絕原因。
    更新後重新計算該筆及其之後所有記錄的 checksum 鏈。
    """
    with transaction() as conn:
        current = tx_run(
            conn,
            "SELECT status, version FROM pending_approvals WHERE approval_id = ?",
            (approval_id,),
        )
        if not current:
            return False
        return transition_approval_status(
            approval_id,
            expected_status=current[0][0],
            expected_version=current[0][1],
            new_status=status,
            approver=approver,
            reason=reason,
            conn=conn,
        )


def get_pending_approval_by_id(approval_id: str) -> dict | None:
    """
    根據審批單 ID 查詢特定審批項目。
    """
    query = """
        SELECT approval_id, tool_name, parameters, requester, status, approver,
               created_at, updated_at, reason, checksum, operation_id,
               payload_digest, resource_version, policy_version, version,
               requester_username
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
        "operation_id": row[10],
        "payload_digest": row[11],
        "resource_version": row[12],
        "policy_version": row[13],
        "version": row[14],
        "requester_username": row[15],
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
            "requester_username": r["requester_username"],
            "risk": risk_level,
            "operation_id": r["operation_id"],
        })
    return result


def approve_action(approval_id: str, approver: str) -> dict:
    """
    提供給 A (Gateway) 與 D (Dashboard) 的核准操作介面。
    核准特定審批項目，並真正執行該工具操作。
    """
    from backend.tool_gateway import gateway
    res = gateway.approve_action(approval_id, approver=approver)
    return res.to_dict()


def reject_action(approval_id: str, reason: str, approver: str) -> dict:
    """
    提供給 A (Gateway) 與 D (Dashboard) 的拒絕操作介面。
    拒絕特定審批項目，操作作廢並記錄原因。
    """
    from backend.tool_gateway import gateway
    res = gateway.reject_action(approval_id, reason, approver=approver)
    return res.to_dict()
