"""
frontend/page_esg.py
🌱 供應鏈透明化（風險係數管理、供應鏈地圖、風險事件與交期）
"""

import sqlite3
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime
from backend import DB_FILE, run_query


def render(sub_menu: str, api_key: str):
    st.markdown("<div class='premium-title'>🌱 供應鏈透明化</div>", unsafe_allow_html=True)
    menus = ['風險係數管理', '供應鏈地圖', '風險事件與交期']
    styled_menus = [f"🌟 **{m}**" if m == sub_menu else f"{m}" for m in menus]
    st.markdown(" ｜ ".join(styled_menus))

    if sub_menu == "風險係數管理":
        st.subheader("風險管理係數", divider="blue")
        st.caption("設定地區、事件類型或供應商類別的風險係數（0–100）與權重，供供應鏈與 ESG 風險評估使用。")
        with st.expander("➕ 新增／更新風險係數"):
            with st.form("risk_factor_form"):
                risk_type = st.selectbox("類型", ["region", "event_type", "supplier_category"], format_func=lambda x: {"region": "地區", "event_type": "事件類型", "supplier_category": "供應商類別"}[x])
                risk_key = st.text_input("代碼／名稱（如：東亞、地震、高風險）")
                risk_score = st.slider("風險分數 (0–100)", 0, 100, 50)
                weight = st.number_input("權重 (0–2)", min_value=0.0, max_value=2.0, value=1.0, step=0.1)
                note = st.text_input("備註")
                if st.form_submit_button("儲存"):
                    if risk_key and risk_key.strip():
                        now = datetime.now().strftime("%Y-%m-%d %H:%M")
                        run_query(
                            "INSERT OR REPLACE INTO esg_risk_factors (risk_type, risk_key, risk_score, weight, note, updated_at) VALUES (?,?,?,?,?,?)",
                            (risk_type, risk_key.strip(), float(risk_score), float(weight), note or None, now),
                            fetch=False,
                        )
                        st.success("已儲存風險係數")
                    else:
                        st.warning("請填寫代碼／名稱。")
        try:
            df_r = pd.read_sql_query(
                "SELECT risk_type as 類型, risk_key as 代碼, risk_score as 風險分數, weight as 權重, note as 備註, updated_at as 更新時間 FROM esg_risk_factors ORDER BY risk_type, risk_key",
                sqlite3.connect(DB_FILE),
            )
            if not df_r.empty:
                st.markdown("#### 風險係數一覽")
                st.dataframe(df_r, use_container_width=True, hide_index=True)
                st.markdown("#### 📊 風險係數分布")
                fig_r = px.bar(df_r, x="代碼", y="風險分數", color="類型", barmode="group", title="各項目風險分數", labels={"風險分數": "風險分數 (0–100)"})
                st.plotly_chart(fig_r, use_container_width=True)
                df_r["加權分"] = df_r["風險分數"] * df_r["權重"]
                by_type = df_r.groupby("類型").agg({"加權分": "mean", "風險分數": "count"}).reset_index()
                by_type.columns = ["類型", "平均加權分", "項目數"]
                fig_agg = px.pie(by_type, values="平均加權分", names="類型", title="依類型之平均加權風險占比")
                st.plotly_chart(fig_agg, use_container_width=True)
            else:
                st.info("尚無風險係數。請於上方表單新增。")
        except Exception:
            st.warning("請確認資料表 esg_risk_factors 已建立。")

    elif sub_menu == "供應鏈地圖":
        st.subheader("供應鏈動態地圖", divider="blue")
        st.caption("即時追蹤供應商地理位置與風險等級；請在「採購管理 → 供應商管理」補充國家、地區與經緯度。")
        try:
            df = pd.read_sql_query("SELECT supplier_id, name, country, region, latitude, longitude, risk_level FROM suppliers", sqlite3.connect(DB_FILE))
            df_map = df.dropna(subset=['latitude', 'longitude']) if 'latitude' in df.columns else pd.DataFrame()
            if not df_map.empty and len(df_map) > 0:
                df_map = df_map.rename(columns={"latitude": "lat", "longitude": "lon"})
                st.map(df_map, latitude="lat", longitude="lon", size=100)
            else:
                st.info("尚無供應商座標。請至「採購管理 → 供應商管理」為供應商填寫經緯度（latitude, longitude），或滙入含國家/地區的資料。")
            st.markdown("#### 供應商清單（國家 / 地區 / 風險）")
            if not df.empty:
                st.dataframe(df.fillna("—"), use_container_width=True, hide_index=True)
        except Exception:
            st.warning("請確認 suppliers 表已包含 country, region, latitude, longitude, risk_level 欄位。")

    else:  # 風險事件與交期
        st.subheader("風險事件與交期影響", divider="blue")
        st.caption("登錄突發事件（如地震、天候、政治風險），系統依供應商所在地評估對交期的影響。")
        with st.expander("➕ 新增風險事件"):
            with st.form("event_form"):
                etype = st.selectbox("事件類型", ["地震", "天候", "政治", "疫情", "其他"])
                region = st.text_input("影響地區（如：東亞、日本）")
                country = st.text_input("影響國家（選填）")
                impact_days = st.number_input("預估交期延遲天數", min_value=0, value=7)
                desc = st.text_area("說明")
                if st.form_submit_button("新增"):
                    run_query(
                        "INSERT INTO supply_chain_events (event_type, region, country, impact_days, description, created_at) VALUES (?,?,?,?,?,?)",
                        (etype, region or None, country or None, impact_days, desc or None, datetime.now().strftime("%Y-%m-%d %H:%M")),
                        fetch=False,
                    )
                    st.success("已登錄事件")
        try:
            events = pd.read_sql_query(
                "SELECT id, event_type, region, country, impact_days, description, created_at FROM supply_chain_events ORDER BY id DESC LIMIT 20",
                sqlite3.connect(DB_FILE),
            )
            if not events.empty:
                st.markdown("#### 近期風險事件")
                st.dataframe(events.rename(columns={"event_type": "類型", "region": "地區", "country": "國家", "impact_days": "延遲天數", "description": "說明", "created_at": "建立時間"}), use_container_width=True, hide_index=True)
                st.markdown("#### 受影響供應商與交期評估")
                for _, row in events.head(5).iterrows():
                    region, country, impact = row.get('region') or '', row.get('country') or '', row.get('impact_days') or 0
                    if not region and not country:
                        continue
                    conditions = []
                    params = []
                    if region:
                        conditions.append("region LIKE ?")
                        params.append(f"%{region}%")
                    if country:
                        conditions.append("country LIKE ?")
                        params.append(f"%{country}%")
                    where = " OR ".join(conditions) if conditions else "1=0"
                    q = "SELECT supplier_id, name, country, region FROM suppliers WHERE " + where
                    try:
                        affected = run_query(q, tuple(params))
                        if affected:
                            st.markdown(f"**{row.get('event_type')}** — {region or country}（+{impact} 天）")
                            for a in affected:
                                sid, name, c, r = (a[0], a[1], a[2], a[3]) if len(a) >= 4 else (a[0], a[1], "", "")
                                st.caption(f"・{sid} {name}（{c or r or '-'}）")
                    except Exception:
                        pass
            else:
                st.info("尚無風險事件。請先新增事件並確保供應商已填寫國家/地區。")
        except Exception as e:
            st.error(str(e))
