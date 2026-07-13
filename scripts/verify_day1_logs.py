"""
scripts/verify_day1_logs.py
Verify database logging and pending approvals functionality.
"""

import sys
import os
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.tool_gateway import gateway
from backend.agent_logger import get_action_logs, get_pending_approvals, update_approval_status
from backend.database import run_query

def main():
    print("=== Start Verification ===")
    
    # 1. Clean existing test logs if any to have a clean test
    # (Just querying count first to compare later)
    initial_action_logs = get_action_logs()
    initial_pending_approvals = get_pending_approvals()
    print(f"Initial DB state - action logs count: {len(initial_action_logs)}, pending approvals count: {len(initial_pending_approvals)}")
    
    # 2. Call a read-only tool
    print("\nCalling a read-only tool: check_inventory...")
    res1 = gateway.call("check_inventory", {"product_id": "P001"}, role="warehouse")
    print(f"Result status: {res1.status}")
    
    # Verify action log is written
    action_logs = get_action_logs(limit=5)
    print("\nRecent action logs in DB:")
    for log in action_logs:
        print(f"- Tool: {log['tool_name']}, Caller: {log['caller']}, Success: {log['success']}, Timestamp: {log['timestamp']}")
        
    assert any(log['tool_name'] == "check_inventory" for log in action_logs), "Error: check_inventory log not found!"
    print("Verification Success: Read-only tool call is logged in DB!")
    
    # 3. Call a write tool (should be intercepted and create a pending approval)
    print("\nCalling a write tool: update_inventory...")
    res2 = gateway.call("update_inventory", {"product_id": "P001", "qty": 10}, role="warehouse")
    print(f"Result status: {res2.status}")
    print(f"Result message: {res2.message}")
    print(f"Approval ID: {res2.approval_id}")
    
    # Verify pending approval is written
    pending_approvals = get_pending_approvals()
    print("\nPending approvals in DB:")
    for app in pending_approvals:
        print(f"- Approval ID: {app['approval_id']}, Tool: {app['tool_name']}, Requester: {app['requester']}, Status: {app['status']}")
        
    assert any(app['approval_id'] == res2.approval_id for app in pending_approvals), "Error: Pending approval record not found!"
    print("Verification Success: Write tool call is intercepted and pending approval is saved in DB!")
    
    # 4. Test updating approval status
    print(f"\nUpdating approval status for ID: {res2.approval_id}...")
    update_approval_status(res2.approval_id, approver="admin", status="approved")
    
    # Verify updated status
    all_approvals = get_pending_approvals()
    target_approval = next((app for app in all_approvals if app['approval_id'] == res2.approval_id), None)
    assert target_approval is not None, "Error: Target approval not found!"
    print(f"Updated status: {target_approval['status']}, Approver: {target_approval['approver']}, Updated At: {target_approval['updated_at']}")
    assert target_approval['status'] == "approved", "Error: Approval status was not updated!"
    assert target_approval['approver'] == "admin", "Error: Approver was not updated!"
    
    print("\nVerification Success: Approval status update works correctly!")
    print("\n=== Verification Completed Successfully ===")

if __name__ == "__main__":
    main()
