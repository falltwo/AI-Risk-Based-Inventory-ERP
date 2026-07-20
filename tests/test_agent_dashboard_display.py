from frontend.page_agent_dashboard import format_parameters_to_chinese


def test_purchase_order_approval_summary_includes_unit_price():
    summary = format_parameters_to_chinese(
        "create_purchase_order",
        {
            "po_id": "PO-1",
            "supplier_id": "SUP01",
            "product_id": "P001",
            "qty": 2,
            "unit_price": 500.0,
        },
    )

    assert "單價 NT$500.00" in summary
