"""Shared news eligibility and source validation for L1, L2 and L3."""
from .risk_validation import number, text, EVENT_TYPES
from .region_matching import matches_location


def valid_news_sql(alias=""):
    if alias not in ("", "n."):
        raise ValueError("Unsupported news alias")
    return f"{alias}analysis_status='succeeded' AND {alias}is_relevant=1"


def valid_event_sql(alias=""):
    if alias not in ("", "e."):
        raise ValueError("Unsupported event alias")
    return f"({alias}news_id IS NULL OR {alias}news_id IN (SELECT id FROM supply_chain_news WHERE {valid_news_sql()} AND estimated_delay IS NOT NULL))"


def analyzed_news(row):
    """Project analysis without ever falling back to raw geography or prose."""
    if row.get("analysis_status") != "succeeded" or row.get("is_relevant") != 1:
        return None
    try:
        days = number(row.get("estimated_delay"), maximum=365, integer=True, nullable=True)
        country = text(row.get("analysis_country"))
        region = text(row.get("analysis_region"))
        summary = text(row.get("analysis_summary"))
        if row.get("category") not in EVENT_TYPES:
            return None
    except (ValueError, TypeError):
        return None
    return dict(row, country=country, region=region, summary=summary, estimated_delay=days)


def validate_event(conn, event_type, region, country, impact_days, description, news_id):
    event_type, region, country, description = map(text, (event_type, region, country, description))
    if event_type not in EVENT_TYPES or not (region or country):
        raise ValueError("事件類型或地區無效")
    days = number(impact_days, maximum=365, integer=True)
    if news_id is not None:
        news_id = number(news_id, maximum=2**53-1, integer=True)
        row = conn.execute("SELECT analysis_status,is_relevant,estimated_delay,analysis_country,analysis_region FROM supply_chain_news WHERE id=?", (news_id,)).fetchone()
        if not row or row[0] != "succeeded" or row[1] != 1 or row[2] is None:
            raise ValueError("新聞分析尚未成功或延遲未知，無法登錄風險")
        number(row[2], maximum=365, integer=True)
        if not matches_location(country, region, row[4], row[3]):
            raise ValueError("事件地區不符合來源新聞的分析地區")
    return event_type, region, country, days, description, news_id
