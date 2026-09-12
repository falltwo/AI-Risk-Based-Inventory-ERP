"""Read-only helpers for the L1 supply-chain monitoring surface."""

from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3

from backend import database
from backend.access_control import RISK_OVERVIEW_READ, require_capability


# 告警嚴重度依預估延遲天數分級；L1 只讀不寫，分級規則放在後端以便 LINE / Web 共用。
ALERT_SEVERITY_HIGH_DAYS = 14
ALERT_SEVERITY_MEDIUM_DAYS = 7

ALERT_SOURCE_NEWS = "新聞登錄"
ALERT_SOURCE_MANUAL = "人工登錄"
CANDIDATE_STATUS = "AI 偵測待確認"


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


# ── 最新事件告警（唯讀 feed） ──────────────────────────────────────────


def classify_alert_severity(impact_days) -> str:
    """依預估延遲天數回傳「高／中／低／無」。"""
    days = _impact_days({"impact_days": impact_days})
    if days >= ALERT_SEVERITY_HIGH_DAYS:
        return "高"
    if days >= ALERT_SEVERITY_MEDIUM_DAYS:
        return "中"
    if days >= 1:
        return "低"
    return "無"


def _window_start(since_days: int, *, now: datetime | None = None) -> str:
    days = max(0, int(since_days or 0))
    reference = now or datetime.now()
    return (reference - timedelta(days=days)).strftime("%Y-%m-%d")


def _load_confirmed_alerts(conn: sqlite3.Connection, *, since: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT e.id, e.event_type, e.region, e.country, e.impact_days,
               e.description, e.created_at, e.news_id,
               n.title AS news_title, n.url AS news_url, n.source AS news_source
        FROM supply_chain_events e
        LEFT JOIN supply_chain_news n ON n.id = e.news_id
        WHERE substr(COALESCE(e.created_at, ''), 1, 10) >= ?
        ORDER BY COALESCE(e.created_at, '') DESC, e.id DESC
        LIMIT ?
        """,
        (since, limit),
    ).fetchall()
    alerts = []
    for row in rows:
        (
            event_id, event_type, region, country, impact_days,
            description, created_at, news_id, news_title, news_url, news_source,
        ) = row
        alerts.append(
            {
                "id": event_id,
                "event_type": _text(event_type) or "未分類",
                "country": _text(country),
                "region": _text(region),
                "impact_days": _impact_days({"impact_days": impact_days}),
                "severity": classify_alert_severity(impact_days),
                "description": _text(description),
                "created_at": _text(created_at),
                "news_id": news_id,
                "source": ALERT_SOURCE_NEWS if news_id is not None else ALERT_SOURCE_MANUAL,
                "news_title": _text(news_title),
                "news_url": _text(news_url),
                "news_source": _text(news_source),
            }
        )
    return alerts


def _load_candidate_alerts(conn: sqlite3.Connection, *, since: str, limit: int) -> list[dict]:
    """尚未登錄為正式事件、但 AI 判定有實質延遲的新聞。

    這層讓 L1 在 L2 尚未按「登錄」之前就能看到新偵測到的風險；資料只來自
    排程／L2 已寫入的 supply_chain_news，本函式不觸發抓取也不寫入。
    """
    rows = conn.execute(
        """
        SELECT n.id, n.category, n.region, n.country, n.estimated_delay,
               n.title, n.summary, n.url, n.source, n.published_at, n.fetched_at
        FROM supply_chain_news n
        WHERE COALESCE(n.is_relevant, 1) = 1
          AND COALESCE(n.estimated_delay, 0) > 0
          AND COALESCE(date(n.published_at), date(n.fetched_at), '') >= ?
          AND NOT EXISTS (
              SELECT 1 FROM supply_chain_events e WHERE e.news_id = n.id
          )
        ORDER BY COALESCE(date(n.published_at), date(n.fetched_at), '') DESC,
                 n.estimated_delay DESC, n.id DESC
        """,
        (since,),
    ).fetchall()
    candidates = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        (
            news_id, category, region, country, estimated_delay,
            title, summary, url, source, published_at, fetched_at,
        ) = row
        dedupe_key = (_text(title)[:200], _text(url))
        if dedupe_key in seen or dedupe_key == ("", ""):
            continue
        seen.add(dedupe_key)
        candidates.append(
            {
                "news_id": news_id,
                "event_type": _text(category) or "其他",
                "country": _text(country),
                "region": _text(region),
                "impact_days": _impact_days({"impact_days": estimated_delay}),
                "severity": classify_alert_severity(estimated_delay),
                "title": _text(title),
                "summary": _text(summary),
                "url": _text(url),
                "news_source": _text(source),
                "observed_at": _text(published_at) or _text(fetched_at),
                "status": CANDIDATE_STATUS,
            }
        )
        if len(candidates) >= limit:
            break
    return candidates


def get_latest_event_alerts(
    *,
    actor: str | None,
    since_days: int = 30,
    limit: int = 10,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> dict:
    """L1 告警 feed：已確認事件 + AI 偵測待確認候選，皆為唯讀。

    authorization 先於任何資料讀取；缺少 RISK_OVERVIEW_READ 直接拒絕。
    每次呼叫都重新查詢資料庫，所以排程或 L2 寫入新聞／事件後，
    L1 下一次 rerun 就會看到更新，不依賴 session state。
    """
    require_capability(actor, RISK_OVERVIEW_READ, conn=conn)
    limit = max(1, int(limit or 1))
    since = _window_start(since_days, now=now)

    def _load(active_conn: sqlite3.Connection) -> dict:
        confirmed = _load_confirmed_alerts(active_conn, since=since, limit=limit)
        candidates = _load_candidate_alerts(active_conn, since=since, limit=limit)
        severities = [item["severity"] for item in confirmed + candidates]
        highest = "無"
        for level in ("高", "中", "低"):
            if level in severities:
                highest = level
                break
        return {
            "since": since,
            "since_days": max(0, int(since_days or 0)),
            "generated_at": (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
            "confirmed": confirmed,
            "candidates": candidates,
            "confirmed_count": len(confirmed),
            "candidate_count": len(candidates),
            "highest_severity": highest,
        }

    if conn is not None:
        return _load(conn)
    with sqlite3.connect(database.DB_FILE) as owned_conn:
        return _load(owned_conn)
