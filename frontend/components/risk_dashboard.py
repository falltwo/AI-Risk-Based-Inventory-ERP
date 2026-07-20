import streamlit as st
import re
import pandas as pd
from backend.access_control import ERP_POLICY_WRITE, has_capability
from backend.supply_chain_news import get_news_from_db, refresh_news_for_countries
from backend.supply_chain_risk import (
    translate_to_chinese_traditional,
    infer_affected_region_from_news,
    get_suppliers_for_map,
    get_recent_events_for_delay,
    get_risk_events_list,
    add_risk_event,
    delete_risk_event,
    get_historical_event_precedents,
    get_affected_suppliers_by_event,
    get_affected_sales_orders_by_event,
    get_stockout_alerts_for_event,
    increase_safety_stock_for_event,
    update_reorder_point,
    restore_all_rop_to_baseline,
    get_event_risk_scores,
    get_region_risk_scores,
    get_risk_heatmap_data,
)


def can_write_erp_policy(actor: str) -> bool:
    """Resolve ERP policy write visibility from the live principal."""
    return has_capability(actor, ERP_POLICY_WRITE)


def _auto_refresh_heatmap_ai(api_key, gemini_model):
    from backend.supply_chain_risk import get_heatmap_ai_summary
    from datetime import datetime
    import streamlit as st
    news_list = get_news_from_db(limit=10, order_by_latest=True, within_days=30)
    news_context = ""
    if news_list:
        news_context = "\n".join([
            (n.get("title") or "") + " " + (n.get("summary") or "")[:200]
            for n in news_list
        ])
    ref_date = datetime.now().strftime("%Y-%m-%d")
    s, u, evs = get_heatmap_ai_summary(api_key, news_context, reference_date=ref_date, model=gemini_model)
    st.session_state["heatmap_ai_summary"] = s
    st.session_state["heatmap_updates"] = u
    st.session_state["suggested_events"] = evs
    if "heatmap_needs_refresh" in st.session_state:
        del st.session_state["heatmap_needs_refresh"]

