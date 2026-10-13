"""Authentication and the legacy role-based authorization boundary.

Backend tools must not infer authorization from a Streamlit session.  The
Gateway validates a tool/role pair first, then binds that role only for the
duration of the tool call.  Direct calls therefore fail closed.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

from .database import run_query


_AUTHORIZED_ROLE: ContextVar[str | None] = ContextVar(
    "erp_authorized_role", default=None
)


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


@contextmanager
def authorized_role(role: str) -> Iterator[None]:
    """Bind a role that has already been authorized by the Tool Gateway.

    This helper is an internal execution mechanism, not an authorization
    decision.  Callers must validate the tool/role pair before entering it.
    ContextVar keeps concurrent requests and async tasks isolated.
    """
    normalized_role = str(role or "").strip()
    if not normalized_role:
        raise PermissionError("A verified role is required")
    token = _AUTHORIZED_ROLE.set(normalized_role)
    try:
        yield
    finally:
        _AUTHORIZED_ROLE.reset(token)


def check_permission(allowed_roles: list[str] | tuple[str, ...] | set[str]) -> bool:
    """Check the Gateway-bound role; missing identity always denies access."""
    current_role = _AUTHORIZED_ROLE.get()
    if not current_role:
        return False
    if current_role == "admin":
        return True
    return current_role in allowed_roles
