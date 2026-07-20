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

# 區域與國家映射表：當事件標記為「中東」時，自動影響該區域內的所有國家。
_REGION_COUNTRY_MAP = {
    "中東": ["伊朗", "沙烏地阿拉伯", "阿聯酋", "以色列", "卡達", "伊拉克", "科威特", "約旦", "黎巴嫩", "敘利亞"],
    "東亞": ["台灣", "日本", "中國", "南韓", "北韓", "香港", "澳門"],
    "東南亞": ["越南", "泰國", "新加坡", "菲律賓", "馬來西亞", "印尼", "緬甸"],
    "北美": ["美國", "加拿大", "墨西哥"],
    "非洲": ["埃及", "南非", "摩洛哥", "奈及利亞"]
}

def _get_expanded_region_where(region, country_val, prefix=""):
    """
    擴展區域篩選邏輯：支援逗號分隔的多個國家/地區。
    若輸入包含大區域名稱（如「中東」），則自動擴展為該區域下所有國家的 OR 條件。
    【修正】當 country 與 region 同時指定且皆為單一值時，使用 AND 精確比對，
    避免台灣北區事件誤擴展至台灣中區/南區。
    """
    # 精確節點比對：若 country 與 region 皆提供且為單一值，直接回傳 AND 查詢
    c_single = (country_val or "").strip() if country_val and "," not in str(country_val) and "，" not in str(country_val) else ""
    r_single = (region or "").strip() if region and "," not in str(region) and "，" not in str(region) else ""
    # 若 region 以 country 為前綴（如 "台灣 北區"），去掉前綴只保留地區部分（"北區"）
    if c_single and r_single and r_single.startswith(c_single):
        r_single = r_single[len(c_single):].strip()
    # 只有當 region 確實指向子地區（不為空、且不等於 country）才使用 AND 精確查詢
    if c_single and r_single and r_single != c_single and c_single not in _REGION_COUNTRY_MAP:
        # 使用精確 AND 比對確保只選該特定節點
        return [f"({prefix}country LIKE ? AND {prefix}region LIKE ?)"], [f"%{c_single}%", f"%{r_single}%"]
    
    where_sub = []
    params_sub = []
    
    # 解析輸入：支援「美國, 伊朗」或「北美, 中東」或「台灣 北區」
    input_names = []
    if region:
        # 將全型逗號轉半型，且將空格也視為分隔符（若非大區域關鍵字）
        raw_names = str(region).replace("，", ",").split(",")
        for r in raw_names:
            if r.strip():
                # 特殊處理：如果有空格且不是已定義的大區域，則拆分
                if " " in r.strip() and r.strip() not in _REGION_COUNTRY_MAP:
                    input_names.extend([p.strip() for p in r.strip().split() if p.strip()])
                else:
                    input_names.append(r.strip())
                    
    if country_val:
        input_names.extend([n.strip() for n in str(country_val).replace("，", ",").split(",") if n.strip()])
    
    if not input_names:
        return [], []

    # 展開大區域並收集所有目標關鍵字
    target_set = set()
    for name in input_names:
        target_set.add(name)
        # 檢查是否為大區域
        if name in _REGION_COUNTRY_MAP:
            for c in _REGION_COUNTRY_MAP[name]:
                target_set.add(c)
                
    # 產生內容包含其中任一關鍵字的 OR 條件 (LIKE 查詢)
    conditions = []
    for c in sorted(list(target_set)):
        conditions.append(f"{prefix}country LIKE ?")
        conditions.append(f"{prefix}region LIKE ?")
        params_sub.extend([f"%{c}%", f"%{c}%"])
    
    if conditions:
        where_sub.append(f"({' OR '.join(conditions)})")
            
    return where_sub, params_sub


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
            country = (row.get(country_col) or "").strip()
            if country and country in _COUNTRY_DEFAULT_COORDS:
                lat, lon = _COUNTRY_DEFAULT_COORDS[country]
                df.at[idx, lat_col], df.at[idx, lon_col] = lat, lon
    return df


def get_suppliers_for_map():
    """取得正式供應商清單（含經緯度、國家、地區、風險等級），供地圖與清單使用。經緯度僅後端使用。"""
    conn = sqlite3.connect(DB_FILE)
    df = __pd_read("SELECT supplier_id, name, country, region, latitude, longitude, risk_level FROM suppliers WHERE is_official=1", conn)
    conn.close()
    return _fill_coords_from_country(df)


