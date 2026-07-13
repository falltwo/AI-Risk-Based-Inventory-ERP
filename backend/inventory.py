"""
backend/inventory.py
進銷存 AI 工具函式（庫存查詢、異動）
"""

import streamlit as st
from datetime import datetime, timedelta
from .database import run_query
from .auth import check_permission


def check_inventory(product_id: str) -> str:
    """查詢特定產品的庫存與價格資訊。"""
    if not check_permission(["warehouse", "sales"]):
        return "權限不足：您目前的角色無法查詢庫存資訊。"

    res = run_query(
        "SELECT name, stock, price, reorder_point, daily_sales FROM inventory WHERE product_id=?",
        (product_id,),
    )
    if res:
        name, stock, price, reorder_point, daily_sales = res[0]
        info_str = f"產品 {name} (編號: {product_id}) 目前庫存為 {stock} 件，單價 {price} 元。\n"
        if stock <= reorder_point:
            info_str += f"⚠️ 警告：目前庫存 ({stock}) 已低於或等於安全庫存水位 ({reorder_point})，建議【立即補貨】！"
        else:
            daily_sales = daily_sales if daily_sales > 0 else 1
            days_left = (stock - reorder_point) // daily_sales
            date_str = (datetime.now() + timedelta(days=days_left)).strftime("%m月%d號")
            info_str += f"✅ 目前庫存充足。將於大約 {days_left} 天後（約 {date_str}）需要補貨。"
        return info_str
    return f"找不到產品編號 {product_id} 的庫存資訊。"


