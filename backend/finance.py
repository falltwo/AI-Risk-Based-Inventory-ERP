"""
backend/finance.py
財務會計 AI 工具函式（總帳、財務概況）
"""

from .database import run_query
from .auth import check_permission


def get_ledger_summary() -> str:
    """查詢總帳摘要：近期分錄筆數與合計。"""
    if not check_permission(["admin"]):
        return "權限不足：僅店長可查詢總帳。"
    res = run_query("SELECT COUNT(*), COALESCE(SUM(debit),0), COALESCE(SUM(credit),0) FROM general_ledger")
    cnt, deb, cred = (res[0][0], res[0][1], res[0][2]) if res else (0, 0, 0)
    rows = run_query(
        "SELECT ledger_date, account, debit, credit, description FROM general_ledger ORDER BY id DESC LIMIT 15"
    )
    out = f"📒 總帳摘要：共 **{cnt}** 筆 | 借方合計 {deb:,.0f} | 貸方合計 {cred:,.0f}\n\n最近分錄：\n"
    for r in rows or []:
        out += f"- {r[0]} | {r[1]} | 借 {r[2]:,.0f} | 貸 {r[3]:,.0f} | {str(r[4])[:30]}\n"
    return out


def get_financial_overview() -> str:
    """查詢財務概況：庫存成本、已出貨銷售額、應收/應付簡要。"""
    if not check_permission(["admin"]):
        return "權限不足：僅店長可查詢財務概況。"
    inv = run_query("SELECT SUM(stock * COALESCE(cost, 0)) FROM inventory")
    sales = run_query("SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE status = '已出貨'")
    rec = run_query("SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE status NOT IN ('已取消','已出貨')")
    pay = run_query("SELECT COALESCE(SUM(total_amount),0) FROM purchase_orders WHERE status = '待入庫'")
    inv_val = inv[0][0] or 0
    sales_val = sales[0][0] if sales else 0
    rec_val = rec[0][0] if rec else 0
    pay_val = pay[0][0] if pay else 0
    out = "📊 財務概況：\n"
    out += f"- 庫存成本總額：{inv_val:,.0f} 元\n"
    out += f"- 已出貨銷售額：{sales_val:,.0f} 元\n"
    out += f"- 應收（未出貨訂單）：{rec_val:,.0f} 元\n"
    out += f"- 應付（待入庫採購）：{pay_val:,.0f} 元\n"
    return out


def calculate(expression: str, description: str = "") -> str:
    """
    執行數學公式計算。當你需要對查詢到的數字做加總、比例、百分比、平均等運算時請呼叫此工具。
    expression: 數學算式，僅可含數字與 + - * / ( ) 及小數點，例如 "150000*0.05" 或 "100+200*3"
    description: 可選，說明這是在算什麼（例如「應收的 5% 備抵」）
    """
    if not check_permission(["admin", "sales", "warehouse", "hr"]):
        return "權限不足。"
    import re
    expr = (expression or "").strip().replace(",", "").replace(" ", "")
    if not expr:
        return "請提供算式，例如：150000 * 0.05 或 (100+200)/3"
    if not re.match(r"^[\d+\-*/().]+$", expr):
        return "算式僅能包含數字與 + - * / ( ) 和小數點，請勿包含變數或文字。"
    try:
        result = eval(expr)
        if isinstance(result, (int, float)):
            return f"計算結果：{result:,.2f}" + (f"（{description}" if description else "") + ("）" if description else "")
        return f"計算結果：{result}"
    except Exception as e:
        return f"計算錯誤：{e}。請確認算式正確，例如 100+200*0.05"