def get_customers_for_map():
    """取得客戶清單（含經緯度、國家、地區、風險等級），供地圖與清單使用。經緯度僅後端使用。"""
    conn = sqlite3.connect(DB_FILE)
    try:
        df = __pd_read("SELECT customer_id, name, country, region, latitude, longitude, risk_level FROM customers", conn)
    except Exception:
        df = __empty_df()
    conn.close()
    return _fill_coords_from_country(df)


def get_recent_events_for_delay(limit=50):
    """取得近期供應鏈事件，供地圖判定出貨延遲狀況。"""
    conn = sqlite3.connect(DB_FILE)
    df = __pd_read(
        "SELECT event_type, region, country, impact_days FROM supply_chain_events ORDER BY id DESC LIMIT ?",
        conn,
        params=(limit,),
    )
    conn.close()
    return df


def get_region_procurement_share():
    """依地區彙總採購金額，計算各地區採購佔比（該地區供應商之採購額 / 全公司採購額）。
    回傳 list of dict: region_key, display_name, procurement_ratio (0~1), total_amount, supplier_count。
    用於初始熱圖：採購佔比愈高，集中度風險愈高，可對應風險低/中/高。"""
    conn = sqlite3.connect(DB_FILE)
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
REGION_COUNTRY_MAP = {
    "亞洲": ["台灣", "日本", "中國", "南韓", "北韓", "越南", "泰國", "新加坡", "馬來西亞", "印尼", "菲律賓", "印度", "香港", "澳門"],
    "東亞": ["台灣", "日本", "中國", "南韓", "北韓", "香港", "澳門"],
    "東南亞": ["越南", "泰國", "新加坡", "馬來西亞", "印尼", "菲律賓", "緬甸", "柬埔寨", "寮國"],
    "歐洲": ["德國", "法國", "英國", "義大利", "西班牙", "荷蘭", "波蘭", "比利時", "奧地利", "瑞士"],
    "北美": ["美國", "加拿大", "墨西哥"],
    "中東": ["以色列", "沙烏地阿拉伯", "阿拉伯聯合大公國", "伊朗", "伊拉克", "土耳其", "約旦", "黎巴嫩"],
}

def get_risk_heatmap_data():
    """
    取得熱圖資料：永遠以「供應商據點」為基礎產出完整熱點清單，再以 risk_heatmap 表覆寫風險%與摘要。
    如此手動或 AI 更新單一熱點時，其他未調節的熱點仍會保留在地圖上。
    """
    # 1. 永遠先依供應商據點算出「預設」熱點清單
    suppliers = get_suppliers_for_map()
    if suppliers is None or suppliers.empty:
        return []
    events = get_recent_events_for_delay(20)
    region_scores = get_region_risk_scores()
    procurement_by_region = get_region_procurement_share()
    default_risk = 20.0
    seen = set()
    default_rows = []
    for _, s in suppliers.iterrows():
        country = (s.get("country") or "").strip() or "未填"
        region = (s.get("region") or "").strip() or country
        key = f"{country}|{region}"
        if key in seen:
            continue
        seen.add(key)
        risk = default_risk
        if events is not None and not events.empty:
            for _, ev in events.iterrows():
                if (ev.get("country") and ev["country"] in country) or (ev.get("region") and ev["region"] in region):
                    risk = min(100, risk + 40)
                    break
        for k, v in region_scores.items():
            if k in region or k in country:
                risk = max(risk, min(100, v))
                break
        if key in procurement_by_region:
            ratio = procurement_by_region[key]["procurement_ratio"]
            if ratio >= 0.35:
                risk = max(risk, 70)
            elif ratio >= 0.15:
                risk = max(risk, 45)
            else:
                risk = max(risk, min(35, 20 + ratio * 100))
        lat, lon = s.get("latitude"), s.get("longitude")
        if lat is None or lon is None:
            continue
        default_rows.append({
            "region_key": key,
            "display_name": f"{country} {region}".strip(),
            "latitude": float(lat),
            "longitude": float(lon),
            "risk_pct": round(risk, 1),
            "ai_summary": None,
            "updated_at": None,
        })
    # 2. 讀取 DB 中手動/AI 覆寫的風險%與摘要，依 region_key 覆蓋到預設清單
    conn = sqlite3.connect(DB_FILE)
    df = __pd_read(
        "SELECT region_key, display_name, latitude, longitude, risk_pct, ai_summary, updated_at FROM risk_heatmap",
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
                    "latitude": r.get("latitude"),
                    "longitude": r.get("longitude"),
                }
    # 3. 合併：預設熱點 + 有覆寫則用覆寫的 risk_pct / ai_summary
    out = []
    for row in default_rows:
        rk = row["region_key"]
        if rk in overrides:
            o = overrides[rk]
            out.append({
                "region_key": rk,
                "display_name": row["display_name"],
                "latitude": o.get("latitude") if o.get("latitude") is not None else row["latitude"],
                "longitude": o.get("longitude") if o.get("longitude") is not None else row["longitude"],
                "risk_pct": o.get("risk_pct") if o.get("risk_pct") is not None else row["risk_pct"],
                "ai_summary": o.get("ai_summary"),
                "updated_at": o.get("updated_at"),
            })
        else:
            out.append(row)
    return out


