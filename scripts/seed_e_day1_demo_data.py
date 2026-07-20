from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT_DIR / "data" / "erp.db"


LOW_STOCK_PRODUCTS = [
    {
        "product_id": "P001",
        "name": "筆記型電腦",
        "price": 45000,
        "cost": 35000,
        "stock": 12,
        "reorder_point": 60,
        "daily_sales": 8,
        "baseline_reorder_point": 60,
    },
    {
        "product_id": "P004",
        "name": "電腦螢幕",
        "price": 6000,
        "cost": 4500,
        "stock": 8,
        "reorder_point": 60,
        "daily_sales": 10,
        "baseline_reorder_point": 60,
    },
    {
        "product_id": "P019",
        "name": "USB-C 控制模組",
        "price": 1200,
        "cost": 690,
        "stock": 15,
        "reorder_point": 96,
        "daily_sales": 14,
        "baseline_reorder_point": 96,
    },
]


DEMO_CUSTOMER = {
    "customer_id": "C-E-DEMO",
    "name": "ERP Demo Customer",
    "company": "Demo Operations Team",
    "phone": "02-2999-0602",
    "email": "demo-customer@example.com",
    "contact": "E Day1 QA",
    "country": "台灣",
    "region": "亞洲",
    "latitude": 25.0330,
    "longitude": 121.5654,
    "risk_level": "低",
}


DEMO_SUPPLIERS = [
    {
        "supplier_id": "SUP-E-DEMO-JP",
        "name": "E Demo Tokyo Components",
        "contact": "Yuki Tanaka",
        "phone": "+81-03-0000-0602",
        "email": "tokyo-demo@example.com",
        "country": "日本",
        "region": "亞洲",
        "latitude": 35.6895,
        "longitude": 139.6917,
        "risk_level": "高",
        "is_official": 1,
    },
    {
        "supplier_id": "SUP-E-DEMO-TW",
        "name": "E Demo Taipei Backup Supply",
        "contact": "Chen Demo",
        "phone": "02-2602-0001",
        "email": "taipei-backup@example.com",
        "country": "台灣",
        "region": "亞洲",
        "latitude": 25.0330,
        "longitude": 121.5654,
        "risk_level": "低",
        "is_official": 1,
    },
]


DEMO_PURCHASE_ORDER = {
    "po_id": "PO-E-DAY1-JP-001",
    "supplier_id": "SUP-E-DEMO-JP",
    "status": "待入庫",
    "total_amount": 382000,
    "note": "E Day1 demo: inbound components delayed by Japan risk event.",
    "estimated_delay_days": 12,
    "alternative_suggestion": "Use SUP-E-DEMO-TW as a backup source and raise safety stock before approval.",
    "items": [
        {"product_id": "P019", "qty": 240, "unit_price": 720},
        {"product_id": "P004", "qty": 80, "unit_price": 2620},
    ],
}


DEMO_ORDERS = [
    {
        "order_id": "ORD-E-DAY1-JP-001",
        "customer_id": "C-E-DEMO",
        "product_id": "P019",
        "quantity": 40,
        "status": "待出貨",
        "total_amount": 11960,
    },
    {
        "order_id": "ORD-E-DAY1-JP-002",
        "customer_id": "C-E-DEMO",
        "product_id": "P004",
        "quantity": 12,
        "status": "已下單",
        "total_amount": 72000,
    },
]


DEMO_NEWS = {
    "country": "日本",
    "region": "亞洲",
    "title": "E Demo: Tokyo port congestion delays component shipments",
    "summary": "Demo scenario: a Tokyo port delay may affect display and Type-C component replenishment.",
    "url": "https://example.com/demo/e-day1-tokyo-port-delay",
    "source": "E Day1 demo seed",
    "relevance_tag": "demo_seed",
    "category": "港口壅塞",
    "is_relevant": 1,
    "estimated_delay": 12,
}


DEMO_EVENT = {
    "event_type": "港口壅塞",
    "region": "亞洲",
    "country": "日本",
    "impact_days": 12,
    "description": "E Day1 Demo: Tokyo port congestion delays inbound display and Type-C shipments. This should trigger affected order and stockout checks.",
}


DEMO_HEATMAP = {
    "region_key": "日本|亞洲",
    "display_name": "日本 亞洲",
    "latitude": 35.6895,
    "longitude": 139.6917,
    "risk_pct": 85.0,
    "ai_summary": "E Day1 Demo: Tokyo port congestion creates a high-risk inbound logistics scenario.",
}


