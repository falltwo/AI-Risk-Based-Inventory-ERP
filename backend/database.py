"""
backend/database.py
資料庫連線、路徑設定、初始化、通用查詢函式
"""

import sqlite3
import os
from datetime import datetime, timedelta
import tomllib


# ==========================================
# 資料庫路徑設定（與程式碼分離，企業可自訂或匯入）
# ==========================================
# 優先順序：環境變數 ERP_DB_PATH > .streamlit/secrets.toml [database] path > 預設 data/erp.db
def _get_db_path():
    path = os.environ.get("ERP_DB_PATH", "").strip()
    if path:
        return path
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    secrets_path = os.path.join(base_dir, ".streamlit", "secrets.toml")
    if os.path.isfile(secrets_path):
        try:
            # 只讀取「目前專案」的 secrets，避免誤抓全域/其他專案設定。
            with open(secrets_path, "rb") as f:
                secrets = tomllib.load(f)
            secret_db_path = str(secrets.get("database", {}).get("path", "")).strip()
            if secret_db_path:
                return secret_db_path
        except Exception:
            pass
    return os.path.join(base_dir, "data", "erp.db")


DB_FILE = _get_db_path()


def _ensure_db_dir():
    """確保資料庫所在目錄存在，方便企業指定任意路徑或匯入既有 .db"""
    d = os.path.dirname(DB_FILE)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


