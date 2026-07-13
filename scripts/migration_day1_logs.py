"""
scripts/migration_day1_logs.py
Migration script to create agent_action_logs and pending_approvals tables.
"""

import sys
import os
import sqlite3

# Add parent directory to sys.path to allow backend imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import DB_FILE, init_db

def run_migration():
    print(f"Starting DB migration on: {DB_FILE}")
    
    # Initialize the database using the updated init_db
    init_db()
    
    # Verify the tables exist
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    tables_to_check = ["agent_action_logs", "pending_approvals"]
    missing_tables = []
    
    for table in tables_to_check:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        result = cursor.fetchone()
        if result:
            print(f"Verified: Table '{table}' exists.")
        else:
            missing_tables.append(table)
            
    conn.close()
    
    if missing_tables:
        print(f"Error: Migration failed to create tables: {missing_tables}", file=sys.stderr)
        sys.exit(1)
    else:
        print("Migration complete. All tables created and verified successfully.")
        sys.exit(0)

if __name__ == "__main__":
    run_migration()