DEMO_LINE_LOGS = [
    {
        "user_id": "E_DAY1_DEMO_USER",
        "user_name": "E Demo Manager",
        "user_msg": "請給我低庫存與 AI 補貨建議",
        "ai_reply": "Demo data ready: P001, P004, and P019 are below safety stock and should appear in low-stock flows.",
    },
    {
        "user_id": "E_DAY1_DEMO_USER",
        "user_name": "E Demo Manager",
        "user_msg": "日本供應鏈風險會影響哪些訂單?",
        "ai_reply": "Demo data ready: PO-E-DAY1-JP-001 and ORD-E-DAY1-JP-001/002 are linked to the Japan risk scenario.",
    },
]


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def seed_low_stock(conn: sqlite3.Connection) -> None:
    for item in LOW_STOCK_PRODUCTS:
        conn.execute(
            """
            INSERT INTO inventory (
                product_id, name, stock, price, cost, reorder_point,
                baseline_reorder_point, daily_sales, barcode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                stock = excluded.stock,
                reorder_point = excluded.reorder_point,
                daily_sales = excluded.daily_sales,
                baseline_reorder_point = COALESCE(
                    inventory.baseline_reorder_point,
                    excluded.baseline_reorder_point
                )
            """,
            (
                item["product_id"],
                item["name"],
                item["stock"],
                item["price"],
                item["cost"],
                item["reorder_point"],
                item["baseline_reorder_point"],
                item["daily_sales"],
                f"E-DAY1-{item['product_id']}",
            ),
        )