def init_db():
    _ensure_db_dir()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # 使用者與權限
    c.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT, role TEXT, name TEXT)''')
    # 進銷存：商品、倉庫
    c.execute('''CREATE TABLE IF NOT EXISTS warehouses (warehouse_id TEXT PRIMARY KEY, name TEXT, address TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS inventory (product_id TEXT PRIMARY KEY, name TEXT, stock INTEGER, price INTEGER, cost REAL, reorder_point INTEGER, baseline_reorder_point INTEGER, daily_sales INTEGER, barcode TEXT, warehouse_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS stock_moves (move_id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT, warehouse_id TEXT, qty INTEGER, move_type TEXT, ref_no TEXT, move_date TEXT, note TEXT)''')
    # 採購：供應商、採購單
    c.execute('''CREATE TABLE IF NOT EXISTS suppliers (supplier_id TEXT PRIMARY KEY, name TEXT, contact TEXT, phone TEXT, email TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS purchase_orders (
        po_id TEXT PRIMARY KEY,
        supplier_id TEXT,
        order_date TEXT,
        status TEXT,
        total_amount REAL,
        note TEXT,
        operation_id TEXT,
        last_sync_operation_id TEXT,
        external_source_system TEXT,
        external_id TEXT,
        external_version INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS purchase_order_items (id INTEGER PRIMARY KEY AUTOINCREMENT, po_id TEXT, product_id TEXT, qty INTEGER, unit_price REAL)''')
    # 銷售：客戶、報價單、銷售單、收款
    c.execute('''CREATE TABLE IF NOT EXISTS customers (customer_id TEXT PRIMARY KEY, name TEXT, contact TEXT, phone TEXT, email TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS quotations (quote_id TEXT PRIMARY KEY, customer_id TEXT, quote_date TEXT, status TEXT, total_amount REAL, valid_until TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS quotation_items (id INTEGER PRIMARY KEY AUTOINCREMENT, quote_id TEXT, product_id TEXT, qty INTEGER, unit_price REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, customer_id TEXT, product_id TEXT, quantity INTEGER, status TEXT, order_date TEXT, total_amount REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS payments (payment_id INTEGER PRIMARY KEY AUTOINCREMENT, ref_type TEXT, ref_id TEXT, amount REAL, payment_date TEXT, note TEXT)''')
    # 財務：應收應付、總帳
    c.execute('''CREATE TABLE IF NOT EXISTS receivables (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT, ref_id TEXT, amount REAL, paid REAL, due_date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS payables (id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_id TEXT, ref_id TEXT, amount REAL, paid REAL, due_date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS general_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, ledger_date TEXT, account TEXT, debit REAL, credit REAL, description TEXT)''')
    # 生產：BOM、製造工單
    c.execute('''CREATE TABLE IF NOT EXISTS bom (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT, component_id TEXT, qty_per REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS work_orders (wo_id TEXT PRIMARY KEY, product_id TEXT, qty_plan INTEGER, qty_done INTEGER DEFAULT 0, status TEXT, start_date TEXT, end_date TEXT)''')
    # 人資
    c.execute('''CREATE TABLE IF NOT EXISTS hr (employee_id TEXT PRIMARY KEY, name TEXT, department TEXT, role TEXT, salary REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS payroll (id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id TEXT, period TEXT, base_salary REAL, bonus REAL, deduction REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS attendance (id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id TEXT, work_date TEXT, check_in TEXT, check_out TEXT, status TEXT)''')
    # 永續 ESG：碳係數、供應鏈事件
    c.execute('''CREATE TABLE IF NOT EXISTS carbon_factors (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT, scope INTEGER, kg_co2_per_unit REAL, note TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS supply_chain_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, region TEXT, country TEXT, impact_days INTEGER, description TEXT, created_at TEXT, news_id INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS supply_chain_news (id INTEGER PRIMARY KEY AUTOINCREMENT, country TEXT, region TEXT, title TEXT, summary TEXT, url TEXT, source TEXT, published_at TEXT, relevance_tag TEXT, fetched_at TEXT, category TEXT, is_relevant INTEGER DEFAULT 1, estimated_delay INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS esg_targets (id INTEGER PRIMARY KEY AUTOINCREMENT, target_year INTEGER, scope INTEGER, baseline_kg_co2 REAL, target_kg_co2 REAL, note TEXT)''')
    # 永續 ESG：風險管理係數（地區/事件類型/供應商類別 → 風險分數 0–100、權重）
    c.execute('''CREATE TABLE IF NOT EXISTS esg_risk_factors (id INTEGER PRIMARY KEY AUTOINCREMENT, risk_type TEXT, risk_key TEXT, risk_score REAL, weight REAL, note TEXT, updated_at TEXT, UNIQUE(risk_type, risk_key))''')
    # 客戶關係管理 (CRM)：通訊紀錄
    c.execute('''CREATE TABLE IF NOT EXISTS crm_communications (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT, comm_type TEXT, subject TEXT, content TEXT, comm_date TEXT, created_by TEXT, created_at TEXT)''')
    
    # 供應商與商品配對、替換紀錄
    c.execute('''CREATE TABLE IF NOT EXISTS supplier_products (id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_id TEXT, product_id TEXT, price REAL, carbon_factor REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS supplier_replacement_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, original_supplier_id TEXT, new_supplier_id TEXT, replaced_at TEXT, reason TEXT, executed_by TEXT)''')

    # LINE Bot 對話紀錄
    c.execute('''CREATE TABLE IF NOT EXISTS line_bot_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, user_name TEXT, user_msg TEXT, ai_reply TEXT, created_at TEXT)''')

    # Agent 動作日誌與審批表
    c.execute('''CREATE TABLE IF NOT EXISTS agent_action_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tool_name TEXT,
        parameters TEXT,
        caller TEXT,
        result TEXT,
        success INTEGER,
        timestamp TEXT,
        checksum TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS pending_approvals (
        approval_id TEXT PRIMARY KEY,
        tool_name TEXT,
        parameters TEXT,
        requester TEXT,
        status TEXT DEFAULT 'pending',
        approver TEXT,
        created_at TEXT,
        updated_at TEXT,
        reason TEXT,
        checksum TEXT,
        operation_id TEXT,
        payload_digest TEXT,
        resource_version TEXT,
        policy_version TEXT,
        version INTEGER NOT NULL DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS effect_receipts (
        receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE,
        approval_id TEXT NOT NULL UNIQUE,
        payload_digest TEXT NOT NULL,
        result TEXT NOT NULL,
        created_at TEXT NOT NULL
    )''')

    # L3 ERP CSV exchange: validated staging is kept separate from live PO data.
    # A stable (source_system, external_id) identifies one external record while
    # version/content_digest identify the exact revision submitted for approval.
    c.execute('''CREATE TABLE IF NOT EXISTS erp_exchange_records (
        source_system TEXT NOT NULL,
        external_id TEXT NOT NULL,
        po_id TEXT NOT NULL,
        supplier_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        qty INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        order_date TEXT NOT NULL,
        status TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        content_digest TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        last_synced_version INTEGER,
        last_synced_operation_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source_system, external_id),
        UNIQUE (source_system, po_id)
    )''')

    # External acknowledgements are distinct from effect_receipts. The latter
    # proves the local SQLite effect; this table proves a returned ERP receipt
    # matched the exact approved operation and payload.
    c.execute('''CREATE TABLE IF NOT EXISTS erp_exchange_receipts (
        operation_id TEXT PRIMARY KEY,
        source_system TEXT NOT NULL,
        external_id TEXT NOT NULL,
        approval_id TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        receipt_attempt_id TEXT,
        receipt_status TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        key_id TEXT,
        signature TEXT,
        received_by TEXT NOT NULL,
        received_at TEXT NOT NULL
    )''')

    for column_name, column_type in (
        ("receipt_attempt_id", "TEXT"),
        ("key_id", "TEXT"),
        ("signature", "TEXT"),
        ("received_by", "TEXT"),
    ):
        try:
            c.execute(
                f"ALTER TABLE erp_exchange_receipts ADD COLUMN {column_name} {column_type}"
            )
        except sqlite3.OperationalError:
            pass

    c.execute('''CREATE TABLE IF NOT EXISTS erp_exchange_receipt_events (
        receipt_attempt_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        source_system TEXT NOT NULL,
        external_id TEXT NOT NULL,
        approval_id TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        receipt_status TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        key_id TEXT NOT NULL,
        signature TEXT NOT NULL,
        received_by TEXT NOT NULL,
        received_at TEXT NOT NULL
    )''')
    c.execute('''CREATE INDEX IF NOT EXISTS ix_erp_exchange_receipt_events_operation
        ON erp_exchange_receipt_events(operation_id, received_at)''')

    # 確保待審批表包含 reason 欄位
    try:
        c.execute("ALTER TABLE pending_approvals ADD COLUMN reason TEXT")
    except sqlite3.OperationalError:
        pass

    # 確保舊版資料表包含 checksum 欄位（向後相容遷移）
    try:
        c.execute("ALTER TABLE agent_action_logs ADD COLUMN checksum TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE pending_approvals ADD COLUMN checksum TEXT")
    except sqlite3.OperationalError:
        pass

    # A′：舊資料庫的審批列保留不動；新增欄位允許 NULL，只有新版操作受唯一鍵保護。
    pending_approval_migrations = (
        ("operation_id", "TEXT"),
        ("payload_digest", "TEXT"),
        ("resource_version", "TEXT"),
        ("policy_version", "TEXT"),
        ("version", "INTEGER NOT NULL DEFAULT 0"),
    )
    for column_name, column_type in pending_approval_migrations:
        try:
            c.execute(
                f"ALTER TABLE pending_approvals ADD COLUMN {column_name} {column_type}"
            )
        except sqlite3.OperationalError:
            pass

    c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_approvals_operation_id
        ON pending_approvals(operation_id)
        WHERE operation_id IS NOT NULL''')

    # A′：新建的採購單與核准 operation 建立一對一連結。
    # 舊資料保留 NULL，因此可以無損升級且不會被誤綁定。
    try:
        c.execute("ALTER TABLE purchase_orders ADD COLUMN operation_id TEXT")
    except sqlite3.OperationalError:
        pass
    c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS uq_purchase_orders_operation_id
        ON purchase_orders(operation_id)
        WHERE operation_id IS NOT NULL''')

    for column_name, column_type in (
        ("last_sync_operation_id", "TEXT"),
        ("external_source_system", "TEXT"),
        ("external_id", "TEXT"),
        ("external_version", "INTEGER"),
    ):
        try:
            c.execute(
                f"ALTER TABLE purchase_orders ADD COLUMN {column_name} {column_type}"
            )
        except sqlite3.OperationalError:
            pass
    c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS uq_purchase_orders_external_record
        ON purchase_orders(external_source_system, external_id)
        WHERE external_source_system IS NOT NULL AND external_id IS NOT NULL''')

    # F7：確保派工紀錄表存在（agent_dispatch_logs 原由 dispatch_logger 動態建，
    # 這裡預建讓 init_db 後 4 大治理效益 view 立即可用）
    c.execute('''CREATE TABLE IF NOT EXISTS agent_dispatch_logs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        task           TEXT,
        task_type      TEXT,
        primary_agent  TEXT,
        agent_chain    TEXT,
        routed_by      TEXT,
        needs_approval INTEGER,
        reason         TEXT,
        caller         TEXT,
        timestamp      TEXT
    )''')

    # F7 建立 4 大治理效益視圖
    c.execute("DROP VIEW IF EXISTS view_decision_time")
    c.execute('''CREATE VIEW view_decision_time AS
    SELECT
      AVG(strftime('%s', a.timestamp) - strftime('%s', d.timestamp)) AS avg_decision_time
    FROM agent_dispatch_logs d
    JOIN agent_action_logs a ON a.caller = d.caller
      AND strftime('%s', a.timestamp) >= strftime('%s', d.timestamp)
      AND strftime('%s', a.timestamp) - strftime('%s', d.timestamp) <= 120''')

    c.execute("DROP VIEW IF EXISTS view_pending_intercept_ratio")
    c.execute('''CREATE VIEW view_pending_intercept_ratio AS
    SELECT
      CASE WHEN COUNT(*) = 0 THEN NULL
           ELSE CAST(SUM(CASE WHEN needs_approval = 1 THEN 1 ELSE 0 END) AS REAL) / COUNT(*)
      END AS intercept_ratio
    FROM agent_dispatch_logs''')

    c.execute("DROP VIEW IF EXISTS view_traceability_rate")
    c.execute('''CREATE VIEW view_traceability_rate AS
    SELECT
      CASE WHEN COUNT(*) = 0 THEN NULL
           ELSE CAST(COUNT(d.id) AS REAL) / COUNT(*)
      END AS traceability_rate
    FROM agent_action_logs a
    LEFT JOIN agent_dispatch_logs d ON a.caller = d.caller
      AND strftime('%s', a.timestamp) >= strftime('%s', d.timestamp)
      AND strftime('%s', a.timestamp) - strftime('%s', d.timestamp) <= 120''')

    c.execute("DROP VIEW IF EXISTS view_avg_tools_per_turn")
    c.execute('''CREATE VIEW view_avg_tools_per_turn AS
    SELECT
      CASE WHEN COUNT(DISTINCT d.id) = 0 THEN NULL
           ELSE CAST(COUNT(a.id) AS REAL) / COUNT(DISTINCT d.id)
      END AS avg_tools_per_turn
    FROM agent_dispatch_logs d
    LEFT JOIN agent_action_logs a ON a.caller = d.caller
      AND strftime('%s', a.timestamp) >= strftime('%s', d.timestamp)
      AND strftime('%s', a.timestamp) - strftime('%s', d.timestamp) <= 120''')

    # 所有入口最終都會寫入審批帳本；不以 Agent 派工紀錄代替審批事實。
    c.execute("DROP VIEW IF EXISTS view_approval_summary")
    c.execute('''CREATE VIEW view_approval_summary AS
    SELECT
      COUNT(*) AS total_requests,
      SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_requests,
      SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved_requests,
      SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected_requests
    FROM pending_approvals''')

    c.execute("DROP VIEW IF EXISTS view_governance_daily_trend")
    c.execute('''CREATE VIEW view_governance_daily_trend AS
    SELECT
      activity_date AS date,
      SUM(total_dispatches) AS total_dispatches,
      SUM(approval_requests) AS approval_requests
    FROM (
      SELECT
        substr(timestamp, 1, 10) AS activity_date,
        COUNT(*) AS total_dispatches,
        0 AS approval_requests
      FROM agent_dispatch_logs
      WHERE timestamp IS NOT NULL AND length(timestamp) >= 10
      GROUP BY activity_date
      UNION ALL
      SELECT
        substr(created_at, 1, 10) AS activity_date,
        0 AS total_dispatches,
        COUNT(*) AS approval_requests
      FROM pending_approvals
      WHERE created_at IS NOT NULL AND length(created_at) >= 10
      GROUP BY activity_date
    )
    GROUP BY activity_date''')

    # 供應商擴充：地理位置與風險（供應鏈地圖、ESG）
    try:
        c.execute("SELECT country FROM suppliers LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE suppliers ADD COLUMN country TEXT")
        c.execute("ALTER TABLE suppliers ADD COLUMN region TEXT")
        c.execute("ALTER TABLE suppliers ADD COLUMN latitude REAL")
        c.execute("ALTER TABLE suppliers ADD COLUMN longitude REAL")
        c.execute("ALTER TABLE suppliers ADD COLUMN risk_level TEXT")

    try:
        c.execute("SELECT is_official FROM suppliers LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE suppliers ADD COLUMN is_official INTEGER DEFAULT 0")

    try:
        c.execute("SELECT customer_id FROM orders LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE orders ADD COLUMN customer_id TEXT")
        c.execute("ALTER TABLE orders ADD COLUMN total_amount REAL")
    try:
        c.execute("SELECT cost, barcode, warehouse_id, baseline_reorder_point FROM inventory LIMIT 1")
    except sqlite3.OperationalError:
        try: c.execute("ALTER TABLE inventory ADD COLUMN cost REAL")
        except: pass
        try: c.execute("ALTER TABLE inventory ADD COLUMN barcode TEXT")
        except: pass
        try: c.execute("ALTER TABLE inventory ADD COLUMN warehouse_id TEXT")
        except: pass
        try: c.execute("ALTER TABLE inventory ADD COLUMN baseline_reorder_point INTEGER")
        except: pass
    # 若已新增欄位，回填既有資料的空值（避免商品管理出現空白欄位）
    try:
        c.execute("UPDATE inventory SET barcode = product_id WHERE barcode IS NULL OR TRIM(barcode) = ''")
        c.execute("UPDATE inventory SET warehouse_id = 'WH01' WHERE warehouse_id IS NULL OR TRIM(warehouse_id) = ''")
    except Exception:
        pass
    try:
        c.execute("SELECT salary FROM hr LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE hr ADD COLUMN salary REAL")

    # 永續 ESG：若有新欄位則為既有供應商寫入示範經緯度（台灣）
    try:
        c.execute("SELECT latitude FROM suppliers LIMIT 1")
        c.execute("UPDATE suppliers SET country='台灣', region='北區', latitude=25.0330, longitude=121.5654, risk_level='低' WHERE supplier_id='SUP01'")
        c.execute("UPDATE suppliers SET country='台灣', region='北區', latitude=25.0479, longitude=121.5318, risk_level='低' WHERE supplier_id='SUP02'")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("PRAGMA table_info(customers)")
        columns = [col[1] for col in c.fetchall()]
        if 'contact' not in columns:
            c.execute("ALTER TABLE customers ADD COLUMN contact TEXT")
        if 'phone' not in columns:
            c.execute("ALTER TABLE customers ADD COLUMN phone TEXT")
        if 'email' not in columns:
            c.execute("ALTER TABLE customers ADD COLUMN email TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("SELECT news_id FROM supply_chain_events LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE supply_chain_events ADD COLUMN news_id INTEGER")
    except Exception:
        pass

    # Insert Mock Data if empty
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        # N3：種子帳密以 salted hash 儲存（帳密仍為 admin/admin 等，僅儲存形式加密）
        from backend.passwords import hash_password
        c.execute("INSERT INTO users VALUES ('admin', ?, 'admin', '系統管理員')", (hash_password('admin'),))
        c.execute("INSERT INTO users VALUES ('hr1', ?, 'hr', '人資主管')", (hash_password('hr1'),))
        c.execute("INSERT INTO users VALUES ('wh1', ?, 'warehouse', '倉管人員')", (hash_password('wh1'),))
        c.execute("INSERT INTO users VALUES ('sales1', ?, 'sales', '業務代表')", (hash_password('sales1'),))

        c.execute("INSERT INTO warehouses VALUES ('WH01', '主倉庫', '新北市板橋區')")
        c.execute("INSERT INTO warehouses VALUES ('WH02', '二倉', '桃園市')")

        inventory_data = [
            ("P001", "高階筆記型電腦", 150, 45000, 38000, 50, 5, "6901234567890", "WH01"),
            ("P002", "無線滑鼠", 500, 800, 450, 100, 20, "6901234567891", "WH01"),
            ("P003", "機械鍵盤", 120, 2500, 1800, 50, 5, "6901234567892", "WH01"),
            ("P004", "螢幕顯示器", 30, 6000, 4800, 50, 10, "6901234567893", "WH01"),
        ]
        for row in inventory_data:
            c.execute("INSERT OR IGNORE INTO inventory (product_id, name, stock, price, cost, reorder_point, daily_sales, barcode, warehouse_id) VALUES (?,?,?,?,?,?,?,?,?)",
                      (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]))

        c.execute("INSERT OR IGNORE INTO suppliers (supplier_id, name, contact, phone, email, is_official) VALUES ('SUP01', '鍵鼠供應商', '張先生', '02-12345678', 'sup@example.com', 1)")
        c.execute("INSERT OR IGNORE INTO suppliers (supplier_id, name, contact, phone, email, is_official) VALUES ('SUP02', '螢幕原廠', '李小姐', '03-87654321', 'lcd@example.com', 1)")
        c.execute("INSERT OR IGNORE INTO customers VALUES ('C001', '科技公司A', '王經理', '02-11112222', 'a@example.com')")
        c.execute("INSERT OR IGNORE INTO customers VALUES ('C002', '零售通路B', '陳主任', '02-33334444', 'b@example.com')")

        hr_data = [
            ("E001", "王小明", "業務部", "資深業務", 45000),
            ("E002", "李美鳳", "人資部", "HR 經理", 55000),
        ]
        for row in hr_data:
            c.execute("INSERT OR IGNORE INTO hr (employee_id, name, department, role, salary) VALUES (?,?,?,?,?)", row)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        old_str = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT OR IGNORE INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", ("ORD-20231026-001", "C001", "P001", 3, "處理中", old_str, 135000))
        c.execute("INSERT OR IGNORE INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", ("ORD-20231025-002", "C002", "P002", 15, "已出貨", now_str, 12000))

        # === 以下為 ESG 與碳排所需之預設資料 ===
        c.execute("SELECT COUNT(*) FROM carbon_factors")
        if c.fetchone()[0] == 0:
            c.execute("INSERT INTO carbon_factors (product_id, scope, kg_co2_per_unit, note) VALUES ('P001', 3, 45.5, '筆電製造碳排')")
            c.execute("INSERT INTO carbon_factors (product_id, scope, kg_co2_per_unit, note) VALUES ('P002', 3, 2.1, '滑鼠製造碳排')")
            c.execute("INSERT INTO carbon_factors (product_id, scope, kg_co2_per_unit, note) VALUES ('P003', 3, 5.0, '鍵盤製造碳排')")
            c.execute("INSERT INTO carbon_factors (product_id, scope, kg_co2_per_unit, note) VALUES ('P004', 3, 30.2, '螢幕製造碳排')")
            
            # 建立過去幾個月的歷史訂單，用來畫碳排趨勢圖
            for i in range(1, 6):
                past_date = (datetime.now() - timedelta(days=30*i)).strftime("%Y-%m-%d %H:%M:%S")
                c.execute("INSERT OR IGNORE INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", (f"ORD-HIST-{i}-01", "C001", "P001", 5 + i, "已出貨", past_date, 135000))
                c.execute("INSERT OR IGNORE INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", (f"ORD-HIST-{i}-02", "C002", "P002", 20 - i, "已出貨", past_date, 12000))

    c.execute("SELECT COUNT(*) FROM suppliers")
    if c.fetchone()[0] < 50:
        import random
        regions = [
            ("伊朗", "中東", 35.6892, 51.3890, ["Tehran Supply Co.", "Pars Logistics", "Persian Tech Components", "Iran Manufacturing"]),
            ("沙烏地阿拉伯", "中東", 24.7136, 46.6753, ["Saudi Gulf Trading", "Riyadh Metals", "ME Industrial Parts"]),
            ("阿拉伯聯合大公國", "中東", 25.2048, 55.2708, ["Dubai General Electronics", "Emirates Sourcing", "Gulf Hub Traders"]),
            ("美國", "北美洲", 37.7749, -122.4194, ["American Tech Parts", "US Global Supply", "Silicon Valley Components", "Liberty Systems"]),
            ("墨西哥", "北美洲", 19.4326, -99.1332, ["MexiTech Supply", "Sinaloa Parts", "Monterrey Distribution"]),
            ("德國", "歐洲", 52.5200, 13.4050, ["Euro Parts GmbH", "Berlin Tech Solutions", "Munich Industrial"]),
            ("德國", "歐洲", 48.1351, 11.5820, ["Bavaria Components", "EuroTech Manufacturers"]),
            ("日本", "亞洲", 35.6895, 139.6917, ["Tokyo Electronic", "Osaka Components", "Japan Precision Parts"]),
            ("台灣", "亞洲", 25.0329, 121.5654, ["Taiwan Semi Supply", "Taipei Circuits", "Formosa Technologies"]),
            ("越南", "亞洲", 21.0285, 105.8542, ["Hanoi Sourcing", "Viet Factory Partners"])
        ]
        # 建立 50 家供應商
        for i in range(1, 51):
            sid = f"SUP-{i:03d}"
            country, region, lat_base, lon_base, name_pool = random.choice(regions)
            name = f"{random.choice(name_pool)} {random.randint(100, 999)}"
            contact = random.choice(['陳經理', '林主任', '王小姐', '李先生', '張專員', '劉代表', '黃經辦', 'John Smith', 'Ali Reza', 'Maria Garcia'])
            phone = f'0{random.randint(2, 9)}-{random.randint(10000000, 99999999)}'
            email = f'contact_{sid.lower()}@example.com'
            risk_level = random.choice(["低", "中", "高"])
            lat = lat_base + random.uniform(-1.0, 1.0)
            lon = lon_base + random.uniform(-1.0, 1.0)
            c.execute("INSERT OR IGNORE INTO suppliers (supplier_id, name, contact, phone, email, risk_level, country, region, latitude, longitude, is_official) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)", 
                      (sid, name, contact, phone, email, risk_level, country, region, lat, lon))

            
        # 取得所有商品，為每家供應商建立 supplier_products 關聯
        c.execute("SELECT product_id, cost FROM inventory")
        products = c.fetchall()
        for i in range(1, 51):
            sid = f"SUP-{i:03d}"
            # 隨機指定提供哪些商品 (全給)
            for p in products:
                base_price = p[1] or 1000
                price = base_price * random.uniform(0.9, 1.2)
                # 碳排係數 (低碳排 0.5~2.5, 高碳排 2.0~8.0)
                carbon_factor = random.uniform(0.5, 3.5) if risk_level == "低" else random.uniform(2.0, 8.0)
                c.execute("INSERT INTO supplier_products (supplier_id, product_id, price, carbon_factor) VALUES (?, ?, ?, ?)", (sid, p[0], price, carbon_factor))
                
        # 計算碳排總計，選出最低前 20 家設為 is_official=1
        c.execute("UPDATE suppliers SET is_official = 0")
        c.execute('''
            SELECT supplier_id, SUM(carbon_factor) as tot 
            FROM supplier_products 
            GROUP BY supplier_id 
            ORDER BY tot ASC 
            LIMIT 20
        ''')
        top20 = c.fetchall()
        for row in top20:
            c.execute("UPDATE suppliers SET is_official = 1 WHERE supplier_id=?", (row[0],))

    # N3：既有 DB 的 legacy 明文密碼一次性升級為 salted hash（自我修復式遷移）
    try:
        from backend.passwords import hash_password, is_hashed
        for uname, pw in c.execute("SELECT username, password FROM users").fetchall():
            if pw and not is_hashed(pw):
                c.execute("UPDATE users SET password=? WHERE username=?",
                          (hash_password(pw), uname))
    except Exception:
        pass

    conn.commit()
    conn.close()


def run_query(query, params=(), fetch=True):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(query, params)
    if fetch:
        res = c.fetchall()
    else:
        conn.commit()
        res = c.lastrowid
    conn.close()
    return res


# ── 交易邊界：供多步驟原子讀寫使用 ─────────────────────────────

from contextlib import contextmanager


@contextmanager
def transaction(immediate: bool = False):
    """
    開啟一個交易邊界，讓「讀 prev_hash → 算 row_hash → 寫新列」在同一連線、
    同一 commit 內原子完成。任何中途例外都會自動 rollback。
    """
    conn = sqlite3.connect(DB_FILE, timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def tx_run(conn, query, params=(), fetch=True):
    """在既有交易連線 conn 中執行查詢，不回傳獨立的 connection。"""
    c = conn.cursor()
    c.execute(query, params)
    if fetch:
        return c.fetchall()
    return c.lastrowid
