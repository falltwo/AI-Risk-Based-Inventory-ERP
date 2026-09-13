"""
backend/auth.py
使用者驗證與角色型存取控制 (RBAC)
"""

from datetime import datetime, timedelta, timezone

import streamlit as st
from .database import run_query


MAX_FAILED_LOGIN_ATTEMPTS = 5
LOGIN_ATTEMPT_WINDOW = timedelta(minutes=15)
LOGIN_LOCK_DURATION = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ensure_auth_tables() -> None:
    """支援尚未跑過 init_db 的既有部署與獨立驗證測試。"""
    run_query(
        """CREATE TABLE IF NOT EXISTS login_attempts (
            username TEXT PRIMARY KEY,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            window_started_at TEXT,
            locked_until TEXT
        )""",
        fetch=False,
    )
    run_query(
        """CREATE TABLE IF NOT EXISTS auth_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL
        )""",
        fetch=False,
    )


def _record_auth_event(username: str, event_type: str) -> None:
    run_query(
        "INSERT INTO auth_events (username, event_type, occurred_at) VALUES (?, ?, ?)",
        (username, event_type, _timestamp()),
        fetch=False,
    )


def _is_login_locked(username: str) -> bool:
    rows = run_query(
        "SELECT locked_until FROM login_attempts WHERE username=?", (username,)
    )
    if not rows:
        return False
    locked_until = _parse_timestamp(rows[0][0])
    if locked_until and locked_until > _now():
        return True
    if locked_until:
        # 鎖定期結束後，重新開始計算新的失敗嘗試視窗。
        run_query(
            "UPDATE login_attempts SET failed_attempts=0, window_started_at=NULL, "
            "locked_until=NULL WHERE username=?",
            (username,),
            fetch=False,
        )
    return False


def _record_failed_login(username: str) -> None:
    rows = run_query(
        "SELECT failed_attempts, window_started_at FROM login_attempts WHERE username=?",
        (username,),
    )
    now = _now()
    attempts = 1
    window_started_at = now
    if rows:
        previous_attempts, previous_window = rows[0]
        parsed_window = _parse_timestamp(previous_window)
        if parsed_window and now - parsed_window < LOGIN_ATTEMPT_WINDOW:
            attempts = int(previous_attempts) + 1
            window_started_at = parsed_window

    if attempts >= MAX_FAILED_LOGIN_ATTEMPTS:
        run_query(
            """INSERT INTO login_attempts
               (username, failed_attempts, window_started_at, locked_until)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(username) DO UPDATE SET
                   failed_attempts=excluded.failed_attempts,
                   window_started_at=excluded.window_started_at,
                   locked_until=excluded.locked_until""",
            (username, attempts, _timestamp(window_started_at), _timestamp(now + LOGIN_LOCK_DURATION)),
            fetch=False,
        )
        _record_auth_event(username, "login_locked")
        return

    run_query(
        """INSERT INTO login_attempts
           (username, failed_attempts, window_started_at, locked_until)
           VALUES (?, ?, ?, NULL)
           ON CONFLICT(username) DO UPDATE SET
               failed_attempts=excluded.failed_attempts,
               window_started_at=excluded.window_started_at,
               locked_until=NULL""",
        (username, attempts, _timestamp(window_started_at)),
        fetch=False,
    )
    _record_auth_event(username, "login_failed")


def _clear_failed_logins(username: str) -> None:
    run_query("DELETE FROM login_attempts WHERE username=?", (username,), fetch=False)


def check_login(username: str, password: str) -> dict | None:
    """驗證帳號密碼，成功回傳 {role, name}，失敗回傳 None。
    密碼以 Argon2id 比對；舊 SHA-256 與明文格式會在成功登入時就地升級。"""
    from backend.passwords import hash_password, needs_password_upgrade, verify_password

    _ensure_auth_tables()
    rows = run_query(
        "SELECT password, role, name FROM users WHERE username=?",
        (username,),
    )
    if not rows:
        return None
    if _is_login_locked(username):
        _record_auth_event(username, "login_blocked_locked")
        return None
    stored, role, name = rows[0]
    if not verify_password(password, stored or ""):
        _record_failed_login(username)
        return None
    if needs_password_upgrade(stored or ""):
        run_query("UPDATE users SET password=? WHERE username=?",
                  (hash_password(password), username), fetch=False)
        _record_auth_event(username, "password_upgraded")
    _clear_failed_logins(username)
    _record_auth_event(username, "login_succeeded")
    return {"role": role, "name": name}


def check_permission(allowed_roles: list) -> bool:
    """依目前 session 角色判斷是否有權限；admin 永遠通過"""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        if not get_script_run_ctx():
            return True
    except Exception:
        pass
        
    try:
        current_role = st.session_state.get("role", "")
    except Exception:
        return True

    if current_role == "admin":
        return True
    return current_role in allowed_roles
