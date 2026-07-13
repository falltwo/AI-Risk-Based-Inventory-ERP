"""
scripts/verify_rollback.py
Verify that approval retry correctly reverses database changes (rollback / compensating transaction).
"""

import sys
import os
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.tool_gateway import gateway
from backend.agent_logger import (
    get_pending_list,
    approve_action,
    update_approval_status,
    get_pending_approval_by_id,
    get_action_logs,
    write_action_log,
)
from backend.database import run_query

def get_product_stock(product_id: str) -> int:
    rows = run_query("SELECT stock FROM inventory WHERE product_id = ?", (product_id,))
    return rows[0][0] if rows else 0

def get_order_count(cust_id: str | None, pid: str, qty: int) -> int:
    if cust_id is None:
        rows = run_query(
            "SELECT COUNT(*) FROM orders WHERE customer_id IS NULL AND product_id = ? AND quantity = ?",
            (pid, qty)
        )
    else:
        rows = run_query(
            "SELECT COUNT(*) FROM orders WHERE customer_id = ? AND product_id = ? AND quantity = ?",
            (cust_id, pid, qty)
        )
    return rows[0][0]

def main():
    print("=== Start Rollback / Compensating Transaction Verification ===")
    
    # -------------------------------------------------------------
    # 測試 1: 庫存更新 (update_inventory) 的審批與重試沖銷
    # -------------------------------------------------------------
    product_id = "P001"
    initial_stock = get_product_stock(product_id)
    print(f"\n[庫存測試] 初始庫存: {initial_stock}")
    
    print("1. 呼叫進貨庫存異動 (quantity_change=15)...")
    res1 = gateway.call("update_inventory", {"product_id": product_id, "quantity_change": 15}, role="warehouse")
    approval_id1 = res1.approval_id
    print(f"   待審批單 ID: {approval_id1}")
    
    print("2. 核准該進貨單...")
    app_res1 = approve_action(approval_id1)
    stock_after_app = get_product_stock(product_id)
    print(f"   核准後庫存: {stock_after_app} (預期: {initial_stock + 15})")
    assert stock_after_app == initial_stock + 15, "Error: Stock failed to update!"
    
    print("3. 觸發重試 (模擬點擊 🔄 重試 / 重新審批，執行反向沖銷)...")
    # 記錄沖銷前的 action_logs 筆數
    log_count_before = len(get_action_logs(limit=9999))

    # 模擬 frontend/page_agent_dashboard.py 中的重試沖銷邏輯
    app_record = get_pending_approval_by_id(approval_id1)
    assert app_record is not None, "Error: Approval record not found"
    
    if app_record["status"] == "approved" and app_record["tool_name"] == "update_inventory":
        params = app_record["parameters"]
        pid = params.get("product_id")
        qty_change = params.get("quantity_change")
        if pid and qty_change is not None:
            qty_change = float(qty_change)
            res = gateway.execute_approved("rollback_inventory", {"product_id": pid, "quantity_change": qty_change}, "admin")
            print(f"   沖銷動作執行結果: {res}")
            assert res.is_ok(), f"Rollback failed: {res.message}"

    # 記一筆重試 log（對齊 dashboard 行為）
    write_action_log("retry_approval", {"approval_id": approval_id1}, "admin", f"重試審批 {approval_id1}，已沖銷並重置為 pending", True)
            
    # 更新為 pending
    update_approval_status(approval_id1, approver=None, status="pending", reason=None)
    
    stock_after_retry = get_product_stock(product_id)
    print(f"   重試/沖銷後庫存: {stock_after_retry} (預期回復為: {initial_stock})")
    assert stock_after_retry == initial_stock, "Error: Stock failed to roll back!"
    
    # 檢查是否重新回到待審批清單
    pending_list = get_pending_list()
    assert any(item["id"] == approval_id1 for item in pending_list), "Error: Approval is not pending again!"
    print("   已成功重新回到待審批清單中！")

    # 斷言 action_logs 多了沖銷紀錄
    log_count_after = len(get_action_logs(limit=9999))
    assert log_count_after >= log_count_before + 2, f"Error: Expected at least 2 new action logs (rollback + retry), got {log_count_after - log_count_before}"
    print(f"   action_logs 增加 {log_count_after - log_count_before} 筆（沖銷 + 重試）✅")

    # -------------------------------------------------------------
    # 測試 2: 建立銷售單 (create_order) 的審批與重試沖銷
    # -------------------------------------------------------------
    cust_id = None  # 在資料庫中 create_order 的 customer_id 為 None (NULL)
    order_pid = "P003"
    order_qty = 8
    
    initial_stock_p3 = get_product_stock(order_pid)
    initial_orders = get_order_count(cust_id, order_pid, order_qty)
    print(f"\n[訂單測試] 初始訂單數量 (匹配條件): {initial_orders} | 初始 P003 庫存: {initial_stock_p3}")
    
    print("1. 呼叫建立銷售訂單...")
    res2 = gateway.call("create_order", {"customer_id": "C001", "product_id": order_pid, "quantity": order_qty}, role="sales")
    approval_id2 = res2.approval_id
    print(f"   待審批單 ID: {approval_id2}")
    
    print("2. 核准該訂單...")
    app_res2 = approve_action(approval_id2)
    orders_after_app = get_order_count(cust_id, order_pid, order_qty)
    stock_after_app_p3 = get_product_stock(order_pid)
    print(f"   核准後訂單數: {orders_after_app} (預期增加 1: {initial_orders + 1})")
    print(f"   核准後 P003 庫存: {stock_after_app_p3} (預期減少 {order_qty}: {initial_stock_p3 - order_qty})")
    assert orders_after_app == initial_orders + 1, "Error: Order failed to create!"
    assert stock_after_app_p3 == initial_stock_p3 - order_qty, "Error: Stock not deducted!"
    
    print("3. 觸發重試 (模擬點擊 🔄 重試 / 重新審批，刪除新增的訂單並回補庫存)...")
    app_record2 = get_pending_approval_by_id(approval_id2)
    assert app_record2 is not None, "Error: Approval record not found"
    
    if app_record2["status"] == "approved" and app_record2["tool_name"] == "create_order":
        params = app_record2["parameters"]
        c_id = params.get("customer_id")
        p_id = params.get("product_id")
        qty = params.get("quantity")
        if p_id and qty is not None:
            cancel_args = {"product_id": p_id, "quantity": int(qty)}
            if c_id:
                cancel_args["customer_id"] = c_id
            res = gateway.execute_approved("cancel_order", cancel_args, "admin")
            print(f"   沖銷動作執行結果: {res}")
            assert res.is_ok(), f"Cancel order failed: {res.message}"

    # 記一筆重試 log
    write_action_log("retry_approval", {"approval_id": approval_id2}, "admin", f"重試審批 {approval_id2}，已沖銷並重置為 pending", True)
            
    # 更新為 pending
    update_approval_status(approval_id2, approver=None, status="pending", reason=None)
    
    orders_after_retry = get_order_count(cust_id, order_pid, order_qty)
    stock_after_retry_p3 = get_product_stock(order_pid)
    print(f"   重試/沖銷後訂單數: {orders_after_retry} (預期回復為: {initial_orders})")
    print(f"   重試/沖銷後 P003 庫存: {stock_after_retry_p3} (預期回復為: {initial_stock_p3})")
    assert orders_after_retry == initial_orders, "Error: Order failed to roll back!"
    assert stock_after_retry_p3 == initial_stock_p3, "Error: Stock failed to roll back!"
    
    print("\n=== 沖銷/還原與重試流程驗證完全成功！ ===")

if __name__ == "__main__":
    main()
