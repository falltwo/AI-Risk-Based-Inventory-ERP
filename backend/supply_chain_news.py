"""
backend/supply_chain_news.py
依國家取得可能影響銷售或出貨的即時新聞，供供應鏈地圖頁面使用。
支援 GNews API（需 API Key）與 Google News RSS 備援（免 Key）。
"""

import os
import re
import sqlite3
from datetime import datetime, timedelta
import email.utils
from typing import List, Optional
from urllib.parse import quote_plus

from .access_control import RISK_WORKSPACE_WRITE, require_capability

# 國家名稱 → 英文搜尋用 / 雙碼（給 GNews API 用）
COUNTRY_MAP = {
    "台灣": ("Taiwan", "TW"),
    "日本": ("Japan", "JP"),
    "美國": ("United States", "US"),
    "越南": ("Vietnam", "VN"),
    "德國": ("Germany", "DE"),
    "中國": ("China", "CN"),
    "南韓": ("South Korea", "KR"),
    "新加坡": ("Singapore", "SG"),
    "泰國": ("Thailand", "TH"),
    "馬來西亞": ("Malaysia", "MY"),
    "印尼": ("Indonesia", "ID"),
    "菲律賓": ("Philippines", "PH"),
    "印度": ("India", "IN"),
    "英國": ("United Kingdom", "GB"),
    "法國": ("France", "FR"),
    "荷蘭": ("Netherlands", "NL"),
    "澳洲": ("Australia", "AU"),
    "加拿大": ("Canada", "CA"),
    "墨西哥": ("Mexico", "MX"),
}


def _get_db():
    from .database import DB_FILE
    return DB_FILE


def _get_gnews_api_key() -> Optional[str]:
    """從環境變數或 Streamlit secrets 取得 GNews API Key（選填）。"""
    if os.getenv("ERP_ISOLATED_TEST") == "1":
        return None
    key = os.environ.get("GNEWS_API_KEY", "").strip()
    if key:
        return key
    try:
        import streamlit as st
        if hasattr(st, "secrets") and st.secrets.get("gnews", {}).get("api_key"):
            return st.secrets["gnews"]["api_key"]
        if hasattr(st, "secrets") and st.secrets.get("news", {}).get("api_key"):
            return st.secrets["news"]["api_key"]
    except Exception:
        pass
    return None


def _fetch_via_gnews_api(country_name: str, api_key: str, max_results: int = 10, within_days: int = 7) -> List[dict]:
    """使用 GNews API v4 取得新聞（需 API Key），具備時間篩選。"""
    try:
        import requests
    except ImportError:
        return []
    _name_en, code = COUNTRY_MAP.get(country_name, (country_name, None))
    # 地區由 GNews 的 country 參數篩選；不要把國名放進 q，否則搜尋會
    # 要求文章正文同時包含國名與供應鏈詞，容易在短時間窗內得到 0 筆。
    query = "supply chain OR logistics OR shipping OR export OR tariff OR strike OR port OR shortage OR disruption"
    url = "https://gnews.io/api/v4/search"

    # 產出 GNews API 格式的時間 (YYYY-MM-DDTHH:mm:SSZ)
    from_date = (datetime.now() - timedelta(days=within_days)).strftime("%Y-%m-%dT00:00:00Z")

    params = {
        "q": query,
        "max": max_results,
        "apikey": api_key,
        "lang": "en",
        "from": from_date,
    }
    if code:
        params["country"] = code.lower()
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        articles = data.get("articles") or []
        out = []
        for it in articles:
            # 標準化 GNews API 日期 (YYYY-MM-DDTHH:mm:SSZ -> YYYY-MM-DD HH:MM)
            pub_at = it.get("publishedAt", "")
            if pub_at and "T" in pub_at:
                pub_at = pub_at.replace("T", " ").replace("Z", "")[:16]
            out.append({
                "country": country_name,
                "region": None,
                "title": (it.get("title") or "").strip(),
                "summary": (it.get("description") or it.get("content") or "").strip()[:500],
                "url": (it.get("url") or "").strip(),
                "source": (it.get("source", {}).get("name") or "GNews API").strip(),
                "published_at": pub_at,
                "relevance_tag": "supply_chain",
            })
        return out
    except Exception as exc:
        raise RuntimeError("News provider request failed") from exc