def upsert_risk_heatmap(
    region_key, display_name, latitude, longitude, risk_pct, ai_summary=None, *, actor=None
):
    """新增或更新一筆熱圖熱點。"""
    require_capability(actor, RISK_WORKSPACE_WRITE)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = sqlite3.connect(DB_FILE)
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
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM risk_heatmap")
    conn.commit()
    conn.close()

# （issue #27）Gemini 模型探測器 get_working_model_id 已移除 ——
#   模型選擇統一由 .env（LLM_MODEL / LLM_ANALYSIS_MODEL）決定，
#   供應商層 fallback 由 backend/llm_client → agent_orchestrator._llm 處理。


def _gate_heatmap_updates(raw_updates, valid_list, name_expansions) -> list[dict]:
    """
    issue #47 P1-3：合法區域檢核由 code 執行（取代 prompt 的嚴詞要求）。
      - 名稱在合法清單 → 直接收
      - 名稱是可展開的總稱（如「台灣」「中東」）→ 展開為完整節點
      - 其餘 → 丟棄
    無合法清單（DB 無正式供應商）時退回寬鬆模式：全收。
    """
    gate_on = bool(valid_list) and not (len(valid_list) == 1 and valid_list[0].startswith("（"))
    out = []
    for u in raw_updates or []:
        u = u or {}
        name = str(u.get("地區") or u.get("display_name") or "").strip()
        pct = u.get("風險", u.get("risk_pct"))
        try:
            pct = float(str(pct).replace("%", "").strip())
        except (TypeError, ValueError):
            continue
        if not name:
            continue
        if not gate_on or name in valid_list:
            out.append({"display_name": name, "risk_pct": pct})
        elif name in name_expansions:
            for expanded in name_expansions[name]:
                out.append({"display_name": expanded, "risk_pct": pct})
        # 不在清單也不可展開 → 丟棄（code-side gate）
    return out


def _coerce_heatmap_events(raw_events) -> list[dict]:
    """AI 回傳事件 → 內部契約（型別修正 + 預設值）。維持舊行為：事件不做地區硬閘。"""
    out = []
    for e in raw_events or []:
        e = e or {}
        try:
            days = int(e.get("延遲天數", e.get("impact_days", 14)) or 14)
        except (TypeError, ValueError):
            days = 14
        out.append({
            "event_type": str(e.get("類型") or e.get("event_type") or "其他").strip() or "其他",
            "region": str(e.get("地區") or e.get("region") or "").strip(),
            "country": str(e.get("國家") or e.get("country") or "").strip(),
            "impact_days": days,
            "description": str(e.get("描述") or e.get("description") or "").strip(),
        })
    return out


