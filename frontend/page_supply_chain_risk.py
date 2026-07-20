"""
frontend/page_supply_chain_risk.py
🌱 供應鏈與風險（前端 UI）
細項：供應鏈地圖、風險事件與交期
由 components/supply_map.py 與 components/risk_dashboard.py 提供顯示層。
"""

import streamlit as st
from backend.access_control import load_principal
from frontend.access_navigation import risk_sections
from frontend.components.supply_map import render_supply_chain_map, render_what_if_analysis
from frontend.components.risk_dashboard import render_intelligence_gathering, render_response_execution
from frontend.components.risk_overview import render_risk_overview
from frontend.components.purchase_proposal_workbench import render_purchase_proposal_workbench

def render(
    sub_menu: str,
    api_key: str,
    gnews_api_key: str = "",
    gemini_model: str = "gemini-2.5-flash",
    username: str = "",
):
    principal = load_principal(username)
    if principal is None:
        st.error("登入身分已失效，無法讀取供應鏈風險資料。")
        return
    sections = risk_sections(principal)
    if "overview" not in sections:
        st.error("此帳號沒有供應鏈風險檢視權限。")
        return

    st.markdown("<div class='premium-title'>🌱 供應鏈與風險監控</div>", unsafe_allow_html=True)

    if "analysis" not in sections and "what_if" not in sections:
        render_risk_overview()
        return

    overview_tab, analysis_tab = st.tabs(["📊 L1 風險總覽", "🧭 L2 情報與決策"])

    with overview_tab:
        render_risk_overview()

    with analysis_tab:
        # Step 1: Intelligence Hub
        st.markdown("### 📡 步驟 1: 即時情報獲取與 AI 摘要")
        render_intelligence_gathering(
            api_key=api_key,
            gnews_api_key=gnews_api_key,
            gemini_model=gemini_model,
            actor=principal.username,
        )
        
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("---")
        
        # Step 2: Global Risk Monitoring
        st.markdown("### 🌍 步驟 2: 原物料風險管理地圖")
        render_supply_chain_map(
            api_key,
            gnews_api_key,
            gemini_model,
            actor=principal.username,
        )
        
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("---")
        
        # Step 3: Response Execution 
        st.markdown("### ⚡ 步驟 3: 風險分析與應變執行")
        render_response_execution(
            api_key=api_key,
            gnews_api_key=gnews_api_key,
            gemini_model=gemini_model,
            actor=principal.username,
        )

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("---")

        if "what_if" in sections:
            # Step 4: What-If Simulation
            st.markdown("### 🔮 步驟 4: 情境模擬分析")
            render_what_if_analysis(
                api_key=api_key,
                gemini_model=gemini_model,
                actor=principal.username,
            )

            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("---")
            st.markdown("### 🧾 步驟 5: 建立受治理的替代採購提案")
            render_purchase_proposal_workbench(actor=principal.username)

