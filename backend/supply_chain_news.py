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
    name_en, code = COUNTRY_MAP.get(country_name, (country_name, None))
    # 關鍵字：擴充相關範疇確保不漏抓
    query = f"{name_en} (supply chain OR logistics OR shipping OR export OR tariff OR strike OR port OR pandemic OR war OR shortage OR conflict OR disruption OR natural disaster)"
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
        params["country"] = code
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
    except Exception:
        return []


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
    except Exception:
        return []


def fetch_country_news(country_name: str, api_key: Optional[str] = None, max_results: int = 10, within_days: int = 7) -> List[dict]:
    """
    取得指定國家可能影響銷售或出貨的即時新聞。
    若有 GNews API Key 則優先使用 API，否則使用 Google News RSS。
    """
    if api_key:
        items = _fetch_via_gnews_api(country_name, api_key, max_results, within_days)
        if items:
            return items
    return _fetch_via_rss(country_name, max_results, within_days)


def save_news_to_db(items: List[dict]) -> int:
    """將新聞寫入 supply_chain_news 表。"""
    if not items:
        return 0
    db = _get_db()
    conn = sqlite3.connect(db)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    n = 0
    for it in items:
        # 只存入相關的新聞 (Filter irrelevant already done in refresh_news or here)
        if not it.get("is_relevant", True):
            continue
        try:
            conn.execute(
                """INSERT INTO supply_chain_news (country, region, title, summary, url, source, published_at, relevance_tag, fetched_at, category, is_relevant, estimated_delay)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    it.get("country") or "",
                    it.get("region"),
                    (it.get("title") or "")[:500],
                    (it.get("summary") or "")[:1000],
                    (it.get("url") or "")[:500],
                    (it.get("source") or "")[:100],
                    it.get("published_at"),
                    it.get("relevance_tag"),
                    now,
                    it.get("category"),
                    1 if it.get("is_relevant", True) else 0,
                    it.get("estimated_delay") or 0,
                ),
            )
            n += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    return n


def get_news_from_db(
    country: Optional[str] = None,
    limit: int = 50,
    order_by_latest: bool = True,
    within_days: Optional[int] = None,
) -> List[dict]:
    """從資料庫讀取已快取的新聞。order_by_latest=True 依發布/取得時間取最近最新；within_days=30 僅取近 N 天內。"""
    db = _get_db()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    order = "ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC LIMIT ?"
    date_filter = ""
    params_where = []
    if within_days is not None and within_days > 0:
        date_filter = " AND date(COALESCE(published_at, fetched_at)) >= date('now', ?) "
        params_where.append(f"-{int(within_days)} days")
    if country:
        params = [country] + params_where + [limit]
        rows = conn.execute(
            f"""SELECT id, country, region, title, summary, url, source, published_at, relevance_tag, fetched_at, category, estimated_delay
               FROM supply_chain_news WHERE country = ?{date_filter}{order}""",
            params,
        ).fetchall()
    else:
        params = params_where + [limit]
        rows = conn.execute(
            f"""SELECT id, country, region, title, summary, url, source, published_at, relevance_tag, fetched_at, category, estimated_delay
               FROM supply_chain_news WHERE 1=1{date_filter}{order}""",
            params,
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def refresh_news_for_countries(
    countries: List[str],
    gemini_api_key: Optional[str] = None,
    gnews_api_key: Optional[str] = None,
    max_per_country: int = 15,
    within_days: int = 7,
    gemini_model: str = "gemini-2.5-flash",
    *,
    actor: str | None = None,
) -> dict:
    """
    為多個國家平行抓取新聞，並使用批量 AI 歸類以極大化提升效能。
    """
    require_capability(actor, RISK_WORKSPACE_WRITE)
    import concurrent.futures
    from .supply_chain_risk import batch_infer_affected_region_from_news
    from .llm_client import llm_available

    # issue #27：AI 歸類/熱圖摘要改由 .env 模型設定驅動（gemini_api_key 參數棄用）
    ai_enabled = llm_available()
    g_key = gnews_api_key or _get_gnews_api_key()
    used_gnews = bool(g_key)
    by_country = {}
    total_saved = 0
    total_fetched = 0

    # 1. 平行抓取各國原始新聞 (I/O Bound)
    def fetch_job(c):
        return c, fetch_country_news(c, api_key=g_key, max_results=max_per_country, within_days=within_days)

    all_raw_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(countries), 10)) as executor:
        futures = [executor.submit(fetch_job, c) for c in countries]
        for future in concurrent.futures.as_completed(futures):
            country, items = future.result()
            if items:
                total_fetched += len(items)
                all_raw_items.append((country, items))

    # 2. 批量進行 AI 分析
    if ai_enabled:
        for country, items in all_raw_items:
            texts = [f"{it.get('title', '')}\n{it.get('summary', '')}" for it in items]
            inferred_list = batch_infer_affected_region_from_news(news_texts=texts)
            
            relevant_items = []
            for it, inferred in zip(items, inferred_list):
                # 如果不相關，或者 AI 推估延遲為 0 天，則視為無影響而不抓取
                if not inferred.get("is_relevant", True) or int(inferred.get("estimated_delay") or 0) <= 0:
                    continue
                
                it["is_relevant"] = True
                it["category"] = inferred.get("event_type", "其他")
                it["estimated_delay"] = int(inferred.get("estimated_delay") or 0)
                
                if inferred.get("country"):
                    it["country"] = inferred["country"]
                if inferred.get("region"):
                    it["region"] = inferred["region"]
                if inferred.get("chinese_summary"):
                    it["summary"] = inferred["chinese_summary"]
                relevant_items.append(it)
            
            n = save_news_to_db(relevant_items)
            by_country[country] = n
            total_saved += n
    else:
        # 無 API Key 時僅存入
        for country, items in all_raw_items:
            n = save_news_to_db(items)
            by_country[country] = n
            total_saved += n
    
    # 進行熱圖自動更新 (AI Heatmap Update)
    if ai_enabled:
        try:
            from .supply_chain_risk import get_heatmap_ai_summary, apply_heatmap_updates
            all_news = get_news_from_db(limit=25, order_by_latest=True, within_days=30)
            news_context = "\n".join([
                f"{(n.get('title') or '')} {(n.get('summary') or '')[:150]} [{n.get('published_at') or n.get('fetched_at') or ''}]"
                for n in all_news
            ])
            ref_date = datetime.now().strftime("%Y-%m-%d")
            summary_text, updates, _ = get_heatmap_ai_summary(news_context=news_context, reference_date=ref_date)
            if updates:
                apply_heatmap_updates(updates, summary_text, actor=actor)
        except PermissionError:
            raise
        except Exception:
            pass

    return {
        "updated": total_saved, 
        "fetched_count": total_fetched,
        "saved_count": total_saved,
        "filtered_count": total_fetched - total_saved,
        "by_country": by_country, 
        "used_api": used_gnews
    }