def get_heatmap_ai_summary(api_key: str = "", news_context: str = "", reference_date: str = "2026-04-11", model: str | None = None) -> tuple[str, list[dict], list[dict]]:
    """
    獲取 AI 熱圖摘要，並整合現有的正式事件，確保「情報 -> 摘要 -> 應變」流程連貫。
    """
    events_df = get_active_risk_events()
    events_text = "目前尚無已登錄事件。"
    if events_df is not None and not events_df.empty:
        # 只列出最近的 15 筆事件作為背景
        events_text = "\n".join([
            f"- 【{row['event_type']}】區域：{row['region'] or row['country']} (預計延遲：{row['impact_days']}天)"
            for _, row in events_df.head(15).iterrows()
        ])

    conn = sqlite3.connect(DB_FILE)
    try:
        # 僅選取正式供應商 (is_official=1) 的據點，確保建議清單精確對齊
        valid_regions_df = pd.read_sql_query("SELECT DISTINCT country, region FROM suppliers WHERE is_official=1 AND country IS NOT NULL", conn)
        valid_regions = []
        for _, r in valid_regions_df.iterrows():
            c = str(r['country']).strip()
            rg = str(r['region']).strip()
            if rg and rg != c:
                valid_regions.append(f"{c} {rg}")
            else:
                valid_regions.append(c)
        valid_regions_text = "、".join(set(valid_regions)) or "（目前無正式供應商據點資料，請跳過風險建議清單）"
    except Exception:
        valid_regions_text = "（系統讀取區域資料失敗，請跳過風險建議清單）"
    finally:
        conn.close()

    prompt = HEATMAP_AI_SUMMARY_PROMPT_V2.format(
        reference_date=reference_date,
        events_text=events_text,
        valid_regions_text=valid_regions_text,
        news_context=news_context or "目前尚無快取新聞，請先於頁面「更新即時新聞」取得最近新聞後再產生摘要。"
    )
    # 合法區域清單與展開表（code-side gate 用；如「台灣」→「台灣 北區/中區/南區」）
    valid_list = [v.strip() for v in (valid_regions_text or "").split("、") if v.strip()]
    name_expansions: dict = {}
    for v in valid_list:
        parts = v.split(" ")
        c = parts[0]
        name_expansions.setdefault(c, [])
        if v not in name_expansions[c]:
            name_expansions[c].append(v)
        if len(parts) > 1:
            r = parts[1]
            name_expansions.setdefault(r, [])
            if v not in name_expansions[r]:
                name_expansions[r].append(v)

    try:
        # issue #27/#47：統一 LLM 入口 + 結構化輸出（JSON）。
        # 合法區域檢核由 _gate_heatmap_updates 執行，prompt 只做平述引導；
        # 原本的 UPDATE:/EVENT: 行解析與正文回填 regex 全數移除。
        import json
        from backend.llm_client import complete_text
        raw = (complete_text(prompt, temperature=0.3, json_mode=True,
                             tag="analysis:heatmap") or "").strip()
        if not raw:
            return "AI 摘要失敗：模型未回傳內容。", [], []
        payload = json.loads(re.sub(r"```json\s*|```\s*", "", raw))

        summary = str(payload.get("摘要") or "").strip() or "（AI 未提供摘要內容）"
        updates = _gate_heatmap_updates(payload.get("更新"), valid_list, name_expansions)
        suggested_events = _coerce_heatmap_events(payload.get("事件"))
        return summary, updates, suggested_events
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"AI 摘要解析失敗：{e}", [], []


def apply_heatmap_updates(updates, ai_summary=None, *, actor=None):
    """
    將 AI 回傳的 UPDATE 清單套用到熱圖。
    - 若 update 的 display_name 為廣域地區（如「亞洲」），則將該地區內所有熱點都更新為對應 risk_pct。
    - 否則依「display_name 包含於熱點 display_name」匹配單一熱點後更新。
    """
    require_capability(actor, RISK_WORKSPACE_WRITE)
    if not updates:
        return 0
    heatmap_rows = get_risk_heatmap_data()
    if not heatmap_rows:
        return
    summary_snippet = (ai_summary or "")[:500]
    for u in updates:
        name = (u.get("display_name") or "").strip()
        risk_pct = u.get("risk_pct")
    # 建立別名映射以提升匹配率
    synonyms = {"韓國": "南韓", "南韓": "韓國", "美國": "美洲", "德國": "德國"}
    
    matched_count = 0
    for u in updates:
        name = (u.get("display_name") or "").strip()
        risk_pct = u.get("risk_pct")
        if not name or risk_pct is None:
            continue
        
        target_names = [name]
        if name in synonyms:
            target_names.append(synonyms[name])
            
        # 1. 廣域地區匹配
        is_region_match = False
        for t_name in target_names:
            if t_name in REGION_COUNTRY_MAP:
                countries = REGION_COUNTRY_MAP[t_name]
                for r in heatmap_rows:
                    country = (r.get("region_key") or "").split("|")[0].strip()
                    if country in countries:
                        upsert_risk_heatmap(
                            r["region_key"], r["display_name"], r["latitude"], r["longitude"],
                            float(risk_pct), summary_snippet, actor=actor,
                        )
                        matched_count += 1
                is_region_match = True
                break
        
        if is_region_match:
            continue
            
        # 2. 國家/地區精準或模糊匹配
        for r in heatmap_rows:
            d_name = r.get("display_name") or ""
            r_key = r.get("region_key") or ""
            country_part = r_key.split("|")[0] if "|" in r_key else d_name
            
            matched = False
            for t_name in target_names:
                # 匹配邏輯：名稱包含、國家部包含、或熱點名稱包含
                if t_name in d_name or t_name in country_part or d_name in t_name:
                    matched = True
                    break
            
            if matched:
                upsert_risk_heatmap(
                    r["region_key"], r["display_name"], r["latitude"], r["longitude"],
                    float(risk_pct), summary_snippet, actor=actor,
                )
                matched_count += 1
    return matched_count



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




