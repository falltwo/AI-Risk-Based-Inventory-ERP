import streamlit as st
from frontend.ui_utils import show_error
from backend.supply_chain_risk import get_supply_chain_summary_kpis
from frontend.components.supply_map import render_risk_heatmap

def render_risk_overview():
    """渲染供應鏈風險總覽：KPI 卡片 + 即時風險熱圖。"""
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
