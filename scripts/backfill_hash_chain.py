"""
scripts/backfill_hash_chain.py
為 migration 前已存在的舊列補算 checksum，避免 verify 時誤報篡改。

使用方式：
    python scripts/backfill_hash_chain.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import init_db, transaction, tx_run, run_query
from backend.log_checksum import compute_checksum, GENESIS_HASH

TABLES = {
    "agent_action_logs": {
        "id_col": "id",
        "columns": ("tool_name", "parameters", "caller", "result", "success", "timestamp"),
    },
    "pending_approvals": {
        "id_col": "approval_id",
        "columns": ("tool_name", "parameters", "requester", "status", "approver", "created_at", "updated_at", "reason"),
    },
    "agent_dispatch_logs": {
        "id_col": "id",
        "columns": ("task", "task_type", "primary_agent", "agent_chain", "routed_by", "needs_approval", "reason", "caller", "timestamp"),
    },
}


def backfill_table(table: str, id_col: str, columns: tuple):
    """為指定表中 checksum 為 NULL 的舊列依序補算 hash chain。"""
    rows = run_query(
        f"SELECT {id_col}, {', '.join(columns)}, checksum FROM {table} ORDER BY {id_col} ASC"
    )
    if not rows:
        return 0

    null_count = sum(1 for r in rows if r[-1] is None)
    if null_count == 0:
        return 0

    with transaction() as conn:
        prev = GENESIS_HASH
        updated = 0
        for row in rows:
            row_id = row[0]
            data_fields = row[1:-1]  # 欄位值（不包含 id 與 checksum）
            stored_checksum = row[-1]
            if stored_checksum is not None:
                prev = stored_checksum
                continue
            new_checksum = compute_checksum(prev, *data_fields)
            tx_run(conn,
                f"UPDATE {table} SET checksum = ? WHERE {id_col} = ?",
                (new_checksum, row_id), fetch=False)
            prev = new_checksum
            updated += 1
        return updated


def main():
    init_db()
    for table, meta in TABLES.items():
        # dispatch 表可能還沒被 create
        try:
            n = backfill_table(table, meta["id_col"], meta["columns"])
            if n > 0:
                print(f"  {table}: backfilled {n} rows ✅")
            else:
                print(f"  {table}: no legacy rows, skipped")
        except Exception as e:
            print(f"  {table}: error — {e}")

    from backend.log_checksum import verify_agent_action_logs, verify_pending_approvals, verify_agent_dispatch_logs

    print("\n=== 驗證三表雜湊鏈 ===")
    for name, fn in [
        ("agent_action_logs", verify_agent_action_logs),
        ("pending_approvals", verify_pending_approvals),
        ("agent_dispatch_logs", verify_agent_dispatch_logs),
    ]:
        try:
            r = fn()
            print(f"  {name}: valid={r['valid']}, tampered={r['tampered_rows']}, legacy={r.get('legacy_rows', 0)}")
        except Exception as e:
            print(f"  {name}: error — {e}")


if __name__ == "__main__":
    main()