def get_total_impact_amount(region_key):
    """計算特定地區受波及的採購總金額 (美元)。"""
    conn = sqlite3.connect(DB_FILE)
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
    return res[0] if res else {"is_relevant": True, "country": "", "region": "", "event_type": "其他", "estimated_delay": 0, "chinese_summary": ""}


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
            raw_text = (complete_text(prompt, temperature=0.1, json_mode=True,
                                      tag="analysis:news_batch") or "").strip()
        except Exception as e:
            print("批量新聞分析失敗，回傳預設 7 天延遲。錯誤:", e)
            return [{"is_relevant": True, "country": "", "region": "", "event_type": "其他", "estimated_delay": 7, "chinese_summary": f"AI 分析失敗: {e}"}] * len(news_texts)
        # 去除 markdown 程式碼區塊符號
        clean_json = re.sub(r"```json\s*", "", raw_text)
        clean_json = re.sub(r"```\s*", "", clean_json)
        
        payload = json.loads(clean_json)
        # issue #47 P0-2：頂層改為物件 {"results": [...]}（json_object 模式規格要求）；
        # 相容舊版頂層 array（模型偶爾仍會直接回 array）
        data = payload.get("results", []) if isinstance(payload, dict) else payload
        # 映射回原始順序
        results = [{"is_relevant": True, "country": "", "region": "", "event_type": "其他", "estimated_delay": 0, "chinese_summary": ""}] * len(news_texts)
        for item in data:
            idx = item.get("news_id")
            if idx is not None and 0 <= idx < len(results):
                results[idx] = {
                    "is_relevant": item.get("相關性") == "YES",
                    "country": item.get("國家") if item.get("國家") != "不明" else "",
                    "region": item.get("地區") if item.get("地區") != "不明" else "",
                    "event_type": item.get("事件類型") or "其他",
                    "chinese_summary": item.get("繁體中文簡要") or "",
                    "estimated_delay": item.get("預計延遲") or 0
                }
        return results
    except Exception as e:
        import traceback
        traceback.print_exc()
        return [{"is_relevant": True, "country": "", "region": "", "event_type": "其他", "estimated_delay": 7, "chinese_summary": f"系統錯誤: {e}"}] * len(news_texts)


# ── 受災採購清單 (Impacted PO List) ────────────────────────────────────

def get_impacted_pos(region_key=None, country=None, supplier_id=None):
    """依熱點（地區/國家）或供應商 ID 篩選未結案採購單，回傳：採購單號、供應商、關鍵物料、預計延遲、替代建議。"""
    conn = sqlite3.connect(DB_FILE)
    where, params = ["(p.status IS NULL OR p.status NOT IN ('已完成','已取消'))"], []
    if supplier_id:
        where.append("p.supplier_id = ?")
        params.append(supplier_id)
    if region_key:
        where_sub, params_sub = _get_expanded_region_where(region_key, None, prefix="s.")
        where.extend(where_sub)
        params.extend(params_sub)
    if country:
        where_sub, params_sub = _get_expanded_region_where(None, country, prefix="s.")
        where.extend(where_sub)
        params.extend(params_sub)
    q = """
    SELECT p.po_id, p.supplier_id, s.name as supplier_name, s.country, s.region,
           p.estimated_delay_days, p.alternative_suggestion
    FROM purchase_orders p
    JOIN suppliers s ON p.supplier_id = s.supplier_id
    WHERE """ + " AND ".join(where)
    pos = __pd_read(q, conn, params=tuple(params))
    conn.close()
    if pos is None or pos.empty:
        return []
    out = []
    conn = sqlite3.connect(DB_FILE)
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
            "supplier_name": row["supplier_name"],
            "key_materials": key_materials,
            "estimated_delay": delay_str,
            "alternative_suggestion": alt,
        })
    conn.close()
    return out


