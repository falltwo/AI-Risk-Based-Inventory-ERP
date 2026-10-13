"""Raw news retention, durable analysis state and pre-analysis deduplication."""
import hashlib
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


def now():
    return datetime.now(timezone.utc).isoformat()


def identity_keys(item):
    url = str(item.get("url") or "").strip()
    if url:
        parts = urlsplit(url)
        query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"})
        url = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))
    title = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(item.get("title") or ""))).strip().casefold()
    # Different syndication URLs with the same headline/source/publication day are one story.
    content = "|".join((title, str(item.get("source") or "").strip().casefold(), str(item.get("published_at") or "")[:10])) if title else ""
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None
    return digest(url), digest(content)


def migrate(conn):
    columns = {r[1] for r in conn.execute("PRAGMA table_info(supply_chain_news)")}
    additions = {"analysis_status": "TEXT NOT NULL DEFAULT 'legacy_unverified'",
                 "analysis_error": "TEXT", "analysis_summary": "TEXT", "analysis_country": "TEXT",
                 "analysis_region": "TEXT", "analyzed_at": "TEXT", "url_key": "TEXT", "content_key": "TEXT"}
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE supply_chain_news ADD COLUMN {name} {declaration}")
    if "estimated_delay" not in {r[1] for r in conn.execute("PRAGMA table_info(risk_heatmap)")}:
        conn.execute("ALTER TABLE risk_heatmap ADD COLUMN estimated_delay INTEGER")
    # Keep historical duplicates and IDs (events may reference them); index only the first owner.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS news_url_unique ON supply_chain_news(url_key) WHERE url_key IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS news_content_unique ON supply_chain_news(content_key) WHERE content_key IS NOT NULL")
    if "url_key" not in columns:
        rows = conn.execute("SELECT id,title,url,source,published_at FROM supply_chain_news ORDER BY id").fetchall()
        for row in rows:
            uk, ck = identity_keys(dict(zip(("id", "title", "url", "source", "published_at"), row)))
            for field, value in (("url_key", uk), ("content_key", ck)):
                if value and not conn.execute(f"SELECT 1 FROM supply_chain_news WHERE {field}=?", (value,)).fetchone():
                    conn.execute(f"UPDATE supply_chain_news SET {field}=? WHERE id=?", (value, row[0]))
    conn.execute("""CREATE TABLE IF NOT EXISTS scheduled_jobs (
        job_key TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        started_at TEXT, finished_at TEXT, error TEXT, result_json TEXT)""")


def find_existing(conn, item):
    uk, ck = identity_keys(item)
    return conn.execute("SELECT id,analysis_status FROM supply_chain_news WHERE url_key=? OR content_key=? ORDER BY id LIMIT 1", (uk, ck)).fetchone()


def store_raw(conn, item):
    existing = find_existing(conn, item)
    if existing:
        return existing[0], False
    uk, ck = identity_keys(item)
    if not (uk or ck):
        raise ValueError("News needs a title or URL")
    values = [item.get(k) for k in ("country", "region", "title", "summary", "url", "source", "published_at", "relevance_tag")]
    try:
        cur = conn.execute("""INSERT INTO supply_chain_news
            (country,region,title,summary,url,source,published_at,relevance_tag,fetched_at,
             analysis_status,is_relevant,estimated_delay,url_key,content_key)
            VALUES (?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,?,?)""", (*values, now(), uk, ck))
        return cur.lastrowid, True
    except sqlite3.IntegrityError:
        existing = find_existing(conn, item)
        if existing:
            return existing[0], False
        raise


def store_analysis(conn, news_id, result):
    from .risk_validation import number, text, EVENT_TYPES
    success = result.get("analysis_status") == "succeeded"
    delay = number(result.get("estimated_delay"), maximum=365, integer=True, nullable=True) if success else None
    if success:
        if type(result.get("is_relevant")) is not bool:
            raise ValueError("Invalid analysis relevance")
        for key in ("country", "region", "chinese_summary"):
            text(result.get(key))
        if result.get("event_type") not in EVENT_TYPES:
            raise ValueError("Invalid event type")
        if not result["is_relevant"] and delay not in (None, 0):
            raise ValueError("Irrelevant news cannot have delay")
    conn.execute("""UPDATE supply_chain_news SET analysis_status=?,analysis_error=?,
        analysis_summary=?,analysis_country=?,analysis_region=?,category=?,is_relevant=?,
        estimated_delay=?,analyzed_at=? WHERE id=? AND analysis_status!='succeeded'""",
        ("succeeded" if success else "failed", None if success else result.get("analysis_error", "invalid_output"),
         result.get("chinese_summary") if success else None, result.get("country") if success else None,
         result.get("region") if success else None, result.get("event_type") if success else None,
         int(result["is_relevant"]) if success else None, delay, now(), news_id))
