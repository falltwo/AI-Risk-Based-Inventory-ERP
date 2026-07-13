"""
backend/auth.py
使用者驗證與角色型存取控制 (RBAC)
"""

import streamlit as st
from .database import run_query


def check_login(username: str, password: str) -> dict | None:
    """驗證帳號密碼，成功回傳 {role, name}，失敗回傳 None。
    （N3）密碼以 salted hash 比對；遇到 legacy 明文則於登入成功時就地升級。"""
    from backend.passwords import verify_password, is_hashed, hash_password

    rows = run_query(
        "SELECT password, role, name FROM users WHERE username=?",
        (username,),
    )
    if not rows:
        return None
    stored, role, name = rows[0]
    if not verify_password(password, stored or ""):
        return None
    if not is_hashed(stored or ""):  # legacy 明文 → 自我修復式升級
        run_query("UPDATE users SET password=? WHERE username=?",
                  (hash_password(password), username), fetch=False)
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
