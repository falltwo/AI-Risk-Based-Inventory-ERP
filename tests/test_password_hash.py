"""
tests/test_password_hash.py
N3：密碼雜湊（salted SHA-256）+ legacy 明文自我修復升級。
"""

from backend.passwords import hash_password, verify_password, is_hashed
from backend.database import run_query


def test_hash_roundtrip_and_uniqueness():
    h = hash_password("admin")
    assert is_hashed(h)
    assert verify_password("admin", h)
    assert not verify_password("wrong", h)
    assert hash_password("admin") != h  # salt 不同 → 同密碼不同雜湊


def test_legacy_plaintext_still_verifies():
    assert verify_password("admin", "admin")      # 遷移前的明文可登入
    assert not verify_password("admin", "other")
    assert not is_hashed("admin")


def _ensure_users_table():
    run_query("""CREATE TABLE IF NOT EXISTS users
                 (username TEXT PRIMARY KEY, password TEXT, role TEXT, name TEXT)""",
              fetch=False)


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
