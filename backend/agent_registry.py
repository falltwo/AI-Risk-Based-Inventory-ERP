"""
backend/agent_registry.py
Agent 名單登記表 — Day 1 上午交付（組員 B｜總管 Agent）

職責：
  - 定義總管 Agent 底下的 8 個專責 Agent（名單）
  - 記錄每個 Agent 的工具白名單、可寫入旗標、路由關鍵字
  - 提供一個「關鍵字版」基礎路由器 route_by_keyword()，供 Day 1 驗證用
    （Day 2 的 agent_orchestrator.py 會改用 Gemini 來做語意路由，本檔仍是 fallback）

與 A（工具治理）的對齊：
  - 每個 Agent 的 modules 直接對應 A 的 tool_classification.py 的 "module" 欄位
  - 若 A 的 backend.tool_registry 已合併，get_tools_for_agent() 會優先用
    registry.get_tools_by_module() 動態取得工具，確保與分級表單一真實來源（SSOT）
  - 若 A 尚未合併（目前 main 狀態），則退回使用本檔內建的 _FALLBACK_TOOLS 白名單

使用方式：
    from backend.agent_registry import AGENTS, route_by_keyword, get_tools_for_agent
    agent_id = route_by_keyword("幫我看一下哪些商品快缺貨了")   # -> "inventory_agent"
    tools    = get_tools_for_agent(agent_id)                      # -> ["check_inventory", ...]
"""

from __future__ import annotations


# ──────────────────────────────────────────────────────────────────────────
# 1) Agent 名單（8 個：7 個專責 + 1 個客服 fallback）
#
#    每個 Agent 欄位說明：
#      name_zh     顯示用中文名
#      name_en     程式/log 用英文代號
#      modules     對應 A 的 tool_classification "module" 欄位（工具來源）
#      can_write   此 Agent 是否被允許觸發 write 級工具（仍須走審批，見 C）
#      description 給總管 Agent system prompt 用的職責描述
#      keywords    關鍵字版路由用的觸發詞（Day 2 會被語意路由取代/補強）
# ──────────────────────────────────────────────────────────────────────────
AGENTS: dict[str, dict] = {

    "inventory_agent": {
        "name_zh": "庫存 Agent",
        "name_en": "InventoryAgent",
        "modules": ["inventory", "manufacturing"],
        "can_write": True,                       # update_inventory 為 write，須審批
        "description": "負責商品庫存查詢、低庫存與安全庫存判斷、補貨建議、"
                       "庫存價值與成本分析，以及製造端 BOM 與工單狀態。",
        "keywords": [
            "庫存", "缺貨", "補貨", "安全庫存", "進貨", "盤點", "存貨",
            "成本", "毛利", "BOM", "物料", "工單", "製造", "生產",
        ],
    },

    "procurement_agent": {
        "name_zh": "採購 Agent",
        "name_en": "ProcurementAgent",
        "modules": ["procurement"],
        "can_write": False,                      # 目前採購工具皆 read_only
        "description": "負責供應商名單、採購單摘要、應付帳款查詢，"
                       "以及與庫存 Agent 協作的補貨採購情境。",
        "keywords": [
            "採購", "供應商", "進貨單", "採購單", "應付", "付款", "PO", "下單給供應商",
        ],
    },

    "sales_agent": {
        "name_zh": "銷售 Agent",
        "name_en": "SalesAgent",
        "modules": ["orders"],
        "can_write": True,                       # create_order 為 write，須審批
        "description": "負責銷售訂單查詢與建立、應收帳款、客戶名單、報價單摘要。",
        "keywords": [
            "銷售", "訂單", "下單", "出貨", "客戶", "報價", "應收", "業績", "成交",
        ],
    },

    "finance_agent": {
        "name_zh": "財務 Agent",
        "name_en": "FinanceAgent",
        "modules": ["finance"],
        "can_write": False,
        "description": "負責總帳摘要、財務概況（庫存成本／銷售額／應收應付）、"
                       "以及數學公式計算。財務資料敏感，限管理者層級。",
        "keywords": [
            "財務", "總帳", "帳", "損益", "財報", "現金", "營收", "計算", "公式", "試算",
        ],
    },

    "hr_agent": {
        "name_zh": "人資 Agent",
        "name_en": "HRAgent",
        "modules": ["hr"],
        "can_write": False,
        "description": "負責員工資訊、薪資摘要、出勤統計查詢。"
                       "薪資為高度敏感資料，限人資與管理者。",
        "keywords": [
            "員工", "人資", "薪資", "薪水", "出勤", "請假", "考勤", "人事",
        ],
    },

    "esg_agent": {
        "name_zh": "ESG Agent",
        "name_en": "ESGAgent",
        "modules": ["carbon"],
        "can_write": False,
        "description": "負責碳排放量（月／年）、碳足跡報告、ESG 減碳目標查詢。",
        "keywords": [
            "碳排", "碳", "ESG", "減碳", "碳足跡", "環保", "永續", "排放",
        ],
    },

    "risk_agent": {
        "name_zh": "供應鏈風險 Agent",
        "name_en": "SupplyChainRiskAgent",
        "modules": ["ai_supply_chain"],
        "can_write": False,
        "description": "負責供應鏈風險事件、受影響採購單、風險熱圖摘要查詢，"
                       "並結合外部新聞情報（GNews／RSS）評估延遲天數與替代建議。",
        "keywords": [
            "風險", "供應鏈", "斷鏈", "延遲", "熱圖", "地緣", "戰爭", "罷工",
            "天災", "颱風", "地震", "封港", "新聞", "情報", "受影響",
        ],
    },

    # ── 客服 Agent：無專屬工具。負責一般問答、招呼、無法歸類的任務，
    #    以及「老闆早報」這類跨多 Agent 彙整的場景（由總管派多個 Agent 後彙整）。
    "cs_agent": {
        "name_zh": "客服 Agent",
        "name_en": "CustomerServiceAgent",
        "modules": [],
        "can_write": False,
        "description": "負責一般招呼、操作說明、無法明確歸類的問題，"
                       "以及跨領域『老闆早報』的開場與收斂彙整。為總管的預設 fallback。",
        "keywords": [
            "你好", "嗨", "哈囉", "謝謝", "怎麼用", "說明", "幫助", "早報", "總覽", "報告",
        ],
    },
}

