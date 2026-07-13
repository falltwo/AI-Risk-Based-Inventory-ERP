"""
backend/manufacturing.py
生產管理 AI 工具函式（BOM 物料清單、製造工單）
"""

from .database import run_query
from .auth import check_permission


def get_bom_list() -> str:
    """查詢 BOM 物料清單：成品、組成料件、用量。"""
    if not check_permission(["admin", "warehouse"]):
        return "權限不足：僅店長或倉管可查詢 BOM。"
    res = run_query("SELECT b.product_id, b.component_id, b.qty_per FROM bom b LIMIT 50")
    if not res:
        return "尚無 BOM 資料。"
    out = "📋 BOM 物料清單：\n"
    for r in res:
        out += f"- 成品 {r[0]} → 料件 {r[1]}，用量 {r[2]}\n"
    return out


def get_work_orders_status() -> str:
    """查詢製造工單狀態：工單號、品項、計畫量、完成量、狀態。"""
    if not check_permission(["admin", "warehouse"]):
        return "權限不足：僅店長或倉管可查詢工單。"
    res = run_query(
        "SELECT wo_id, product_id, qty_plan, qty_done, status FROM work_orders ORDER BY start_date DESC LIMIT 25"
    )
    if not res:
        return "尚無工單資料。"
    out = "📋 製造工單狀態：\n"
    for r in res:
        pct = (100.0 * r[3] / r[2]) if r[2] else 0
        out += f"- {r[0]} | 品號 {r[1]} | 計畫 {r[2]} 完成 {r[3]} ({pct:.0f}%) | {r[4]}\n"
    return out
