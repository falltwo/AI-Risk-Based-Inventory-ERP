from backend.region_matching import matches_location, split_location
from backend.supply_chain_risk import build_heatmap_review_rows
import streamlit as st
import pandas as pd
import plotly.express as px
from backend.supply_chain_news import get_news_from_db
from backend.supply_chain_risk import (
    get_risk_heatmap_data,
    get_heatmap_ai_summary,
    apply_heatmap_updates,
    upsert_risk_heatmap,
    reset_risk_heatmap_to_initial,
    get_impacted_pos,
    update_po_impact,
    get_ai_alternative_suggestions,
    what_if_simulation,
)


def _wrap_text(text, width=40):
    """Wrap scalar hover text while treating database/Pandas nulls as empty."""
    if text is None:
        return ""
    try:
        if pd.isna(text):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(text)
    lines = []
    for i in range(0, len(text), width):
        lines.append(text[i:i + width])
    return "<br>".join(lines)


def render_risk_heatmap(key: str = "risk_heatmap", heatmap_rows=None):
    """僅渲染風險熱圖 (Plotly Chart)。"""
    if heatmap_rows is None:
        heatmap_rows = get_risk_heatmap_data()
    if heatmap_rows:
        df_heat = pd.DataFrame(heatmap_rows)
        df_heat["risk_pct"] = df_heat["risk_pct"].fillna(20)
        df_heat["hover_info"] = df_heat.apply(
            lambda r: f"<b>{r['display_name']}</b><br>對您公司的影響：{r['risk_pct']:.0f}%<br>{_wrap_text(r['ai_summary'], 45)}",
            axis=1,
        )
        fig = px.scatter_geo(
            df_heat,
            lat="latitude",
            lon="longitude",
            color="risk_pct",
            color_continuous_scale="Reds",
            range_color=[0, 100],
            hover_name="display_name",
            custom_data=["hover_info"],
            size=[15] * len(df_heat),
            title="全球風險熱圖 — 顏色越深表示對您公司供應鏈影響越大",
        )
        fig.update_traces(
            hovertemplate="%{customdata[0]}<extra></extra>",
            marker=dict(line=dict(width=1, color="darkred")),
        )
        fig.update_layout(
            geo=dict(
                showland=True, landcolor="lightgray",
                showocean=True, oceancolor="aliceblue",
                showcountries=True, countrycolor="white",
                projection_type="natural earth",
            ),
            margin=dict(l=0, r=0, t=40, b=0),
            coloraxis_colorbar=dict(title="影響 %"),
        )
        st.plotly_chart(fig, use_container_width=True, key=key)