# 預設 fallback Agent：路由無法命中任何專責 Agent 時的歸屬
DEFAULT_AGENT = "cs_agent"


# ──────────────────────────────────────────────────────────────────────────
# 2) 工具白名單 fallback
#    A 的 tool_registry 合併前，先用這份內建白名單。
#    內容與 A 的 tool_classification.py "module" 欄位一致（30 個工具全覆蓋）。
# ──────────────────────────────────────────────────────────────────────────
_FALLBACK_TOOLS: dict[str, list[str]] = {
    "inventory_agent": [
        "check_inventory", "get_all_inventory", "get_low_stock_inventory",
        "get_inventory_total_value", "get_cost_analysis",
        "calculate_smart_restocking", "update_inventory",
        "get_bom_list", "get_work_orders_status",            # manufacturing
    ],
    "procurement_agent": [
        "get_payables", "get_suppliers_list", "get_purchase_orders_summary",
    ],
    "sales_agent": [
        "get_recent_orders", "create_order", "get_receivables",
        "get_customers_list", "get_quotations_summary",
    ],
    "finance_agent": [
        "get_ledger_summary", "get_financial_overview", "calculate",
    ],
    "hr_agent": [
        "get_employee_info", "get_payroll_summary", "get_attendance_summary",
    ],
    "esg_agent": [
        "get_carbon_emissions_by_month", "get_carbon_emissions_by_year",
        "get_carbon_footprint_report", "get_esg_targets",
    ],
    "risk_agent": [
        "get_supply_chain_risk_events", "get_impacted_purchase_orders",
        "get_supply_chain_heatmap_summary",
    ],
    "cs_agent": [],
}


