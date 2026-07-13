"""
backend/carbon.py
碳排放與 ESG 查詢，供 AI 智能助理呼叫
"""
import sqlite3
import pandas as pd
from backend.database import DB_FILE
from backend.auth import check_permission

def __pd_read(query, conn, params=()):
    try:
        return pd.read_sql_query(query, conn, params=params)
    except Exception as e:
        print(f"SQL Error: {e}")
        return pd.DataFrame()

def get_carbon_emissions_by_month(year_month: str) -> str:
    """查詢某月（例如 '2023-10'）的碳排放量（依 Scope 1/2/3區分）。"""
    if not check_permission(["admin", "sales", "warehouse"]):
        return "權限不足：您目前的角色沒有權限查詢碳排放資料。"
    conn = sqlite3.connect(DB_FILE)
    q = '''
    SELECT cf.scope as Scope範疇, SUM(o.quantity * cf.kg_co2_per_unit) as 碳排放量_kg_CO2e
    FROM orders o
    JOIN carbon_factors cf ON cf.product_id = o.product_id
    WHERE strftime('%Y-%m', o.order_date)=? AND o.status != '已取消'
    GROUP BY cf.scope
    '''
    df = __pd_read(q, conn, params=(year_month,))
    conn.close()
    if df is None or df.empty:
        return f"{year_month} 沒有碳排放紀錄。"
    return f"🌍 {year_month} 碳排放量：\n" + df.to_string(index=False)

def get_carbon_emissions_by_year(year: str) -> str:
    """查詢某年（例如 '2023'）的碳排放量（依 Scope 1/2/3區分）。"""
    if not check_permission(["admin", "sales", "warehouse"]):
        return "權限不足：您目前的角色沒有權限查詢碳排放資料。"
    conn = sqlite3.connect(DB_FILE)
    q = '''
    SELECT cf.scope as Scope範疇, SUM(o.quantity * cf.kg_co2_per_unit) as 碳排放量_kg_CO2e
    FROM orders o
    JOIN carbon_factors cf ON cf.product_id = o.product_id
    WHERE strftime('%Y', o.order_date)=? AND o.status != '已取消'
    GROUP BY cf.scope
    '''
    df = __pd_read(q, conn, params=(year,))
    conn.close()
    if df is None or df.empty:
        return f"{year} 年沒有碳排放紀錄。"
    return f"🌍 {year} 年碳排放量：\n" + df.to_string(index=False)

def get_carbon_footprint_report(year_month: str) -> str:
    """查詢某月（例如 '2023-10'）各產品的碳足跡明細。"""
    if not check_permission(["admin", "sales", "warehouse"]):
        return "權限不足：您目前的角色沒有權限查詢碳排放資料。"
    conn = sqlite3.connect(DB_FILE)
    q = '''
    SELECT o.product_id as 品號, i.name as 品名, SUM(o.quantity) as 銷售數量,
        (SELECT cf.kg_co2_per_unit FROM carbon_factors cf WHERE cf.product_id=o.product_id ORDER BY cf.scope DESC LIMIT 1) as 碳係數,
        SUM(o.quantity) * (SELECT cf.kg_co2_per_unit FROM carbon_factors cf WHERE cf.product_id=o.product_id ORDER BY cf.scope DESC LIMIT 1) as 碳足跡_kg_CO2e
    FROM orders o
    LEFT JOIN inventory i ON o.product_id=i.product_id
    WHERE strftime('%Y-%m', o.order_date)=? AND o.status != '已取消'
    GROUP BY o.product_id
    '''
    df = __pd_read(q, conn, params=(year_month,))
    conn.close()
    if df is None or df.empty:
        return f"{year_month} 沒有產品碳足跡紀錄。"
    return f"🏢 {year_month} 產品碳足跡明細：\n" + df.to_string(index=False)

def get_esg_targets(year: str) -> str:
    """查詢某年（例如 '2023'）設定的 ESG 年度減量目標。"""
    if not check_permission(["admin"]):
        return "權限不足：僅店長(admin)可查詢 ESG 目標設定。"
    conn = sqlite3.connect(DB_FILE)
    q = "SELECT scope as Scope範疇, baseline_kg_co2 as 基準排放, target_kg_co2 as 目標排放, note as 備註 FROM esg_targets WHERE target_year = ? ORDER BY scope"
    try:
        yr_int = int(year)
    except Exception:
        yr_int = 0
    df = __pd_read(q, conn, params=(yr_int,))
    conn.close()
    if df is None or df.empty:
        return f"尚未設定 {year} 年的 ESG 減量目標。"
    return f"🎯 {year} 年 ESG 減量目標：\n" + df.to_string(index=False)
