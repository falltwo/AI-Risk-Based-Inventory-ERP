"""
tests/test_password_hash.py
密碼雜湊（Argon2id）與舊版 SHA-256／明文的自我修復升級。
"""

import hashlib

from backend.passwords import (
    hash_password,
    is_hashed,
    needs_password_upgrade,
    verify_password,
)
from backend.database import run_query


def test_hash_roundtrip_and_uniqueness():
    h = hash_password("admin")
    assert h.startswith("$argon2id$")
    assert is_hashed(h)
    assert not needs_password_upgrade(h)
    assert verify_password("admin", h)
    assert not verify_password("wrong", h)
    assert hash_password("admin") != h  # salt 不同 → 同密碼不同雜湊


def test_legacy_plaintext_still_verifies():
    assert verify_password("admin", "admin")      # 遷移前的明文可登入
    assert not verify_password("admin", "other")
    assert not is_hashed("admin")
    assert needs_password_upgrade("admin")


def _legacy_sha256_hash(plain: str, salt: str = "legacy-salt") -> str:
    digest = hashlib.sha256((salt + plain).encode("utf-8")).hexdigest()
    return f"sha256${salt}${digest}"


def test_legacy_sha256_still_verifies_and_requires_upgrade():
    stored = _legacy_sha256_hash("oldpw")
    assert is_hashed(stored)
    assert verify_password("oldpw", stored)
    assert not verify_password("wrong", stored)
    assert needs_password_upgrade(stored)


def _ensure_users_table():
    run_query("""CREATE TABLE IF NOT EXISTS users
                 (username TEXT PRIMARY KEY, password TEXT, role TEXT, name TEXT)""",
              fetch=False)


def _auth_events(username: str) -> list[str]:
    return [
        row[0]
        for row in run_query(
            "SELECT event_type FROM auth_events WHERE username=? ORDER BY id", (username,)
        )
    ]


def test_check_login_upgrades_legacy_row():
    from backend.auth import check_login
    _ensure_users_table()
    run_query("INSERT OR REPLACE INTO users VALUES ('legacy_u', 'pw123', 'sales', '測試')",
              fetch=False)

    # 明文列可登入，且登入後被就地升級為 hash
    assert check_login("legacy_u", "pw123") == {"role": "sales", "name": "測試"}
    stored = run_query("SELECT password FROM users WHERE username='legacy_u'")[0][0]
    assert is_hashed(stored)

    # 升級後仍可用原密碼登入、錯誤密碼被拒
    assert check_login("legacy_u", "pw123") is not None
    assert check_login("legacy_u", "wrong") is None
    assert check_login("no_such_user", "x") is None


def test_check_login_upgrades_legacy_sha256_row():
    from backend.auth import check_login

    _ensure_users_table()
    old_hash = _legacy_sha256_hash("pw123")
    run_query(
        "INSERT OR REPLACE INTO users VALUES ('sha_u', ?, 'sales', '測試')",
        (old_hash,),
        fetch=False,
    )

    assert check_login("sha_u", "pw123") == {"role": "sales", "name": "測試"}
    stored = run_query("SELECT password FROM users WHERE username='sha_u'")[0][0]
    assert stored.startswith("$argon2id$")
    assert verify_password("pw123", stored)


def test_five_failed_logins_temporarily_lock_an_account():
    from backend.auth import check_login

    _ensure_users_table()
    run_query(
        "INSERT OR REPLACE INTO users VALUES ('locked_u', ?, 'sales', '測試')",
        (hash_password("correct"),),
        fetch=False,
    )

    for _ in range(5):
        assert check_login("locked_u", "wrong") is None

    attempt = run_query(
        "SELECT failed_attempts, locked_until FROM login_attempts WHERE username='locked_u'"
    )[0]
    assert attempt[0] == 5
    assert attempt[1] is not None
    assert check_login("locked_u", "correct") is None
    assert _auth_events("locked_u") == [
        "login_failed",
        "login_failed",
        "login_failed",
        "login_failed",
        "login_locked",
        "login_blocked_locked",
    ]


def test_successful_login_clears_failed_attempts_and_is_audited():
    from backend.auth import check_login

    _ensure_users_table()
    run_query(
        "INSERT OR REPLACE INTO users VALUES ('clear_u', ?, 'sales', '測試')",
        (hash_password("correct"),),
        fetch=False,
    )
    assert check_login("clear_u", "wrong") is None
    assert check_login("clear_u", "correct") is not None
    assert not run_query("SELECT * FROM login_attempts WHERE username='clear_u'")
    assert _auth_events("clear_u")[-2:] == ["login_failed", "login_succeeded"]


def test_init_db_migrates_legacy_rows():
    """init_db 的一次性遷移會把既有明文列升級（種子雜湊與此走同一 helper）。"""
    from backend.database import init_db
    _ensure_users_table()
    run_query("INSERT OR REPLACE INTO users VALUES ('old_u', 'oldpw', 'hr', '舊帳號')",
              fetch=False)

    init_db()

    stored = run_query("SELECT password FROM users WHERE username='old_u'")[0][0]
    assert is_hashed(stored)
    from backend.auth import check_login
    assert check_login("old_u", "oldpw") is not None  # 遷移後原密碼仍可登入
    assert check_login("old_u", "wrong") is None