def render_risk_shortcuts(key: str, heatmap_rows=None, *, actor: str):
    """區域風險快速分析小卡。"""
    if heatmap_rows is None:
        heatmap_rows = get_risk_heatmap_data()
    if heatmap_rows:
        # 如果有新情報登錄且尚未重新摘要，給予提示
        if st.session_state.get("heatmap_needs_refresh"):
            st.warning("⚠️ 偵測到新的風險登錄，請點擊下方「產生分析」以更新地圖與摘要。")
            
        high_risk_regions = [r for r in heatmap_rows if (r.get("risk_pct") or 0) > 20]
        # 依風險百分比由高至低排序
        high_risk_regions.sort(key=lambda x: x.get("risk_pct") or 0, reverse=True)
        
        if high_risk_regions:
            st.markdown("#### ⚡ 區域風險快速分析")
            st.caption("點擊下方區域即可快速登錄事件或查看現有應變計畫。點擊下方的「分析衝擊」會自動帶您進入詳細應變區。")
            
            # 建立 3 列的小卡片
            cols = st.columns(3)
            from backend.supply_chain_risk import get_active_risk_events
            active_events = get_active_risk_events()
            
            for i, reg in enumerate(high_risk_regions[:6]): # 最多顯示 6 個
                with cols[i % 3]:
                    color = "#EF4444" if reg['risk_pct'] > 60 else "#F59E0B"
                    
                    # 1. 取得總結名稱並計算曝險金額
                    reg_display = reg.get('display_name') or ""
                    from backend.supply_chain_risk import get_total_impact_amount
                    impact_amt = get_total_impact_amount(reg_display)
                    impact_display = f"${impact_amt:,.0f}"

                    # 2. 尋找現有正式事件 (非新聞初篩登錄)
                    from backend.supply_chain_risk import get_active_risk_events
                    active_events = get_active_risk_events()
                    found_ev = None
                    if active_events is not None and not active_events.empty:
                        # 分別對應國家與地區
                        dn_parts = (reg.get('display_name') or "").split(" ", 1)
                        c_name = dn_parts[0].strip().lower()
                        r_name = dn_parts[1].strip().lower() if len(dn_parts) > 1 else ""
                        
                        for _, ev in active_events.iterrows():
                            # 只比對正式事件 (news_id 為空)
                            if pd.isna(ev.get('news_id')):
                                ev_c = (ev.get('country') or "").strip().lower()
                                ev_r = (ev.get('region') or "").strip().lower()
                                if matches_location(*split_location(reg['region_key']), ev_r, ev_c):
                                    found_ev = ev
                                    break
                    
                    # 3. 比對 AI 最新建議 (檢查是否天數有更新)
                    s_events = st.session_state.get("suggested_events", [])
                    match_suggest = None
                    for sev in s_events:
                        s_r, s_c = (sev.get('region') or "").strip().lower(), (sev.get('country') or "").strip().lower()
                        if sev.get('impact_days') is not None and matches_location(*split_location(reg['region_key']), s_r, s_c):
                            match_suggest = sev
                            break
                    
                    if reg.get("estimated_delay") is not None and pd.notna(reg.get("estimated_delay")):
                        c, r = split_location(reg["region_key"])
                        match_suggest = dict(country=c, region=r, impact_days=int(reg["estimated_delay"]),
                                             event_type="其他", description=reg.get("ai_summary") or "已保存的熱圖建議")

                    # 判定按鈕狀態
                    btn_state = "add" # 待登錄
                    if found_ev is not None:
                        # 只有當 AI 建議的天數與現有計畫「不一致」時，才顯示「更新應變建議」
                        # 這樣一鍵更新後，兩者數據一致，狀態就會自動變回綠色的「查看分析」
                        suggested_days = int(match_suggest.get('impact_days', 0)) if match_suggest else None
                        actual_days = int(found_ev.get('impact_days', 0))
                        
                        if suggested_days is not None and suggested_days != actual_days:
                            btn_state = "update"
                        else:
                            btn_state = "view" # 執行中
                    elif match_suggest:
                        btn_state = "ready" # 建議啟動

                    status_badge = ""
                    if btn_state == "view":
                        status_badge = '<div style="background: #D1FAE5; color: #065F46; font-size: 0.7rem; padding: 2px 6px; border-radius: 4px; display: inline-block; margin-bottom: 5px;">✅ 應變執行中</div>'
                    elif btn_state == "update":
                        status_badge = '<div style="background: #FEF3C7; color: #92400E; font-size: 0.7rem; padding: 2px 6px; border-radius: 4px; display: inline-block; margin-bottom: 5px;">⚠️ 數據異動(建議更新)</div>'
                    elif btn_state == "ready":
                        status_badge = '<div style="background: #DBEAFE; color: #1E40AF; font-size: 0.7rem; padding: 2px 6px; border-radius: 4px; display: inline-block; margin-bottom: 5px;">⚡ AI 建議啟動應變</div>'
                    else:
                        status_badge = '<div style="background: #F3F4F6; color: #374151; font-size: 0.7rem; padding: 2px 6px; border-radius: 4px; display: inline-block; margin-bottom: 5px;">🔍 待評估</div>'

                    st.markdown(f"""
                    <div style="border: 1px solid #e0e0e0; border-radius: 10px; padding: 12px; margin-bottom: 10px; border-left: 5px solid {color}; shadow: 0 4px 6px rgba(0,0,0,0.05);">
                        {status_badge}
                        <div style="font-size: 0.85rem; color: #666; font-weight: 500;">{reg['display_name']}</div>
                        <div style="font-size: 1.25rem; font-weight: bold; color: {color};">{reg['risk_pct']:.0f}% <span style="font-size: 0.8rem;">風險</span></div>
                        <div style="font-size: 0.75rem; color: #6B7280; margin-top: 5px;">曝險金額: <b style="color:#111827;">{impact_display}</b></div>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    if btn_state == "view":
                        if st.button(f"📊 查看分析", key=f"{key}_quick_anal_{reg.get('region_key') or i}_{i}", use_container_width=True, type="secondary"):
                            st.session_state["active_risk_event_id"] = found_ev["id"]
                            st.rerun()
                    elif btn_state == "update":
                        if st.button(f"🔄 更新應變建議", key=f"{key}_upd_{reg.get('region_key') or i}", use_container_width=True, type="primary"):
                            # 執行覆寫更新
                            impact_days = match_suggest.get('impact_days', 7)
                            etype = match_suggest.get('event_type', '其他')
                            desc = f"【AI 建議更新】{match_suggest.get('description', '')}"
                            ev_c, ev_r = split_location(reg["region_key"])
                            
                            from backend.supply_chain_risk import add_risk_event
                            add_risk_event(
                                etype, ev_r, ev_c, impact_days, desc, actor=actor
                            )
                            st.toast(f"✅ 已將 {reg_display} 的數據更新", icon="🔄")
                            st.rerun()
                    elif btn_state == "ready":
                        if st.button("⚡ 啟動 AI 建議應變", key=f"{key}_heat_ana_ready_{i}_{reg_display}", use_container_width=True, type="primary"):
                            st.session_state["selected_region_for_response"] = reg_display
                            impact_days = match_suggest.get('impact_days', 7)
                            etype = match_suggest.get('event_type', '其他')
                            desc = f"AI 熱圖分析：{match_suggest.get('description', '')}"
                            ev_c, ev_r = split_location(reg["region_key"])
                            from backend.supply_chain_risk import add_risk_event
                            add_risk_event(
                                etype, ev_r, ev_c, impact_days, desc, actor=actor
                            )
                            st.session_state["heatmap_needs_refresh"] = True
                            st.toast(f"📍 已啟動 {reg_display} 應變計畫", icon="🤖")
                            st.rerun()
                    else:
                        manual_days = st.number_input("手動確認延遲天數", min_value=0, max_value=365, value=0, key=f"manual_days_{key}_{i}")
                        if st.button("🏗️ 加入應變計畫", key=f"{key}_heat_ana_manual_{i}_{reg_display}", use_container_width=True):
                            st.session_state["selected_region_for_response"] = reg_display
                            impact_days, etype, desc = manual_days, "其他", f"手動加入：偵測到 {reg_display} 高風險。"
                            ev_c, ev_r = split_location(reg["region_key"])
                            from backend.supply_chain_risk import add_risk_event
                            add_risk_event(
                                etype, ev_r, ev_c, impact_days, desc, actor=actor
                            )
                            st.session_state["heatmap_needs_refresh"] = True
                            st.rerun()

            if len(high_risk_regions) > 6:
                st.markdown("<br>", unsafe_allow_html=True)
                with st.expander(f"➕ 查看並登錄其他 {len(high_risk_regions) - 6} 個高風險區域", expanded=False):
                    other_regs = high_risk_regions[6:]
                    c1, c2, c3 = st.columns([2, 1, 1])
                    with c1:
                        opt_names = [f"{r['display_name']} ({r['risk_pct']}%)" for r in other_regs]
                        sel_idx = st.selectbox("選擇其他高風險區域", range(len(opt_names)), format_func=lambda i: opt_names[i], key=f"{key}_other_reg_sel", label_visibility="collapsed")
                        selected_r = other_regs[sel_idx]
                    with c2:
                        # 顯示曝險金額
                        from backend.supply_chain_risk import get_total_impact_amount
                        impact_amt = get_total_impact_amount(selected_r.get('display_name'))
                        st.markdown(f"<div style='padding-top:8px; color:#666;'>曝險金額: <b>${impact_amt:,.0f}</b></div>", unsafe_allow_html=True)
                    with c3:
                        persisted_days = selected_r.get("estimated_delay")
                        confirmed_days = st.number_input("確認延遲天數", min_value=0, max_value=365,
                            value=int(persisted_days) if persisted_days is not None and pd.notna(persisted_days) else 0,
                            key=f"{key}_other_days_{selected_r['region_key']}")
                        if st.button("🏗️ 加入應變計畫", key=f"{key}_other_reg_btn", use_container_width=True, type="secondary"):
                            c, r = split_location(selected_r["region_key"])
                            from backend.supply_chain_risk import add_risk_event
                            add_risk_event("其他", r, c, confirmed_days,
                                selected_r.get("ai_summary") or "手動確認的應變計畫", actor=actor)
                            st.session_state["heatmap_needs_refresh"] = True
                            st.rerun()


def render_supply_chain_map(
    api_key: str,
    gnews_api_key: str,
    gemini_model: str = "gemini-2.5-flash",
    *,
    actor: str,
):
    """供應鏈地圖：第一層即時風險熱圖 + AI 摘要，第二層受災採購清單，第三層 What-If 模擬。"""
    st.subheader("🌍 原物料風險管理地圖")
    st.caption("熱圖顯示與管理、AI 深度摘要。")

    heatmap_rows = get_risk_heatmap_data()

    # ── 即時風險熱圖 (Risk Heatmap) ─────────────────────────────────
    render_risk_heatmap(key="detail_heatmap", heatmap_rows=heatmap_rows)

    # AI 摘要（使用最近最新新聞）
    st.markdown("**AI 摘要**")
    news_context = ""
    try:
        news_list = get_news_from_db(limit=10, order_by_latest=True, within_days=30, analyzed_only=True)
        if news_list:
            news_context = "\\n".join([
                (n.get("title") or "") + " " + (n.get("analysis_summary") or n.get("summary") or "")[:200] +
                f" [{n.get('published_at') or n.get('fetched_at') or ''}, 預估延遲: {n.get('estimated_delay') if n.get('estimated_delay') is not None else '未知'}天]"
                for n in news_list
            ])
    except Exception:
        pass
    from datetime import datetime
    ref_date = datetime.now().strftime("%Y-%m-%d")
    col_ai_btn, col_reset = st.columns(2)
    with col_ai_btn:
        if st.button("🔄 產生／更新即時風險摘要", key="heatmap_ai_btn"):
            with st.spinner("AI 正在分析情報並偵測風險等級..."):
                summary_text, updates, suggested_events = get_heatmap_ai_summary(api_key, news_context, reference_date=ref_date, model=gemini_model)
                st.session_state["heatmap_ai_summary"] = summary_text
                st.session_state["heatmap_updates"] = updates
                st.session_state["suggested_events"] = suggested_events
                if "heatmap_needs_refresh" in st.session_state:
                    del st.session_state["heatmap_needs_refresh"]
            st.rerun()
    with col_reset:
        if st.button("🔄 重置為初始熱圖", key="reset_heatmap_btn"):
            reset_risk_heatmap_to_initial(actor=actor)
            for key in ["heatmap_ai_summary", "heatmap_updates", "suggested_events"]:
                if key in st.session_state: del st.session_state[key]
            st.success("已重置為初始熱圖。")
            st.rerun()

    if "heatmap_ai_summary" in st.session_state:
        # issue #47 P1-1：摘要已由後端以結構化 JSON 產出（純敘事 markdown），
        # 原本剝離 UPDATE:/EVENT: 技術指令行的 regex 邏輯不再需要。
        with st.container(border=True):
            st.markdown("### 🤖 AI 供應鏈與地理風險深度分析")
            st.markdown(st.session_state["heatmap_ai_summary"])
        
        # --- 選擇性帶入：風險建議值 (Selective Apply Risk Updates) ---
        # 核心策略：完全使用熱圖節點清單（供應商產生），而不依賴 AI 的名稱自由發揮
        # AI 的更新建議只用來「查詢風險百分比」，最後對應到正確的熱圖節點名稱
        heatmap_rows_for_update = get_risk_heatmap_data()
        h_updates_raw = st.session_state.get("heatmap_updates", [])
        
        if heatmap_rows_for_update:
            st.markdown("##### 🎯 審核並套用 AI 風險建議")
            st.caption("套用會保存各據點的風險與延遲天數；空白代表未知，0 代表確認為零。正式事件需另行登錄。")
            
            table_rows = build_heatmap_review_rows(
                h_updates_raw, st.session_state.get("suggested_events", []), heatmap_rows_for_update
            )

            if table_rows:
                df_upd = pd.DataFrame(table_rows)
                edited_risk_df = st.data_editor(
                    df_upd,
                    column_config={
                        "套用": st.column_config.CheckboxColumn("是否套用", default=True),
                        "地區": st.column_config.TextColumn("熱點名稱", disabled=True),
                        "預估風險 (%)": st.column_config.NumberColumn("影響 %", min_value=0, max_value=100, step=1, required=True),
                        "預估延遲 (天)": st.column_config.NumberColumn("延遲天數", min_value=0, max_value=365, step=1)
                    },
                    hide_index=True,
                    use_container_width=True,
                    key="ai_risk_editor"
                )
                
                sel_risks = edited_risk_df[edited_risk_df["套用"] == True]
                if st.button(f"📥 套用打勾的 {len(sel_risks)} 個地區風險至地圖", key="apply_ai_risk_btn", type="primary", disabled=len(sel_risks)==0):
                    from backend.supply_chain_risk import apply_heatmap_updates
                    final_updates = [{"display_name": r["地區"], "risk_pct": r["預估風險 (%)"],
                                      "estimated_delay": None if pd.isna(r["預估延遲 (天)"]) else int(r["預估延遲 (天)"])}
                                     for _, r in sel_risks.iterrows()]

                    cnt = apply_heatmap_updates(
                        final_updates,
                        st.session_state["heatmap_ai_summary"],
                        actor=actor,
                    )
                    st.session_state.pop("suggested_events", None)
                    st.session_state["heatmap_apply_success"] = f"✅ 已成功同步 {cnt} 個地區的風險與延遲天數至資料庫（尚未登錄正式事件）！"
                    if "heatmap_updates" in st.session_state:
                        del st.session_state["heatmap_updates"]
                    st.rerun()
            elif "heatmap_apply_success" in st.session_state:
                st.success(st.session_state["heatmap_apply_success"])
                if st.button("知道了", key="clear_apply_msg"):
                    del st.session_state["heatmap_apply_success"]
                    st.rerun()
            else:
                st.info("AI 本次分析未偵測到與您供應商節點直接相關的變動建議。")
        
        # 移除原有的「審核 AI 偵測到之新事件」區塊（依需求隱藏）
        pass
    else:
        st.caption("提示：點擊「即時全球情報」區塊的「更新即時新聞」後，系統會自動同步更新此熱圖與 AI 摘要。")

    st.markdown("<br>", unsafe_allow_html=True)
    # ── 🔍 區域風險摘要與快速分析 (Regional Impact Shortcuts) ──────────
    render_risk_shortcuts(
        key="detail_shortcuts", heatmap_rows=heatmap_rows, actor=actor
    )

    # ── 手動調節熱圖風險% (使用 st.data_editor) ────────────────────────
    if heatmap_rows:
        df_heat = pd.DataFrame(heatmap_rows)
        with st.expander("✏️ 手動微調熱圖影響程度 (Heatmap Editing)", expanded=False):
            st.caption("您可手動調整特定地區的影響百分比 (0-100%)。")
            df_edit = df_heat[["region_key", "display_name", "risk_pct"]].copy()
            edited_df = st.data_editor(
                df_edit,
                column_config={
                    "region_key": None,
                    "display_name": st.column_config.TextColumn("熱點名稱", disabled=True),
                    "risk_pct": st.column_config.NumberColumn("影響 %", min_value=0, max_value=100, step=1, required=True)
                },
                hide_index=True,
                use_container_width=True,
                key="heatmap_data_editor"
            )
            if not edited_df.equals(df_edit):
                if st.button("💾 儲存並更新地圖"):
                    for idx, row in edited_df.iterrows():
                        old_val = df_edit.at[idx, "risk_pct"]
                        new_val = row["risk_pct"]
                        if old_val != new_val:
                            orig_row = df_heat[df_heat["region_key"] == row["region_key"]].iloc[0]
                            upsert_risk_heatmap(
                                orig_row["region_key"],
                                orig_row["display_name"],
                                orig_row["latitude"],
                                orig_row["longitude"],
                                float(new_val),
                                (orig_row.get("ai_summary") or "")[:500],
                                actor=actor,
                            )
                    st.success("地圖已更新。")
                    st.rerun()

def render_what_if_analysis(
    api_key: str,
    gemini_model: str = "gemini-2.5-flash",
    *,
    actor: str,
):
    """模擬情境分析 (What-If Simulation)。"""
    st.markdown("---")
    with st.expander("🔮 模擬情境分析 (What-If Simulation)", expanded=True):
        st.write("主動詢問 AI：例如「如果南海發生衝突導致航線中斷 1 個月，哪些訂單會斷貨？」AI 將依 ERP 資料回覆影響與建議。")
        
        st.markdown("**快速帶入情境範例：**")
        col1, col2, col3, _ = st.columns([1, 1, 1, 2])
        with col1:
            if st.button("🌊 紅海航線中斷 2 週", key="preset_1"):
                st.session_state["whatif_question"] = "如果紅海航線中斷 2 週，我司哪些採購單會受影響？"
        with col2:
            if st.button("🗾 台灣發生規模 7 地震", key="preset_2"):
                st.session_state["whatif_question"] = "如果台灣發生規模 7 以上的地震導致停工 3 天，哪些訂單會受影響？"
        with col3:
            if st.button("🚧 越南關口罷工 1 個月", key="preset_3"):
                st.session_state["whatif_question"] = "如果越南主要港口罷工 1 個月，我司庫存還能撐多久？"

        if "whatif_question" not in st.session_state:
            st.session_state["whatif_question"] = "如果南海發生衝突導致航線中斷 1 個月，哪些訂單會斷貨？"

        user_question = st.text_area(
            "輸入情境問題",
            height=80,
            key="whatif_question"
        )
        if st.button("執行 What-If 模擬分析", key="whatif_btn"):
            with st.spinner("AI 正在依供應商、採購單與庫存資料分析情境…"):
                answer = what_if_simulation(
                    api_key, user_question, model=gemini_model, actor=actor
                )
            st.markdown("**AI 回覆**")
            # 隱藏技術後綴
            clean_answer = answer.split("【自動化指令】")[0].strip()
            st.info(clean_answer)
            st.caption("範例回覆：「這將影響您 40% 的原材料供應。建議現在就將 X 物料的安全庫存從 30 天提高到 60 天。」")