def render_intelligence_gathering(
    api_key: str = "",
    gnews_api_key: str = "",
    gemini_model: str = "gemini-2.5-flash",
    *,
    actor: str,
):
    """
    第一階段：🔍 即時全球情報 (Intelligence)
    職責：抓取全球新聞、AI 自動歸類與風險等級評估、登錄為正式風險事件。
    """
    st.subheader("🔍 即時全球情報與事件登錄")
    st.caption("透過 GNews/RSS 抓取全球供應鏈相關新聞，並利用 AI 自動偵測受影響國家、地區與事件類型（戰爭、氣候、罷工等）。")

    # 更新即時新聞：依供應商國家從 GNews/RSS 抓取並寫入 DB
    _suppliers = get_suppliers_for_map()
    _countries = []
    if _suppliers is not None and not _suppliers.empty and "country" in _suppliers.columns:
        _countries = _suppliers["country"].dropna().unique().tolist()
        _countries = [str(c).strip() for c in _countries if str(c).strip()]
    if not _countries:
        _countries = ["台灣", "日本", "美國", "南韓", "中國", "越南", "墨西哥"]
    
    col_time, col_cate, col_btn, col_help = st.columns([1, 1, 1, 2])
    with col_time:
        time_options = {"7 天": 7, "30 天": 30, "90 天": 90}
        selected_time = st.selectbox("📅 抓取多久內的新聞", list(time_options.keys()), index=0, key="intel_time_sel")
        within_days = time_options[selected_time]
    with col_cate:
        cate_opts = ["全部", "戰爭", "氣候", "罷工", "政策", "交通", "其他"]
        selected_cates = st.multiselect("🏷️ 顯示類別", cate_opts, default=["全部"], key="intel_cate_sel")
    with col_help:
        precedents = get_historical_event_precedents()
        prec_lines = ""
        for etype, avg, cnt in precedents[:3]:
            prec_lines += f"<div style='margin-bottom:4px;'>• <b>{etype}</b>: 歷史平均 {avg:.1f} 天 <span style='font-size:0.8rem; color:#666;'>({cnt} 筆)</span></div>"
        
        if not prec_lines:
            prec_lines = "<div>目前尚無歷史數據</div>"
        
        st.markdown(f"""
        <div style="background-color: #f0f7ff; padding: 12px; border-radius: 8px; border-left: 5px solid #2196f3; font-size: 0.9rem; line-height: 1.4;">
            <div style="font-weight: bold; margin-bottom: 8px;">⏳ 延遲判斷基準 (AI 已學習過往慣例)</div>
            {prec_lines}
            <hr style="margin: 8px 0; border: 0; border-top: 1px solid #d1e3f3;">
            <div style="font-size: 0.8rem; color: #555;">
                <b>預設參考範圍 (若無紀錄)：</b><br>
                戰爭: 30-90天 | 罷工: 7-21天 | 氣候: 3-14天<br>
                政策: 7-30天 | 交通: 1-7天 | 不相關: 0天
            </div>
        </div>
        """, unsafe_allow_html=True)
        st.caption("系統會過濾不相關新聞，並參考過往紀錄推估延遲。")
    with col_btn:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("📡 更新即時新聞", key="refresh_news_btn", help="依各供應商國家抓取最近新聞，並由 AI 自動分析類別與延遲天數。"):
            with st.status("正在獲獲取供應鏈情報並由 AI 進行分析評分...") as status:
                status.write("📡 正在平行抓取各國原始新聞與預過濾...")
                status.write("🧠 正在啟動 Gemini 進行深度風險評估 (約 30-40 秒)...")
                res = refresh_news_for_countries(
                    _countries, 
                    gemini_api_key=api_key or None,
                    gnews_api_key=gnews_api_key or None, 
                    max_per_country=8, 
                    within_days=within_days,
                    gemini_model=gemini_model,
                    actor=actor,
                )
                fetched = res.get("fetched_count", 0)
                filtered = res.get("filtered_count", 0)
                saved = res.get("saved_count", 0)
                
                status.update(label=f"✅ 全球情報更新完成！(已掃描 {fetched} 則，AI 過濾掉 {filtered} 則無關情報)", state="complete")
            st.toast(f"📍 AI 已自動過濾 {filtered} 則不相關新聞，保留 {saved} 則關鍵情報。", icon="🤖")
            st.rerun()

    # 讀取現有新聞
    news_list_raw = get_news_from_db(limit=60, order_by_latest=True, within_days=within_days)
    
    # 執行類別過濾與去重
    filtered_news = []
    seen = set()
    ai_filtered_count = 0
    for n in news_list_raw:
        cat = n.get("category") or "其他"
        if "全部" not in selected_cates and selected_cates and cat not in selected_cates:
            continue
        # 2. 去重 (標題與 URL)
        key = ((n.get("title") or "").strip()[:200], (n.get("url") or "").strip())
        if key in seen or (not key[0] and not key[1]):
            continue
        
        # 3. 延遲過濾 (只抓有實質影響的新聞，排除預估 0 天者)
        if (n.get("estimated_delay") or 0) <= 0:
            ai_filtered_count += 1
            seen.add(key)
            continue
            
        seen.add(key)
        filtered_news.append(n)

    if not filtered_news:
        st.info("✅ 目前尚無具有「實質延遲風險 (大於 0 天)」的情報。")
        if ai_filtered_count > 0:
            st.caption(f"🤖 AI 在背景已為您處理並過濾了 **{ai_filtered_count}** 筆無顯著影響（預估 0 天延遲）的一般新聞或重複新聞。")
        else:
            st.caption("請點擊上方按鈕更新或調整時間/類別篩選條件。")
        return

    st.markdown("---")
    
    st.markdown("---")
    
    # --- 排除已登錄項目 ---
    existing_events = get_risk_events_list(30)
    registered_ids = set()
    if existing_events is not None and not existing_events.empty and 'news_id' in existing_events.columns:
        registered_ids = set(existing_events['news_id'].dropna().unique())
    unregistered_news = [n for n in filtered_news if n.get('id') not in registered_ids]

    # 2. 顯示原始情報清單 (直接顯示)
    st.markdown("#### 📊 當前全球情報分析 (Latest Intelligence)")
    container = st.container(border=True)
    with container:

        news_options = []
        for n in unregistered_news:
            cat = n.get('category') or '其他'
            delay = n.get('estimated_delay') or 0
            title = n.get('title') or '（無標題）'
            news_options.append(f"【{cat} | 預估 {delay}天】{title}")

        if not unregistered_news:
            st.info("目前無待處理的新聞情報。")
        else:
            sel_idx = st.selectbox("選擇一則新聞進行詳細閱覽：", range(len(unregistered_news)), format_func=lambda i: (news_options[i][:110] + "…" if len(news_options[i]) > 110 else news_options[i]))
            chosen = unregistered_news[sel_idx]
            
            raw_intro = "\n\n".join(p for p in [(chosen.get("title") or "").strip(), (chosen.get("summary") or "").strip()] if p).strip() or "（無簡介）"
            
            # --- 🚀 一鍵批量登錄功能 ---
            col_bulk, _ = st.columns([1, 2])
            with col_bulk:
                if st.button("🚀 一鍵登錄全部情報", use_container_width=True, type="primary"):
                    with st.status("正在登錄情報...") as status:
                        bulk_count = 0
                        for n in unregistered_news:
                            add_risk_event(
                                event_type=n.get("category") or "其他",
                                region=n.get("region") or "",
                                country=n.get("country") or "",
                                impact_days=n.get("estimated_delay") or 7,
                                description=f"【一鍵批量登錄】{n.get('title')}",
                                news_id=n.get('id'),
                                actor=actor,
                            )
                            bulk_count += 1
                        st.session_state["heatmap_needs_refresh"] = True
                        status.update(label=f"✅ 已成功登錄 {bulk_count} 則風險事件！", state="complete")
                    st.rerun()

            # 不再切分兩欄，直接全寬顯示簡介與單筆一鍵登錄按鈕
            st.markdown("**📝 簡介分析**")
            intro_text = chosen.get("summary") or "（無簡介）"
            st.info(intro_text)
            
            # 選配：點擊後才進行深度翻譯
            cache_key = f"news_cn_{chosen.get('id')}"
            if cache_key in st.session_state:
                st.success("🤖 AI 深度解析：")
                st.write(st.session_state[cache_key])
            else:
                if st.button("🔍 取得 AI 深度翻譯與建議", key=f"trans_btn_{chosen.get('id')}"):
                    with st.spinner("正在聯絡 AI 進行詳細解析..."):
                        st.session_state[cache_key] = translate_to_chinese_traditional(api_key, raw_intro, gemini_model)
                    st.rerun()

            col_link, col_reg = st.columns([1, 1])
            with col_link:
                if chosen.get("url"): st.link_button("🔗 查看原文", chosen.get("url"), use_container_width=True)
            with col_reg:
                def_country = chosen.get("country") or ""
                def_region = chosen.get("region") or ""
                def_etype = chosen.get("category") or "其他"
                def_delay = chosen.get("estimated_delay") or 0
                
                if st.button(f"🚀 一鍵登錄：{def_etype}風險 (預估延遲 {def_delay} 天)", type="primary", use_container_width=True):
                    add_risk_event(
                        def_etype,
                        def_region,
                        def_country,
                        def_delay,
                        f"【自動登錄】{chosen.get('title')}",
                        news_id=chosen.get('id'),
                        actor=actor,
                    )
                    st.session_state["heatmap_needs_refresh"] = True
                    st.success("事件已登錄！記得至地圖區更新 AI 摘要。")
                    st.rerun()


    st.markdown("---")
    with st.expander("➕ 手動新增風險事件 (非新聞來源)"):
        with st.form("manual_event_form"):
            col1, col2 = st.columns(2)
            with col1:
                m_etype = st.selectbox("事件類型", ["戰爭", "氣候", "罷工", "政策", "交通", "其他"])
                m_country = st.text_input("受影響國家")
            with col2:
                m_region = st.text_input("受影響地區")
                m_impact = st.number_input("預估延遲天數", min_value=0, value=0)
            m_desc = st.text_area("事件說明")
            if st.form_submit_button("新增事件"):
                add_risk_event(
                    m_etype, m_region, m_country, m_impact, m_desc, actor=actor
                )
                st.session_state["heatmap_needs_refresh"] = True
                st.success("手動事件已登錄！記得至地圖區更新 AI 摘要。")
                st.rerun()

