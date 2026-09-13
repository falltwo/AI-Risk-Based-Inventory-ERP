"""查詢登入安全稽核事件；只允許具備 security.audit.read 的管理者。"""

from __future__ import annotations

from datetime import date, timedelta

from backend.access_control import SECURITY_AUDIT_READ, require_capability
from backend.database import run_query


EVENT_LABELS = {
    "login_failed": "登入失敗",
    "login_locked": "帳號已鎖定",
    "login_blocked_locked": "鎖定期間登入遭拒",
    "login_succeeded": "登入成功",
    "password_upgraded": "密碼已升級為 Argon2id",
}


def list_auth_events(
    requester_username: str,
    *,
    target_username: str = "",
    event_type: str = "",
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 500,
) -> list[dict[str, str]]:
    """回傳經過授權的登入稽核紀錄，絕不包含密碼或雜湊值。"""
    require_capability(requester_username, SECURITY_AUDIT_READ)

    clauses: list[str] = []
    params: list[str] = []
    target_username = str(target_username or "").strip()
    event_type = str(event_type or "").strip()
    if target_username:
        clauses.append("username = ?")
        params.append(target_username)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if start_date:
        clauses.append("occurred_at >= ?")
        params.append(f"{start_date.isoformat()} 00:00:00")
    if end_date:
        clauses.append("occurred_at < ?")
        params.append(f"{(end_date + timedelta(days=1)).isoformat()} 00:00:00")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    bounded_limit = max(1, min(int(limit), 500))
    rows = run_query(
        "SELECT username, event_type, occurred_at FROM auth_events"
        f"{where} ORDER BY occurred_at DESC, id DESC LIMIT ?",
        tuple(params + [bounded_limit]),
    )
    return [
        {
            "帳號": row[0],
            "事件": EVENT_LABELS.get(row[1], row[1]),
            "時間": row[2],
        }
        for row in rows
    ]