# ──────────────────────────────────────────────────────────────────────────
# 3) 查詢輔助函式
# ──────────────────────────────────────────────────────────────────────────
def list_agents() -> list[str]:
    """回傳所有 agent_id"""
    return list(AGENTS.keys())


def get_agent(agent_id: str) -> dict | None:
    """回傳單一 Agent 的 metadata，不存在時回傳 None"""
    return AGENTS.get(agent_id)


def get_tools_for_agent(agent_id: str) -> list[str]:
    """
    回傳某 Agent 的工具白名單。
    優先使用 A 的 tool_registry（單一真實來源）；A 未合併時退回 _FALLBACK_TOOLS。
    """
    agent = AGENTS.get(agent_id)
    if not agent:
        return []

    try:
        # A（工具治理）已合併時走這條：依 module 動態取工具，保證與分級表一致
        from backend.tool_registry import registry  # type: ignore
        tools: list[str] = []
        for module in agent["modules"]:
            tools.extend(registry.get_tools_by_module(module))
        return tools
    except Exception:
        # A 尚未合併（目前 main）：用內建白名單
        return list(_FALLBACK_TOOLS.get(agent_id, []))


def route_by_keyword(task: str) -> str:
    """
    關鍵字版基礎路由器（Day 1 驗證用 / Day 2 語意路由的 fallback）。

    規則：
      - 對每個專責 Agent，數 task 中命中的關鍵字數量
      - 命中最多者勝出；全部 0 命中則回 DEFAULT_AGENT（客服）
      - 平手時依 AGENTS 宣告順序（庫存 > 採購 > 銷售 …）取第一個
    """
    text = (task or "").lower()
    best_id = DEFAULT_AGENT
    best_hits = 0
    for agent_id, meta in AGENTS.items():
        if agent_id == DEFAULT_AGENT:
            continue
        hits = sum(1 for kw in meta["keywords"] if kw.lower() in text)
        if hits > best_hits:
            best_hits = hits
            best_id = agent_id
    return best_id


# 反查：工具 → 所屬 Agent（給 Gateway / Dashboard 顯示「這個工具是哪個 Agent 在用」）
def get_agent_for_tool(tool_name: str) -> str | None:
    for agent_id in AGENTS:
        if tool_name in _FALLBACK_TOOLS.get(agent_id, []):
            return agent_id
    return None


# ── F10：SSOT 一致性檢查 ─────────────────────────────────────────
#   啟動時自動驗證：每個 Agent 的 can_write 旗標與 tool_classification 的
#   risk_level 是否一致。can_write=True 至少要有一個 write 級工具；
#   can_write=False 則完全不能有 write 級工具。不符則 raise，
#   防止兩份真實來源（registry 手填 vs 分級表）靜默飄移。
def _assert_can_write_consistency() -> None:
    try:
        from backend.tool_registry import registry
    except Exception:
        return  # 分級表尚未就緒時跳過（測試環境 / 部分載入）
    for agent_id, meta in AGENTS.items():
        tools = get_tools_for_agent(agent_id)
        has_write = any(
            (registry.get_tool_info(t) or {}).get("risk_level") in ("write", "dangerous")
            for t in tools
        )
        if meta.get("can_write") and not has_write and tools:
            raise ValueError(
                f"[F10 SSOT] Agent '{agent_id}' can_write=True 但工具白名單中"
                f"沒有任何 write/dangerous 級工具 —— 請檢查 tool_classification.py"
            )
        if not meta.get("can_write") and has_write:
            raise ValueError(
                f"[F10 SSOT] Agent '{agent_id}' can_write=False 但工具白名單中"
                f"含有 write/dangerous 級工具 —— 請修正 agent_registry 或分級表"
            )


_assert_can_write_consistency()