def render_response_execution(
    api_key: str = "",
    gnews_api_key: str = "",
    gemini_model: str = "gemini-2.5-flash",
    *,
    actor: str,
):
    """
    第二階段：🚨 執行應變與衝擊分析 (Action)
    職責：針對已登錄的風險事件，快速分析其對供應商、庫存、銷售訂單的實際衝擊。
    """
    st.subheader("🚨 應變執行與衝擊分析")
    st.caption("針對情報區塊已登錄的風險事件進行深度比對，評估對您供應鏈的真實影響並採取應變行動。")

    events_raw = get_risk_events_list(30)
    if events_raw is None or events_raw.empty:
        st.info("目前尚無活躍的風險事件。請先於上方「情報獲取」登錄事件。")
        return

    # 【核心邏輯解耦】第三步驟只顯示「正式應變事件」（即：非直接從新聞初篩登錄的事件）
    # 從新聞一鍵選入的事件會帶有 news_id，在此排除，僅保留地圖建議或手動登錄的純事件
    events = events_raw[events_raw['news_id'].isna()]
    
    if events.empty:
        st.info("目前尚無正式應變事件。請至「步驟 2: 全域風險監控」點擊地圖區域之「加入應變計畫」以啟動分析。")
        return

    # 【核心優化】過濾選單，僅顯示熱圖中具備中高風險 (>20%) 或 AI 有積極建議的地區
    heatmap_rows = get_risk_heatmap_data()
    high_risk_names = [hr['display_name'] for hr in (heatmap_rows or []) if (hr.get('risk_pct') or 0) > 20]
    
    # 建立 country -> display_name 的查詢字典
    country_to_display = {}
    for hr in (heatmap_rows or []):
        c = (hr.get('display_name') or '').split(' ')[0]
        if c and c not in country_to_display:
            country_to_display[c] = hr['display_name']
    
    event_options = ["--- 請選擇要分析的事件 ---"]
    event_ids = [None]
    
    seen_display = set()
    for _, row in events.iterrows():
        country = (row.get('country') or '').strip()
        display = country_to_display.get(country) or country or '未知'
        
        # 僅顯示高風險區域，或若該區域已經有進入應變狀態，則保留顯示
        if display in high_risk_names or display in seen_display:
            if display not in seen_display:
                event_options.append(f"【{row['event_type']}】{display}")
                event_ids.append(row['id'])
                seen_display.add(display)
    
    # ── 聯動邏輯：檢查是否有外部 (如地圖/情報) 指令要選中特定事件 ──
    if "resp_active_event_sel" not in st.session_state:
        st.session_state["resp_active_event_sel"] = 0

    if "active_risk_event_id" in st.session_state:
        target_id = st.session_state["active_risk_event_id"]
        if target_id in event_ids:
            new_idx = event_ids.index(target_id)
            # 🧪 關鍵修正：若有外部跳轉指令，手動強制覆寫 selectbox 的內部 state
            st.session_state["resp_active_event_sel"] = new_idx
    
    selected_idx = st.selectbox(
        "選擇要分析與執行的風險事件", 
        range(len(event_options)), 
        format_func=lambda i: event_options[i], 
        key="resp_active_event_sel"
    )
    
    if selected_idx == 0:
        st.info("請從上方下拉選單選擇一個事件，以展開詳細衝擊分析與應變建議。")
        # 清除 state 以免干擾其他組件
        if "active_risk_event_id" in st.session_state:
            del st.session_state["active_risk_event_id"]
        return

    # 同步更新 session_state
    active_ev_id = event_ids[selected_idx]
    active_ev = events[events['id'] == active_ev_id].iloc[0]
    st.session_state["active_risk_event_id"] = active_ev_id
    
    region = active_ev.get("region") or ""
    country = active_ev.get("country") or ""
    impact_days = int(active_ev.get("impact_days") or 0)
    
    # 快速計算影響規模
    affected_sup = get_affected_suppliers_by_event(region, country) or []
    affected_ord = get_affected_sales_orders_by_event(region, country, impact_days) or []
    stock_alerts = get_stockout_alerts_for_event(region, country, impact_days) or []

    # 衝擊概覽 (Small Header)
    st.markdown(f"**事件詳情：** `{active_ev.get('event_type')}` | **區域：** `{region or country}` | **預計延遲：** `+{impact_days} 天`")
    
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"📊 **衝擊規模：** `受波及供應商 {len(affected_sup)}` | `受波及銷售單 {len(affected_ord)}` | `斷鏈風險物料 {len(stock_alerts)}`")
    with col2:
        with st.popover("🗑️ 刪除此事件", use_container_width=True):
            st.warning("確認移除此事件？此操作無法復原。")
            ev_id = int(active_ev["id"])
            if st.button("🔴 確認點擊刪除", key=f"del_btn_{ev_id}", type="primary", use_container_width=True):
                delete_risk_event(ev_id, actor=actor)
                st.success("事件已從清單中移除。")
                st.rerun()

    def get_ai_safety_multiplier(etype):
        # AI 根據事件嚴重性建議額外的安全係數
        mapping = {"戰爭": 1.5, "罷工": 1.3, "氣候": 1.3, "政策": 1.2, "交通": 1.1}
        return mapping.get(etype, 1.0)

    # 執行與分析細節 (用摺疊式選單以省空間)
    with st.expander("🚚 1. 斷鏈庫存預警與應變 (Increase Safety Stock)", expanded=True):
        if stock_alerts:
            etype = active_ev.get('event_type', '其他')
            st.caption(f"針對此 **{etype}** 事件造成的預計 **{impact_days} 天** 延期，系統建議動態調整受影響物料的安全水位。")

            can_write_policy = can_write_erp_policy(actor)
            if can_write_policy:
                ai_mult = get_ai_safety_multiplier(etype)
                btn_label = f"🤖 AI 建議：一鍵動態調高受影響物料安全水位 (+{impact_days}天需求 ⚡)"

                if st.button(btn_label, key="adj_stock_btn_dynamic", type="primary"):
                    from backend.supply_chain_risk import increase_safety_stock_for_event
                    cnt = increase_safety_stock_for_event(
                        region,
                        country,
                        impact_days=impact_days,
                        multiplier=ai_mult,
                        actor=actor,
                    )
                    st.success(f"✅ 已依據預期延遲與日銷量，完成 {cnt} 項物料的安全水位動態調整！")
                    st.rerun()

                with st.popover("🔄 重設風險緩衝 (Restore Baseline)", use_container_width=True):
                    st.warning("這將把所有物料的安全水位恢復至原始基準值 (Baseline)。")
                    if st.button("🔴 確認還原所有基準水位", key="restore_baseline_btn"):
                        restore_all_rop_to_baseline(actor=actor)
                        st.success("已還原所有物料至基準水位。")
                        st.rerun()
            else:
                st.info(
                    "目前帳號可分析風險，但不能直接修改 ERP 安全庫存政策；"
                    "請透過受治理提案送交具權限人員審核。"
                )
            
            df_stk = pd.DataFrame(stock_alerts).rename(columns={
                "product_name": "物料名稱", "stock": "現有庫存", "projected_stock": "延期後剩餘", "reorder_point": "原安全水位", "suggestion": "建議"
            })
            st.dataframe(df_stk[["物料名稱", "現有庫存", "延期後剩餘", "原安全水位", "建議"]], use_container_width=True, hide_index=True)
        else:
            st.success("目前庫存足以應對此事件，暫無斷鏈風險。")

    with st.expander("👤 2. 受波及客戶連結 (Contact Customers)", expanded=False):
        if affected_ord:
            st.caption("以下銷售單（Sales Orders）可能因原材料短缺面臨延誤，請與客戶溝通。")
            df_ord = pd.DataFrame(affected_ord).rename(columns={
                "order_id": "單號", "customer_name": "客戶", "product_name": "產品", "original_delivery": "原交期", "new_delivery": "預計交期"
            })
            st.dataframe(df_ord, use_container_width=True, hide_index=True)
        else:
            st.info("尚無受影響的客戶銷售單。")

    with st.expander("🏭 3. 受波及供應商清單 (Affected Suppliers)", expanded=False):
        if affected_sup:
            st.caption("以下位於受災區域內的供應商據點可能面臨交期延遲風險。")
            df_sup = pd.DataFrame(affected_sup).rename(columns={
                "name": "供應商名稱", "country": "國家", "region": "地區", "risk_level": "原始風險等級"
            })
            st.dataframe(df_sup[["供應商名稱", "國家", "地區", "原始風險等級"]], use_container_width=True, hide_index=True)
        else:
            st.info("尚無直接受影響的供應商。")

    with st.expander("✉️ 4. 閉環行動：生成應變信件草稿 (Generate Action Mail)", expanded=False):
        if affected_sup or affected_ord:
            st.caption("基於此風險事件的衝擊分析，AI 可以為您擬定發送給供應商或客戶的溝通草稿。")
            target_type = st.radio("選擇目標對象：", ["供應商 (詢問交期與催貨)", "客戶 (延遲通知)"], horizontal=True)
            
            if st.button("🤖 生成 AI 溝通草稿", key="gen_mail_btn", type="primary"):
                context = f"事件:{active_ev.get('event_type')}, 地區:{region or country}, 預計延遲:{impact_days}天\n"
                if target_type == "供應商 (詢問交期與催貨)" and affected_sup:
                    context += f"對象供應商:{affected_sup[0].get('name')}\n"
                elif affected_ord:
                    context += f"對象客戶:{affected_ord[0].get('customer_name')}\n"
                
                with st.spinner("正在擬定專業溝通草稿..."):
                    from backend.supply_chain_risk import generate_communication_draft
                    draft = generate_communication_draft(api_key, context, target_type, gemini_model)
                    st.text_area("生成的草稿內容 (中英雙語)：", value=draft, height=450)
                    st.info("💡 您可以複製內容至郵件軟體發送。未來版本將支援「一鍵發送」。")
        else:
            st.info("尚無受影響對象，無須發送通知。")

    st.markdown("<br>", unsafe_allow_html=True)
