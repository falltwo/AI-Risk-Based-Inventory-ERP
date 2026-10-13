"""Evidence, provenance and durable summary storage using the batch-one contract."""
import json
import sqlite3
import math
from datetime import datetime
from . import database
from .access_control import RISK_WORKSPACE_WRITE, require_capability
from .risk_contract import analyzed_news
from .risk_validation import number
from .region_matching import matches_location, split_location, normalize


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS risk_ai_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
        actor TEXT, reference_date TEXT, summary TEXT NOT NULL,
        updates_json TEXT NOT NULL, events_json TEXT NOT NULL, audit_json TEXT NOT NULL,
        news_count INTEGER DEFAULT 0, event_count INTEGER DEFAULT 0)""")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(risk_ai_summaries)")}
    for name, definition in {
        "analysis_status": "TEXT NOT NULL DEFAULT 'legacy_unverified'",
        "analysis_error": "TEXT", "sources_json": "TEXT NOT NULL DEFAULT '[]'",
        "raw_summary": "TEXT",
    }.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE risk_ai_summaries ADD COLUMN {name} {definition}")


def save_ai_risk_summary(result, *, actor=None, conn=None):
    require_capability(actor, RISK_WORKSPACE_WRITE, conn=conn)
    status = result.get("analysis_status")
    if status not in {"succeeded", "failed"} or (status == "succeeded" and result.get("error")):
        raise ValueError("Summary needs an explicit consistent analysis status")
    owned = conn is None
    conn = conn or sqlite3.connect(database.DB_FILE)
    try:
        migrate(conn)
        cur = conn.execute("""INSERT INTO risk_ai_summaries
            (created_at,actor,reference_date,summary,updates_json,events_json,audit_json,
             news_count,event_count,analysis_status,analysis_error,sources_json,raw_summary)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            result.get("generated_at") or datetime.now().isoformat(), actor, result.get("reference_date"),
            result.get("summary") or "", json.dumps(result.get("updates") or [], ensure_ascii=False),
            json.dumps(result.get("events") or [], ensure_ascii=False), json.dumps(result.get("audit") or [], ensure_ascii=False),
            result.get("news_count", 0), result.get("event_count", 0), status, result.get("analysis_error"),
            json.dumps(result.get("sources") or [], ensure_ascii=False, allow_nan=False), result.get("raw_summary"),
        ))
        if owned:
            conn.commit()
        return cur.lastrowid
    finally:
        if owned:
            conn.close()


def get_latest_ai_risk_summary(conn=None):
    owned = conn is None
    conn = conn or sqlite3.connect(database.DB_FILE)
    try:
        cur = conn.execute("SELECT * FROM risk_ai_summaries WHERE analysis_status='succeeded' ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if row is None:
            return None
        r = dict(zip([c[0] for c in cur.description], row))
        return dict(summary_id=r["id"], generated_at=r["created_at"], actor=r["actor"],
                    reference_date=r["reference_date"], summary=r["summary"],
                    updates=json.loads(r["updates_json"]), events=json.loads(r["events_json"]),
                    audit=json.loads(r["audit_json"]), sources=json.loads(r["sources_json"]),
                    analysis_status=r["analysis_status"], analysis_error=r["analysis_error"],
                    news_count=r["news_count"], event_count=r["event_count"], error=False)
    finally:
        if owned:
            conn.close()


def build_risk_evidence(news_items, events):
    locations = {}
    sources = []
    def add(row, kind):
        country, region = normalize(row.get("country")), normalize(row.get("region"))
        if not (country or region):
            return
        days = number(row.get("estimated_delay") if kind == "news" else row.get("impact_days"), maximum=365, integer=True, nullable=True)
        key = f"{country}|{region}"
        entry = locations.setdefault(key, {"country": country, "region": region, "types": set(), "max_days": None})
        entry["types"].add(row.get("category") if kind == "news" else row.get("event_type"))
        if days is not None:
            entry["max_days"] = max(days, entry["max_days"] if entry["max_days"] is not None else 0)
        scalar = lambda v: None if isinstance(v, float) and math.isnan(v) else v
        sources.append(dict(kind=kind, id=scalar(row.get("id")), news_id=scalar(row.get("news_id")),
                            analysis_status="succeeded" if kind == "news" else "validated_event",
                            country=country, region=region, delay=days,
                            title=row.get("title"), url=row.get("url"),
                            analyzed_at=row.get("analyzed_at"), event_type=row.get("category") if kind == "news" else row.get("event_type")))
    for raw in news_items or []:
        row = analyzed_news(raw)
        if row is not None:
            add(row, "news")
    rows = events.to_dict("records") if hasattr(events, "to_dict") else events or []
    for row in rows:
        # Event loaders already enforce source status. Unknown/invalid days are
        # never turned into a zero measurement by evidence construction.
        try:
            add(row, "event")
        except (TypeError, ValueError):
            continue
    return dict(locations=locations, sources=sources)


def _evidence_for(name, evidence):
    country, region = split_location(name)
    hits = [v for v in evidence.get("locations", {}).values()
            if matches_location(v["country"], v["region"], name)
            or (country and matches_location(country, region, v["region"], v["country"]))]
    if not hits:
        return None
    known = [v["max_days"] for v in hits if v["max_days"] is not None]
    return {"max_days": max(known) if known else None, "types": set().union(*(v["types"] for v in hits))}


def gate_by_evidence(updates, events, evidence):
    kept_updates, kept_events, audit = [], [], []
    def note(kind, name, reason, action="略過"):
        audit.append(dict(kind=kind, name=name, reason=reason, action=action))
    for u in updates or []:
        name = u.get("display_name", "")
        if _evidence_for(name, evidence) is None:
            note("更新", name, "沒有此據點的有效分析證據")
        else:
            kept_updates.append(dict(u))
    for e in events or []:
        name = f"{e.get('country') or ''}|{e.get('region') or ''}"
        ev = _evidence_for(name, evidence)
        if ev is None or ev["max_days"] is None:
            note("事件", name, "沒有此據點的已知延遲證據")
            continue
        item = dict(e)
        days = number(item.get("impact_days"), maximum=365, integer=True, nullable=True)
        if days is None:
            note("事件", name, "建議延遲未知，不能建立事件")
            continue
        cap = min(365, ev["max_days"] * 2)
        if days > cap:
            item["impact_days"] = cap
            note("事件", name, f"延遲超過證據上限，調整為 {cap} 天", "調整")
        types = ev["types"] - {None, "", "其他"}
        if item.get("event_type") != "其他" and item.get("event_type") not in types:
            item["event_type"] = "其他"
            note("事件", name, "事件類型沒有證據，改為其他", "調整")
        kept_events.append(item)
    return kept_updates, kept_events, audit
