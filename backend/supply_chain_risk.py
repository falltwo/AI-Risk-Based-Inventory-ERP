"""
backend/supply_chain_risk.py
供應鏈與風險 — 後端邏輯
職責：供應鏈地圖資料、風險事件與交期、風險係數管理、風險報告產出
"""

import sqlite3
import re
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Optional, Any
from backend.database import DB_FILE, run_query
from backend.access_control import (
    ERP_POLICY_WRITE,
    RISK_WHAT_IF_RUN,
    RISK_WORKSPACE_WRITE,
    require_capability,
)
from backend.prompts import (
    HEATMAP_AI_SUMMARY_PROMPT_V2,
    BATCH_INFER_WITH_PRECEDENTS_PROMPT,
    PO_ALTERNATIVE_SUGGESTION_PROMPT,
    WHAT_IF_SYSTEM_PROMPT,
    WHAT_IF_USER_PROMPT
)


# ── 供應鏈地圖 ────────────────────────────────────────────────────────
# 經緯度僅在後端使用（地圖繪圖），前端不呈現；若 DB 為空則依國家代碼由後端帶入預設座標。

_COUNTRY_DEFAULT_COORDS = {
    "台灣": (25.0330, 121.5654),
    "日本": (35.6895, 139.6917),
    "美國": (37.7749, -122.4194),
    "德國": (48.1351, 11.5820),
    "越南": (21.0285, 105.8542),
    "墨西哥": (23.6345, -102.5528),
    "中國": (39.9042, 116.4074),
    "南韓": (37.5665, 126.9780),
    "泰國": (13.7563, 100.5018),
    "新加坡": (1.3521, 103.8198),
}

from .region_matching import (
    REGION_COUNTRY_MAP, matches_location, split_location, connect_db, normalize,
    expanded_region_where as _get_expanded_region_where,
)
from .risk_validation import number, text, json_payload, failed_analysis, parse_news_batch, EVENT_TYPES
from .risk_intelligence import save_ai_risk_summary, get_latest_ai_risk_summary, build_risk_evidence, gate_by_evidence


def _fill_coords_from_country(df, country_col="country", lat_col="latitude", lon_col="longitude"):
    """若經緯度為空但有國家，由後端依國家帶入預設座標（僅後端使用，前端不顯示經緯度欄位）。"""
    if df is None or df.empty or country_col not in df.columns:
        return df
    df = df.copy()
    if lat_col not in df.columns:
        df[lat_col] = pd.NA
    if lon_col not in df.columns:
        df[lon_col] = pd.NA
    for idx, row in df.iterrows():
        if pd.isna(row.get(lat_col)) or pd.isna(row.get(lon_col)):
            country = normalize(row.get(country_col))
            if country and country in _COUNTRY_DEFAULT_COORDS:
                lat, lon = _COUNTRY_DEFAULT_COORDS[country]
                df.at[idx, lat_col], df.at[idx, lon_col] = lat, lon
    return df


def get_suppliers_for_map():
    """取得正式供應商清單（含經緯度、國家、地區、風險等級），供地圖與清單使用。經緯度僅後端使用。"""
    conn = connect_db(DB_FILE)
    df = __pd_read("SELECT supplier_id, name, country, region, latitude, longitude, risk_level FROM suppliers WHERE is_official=1", conn)
    conn.close()
    return _fill_coords_from_country(df)


def get_customers_for_map():
    """取得客戶清單（含經緯度、國家、地區、風險等級），供地圖與清單使用。經緯度僅後端使用。"""
    conn = connect_db(DB_FILE)
    try:
        df = __pd_read("SELECT customer_id, name, country, region, latitude, longitude, risk_level FROM customers", conn)
    except Exception:
        df = __empty_df()
    conn.close()
    return _fill_coords_from_country(df)


from .risk_contract import valid_event_sql

_VALID_EVENT_SOURCE = valid_event_sql()


def get_recent_events_for_delay(limit=50):
    """取得近期供應鏈事件，供地圖判定出貨延遲狀況。"""
    conn = connect_db(DB_FILE)
    df = __pd_read(
        f"SELECT event_type, region, country, impact_days, created_at, news_id FROM supply_chain_events WHERE {_VALID_EVENT_SOURCE} ORDER BY COALESCE(created_at, '') DESC, id DESC LIMIT ?",
        conn,
        params=(limit,),
    )
    conn.close()
    return df


# ── 熱圖事件加權 ──────────────────────────────────────────────────────
# 原本「只要有任何事件就 +40」會讓每個有事件的據點都停在 60%，看不出差異。
# 改為：依「最嚴重事件的延遲天數」給分，多筆事件再加成，逾期事件減半。
HEATMAP_BASE_RISK = 20.0
HEATMAP_EVENT_POINTS = ((30, 50), (14, 40), (7, 30), (1, 20), (0, 10))  # (延遲天數下限, 加權)
HEATMAP_EXTRA_EVENT_BONUS = 5      # 每多一筆事件
HEATMAP_EXTRA_EVENT_CAP = 15
HEATMAP_EVENT_STALE_DAYS = 30      # 登錄超過此天數的事件加權減半
HEATMAP_EVENT_LOOKBACK = 200       # 參與計算的事件筆數上限（依登錄時間新→舊）


def _event_points(impact_days) -> float:
    try:
        days = max(0, int(impact_days or 0))
    except (TypeError, ValueError):
        days = 0
    for floor, points in HEATMAP_EVENT_POINTS:
        if days >= floor:
            return float(points)
    return 0.0


def _clean_text(value) -> str:
    """DataFrame 的 NaN／None 一律視為空字串（str(nan) 會變成 "nan" 而誤判為有值）。"""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.casefold() in {"nan", "none"} else text


def _event_matches_location(ev, country, region):
    return matches_location(country, region, _clean_text(ev.get("region")), _clean_text(ev.get("country")))


def score_region_events(country: str, region: str, events, *, now=None) -> dict:
    """算出單一據點的事件加權與可讀理由。

    回傳 {"points", "count", "max_days", "reason"}；events 可為 DataFrame 或 list[dict]。
    """
    if events is None:
        rows = []
    elif hasattr(events, "iterrows"):
        rows = [r.to_dict() for _, r in events.iterrows()]
    else:
        rows = list(events)
    matched = [ev for ev in rows if _event_matches_location(ev, country or "", region or "")]
    if not matched:
        return {"points": 0.0, "count": 0, "max_days": 0, "reason": "近期無登錄事件"}

    reference = now or datetime.now()
    best = 0.0
    max_days = 0
    stale = 0
    for ev in matched:
        points = _event_points(ev.get("impact_days"))
        try:
            max_days = max(max_days, int(ev.get("impact_days") or 0))
        except (TypeError, ValueError):
            pass
        created = str(ev.get("created_at") or "")[:10]
        try:
            age = (reference - datetime.strptime(created, "%Y-%m-%d")).days
        except ValueError:
            age = 0
        if age > HEATMAP_EVENT_STALE_DAYS:
            points *= 0.5
            stale += 1
        best = max(best, points)
    bonus = min(HEATMAP_EXTRA_EVENT_CAP, HEATMAP_EXTRA_EVENT_BONUS * (len(matched) - 1))
    reason = f"{len(matched)} 則事件・最長延遲 {max_days} 天"
    if stale:
        reason += f"（{stale} 則已逾 {HEATMAP_EVENT_STALE_DAYS} 天）"
    return {"points": best + bonus, "count": len(matched), "max_days": max_days, "reason": reason}


def get_region_procurement_share():
    """依地區彙總採購金額，計算各地區採購佔比（該地區供應商之採購額 / 全公司採購額）。
    回傳 list of dict: region_key, display_name, procurement_ratio (0~1), total_amount, supplier_count。
    用於初始熱圖：採購佔比愈高，集中度風險愈高，可對應風險低/中/高。"""
    conn = connect_db(DB_FILE)
    total = __pd_read(
        "SELECT COALESCE(SUM(total_amount), 0) as tot FROM purchase_orders WHERE total_amount IS NOT NULL AND total_amount > 0",
        conn,
    )
    global_sum = float(total["tot"].iloc[0]) if total is not None and not total.empty else 0
    if global_sum <= 0:
        conn.close()
        return {}
    df = __pd_read(
        """SELECT s.country, s.region, SUM(COALESCE(p.total_amount, 0)) as amt, COUNT(DISTINCT s.supplier_id) as cnt
           FROM suppliers s
           LEFT JOIN purchase_orders p ON s.supplier_id = p.supplier_id AND p.total_amount IS NOT NULL AND p.total_amount > 0
           WHERE (s.country IS NOT NULL AND s.country != '') OR (s.region IS NOT NULL AND s.region != '')
           GROUP BY s.country, s.region""",
        conn,
    )
    conn.close()
    if df is None or df.empty:
        return {}
    out = {}
    for _, r in df.iterrows():
        country = (r.get("country") or "").strip() or "未填"
        region = (r.get("region") or "").strip() or country
        key = f"{country}|{region}"
        amt = float(r.get("amt") or 0)
        ratio = amt / global_sum
        out[key] = {
            "region_key": key,
            "display_name": f"{country} {region}".strip(),
            "procurement_ratio": round(ratio, 4),
            "total_amount": amt,
            "supplier_count": int(r.get("cnt") or 0),
        }
    return out