def update_po_impact(
    po_id, estimated_delay_days=None, alternative_suggestion=None, *, actor=None
):
    """更新採購單的預計延遲天數與替代建議。"""
    require_capability(actor, ERP_POLICY_WRITE)
    conn = sqlite3.connect(DB_FILE)
    if estimated_delay_days is not None:
        conn.execute("UPDATE purchase_orders SET estimated_delay_days = ? WHERE po_id = ?", (estimated_delay_days, po_id))
    if alternative_suggestion is not None:
        conn.execute("UPDATE purchase_orders SET alternative_suggestion = ? WHERE po_id = ?", (alternative_suggestion, po_id))
    conn.commit()
    conn.close()


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
            hotspot_parts = [p.strip() for p in (hotspot_name or "").replace(" ", " ").split() if p.strip()]
            seen = set()
            parts = []
            for _, s in suppliers.iterrows():
                country = (s.get("country") or "").strip()
                region = (s.get("region") or "").strip() or country
                if not country:
                    continue
                # 若該據點屬於當前熱點（國家或地區名重合）則跳過
                if any(p in country or p in region for p in hotspot_parts):
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
        payload = json.loads(re.sub(r"```json\s*|```\s*", "", raw))
        items = payload.get("results", []) if isinstance(payload, dict) else payload

        result = []
        for it in items or []:
            it = it or {}
            po_id = str(it.get("po_id") or "").strip()
            if po_id not in po_ids:
                continue
            try:
                delay_days = int(it.get("延遲天數", 7) or 7)
            except (TypeError, ValueError):
                delay_days = 7
            suggestion = str(it.get("建議") or "").strip()
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
    conn = sqlite3.connect(DB_FILE)
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
    conn = sqlite3.connect(DB_FILE)
    df = __pd_read(
        "SELECT id, event_type, region, country, impact_days, description, created_at, news_id FROM supply_chain_events ORDER BY id DESC LIMIT ?",
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
    conn = sqlite3.connect(DB_FILE)
    # 1. 30 天內事件數
    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")
    event_count = conn.execute("SELECT COUNT(*) FROM supply_chain_events WHERE created_at >= ?", (since,)).fetchone()[0]
    
    # 2. 受波及供應商與訂單 (去重)
    # 取得最近 50 件事件作為代表性 KPI
    active_events = conn.execute("SELECT region, country, impact_days FROM supply_chain_events ORDER BY id DESC LIMIT 50").fetchall()
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
    conn = sqlite3.connect(DB_FILE)
    try:
        # 統計各類事件的平均值與次數
        res = conn.execute(
            """SELECT event_type, AVG(impact_days) as avg_days, COUNT(*) as cnt 
               FROM supply_chain_events 
               WHERE impact_days > 0 
               GROUP BY event_type 
               ORDER BY cnt DESC"""
        ).fetchall()
        return res # [(type, avg, count), ...]
    except Exception:
        return []
    finally:
        conn.close()


def add_risk_event(
    event_type, region, country, impact_days, description, news_id=None, *, actor=None
):
    """新增或更新風險事件（如果該區域已存在事件則覆蓋）。"""
    require_capability(actor, RISK_WORKSPACE_WRITE)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # 核心優化：直接覆寫同區域的正式事件 (news_id 為空者)
    c.execute(
        """SELECT id FROM supply_chain_events 
           WHERE COALESCE(country, '') = ? AND COALESCE(region, '') = ? AND news_id IS ?""",
        (country or "", region or "", news_id)
    )
    existing = c.fetchone()
    
    if existing:
        event_id = existing[0]
        c.execute(
            """UPDATE supply_chain_events 
               SET event_type=?, impact_days=?, description=?, created_at=?
               WHERE id=?""",
            (event_type, impact_days, description or None, datetime.now().strftime("%Y-%m-%d %H:%M"), event_id)
        )
        conn.commit()
        conn.close()
        return event_id
    else:
        c.execute(
            "INSERT INTO supply_chain_events (event_type, region, country, impact_days, description, created_at, news_id) VALUES (?,?,?,?,?,?,?)",
            (event_type, region or None, country or None, impact_days, description or None, datetime.now().strftime("%Y-%m-%d %H:%M"), news_id)
        )
        new_id = c.lastrowid
        conn.commit()
        conn.close()
        return new_id


def delete_risk_event(event_id, *, actor=None):
    """刪除一筆風險事件。"""
    require_capability(actor, RISK_WORKSPACE_WRITE)
    run_query("DELETE FROM supply_chain_events WHERE id = ?", (event_id,), fetch=False)


def get_affected_suppliers_by_event(region: str, country: str = None):
    """依地區與國家篩選受影響的正式供應商（僅限 is_official=1）。"""
    conn = sqlite3.connect(DB_FILE)
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
    conn = sqlite3.connect(DB_FILE)
    
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
    conn = sqlite3.connect(DB_FILE)
    where = []
    params = []
    
    combined_loc = f"{region} {country}".strip()
    if combined_loc:
        where_sub, params_sub = _get_expanded_region_where(combined_loc, None, prefix="s.")
        where.extend(where_sub)
        params.extend(params_sub)
        
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
    conn = sqlite3.connect(DB_FILE)
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
    conn = sqlite3.connect(DB_FILE)
    # 僅針對有設定 baseline 的進行還原
    conn.execute("UPDATE inventory SET reorder_point = baseline_reorder_point WHERE baseline_reorder_point IS NOT NULL")
    conn.commit()
    conn.close()
    return True
def update_reorder_point(product_id: str, new_reorder_point: int, *, actor=None):
    """手動更新指定物料的安全庫存水位。"""
    require_capability(actor, ERP_POLICY_WRITE)
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "UPDATE inventory SET reorder_point = ? WHERE product_id = ?",
        (int(new_reorder_point), product_id)
    )
    conn.commit()
    conn.close()

