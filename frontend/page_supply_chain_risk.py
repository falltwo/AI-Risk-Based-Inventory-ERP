"""
frontend/page_supply_chain_risk.py
🌱 供應鏈與風險（前端 UI）
細項：供應鏈地圖、風險事件與交期
由 components/supply_map.py 與 components/risk_dashboard.py 提供顯示層。
"""

import streamlit as st
from frontend.components.supply_map import render_supply_chain_map, render_what_if_analysis
from frontend.components.risk_dashboard import render_intelligence_gathering, render_response_execution
from frontend.components.risk_overview import render_risk_overview

def render(sub_menu: str, api_key: str, gnews_api_key: str = "", gemini_model: str = "gemini-2.5-flash"):
    st.markdown("<div class='premium-title'>🌱 供應鏈與風險監控</div>", unsafe_allow_html=True)
    
    # 使用 Tabs 切換總覽與詳細分析
    tab1, tab2 = st.tabs(["📊 風險總覽", "🌍 詳細監控與分析"])
    
    with tab1:
        render_risk_overview()
        
    with tab2:
        # Step 1: Intelligence Hub
        st.markdown("### 📡 步驟 1: 即時情報獲取與 AI 摘要")
        render_intelligence_gathering(api_key=api_key, gnews_api_key=gnews_api_key, gemini_model=gemini_model)
        
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("---")
        
        # Step 2: Global Risk Monitoring
        st.markdown("### 🌍 步驟 2: 原物料風險管理地圖")
        render_supply_chain_map(api_key, gnews_api_key, gemini_model)
        
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("---")
        
        # Step 3: Response Execution 
        st.markdown("### ⚡ 步驟 3: 風險分析與應變執行")
        render_response_execution(api_key=api_key, gnews_api_key=gnews_api_key, gemini_model=gemini_model)

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("---")

        # Step 4: What-If Simulation
        st.markdown("### 🔮 步驟 4: 情境模擬分析")
        render_what_if_analysis(api_key=api_key, gemini_model=gemini_model)

