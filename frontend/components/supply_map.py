from backend.region_matching import matches_location, split_location
from backend.supply_chain_risk import build_heatmap_review_rows
import streamlit as st
import pandas as pd
import plotly.express as px
from backend.supply_chain_news import get_news_from_db
from backend.supply_chain_risk import (
    get_risk_heatmap_data,
    analyze_heatmap_risk,
    get_latest_ai_risk_summary,
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


def _store_summary_result(result: dict) -> None:
    """analyze_heatmap_risk / get_latest_ai_risk_summary 的結果 → session_state（產生與載入共用）。"""
    if result.get("analysis_status") != "succeeded":
        st.session_state["heatmap_analysis_error"] = result.get("summary") or "AI 分析失敗；保留上次有效摘要。"
        return
    st.session_state.pop("heatmap_analysis_error", None)
    st.session_state["heatmap_ai_summary"] = result.get("summary") or ""
    st.session_state["heatmap_updates"] = list(result.get("updates") or [])
    st.session_state["suggested_events"] = [dict(e) for e in (result.get("events") or [])]
    st.session_state["heatmap_ai_meta"] = {
        "generated_at": result.get("generated_at"),
        "actor": result.get("actor"),
        "news_count": result.get("news_count", 0),
        "event_count": result.get("event_count", 0),
        "audit": list(result.get("audit") or []),
        "summary_id": result.get("summary_id"),
        "error": bool(result.get("error")),
        "analysis_status": result.get("analysis_status"), "sources": result.get("sources", []),
    }


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

_BADGE_STYLE = "font-size: 0.7rem; padding: 2px 6px; border-radius: 4px; display: inline-block; margin-bottom: 5px;"
_CARD_BADGES = {
    "view": ("#D1FAE5", "#065F46", "✅ 應變執行中"),
    "update": ("#FEF3C7", "#92400E", "⚠️ 數據異動(建議更新)"),
    "ready": ("#DBEAFE", "#1E40AF", "⚡ AI 建議啟動應變"),
    "news": ("#EDE9FE", "#5B21B6", "📰 已登錄情報"),
    "add": ("#F3F4F6", "#374151", "🔍 待評估"),
}


def _split_display_name(display_name):
    return split_location(display_name)


def _find_suggestion(display_name, suggested_events):
    c,r = split_location(display_name)
    return next((s for s in suggested_events or [] if s.get("impact_days") is not None and matches_location(c,r,s.get("region"),s.get("country"))), None)


def _exposure_line(exposure: dict) -> str:
    """曝險金額只在真的有未結採購單時才顯示金額；否則說清楚為什麼沒有數字。"""
    if exposure.get("open_po_count"):
        return (f"曝險金額: <b style='color:#111827;'>${exposure['open_po_amount']:,.0f}</b>"
                f"（{exposure['open_po_count']} 張未結採購單）")
    return (f"無未結採購單 ・ 供應商 <b style='color:#111827;'>{exposure.get('supplier_count', 0)}</b> 家"
            f"（正式 {exposure.get('official_supplier_count', 0)} 家）")


def _card_state(found_ev, match_suggest, news_events) -> str:
    if found_ev is not None:
        suggested_days = int(match_suggest.get("impact_days", 0)) if match_suggest else None
        actual_days = int(found_ev.get("impact_days") or 0)
        # 只有當 AI 建議的天數與現有計畫「不一致」時，才顯示「更新應變建議」
        return "update" if (suggested_days is not None and suggested_days != actual_days) else "view"
    if match_suggest:
        return "ready"
    if news_events:
        return "news"
    return "add"


def render_risk_shortcuts(key: str, heatmap_rows=None, *, actor: str):
    """區域風險快速分析小卡。"""
    from backend.supply_chain_risk import (
        add_risk_event,
        events_for_location,
        get_active_risk_events,
        get_region_exposure,
        is_news_event,
        update_risk_event,
    )

    if heatmap_rows is None:
        heatmap_rows = get_risk_heatmap_data()
    if not heatmap_rows:
        return
    # 如果有新情報登錄且尚未重新摘要，給予提示
    if st.session_state.get("heatmap_needs_refresh"):
        st.warning("⚠️ 偵測到新的風險登錄，請點擊上方「產生／更新即時風險摘要」以更新地圖與摘要。")

    high_risk_regions = [r for r in heatmap_rows if (r.get("risk_pct") or 0) > 20]
    # 依風險百分比由高至低排序
    high_risk_regions.sort(key=lambda x: x.get("risk_pct") or 0, reverse=True)
    if not high_risk_regions:
        return

    st.markdown("#### ⚡ 區域風險快速分析")
    st.caption("卡片依風險由高至低。已有情報的據點可一鍵建立應變計畫；已有計畫的據點可查看分析或套用 AI 更新。")

    # 事件只查一次，每張卡片再從記憶體篩選（原本每張卡各查一次資料庫）
    all_events = get_active_risk_events(limit=200)
    s_events = st.session_state.get("suggested_events", [])

    cols = st.columns(3)
    for i, reg in enumerate(high_risk_regions[:6]):  # 最多顯示 6 個
        with cols[i % 3]:
            color = "#EF4444" if reg["risk_pct"] > 60 else "#F59E0B"
            reg_display = reg.get("display_name") or ""
            region_key = reg.get("region_key") or reg_display
            ev_country, ev_region = _split_display_name(reg_display)

            exposure = get_region_exposure(region_key)
            matched = events_for_location(ev_country, ev_region, all_events)
            news_events = [ev for ev in matched if is_news_event(ev)]
            formal_events = [ev for ev in matched if not is_news_event(ev)]
            found_ev = formal_events[0] if formal_events else None   # 已依天數排序，取最嚴重者
            match_suggest = _find_suggestion(reg_display, s_events)
            if reg.get("estimated_delay") is not None and pd.notna(reg["estimated_delay"]):
                match_suggest = dict(country=ev_country, region=ev_region,
                    impact_days=int(reg["estimated_delay"]), event_type=(match_suggest or {}).get("event_type", "其他"),
                    description=reg.get("ai_summary") or "已保存的風險評估")
            btn_state = _card_state(found_ev, match_suggest, news_events)

            bg, fg, label = _CARD_BADGES[btn_state]
            if btn_state == "news":
                label = f"📰 已登錄 {len(news_events)} 則情報"
            status_badge = f'<div style="background: {bg}; color: {fg}; {_BADGE_STYLE}">{label}</div>'
            reason = reg.get("risk_reason") or ""

            st.markdown(f"""
            <div style="border: 1px solid #e0e0e0; border-radius: 10px; padding: 12px; margin-bottom: 10px; border-left: 5px solid {color};">
                {status_badge}
                <div style="font-size: 0.85rem; color: #666; font-weight: 500;">{reg_display}</div>
                <div style="font-size: 1.25rem; font-weight: bold; color: {color};">{reg['risk_pct']:.0f}% <span style="font-size: 0.8rem;">風險</span></div>
                <div style="font-size: 0.72rem; color: #9CA3AF; margin-top: 2px;">依據：{reason}</div>
                <div style="font-size: 0.75rem; color: #6B7280; margin-top: 5px;">{_exposure_line(exposure)}</div>
            </div>
            """, unsafe_allow_html=True)

            if btn_state == "view":
                if st.button("📊 查看分析", key=f"{key}_quick_anal_{region_key}_{i}", use_container_width=True, type="secondary"):
                    st.session_state["active_risk_event_id"] = found_ev["id"]
                    st.rerun()
            elif btn_state == "update":
                if st.button("🔄 更新應變建議", key=f"{key}_upd_{region_key}", use_container_width=True, type="primary"):
                    # 就地更新同一筆事件，不再靠「同區域覆寫」的副作用
                    update_risk_event(
                        found_ev["id"],
                        event_type=match_suggest.get("event_type", found_ev.get("event_type")),
                        impact_days=match_suggest["impact_days"],
                        description=f"【AI 建議更新】{match_suggest.get('description', '')}",
                        actor=actor,
                    )
                    st.toast(f"✅ 已將 {reg_display} 的數據更新", icon="🔄")
                    st.rerun()
            elif btn_state == "ready":
                if st.button("⚡ 啟動 AI 建議應變", key=f"{key}_heat_ana_ready_{i}_{reg_display}", use_container_width=True, type="primary"):
                    st.session_state["selected_region_for_response"] = reg_display
                    add_risk_event(
                        match_suggest.get("event_type", "其他"), ev_region, ev_country,
                        match_suggest["impact_days"],
                        f"AI 熱圖分析：{match_suggest.get('description', '')}",
                        actor=actor,
                    )
                    st.session_state["heatmap_needs_refresh"] = True
                    st.toast(f"📍 已啟動 {reg_display} 應變計畫", icon="🤖")
                    st.rerun()
            elif btn_state == "news":
                # 用最嚴重那則情報的類型／天數建立正式應變事件，不再硬塞「其他／7 天」
                top = news_events[0]
                top_days = int(top["impact_days"])
                top_type = top.get("event_type") or "其他"
                if st.button(f"🏗️ 建立應變計畫（{top_type}・{top_days} 天）", key=f"{key}_from_news_{i}_{region_key}", use_container_width=True, type="primary"):
                    st.session_state["selected_region_for_response"] = reg_display
                    add_risk_event(
                        top_type, ev_region, ev_country, top_days,
                        f"依 {len(news_events)} 則已登錄情報建立：{(top.get('description') or '')[:80]}",
                        actor=actor,
                    )
                    st.session_state["heatmap_needs_refresh"] = True
                    st.toast(f"📍 已依情報建立 {reg_display} 應變計畫", icon="📰")
                    st.rerun()
            else:
                confirmed_days = st.number_input("手動確認延遲天數", min_value=0, max_value=365, value=0, key=f"manual_days_{key}_{i}")
                if st.button("🏗️ 加入應變計畫", key=f"{key}_heat_ana_manual_{i}_{reg_display}", use_container_width=True):
                    st.session_state["selected_region_for_response"] = reg_display
                    add_risk_event(
                        "其他", ev_region, ev_country, confirmed_days, f"手動加入：偵測到 {reg_display} 高風險。", actor=actor
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
                exposure = get_region_exposure(selected_r.get("region_key") or selected_r.get("display_name"))
                st.markdown(f"<div style='padding-top:8px; color:#666; font-size:0.85rem;'>{_exposure_line(exposure)}</div>", unsafe_allow_html=True)
            with c3:
                confirmed_days = st.number_input("確認延遲天數", min_value=0, max_value=365, value=0, key=f"{key}_other_days")
                if st.button("🏗️ 加入應變計畫", key=f"{key}_other_reg_btn", use_container_width=True, type="secondary"):
                    sel_country, sel_region = _split_display_name(selected_r["display_name"])
                    match = _find_suggestion(selected_r["display_name"], s_events)
                    if match:
                        impact_days = match["impact_days"]
                        etype = match.get("event_type", "其他")
                        desc = f"AI 熱圖分析建議：{match.get('description', '建議登錄應變計畫')}"
                    else:
                        impact_days, etype, desc = confirmed_days, "其他", f"快速登錄：AI 偵測到 {selected_r['display_name']} 之 {selected_r['risk_pct']}% 地理風險。"
                    add_risk_event(etype, sel_region, sel_country, impact_days, desc, actor=actor)
                    st.session_state["heatmap_needs_refresh"] = True
                    if match:
                        st.toast(f"📍 已採用 AI 建議之 {impact_days} 天延遲 (類型: {etype})", icon="🤖")
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
    if st.session_state.get("heatmap_analysis_error"):
        st.error(st.session_state["heatmap_analysis_error"])

    heatmap_rows = get_risk_heatmap_data()

    # ── 即時風險熱圖 (Risk Heatmap) ─────────────────────────────────
    render_risk_heatmap(key="detail_heatmap", heatmap_rows=heatmap_rows)

    # AI 摘要（使用最近最新新聞）
    st.markdown("**AI 摘要**")
    news_items = []
    try:
        news_items = get_news_from_db(limit=10, order_by_latest=True, within_days=30, analyzed_only=True) or []
    except Exception:
        pass
    from datetime import datetime
    ref_date = datetime.now().strftime("%Y-%m-%d")

    # 頁面剛開或重新整理：session 沒有摘要就載入最近一次落地的結果（L2 換頁不會再遺失）
    if "heatmap_ai_summary" not in st.session_state and not st.session_state.get("heatmap_summary_dismissed"):
        latest = get_latest_ai_risk_summary()
        if latest:
            _store_summary_result(latest)

    col_ai_btn, col_reset = st.columns(2)
    with col_ai_btn:
        if st.button("🔄 產生／更新即時風險摘要", key="heatmap_ai_btn"):
            with st.spinner("AI 正在分析情報並偵測風險等級（思考型模型約需 1～2 分鐘）..."):
                result = analyze_heatmap_risk(news_items, reference_date=ref_date, actor=actor)
                _store_summary_result(result)
                st.session_state.pop("heatmap_summary_dismissed", None)
                if "heatmap_needs_refresh" in st.session_state:
                    del st.session_state["heatmap_needs_refresh"]
            st.rerun()
    with col_reset:
        if st.button("🔄 重置為初始熱圖", key="reset_heatmap_btn"):
            reset_risk_heatmap_to_initial(actor=actor)
            for key in ["heatmap_ai_summary", "heatmap_updates", "suggested_events", "heatmap_ai_meta"]:
                if key in st.session_state: del st.session_state[key]
            st.session_state["heatmap_summary_dismissed"] = True
            st.success("已重置為初始熱圖。")
            st.rerun()

    if "heatmap_ai_summary" in st.session_state:
        # issue #47 P1-1：摘要已由後端以結構化 JSON 產出（純敘事 markdown），
        # 原本剝離 UPDATE:/EVENT: 技術指令行的 regex 邏輯不再需要。
        meta = st.session_state.get("heatmap_ai_meta") or {}
        with st.container(border=True):
            st.markdown("### 🤖 AI 供應鏈與地理風險深度分析")
            if meta.get("generated_at"):
                st.caption(
                    f"產生時間 {meta['generated_at']} ・ 依據 {meta.get('news_count', 0)} 則新聞、"
                    f"{meta.get('event_count', 0)} 筆已登錄事件"
                    + (f" ・ 由 {meta['actor']} 產生" if meta.get("actor") else "")
                )
            st.markdown(st.session_state["heatmap_ai_summary"])
            if meta.get("sources"):
                with st.expander("分析依據與來源"):
                    st.json(meta["sources"])
            audit = meta.get("audit") or []
            if audit:
                with st.expander(f"🔎 證據檢核：{len(audit)} 項 AI 建議被略過或調整", expanded=False):
                    st.caption("AI 提到但新聞與已登錄事件裡都沒有的地區會被略過；延遲天數或事件類型超出證據範圍的會被調整。")
                    for item in audit:
                        st.markdown(f"- {item.get('kind')}「**{item.get('name')}**」{item.get('action')}：{item.get('reason')}")

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

