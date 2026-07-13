# scripts/create_views.py
import sqlite3
import os
import sys

# Add project root to sys.path so we can import backend
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import DB_FILE

def main():
    print(f"Connecting to database: {DB_FILE}")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Read the SQL file
    sql_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics_view.sql")
    with open(sql_path, "r", encoding="utf-8") as f:
        sql = f.read()
        
    # Execute script
    c.executescript(sql)
    conn.commit()
    conn.close()
    print("Metrics views created successfully!")

if __name__ == '__main__':
    main()
