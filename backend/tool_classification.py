"""
backend/tool_classification.py
工具分級定義表 — Day 1 上午交付
供 tool_registry.py 直接引用

風險等級：
  read_only  : 只查詢，不改資料庫，直接執行
  suggestion : 產生建議但不寫入資料庫，直接執行
  write      : 會改資料庫，需送審批後才執行
  dangerous  : 高風險操作（刪除、大量修改），需人工審批
"""

# 角色清單：admin / warehouse / sales / hr
# ALL 代表所有角色皆可使用

TOOL_CLASSIFICATION = {

    # ── 庫存模組 ──────────────────────────────────────────────
    "check_inventory": {
        "module": "inventory",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "查詢單一商品庫存、價格、補貨警告",
    },
    "get_all_inventory": {
        "module": "inventory",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "查詢全部商品庫存列表",
    },
    "get_low_stock_inventory": {
        "module": "inventory",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "查詢低於安全庫存的商品清單",
    },
    "get_inventory_total_value": {
        "module": "inventory",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "查詢庫存總價值（成本或售價）",
    },
    "get_cost_analysis": {
        "module": "inventory",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse"],
        "description": "查詢品項成本、售價、毛利率分析",
    },
    "calculate_smart_restocking": {
        "module": "inventory",
        "risk_level": "suggestion",
        "allowed_roles": ["admin", "warehouse"],
        "description": "根據歷史銷量產生補貨建議，不寫入資料庫",
    },
    "update_inventory": {
        "module": "inventory",
        "risk_level": "write",
        "allowed_roles": ["admin", "warehouse"],
        "description": "更新庫存數量（進貨／退貨），會寫入資料庫",
    },
    "rollback_inventory": {
        "module": "inventory",
        "risk_level": "write",
        "allowed_roles": ["admin"],
        "description": "沖銷先前的庫存異動（補償交易），會寫入資料庫",
    },

    # ── 訂單模組 ──────────────────────────────────────────────
    "get_recent_orders": {
        "module": "orders",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "查詢近期銷售訂單列表",
    },
    "get_receivables": {
        "module": "orders",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "sales"],
        "description": "查詢應收帳款（未出貨訂單總額）",
    },
    "get_customers_list": {
        "module": "orders",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "sales"],
        "description": "查詢客戶清單",
    },
    "get_quotations_summary": {
        "module": "orders",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "sales"],
        "description": "查詢報價單摘要",
    },
    "create_order": {
        "module": "orders",
        "risk_level": "write",
        "allowed_roles": ["admin", "sales"],
        "description": "建立銷售訂單並自動扣庫存，會寫入資料庫",
    },
    "cancel_order": {
        "module": "orders",
        "risk_level": "write",
        "allowed_roles": ["admin"],
        "description": "取消訂單並回補庫存（補償交易），會寫入資料庫",
    },

    # ── 採購模組 ──────────────────────────────────────────────
    "get_payables": {
        "module": "procurement",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse"],
        "description": "查詢應付帳款（待結清採購金額）",
    },
    "get_suppliers_list": {
        "module": "procurement",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "查詢供應商清單",
    },
    "get_purchase_orders_summary": {
        "module": "procurement",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse"],
        "description": "查詢採購單摘要",
    },

    # ── 財務模組 ──────────────────────────────────────────────
    "get_ledger_summary": {
        "module": "finance",
        "risk_level": "read_only",
        "allowed_roles": ["admin"],
        "description": "查詢總帳摘要（借貸合計）",
    },
    "get_financial_overview": {
        "module": "finance",
        "risk_level": "read_only",
        "allowed_roles": ["admin"],
        "description": "查詢財務概況（庫存成本、銷售額、應收應付）",
    },
    "calculate": {
        "module": "finance",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "執行數學公式計算（加減乘除、百分比），不改資料庫",
    },

    # ── 人資模組 ──────────────────────────────────────────────
    "get_employee_info": {
        "module": "hr",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "hr"],
        "description": "查詢員工個人資訊（薪資敏感，限人資）",
    },
    "get_payroll_summary": {
        "module": "hr",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "hr"],
        "description": "查詢薪資摘要（按月份）",
    },
    "get_attendance_summary": {
        "module": "hr",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "hr"],
        "description": "查詢出勤統計與紀錄",
    },

    # ── 製造模組 ──────────────────────────────────────────────
    "get_bom_list": {
        "module": "manufacturing",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse"],
        "description": "查詢 BOM 物料清單（成品對應料件）",
    },
    "get_work_orders_status": {
        "module": "manufacturing",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse"],
        "description": "查詢製造工單狀態與進度",
    },

    # ── 碳排 / ESG 模組 ───────────────────────────────────────
    "get_carbon_emissions_by_month": {
        "module": "carbon",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "查詢每月碳排放量",
    },
    "get_carbon_emissions_by_year": {
        "module": "carbon",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "查詢每年碳排放量",
    },
    "get_carbon_footprint_report": {
        "module": "carbon",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "查詢碳足跡完整報告",
    },
    "get_esg_targets": {
        "module": "carbon",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "查詢 ESG 碳排目標",
    },

    # ── 供應鏈風險模組 ────────────────────────────────────────
    "get_supply_chain_risk_events": {
        "module": "ai_supply_chain",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "查詢供應鏈風險事件清單",
    },
    "get_impacted_purchase_orders": {
        "module": "ai_supply_chain",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse"],
        "description": "查詢受風險事件影響的採購單",
    },
    "get_supply_chain_heatmap_summary": {
        "module": "ai_supply_chain",
        "risk_level": "read_only",
        "allowed_roles": ["admin", "warehouse", "sales", "hr"],
        "description": "查詢供應鏈風險熱圖摘要",
    },
}

# 快速查詢用的分組
READ_ONLY_TOOLS  = [k for k, v in TOOL_CLASSIFICATION.items() if v["risk_level"] == "read_only"]
SUGGESTION_TOOLS = [k for k, v in TOOL_CLASSIFICATION.items() if v["risk_level"] == "suggestion"]
WRITE_TOOLS      = [k for k, v in TOOL_CLASSIFICATION.items() if v["risk_level"] == "write"]
DANGEROUS_TOOLS  = [k for k, v in TOOL_CLASSIFICATION.items() if v["risk_level"] == "dangerous"]
