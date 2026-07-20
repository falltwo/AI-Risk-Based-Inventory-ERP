from copy import deepcopy
from pathlib import Path

from backend.l1_monitoring import map_purchase_rows_to_events


ROOT = Path(__file__).resolve().parents[1]


def _purchase_row(**overrides):
    row = {
        "external_id": "EXT-001",
        "po_id": "PO-001",
        "supplier_id": "SUP01",
        "product_id": "P001",
        "qty": 100,
        "unit_price": 25.0,
        "order_date": "2026-07-21",
        "status": "待交貨",
        "note": "",
    }
    row.update(overrides)
    return row


def test_l1_csv_rows_map_to_highest_impact_location_event_without_mutating_inputs():
    purchase_rows = [_purchase_row()]
    supplier_context = {
        "SUP01": {
            "country": "日本",
            "region": "關東",
            "risk_level": "中",
        }
    }
    events = [
        {
            "id": 10,
            "event_type": "交通",
            "country": "日本",
            "region": "關西",
            "impact_days": 3,
            "description": "港口壅塞",
        },
        {
            "id": 11,
            "event_type": "地震",
            "country": "日本",
            "region": "關東",
            "impact_days": 14,
            "description": "區域物流中斷",
        },
    ]
    original_rows = deepcopy(purchase_rows)
    original_context = deepcopy(supplier_context)
    original_events = deepcopy(events)

    result = map_purchase_rows_to_events(
        purchase_rows, supplier_context=supplier_context, events=events
    )

    assert result == [
        {
            **purchase_rows[0],
            "supplier_country": "日本",
            "supplier_region": "關東",
            "supplier_risk_level": "中",
            "match_status": "需關注",
            "matched_event_id": 11,
            "event_type": "地震",
            "impact_days": 14,
            "notification_status": "待人工確認",
            "notification": (
                "採購單 PO-001：供應商 SUP01 位於日本／關東，"
                "命中地震風險，預估延遲 14 天。"
            ),
        }
    ]
    assert purchase_rows == original_rows
    assert supplier_context == original_context
    assert events == original_events


def test_l1_unknown_supplier_remains_unmatched_and_does_not_invent_an_alert():
    result = map_purchase_rows_to_events(
        [_purchase_row(supplier_id="UNKNOWN")],
        supplier_context={},
        events=[
            {
                "id": 1,
                "event_type": "戰爭",
                "country": "日本",
                "region": "關東",
                "impact_days": 30,
            }
        ],
    )

    assert result[0]["match_status"] == "資料待補"
    assert result[0]["matched_event_id"] is None
    assert result[0]["event_type"] == "未命中"
    assert result[0]["impact_days"] == 0
    assert result[0]["notification_status"] == "無法判定"
    assert "缺少供應商地區資料" in result[0]["notification"]


def test_l1_known_supplier_without_matching_event_is_reported_as_normal():
    result = map_purchase_rows_to_events(
        [_purchase_row()],
        supplier_context={
            "SUP01": {"country": "台灣", "region": "中部", "risk_level": "低"}
        },
        events=[
            {
                "id": 1,
                "event_type": "罷工",
                "country": "德國",
                "region": "漢堡",
                "impact_days": 7,
            }
        ],
    )

    assert result[0]["match_status"] == "正常"
    assert result[0]["matched_event_id"] is None
    assert result[0]["event_type"] == "未命中"
    assert result[0]["notification_status"] == "無需通知"
    assert "未命中目前風險事件" in result[0]["notification"]


def test_l1_overview_source_contains_read_only_csv_mapping_and_notification_flow():
    source = (ROOT / "frontend/components/risk_overview.py").read_text(
        encoding="utf-8"
    )

    assert "build_purchase_order_template_csv" in source
    assert "parse_purchase_order_csv" in source
    assert "map_purchase_rows_to_events" in source
    assert 'type=["csv"]' in source
    assert "不會寫入 ERP" in source
    assert "L1 告警與通知中心" in source
    assert "stage_purchase_order_rows" not in source
    assert "submit_exchange_record" not in source
