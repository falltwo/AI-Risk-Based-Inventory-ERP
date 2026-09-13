"""
backend/passwords.py
密碼雜湊：Argon2id；舊版 salted SHA-256 與明文帳號可在登入時遷移。

新儲存格式為 argon2-cffi 的標準編碼（例如
"$argon2id$v=19$m=65536,t=3,p=4$..."）。它含有演算法版本、成本參數與 salt，
因此不需另行維護格式版本欄位。
"""

import hashlib
import hmac

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type


# OWASP 的一般用途密碼雜湊建議：64 MiB 記憶體、3 次迭代、4 個平行度。
_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def hash_password(plain: str) -> str:
    """以目前的 Argon2id 參數雜湊密碼。"""
    return _PASSWORD_HASHER.hash(plain)


def verify_password(plain: str, stored: str) -> bool:
    """驗證目前與舊版格式；呼叫端須在成功後檢查是否需要升級。"""
    if not stored:
        return False
    if stored.startswith("$argon2id$"):
        try:
            return _PASSWORD_HASHER.verify(stored, plain)
        except (InvalidHashError, VerificationError):
            return False

    if "$" not in stored:  # legacy 明文（遷移前）
        return hmac.compare_digest(plain, stored)
    try:
        algo, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "sha256":
        return False
    candidate = hashlib.sha256((salt + plain).encode("utf-8")).hexdigest()
    return hmac.compare_digest(candidate, digest)


def is_hashed(stored: str) -> bool:
    """回傳值是否已是任一已知雜湊格式（供資料庫初始化保護舊資料）。"""
    return bool(stored) and (
        stored.startswith("$argon2id$")
        or (stored.startswith("sha256$") and stored.count("$") == 2)
    )


def needs_password_upgrade(stored: str) -> bool:
    """成功驗證後，這筆密碼是否應以目前 Argon2id 參數重寫。"""
    if not stored.startswith("$argon2id$"):
        return True
    try:
        return _PASSWORD_HASHER.check_needs_rehash(stored)
    except InvalidHashError:
        # 正常流程中此情形不會通過 verify；保守地要求升級。
        return True