def get_event_risk_scores():
    """取得事件類型對應的風險分數（event_type -> score）。"""
    conn = sqlite3.connect(DB_FILE)
    df = __pd_read(
        "SELECT risk_type, risk_key, risk_score, weight FROM esg_risk_factors WHERE risk_type = 'event_type'", conn)
    conn.close()
    if df is None or df.empty:
        return {}
    return dict(zip(df["risk_key"], df["risk_score"] * df["weight"]))


def get_region_risk_scores():
    """取得地區對應的風險分數（region key -> score）。"""
    conn = sqlite3.connect(DB_FILE)
    df = __pd_read("SELECT risk_type, risk_key, risk_score, weight FROM esg_risk_factors WHERE risk_type = 'region'", conn)
    conn.close()
    if df is None or df.empty:
        return {}
    return dict(zip(df["risk_key"], df["risk_score"] * df["weight"]))


# ── 風險係數管理 ────────────────────────────────────────────────────────

def get_risk_factors():
    """取得所有風險係數（id, 類型, 代碼, 風險分數, 權重, 備註, 更新時間）。"""
    conn = sqlite3.connect(DB_FILE)
    df = __pd_read(
        "SELECT id, risk_type as 類型, risk_key as 代碼, risk_score as 風險分數, weight as 權重, note as 備註, updated_at as 更新時間 FROM esg_risk_factors ORDER BY risk_type, risk_key",
        conn,
    )
    conn.close()
    return df


def get_risk_factors_raw():
    """取得原始欄位名的風險係數（供加權計算、預覽用）。"""
    conn = sqlite3.connect(DB_FILE)
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
    conn = sqlite3.connect(DB_FILE)
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
            if rk in name or name in rk:
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
    conn = sqlite3.connect(DB_FILE)
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
            if rk in display_name or rk in key:
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
    conn = sqlite3.connect(DB_FILE)
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
            if k in str(p.get("region") or "") or k in str(p.get("country") or ""):
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
