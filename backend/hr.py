"""
backend/hr.py
人資 AI 工具函式（員工查詢、薪資、出勤）
"""

from .database import run_query
from .auth import check_permission


def get_employee_info(employee_id_or_name: str) -> str:
    """查詢員工資訊"""
    if not check_permission(["hr"]):
        return "權限不足：機密資料！只有『人資部門』可以查詢企業內部員工詳細資料。"

    res = run_query(
        "SELECT employee_id, name, department, role FROM hr WHERE employee_id=? OR name=?",
        (employee_id_or_name, employee_id_or_name),
    )
    if res:
        e_id, name, dept, role = res[0]
        return f"員工資料：\n- 姓名：{name}\n- 員工編號：{e_id}\n- 部門：{dept}\n- 職位：{role}"
    return f"找不到員工 {employee_id_or_name} 的資料。"


def get_payroll_summary() -> str:
    """查詢薪資摘要：最近月份、筆數、總額。"""
    if not check_permission(["admin", "hr"]):
        return "權限不足：僅店長或人資可查詢薪資。"
    res = run_query(
        "SELECT period, COUNT(*), SUM(base_salary + bonus - deduction) FROM payroll GROUP BY period ORDER BY period DESC LIMIT 12"
    )
    if not res:
        return "尚無薪資資料。"
    out = "📋 薪資摘要（依月份）：\n"
    for r in res:
        out += f"- {r[0]} | {r[1]} 人 | 實領合計 {r[2]:,.0f} 元\n"
    return out


def get_attendance_summary() -> str:
    """查詢出勤摘要：最近筆數、正常/請假/加班等統計。"""
    if not check_permission(["admin", "hr"]):
        return "權限不足：僅店長或人資可查詢出勤。"
    res = run_query("SELECT status, COUNT(*) FROM attendance GROUP BY status")
    if not res:
        return "尚無出勤資料。"
    out = "📋 出勤統計：\n"
    for r in res:
        out += f"- {r[0]}：{r[1]} 筆\n"
    rows = run_query(
        "SELECT work_date, employee_id, check_in, check_out, status FROM attendance ORDER BY work_date DESC LIMIT 15"
    )
    out += "\n最近紀錄：\n"
    for r in rows or []:
        out += f"- {r[0]} | {r[1]} | {r[2]}~{r[3]} | {r[4]}\n"
    return out
