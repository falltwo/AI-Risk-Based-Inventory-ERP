"""
backend/ai_supply_chain.py
供應鏈風險查詢，供 AI 智能助理呼叫的字串回傳包裝
"""
from backend.supply_chain_risk import (
    get_risk_events_list,
    get_impacted_pos,
    get_risk_heatmap_data
)

def get_supply_chain_risk_events(limit: int = 10) -> str:
    """查詢近期發生的供應鏈風險事件列表（包含事件類型、地區、國家、預計影響天數及說明）。"""
    df = get_risk_events_list(limit)
    if df is None or df.empty:
        return "近期沒有登錄任何供應鏈風險事件。"
    return "🌪️ 近期供應鏈風險事件：\n" + df.to_string(index=False)

def get_impacted_purchase_orders(region_or_country: str) -> str:
    """查詢特定地區或國家（例如 '台灣', '墨西哥' 或 '亞洲'）因為風險事件可能受到影響（延遲）的採購單。"""
    pos = get_impacted_pos(region_key=region_or_country)
    if not pos:
        return f"目前沒有查詢到與「{region_or_country}」相關的受影響採購單。"
    
    out = f"⚠️ 位於「{region_or_country}」的受影響採購單：\n"
    for x in pos:
         out += f"- PO單號: {x['po_id']} | 供應商: {x['supplier_name']} | 關鍵物料: {x['key_materials']} | 預計延遲: {x['estimated_delay']} | 建議: {x['alternative_suggestion']}\n"
    return out

def get_supply_chain_heatmap_summary() -> str:
    """查詢供應鏈地圖各據點的即時風險評估（0~100%）。"""
    heatmap = get_risk_heatmap_data()
    if not heatmap:
        return "目前沒有供應鏈風險地圖資料。"
    
    out = "🗺️ 供應鏈即時風險熱點評估：\n"
    for r in heatmap:
        summary = r.get("ai_summary") or "無"
        out += f"- 地區: {r['display_name']} | 風險: {r['risk_pct']}% | 摘要: {summary[:50]}...\n"
    return out