# ── 即時風險熱圖 (Risk Heatmap) ─────────────────────────────────────────
# 各國／各地區風險% 來源（優先順序）：
# 1. risk_heatmap 表：若已有資料則直接使用（可被「產生即時風險摘要」的 AI 更新）
# 2. 初始熱圖推算：依「供應商據點」+「風險事件」+「風險係數(region)」計算（見下方）
# 3. 供應商／客戶的 risk_level（高/中/低）為手動欄位，在採購／銷售管理維護
#
# 【初始熱圖風險值統計方式】
# - 基礎值：每個據點預設 20%。
# - 風險事件加權：若「風險事件與交期」中有登錄事件，且事件的地區/國家涵蓋該據點，則 +40%（上限 100%）。
# - 地區係數：若「風險係數管理」有設定類型=地區(region)的係數（如東亞 60、日本 40、越南 55），
#   則該據點的風險% = max(上述計算值, 該地區係數)，即取「事件加權後」與「地區係數」較高者。
# - 熱點來源：僅從「供應商」的國家/地區去重後產生，每個 (國家|地區) 一筆，經緯度取自該區任一台供應商。
# - 採購佔比：系統自動彙總該地區所有供應商的採購金額佔比；佔比愈高視為集中度風險愈高，對應風險低/中/高（見 get_region_procurement_share）。
#
# 【廣域地區對應】當 AI 建議的更新地區為廣域名稱（如「亞洲」）時，
#   apply_heatmap_updates 需將其對應到該區所有國家之熱點一併更新。
def get_risk_heatmap_data():
    """
    取得熱圖資料：永遠以「供應商據點」為基礎產出完整熱點清單，再以 risk_heatmap 表覆寫風險%與摘要。
    如此手動或 AI 更新單一熱點時，其他未調節的熱點仍會保留在地圖上。
    """
    # 1. 永遠先依供應商據點算出「預設」熱點清單
    suppliers = get_suppliers_for_map()
    if suppliers is None or suppliers.empty:
        return []
    events = get_recent_events_for_delay(HEATMAP_EVENT_LOOKBACK)
    region_scores = get_region_risk_scores()
    procurement_by_region = get_region_procurement_share()
    seen = set()
    default_rows = []
    for _, s in suppliers.iterrows():
        country = (s.get("country") or "").strip() or "未填"
        region = (s.get("region") or "").strip() or country
        key = f"{country}|{region}"
        if key in seen:
            continue
        seen.add(key)
        event_score = score_region_events(country, region, events)
        risk = min(100.0, HEATMAP_BASE_RISK + event_score["points"])
        reasons = [event_score["reason"]]
        for k, v in region_scores.items():
            if matches_location(country, region, k):
                if v > risk:
                    reasons.append(f"地區係數 {k} {v:.0f}%")
                risk = max(risk, min(100, v))
        if key in procurement_by_region:
            ratio = procurement_by_region[key]["procurement_ratio"]
            if ratio >= 0.35:
                floor = 70
            elif ratio >= 0.15:
                floor = 45
            else:
                floor = min(35, 20 + ratio * 100)
            if floor > risk:
                reasons.append(f"採購集中度 {ratio:.0%}")
            risk = max(risk, floor)
        lat, lon = s.get("latitude"), s.get("longitude")
        if lat is None or lon is None:
            continue
        default_rows.append({
            "region_key": key,
            "display_name": f"{country} {region}".strip(),
            "latitude": float(lat),
            "longitude": float(lon),
            "risk_pct": round(risk, 1),
            "risk_reason": "；".join(reasons),
            "event_count": event_score["count"],
            "event_max_days": event_score["max_days"],
            "ai_summary": None,
            "updated_at": None,
            "estimated_delay": None,
        })
    # 2. 讀取 DB 中手動/AI 覆寫的風險%與摘要，依 region_key 覆蓋到預設清單
    conn = connect_db(DB_FILE)
    df = __pd_read(
        "SELECT region_key, display_name, latitude, longitude, risk_pct, ai_summary, updated_at, estimated_delay FROM risk_heatmap",
        conn,
    )
    conn.close()
    overrides = {}
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            k = (r.get("region_key") or "").strip()
            if k:
                overrides[k] = {
                    "risk_pct": r.get("risk_pct"),
                    "ai_summary": r.get("ai_summary"),
                    "updated_at": r.get("updated_at"),
                    "estimated_delay": None if pd.isna(r.get("estimated_delay")) else r.get("estimated_delay"),
                    "latitude": r.get("latitude"),
                    "longitude": r.get("longitude"),
                }
    # 3. 合併：預設熱點 + 有覆寫則用覆寫的 risk_pct / ai_summary
    out = []
    for row in default_rows:
        rk = row["region_key"]
        if rk in overrides:
            o = overrides[rk]
            overridden = o.get("risk_pct") is not None
            out.append({
                "region_key": rk,
                "display_name": row["display_name"],
                "latitude": o.get("latitude") if o.get("latitude") is not None else row["latitude"],
                "longitude": o.get("longitude") if o.get("longitude") is not None else row["longitude"],
                "risk_pct": o.get("risk_pct") if overridden else row["risk_pct"],
                "risk_reason": (f"AI／人工設定（{o.get('updated_at') or '時間未記錄'}）" if overridden
                                else row["risk_reason"]),
                "event_count": row["event_count"],
                "event_max_days": row["event_max_days"],
                "ai_summary": o.get("ai_summary"),
                "updated_at": o.get("updated_at"),
                "estimated_delay": o.get("estimated_delay"),
            })
        else:
            out.append(row)
    return out


def upsert_risk_heatmap(
    region_key, display_name, latitude, longitude, risk_pct, ai_summary=None, *, actor=None
):
    """新增或更新一筆熱圖熱點。"""
    require_capability(actor, RISK_WORKSPACE_WRITE)
    risk_pct = number(risk_pct, maximum=100)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = connect_db(DB_FILE)
    conn.execute(
        """INSERT INTO risk_heatmap (region_key, display_name, latitude, longitude, risk_pct, ai_summary, updated_at)
           VALUES (?,?,?,?,?,?,?) ON CONFLICT(region_key) DO UPDATE SET
           display_name=excluded.display_name, latitude=excluded.latitude, longitude=excluded.longitude,
           risk_pct=excluded.risk_pct, ai_summary=excluded.ai_summary, updated_at=excluded.updated_at""",
        (region_key, display_name, latitude, longitude, risk_pct, ai_summary, now),
    )
    conn.commit()
    conn.close()


def reset_risk_heatmap_to_initial(*, actor=None):
    """清空 risk_heatmap 表，使熱圖還原為依供應商據點與風險事件計算的初始狀態。"""
    require_capability(actor, RISK_WORKSPACE_WRITE)
    conn = connect_db(DB_FILE)
    conn.execute("DELETE FROM risk_heatmap")
    conn.commit()
    conn.close()

# （issue #27）Gemini 模型探測器 get_working_model_id 已移除 ——
#   模型選擇統一由 .env（LLM_MODEL / LLM_ANALYSIS_MODEL）決定，
#   供應商層 fallback 由 backend/llm_client → agent_orchestrator._llm 處理。


def _gate_heatmap_updates(raw_updates, valid_list, name_expansions) -> list[dict]:
    """Only validated numeric suggestions matching an actual node are actionable."""
    if not isinstance(raw_updates, list):
        return []
    out = []
    for u in raw_updates:
        try:
            name = text(u.get("地區", u.get("display_name")))
            pct = number(u.get("風險", u.get("risk_pct")), maximum=100)
        except (AttributeError, TypeError, ValueError):
            continue
        for node in valid_list:
            c, r = split_location(node)
            if node == name or node in name_expansions.get(name, []) or matches_location(c or node, r, name):
                out.append({"display_name": node, "risk_pct": pct})
    return out


