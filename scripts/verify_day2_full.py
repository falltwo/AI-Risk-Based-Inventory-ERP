"""
scripts/verify_day2_full.py
Verify Day 2 Complete Approval & Rejection Flow with reason and query functions.
"""

import sys
import os
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.tool_gateway import gateway
from backend.agent_logger import (
    get_pending_list,
    get_action_logs,
    approve_action,
    reject_action,
    get_pending_approval_by_id
)

def get_product_stock(product_id: str) -> int:
    from backend.database import run_query
    rows = run_query("SELECT stock FROM inventory WHERE product_id = ?", (product_id,))
    return rows[0][0] if rows else 0

def main():
    print("=== Start Day 2 Full Verification ===")
    
    product_id = "P001"
    initial_stock = get_product_stock(product_id)
    print(f"Initial stock for {product_id}: {initial_stock}")
    
    # -------------------------------------------------------------
    # 測試 1: 建立待審批，並驗證 get_pending_list() 取得的資料
    # -------------------------------------------------------------
    print("\n[測試 1] 呼叫寫入操作 (quantity_change=10)...")
    res1 = gateway.call("update_inventory", {"product_id": product_id, "quantity_change": 10}, role="warehouse")
    assert res1.status == "pending", "Error: Action should be pending!"
    approval_id1 = res1.approval_id
    print(f"待審批單建立成功，ID: {approval_id1}")
    
    # 用 get_pending_list() 查詢
    pending_list = get_pending_list()
    target_pending = next((item for item in pending_list if item["id"] == approval_id1), None)
    assert target_pending is not None, "Error: approval_id not found in get_pending_list!"
    print("get_pending_list 查詢結果:")
    print(f" - ID: {target_pending['id']}")
    print(f" - Time: {target_pending['time']}")
    print(f" - Agent: {target_pending['agent']}")
    print(f" - Tool: {target_pending['tool']}")
    print(f" - Args: {target_pending['args']}")
    print(f" - Role: {target_pending['role']}")
    print(f" - Risk: {target_pending['risk']}")
    
    # -------------------------------------------------------------
    # 測試 2: 核准操作
    # -------------------------------------------------------------
    print(f"\n[測試 2] 呼叫 approve_action({approval_id1})...")
    app_res = approve_action(approval_id1)
    print(f"核准結果: {app_res}")
    assert app_res["status"] == "ok", "Error: approve_action failed!"
    
    # 確認庫存已更新
    stock_after_app = get_product_stock(product_id)
    print(f"核准後庫存: {stock_after_app} (預期增加 10: {initial_stock + 10})")
    assert stock_after_app == initial_stock + 10, "Error: Stock did not update!"
    
    # -------------------------------------------------------------
    # 測試 3: 建立第二筆，並拒絕且記錄原因
    # -------------------------------------------------------------
    time.sleep(0.1) # 避免時間戳記重複
    print("\n[測試 3] 呼叫第二個寫入操作 (quantity_change=20)...")
    res2 = gateway.call("update_inventory", {"product_id": product_id, "quantity_change": 20}, role="warehouse")
    assert res2.status == "pending", "Error: Action should be pending!"
    approval_id2 = res2.approval_id
    print(f"第二個待審批單建立成功，ID: {approval_id2}")
    
    # 呼叫拒絕，寫入原因
    reject_reason = "不符當前採購預算"
    print(f"呼叫 reject_action({approval_id2}, '{reject_reason}')...")
    rej_res = reject_action(approval_id2, reason=reject_reason)
    print(f"拒絕結果: {rej_res}")
    assert rej_res["status"] == "denied", "Error: reject_action failed!"
    
    # 驗證資料庫中的拒絕原因與狀態
    app_record = get_pending_approval_by_id(approval_id2)
    print(f"資料庫紀錄驗證 - 狀態: {app_record['status']}, 拒絕原因: {app_record['reason']}")
    assert app_record["status"] == "rejected", "Error: status not updated to rejected!"
    assert app_record["reason"] == reject_reason, "Error: reject reason mismatch!"
    
    # -------------------------------------------------------------
    # 測試 4: 查詢工具呼叫日誌
    # -------------------------------------------------------------
    print("\n[測試 4] 呼叫 get_action_logs(10) 查詢日誌...")
    logs = get_action_logs(10)
    print("近期日誌前 3 筆:")
    for log in logs[:3]:
        print(f" - 時間: {log['timestamp']} | 工具: {log['tool_name']} | 結果: {log['result'][:80]}")
        
    # 確保拒絕的日誌有包含原因
    reject_log = next((log for log in logs if approval_id2 in str(log['result']) or reject_reason in str(log['result'])), None)
    assert reject_log is not None, "Error: Rejection log not found in action logs!"
    print(f"驗證作廢日誌內容: {reject_log['result']}")
    
    print("\n=== Day 2 完整審批核准/拒絕流程與查詢 API 驗證完全成功！ ===")

if __name__ == "__main__":
    main()
