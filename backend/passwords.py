"""
backend/passwords.py
密碼雜湊（N3）：salted SHA-256，標準庫實作、零外部依賴。

儲存格式："sha256$<hex_salt>$<hex_digest>"
不含 '$' 的舊值視為 legacy 明文 —— verify 時直接比對，
並由 auth.check_login / database.init_db 在適當時機就地升級。
"""

import hashlib
import os


def hash_password(plain: str) -> str:
    salt = os.urandom(16).hex()
    digest = hashlib.sha256((salt + plain).encode("utf-8")).hexdigest()
    return f"sha256${salt}${digest}"


def verify_password(plain: str, stored: str) -> bool:
    if not stored:
        return False
    if "$" not in stored:  # legacy 明文（遷移前）
        return plain == stored
    try:
        algo, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "sha256":
        return False
    return hashlib.sha256((salt + plain).encode("utf-8")).hexdigest() == digest


def is_hashed(stored: str) -> bool:
    return bool(stored) and stored.startswith("sha256$") and stored.count("$") == 2