def _fetch_via_rss(country_name: str, max_results: int = 15, within_days: int = 7) -> List[dict]:
    """使用 Google News RSS 取得新聞（免 API Key），支援時間篩選。"""
    name_en = COUNTRY_MAP.get(country_name, (country_name,))[0]
    # Google News RSS 支援 when:[N]d 語法，加入 OR 運算子以擴大搜尋範圍 (避免括號可能導致的解析問題)
    query = f"{name_en} supply chain OR {name_en} logistics OR {name_en} port OR {name_en} strike when:{within_days}d"
    q_enc = quote_plus(query)
    url = f"https://news.google.com/rss/search?q={q_enc}&hl=en-US&gl=US&ceid=US:en"
    out = []
    try:
        import xml.etree.ElementTree as ET
        from urllib.request import urlopen, Request
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ERP-Bot/1.0)"})
        with urlopen(req, timeout=15) as resp:
            tree = ET.parse(resp)
        root = tree.getroot()
        channel = root.find("channel")
        if channel is None:
            return []
        items = list(channel.findall("item"))[:max_results]
        for item in items:
            title = item.find("title").text if item.find("title") is not None else ""
            link = item.find("link").text if item.find("link") is not None else ""
            desc_el = item.find("description")
            summary = (desc_el.text or "") if desc_el is not None else ""
            if summary:
                summary = re.sub(r"<[^>]+>", "", summary)[:500]
            pub_date_raw = item.find("pubDate").text if item.find("pubDate") is not None else ""

            # 標準化日期格式 (RFC 2822 -> ISO)
            pub_date_iso = ""
            try:
                if pub_date_raw:
                    dt = email.utils.parsedate_to_datetime(pub_date_raw)
                    pub_date_iso = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

            out.append({
                "country": country_name,
                "region": None,
                "title": title.strip(),
                "summary": summary.strip(),
                "url": link.strip(),
                "source": "Google News RSS",
                "published_at": pub_date_iso or pub_date_raw,
                "relevance_tag": "supply_chain",
            })
        return out
    except Exception as exc:
        raise RuntimeError("News provider request failed") from exc


def fetch_country_news(country_name: str, api_key: Optional[str] = None, max_results: int = 10, within_days: int = 7) -> List[dict]:
    """
    取得指定國家可能影響銷售或出貨的即時新聞。
    若有 GNews API Key 則優先使用 API，否則使用 Google News RSS。
    """
    if os.getenv("ERP_ISOLATED_TEST") == "1":
        from .isolated_runtime import fixture_news
        return fixture_news(country_name)[:max_results]
    if api_key:
        try:
            items = _fetch_via_gnews_api(country_name, api_key, max_results, within_days)
            if items:
                return items
        except RuntimeError:
            pass
    return _fetch_via_rss(country_name, max_results, within_days)


def save_news_to_db(items: List[dict]) -> int:
    """Retain raw items once; analysis fields are stored separately."""
    from .news_store import store_raw, store_analysis
    added = 0
    with sqlite3.connect(_get_db()) as conn:
        for item in items:
            news_id, created = store_raw(conn, item)
            added += created
            if "analysis_status" in item:
                store_analysis(conn, news_id, item)
    return added


