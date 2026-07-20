"""Fresh databases must support the bundled E Day 1 demo seeder."""

import sqlite3
from datetime import datetime

import pytest

from backend import database
from scripts import seed_e_day1_demo_data


def test_fresh_database_has_every_column_required_by_demo_seeder(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "fresh-demo.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))

    database.init_db()

    with sqlite3.connect(db_path) as conn:
        seed_e_day1_demo_data.validate_expected_schema(conn)
        conn.execute("BEGIN")
        seed_e_day1_demo_data.seed_low_stock(conn)
        seed_e_day1_demo_data.seed_customer(conn)
        seed_e_day1_demo_data.seed_suppliers(conn)
        seed_e_day1_demo_data.seed_purchase_order(conn, datetime.now())
        seed_e_day1_demo_data.seed_orders(conn, datetime.now())
        seed_e_day1_demo_data.seed_news_and_event(conn, datetime.now())
        seed_e_day1_demo_data.seed_line_logs(conn, datetime.now())
        assert conn.execute(
            "SELECT COUNT(*) FROM supply_chain_events"
        ).fetchone()[0] >= 1
        orphan_item_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM purchase_order_items item
            LEFT JOIN inventory product
              ON product.product_id = item.product_id
            WHERE item.po_id = ? AND product.product_id IS NULL
            """,
            (seed_e_day1_demo_data.DEMO_PURCHASE_ORDER["po_id"],),
        ).fetchone()[0]
        assert orphan_item_count == 0
        conn.rollback()


def test_demo_seed_preserves_source_line_identity_after_approval(
    tmp_path, monkeypatch
):
    from backend.purchase_proposals import (
        ApprovalDecision,
        decide_purchase_proposal,
        prepare_alternative_purchase_proposal,
        submit_purchase_proposal,
    )

    db_path = tmp_path / "stable-demo-source.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    database.init_db()
    now = datetime.now()

    with sqlite3.connect(db_path) as conn:
        seed_e_day1_demo_data.seed_low_stock(conn)
        seed_e_day1_demo_data.seed_suppliers(conn)
        seed_e_day1_demo_data.seed_purchase_order(conn, now)
        source_id = conn.execute(
            "SELECT id FROM purchase_order_items "
            "WHERE po_id = ? AND product_id = 'P019'",
            (seed_e_day1_demo_data.DEMO_PURCHASE_ORDER["po_id"],),
        ).fetchone()[0]

    proposal = prepare_alternative_purchase_proposal(
        proposal_id="PROP-SEED-STABLE-A",
        affected_po_id=seed_e_day1_demo_data.DEMO_PURCHASE_ORDER["po_id"],
        product_id="P019",
        source_po_item_id=source_id,
        alternative_supplier_id="SUP-E-DEMO-TW",
        reason="Verify stable demo source identity",
        actor="planner",
    )
    submit_purchase_proposal(proposal, actor="planner")
    approved = decide_purchase_proposal(
        ApprovalDecision(proposal_id=proposal.proposal_id, outcome="approve"),
        actor="approver",
    )
    assert approved.status == "ok"

    with sqlite3.connect(db_path) as conn:
        seed_e_day1_demo_data.seed_purchase_order(conn, now)
        replayed_source_id = conn.execute(
            "SELECT id FROM purchase_order_items "
            "WHERE po_id = ? AND product_id = 'P019'",
            (seed_e_day1_demo_data.DEMO_PURCHASE_ORDER["po_id"],),
        ).fetchone()[0]
    assert replayed_source_id == source_id

    second = prepare_alternative_purchase_proposal(
        proposal_id="PROP-SEED-STABLE-B",
        affected_po_id=seed_e_day1_demo_data.DEMO_PURCHASE_ORDER["po_id"],
        product_id="P019",
        source_po_item_id=source_id,
        alternative_supplier_id="SUP-E-DEMO-TW",
        reason="A second full replacement must remain blocked",
        actor="planner",
    )
    with pytest.raises(PermissionError, match="已有另一份替代提案"):
        submit_purchase_proposal(second, actor="planner")


def test_init_db_additively_upgrades_old_demo_schema_without_data_loss(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "existing-deployment.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE purchase_orders (
                po_id TEXT PRIMARY KEY,
                supplier_id TEXT,
                order_date TEXT,
                status TEXT,
                total_amount REAL,
                note TEXT,
                operation_id TEXT,
                last_sync_operation_id TEXT,
                external_source_system TEXT,
                external_id TEXT,
                external_version INTEGER
            );
            CREATE TABLE customers (
                customer_id TEXT PRIMARY KEY,
                name TEXT,
                contact TEXT,
                phone TEXT,
                email TEXT
            );
            INSERT INTO purchase_orders (
                po_id, supplier_id, order_date, status, total_amount, note,
                operation_id, last_sync_operation_id, external_source_system,
                external_id, external_version
            ) VALUES (
                'PO-OLD-1', 'SUP-OLD-1', '2026-01-02', '待入庫', 1234.5,
                'preserve purchase order', 'op-old', 'sync-old', 'legacy-erp',
                'external-old', 3
            );
            INSERT INTO customers (
                customer_id, name, contact, phone, email
            ) VALUES (
                'C-OLD-1', '既有客戶', '林小姐', '04-12345678',
                'legacy@example.com'
            );
            """
        )

    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    database.init_db()

    with sqlite3.connect(db_path) as conn:
        purchase_order = conn.execute(
            "SELECT po_id, supplier_id, total_amount, note, external_version "
            "FROM purchase_orders WHERE po_id = 'PO-OLD-1'"
        ).fetchone()
        customer = conn.execute(
            "SELECT customer_id, name, contact, phone, email "
            "FROM customers WHERE customer_id = 'C-OLD-1'"
        ).fetchone()
        po_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(purchase_orders)")
        }
        customer_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(customers)")
        }
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert purchase_order == (
        "PO-OLD-1", "SUP-OLD-1", 1234.5, "preserve purchase order", 3
    )
    assert customer == (
        "C-OLD-1", "既有客戶", "林小姐", "04-12345678",
        "legacy@example.com",
    )
    assert {"estimated_delay_days", "alternative_suggestion"} <= po_columns
    assert {
        "company", "country", "region", "latitude", "longitude", "risk_level"
    } <= customer_columns
    assert "risk_heatmap" in tables
