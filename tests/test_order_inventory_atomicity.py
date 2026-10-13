"""Sales-order and inventory effects must commit atomically."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend import database
from backend.orders import (
    InsufficientStockError,
    create_sales_order,
    transition_order_status,
)


@pytest.fixture(autouse=True)
def initialized_db():
    database.init_db()
    database.run_query("UPDATE inventory SET stock = 10 WHERE product_id = 'P001'", fetch=False)
    database.run_query("DELETE FROM stock_moves WHERE product_id = 'P001'", fetch=False)
    database.run_query("DELETE FROM orders WHERE order_id LIKE 'ATOMIC-%'", fetch=False)


def _stock() -> int:
    return database.run_query(
        "SELECT stock FROM inventory WHERE product_id = 'P001'"
    )[0][0]


def test_create_order_commits_order_stock_and_movement_together():
    result = create_sales_order(
        order_id="ATOMIC-CREATE",
        customer_id="C001",
        product_id="P001",
        quantity=3,
    )

    assert result["remaining_stock"] == 7
    assert _stock() == 7
    assert database.run_query(
        "SELECT status FROM orders WHERE order_id = 'ATOMIC-CREATE'"
    ) == [("處理中",)]
    assert database.run_query(
        "SELECT qty, move_type, ref_no FROM stock_moves WHERE ref_no = 'ATOMIC-CREATE'"
    ) == [(-3, "銷售預留", "ATOMIC-CREATE")]


def test_duplicate_order_id_rolls_back_stock_change():
    create_sales_order(
        order_id="ATOMIC-DUPLICATE",
        customer_id="C001",
        product_id="P001",
        quantity=2,
    )
    stock_before_retry = _stock()

    with pytest.raises(sqlite3.IntegrityError):
        create_sales_order(
            order_id="ATOMIC-DUPLICATE",
            customer_id="C001",
            product_id="P001",
            quantity=2,
        )

    assert _stock() == stock_before_retry
    assert database.run_query(
        "SELECT COUNT(*) FROM stock_moves WHERE ref_no = 'ATOMIC-DUPLICATE'"
    )[0][0] == 1


def test_concurrent_orders_cannot_oversell():
    def submit(order_id: str):
        try:
            create_sales_order(
                order_id=order_id,
                customer_id="C001",
                product_id="P001",
                quantity=7,
            )
            return "created"
        except InsufficientStockError:
            return "insufficient"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, ("ATOMIC-RACE-A", "ATOMIC-RACE-B")))

    assert sorted(outcomes) == ["created", "insufficient"]
    assert _stock() == 3
    assert database.run_query(
        "SELECT COUNT(*) FROM orders WHERE order_id IN ('ATOMIC-RACE-A', 'ATOMIC-RACE-B')"
    )[0][0] == 1


def test_cancel_is_idempotent_and_reactivation_reserves_again():
    create_sales_order(
        order_id="ATOMIC-STATUS",
        customer_id="C001",
        product_id="P001",
        quantity=4,
    )
    assert _stock() == 6

    cancelled = transition_order_status("ATOMIC-STATUS", "已取消")
    assert cancelled["remaining_stock"] == 10
    assert _stock() == 10

    repeated = transition_order_status("ATOMIC-STATUS", "已取消")
    assert repeated["changed"] is False
    assert _stock() == 10

    reactivated = transition_order_status("ATOMIC-STATUS", "處理中")
    assert reactivated["remaining_stock"] == 6
    assert _stock() == 6
    assert database.run_query(
        "SELECT qty, move_type FROM stock_moves WHERE ref_no = 'ATOMIC-STATUS' ORDER BY move_id"
    ) == [
        (-4, "銷售預留"),
        (4, "取消回補"),
        (-4, "重新預留"),
    ]


def test_failed_reactivation_keeps_cancelled_status_and_stock():
    create_sales_order(
        order_id="ATOMIC-REACTIVATE",
        customer_id="C001",
        product_id="P001",
        quantity=8,
    )
    transition_order_status("ATOMIC-REACTIVATE", "已取消")
    database.run_query("UPDATE inventory SET stock = 2 WHERE product_id = 'P001'", fetch=False)

    with pytest.raises(InsufficientStockError):
        transition_order_status("ATOMIC-REACTIVATE", "已出貨")

    assert _stock() == 2
    assert database.run_query(
        "SELECT status FROM orders WHERE order_id = 'ATOMIC-REACTIVATE'"
    ) == [("已取消",)]
