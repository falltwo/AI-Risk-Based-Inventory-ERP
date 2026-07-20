"""Read-only helpers for the L1 supply-chain monitoring surface."""

from __future__ import annotations


def _text(value) -> str:
    if value is None:
        return ""
    normalized = str(value).strip()
    if normalized.casefold() in {"nan", "none", "<na>"}:
        return ""
    return normalized


def _location_matches(left, right) -> bool:
    left_text = "".join(_text(left).casefold().split())
    right_text = "".join(_text(right).casefold().split())
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    return min(len(left_text), len(right_text)) >= 2 and (
        left_text in right_text or right_text in left_text
    )


def _event_matches_supplier(event: dict, supplier: dict) -> bool:
    event_country = _text(event.get("country"))
    event_region = _text(event.get("region"))
    supplier_country = _text(supplier.get("country"))
    supplier_region = _text(supplier.get("region"))

    country_matches = _location_matches(event_country, supplier_country)
    region_matches = _location_matches(event_region, supplier_region)

    if event_region and supplier_region:
        if event_country and supplier_country:
            return country_matches and region_matches
        return region_matches
    if event_country and supplier_country:
        return country_matches
    return region_matches


def _impact_days(event: dict) -> int:
    try:
        return max(0, int(event.get("impact_days") or 0))
    except (TypeError, ValueError):
        return 0


def _event_id(event: dict) -> int:
    try:
        return int(event.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def map_purchase_rows_to_events(
    purchase_rows: list[dict],
    *,
    supplier_context: dict[str, dict],
    events: list[dict],
) -> list[dict]:
    """Enrich imported PO rows with deterministic, non-persistent alert matches."""
    mapped_rows: list[dict] = []
    event_records = [dict(event) for event in events]

    for purchase_row in purchase_rows:
        row = dict(purchase_row)
        supplier_id = _text(row.get("supplier_id"))
        supplier = dict(supplier_context.get(supplier_id) or {})
        country = _text(supplier.get("country"))
        region = _text(supplier.get("region"))
        risk_level = _text(supplier.get("risk_level")) or "未設定"
        row.update(
            {
                "supplier_country": country or "未設定",
                "supplier_region": region or "未設定",
                "supplier_risk_level": risk_level,
            }
        )

        if not country and not region:
            row.update(
                {
                    "match_status": "資料待補",
                    "matched_event_id": None,
                    "event_type": "未命中",
                    "impact_days": 0,
                    "notification_status": "無法判定",
                    "notification": (
                        f"採購單 {_text(row.get('po_id')) or _text(row.get('external_id'))}："
                        f"供應商 {supplier_id or '未設定'} 缺少供應商地區資料，"
                        "目前無法完成事件對映。"
                    ),
                }
            )
            mapped_rows.append(row)
            continue

        matches = [
            event
            for event in event_records
            if _event_matches_supplier(event, supplier)
        ]
        if not matches:
            location = "／".join(part for part in (country, region) if part)
            row.update(
                {
                    "match_status": "正常",
                    "matched_event_id": None,
                    "event_type": "未命中",
                    "impact_days": 0,
                    "notification_status": "無需通知",
                    "notification": (
                        f"採購單 {_text(row.get('po_id')) or _text(row.get('external_id'))}："
                        f"供應商 {supplier_id} 位於{location}，未命中目前風險事件。"
                    ),
                }
            )
            mapped_rows.append(row)
            continue

        matched_event = max(matches, key=lambda event: (_impact_days(event), _event_id(event)))
        event_type = _text(matched_event.get("event_type")) or "未分類事件"
        impact_days = _impact_days(matched_event)
        location = "／".join(part for part in (country, region) if part)
        po_reference = _text(row.get("po_id")) or _text(row.get("external_id"))
        row.update(
            {
                "match_status": "需關注",
                "matched_event_id": matched_event.get("id"),
                "event_type": event_type,
                "impact_days": impact_days,
                "notification_status": "待人工確認",
                "notification": (
                    f"採購單 {po_reference}：供應商 {supplier_id} 位於{location}，"
                    f"命中{event_type}風險，預估延遲 {impact_days} 天。"
                ),
            }
        )
        mapped_rows.append(row)

    return mapped_rows
