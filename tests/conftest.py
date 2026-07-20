"""
tests/conftest.py
測試環境隔離：
  1. 把 repo root 加進 sys.path，讓 `import backend` 在任何執行方式下都成立。
  2. 在 import backend 之前把 ERP_DB_PATH 指到暫存目錄 ——
     測試永遠不碰開發用的 data/erp.db（backend/database.py 於 import 時讀此環境變數）。
"""

import os
import sys
import tempfile

# 1) repo root 進 sys.path（conftest 位於 tests/，上一層即 root）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 2) 測試專用 DB 路徑（模組載入期設定，早於任何 backend import）
_TMP_DIR = tempfile.mkdtemp(prefix="erp_test_")
os.environ["ERP_DB_PATH"] = os.path.join(_TMP_DIR, "test_erp.db")
# 測試套件明確啟用合成資料；正式執行的安全預設維持關閉。
os.environ["ERP_DEMO_MODE"] = "1"
