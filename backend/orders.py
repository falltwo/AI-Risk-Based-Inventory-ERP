"""
backend/orders.py
銷售訂單 AI 工具函式（建立訂單、查詢訂單、應收帳款）
"""

from datetime import datetime
from .database import run_query
from .auth import check_permission


def get_recent_orders() -> str:
    """查詢近期所有訂單（包含訂單建立日期）"""
    if not check_permission(["sales", "warehouse"]):
        return "權限不足：您目前的角色沒有權限查詢訂單系統。"

    res = run_query(
        "SELECT o.order_id, i.name, o.quantity, o.status, o.order_date "
        "FROM orders o LEFT JOIN inventory i ON o.product_id = i.product_id"
    )
    if not res:
        return "目前沒有任何訂單記錄。"

    result = "🧾 近期訂單列表：\n"
    for r in res:
        result += f"- 訂單號碼: {r[0]}, 產品: {r[1]}, 數量: {r[2]}, 狀態: {r[3]}, 建立時間: {r[4]}\n"
    return result


def create_order(product_id: str, quantity: int) -> str:
    """
    建立一筆新訂單（銷售產品）。
    這會自動在訂單管理系統中新增一筆紀錄，並且自動扣除對應的產品庫存。
    Args:
        product_id: 產品編號 (例如 P001)。
        quantity: 訂購/售出的數量 (必須為正數)。
    """
    if not check_permission(["sales", "admin"]):
        return "權限不足：只有『業務部』或『店長』可以建立新訂單。"

    if quantity <= 0:
        return "建立失敗：訂單數量必須 > 0。"

    # 先檢查庫存是否足夠
    res = run_query("SELECT stock, name FROM inventory WHERE product_id=?", (product_id,))
    if not res:
        return f"建立失敗：找不到產品編號 {product_id}。"

    current_stock, name = res[0]
    if current_stock < quantity:
        return f"建立失敗：庫存不足！產品 {name} 目前只有 {current_stock} 件，無法售出 {quantity} 件。"

    # 產生訂單號碼與時間
    now_dt = datetime.now()
    order_id = f"ORD-{now_dt.strftime('%Y%m%d-%H%M%S')}"
    order_date = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    res_price = run_query("SELECT price FROM inventory WHERE product_id=?", (product_id,))
    total_amount = (quantity * res_price[0][0]) if res_price else 0

    try:
        run_query(
            "INSERT INTO orders (order_id, customer_id, product_id, quantity, status, order_date, total_amount) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (order_id, None, product_id, quantity, "處理中", order_date, total_amount),
            fetch=False,
        )
        # 扣除庫存
        run_query("UPDATE inventory SET stock=? WHERE product_id=?", (current_stock - quantity, product_id), fetch=False)
        return (
            f"✅ 成功建立新訂單 (單號: {order_id}，時間: {order_date})："
            f"售出 {quantity} 件 {name} ({product_id})，庫存已自動扣除，目前剩餘 {current_stock - quantity} 件。"
        )
    except Exception as e:
        return f"建立訂單時發生資料庫錯誤：{e}"


def cancel_order(product_id: str, quantity: int, customer_id: str = "") -> str:
    """
    取消最近一筆符合條件的訂單，並回補庫存。
    需 admin 權限。
    若提供 customer_id 則只比對該客戶的訂單，避免誤刪他客戶同品項同數量訂單。
    """
    if not check_permission(["admin"]):
        return "權限不足：只有『管理員』可以取消訂單。"

    if customer_id:
        rows = run_query(
            "SELECT order_id FROM orders WHERE (customer_id = ? OR customer_id IS NULL) AND product_id = ? AND quantity = ? ORDER BY order_date DESC LIMIT 1",
            (customer_id, product_id, quantity),
        )
    else:
        rows = run_query(
            "SELECT order_id FROM orders WHERE product_id = ? AND quantity = ? ORDER BY order_date DESC LIMIT 1",
            (product_id, quantity),
        )

    if not rows:
        return f"找不到產品 {product_id} 數量 {quantity} 的待取消訂單。"

    order_id = rows[0][0]
    run_query("DELETE FROM orders WHERE order_id = ?", (order_id,), fetch=False)
    from backend.inventory import update_inventory
    update_inventory(product_id=product_id, quantity_change=quantity)
    return f"✅ 已取消訂單 {order_id}，並將產品 {product_id} 庫存回補 {quantity} 件。"


def get_receivables() -> str:
    """查詢應收帳款：未出貨或待收款的訂單金額總和與摘要。"""
    if not check_permission(["admin", "sales"]):
        return "權限不足：僅店長或業務可查詢應收帳款。"
    res = run_query("SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE status NOT IN ('已取消') AND status != '已出貨'")
    total = res[0][0] if res else 0
    detail = run_query(
        "SELECT order_id, customer_id, total_amount, status FROM orders WHERE status NOT IN ('已取消') ORDER BY order_date DESC LIMIT 20"
    )
    out = f"📊 應收帳款（未出貨/處理中訂單）：總計 **{total:,.0f}** 元\n\n近期訂單：\n"
    for r in detail or []:
        out += f"- 訂單 {r[0]} | 客戶 {r[1] or '-'} | 金額 {r[2]:,.0f} | 狀態 {r[3]}\n"
    return out


def get_customers_list() -> str:
    """查詢客戶列表：代號、名稱、聯絡人、電話。"""
    if not check_permission(["admin", "sales"]):
        return "權限不足：僅店長或業務可查詢客戶資料。"
    res = run_query("SELECT customer_id, name, contact, phone FROM customers")
    if not res:
        return "目前尚無客戶資料。"
    out = "📋 客戶列表：\n"
    for r in res:
        out += f"- {r[0]} | {r[1]} | 聯絡人 {r[2] or '-'} | {r[3] or '-'}\n"
    return out


def get_quotations_summary() -> str:
    """查詢報價單摘要：單號、客戶、金額、狀態、筆數。"""
    if not check_permission(["admin", "sales"]):
        return "權限不足：僅店長或業務可查詢報價單。"
    res = run_query("SELECT COUNT(*), COALESCE(SUM(total_amount),0) FROM quotations")
    cnt, total = (res[0][0], res[0][1]) if res else (0, 0)
    rows = run_query(
        "SELECT quote_id, customer_id, total_amount, status, quote_date FROM quotations ORDER BY quote_date DESC LIMIT 15"
    )
    out = f"📋 報價單摘要：共 **{cnt}** 筆，總金額 **{total:,.0f}** 元\n\n最近報價：\n"
    for r in rows or []:
        out += f"- {r[0]} | 客戶 {r[1]} | {r[2]:,.0f} 元 | {r[3]} | {r[4]}\n"
    return out
