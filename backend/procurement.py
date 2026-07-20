"""
backend/procurement.py
採購管理 AI 工具函式（供應商、採購單、應付帳款）
"""

import math
import sqlite3

from .database import run_query
from .auth import check_permission


# 只有 Tool Gateway 的核准交易路徑會持有此 module-private sentinel。
# 這是應用程式內的防誤用邊界，不是可抵擋惡意 Python 程式碼的密碼邊界。
_PO_APPROVAL_CONTEXT = object()


def _required_text(value, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def create_purchase_order(
    po_id: str,
    supplier_id: str,
    product_id: str,
    qty: int,
    unit_price: float,
    order_date: str,
    status: str,
    note: str = "",
    **_internal,
) -> dict:
    """Create one PO header and item inside an already-approved DB transaction.

    This protected write primitive deliberately cannot open or commit its own
    connection. The Tool Gateway must inject the active approval context,
    SQLite connection, and stable operation ID after revalidating approval.
    """
    approval_context = _internal.get("_approval_context")
    conn = _internal.get("_conn")
    operation_id = _internal.get("_operation_id")
    if (
        approval_context is not _PO_APPROVAL_CONTEXT
        or not isinstance(conn, sqlite3.Connection)
        or not conn.in_transaction
        or not isinstance(operation_id, str)
        or not operation_id.strip()
    ):
        raise PermissionError(
            "create_purchase_order requires an active approved Gateway transaction"
        )

    po_id = _required_text(po_id, "po_id")
    supplier_id = _required_text(supplier_id, "supplier_id")
    product_id = _required_text(product_id, "product_id")
    order_date = _required_text(order_date, "order_date")
    status = _required_text(status, "status")
    operation_id = operation_id.strip()

    executing_approval = conn.execute(
        """
        SELECT 1 FROM pending_approvals
        WHERE operation_id = ?
          AND tool_name = 'create_purchase_order'
          AND status = 'executing'
        LIMIT 1
        """,
        (operation_id,),
    ).fetchone()
    if executing_approval is None:
        raise PermissionError(
            "create_purchase_order requires a matching executing approval"
        )

    if isinstance(note, str):
        note = note.strip()
    elif note is None:
        note = ""
    else:
        raise ValueError("note must be a string")

    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise ValueError("qty must be a positive integer")
    if isinstance(unit_price, bool) or not isinstance(unit_price, (int, float)):
        raise ValueError("unit_price must be a non-negative finite number")
    unit_price = float(unit_price)
    if not math.isfinite(unit_price) or unit_price < 0:
        raise ValueError("unit_price must be a non-negative finite number")

    supplier = conn.execute(
        "SELECT is_official FROM suppliers WHERE supplier_id = ?",
        (supplier_id,),
    ).fetchone()
    if supplier is None:
        raise ValueError(f"unknown supplier_id: {supplier_id}")
    if supplier[0] != 1:
        raise PermissionError(f"supplier is not official: {supplier_id}")

    product = conn.execute(
        "SELECT 1 FROM inventory WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    if product is None:
        raise ValueError(f"unknown product_id: {product_id}")

    total_amount = qty * unit_price
    conn.execute(
        """
        INSERT INTO purchase_orders (
            po_id, supplier_id, order_date, status, total_amount, note,
            operation_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            po_id,
            supplier_id,
            order_date,
            status,
            total_amount,
            note,
            operation_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO purchase_order_items (
            po_id, product_id, qty, unit_price
        ) VALUES (?, ?, ?, ?)
        """,
        (po_id, product_id, qty, unit_price),
    )

    return {
        "po_id": po_id,
        "operation_id": operation_id,
        "supplier_id": supplier_id,
        "product_id": product_id,
        "qty": qty,
        "unit_price": unit_price,
        "total_amount": total_amount,
        "order_date": order_date,
        "status": status,
        "note": note,
    }


def get_payables() -> str:
    """查詢應付帳款：採購單待入庫或未結清金額總和與摘要。"""
    if not check_permission(["admin", "warehouse"]):
        return "權限不足：僅店長或倉管可查詢應付帳款。"
    res = run_query("SELECT COALESCE(SUM(total_amount),0) FROM purchase_orders WHERE status IN ('待入庫','已入庫')")
    total = res[0][0] if res else 0
    detail = run_query(
        "SELECT po_id, supplier_id, total_amount, status FROM purchase_orders ORDER BY order_date DESC LIMIT 20"
    )
    out = f"📊 應付帳款（採購單）：總計 **{total:,.0f}** 元\n\n近期採購單：\n"
    for r in detail or []:
        out += f"- 採購單 {r[0]} | 供應商 {r[1]} | 金額 {r[2]:,.0f} | 狀態 {r[3]}\n"
    return out


def get_suppliers_list() -> str:
    """查詢供應商列表：代號、名稱、聯絡人、電話。"""
    if not check_permission(["admin", "warehouse"]):
        return "權限不足：僅店長或倉管可查詢供應商資料。"
    res = run_query("SELECT supplier_id, name, contact, phone FROM suppliers")
    if not res:
        return "目前尚無供應商資料。"
    out = "📋 供應商列表：\n"
    for r in res:
        out += f"- {r[0]} | {r[1]} | 聯絡人 {r[2] or '-'} | {r[3] or '-'}\n"
    return out


def get_purchase_orders_summary(
    status: str = "",
    start_date: str = "",
    end_date: str = "",
    supplier_keyword: str = "",
    limit: int = 15,
) -> str:
    """查詢採購單摘要，可依狀態、日期區間、供應商關鍵字篩選。"""
    return get_purchase_orders_summary_filtered(
        status=status,
        start_date=start_date,
        end_date=end_date,
        supplier_keyword=supplier_keyword,
        limit=limit,
    )


def get_purchase_orders_summary_filtered(
    status: str = "",
    start_date: str = "",
    end_date: str = "",
    supplier_keyword: str = "",
    limit: int = 15,
) -> str:
    """
    查詢採購單摘要（支援條件）：
    - status: 狀態（例：草稿、待入庫、已入庫、已完成；留空或「全部」=不篩選）
    - start_date: 起始日期（YYYY-MM-DD）
    - end_date: 結束日期（YYYY-MM-DD）
    - supplier_keyword: 供應商代號或名稱關鍵字
    - limit: 顯示筆數上限（預設 15，最大 100）
    """
    if not check_permission(["admin", "warehouse"]):
        return "權限不足：僅店長或倉管可查詢採購單。"

    safe_limit = max(1, min(int(limit or 15), 100))
    where = []
    params = []

    status = (status or "").strip()
    start_date = (start_date or "").strip()
    end_date = (end_date or "").strip()
    supplier_keyword = (supplier_keyword or "").strip()

    if status and status != "全部":
        where.append("p.status = ?")
        params.append(status)
    if start_date:
        where.append("date(p.order_date) >= date(?)")
        params.append(start_date)
    if end_date:
        where.append("date(p.order_date) <= date(?)")
        params.append(end_date)
    if supplier_keyword:
        where.append("(p.supplier_id LIKE ? OR COALESCE(s.name,'') LIKE ?)")
        kw = f"%{supplier_keyword}%"
        params.extend([kw, kw])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    summary_sql = (
        "SELECT COUNT(*), COALESCE(SUM(p.total_amount),0) "
        "FROM purchase_orders p "
        "LEFT JOIN suppliers s ON p.supplier_id = s.supplier_id"
        f"{where_sql}"
    )
    res = run_query(summary_sql, tuple(params))
    cnt, total = (res[0][0], res[0][1]) if res else (0, 0)

    status_sql = (
        "SELECT COALESCE(p.status, '未設定') as st, COUNT(*) "
        "FROM purchase_orders p "
        "LEFT JOIN suppliers s ON p.supplier_id = s.supplier_id"
        f"{where_sql} "
        "GROUP BY COALESCE(p.status, '未設定') "
        "ORDER BY COUNT(*) DESC, st ASC"
    )
    status_rows = run_query(status_sql, tuple(params))

    rows_sql = (
        "SELECT p.po_id, p.supplier_id, COALESCE(s.name,''), p.total_amount, p.status, p.order_date "
        "FROM purchase_orders p "
        "LEFT JOIN suppliers s ON p.supplier_id = s.supplier_id"
        f"{where_sql} "
        "ORDER BY p.order_date DESC, p.po_id DESC LIMIT ?"
    )
    rows = run_query(rows_sql, tuple(params + [safe_limit]))

    cond_parts = []
    if status and status != "全部":
        cond_parts.append(f"狀態={status}")
    if start_date:
        cond_parts.append(f"起={start_date}")
    if end_date:
        cond_parts.append(f"迄={end_date}")
    if supplier_keyword:
        cond_parts.append(f"供應商關鍵字={supplier_keyword}")
    cond_text = "、".join(cond_parts) if cond_parts else "無（顯示全部）"

    status_text = "、".join([f"{s[0]} {s[1]}筆" for s in status_rows]) if status_rows else "無資料"

    out = (
        "📋 採購單摘要\n"
        f"查詢條件：{cond_text}\n"
        f"符合筆數：**{cnt}** 筆｜總金額：**{total:,.0f}** 元\n"
        f"狀態分布：{status_text}\n\n"
        f"最近 {safe_limit} 筆：\n"
    )
    if not rows:
        return out + "（查無符合條件的採購單）"

    for r in rows or []:
        supplier = f"{r[1]} {r[2]}".strip()
        out += f"- {r[0]} | 供應商 {supplier} | {r[3]:,.0f} 元 | {r[4]} | {r[5]}\n"
    return out