def get_news_from_db(country=None, limit=50, order_by_latest=True, within_days=None,
                     *, analyzed_only=False) -> List[dict]:
    """Raw content plus explicit analysis fields. Only successful relevant rows feed AI."""
    clauses, params = [], []
    if country:
        clauses.append("country=?")
        params.append(country)
    if within_days is not None and within_days > 0:
        clauses.append("date(COALESCE(NULLIF(published_at,''),fetched_at)) >= date('now',?)")
        params.append(f"-{int(within_days)} days")
    if analyzed_only:
        clauses.append("analysis_status='succeeded' AND is_relevant=1")
    order = "DESC" if order_by_latest else "ASC"
    with sqlite3.connect(_get_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT * FROM supply_chain_news WHERE {' AND '.join(clauses) or '1=1'} ORDER BY COALESCE(NULLIF(published_at,''),fetched_at) {order}, id {order} LIMIT ?", (*params, limit)).fetchall()
    return [dict(row) for row in rows]


def refresh_news_for_countries(countries, gemini_api_key=None, gnews_api_key=None,
    max_per_country=15, within_days=7, gemini_model="gemini-2.5-flash", *, actor=None):
    from .job_lock import exclusive_job_lock
    require_capability(actor, RISK_WORKSPACE_WRITE)
    with exclusive_job_lock(_get_db(), "news") as acquired:
        if not acquired:
            return {"status": "busy", "saved_count": 0, "updated": 0}
        return _refresh(countries, gnews_api_key, max_per_country, within_days, actor)


def _refresh(countries, gnews_api_key, max_per_country, within_days, actor):
    from .news_store import store_raw, store_analysis
    from .supply_chain_risk import batch_infer_affected_region_from_news
    from .llm_client import llm_available
    from .risk_validation import failed_analysis
    from .region_matching import normalize
    countries = list(dict.fromkeys(normalize(c) for c in countries if str(c or "").strip()))
    ai_enabled = llm_available()
    g_key = gnews_api_key or _get_gnews_api_key()
    result = dict(status="succeeded", fetched_count=0, saved_count=0, updated=0,
                  duplicate_count=0, analyzed_count=0, failed_count=0, pending_count=0,
                  filtered_count=0, fetch_failed_count=0, by_country={}, used_api=bool(g_key))
    processed = set()
    import concurrent.futures
    fetched = {}
    if countries:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(countries))) as pool:
            jobs = {pool.submit(fetch_country_news, c, api_key=g_key, max_results=max_per_country, within_days=within_days): c for c in countries}
            for future in concurrent.futures.as_completed(jobs):
                c = jobs[future]
                try:
                    fetched[c] = future.result()
                except Exception:
                    result["fetch_failed_count"] += 1
                    result["by_country"][c] = 0
    for country in countries:
        items = fetched.get(country, [])
        result["fetched_count"] += len(items)
        pending = []
        result["by_country"][country] = 0
        require_capability(actor, RISK_WORKSPACE_WRITE)
        with sqlite3.connect(_get_db()) as conn:
            for item in items:
                news_id, created = store_raw(conn, item)
                result["saved_count"] += int(created)
                result["by_country"][country] += int(created)
                result["duplicate_count"] += int(not created)
                status = conn.execute("SELECT analysis_status FROM supply_chain_news WHERE id=?", (news_id,)).fetchone()[0]
                if news_id not in processed and status != "succeeded":
                    raw = conn.execute("SELECT title,summary FROM supply_chain_news WHERE id=?", (news_id,)).fetchone()
                    pending.append((news_id, f"{raw[0] or ''}\n{raw[1] or ''}"))
                processed.add(news_id)
        if not ai_enabled:
            result["pending_count"] += len(pending)
            continue
        if pending:
            inferred = batch_infer_affected_region_from_news(news_texts=[p[1] for p in pending])
            require_capability(actor, RISK_WORKSPACE_WRITE)
            with sqlite3.connect(_get_db()) as conn:
                for i, (news_id, _) in enumerate(pending):
                    analysis = inferred[i] if i < len(inferred) else failed_analysis("missing_result")
                    store_analysis(conn, news_id, analysis)
                    if analysis.get("analysis_status") == "succeeded":
                        result["analyzed_count"] += 1
                        result["filtered_count"] += int(analysis["is_relevant"] is False)
                    else:
                        result["failed_count"] += 1
    # Include retained failures/pending rows even if the next provider fetch omits them.
    with sqlite3.connect(_get_db()) as conn:
        exclusions = ",".join("?" for _ in processed) or "NULL"
        clause = f"AND id NOT IN ({exclusions})" if processed else ""
        backlog = conn.execute(f"SELECT id,title,summary FROM supply_chain_news WHERE analysis_status IN ('pending','failed') {clause} ORDER BY COALESCE(analyzed_at,fetched_at),id LIMIT 100", sorted(processed)).fetchall()
    if ai_enabled and backlog:
        require_capability(actor, RISK_WORKSPACE_WRITE)
        inferred = batch_infer_affected_region_from_news(news_texts=[f"{r[1] or ''}\n{r[2] or ''}" for r in backlog])
        require_capability(actor, RISK_WORKSPACE_WRITE)
        with sqlite3.connect(_get_db()) as conn:
            for i, row in enumerate(backlog):
                analysis = inferred[i] if i < len(inferred) else failed_analysis("missing_result")
                store_analysis(conn, row[0], analysis)
                result["analyzed_count" if analysis.get("analysis_status") == "succeeded" else "failed_count"] += 1
    with sqlite3.connect(_get_db()) as conn:
        result["remaining_analysis_count"] = conn.execute("SELECT COUNT(*) FROM supply_chain_news WHERE analysis_status IN ('pending','failed')").fetchone()[0]
    result["updated"] = result["saved_count"]
    if result["failed_count"] or result["fetch_failed_count"]:
        result["status"] = "partial_failure"
    elif result["remaining_analysis_count"]:
        result["status"] = "pending_analysis"
    if ai_enabled:
        from .supply_chain_risk import get_heatmap_ai_analysis, apply_heatmap_updates, build_heatmap_review_rows, get_risk_heatmap_data
        eligible = get_news_from_db(limit=25, within_days=30, analyzed_only=True)
        result["heatmap_status"] = "no_valid_news"
        if eligible:
            context = "\n".join(f"{n['title']} {n.get('analysis_summary') or ''} [delay={n.get('estimated_delay')}]" for n in eligible)
            heatmap = get_heatmap_ai_analysis(news_context=context, reference_date=datetime.now().strftime("%Y-%m-%d"))
            summary, updates, events = heatmap["summary"], heatmap["updates"], heatmap["events"]
            if heatmap["analysis_status"] != "succeeded":
                result["heatmap_status"] = "failed"
                result["status"] = "partial_failure"
            else:
                review = build_heatmap_review_rows(updates, events, get_risk_heatmap_data())
                apply_heatmap_updates([dict(display_name=r["地區"],risk_pct=r["預估風險 (%)"],estimated_delay=r["預估延遲 (天)"]) for r in review], summary, actor=actor)
                result["heatmap_status"] = "succeeded"
    return result
