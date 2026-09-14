"""Exactly-once compensation of a recorded approval, inside one SQLite transaction."""
import json
import math
import re
from datetime import datetime
from . import database
from .access_control import GLOBAL_APPROVAL_DECIDE, require_capability


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS approval_reversals (
        approval_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL,
        actor TEXT NOT NULL, created_at TEXT NOT NULL, result TEXT NOT NULL)""")


def reverse_approval(approval_id, *, actor):
    from .agent_logger import get_reversal_record, write_action_log
    # BEGIN IMMEDIATE serializes readers/compensators across processes. The
    # receipt, stock/order changes, stock move and audit share this commit.
    with database.transaction(immediate=True) as conn:
        principal = require_capability(actor, GLOBAL_APPROVAL_DECIDE, conn=conn)
        if principal.role != "admin":
            raise PermissionError("僅管理員可沖銷")
        existing = get_reversal_record(approval_id, conn=conn)
        if existing:
            return dict(status="already_reversed", message=existing["result"])
        row = conn.execute("""SELECT p.tool_name,p.parameters,p.status,r.result
            FROM pending_approvals p LEFT JOIN effect_receipts r ON r.approval_id=p.approval_id
            WHERE p.approval_id=?""", (approval_id,)).fetchone()
        if not row or row[2] != "approved" or row[0] not in {"update_inventory", "create_order"}:
            raise ValueError("審批未成功或不支援沖銷")
        if row[3] is None:
            raise ValueError("舊審批缺少執行收據，需人工對帳後處理")
        args = json.loads(row[1])
        product_id = args.get("product_id")
        value = args.get("quantity_change") if row[0] == "update_inventory" else args.get("quantity")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value == 0:
            raise ValueError("原始審批數量無效")
        delta = -value if row[0] == "update_inventory" else value
        if row[0] == "create_order":
            ids = set(re.findall(r"ORD-\d{8}-\d{6}", row[3]))
            if len(ids) != 1 or value <= 0:
                raise ValueError("執行收據無法唯一識別訂單，需人工對帳")
            order_id = ids.pop()
            order = conn.execute("SELECT product_id,quantity,status FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if not order or order[0] != product_id or order[1] != value or order[2] != "處理中":
                raise ValueError("訂單狀態或內容已異動，需人工對帳")
            conn.execute("UPDATE orders SET status='已取消' WHERE order_id=?", (order_id,))
        stock = conn.execute("SELECT stock,warehouse_id FROM inventory WHERE product_id=?", (product_id,)).fetchone()
        if not stock or stock[0] + delta < 0:
            raise ValueError("品項不存在或沖銷後庫存不足")
        conn.execute("UPDATE inventory SET stock=stock+? WHERE product_id=?", (delta,product_id))
        now = datetime.now().isoformat()
        conn.execute("INSERT INTO stock_moves(product_id,warehouse_id,qty,move_type,ref_no,move_date,note) VALUES(?,?,?,?,?,?,?)",
                     (product_id, stock[1] or "WH01", abs(delta), "入庫" if delta>0 else "出庫", approval_id, now, "核准紀錄沖銷"))
        message = f"已沖銷 {approval_id}；{product_id} 庫存異動 {delta:+g}。"
        conn.execute("INSERT INTO approval_reversals VALUES(?,?,?,?,?)", (approval_id,row[0],actor,now,message))
        write_action_log("retry_approval", {"approval_id":approval_id}, actor, message, True, conn=conn)
        return dict(status="ok", message=message)