def _coerce_heatmap_events(raw_events) -> list[dict]:
    """Reject malformed events; null delay remains unknown and 0 stays zero."""
    if not isinstance(raw_events, list):
        return []
    out = []
    for e in raw_events:
        try:
            etype = text(e.get("類型", e.get("event_type")))
            if etype not in EVENT_TYPES:
                raise ValueError("Invalid event type")
            region = text(e.get("地區", e.get("region", "")))
            country = text(e.get("國家", e.get("country", "")))
            if not (region or country):
                raise ValueError("Missing geography")
            if "延遲天數" not in e and "impact_days" not in e:
                raise ValueError("Missing delay")
            days = number(e.get("延遲天數", e.get("impact_days")), maximum=365, integer=True, nullable=True)
            out.append(dict(event_type=etype, region=region, country=country,
                            impact_days=days, description=text(e.get("描述", e.get("description", "")))))
        except (AttributeError, TypeError, ValueError):
            continue
    return out


# ── AI 風險摘要：證據閘門 + 持久化 ───────────────────────────────────
# 摘要原本只活在 session_state：重新整理就消失、L1 看不到、排程產生的建議事件直接丟掉。
# 現在每次產生都寫進 risk_ai_summaries，L2 重開頁面與 L1 總覽都讀最新一筆。

AI_SUMMARY_TABLE = "risk_ai_summaries"
EVIDENCE_DAYS_MULTIPLIER = 2     # AI 建議延遲天數上限 = 證據最長天數 × 此倍率
EVIDENCE_DAYS_FLOOR = 7          # …但至少允許到這個天數（證據只有 1-2 天時仍可合理外推）


























def _summary_news_context(news_items):
    from .risk_contract import analyzed_news
    rows = [analyzed_news(n) for n in news_items or []]
    return "\n".join(f"{n.get('title') or ''} {n['summary']} [{n['country']} {n['region']}; 預估延遲: {n['estimated_delay'] if n['estimated_delay'] is not None else '未知'}天]" for n in rows if n is not None)


