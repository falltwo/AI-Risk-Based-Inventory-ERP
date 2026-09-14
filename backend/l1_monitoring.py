"""Read-only helpers for the L1 supply-chain monitoring surface."""

from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3

from backend import database
from backend.risk_contract import valid_event_sql
from backend.access_control import RISK_ALERT_ACK, RISK_ANALYSIS_READ, RISK_OVERVIEW_READ, require_capability


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


def _location_matches(left, right):
    from .region_matching import matches_location, split_location
    c,r = split_location(left)
    return matches_location(c,r,right)


def _event_matches_supplier(event, supplier):
    from .region_matching import matches_location
    return matches_location(supplier.get("country"), supplier.get("region"), event.get("region"), event.get("country"))


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
        f"""
        SELECT e.id, e.event_type, e.region, e.country, e.impact_days,
               e.description, e.created_at, e.news_id,
               n.title AS news_title, n.url AS news_url, n.source AS news_source
        FROM supply_chain_events e
        LEFT JOIN supply_chain_news n ON n.id = e.news_id
        WHERE {valid_event_sql('e.')}
          AND substr(COALESCE(e.created_at, ''), 1, 10) >= ?
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
        SELECT n.id, n.category, n.analysis_region, n.analysis_country, n.estimated_delay,
               n.title, n.analysis_summary, n.url, n.source, n.published_at, n.fetched_at
        FROM supply_chain_news n
        WHERE n.analysis_status='succeeded' AND n.is_relevant=1
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


# ── 告警狀態（已讀／處理中／已通知 L2） ────────────────────────────────
# 監控狀態獨立一張表，不碰事件與新聞本體；L1 每次重整仍直接讀 DB，但狀態會留下來。

ALERT_KIND_CONFIRMED = "confirmed"
ALERT_KIND_CANDIDATE = "candidate"
ALERT_STATUS_UNREAD = "未讀"
ALERT_STATUS_READ = "已讀"
ALERT_STATUS_IN_PROGRESS = "處理中"
ALERT_STATUS_NOTIFIED_L2 = "已通知L2"
CONFIRMED_STATUS_OPTIONS = (ALERT_STATUS_UNREAD, ALERT_STATUS_READ, ALERT_STATUS_IN_PROGRESS)
CANDIDATE_STATUS_OPTIONS = (ALERT_STATUS_UNREAD, ALERT_STATUS_READ, ALERT_STATUS_NOTIFIED_L2)
_STATUS_OPTIONS = {
    ALERT_KIND_CONFIRMED: CONFIRMED_STATUS_OPTIONS,
    ALERT_KIND_CANDIDATE: CANDIDATE_STATUS_OPTIONS,
}


def _ensure_alert_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS risk_alert_states (
               alert_key TEXT PRIMARY KEY, kind TEXT NOT NULL, ref_id INTEGER NOT NULL,
               status TEXT NOT NULL, note TEXT, updated_by TEXT, updated_at TEXT NOT NULL)"""
    )


def _alert_key(kind: str, ref_id) -> str:
    return f"{kind}:{int(ref_id)}"


