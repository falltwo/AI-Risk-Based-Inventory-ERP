import sqlite3

import pandas as pd
import streamlit as st

from backend.database import DB_FILE
from backend.erp_exchange import (
    build_purchase_order_template_csv,
    parse_purchase_order_csv,
)
from backend.l1_monitoring import map_purchase_rows_to_events
from backend.supply_chain_risk import (
    get_risk_events_list,
    get_supply_chain_summary_kpis,
)
from frontend.components.supply_map import render_risk_heatmap
from frontend.ui_utils import show_error


_L1_DISPLAY_COLUMNS = {
    "po_id": "採購單",
    "supplier_id": "供應商",
    "product_id": "物料",
    "supplier_country": "國家",
    "supplier_region": "地區",
    "event_type": "命中事件",
    "impact_days": "預估延遲天數",
    "match_status": "對映結果",
    "notification_status": "通知狀態",
}


def _load_supplier_context(supplier_ids: set[str]) -> dict[str, dict]:
    if not supplier_ids:
        return {}
    placeholders = ",".join("?" for _ in supplier_ids)
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT supplier_id, country, region, risk_level "
            f"FROM suppliers WHERE supplier_id IN ({placeholders})",
            tuple(sorted(supplier_ids)),
        ).fetchall()
    return {row["supplier_id"]: dict(row) for row in rows}


def _render_latest_event_alerts(events: list[dict]) -> None:
    st.markdown("#### 🚨 最新事件告警")
    if not events:
        st.info("目前尚無已登錄的供應鏈風險事件。")
        return

    event_rows = []
    for event in events[:5]:
        event_rows.append(
            {
                "事件": event.get("event_type") or "未分類",
                "地區": event.get("region") or event.get("country") or "未設定",
                "預估延遲": f"{int(event.get('impact_days') or 0)} 天",
                "事件說明": event.get("description") or "未提供",
            }
        )
    st.dataframe(pd.DataFrame(event_rows), width="stretch", hide_index=True)


def _render_read_only_mapping(events: list[dict]) -> None:
    st.markdown("#### 🔔 L1 告警與通知中心")
    st.caption(
        "上傳資料只會在記憶體中進行格式驗證、事件對映與通知預覽，"
        "不會寫入 ERP 或提案暫存區。Excel 資料請先另存為 UTF-8 CSV。"
    )
    st.download_button(
        "下載唯讀對映 CSV 範本",
        data=build_purchase_order_template_csv(),
        file_name="l1_purchase_order_monitoring_template.csv",
        mime="text/csv",
        key="l1_monitor_download_template",
    )
    uploaded = st.file_uploader(
        "上傳採購資料 CSV",
        type=["csv"],
        key="l1_monitor_csv_upload",
        help="檔案必須為 UTF-8；上傳與對映均不會修改 ERP。",
    )
    if uploaded is None:
        st.info("可下載範本後匯入採購資料，以預覽事件對映與通知結果。")
        return

    try:
        purchase_rows = parse_purchase_order_csv(uploaded.getvalue())
        supplier_context = _load_supplier_context(
            {row["supplier_id"] for row in purchase_rows}
        )
        mapped_rows = map_purchase_rows_to_events(
            purchase_rows,
            supplier_context=supplier_context,
            events=events,
        )
    except ValueError as exc:
        st.error(f"CSV 驗證失敗：{exc}")
        return
    except sqlite3.Error:
        st.error("目前無法讀取供應商地區資料，請稍後再試。")
        return

    alert_rows = [row for row in mapped_rows if row["match_status"] == "需關注"]
    incomplete_rows = [
        row for row in mapped_rows if row["match_status"] == "資料待補"
    ]
    metric_a, metric_b, metric_c = st.columns(3)
    metric_a.metric("完成對映", f"{len(mapped_rows)} 筆")
    metric_b.metric("需通知", f"{len(alert_rows)} 筆")
    metric_c.metric("資料待補", f"{len(incomplete_rows)} 筆")

    display = pd.DataFrame(mapped_rows).rename(columns=_L1_DISPLAY_COLUMNS)
    st.dataframe(
        display[list(_L1_DISPLAY_COLUMNS.values())],
        width="stretch",
        hide_index=True,
    )

    st.markdown("##### 通知預覽（尚未發送）")
    if alert_rows:
        for row in alert_rows:
            st.warning(row["notification"])
    else:
        st.success("本次匯入資料未命中已登錄事件，無需發送風險通知。")

    export_rows = pd.DataFrame(
        {
            "採購單": [row.get("po_id") for row in mapped_rows],
            "供應商": [row.get("supplier_id") for row in mapped_rows],
            "對映結果": [row["match_status"] for row in mapped_rows],
            "通知狀態": [row["notification_status"] for row in mapped_rows],
            "通知內容": [row["notification"] for row in mapped_rows],
        }
    )
    st.download_button(
        "下載告警與通知清單",
        data=export_rows.to_csv(index=False).encode("utf-8-sig"),
        file_name="l1_alert_notifications.csv",
        mime="text/csv",
        key="l1_monitor_download_alerts",
    )

def render_risk_overview():
    """渲染 L1 唯讀閉環：事件告警、熱圖、資料對映與通知預覽。"""
    st.markdown("#### 📊 供應鏈風險總覽 (Risk Overview)")
    
    # 取得 KPI 數據
    try:
        kpis = get_supply_chain_summary_kpis()
    except Exception as e:
        show_error("KPI 數據讀取失敗", e)
        kpis = {"event_count": 0, "supplier_count": 0, "order_count": 0}

    # 顯示 KPI 卡片
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("📡 最新風險事件 (近30天)", f"{kpis['event_count']} 宗")
        st.caption("AI 偵測並登錄之供應鏈異常事件")
        
    with col2:
        st.metric("🏭 受波及供應商", f"{kpis['supplier_count']} 家")
        st.caption("位於受災區域且有進行中採購之供應商")
        
    with col3:
        st.metric("🧾 受波及銷售訂單", f"{kpis['order_count']} 筆")
        st.caption("因原材料延遲可能面臨交期風險之訂單")

    st.markdown("<br>", unsafe_allow_html=True)
    
    # 顯示熱圖
    with st.container(border=True):
        st.markdown("**🌍 全球即時風險熱圖**")
        render_risk_heatmap(key="overview_heatmap")

    st.markdown("<br>", unsafe_allow_html=True)
    try:
        event_frame = get_risk_events_list(limit=30)
        events = [] if event_frame is None or event_frame.empty else event_frame.to_dict("records")
    except Exception as exc:
        show_error("風險事件讀取失敗", exc)
        events = []

    _render_latest_event_alerts(events)
    st.markdown("<br>", unsafe_allow_html=True)
    _render_read_only_mapping(events)