def analyze_heatmap_risk(
    news_items=None, *, news_context: str = "", reference_date: str | None = None,
    actor=None, persist: bool = True,
) -> dict:
    """AI 熱圖摘要（結構化）。

    - news_items：新聞列（含 country/region/category/estimated_delay）→ 同時當 prompt 素材與證據
    - news_context：舊介面的純文字素材（沒有 news_items 時使用；證據只剩已登錄事件）
    - actor 有給且 persist=True 時把結果寫進 risk_ai_summaries（需 RISK_WORKSPACE_WRITE）
    回傳 dict：summary / updates / events / audit / evidence_locations / generated_at /
              reference_date / news_count / event_count / summary_id / error
    """
    import json
    from backend.llm_client import complete_text

    from .risk_contract import analyzed_news
    news_items = [n for n in (news_items or []) if analyzed_news(n) is not None]
    reference_date = reference_date or datetime.now().strftime("%Y-%m-%d")
    events_df = get_active_risk_events()
    events_text = "目前尚無已登錄事件。"
    if events_df is not None and not events_df.empty:
        # 只列出最近的 15 筆事件作為背景
        # region 為 NaN 時 pandas 值為 truthy，原本會把字面 "nan" 餵給模型（模型真的回了「地區欄位為 nan」）
        events_text = "\n".join([
            f"- 【{_clean_text(row['event_type']) or '其他'}】區域："
            f"{' '.join(p for p in (_clean_text(row['country']), _clean_text(row['region'])) if p) or '未填'}"
            f" (預計延遲：{row['impact_days']}天)"
            for _, row in events_df.head(15).iterrows()
        ])

    conn = connect_db(DB_FILE)
    try:
        # 僅選取正式供應商 (is_official=1) 的據點，確保建議清單精確對齊
        valid_regions_df = pd.read_sql_query("SELECT DISTINCT country, region FROM suppliers WHERE is_official=1 AND country IS NOT NULL", conn)
        valid_regions = []
        valid_locations = []
        for _, r in valid_regions_df.iterrows():
            c = _clean_text(r['country'])
            rg = _clean_text(r['region'])
            valid_locations.append((c, rg))
            if rg and rg != c:
                valid_regions.append(f"{c} {rg}")
            else:
                valid_regions.append(c)
        valid_regions_text = "、".join(set(valid_regions)) or "（目前無正式供應商據點資料，請跳過風險建議清單）"
    except Exception:
        valid_locations = []
        valid_regions_text = "（系統讀取區域資料失敗，請跳過風險建議清單）"
    finally:
        conn.close()

    if news_items:
        news_context = _summary_news_context(news_items)
    prompt = HEATMAP_AI_SUMMARY_PROMPT_V2.format(
        reference_date=reference_date,
        events_text=events_text,
        valid_regions_text=valid_regions_text,
        news_context=news_context or "目前尚無快取新聞，請先於頁面「更新即時新聞」取得最近新聞後再產生摘要。"
    )
    # 合法區域清單與展開表（code-side gate 用；如「台灣」→「台灣 北區/中區/南區」）
    valid_list = [v.strip() for v in (valid_regions_text or "").split("、") if v.strip()]
    name_expansions = {}
    for country, region in valid_locations:
        name = f"{country} {region}" if region and region != country else country
        name_expansions.setdefault(country, []).append(name)

    evidence = build_risk_evidence(news_items, events_df)
    result = {
        "summary": "",
        "updates": [],
        "events": [],
        "audit": [],
        "evidence_locations": sorted(evidence["locations"]),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reference_date": reference_date,
        "news_count": len(news_items or []),
        "event_count": 0 if events_df is None else int(len(events_df)),
        "summary_id": None,
        "error": False, "analysis_status": "succeeded", "analysis_error": None,
        "sources": evidence["sources"], "actor": actor,
    }
    try:
        # issue #27/#47：統一 LLM 入口 + 結構化輸出（JSON）。
        # 合法區域檢核由 _gate_heatmap_updates 執行、證據檢核由 gate_by_evidence 執行，
        # prompt 只做平述引導。
        raw = (complete_text(prompt, temperature=0.3, json_mode=True,
                             tag="analysis:heatmap") or "").strip()
        if not raw:
            result.update(analysis_status="failed", analysis_error="empty_response", summary="AI 摘要失敗：模型未回傳內容。", error=True)
            return result
        payload = json_payload(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("摘要"), str) or not isinstance(payload.get("更新"), list) or not isinstance(payload.get("事件"), list):
            raise ValueError("Invalid heatmap response schema")

        for update in payload["更新"]:
            if not isinstance(update, dict) or not text(update.get("地區")):
                raise ValueError("Invalid heatmap update")
            number(update.get("風險"), maximum=100)
        if len(_coerce_heatmap_events(payload["事件"])) != len(payload["事件"]):
            raise ValueError("Invalid heatmap event")
        summary = text(payload["摘要"])
        if not summary:
            raise ValueError("Missing summary")
        updates = _gate_heatmap_updates(payload.get("更新"), valid_list, name_expansions)
        suggested_events = [e for e in _coerce_heatmap_events(payload.get("事件"))
                            if any(matches_location(c, r, e["region"], e["country"]) for c,r in valid_locations)]
        updates, suggested_events, audit = gate_by_evidence(updates, suggested_events, evidence)
        result["raw_summary"] = summary
        if audit:
            summary = "證據檢核後的風險摘要：\n" + "\n".join([f"- {u['display_name']}: {u['risk_pct']}%" for u in updates] + [f"- {e['country']} {e['region']}: {e['event_type']}，{e['impact_days']} 天" for e in suggested_events])
            if not updates and not suggested_events:
                summary += "沒有可套用的有效建議。"
        result.update({"summary": summary, "updates": updates, "events": suggested_events, "audit": audit,
                       # 模型可能想 1～2 分鐘，「產生時間」以回覆完成為準
                       "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    except Exception:
        result.update(summary="AI 摘要解析失敗：請稍後重試。", error=True, analysis_status="failed", analysis_error="invalid_output_or_provider_error", updates=[], events=[])
        return result

    if persist and actor:
        result["summary_id"] = save_ai_risk_summary(result, actor=actor)
    return result


def resolve_heatmap_updates(updates, heatmap_rows):
    """Resolve once for both UI preview and persistence; last matching update wins."""
    resolved = {}
    for u in updates:
        pct = number(u.get("risk_pct"), maximum=100)
        name = text(u.get("display_name"))
        for row in heatmap_rows:
            c, r = split_location(row["region_key"])
            if matches_location(c, r, name):
                value = dict(row, risk_pct=pct)
                if "estimated_delay" in u:
                    value["estimated_delay"] = number(u["estimated_delay"], maximum=365, integer=True, nullable=True)
                resolved[row["region_key"]] = value
    return list(resolved.values())


def build_heatmap_review_rows(updates, events, heatmap_rows):
    rows = []
    for row in resolve_heatmap_updates(updates, heatmap_rows):
        c, r = split_location(row["region_key"])
        days = row.get("estimated_delay")
        for event in events:
            if matches_location(c, r, event.get("region"), event.get("country")):
                days = event.get("impact_days")
                break
        rows.append({"套用": True, "地區": row["display_name"],
                     "預估風險 (%)": row["risk_pct"], "預估延遲 (天)": days})
    return rows


def apply_heatmap_updates(updates, ai_summary=None, *, actor=None, summary_result=None):
    """Atomically persist the exact reviewed risk AND delay per node."""
    require_capability(actor, RISK_WORKSPACE_WRITE)
    rows = resolve_heatmap_updates(updates or [], get_risk_heatmap_data())
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with connect_db(DB_FILE) as conn:
        for row in rows:
            conn.execute("""INSERT INTO risk_heatmap
                (region_key,display_name,latitude,longitude,risk_pct,ai_summary,updated_at,estimated_delay)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(region_key) DO UPDATE SET
                risk_pct=excluded.risk_pct,ai_summary=excluded.ai_summary,
                updated_at=excluded.updated_at,estimated_delay=excluded.estimated_delay""",
                (row["region_key"],row["display_name"],row["latitude"],row["longitude"],
                 row["risk_pct"],(ai_summary or "")[:500],now,row.get("estimated_delay")))
        if summary_result is not None:
            save_ai_risk_summary(summary_result, actor=actor, conn=conn)
    return len(rows)


def translate_to_chinese_traditional(api_key: str = "", text: str = "", model_name: str = "") -> str:
    """將文字翻譯為繁體中文；失敗回傳原文。（issue #27：api_key/model_name 參數棄用，.env 驅動）"""
    if not (text and str(text).strip()):
        return (text or "").strip()
    try:
        from backend.llm_client import complete_text
        out = complete_text(
            f"請將以下文字翻譯成繁體中文，保持專業語氣，只需回覆翻譯後的結果：\n\n{text}",
            tag="analysis:translate",
        )
        return (out or text).strip()
    except Exception:
        return (text or "").strip()


def generate_communication_draft(api_key: str = "", context: str = "", target_type: str = "", model_name: str = "") -> str:
    """由 AI 依據事件衝擊，為人員擬定應變信件草稿。（issue #27：api_key 參數棄用，.env 驅動）"""
    prompt = f"""
    你現在是一位專業的供應鏈經理。請根據以下背景資訊，擬定一份給「{target_type}」的應變溝通電郵草稿。
    
    **背景資訊**：
    {context}
    
    **要求**：
    1. 語氣專業、誠懇、冷靜、具有說服力。
    2. 請同時提供「英文版」與「繁體中文版」。
    3. 內容需包含事件概況、預期影響、以及後續的應變步驟或詢問。
    4. 信末需留出聯絡人資訊的佔位符。
    
    請先提供英文版，再提供中文版，兩者之間用分隔線分開。只需要回覆信件內容，不要有其他廢話。
    """
    
    try:
        from backend.llm_client import complete_text
        return (complete_text(prompt, tag="analysis:comm_draft") or "AI 無法產出內容").strip()
    except Exception as e:
        return f"AI 草稿生成失敗：{e}"




def get_region_exposure(region_key) -> dict:
    """單一據點的曝險資訊：未結採購單金額／張數 + 該區供應商數。

    「曝險金額」只算 status 不在 (已完成, 已取消) 的採購單；沒有採購單時金額為 0，
    前端應改顯示供應商家數而不是誤導的 $0。
    """
    conn = connect_db(DB_FILE)
    try:
        country, region = split_location(region_key)
        where_sub, params_sub = _get_expanded_region_where(region, country, prefix="s.")
        supplier_where = " AND ".join(where_sub) or "1=1"
        sup_row = conn.execute(
            f"""SELECT COUNT(*), COALESCE(SUM(CASE WHEN s.is_official=1 THEN 1 ELSE 0 END), 0)
                FROM suppliers s WHERE {supplier_where}""",
            tuple(params_sub),
        ).fetchone()
        po_row = conn.execute(
            f"""SELECT COUNT(p.po_id), COALESCE(SUM(p.total_amount), 0)
                FROM purchase_orders p
                JOIN suppliers s ON p.supplier_id = s.supplier_id
                WHERE (p.status IS NULL OR p.status NOT IN ('已完成','已取消'))
                  AND {supplier_where}""",
            tuple(params_sub),
        ).fetchone()
    finally:
        conn.close()
    return {
        "supplier_count": int(sup_row[0] or 0),
        "official_supplier_count": int(sup_row[1] or 0),
        "open_po_count": int(po_row[0] or 0),
        "open_po_amount": float(po_row[1] or 0),
    }


def get_total_impact_amount(region_key):
    """計算特定地區受波及的採購總金額 (美元)。"""
    conn = connect_db(DB_FILE)
    where = ["(p.status IS NULL OR p.status NOT IN ('已完成','已取消'))"]
    params = []
    where_sub, params_sub = _get_expanded_region_where(region_key, None, prefix="s.")
    where.extend(where_sub)
    params.extend(params_sub)
    
    q = f"""
    SELECT SUM(p.total_amount)
    FROM purchase_orders p
    JOIN suppliers s ON p.supplier_id = s.supplier_id
    WHERE {" AND ".join(where)}
    """
    res = conn.execute(q, tuple(params)).fetchone()
    conn.close()
    return float(res[0] or 0)


def infer_affected_region_from_news(api_key: str, news_text: str, model: str | None = None) -> dict:
    """單篇新聞分析（保留原介面）。"""
    res = batch_infer_affected_region_from_news(api_key, [news_text], model=model)
    return res[0] if res else failed_analysis("missing_result")


def batch_infer_affected_region_from_news(api_key: str = "", news_texts: List[str] = None, model: str | None = None) -> List[dict]:
    """批量分析新聞內容，顯著提升效能。（issue #27：api_key/model 參數棄用，.env 驅動）"""
    news_texts = news_texts or []
    if not news_texts:
        return []

    try:
        import json
        import re

        # 1. 取得歷史慣例數據 (Precedents)
        precedents = get_historical_event_precedents()
        precedents_text = "\n".join([f"- {etype}: 平均延遲 {days:.1f} 天 (根據 {count} 筆紀錄)" for etype, days, count in precedents])
        if not precedents_text:
            precedents_text = "目前尚無歷史慣例數據。"

        # 2. 建立批量文本
        formatted_items = []
        for i, txt in enumerate(news_texts):
            formatted_items.append(f"【新聞編號 {i}】\n{str(txt)[:1000]}")
        news_items_text = "\n\n".join(formatted_items)
        
        # 3. 使用含慣例數據的 Prompt
        prompt = BATCH_INFER_WITH_PRECEDENTS_PROMPT.format(
            news_items_text=news_items_text,
            precedents_text=precedents_text
        )

        # issue #27：統一 LLM 入口（json_mode + 低溫；供應商 fallback 在底層）
        from backend.llm_client import complete_text
        try:
            raw_text = complete_text(prompt, temperature=0.1, json_mode=True, tag="analysis:news_batch") or ""
        except Exception:
            return [failed_analysis("provider_error") for _ in news_texts]
        return parse_news_batch(raw_text, len(news_texts))
    except Exception:
        return [failed_analysis("invalid_output") for _ in news_texts]


# ── 受災採購清單 (Impacted PO List) ────────────────────────────────────

def get_impacted_pos(region_key=None, country=None, supplier_id=None):
    """依熱點（地區/國家）或供應商 ID 篩選未結案採購單，回傳：採購單號、供應商、關鍵物料、預計延遲、替代建議。"""
    conn = connect_db(DB_FILE)
    where, params = ["(p.status IS NULL OR p.status NOT IN ('已完成','已取消'))"], []
    if supplier_id:
        where.append("p.supplier_id = ?")
        params.append(supplier_id)
    if region_key or country:
        where_sub, params_sub = _get_expanded_region_where(region_key, country, prefix="s.")
        where.extend(where_sub)
        params.extend(params_sub)
    q = """
    SELECT p.po_id, p.supplier_id, s.name as supplier_name, s.country, s.region,
           p.estimated_delay_days, p.alternative_suggestion, p.total_amount, p.status
    FROM purchase_orders p
    JOIN suppliers s ON p.supplier_id = s.supplier_id
    WHERE """ + " AND ".join(where)
    pos = __pd_read(q, conn, params=tuple(params))
    conn.close()
    if pos is None or pos.empty:
        return []
    out = []
    conn = connect_db(DB_FILE)
    for _, row in pos.iterrows():
        items = __pd_read(
            "SELECT product_id FROM purchase_order_items WHERE po_id = ?", conn, params=(row["po_id"],)
        )
        names = []
        if items is not None and not items.empty:
            for _, it in items.iterrows():
                inv = __pd_read("SELECT name, stock, daily_sales FROM inventory WHERE product_id = ?", conn, params=(it["product_id"],))
                if inv is not None and not inv.empty:
                    n_str = inv["name"].iloc[0]
                    stk = inv["stock"].iloc[0] or 0
                    ds = inv["daily_sales"].iloc[0] or 0
                    if ds > 0:
                        days_left = int(stk / ds)
                        names.append(f"{n_str} (庫存約剩 {days_left} 天)")
                    else:
                        names.append(n_str)
        key_materials = "、".join(names) if names else "—"
        delay = row.get("estimated_delay_days")
        # 處理 NaN（pandas 從 DB 讀出空值時可能為 NaN，NaN != NaN）
        try:
            delay_str = f"+{int(delay)} 天" if delay is not None and delay == delay else "—"
        except (TypeError, ValueError):
            delay_str = "—"
        alt_raw = row.get("alternative_suggestion")
        import pandas as _pd
        alt = str(alt_raw).strip() if (_pd.notna(alt_raw) and alt_raw) else "—"
        out.append({
            "po_id": row["po_id"],
            "supplier_id": row["supplier_id"],
            "supplier_name": row["supplier_name"],
            "country": _clean_text(row.get("country")),
            "region": _clean_text(row.get("region")),
            "key_materials": key_materials,
            "estimated_delay": delay_str,
            "estimated_delay_days": int(delay) if (delay is not None and delay == delay) else None,
            "alternative_suggestion": alt,
            "alternative_suggestion_raw": alt if alt != "—" else "",
            "total_amount": float(row.get("total_amount") or 0) if row.get("total_amount") == row.get("total_amount") else 0.0,
            "status": _clean_text(row.get("status")),
        })
    conn.close()
    return out


def update_po_impact(po_id, estimated_delay_days=None, alternative_suggestion=None, *, actor=None):
    """Planner may change assessment notes, never the underlying transaction."""
    require_capability(actor, RISK_WORKSPACE_WRITE)
    days = number(estimated_delay_days, maximum=365, integer=True) if estimated_delay_days is not None else None
    suggestion = text(alternative_suggestion) if alternative_suggestion is not None else None
    with connect_db(DB_FILE) as conn:
        if days is not None:
            conn.execute("UPDATE purchase_orders SET estimated_delay_days=? WHERE po_id=?", (days,po_id))
        if suggestion is not None:
            conn.execute("UPDATE purchase_orders SET alternative_suggestion=? WHERE po_id=?", (suggestion,po_id))


def get_ai_alternative_suggestions(api_key="", impacted_list=None, hotspot_name="", model: str | None = None):
    """由 AI 依熱點、供應商與關鍵物料分析，為每張採購單產生替代建議，
    回傳 [{"po_id": ..., "estimated_delay_days": ..., "alternative_suggestion": ...}, ...]。
    （issue #27：api_key/model 參數棄用，模型由 .env 決定）"""
    if not impacted_list:
        return []
    rows_text = "\n".join(
        f"- PO: {x['po_id']} | 供應商: {x['supplier_name']} | 關鍵物料: {x['key_materials']} | 預計延遲: {x['estimated_delay']}"
        for x in impacted_list
    )
    # 取得我司其他供應商據點（國家/地區），供 AI 明確建議「從哪裡調貨」
    other_regions_text = ""
    try:
        suppliers = get_suppliers_for_map()
        if suppliers is not None and not suppliers.empty:
            # 當前熱點可能為「墨西哥 中北部」或「台灣 北區」，用關鍵字排除
            seen = set()
            parts = []
            for _, s in suppliers.iterrows():
                country = (s.get("country") or "").strip()
                region = (s.get("region") or "").strip() or country
                if not country:
                    continue
                # 若該據點屬於當前熱點（國家或地區名重合）則跳過
                if matches_location(country, region, hotspot_name):
                    continue
                key = f"{country} {region}".strip()
                if key not in seen:
                    seen.add(key)
                    parts.append(key)
            if parts:
                other_regions_text = "、".join(parts)
    except Exception:
        pass
    if not other_regions_text:
        other_regions_text = "（系統內暫無其他地區供應商，可依產業常識建議具體國家，例如：越南、泰國、中國華南、美國）"

    prompt = PO_ALTERNATIVE_SUGGESTION_PROMPT.format(
        hotspot_name=hotspot_name,
        other_regions_text=other_regions_text,
        rows_text=rows_text,
        impact_count=len(impacted_list)
    )
    po_ids = {x["po_id"] for x in impacted_list}
    try:
        # issue #27/#47：統一 LLM 入口 + 結構化輸出（原 pipe 行格式改 JSON）
        import json
        from backend.llm_client import complete_text
        raw = (complete_text(prompt, json_mode=True, tag="analysis:po_suggest") or "").strip()
        payload = json_payload(raw)
        items = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise ValueError("Invalid PO response")
        result, seen = [], set()
        for it in items:
            if not isinstance(it, dict):
                raise ValueError("Invalid PO item")
            po_id = text(it.get("po_id"))
            if po_id not in po_ids:
                continue
            if po_id in seen:
                raise ValueError("Duplicate PO result")
            seen.add(po_id)
            try:
                delay_days = number(it["延遲天數"], maximum=365, integer=True, nullable=True)
                suggestion = text(it["建議"])
            except (KeyError, TypeError, ValueError):
                continue
            if suggestion:
                result.append({"po_id": po_id, "estimated_delay_days": delay_days,
                               "alternative_suggestion": suggestion})
        return result
    except Exception:
        return []


# ── 模擬情境分析 (What-If Simulation) ──────────────────────────────────

def what_if_simulation(
    api_key,
    user_question,
    model: str | None = "gemini-2.5-flash",
    *,
    actor=None,
):
    """依使用者情境問題，結合 ERP 供應商、未結案採購單、庫存安全天數，由 AI 回覆影響與建議。model 為 Gemini 模型 ID。"""
    require_capability(actor, RISK_WHAT_IF_RUN)
    conn = connect_db(DB_FILE)
    suppliers = __pd_read("SELECT supplier_id, name, country, region FROM suppliers", conn)
    pos = __pd_read(
        """SELECT p.po_id, p.supplier_id, s.name, s.country, s.region, p.estimated_delay_days, p.alternative_suggestion
           FROM purchase_orders p JOIN suppliers s ON p.supplier_id = s.supplier_id
           WHERE p.status NOT IN ('已完成','已取消') OR p.status IS NULL""",
        conn,
    )
    inv = __pd_read(
        "SELECT product_id, name, stock, reorder_point, daily_sales FROM inventory WHERE daily_sales > 0 OR reorder_point > 0",
        conn,
    )
    conn.close()
    supplier_text = suppliers.to_string(index=False) if suppliers is not None and not suppliers.empty else "無"
    po_text = pos.to_string(index=False) if pos is not None and not pos.empty else "無進行中採購單"
    inv_text = inv.to_string(index=False) if inv is not None and not inv.empty else "無庫存資料"
    system = WHAT_IF_SYSTEM_PROMPT
    prompt = WHAT_IF_USER_PROMPT.format(
        supplier_text=supplier_text,
        po_text=po_text,
        inv_text=inv_text,
        user_question=user_question
    )
    try:
        # issue #27：統一 LLM 入口（api_key 參數棄用，.env 驅動）
        from backend.llm_client import complete_text
        return (complete_text(prompt, system=system, temperature=0.2,
                              tag="analysis:whatif") or "").strip()
    except Exception as e:
        return f"模擬分析暫時無法產生：{e}"


# ── 風險事件與交期 ────────────────────────────────────────────────────

def get_risk_events_list(limit=20):
    """取得風險事件列表（id, event_type, region, country, impact_days, description, created_at）。"""
    conn = connect_db(DB_FILE)
    df = __pd_read(
        f"SELECT id, event_type, region, country, impact_days, description, created_at, news_id FROM supply_chain_events WHERE {_VALID_EVENT_SOURCE} ORDER BY COALESCE(created_at,'') DESC,id DESC LIMIT ?",
        conn,
        params=(limit,),
    )
    conn.close()
    return df

def get_active_risk_events(limit=30):
    """取得活耀（最近）的風險事件，作為 AI 分析的背景。"""
    return get_risk_events_list(limit=limit)

def get_supply_chain_summary_kpis():
    """計算供應鏈風險總覽 KPI：30天內事件數、去重後的受影響供應商數與銷售訂單數。"""
    conn = connect_db(DB_FILE)
    # 1. 30 天內事件數
    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")
    event_count = conn.execute(f"SELECT COUNT(*) FROM supply_chain_events WHERE {_VALID_EVENT_SOURCE} AND created_at >= ?", (since,)).fetchone()[0]
    
    # 2. 受波及供應商與訂單 (去重)
    # 取得最近 50 件事件作為代表性 KPI
    active_events = conn.execute(f"SELECT region, country, impact_days FROM supply_chain_events WHERE {_VALID_EVENT_SOURCE} ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    
    affected_suppliers = set()
    affected_orders = set()
    
    for region, country, impact_days in active_events:
        sups = get_affected_suppliers_by_event(region or "", country)
        for s in sups:
            affected_suppliers.add(s.get('supplier_id'))
            
        ords = get_affected_sales_orders_by_event(region or "", country, impact_days or 0)
        for o in ords:
            affected_orders.add(o.get('order_id'))
            
    return {
        "event_count": event_count,
        "supplier_count": len(affected_suppliers),
        "order_count": len(affected_orders)
    }

def get_historical_event_precedents():
    """從資料庫統計各類事件的平均延遲天數，作為 AI 推估的依據。"""
    conn = connect_db(DB_FILE)
    try:
        # 統計各類事件的平均值與次數
        res = conn.execute(
            f"""SELECT event_type, AVG(impact_days) as avg_days, COUNT(*) as cnt
               FROM supply_chain_events 
               WHERE {_VALID_EVENT_SOURCE} AND impact_days > 0
               GROUP BY event_type 
               ORDER BY cnt DESC"""
        ).fetchall()
        return res # [(type, avg, count), ...]
    except Exception:
        return []
    finally:
        conn.close()


def add_risk_event(event_type, region, country, impact_days, description, news_id=None, *, actor=None):
    from .risk_contract import validate_event
    require_capability(actor, RISK_WORKSPACE_WRITE)
    with connect_db(DB_FILE) as conn:
        conn.execute("BEGIN IMMEDIATE")
        event_type, region, country, impact_days, description, news_id = validate_event(conn, event_type, region, country, impact_days, description, news_id)
        row = conn.execute("SELECT id FROM supply_chain_events WHERE COALESCE(country,'')=? AND COALESCE(region,'')=? AND event_type=? AND news_id IS ?", (country, region, event_type, news_id)).fetchone()
        if row:
            conn.execute("UPDATE supply_chain_events SET impact_days=?, description=?, created_at=? WHERE id=?", (impact_days, description, datetime.now().isoformat(), row[0]))
            return row[0]
        cur = conn.execute("INSERT INTO supply_chain_events(event_type,region,country,impact_days,description,created_at,news_id) VALUES(?,?,?,?,?,?,?)", (event_type,region,country,impact_days,description,datetime.now().isoformat(),news_id))
        return cur.lastrowid


def update_risk_event(event_id, *, event_type=None, impact_days=None, description=None, actor=None):
    from .risk_contract import validate_event
    require_capability(actor, RISK_WORKSPACE_WRITE)
    event_id = number(event_id, maximum=2**53-1, integer=True)
    with connect_db(DB_FILE) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT event_type,region,country,impact_days,description,news_id FROM supply_chain_events WHERE id=?", (event_id,)).fetchone()
        if not row:
            return False
        values = validate_event(conn, row[0] if event_type is None else event_type, row[1] or '', row[2] or '', row[3] if impact_days is None else impact_days, (row[4] or '') if description is None else description, row[5])
        collision = conn.execute("SELECT id FROM supply_chain_events WHERE COALESCE(country,'')=? AND COALESCE(region,'')=? AND event_type=? AND news_id IS ? AND id<>?", (values[2],values[1],values[0],values[5],event_id)).fetchone()
        if collision:
            raise ValueError("另一事件已使用相同類型與來源；請更新該事件")
        conn.execute("UPDATE supply_chain_events SET event_type=?,impact_days=?,description=?,created_at=? WHERE id=?", (values[0],values[3],values[4],datetime.now().isoformat(),event_id))
        return True


def delete_risk_event(event_id, *, actor=None):
    """刪除一筆風險事件。"""
    require_capability(actor, RISK_WORKSPACE_WRITE)
    run_query("DELETE FROM supply_chain_events WHERE id = ?", (event_id,), fetch=False)


def get_affected_suppliers_by_event(region: str, country: str = None):
    """依地區與國家篩選受影響的正式供應商（僅限 is_official=1）。"""
    conn = connect_db(DB_FILE)
    where, params = _get_expanded_region_where(region, country)
    if not where:
        conn.close()
        return []
    # 限制為正式供應商
    where.append("is_official = 1")
    q = "SELECT supplier_id, name, country, region, risk_level FROM suppliers WHERE " + " AND ".join(where)
    df = __pd_read(q, conn, params=tuple(params))
    conn.close()
    if df is None or df.empty:
        return []
    return df.to_dict('records')

def get_affected_sales_orders_by_event(region: str, country: str, impact_days: int):
    """
    Find sales orders impacted by a regional risk event.
    Trace: Suppliers (Region) -> Purchase Orders (Pending) -> Products -> BOM (Finished Good) -> Sales Orders (Pending).
    Returns list of dicts with order details.
    """
    conn = connect_db(DB_FILE)
    
    where, params = _get_expanded_region_where(region, country, prefix="s.")
    if not where:
        conn.close()
        return []
        
    query = f"""
    -- CTE to find all pending sales orders that either directly sell the affected component,
    -- or sell a BOM finished good that relies on the affected component.
    WITH AffectedProducts AS (
        SELECT DISTINCT poi.product_id as component_id
        FROM suppliers s
        JOIN purchase_orders po ON s.supplier_id = po.supplier_id
        JOIN purchase_order_items poi ON po.po_id = poi.po_id
        WHERE {" AND ".join(where)}
          AND (po.status IS NULL OR po.status NOT IN ('已完成', '已取消', '已入庫'))
    ),
    AffectedFinalGoods AS (
        -- Directly matching products
        SELECT component_id as final_good_id, component_id as reason_component
        FROM AffectedProducts
        UNION
        -- Indirect matching products via BOM
        SELECT b.product_id as final_good_id, b.component_id as reason_component
        FROM bom b 
        JOIN AffectedProducts ap ON b.component_id = ap.component_id
    )
    SELECT DISTINCT 
        o.order_id, 
        c.name as customer_name, 
        inv.name as product_name,
        o.order_date
    FROM AffectedFinalGoods fg
    JOIN orders o ON o.product_id = fg.final_good_id
    JOIN inventory inv ON o.product_id = inv.product_id
    LEFT JOIN customers c ON o.customer_id = c.customer_id
    WHERE (o.status IS NULL OR o.status NOT IN ('已完成', '已取消', '已出貨'))
    """
    
    df = __pd_read(query, conn, params=tuple(params))
    conn.close()
    
    if df is None or df.empty:
        return []
        
    results = []
    for _, row in df.iterrows():
        order_date_raw = row['order_date']
        try:
            # 使用 pd.to_datetime 處理各種日期時間格式，並轉為日期物體
            dt = pd.to_datetime(order_date_raw).to_pydatetime()
            # 預設提前期 (Lead Time) 為 7 天，可依業務需求調整
            orig_delivery = dt + timedelta(days=7)
            new_delivery = orig_delivery + timedelta(days=impact_days)
            orig_str = orig_delivery.strftime("%Y-%m-%d")
            new_str = f"{new_delivery.strftime('%Y-%m-%d')} (+{impact_days}天)"
        except Exception:
            orig_str = "未定"
            new_str = f"未定 (+{impact_days}天)"
            
        results.append({
            "order_id": row['order_id'],
            "customer_name": row['customer_name'] or "Unknown",
            "product_name": row['product_name'] or "Unknown",
            "original_delivery": orig_str,
            "new_delivery": new_str
        })
        
    return results

def get_stockout_alerts_for_event(region: str, country: str, impact_days: int):
    """
    計算因風險事件導致的採購延遲，是否會造成庫存斷鏈（量 < 0）或跌破安全水位（量 < reorder_point）。
    回傳列表：包含商品名稱、現有庫存、預估延期消耗量、預估剩餘庫存、警報等級。
    """
    conn = connect_db(DB_FILE)
    where = []
    params = []
    
    where, params = _get_expanded_region_where(region, country, prefix="s.")

    if not where:
        conn.close()
        return []

    # 找出受影響的採購單項目與對應庫存
    query = f"""
    SELECT DISTINCT 
        inv.product_id,
        inv.name as product_name,
        inv.stock,
        inv.reorder_point,
        inv.daily_sales
    FROM suppliers s
    JOIN purchase_orders po ON s.supplier_id = po.supplier_id
    JOIN purchase_order_items poi ON po.po_id = poi.po_id
    JOIN inventory inv ON poi.product_id = inv.product_id
    WHERE {" AND ".join(where)}
      AND (po.status IS NULL OR po.status NOT IN ('已完成', '已取消', '已入庫'))
    """
    df = __pd_read(query, conn, params=tuple(params))
    conn.close()

    if df is None or df.empty:
        return []

    alerts = []
    for _, row in df.iterrows():
        stock = int(row['stock'] or 0)
        reorder_point = int(row['reorder_point'] or 0)
        daily_sales = int(row['daily_sales'] or 0)
        
        # 延遲天數帶來的額外消耗量
        extra_consumption = impact_days * daily_sales
        projected_stock = stock - extra_consumption
        
        if projected_stock < 0:
            level = "🔴 高風險 (確定斷鏈)"
            shortage_days = abs(projected_stock) / daily_sales if daily_sales > 0 else 0
            suggestion = f"預計在到貨前 {shortage_days:.1f} 天發生斷貨！請立即聯絡採購啟動替代方案。"
        elif projected_stock < reorder_point:
            level = "🟡 中風險 (跌破安全水位)"
            suggestion = f"將跌破安全水位 ({reorder_point})，剩餘 {projected_stock} 件。建議提早發出下一批常規訂單。"
        else:
            level = "🟢 低風險 (安全過關)"
            suggestion = f"庫存充足，延期後仍有 {projected_stock} 件，高於安全水位。"
            
        alerts.append({
            "product_id": row['product_id'],
            "product_name": row['product_name'] or "未知商品",
            "stock": stock,
            "extra_consumption": extra_consumption,
            "projected_stock": projected_stock,
            "reorder_point": reorder_point,
            "risk_level": level,
            "suggestion": suggestion
        })

    # 先依風險等級排序：高 -> 中 -> 低
    def risk_weight(lvl):
        if "高" in lvl: return 1
        if "中" in lvl: return 2
        return 3
    
    alerts.sort(key=lambda x: risk_weight(x['risk_level']))
    return alerts

def increase_safety_stock_for_event(
    region: str,
    country: str,
    impact_days: int,
    multiplier: float = 1.0,
    *,
    actor=None,
):
    """
    針對受風險事件影響的地區，找出該區供應商提供的所有物料，
    動態計算應調高的安全水位。公式：新水位 = 基準水位 + (日銷量 * 影響天數 * 倍率)。
    基準水位會被保存在 baseline_reorder_point 中以供日後還原。
    """
    require_capability(actor, ERP_POLICY_WRITE)
    conn = connect_db(DB_FILE)
    where, params = _get_expanded_region_where(region, country, prefix="s.")
    if not where:
        conn.close()
        return 0

    # 取得受影響的產品及其目前的基準水位與日銷量
    query = f"""
    SELECT DISTINCT inv.product_id, inv.reorder_point, inv.baseline_reorder_point, inv.daily_sales
    FROM suppliers s
    JOIN purchase_orders po ON s.supplier_id = po.supplier_id
    JOIN purchase_order_items poi ON po.po_id = poi.po_id
    JOIN inventory inv ON poi.product_id = inv.product_id
    WHERE {" AND ".join(where)}
    """
    df = __pd_read(query, conn, params=tuple(params))
    
    if df is None or df.empty:
        conn.close()
        return 0
        
    updated_count = 0
    for _, row in df.iterrows():
        pid = row["product_id"]
        # 如果 baseline 是空的，代表這是第一次調整，將目前的 reorder_point 存入 baseline
        baseline = row["baseline_reorder_point"]
        if baseline is None:
            baseline = row["reorder_point"] or 0
            conn.execute("UPDATE inventory SET baseline_reorder_point = ? WHERE product_id = ?", (baseline, pid))
            
        dsales = row["daily_sales"] or 0
        
        # 動態計算增量
        increment = int(dsales * impact_days * multiplier)
        if increment <= 0 and impact_days > 0:
            increment = int(baseline * 0.2)
            
        new_rop = baseline + increment
        
        conn.execute(
            "UPDATE inventory SET reorder_point = ? WHERE product_id = ?",
            (new_rop, pid)
        )
        updated_count += 1
        
    conn.commit()
    conn.close()
    return updated_count

def restore_all_rop_to_baseline(*, actor=None):
    """將所有產品的安全水位還原至基準值 (baseline_reorder_point)。"""
    require_capability(actor, ERP_POLICY_WRITE)
    conn = connect_db(DB_FILE)
    # 僅針對有設定 baseline 的進行還原
    conn.execute("UPDATE inventory SET reorder_point = baseline_reorder_point WHERE baseline_reorder_point IS NOT NULL")
    conn.commit()
    conn.close()
    return True
def update_reorder_point(product_id: str, new_reorder_point: int, *, actor=None):
    """手動更新指定物料的安全庫存水位。"""
    require_capability(actor, ERP_POLICY_WRITE)
    conn = connect_db(DB_FILE)
    conn.execute(
        "UPDATE inventory SET reorder_point = ? WHERE product_id = ?",
        (int(new_reorder_point), product_id)
    )
    conn.commit()
    conn.close()

def get_event_risk_scores():
    """取得事件類型對應的風險分數（event_type -> score）。"""
    conn = connect_db(DB_FILE)
    df = __pd_read(
        "SELECT risk_type, risk_key, risk_score, weight FROM esg_risk_factors WHERE risk_type = 'event_type'", conn)
    conn.close()
    if df is None or df.empty:
        return {}
    return dict(zip(df["risk_key"], df["risk_score"] * df["weight"]))


def get_region_risk_scores():
    """取得地區對應的風險分數（region key -> score）。"""
    conn = connect_db(DB_FILE)
    df = __pd_read("SELECT risk_type, risk_key, risk_score, weight FROM esg_risk_factors WHERE risk_type = 'region'", conn)
    conn.close()
    if df is None or df.empty:
        return {}
    return dict(zip(df["risk_key"], df["risk_score"] * df["weight"]))


# ── 風險係數管理 ────────────────────────────────────────────────────────

def get_risk_factors():
    """取得所有風險係數（id, 類型, 代碼, 風險分數, 權重, 備註, 更新時間）。"""
    conn = connect_db(DB_FILE)
    df = __pd_read(
        "SELECT id, risk_type as 類型, risk_key as 代碼, risk_score as 風險分數, weight as 權重, note as 備註, updated_at as 更新時間 FROM esg_risk_factors ORDER BY risk_type, risk_key",
        conn,
    )
    conn.close()
    return df


def get_risk_factors_raw():
    """取得原始欄位名的風險係數（供加權計算、預覽用）。"""
    conn = connect_db(DB_FILE)
    df = __pd_read("SELECT risk_type, risk_key, risk_score, weight FROM esg_risk_factors", conn)
    conn.close()
    return df


def save_risk_factor(
    risk_type, risk_key, risk_score, weight, note=None, *, actor=None
):
    """新增或更新一筆風險係數。"""
    require_capability(actor, RISK_WORKSPACE_WRITE)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    run_query(
        "INSERT OR REPLACE INTO esg_risk_factors (risk_type, risk_key, risk_score, weight, note, updated_at) VALUES (?,?,?,?,?,?)",
        (risk_type, risk_key.strip(), float(risk_score), float(weight), note or None, now),
        fetch=False,
    )


def delete_risk_factor(factor_id, *, actor=None):
    """刪除一筆風險係數。"""
    require_capability(actor, RISK_WORKSPACE_WRITE)
    run_query("DELETE FROM esg_risk_factors WHERE id = ?", (factor_id,), fetch=False)


def clear_all_risk_factors(*, actor=None):
    """清空全部風險係數（供重新實作或重置使用）。"""
    require_capability(actor, RISK_WORKSPACE_WRITE)
    conn = connect_db(DB_FILE)
    conn.execute("DELETE FROM esg_risk_factors")
    conn.commit()
    conn.close()


def get_geographic_risk_display():
    """地理風險圖用：回傳 list of dict {name, score, level, emoji}。level 為 高/中/低，emoji 為 🔴/🟡/🟢。"""
    region_scores = get_region_risk_scores()
    default_regions = ["中國", "越南", "台灣", "日本", "美國", "南韓", "墨西哥", "歐洲", "東南亞"]
    default_fallback = {"中國": 75, "越南": 55, "台灣": 35, "日本": 45, "美國": 50, "南韓": 50, "墨西哥": 45, "歐洲": 45, "東南亞": 55}
    seen = set()
    out = []
    for name in default_regions:
        if name in seen:
            continue
        seen.add(name)
        score = 0
        for rk, rs in region_scores.items():
            if matches_location(name, name, rk):
                score = max(score, min(100, rs))
                break
        if score == 0 and name in default_fallback:
            score = default_fallback[name]
        if score >= 70:
            level, emoji = "高", "🔴"
        elif score >= 40:
            level, emoji = "中", "🟡"
        else:
            level, emoji = "低", "🟢"
        out.append({"name": name, "score": score, "level": level, "emoji": emoji})
    for rk, rs in region_scores.items():
        if rk in seen:
            continue
        seen.add(rk)
        score = min(100, rs)
        if score >= 70:
            level, emoji = "高", "🔴"
        elif score >= 40:
            level, emoji = "中", "🟡"
        else:
            level, emoji = "低", "🟢"
        out.append({"name": rk, "score": score, "level": level, "emoji": emoji})
    return sorted(out, key=lambda x: -x["score"])


def get_risk_ai_suggestions(api_key: str = "", news_context: str = "", region_summary: str = "", model: str = "") -> str:
    """依地理風險與新聞由 AI 產出建議，考量政治風險、物流風險、匯率。（issue #27：api_key 參數棄用）"""
    prompt = f"""你是供應鏈風險分析師。請根據以下「地理風險」與「近期新聞」，針對 **政治風險、物流風險、匯率** 三方面，給我司簡要的供應鏈風險建議（每項 1～2 句，繁體中文）。

【地理風險】
{region_summary}

【近期新聞】
{news_context or "（尚無新聞，請先於「風險事件與交期」頁按「更新即時新聞」）"}

請依序回覆：
1. 政治風險建議
2. 物流風險建議
3. 匯率建議
簡潔、可直接供決策參考。"""
    try:
        # issue #27：統一 LLM 入口
        from backend.llm_client import complete_text
        text = (complete_text(prompt, temperature=0.2, tag="analysis:risk_suggest") or "").strip()
    except Exception as e:
        return f"AI 建議暫時無法產生（{e}）。請確認 .env 模型設定與網路。"

    if not text:
        return "AI 建議暫時無法產生（模型未回傳內容）。"
    return text


def load_preset_risk_factors(*, actor=None):
    """載入預設風險係數範本（地區、事件類型、供應商類別）。"""
    require_capability(actor, RISK_WORKSPACE_WRITE)
    conn = connect_db(DB_FILE)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    presets = [
        ("region", "東亞", 60, 1.0, "預設範本"),
        ("region", "日本", 40, 1.0, "預設範本"),
        ("region", "美國", 50, 1.0, "預設範本"),
        ("region", "越南", 55, 1.0, "預設範本"),
        ("region", "歐洲", 45, 1.0, "預設範本"),
        ("event_type", "地震", 85, 1.0, "預設範本"),
        ("event_type", "天候", 65, 1.0, "預設範本"),
        ("event_type", "政治", 75, 1.0, "預設範本"),
        ("event_type", "疫情", 70, 1.0, "預設範本"),
        ("event_type", "罷工", 60, 1.0, "預設範本"),
        ("event_type", "其他", 50, 1.0, "預設範本"),
        ("supplier_category", "高", 80, 1.0, "預設範本"),
        ("supplier_category", "中", 50, 1.0, "預設範本"),
        ("supplier_category", "低", 20, 1.0, "預設範本"),
    ]
    for rt, rk, rs, w, nt in presets:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO esg_risk_factors (risk_type, risk_key, risk_score, weight, note, updated_at) VALUES (?,?,?,?,?,?)",
                (rt, rk, rs, w, nt, now),
            )
        except Exception:
            pass
    conn.commit()
    conn.close()


def get_procurement_by_region_with_risk():
    """依地區彙總採購金額並帶出該地區風險係數（ERP 供應鏈連動）。回傳 list of dict: display_name, total_amount, supplier_count, risk_score。"""
    proc = get_region_procurement_share()
    if not proc:
        return []
    region_scores = get_region_risk_scores()
    out = []
    for key, v in proc.items():
        display_name = v.get("display_name") or key
        total_amount = float(v.get("total_amount") or 0)
        supplier_count = int(v.get("supplier_count") or 0)
        risk_score = 0
        for rk, rs in region_scores.items():
            if matches_location(*split_location(key), rk):
                risk_score = max(risk_score, min(100, rs))
        out.append({
            "display_name": display_name,
            "total_amount": round(total_amount, 0),
            "supplier_count": supplier_count,
            "risk_score": round(risk_score, 1),
        })
    return sorted(out, key=lambda x: -x["total_amount"])


def get_aggregated_risk_preview():
    """綜合風險預覽：據點 × 地區係數 × 供應商類別係數，回傳 list of dict。"""
    conn = connect_db(DB_FILE)
    factors = __pd_read("SELECT risk_type, risk_key, risk_score, weight FROM esg_risk_factors", conn)
    sup = __pd_read(
        "SELECT supplier_id as id, name, country, region, risk_level FROM suppliers WHERE (country IS NOT NULL AND country != '') OR (region IS NOT NULL AND region != '')",
        conn,
    )
    if sup is None:
        sup = __empty_df()
    sup["據點類型"] = "供應商"
    try:
        cust = __pd_read(
            "SELECT customer_id as id, name, country, region, risk_level FROM customers WHERE (country IS NOT NULL AND country != '') OR (region IS NOT NULL AND region != '')",
            conn,
        )
        cust["據點類型"] = "客戶"
        partners = __pd_concat(sup, cust)
    except Exception:
        partners = sup
    conn.close()

    if factors.empty or partners.empty:
        return []

    region_df = factors[factors["risk_type"] == "region"]
    region_map = dict(zip(region_df["risk_key"], region_df["risk_score"] * region_df["weight"])) if not region_df.empty else {}
    cat_df = factors[factors["risk_type"] == "supplier_category"]
    cat_map = dict(zip(cat_df["risk_key"], cat_df["risk_score"])) if not cat_df.empty else {}

    rows = []
    for _, p in partners.iterrows():
        region_score = None
        for k, v in region_map.items():
            if matches_location(p.get("country"), p.get("region"), k):
                region_score = v
                break
        cat_score = cat_map.get(str(p.get("risk_level") or "").strip())
        if region_score is not None or cat_score is not None:
            r = (region_score or 0) + (cat_score or 0)
            level = "高" if r >= 100 else ("中" if r >= 50 else "低")
            rows.append({
                "據點": p["name"],
                "類型": p["據點類型"],
                "國家/地區": p.get("country") or p.get("region") or "-",
                "地區係數": region_score if region_score is not None else "-",
                "類別係數": cat_score if cat_score is not None else "-",
                "綜合關注": f"{r:.0f} ({level})",
            })
    return rows


# ── 內部輔助 ────────────────────────────────────────────────────────────

def __pd_read(query, conn, params=()):
    try:
        import pandas as pd
        out = pd.read_sql_query(query, conn, params=params if params else ())
        return out if out is not None else __empty_df()
    except Exception:
        return __empty_df()


def __empty_df():
    import pandas as pd
    return pd.DataFrame()


def __pd_concat(a, b):
    import pandas as pd
    return pd.concat([a, b], ignore_index=True)


def events_for_location(country: str, region: str, events=None) -> list[dict]:
    """某據點命中的事件（含新聞登錄與人工登錄），依延遲天數→登錄時間新到舊排序。

    前端卡片用它判斷「已有情報／已有應變計畫」，避免每張卡各自重查資料庫。
    """
    if events is None:
        events = get_active_risk_events(limit=HEATMAP_EVENT_LOOKBACK)
    if events is None:
        rows = []
    elif hasattr(events, "iterrows"):
        rows = [r.to_dict() for _, r in events.iterrows()]
    else:
        rows = list(events)
    matched = [ev for ev in rows if _event_matches_location(ev, country or "", region or "")]

    def _days(ev):
        try:
            return int(ev.get("impact_days") or 0)
        except (TypeError, ValueError):
            return 0

    matched.sort(key=lambda ev: (_days(ev), str(ev.get("created_at") or "")), reverse=True)
    return matched


def is_news_event(ev: dict) -> bool:
    """news_id 非空 → 由新聞一鍵登錄；否則為人工／AI 建議建立的正式應變事件。"""
    news_id = ev.get("news_id")
    if news_id is None:
        return False
    try:
        return not pd.isna(news_id)
    except (TypeError, ValueError):
        return True


def get_heatmap_ai_summary(api_key="", news_context="", reference_date=None, model=None, *, news_items=None, actor=None):
    """Compatibility tuple for existing callers; structured status is available below."""
    result = get_heatmap_ai_analysis(api_key, news_context, reference_date, model, news_items=news_items, actor=actor)
    return result["summary"], result["updates"], result["events"]



def get_heatmap_ai_analysis(api_key="", news_context="", reference_date=None, model=None, *, news_items=None, actor=None, persist=True):
    return analyze_heatmap_risk(news_items, news_context=news_context, reference_date=reference_date, actor=actor, persist=persist)