def set_alert_status(kind: str, ref_id, status: str, *, actor: str | None, note: str = "",
                     conn: sqlite3.Connection | None = None, now: datetime | None = None) -> dict:
    """L1 標記告警狀態。authorization 先於任何寫入；狀態值必須是該類別允許的選項。"""
    require_capability(actor, RISK_ALERT_ACK, conn=conn)
    if kind not in _STATUS_OPTIONS:
        raise ValueError(f"不支援的告警類別：{kind}")
    status = _text(status)
    if status not in _STATUS_OPTIONS[kind]:
        raise ValueError(f"{kind} 告警不支援狀態「{status}」")
    record = {
        "alert_key": _alert_key(kind, ref_id),
        "kind": kind,
        "ref_id": int(ref_id),
        "status": status,
        "note": _text(note)[:500],
        "updated_by": actor,
        "updated_at": (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
    }

    def _write(active_conn: sqlite3.Connection) -> None:
        _ensure_alert_state_table(active_conn)
        active_conn.execute(
            """INSERT INTO risk_alert_states (alert_key, kind, ref_id, status, note, updated_by, updated_at)
               VALUES (:alert_key, :kind, :ref_id, :status, :note, :updated_by, :updated_at)
               ON CONFLICT(alert_key) DO UPDATE SET status=excluded.status, note=excluded.note,
                   updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
            record,
        )
        active_conn.commit()

    if conn is not None:
        _write(conn)
    else:
        with sqlite3.connect(database.DB_FILE) as owned_conn:
            _write(owned_conn)
    return record


def get_alert_states(kind: str, ref_ids, *, conn: sqlite3.Connection | None = None) -> dict[int, dict]:
    ids = sorted({int(i) for i in (ref_ids or []) if i is not None})
    if not ids:
        return {}

    def _load(active_conn: sqlite3.Connection) -> dict[int, dict]:
        _ensure_alert_state_table(active_conn)
        placeholders = ",".join("?" for _ in ids)
        rows = active_conn.execute(
            f"""SELECT ref_id, status, note, updated_by, updated_at FROM risk_alert_states
                WHERE kind = ? AND ref_id IN ({placeholders})""",
            (kind, *ids),
        ).fetchall()
        return {
            int(ref_id): {"status": status, "note": _text(note), "updated_by": _text(by), "updated_at": _text(at)}
            for ref_id, status, note, by, at in rows
        }

    if conn is not None:
        return _load(conn)
    with sqlite3.connect(database.DB_FILE) as owned_conn:
        return _load(owned_conn)


def list_l1_notifications_for_l2(*, actor: str | None, conn: sqlite3.Connection | None = None) -> list[dict]:
    """L1 標成「已通知L2」、而 L2 還沒登錄成事件的情報。L2 頁面頂端提醒用。"""
    require_capability(actor, RISK_ANALYSIS_READ, conn=conn)

    def _load(active_conn: sqlite3.Connection) -> list[dict]:
        _ensure_alert_state_table(active_conn)
        rows = active_conn.execute(
            """
            SELECT s.ref_id, s.note, s.updated_by, s.updated_at,
                   n.title, n.analysis_country, n.analysis_region, n.category, n.estimated_delay, n.url
            FROM risk_alert_states s
            JOIN supply_chain_news n ON n.id = s.ref_id
            WHERE s.kind = ? AND s.status = ?
              AND n.analysis_status='succeeded' AND n.is_relevant=1 AND n.estimated_delay > 0
              AND NOT EXISTS (SELECT 1 FROM supply_chain_events e WHERE e.news_id = n.id)
            ORDER BY s.updated_at DESC
            """,
            (ALERT_KIND_CANDIDATE, ALERT_STATUS_NOTIFIED_L2),
        ).fetchall()
        return [
            {
                "news_id": int(ref_id), "note": _text(note), "notified_by": _text(by), "notified_at": _text(at),
                "title": _text(title), "country": _text(country), "region": _text(region),
                "event_type": _text(category) or "其他", "impact_days": _impact_days({"impact_days": delay}),
                "url": _text(url),
            }
            for ref_id, note, by, at, title, country, region, category, delay, url in rows
        ]

    if conn is not None:
        return _load(conn)
    with sqlite3.connect(database.DB_FILE) as owned_conn:
        return _load(owned_conn)


def load_open_purchase_rows(*, actor: str | None, conn: sqlite3.Connection | None = None) -> list[dict]:
    """系統內未結採購單（一列一品項），格式與 CSV 範本相同，供 L1 對映事件。唯讀。"""
    require_capability(actor, RISK_OVERVIEW_READ, conn=conn)

    def _load(active_conn: sqlite3.Connection) -> list[dict]:
        rows = active_conn.execute(
            """
            SELECT p.po_id, p.supplier_id, i.product_id, i.qty, p.status, p.order_date, p.total_amount
            FROM purchase_orders p
            LEFT JOIN purchase_order_items i ON i.po_id = p.po_id
            WHERE (p.status IS NULL OR p.status NOT IN ('已完成', '已取消'))
            ORDER BY p.po_id, i.id
            """
        ).fetchall()
        return [
            {
                "external_id": po_id, "po_id": po_id, "supplier_id": _text(supplier_id),
                "product_id": _text(product_id), "qty": int(qty or 0), "status": _text(status),
                "order_date": _text(order_date), "total_amount": float(total_amount or 0),
            }
            for po_id, supplier_id, product_id, qty, status, order_date, total_amount in rows
        ]

    if conn is not None:
        return _load(conn)
    with sqlite3.connect(database.DB_FILE) as owned_conn:
        return _load(owned_conn)


def _attach_ack_and_proposals(conn: sqlite3.Connection, confirmed: list[dict], candidates: list[dict]) -> None:
    """把 L1 標記狀態與 L3 提案計數併進告警列（唯讀）。"""
    from backend.purchase_proposals import proposal_status_summary_by_event

    confirmed_states = get_alert_states(ALERT_KIND_CONFIRMED, [item["id"] for item in confirmed], conn=conn)
    proposal_counts = proposal_status_summary_by_event([item["id"] for item in confirmed], conn=conn)
    for item in confirmed:
        state = confirmed_states.get(int(item["id"]), {})
        item["ack_status"] = state.get("status") or ALERT_STATUS_UNREAD
        item["ack_note"] = state.get("note", "")
        item["ack_by"] = state.get("updated_by", "")
        item["ack_at"] = state.get("updated_at", "")
        item["proposals"] = proposal_counts.get(int(item["id"]), {"pending": 0, "approved": 0, "rejected": 0, "unsubmitted": 0})
    candidate_states = get_alert_states(ALERT_KIND_CANDIDATE, [item["news_id"] for item in candidates], conn=conn)
    for item in candidates:
        state = candidate_states.get(int(item["news_id"]), {})
        item["ack_status"] = state.get("status") or ALERT_STATUS_UNREAD
        item["ack_note"] = state.get("note", "")
        item["ack_by"] = state.get("updated_by", "")
        item["ack_at"] = state.get("updated_at", "")


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
        _attach_ack_and_proposals(active_conn, confirmed, candidates)
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


# ── 最新 AI 風險摘要（唯讀） ──────────────────────────────────────────


def get_latest_risk_summary(*, actor: str | None, conn: sqlite3.Connection | None = None) -> dict | None:
    """L2／排程最近一次產生並落地的 AI 風險摘要；L1 只讀、不觸發任何模型呼叫。

    authorization 先於任何資料讀取；缺少 RISK_OVERVIEW_READ 直接拒絕。
    """
    require_capability(actor, RISK_OVERVIEW_READ, conn=conn)
    from backend.supply_chain_risk import get_latest_ai_risk_summary

    return get_latest_ai_risk_summary(conn=conn)