def get_all_inventory() -> str:
    """查詢所有產品庫存資料。"""
    if not check_permission(["warehouse", "sales", "admin"]):
        return "權限不足：您目前的角色無法查詢庫存清單。"

    # 只選取所需欄位，相容 6 欄或 9 欄的 inventory 表
    res = run_query("SELECT product_id, name, stock, price, reorder_point, daily_sales FROM inventory")
    if not res:
        return "目前庫存為空。"

    result = "📦『全部產品庫存狀態』\n\n"
    low_stock_count = 0
    for r in res:
        p_id, name, stock, price, reorder, daily = r
        reorder = reorder if reorder is not None else 0
        daily = daily if (daily is not None and daily > 0) else 0
        if stock <= reorder:
            status = "🔴 需立即補貨"
            restock_note = "立即"
            low_stock_count += 1
        else:
            if daily <= 0:
                status = "🟢 庫存充足（近期無銷售）"
                restock_note = "暫無明確時間"
            else:
                days_left = max((stock - reorder) // daily, 0)
                date_str = (datetime.now() + timedelta(days=days_left)).strftime("%m月%d號")
                status = f"🟡 約 {days_left} 天後（{date_str}）需補貨"
                restock_note = f"{days_left} 天後"

        result += (
            f"『{name} ({p_id})』\n"
            f"● 庫存: {stock}\n"
            f"● 單價: {price}\n"
            f"● 安全庫存: {reorder}\n"
            f"● 預估補貨: {restock_note}\n"
            f"● 狀態: {status}\n\n"
        )

    result += f"共 {len(res)} 項商品；其中 {low_stock_count} 項需立即補貨。\n請您參考下方的圖表以獲取更詳細的資訊。"
    return result


def get_low_stock_inventory() -> str:
    """僅列出低於或等於安全庫存的品項（給智慧庫存管理使用）。"""
    if not check_permission(["warehouse", "sales", "admin"]):
        return "權限不足：您目前的角色無法查詢補貨清單。"

    res = run_query(
        "SELECT product_id, name, stock, price, reorder_point FROM inventory WHERE stock <= COALESCE(reorder_point, 0) ORDER BY stock ASC"
    )
    if not res:
        return "📦『需立即補貨的庫存列表』\n\n✅ 目前沒有低庫存品項。"

    result = "📦『需立即補貨的庫存列表』\n\n"
    for p_id, name, stock, price, reorder in res:
        reorder = reorder if reorder is not None else 0
        result += (
            f"『{name} ({p_id})』\n"
            f"● 庫存: {stock}\n"
            f"● 單價: {price}\n"
            f"● 安全庫存: {reorder}\n"
            f"● 狀態: 🔴 需立即補貨\n\n"
        )

    result += f"共 {len(res)} 項需補貨。"
    return result


def update_inventory(product_id: str, quantity_change: int) -> str:
    """更新特定產品的庫存數量(進貨/出貨)。"""
    if not check_permission(["warehouse"]):
        return "權限不足：只有『倉管人員』可以進行進退貨等庫存數量異動。"

    res = run_query("SELECT stock, name, warehouse_id FROM inventory WHERE product_id=?", (product_id,))
    if res:
        current_stock, name, warehouse_id = res[0]
        # 如果商品未設定倉庫，先給個預設值
        warehouse_id = warehouse_id if warehouse_id else 'WH01'
        
        new_stock = current_stock + quantity_change
        if new_stock < 0:
            return f"庫存不足！產品 {name} 目前只有 {current_stock} 件，無法出貨 {abs(quantity_change)} 件。"
        
        # 1. 更新主庫存表
        run_query("UPDATE inventory SET stock=? WHERE product_id=?", (new_stock, product_id), fetch=False)
        
        # 2. 自動寫入出入庫異動紀錄 (stock_moves) 以同步顯示在前端網頁
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        move_type = "入庫" if quantity_change > 0 else "出庫"
        run_query(
            "INSERT INTO stock_moves (product_id, warehouse_id, qty, move_type, ref_no, move_date, note) VALUES (?, ?, ?, ?, 'AI_BOT_01', ?, '透過 AI 語音/文字處理')",
            (product_id, warehouse_id, abs(quantity_change), move_type, now_str),
            fetch=False
        )
        
        action = "進貨" if quantity_change > 0 else "出貨"
        return f"✅ 成功{action} {abs(quantity_change)} 件。產品 {name} ({product_id}) 最新庫存為 {new_stock} 件。"
    return f"找不到產品編號 {product_id}。"


def rollback_inventory(product_id: str, quantity_change: int) -> str:
    """
    沖銷先前的庫存異動（補償交易）。
    將 update_inventory 的異動量反向執行，需 admin 權限。
    """
    if not check_permission(["admin"]):
        return "權限不足：只有『管理員』可以執行庫存沖銷。"
    return update_inventory(product_id=product_id, quantity_change=-quantity_change)


def get_inventory_total_value(use_cost: bool = True) -> str:
    """
    直接從資料庫取得庫存總價值的單一精確數字，供後續公式計算使用。
    問庫存總價值、打幾折、占比時請優先呼叫此工具取得數字，再用 calculate 運算。
    use_cost: True=以成本計（會計常用），False=以售價計。
    """
    if not check_permission(["admin", "warehouse", "sales"]):
        return "權限不足。"
    if use_cost:
        res = run_query("SELECT SUM(stock * COALESCE(cost, 0)) FROM inventory")
        label = "庫存總價值（成本）"
    else:
        res = run_query("SELECT SUM(stock * COALESCE(price, 0)) FROM inventory")
        label = "庫存總價值（售價）"
    val = (res[0][0] or 0) if res else 0
    # 回傳單一明確數字，方便 AI 直接帶入 calculate()
    return f"{label}：{val:,.0f} 元（精確數值={val}）"


def get_cost_analysis() -> str:
    """查詢成本分析：各品項成本、售價、毛利與毛利率。"""
    if not check_permission(["admin", "sales", "warehouse"]):
        return "權限不足：僅店長、業務或倉管可查詢成本分析。"
    res = run_query("SELECT product_id, name, cost, price FROM inventory")
    if not res:
        return "尚無品項可分析。"
    out = "📊 成本分析：\n"
    for r in res:
        cost, price = (r[2] or 0), (r[3] or 0)
        margin = price - cost
        pct = (margin * 100.0 / price) if price else 0
        out += f"- {r[0]} {r[1]} | 成本 {cost:,.0f} | 售價 {price:,.0f} | 毛利 {margin:,.0f} ({pct:.1f}%)\n"
    return out


def calculate_smart_restocking(days: int = 30) -> str:
    """
    根據過去特定天數的實際銷售訂單，計算動態安全庫存水位並建議補貨數量。
    AI 助理可呼叫此工具提供智慧補貨計畫。
    """
    if not check_permission(["admin", "warehouse", "sales"]):
        return "權限不足：您目前的角色無法執行智慧補貨分析。"

    # 預設補貨前置時間（Lead Time）為 7 天
    lead_time_days = 7
    # 緩衝期 3 天
    buffer_days = 3

    # 計算指定天數內的總銷量
    date_limit = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    sales_data = run_query(
        """
        SELECT product_id, SUM(quantity) 
        FROM orders 
        WHERE status != '已取消' AND order_date >= ? 
        GROUP BY product_id
        """,
        (date_limit,)
    )
    sales_dict = {row[0]: row[1] for row in sales_data} if sales_data else {}

    inventory_data = run_query("SELECT product_id, name, stock FROM inventory")
    if not inventory_data:
        return "目前沒有庫存資料可供分析。"

    result = f"🤖 智慧補貨建議報告（基於過去 {days} 天歷史銷量，預估進貨前置時間 {lead_time_days} 天）：\n\n"
    needs_restock = False

    for item in inventory_data:
        p_id, name, stock = item
        total_sold = sales_dict.get(p_id, 0)
        
        # 避免沒有銷售紀錄導致的除零或全零狀況
        avg_daily_sales = total_sold / days if total_sold > 0 else 0.1
        
        # 更新的安全庫存 = 日均銷量 * (前置天數 + 緩衝天數)
        suggested_reorder_point = int(avg_daily_sales * (lead_time_days + buffer_days))
        # 若連 1 件都不到，最少設為 1
        suggested_reorder_point = max(suggested_reorder_point, 1)

        if stock <= suggested_reorder_point:
            needs_restock = True
            # 建議進貨量 = 安全庫存 - 目前庫存 + (一週額外銷售量)
            suggested_po_qty = suggested_reorder_point - stock + int(avg_daily_sales * 7)
            suggested_po_qty = max(suggested_po_qty, 1)
            
            result += f"⚠️ **{name} ({p_id})**\n"
            result += f"  - 日均銷量: {avg_daily_sales:.1f} 件/天\n"
            result += f"  - 目前庫存: {stock} / 建議安全水位: {suggested_reorder_point}\n"
            result += f"  - 👉 **建議進貨: {suggested_po_qty} 件**\n\n"
            
    if not needs_restock:
        result += "✅ 目前所有產品庫存皆位於安全水位之上，暫時不需要補貨。"
        
    return result