def seed_customer(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO customers (
            customer_id, name, company, phone, email, contact,
            country, region, latitude, longitude, risk_level
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(customer_id) DO UPDATE SET
            name = excluded.name,
            company = excluded.company,
            phone = excluded.phone,
            email = excluded.email,
            contact = excluded.contact,
            country = excluded.country,
            region = excluded.region,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            risk_level = excluded.risk_level
        """,
        (
            DEMO_CUSTOMER["customer_id"],
            DEMO_CUSTOMER["name"],
            DEMO_CUSTOMER["company"],
            DEMO_CUSTOMER["phone"],
            DEMO_CUSTOMER["email"],
            DEMO_CUSTOMER["contact"],
            DEMO_CUSTOMER["country"],
            DEMO_CUSTOMER["region"],
            DEMO_CUSTOMER["latitude"],
            DEMO_CUSTOMER["longitude"],
            DEMO_CUSTOMER["risk_level"],
        ),
    )


def seed_suppliers(conn: sqlite3.Connection) -> None:
    for supplier in DEMO_SUPPLIERS:
        conn.execute(
            """
            INSERT INTO suppliers (
                supplier_id, name, contact, phone, email, country, region,
                latitude, longitude, risk_level, is_official
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(supplier_id) DO UPDATE SET
                name = excluded.name,
                contact = excluded.contact,
                phone = excluded.phone,
                email = excluded.email,
                country = excluded.country,
                region = excluded.region,
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                risk_level = excluded.risk_level,
                is_official = excluded.is_official
            """,
            (
                supplier["supplier_id"],
                supplier["name"],
                supplier["contact"],
                supplier["phone"],
                supplier["email"],
                supplier["country"],
                supplier["region"],
                supplier["latitude"],
                supplier["longitude"],
                supplier["risk_level"],
                supplier["is_official"],
            ),
        )

    conn.execute("DELETE FROM supplier_products WHERE supplier_id LIKE 'SUP-E-DEMO-%'")
    for supplier in DEMO_SUPPLIERS:
        for product_id, price, carbon_factor in [
            ("P019", 690, 1.8 if supplier["country"] == "台灣" else 3.2),
            ("P004", 2500, 4.1 if supplier["country"] == "台灣" else 6.8),
        ]:
            conn.execute(
                """
                INSERT INTO supplier_products (supplier_id, product_id, price, carbon_factor)
                VALUES (?, ?, ?, ?)
                """,
                (supplier["supplier_id"], product_id, price, carbon_factor),
            )


def seed_purchase_order(conn: sqlite3.Connection, today: datetime) -> None:
    po = DEMO_PURCHASE_ORDER
    order_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    conn.execute(
        """
        INSERT INTO purchase_orders (
            po_id, supplier_id, order_date, status, total_amount,
            note, estimated_delay_days, alternative_suggestion
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(po_id) DO UPDATE SET
            supplier_id = excluded.supplier_id,
            order_date = excluded.order_date,
            status = excluded.status,
            total_amount = excluded.total_amount,
            note = excluded.note,
            estimated_delay_days = excluded.estimated_delay_days,
            alternative_suggestion = excluded.alternative_suggestion
        """,
        (
            po["po_id"],
            po["supplier_id"],
            order_date,
            po["status"],
            po["total_amount"],
            po["note"],
            po["estimated_delay_days"],
            po["alternative_suggestion"],
        ),
    )

    for item in po["items"]:
        existing_items = conn.execute(
            "SELECT id FROM purchase_order_items "
            "WHERE po_id = ? AND product_id = ? ORDER BY id",
            (po["po_id"], item["product_id"]),
        ).fetchall()
        if len(existing_items) > 1:
            raise RuntimeError(
                "Demo purchase order has duplicate product lines; "
                "refusing to replace durable source evidence."
            )
        if existing_items:
            conn.execute(
                "UPDATE purchase_order_items SET qty = ?, unit_price = ? WHERE id = ?",
                (item["qty"], item["unit_price"], existing_items[0][0]),
            )
        else:
            conn.execute(
                """
                INSERT INTO purchase_order_items (po_id, product_id, qty, unit_price)
                VALUES (?, ?, ?, ?)
                """,
                (po["po_id"], item["product_id"], item["qty"], item["unit_price"]),
            )


def seed_orders(conn: sqlite3.Connection, today: datetime) -> None:
    order_date = today.strftime("%Y-%m-%d")
    for order in DEMO_ORDERS:
        conn.execute(
            """
            INSERT INTO orders (
                order_id, customer_id, product_id, quantity,
                status, order_date, total_amount
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                customer_id = excluded.customer_id,
                product_id = excluded.product_id,
                quantity = excluded.quantity,
                status = excluded.status,
                order_date = excluded.order_date,
                total_amount = excluded.total_amount
            """,
            (
                order["order_id"],
                order["customer_id"],
                order["product_id"],
                order["quantity"],
                order["status"],
                order_date,
                order["total_amount"],
            ),
        )


def seed_news_and_event(conn: sqlite3.Connection, now: datetime) -> None:
    published_at = now.strftime("%Y-%m-%d %H:%M")
    fetched_at = published_at

    existing = conn.execute(
        "SELECT id FROM supply_chain_news WHERE url = ?",
        (DEMO_NEWS["url"],),
    ).fetchone()

    if existing:
        news_id = existing[0]
        conn.execute(
            """
            UPDATE supply_chain_news
               SET country = ?,
                   region = ?,
                   title = ?,
                   summary = ?,
                   source = ?,
                   published_at = ?,
                   relevance_tag = ?,
                   fetched_at = ?,
                   category = ?,
                   is_relevant = ?,
                   estimated_delay = ?
             WHERE id = ?
            """,
            (
                DEMO_NEWS["country"],
                DEMO_NEWS["region"],
                DEMO_NEWS["title"],
                DEMO_NEWS["summary"],
                DEMO_NEWS["source"],
                published_at,
                DEMO_NEWS["relevance_tag"],
                fetched_at,
                DEMO_NEWS["category"],
                DEMO_NEWS["is_relevant"],
                DEMO_NEWS["estimated_delay"],
                news_id,
            ),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO supply_chain_news (
                country, region, title, summary, url, source, published_at,
                relevance_tag, fetched_at, category, is_relevant, estimated_delay
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                DEMO_NEWS["country"],
                DEMO_NEWS["region"],
                DEMO_NEWS["title"],
                DEMO_NEWS["summary"],
                DEMO_NEWS["url"],
                DEMO_NEWS["source"],
                published_at,
                DEMO_NEWS["relevance_tag"],
                fetched_at,
                DEMO_NEWS["category"],
                DEMO_NEWS["is_relevant"],
                DEMO_NEWS["estimated_delay"],
            ),
        )
        news_id = cur.lastrowid

    existing_event = conn.execute(
        "SELECT id FROM supply_chain_events WHERE news_id = ?",
        (news_id,),
    ).fetchone()
    created_at = now.strftime("%Y-%m-%d %H:%M")
    if existing_event:
        conn.execute(
            """
            UPDATE supply_chain_events
               SET event_type = ?,
                   region = ?,
                   country = ?,
                   impact_days = ?,
                   description = ?,
                   created_at = ?
             WHERE id = ?
            """,
            (
                DEMO_EVENT["event_type"],
                DEMO_EVENT["region"],
                DEMO_EVENT["country"],
                DEMO_EVENT["impact_days"],
                DEMO_EVENT["description"],
                created_at,
                existing_event[0],
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO supply_chain_events (
                event_type, region, country, impact_days,
                description, created_at, news_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                DEMO_EVENT["event_type"],
                DEMO_EVENT["region"],
                DEMO_EVENT["country"],
                DEMO_EVENT["impact_days"],
                DEMO_EVENT["description"],
                created_at,
                news_id,
            ),
        )

    conn.execute(
        """
        INSERT INTO risk_heatmap (
            region_key, display_name, latitude, longitude,
            risk_pct, ai_summary, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(region_key) DO UPDATE SET
            display_name = excluded.display_name,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            risk_pct = excluded.risk_pct,
            ai_summary = excluded.ai_summary,
            updated_at = excluded.updated_at
        """,
        (
            DEMO_HEATMAP["region_key"],
            DEMO_HEATMAP["display_name"],
            DEMO_HEATMAP["latitude"],
            DEMO_HEATMAP["longitude"],
            DEMO_HEATMAP["risk_pct"],
            DEMO_HEATMAP["ai_summary"],
            created_at,
        ),
    )


def seed_line_logs(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute(
        "DELETE FROM line_bot_logs WHERE user_id = ?",
        ("E_DAY1_DEMO_USER",),
    )
    for index, log in enumerate(DEMO_LINE_LOGS):
        created_at = (now + timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            INSERT INTO line_bot_logs (
                user_id, user_name, user_msg, ai_reply, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                log["user_id"],
                log["user_name"],
                log["user_msg"],
                log["ai_reply"],
                created_at,
            ),
        )


def validate_expected_schema(conn: sqlite3.Connection) -> None:
    required_columns = {
        "inventory": ["product_id", "stock", "reorder_point", "daily_sales", "baseline_reorder_point"],
        "customers": [
            "customer_id", "name", "company", "contact", "country", "region",
            "latitude", "longitude", "risk_level",
        ],
        "suppliers": ["supplier_id", "country", "region", "latitude", "longitude", "risk_level", "is_official"],
        "purchase_orders": ["po_id", "supplier_id", "status", "estimated_delay_days", "alternative_suggestion"],
        "purchase_order_items": ["po_id", "product_id", "qty", "unit_price"],
        "orders": ["order_id", "customer_id", "product_id", "status"],
        "supply_chain_news": ["id", "url", "estimated_delay"],
        "supply_chain_events": ["id", "event_type", "region", "country", "impact_days", "news_id"],
        "risk_heatmap": ["region_key", "risk_pct", "ai_summary"],
        "line_bot_logs": ["user_id", "user_msg", "ai_reply"],
    }

    missing = []
    for table, columns in required_columns.items():
        for column in columns:
            if not column_exists(conn, table, column):
                missing.append(f"{table}.{column}")

    if missing:
        raise RuntimeError("Missing expected database columns: " + ", ".join(missing))


def print_summary(conn: sqlite3.Connection) -> None:
    print("\nE Day1 demo seed summary")
    print("------------------------")

    low_stock_rows = conn.execute(
        """
        SELECT product_id, name, stock, reorder_point, daily_sales
          FROM inventory
         WHERE product_id IN ('P001', 'P004', 'P019')
         ORDER BY product_id
        """
    ).fetchall()
    print("Low-stock products:")
    for row in low_stock_rows:
        print(f"- {row[0]} {row[1]}: stock={row[2]}, reorder_point={row[3]}, daily_sales={row[4]}")

    event_row = conn.execute(
        """
        SELECT id, event_type, region, country, impact_days
          FROM supply_chain_events
         WHERE country = '日本' AND region = '亞洲'
         ORDER BY id DESC
         LIMIT 1
        """
    ).fetchone()
    if event_row:
        print(f"Risk event: #{event_row[0]} {event_row[1]} {event_row[2]}/{event_row[3]} impact_days={event_row[4]}")

    po_row = conn.execute(
        """
        SELECT po_id, supplier_id, status, estimated_delay_days
          FROM purchase_orders
         WHERE po_id = ?
        """,
        (DEMO_PURCHASE_ORDER["po_id"],),
    ).fetchone()
    if po_row:
        print(f"Purchase order: {po_row[0]} supplier={po_row[1]} status={po_row[2]} delay={po_row[3]}")

    affected_orders = conn.execute(
        """
        SELECT order_id, product_id, quantity, status
          FROM orders
         WHERE order_id LIKE 'ORD-E-DAY1-%'
         ORDER BY order_id
        """
    ).fetchall()
    print("Affected sales orders:")
    for row in affected_orders:
        print(f"- {row[0]} product={row[1]} qty={row[2]} status={row[3]}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed E Day 1 demo data into the ERP SQLite database.")
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="SQLite database path. Defaults to data/erp.db.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all checks and SQL statements, then roll back instead of saving.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    now = datetime.now()
    conn = sqlite3.connect(db_path)
    try:
        validate_expected_schema(conn)
        conn.execute("BEGIN")
        seed_low_stock(conn)
        seed_customer(conn)
        seed_suppliers(conn)
        seed_purchase_order(conn, now)
        seed_orders(conn, now)
        seed_news_and_event(conn, now)
        seed_line_logs(conn, now)

        if args.dry_run:
            print_summary(conn)
            conn.rollback()
            print("\nDry run complete. No changes were saved.")
        else:
            conn.commit()
            print_summary(conn)
            print("\nSeed complete. Demo data was saved.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
