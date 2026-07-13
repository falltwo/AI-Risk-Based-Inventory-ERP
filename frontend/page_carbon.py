"""
frontend/page_carbon.py
🌿 碳排放管理（碳排放總覽、碳足跡追蹤、減量目標、年度碳目標分析、ESG 報告）
"""

import sqlite3
import streamlit as st
from frontend.ui_utils import show_error
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
from backend import DB_FILE, run_query


def render(sub_menu: str, api_key: str):
    st.markdown("<div class='premium-title'>🌿 碳排放管理</div>", unsafe_allow_html=True)
    menus = ['碳排放總覽', '碳足跡追蹤', '減量目標', '年度碳目標分析', 'ESG 報告', '供應商風險與碳排']
    styled_menus = [f"🌟 **{m}**" if m == sub_menu else f"{m}" for m in menus]
    st.markdown(" ｜ ".join(styled_menus))

    if sub_menu == "碳排放總覽":
        st.subheader("碳排放總覽", divider="blue")
        st.caption("依 Scope 1/2/3 彙總銷售與採購所產生之碳排放，支援當月與當年累計。")
        conn = sqlite3.connect(DB_FILE)
        period_month = st.text_input("查詢月份（年-月）", value=datetime.now().strftime("%Y-%m"), key="em_month")
        period_year = period_month[:4] if period_month else datetime.now().strftime("%Y")
        q_month = """
        SELECT cf.scope, SUM(o.quantity * cf.kg_co2_per_unit) as kg_co2
        FROM orders o JOIN carbon_factors cf ON cf.product_id = o.product_id
        WHERE strftime('%Y-%m', o.order_date)=? AND o.status != '已取消' AND cf.scope IN (1, 2)
        GROUP BY cf.scope
        UNION ALL
        SELECT 3 as scope, SUM(poi.qty * sp.carbon_factor) as kg_co2
        FROM purchase_orders po
        JOIN purchase_order_items poi ON po.po_id = poi.po_id
        JOIN supplier_products sp ON po.supplier_id = sp.supplier_id AND poi.product_id = sp.product_id
        WHERE strftime('%Y-%m', po.order_date)=? AND (po.status != '已取消' OR po.status IS NULL)
        """
        q_year = """
        SELECT cf.scope, SUM(o.quantity * cf.kg_co2_per_unit) as kg_co2
        FROM orders o JOIN carbon_factors cf ON cf.product_id = o.product_id
        WHERE strftime('%Y', o.order_date)=? AND o.status != '已取消' AND cf.scope IN (1, 2)
        GROUP BY cf.scope
        UNION ALL
        SELECT 3 as scope, SUM(poi.qty * sp.carbon_factor) as kg_co2
        FROM purchase_orders po
        JOIN purchase_order_items poi ON po.po_id = poi.po_id
        JOIN supplier_products sp ON po.supplier_id = sp.supplier_id AND poi.product_id = sp.product_id
        WHERE strftime('%Y', po.order_date)=? AND (po.status != '已取消' OR po.status IS NULL)
        """
        try:
            df_m = pd.read_sql_query(q_month, conn, params=(period_month, period_month))
            df_y = pd.read_sql_query(q_year, conn, params=(period_year, period_year))
            scope_name = {1: "Scope 1 直接", 2: "Scope 2 能源", 3: "Scope 3 供應鏈"}

            # ── KPI 計算 ──────────────────────────────────────────────
            total_y = df_y['kg_co2'].sum() if not df_y.empty else 0

            # 年增率：去年同期總排放
            prev_year = str(int(period_year) - 1)
            df_prev = pd.read_sql_query(
                """SELECT SUM(kg_co2) as kg_co2 FROM (
                    SELECT SUM(o.quantity * cf.kg_co2_per_unit) as kg_co2
                    FROM orders o JOIN carbon_factors cf ON cf.product_id = o.product_id
                    WHERE strftime('%Y', o.order_date)=? AND o.status != '已取消' AND cf.scope IN (1, 2)
                    UNION ALL
                    SELECT SUM(poi.qty * sp.carbon_factor) as kg_co2
                    FROM purchase_orders po
                    JOIN purchase_order_items poi ON po.po_id = poi.po_id
                    JOIN supplier_products sp ON po.supplier_id = sp.supplier_id AND poi.product_id = sp.product_id
                    WHERE strftime('%Y', po.order_date)=? AND (po.status != '已取消' OR po.status IS NULL)
                )""",
                conn, params=(prev_year, prev_year)
            )
            total_prev = float(df_prev['kg_co2'].iloc[0] or 0) if not df_prev.empty else 0
            if total_prev > 0:
                yoy_pct = (total_y - total_prev) / total_prev * 100
                yoy_str = f"{yoy_pct:+.1f}%"
                yoy_delta = f"vs {prev_year} 年 {total_prev:,.0f} kg"
                yoy_color = "inverse" if yoy_pct <= 0 else "normal"
            else:
                yoy_str = "—"
                yoy_delta = f"去年（{prev_year}）無資料"
                yoy_color = "off"

            # 平均單位碳排：總碳排 ÷ 本年銷售總量
            df_qty = pd.read_sql_query(
                """SELECT SUM(o.quantity) as total_qty
                   FROM orders o JOIN carbon_factors cf ON cf.product_id = o.product_id
                   WHERE strftime('%Y', o.order_date)=? AND o.status != '已取消'""",
                conn, params=(period_year,)
            )
            total_qty = float(df_qty['total_qty'].iloc[0] or 0) if not df_qty.empty else 0
            avg_unit = total_y / total_qty if total_qty > 0 else 0

            # ── KPI 卡片 ──────────────────────────────────────────────
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric(
                    label=f"🌍 {period_year} 年總碳排",
                    value=f"{total_y:,.1f} kg CO₂e",
                    delta=f"當月 {df_m['kg_co2'].sum() if not df_m.empty else 0:,.1f} kg",
                    delta_color="off",
                )
            with col2:
                st.metric(
                    label="📈 年增率（vs 去年）",
                    value=yoy_str,
                    delta=yoy_delta,
                    delta_color=yoy_color,
                )
            with col3:
                st.metric(
                    label="⚡ 平均單位碳排",
                    value=f"{avg_unit:.2f} kg CO₂e／件",
                    delta=f"總銷售：{total_qty:,.0f} 件",
                    delta_color="off",
                )

            # ── AI 碳排快析 (Overview Insight) ──────────────────────────────────
            with st.container(border=True):
                col_ai1, col_ai2 = st.columns([1, 4])
                with col_ai1:
                    st.markdown("🤖 **AI 碳排快析**")
                    do_ai_quick = st.button("產出即時洞察", key="carbon_quick_ai")
                with col_ai2:
                    if do_ai_quick:
                        if True:
                            try:
                                # issue #27：統一 LLM 入口（.env 驅動，不再需要側邊欄 key）
                                from backend.llm_client import complete_text

                                prompt_data = f"""
                                請根據以下碳排指標進行簡短的「3點式核心判讀」：
                                - {period_year} 年總排放：{total_y:,.1f} kg CO₂e
                                - 年增率：{yoy_str} ({yoy_delta})
                                - 平均單位碳排：{avg_unit:.2f} kg CO₂e／件
                                
                                請直接輸出 3 個精簡的點位（繁體中文），語氣專業且具建設性。
                                """
                                with st.spinner("AI 分析中..."):
                                    _text = complete_text(prompt_data, temperature=0.2,
                                                          tag="analysis:carbon_quick")
                                if _text:
                                    st.markdown(_text)
                                else:
                                    st.info("AI 本次分析未回傳內容，請再試一次。")
                            except Exception as e:
                                show_error("AI 快析失敗，請檢查 .env 模型設定", e)
                    else:
                        st.caption("點擊按鈕獲取本年度排放趨勢的 AI 核心判讀。")
            col_left, col_right = st.columns([1, 1.3])
            with col_left:
                st.markdown(f"#### 當月依範疇（{period_month}）")
                if not df_m.empty:
                    df_m['範疇'] = df_m['scope'].map(scope_name)
                    st.dataframe(df_m[['範疇', 'kg_co2']].rename(columns={'kg_co2': 'kg CO₂e'}), use_container_width=True, hide_index=True)
                else:
                    st.info("當月無資料，請先於「碳足跡追蹤」設定產品碳係數並有銷售訂單。")
            with col_right:
                if not df_m.empty:
                    df_m_plot = df_m.copy()
                    df_m_plot['月份'] = period_month
                    if '範疇' not in df_m_plot.columns:
                        df_m_plot['範疇'] = df_m_plot['scope'].map(scope_name)
                    color_map = {
                        "Scope 1 直接": "#1b5e20",
                        "Scope 2 能源": "#43a047",
                        "Scope 3 供應鏈": "#a5d6a7",
                    }
                    fig_stack = px.bar(
                        df_m_plot,
                        x='月份', y='kg_co2', color='範疇',
                        barmode='group',
                        title=f'{period_month} Scope 1 / 2 / 3 碳排放 (kg CO₂e)',
                        labels={'kg_co2': 'kg CO₂e', '月份': '月份', '範疇': 'Scope'},
                        color_discrete_map=color_map,
                    )
                    fig_stack.update_layout(
                        legend=dict(orientation="h", y=-0.3),
                        margin=dict(t=50, b=80),
                        xaxis=dict(type='category'),
                        height=350,
                    )
                    st.plotly_chart(fig_stack, use_container_width=True)

            # ── 當年依範疇 ───────────────────────────────────────────
            st.markdown("---")
            st.markdown(f"#### 當年依範疇（{period_year}）")
            if not df_y.empty:
                df_y['範疇'] = df_y['scope'].map(scope_name)
                st.dataframe(df_y[['範疇', 'kg_co2']].rename(columns={'kg_co2': 'kg CO₂e'}), use_container_width=True, hide_index=True)
            else:
                st.info("當年尚無碳排放資料。")

            # ── 當年碳排放趨勢與範疇分析 ──────────────────────────────
            st.markdown("---")
            st.markdown("#### 📈 當年碳排放趨勢與範疇分析")
            try:
                q_monthly = """
                SELECT strftime('%Y-%m', o.order_date) as 月份, cf.scope, SUM(o.quantity * cf.kg_co2_per_unit) as kg_co2
                FROM orders o JOIN carbon_factors cf ON cf.product_id = o.product_id
                WHERE strftime('%Y', o.order_date)=? AND o.status != '已取消' AND cf.scope IN (1, 2)
                GROUP BY strftime('%Y-%m', o.order_date), cf.scope
                UNION ALL
                SELECT strftime('%Y-%m', po.order_date) as 月份, 3 as scope, SUM(poi.qty * sp.carbon_factor) as kg_co2
                FROM purchase_orders po
                JOIN purchase_order_items poi ON po.po_id = poi.po_id
                JOIN supplier_products sp ON po.supplier_id = sp.supplier_id AND poi.product_id = sp.product_id
                WHERE strftime('%Y', po.order_date)=? AND (po.status != '已取消' OR po.status IS NULL)
                GROUP BY strftime('%Y-%m', po.order_date)
                """
                df_monthly = pd.read_sql_query(q_monthly, conn, params=(period_year, period_year))
                if not df_monthly.empty:
                    df_monthly['範疇'] = df_monthly['scope'].map(scope_name)
                    col_c1, col_c2 = st.columns(2)
                    with col_c1:
                        monthly_tot = df_monthly.groupby('月份')['kg_co2'].sum().reset_index()
                        fig_trend = px.line(monthly_tot, x='月份', y='kg_co2', title=f'{period_year} 年各月碳排放趨勢 (kg CO₂e)', markers=True)
                        fig_trend.update_layout(xaxis_tickangle=-45, margin=dict(t=40, b=60))
                        st.plotly_chart(fig_trend, use_container_width=True)
                    with col_c2:
                        scope_tot = df_monthly.groupby('範疇')['kg_co2'].sum().reset_index()
                        fig_pie = px.pie(scope_tot, values='kg_co2', names='範疇', title=f'{period_year} 年依範疇占比', color_discrete_sequence=px.colors.sequential.Greens_r)
                        st.plotly_chart(fig_pie, use_container_width=True)
                else:
                    st.caption("當年尚無分月碳排放資料，無法繪製趨勢圖。")
            except Exception as ex:
                st.caption(f"圖表載入略過：{ex}")

        except Exception as e:
            st.error(str(e))
        finally:
            conn.close()


    elif sub_menu == "碳足跡追蹤":
        st.subheader("碳足跡追蹤與 ESG 報告", divider="blue")
        st.caption("結合採購與銷售數據，依產品碳係數計算碳排放量，協助生成 ESG 報告。")
        # 自動整理：刪除既有碳係數並為每個產品補上預設值（只會做一次，避免每次刷新都重複刪寫）
        try:
            df_p_all = pd.read_sql_query("SELECT product_id FROM inventory", sqlite3.connect(DB_FILE))
            product_ids = df_p_all["product_id"].tolist() if df_p_all is not None and not df_p_all.empty else []
            expected = len(product_ids) * 3
            marker = "AUTO_PRESET_20260313"
            row = run_query("SELECT COUNT(*) FROM carbon_factors WHERE note = ?", (marker,))
            already = int(row[0][0] or 0) if row else 0

            if expected > 0 and already != expected:
                run_query("DELETE FROM carbon_factors", fetch=False)
                presets = [
                    (1, 1.20, marker),
                    (2, 0.80, marker),
                    (3, 2.50, marker),
                ]
                for pid in product_ids:
                    for scope_val, kg_val, note_val in presets:
                        run_query(
                            "INSERT INTO carbon_factors (product_id, scope, kg_co2_per_unit, note) VALUES (?,?,?,?)",
                            (pid, scope_val, kg_val, note_val),
                            fetch=False,
                        )
        except Exception:
            pass

        with st.expander("📌 設定產品碳係數（kg CO₂/單位）", expanded=False):
            with st.form("carbon_factor_form"):
                df_p = pd.read_sql_query("SELECT product_id, name FROM inventory", sqlite3.connect(DB_FILE))
                prods = list(df_p['product_id']) if not df_p.empty else []
                p_id = st.selectbox("產品", prods) if prods else None
                scope = st.selectbox("範疇", [1, 2, 3], format_func=lambda x: {1: "Scope 1 直接排放", 2: "Scope 2 能源間接", 3: "Scope 3 供應鏈"}[x])
                kg_co2 = st.number_input("kg CO₂ / 單位", min_value=0.0, value=0.0, step=0.1)
                note = st.text_input("備註")
                if st.form_submit_button("儲存") and p_id is not None:
                    run_query("INSERT INTO carbon_factors (product_id, scope, kg_co2_per_unit, note) VALUES (?,?,?,?)", (p_id, scope, kg_co2, note or None), fetch=False)
                    st.success("已儲存碳係數")
        try:
            df_cf = pd.read_sql_query(
                "SELECT cf.product_id, i.name, cf.scope, cf.kg_co2_per_unit, cf.note FROM carbon_factors cf LEFT JOIN inventory i ON cf.product_id=i.product_id",
                sqlite3.connect(DB_FILE),
            )
            if not df_cf.empty:
                st.markdown("#### 已設定碳係數")
                st.dataframe(df_cf.rename(columns={"product_id": "品號", "name": "品名", "scope": "範疇", "kg_co2_per_unit": "kg CO₂/單位", "note": "備註"}), use_container_width=True, hide_index=True)
        except Exception:
            pass
        st.markdown("---")
        st.subheader("碳足跡報表", divider="blue")
        period = st.text_input("報表期間（年-月）", value=datetime.now().strftime("%Y-%m"))
        if st.button("計算碳足跡"):
            try:
                conn = sqlite3.connect(DB_FILE)
                q = """
                SELECT o.product_id, i.name, SUM(o.quantity) as qty,
                    ((SELECT COALESCE(SUM(cf.kg_co2_per_unit), 0) FROM carbon_factors cf WHERE cf.product_id=o.product_id AND cf.scope IN (1, 2)) +
                     (SELECT COALESCE(AVG(sp.carbon_factor), 2.5) FROM supplier_products sp WHERE sp.product_id=o.product_id)) as factor
                FROM orders o
                LEFT JOIN inventory i ON o.product_id=i.product_id
                WHERE strftime('%Y-%m', o.order_date)=? AND o.status != '已取消'
                GROUP BY o.product_id
                """
                df = pd.read_sql_query(q, conn, params=(period,))
                df['factor'] = df['factor'].fillna(0)
                df['kg_co2'] = df['qty'] * df['factor']
                total_kg = df['kg_co2'].sum()
                st.metric("當月產品碳足跡合計（銷售）", f"{total_kg:,.1f} kg CO₂e")
                st.dataframe(df.rename(columns={"product_id": "品號", "name": "品名", "qty": "銷售數量", "factor": "係數", "kg_co2": "kg CO₂e"}), use_container_width=True, hide_index=True)
                st.download_button("下載 ESG 摘要（文字）", f"ESG 碳足跡報告 {period}\n總碳排放（銷售）：{total_kg:,.1f} kg CO₂e\n期間：{period}", file_name=f"esg_carbon_{period}.txt", mime="text/plain")
                conn.close()
            except Exception as e:
                st.error(str(e))

    elif sub_menu == "減量目標":
        st.subheader("減量目標與達成率", divider="blue")
        st.caption("設定年度碳排放基準與減量目標，追蹤達成情況。")
        with st.expander("➕ 設定年度減量目標"):
            with st.form("esg_target_form"):
                y = st.number_input("目標年度", min_value=2020, max_value=2035, value=datetime.now().year)
                scope = st.selectbox("範疇", [1, 2, 3], format_func=lambda x: {1: "Scope 1", 2: "Scope 2", 3: "Scope 3"}[x])
                baseline = st.number_input("基準年排放（kg CO₂e）", min_value=0.0, value=0.0, step=100.0)
                target = st.number_input("目標排放（kg CO₂e）", min_value=0.0, value=0.0, step=100.0)
                note = st.text_input("備註")
                if st.form_submit_button("儲存") and (baseline > 0 or target > 0):
                    run_query("INSERT INTO esg_targets (target_year, scope, baseline_kg_co2, target_kg_co2, note) VALUES (?,?,?,?,?)", (y, scope, baseline, target, note or None), fetch=False)
                    st.success("已儲存目標")
        try:
            # 讀取完整資料（含 ID 用於操作）
            df_t_raw = pd.read_sql_query("SELECT id, target_year as 年度, scope as 範疇, baseline_kg_co2 as 基準, target_kg_co2 as 目標, note as 備註 FROM esg_targets ORDER BY target_year DESC, scope", sqlite3.connect(DB_FILE))
            if not df_t_raw.empty:
                # 顯示表格（隱藏 ID）
                st.dataframe(df_t_raw.drop(columns=['id']), use_container_width=True, hide_index=True)
                
                # ── 編輯與刪除功能 ──
                with st.expander("🛠️ 編輯或刪除現有目標"):
                    target_opts = [f"[{r['年度']} - Scope {r['範疇']}] {r['備註'] or ''} (ID:{r['id']})" for _, r in df_t_raw.iterrows()]
                    sel_target_str = st.selectbox("選擇要操作的目標", target_opts)
                    target_id = int(sel_target_str.split("(ID:")[1].replace(")", ""))
                    target_row = df_t_raw[df_t_raw['id'] == target_id].iloc[0]
                    
                    c1, c2 = st.columns(2)
                    with c1:
                        st.markdown("#### 📝 編輯")
                        with st.form(f"edit_target_{target_id}"):
                            new_y = st.number_input("年度", value=int(target_row['年度']))
                            new_scope = st.selectbox("範疇", [1, 2, 3], index=int(target_row['範疇'])-1)
                            new_base = st.number_input("基準排放", value=float(target_row['基準']))
                            new_target = st.number_input("目標排放", value=float(target_row['目標']))
                            new_note = st.text_input("備註", value=target_row['備註'] or "")
                            if st.form_submit_button("更新目標", use_container_width=True):
                                run_query(
                                    "UPDATE esg_targets SET target_year=?, scope=?, baseline_kg_co2=?, target_kg_co2=?, note=? WHERE id=?",
                                    (new_y, new_scope, new_base, new_target, new_note or None, target_id),
                                    fetch=False
                                )
                                st.success("目標已更新")
                                st.rerun()
                    with c2:
                        st.markdown("#### 🗑️ 刪除")
                        st.write(f"確定要移除 {sel_target_str} 嗎？")
                        if st.button("確認執行刪除", type="primary", use_container_width=True, key=f"del_tgt_{target_id}"):
                            run_query("DELETE FROM esg_targets WHERE id = ?", (target_id,), fetch=False)
                            st.success("目標已移除")
                            st.rerun()

                st.markdown("---")
                st.markdown("#### 達成率試算")
                sel_year = st.selectbox("選擇年度", options=sorted(df_t_raw['年度'].unique().tolist(), reverse=True), key="target_yr")
                conn = sqlite3.connect(DB_FILE)
                q_actual = """SELECT cf.scope, SUM(o.quantity * cf.kg_co2_per_unit) as kg 
                              FROM orders o JOIN carbon_factors cf ON cf.product_id=o.product_id 
                              WHERE strftime('%Y', o.order_date)=? AND o.status != '已取消' AND cf.scope IN (1, 2) GROUP BY cf.scope
                              UNION ALL
                              SELECT 3 as scope, SUM(poi.qty * sp.carbon_factor) as kg
                              FROM purchase_orders po
                              JOIN purchase_order_items poi ON po.po_id = poi.po_id
                              JOIN supplier_products sp ON po.supplier_id = sp.supplier_id AND poi.product_id = sp.product_id
                              WHERE strftime('%Y', po.order_date)=? AND (po.status != '已取消' OR po.status IS NULL)"""
                actual = pd.read_sql_query(q_actual, conn, params=(str(sel_year), str(sel_year)))
                conn.close()
                for _, row in df_t_raw[df_t_raw['年度'] == sel_year].iterrows():
                    scope_val = row['範疇']
                    target_kg = row['目標']
                    act = actual[actual['scope'] == scope_val]['kg'].sum() if not actual.empty else 0
                    if target_kg and target_kg > 0:
                        pct = (1 - act / target_kg) * 100 if target_kg else 0
                        st.caption(f"Scope {scope_val}：實際 {act:,.1f} kg / 目標 {target_kg:,.0f} kg → 達成率 {pct:.1f}%")
                
                st.markdown("#### 📊 目標 vs 實際排放（圖表）")
                rows_yr = df_t_raw[df_t_raw['年度'] == sel_year]
                if not rows_yr.empty and not actual.empty:
                    chart_data = []
                    for _, row in rows_yr.iterrows():
                        scope_val = int(row['範疇'])
                        target_kg = row['目標'] or 0
                        act = actual[actual['scope'] == scope_val]['kg'].sum()
                        chart_data.append({"範疇": f"Scope {scope_val}", "目標 (kg CO₂e)": target_kg, "實際 (kg CO₂e)": act})
                    df_chart = pd.DataFrame(chart_data)
                    if not df_chart.empty:
                        fig_bar = go.Figure(data=[
                            go.Bar(name="目標", x=df_chart["範疇"], y=df_chart["目標 (kg CO₂e)"], marker_color="#2e7d32"),
                            go.Bar(name="實際", x=df_chart["範疇"], y=df_chart["實際 (kg CO₂e)"], marker_color="#1565c0"),
                        ])
                        fig_bar.update_layout(barmode="group", title=f"{sel_year} 年減量目標 vs 實際排放", xaxis_title="範疇", yaxis_title="kg CO₂e")
                        st.plotly_chart(fig_bar, use_container_width=True)
            else:
                st.info("尚無減量目標，請展開上方選單新增。")
        except Exception as e:
            st.info(f"載入目標發生錯誤: {e}")

    elif sub_menu == "年度碳目標分析":
        st.subheader("🤖 AI 年度碳排放目標分析", divider="blue")
        st.caption("依歷史排放與既有目標，由 AI 提供年度碳目標建議與圖表分析。")
        analysis_year = st.number_input("分析年度", min_value=2020, max_value=2030, value=datetime.now().year, key="carbon_analysis_yr")
        if st.button("產生 AI 碳目標分析"):
            conn = sqlite3.connect(DB_FILE)
            q_month = """
            SELECT ym, SUM(kg) as kg FROM (
                SELECT strftime('%Y-%m', o.order_date) as ym, SUM(o.quantity * cf.kg_co2_per_unit) as kg
                FROM orders o JOIN carbon_factors cf ON cf.product_id = o.product_id
                WHERE o.status != '已取消' AND cf.scope IN (1, 2) GROUP BY strftime('%Y-%m', o.order_date)
                UNION ALL
                SELECT strftime('%Y-%m', po.order_date) as ym, SUM(poi.qty * sp.carbon_factor) as kg
                FROM purchase_orders po
                JOIN purchase_order_items poi ON po.po_id = poi.po_id
                JOIN supplier_products sp ON po.supplier_id = sp.supplier_id AND poi.product_id = sp.product_id
                WHERE po.status != '已取消' OR po.status IS NULL GROUP BY strftime('%Y-%m', po.order_date)
            ) GROUP BY ym
            """
            df_all = pd.read_sql_query(q_month, conn)
            prev_year = str(analysis_year - 1)
            curr_year = str(analysis_year)
            df_prev = df_all[df_all['ym'].str.startswith(prev_year)] if not df_all.empty else pd.DataFrame()
            df_curr = df_all[df_all['ym'].str.startswith(curr_year)] if not df_all.empty else pd.DataFrame()
            targets = pd.read_sql_query("SELECT target_year, scope, baseline_kg_co2, target_kg_co2 FROM esg_targets WHERE target_year=?", conn, params=(analysis_year,))
            
            # [NEW] 找出排放量最高的前 5 名商品以便 AI 分析
            q_top_emit = """
                SELECT name, SUM(kg) as kg FROM (
                    SELECT i.name, SUM(o.quantity * cf.kg_co2_per_unit) as kg
                    FROM orders o JOIN carbon_factors cf ON cf.product_id = o.product_id
                    JOIN inventory i ON i.product_id = o.product_id
                    WHERE strftime('%Y', o.order_date)=? AND o.status != '已取消' AND cf.scope IN (1, 2)
                    GROUP BY i.name
                    UNION ALL
                    SELECT i.name, SUM(poi.qty * sp.carbon_factor) as kg
                    FROM purchase_orders po JOIN purchase_order_items poi ON po.po_id = poi.po_id
                    JOIN supplier_products sp ON po.supplier_id = sp.supplier_id AND poi.product_id = sp.product_id
                    JOIN inventory i ON i.product_id = poi.product_id
                    WHERE strftime('%Y', po.order_date)=? AND (po.status != '已取消' OR po.status IS NULL)
                    GROUP BY i.name
                ) GROUP BY name ORDER BY kg DESC LIMIT 5
            """
            df_top_emit = pd.read_sql_query(q_top_emit, conn, params=(curr_year, curr_year))
            top_emit_text = ""
            if not df_top_emit.empty:
                top_emit_text = "\n".join([f"- {r['name']}: {r['kg']:,.1f} kg" for _, r in df_top_emit.iterrows()])

            total_prev = float(df_prev['kg'].sum()) if not df_prev.empty else 0
            total_curr = float(df_curr['kg'].sum()) if not df_curr.empty else 0
            target_total = float(targets['target_kg_co2'].sum()) if not targets.empty else 0
            conn.close()
            ai_analysis = ""
            if True:  # issue #27：統一 LLM 入口（.env 驅動），不再依賴側邊欄 key
                try:
                    from backend.llm_client import complete_text
                    prompt = f"""你是一位永續與碳管理顧問。請根據以下數據，用繁體中文撰寫「年度碳排放趨勢深度分析報告」：
- 分析年度：{analysis_year}
- 前一年度總排放：{total_prev:,.1f} kg CO₂e
- 本年度目前總排放：{total_curr:,.1f} kg CO₂e
- 已設定年度目標：{target_total:,.1f} kg CO₂e

【高排放商品 Top 5】
{top_emit_text if top_emit_text else "暫無明確品項數據"}

請提供：
1. 排放趨勢與目標落差分析
2. **關鍵排放源辨識**：針對上述高排放產品提出具體的減量或替代建議
3. **動態減量空間評估**：預估下一年度可能的減量區間與策略
4. **管理層具體行動**：建議 3 個具備 ESG 亮點的手法

回覆請條列、專業，且具備洞察力。"""
                    ai_analysis = (complete_text(prompt, temperature=0.3,
                                                 tag="analysis:carbon_target") or "").strip()
                except Exception as e:
                    ai_analysis = f"AI 分析暫時無法產生（{e}）。請確認 .env 模型設定與網路。"
            st.markdown("#### AI 分析摘要")
            st.markdown(ai_analysis)
            st.markdown("---")
            st.markdown("#### 📊 年度目標與實際排放圖表")
            chart_data = []
            if total_curr > 0 or target_total > 0:
                chart_data.append({"項目": "實際排放", "kg CO₂e": total_curr})
                if target_total > 0:
                    chart_data.append({"項目": "年度目標", "kg CO₂e": target_total})
            if total_prev > 0:
                chart_data.append({"項目": "前一年度", "kg CO₂e": total_prev})
            if chart_data:
                df_ch = pd.DataFrame(chart_data)
                fig = px.bar(df_ch, x="項目", y="kg CO₂e", title=f"{analysis_year} 年碳排放：實際 vs 目標", color="項目",
                             color_discrete_map={"實際排放": "#1565c0", "年度目標": "#2e7d32", "前一年度": "#78909c"})
                st.plotly_chart(fig, use_container_width=True)
            if not df_prev.empty or not df_curr.empty:
                series = []
                if not df_prev.empty:
                    for _, r in df_prev.iterrows():
                        series.append({"月份": r["ym"], "排放 (kg CO₂e)": r["kg"], "年度": str(analysis_year - 1)})
                if not df_curr.empty:
                    for _, r in df_curr.iterrows():
                        series.append({"月份": r["ym"], "排放 (kg CO₂e)": r["kg"], "年度": str(analysis_year)})
                df_trend = pd.DataFrame(series)
                fig_trend = px.line(df_trend, x="月份", y="排放 (kg CO₂e)", color="年度", title="各月碳排放趨勢比較", markers=True)
                fig_trend.update_layout(xaxis_tickangle=-45)
                st.plotly_chart(fig_trend, use_container_width=True)

    elif sub_menu == "供應商風險與碳排":
        st.subheader("供應商 ESG 雙重風險分析", divider="blue")
        st.caption("結合採購紀錄與產品碳係數，並交叉比對供應商風險等級，找出雙重風險供應商。")
        
        conn = sqlite3.connect(DB_FILE)
        
        # 建議一：結合財務數據 (Total Spend, Carbon Intensity)，直接動態查詢資料庫以保持同步
        q = """
        SELECT 
            s.supplier_id as 供應商代號, 
            s.name as 供應商名稱, 
            s.risk_level as 風險等級, 
            COALESCE(SUM(poi.qty), 0) as 總採購數量,
            COALESCE(SUM(poi.qty * poi.unit_price), 0) as 總採購金額,
            COALESCE(SUM(poi.qty * sp.carbon_factor), s_tot.tot_carbon) as 供應商總碳排_kg_CO2e
        FROM suppliers s
        LEFT JOIN (SELECT supplier_id, SUM(carbon_factor) as tot_carbon FROM supplier_products GROUP BY supplier_id) s_tot ON s.supplier_id = s_tot.supplier_id
        LEFT JOIN purchase_orders po ON s.supplier_id = po.supplier_id AND (po.status != '已取消' OR po.status IS NULL)
        LEFT JOIN purchase_order_items poi ON po.po_id = poi.po_id
        LEFT JOIN supplier_products sp ON poi.product_id = sp.product_id AND s.supplier_id = sp.supplier_id
        WHERE s.is_official = 1
        GROUP BY s.supplier_id, s.name, s.risk_level
        ORDER BY 供應商總碳排_kg_CO2e DESC
        """
        try:
            df = pd.read_sql_query(q, conn)
            # Fetch suppliers locations for geographic visualization (建議三)
            q_loc = "SELECT supplier_id, country, region, latitude, longitude FROM suppliers WHERE latitude IS NOT NULL"
            df_loc = pd.read_sql_query(q_loc, conn)

            if not df.empty:
                # 處理風險等級空值
                df['風險等級'] = df['風險等級'].fillna('未評估')
                df['總採購金額'] = df['總採購金額'].fillna(0)
                
                # 計算碳強度 (每萬元花費產生多少 kg 碳排)
                df['碳強度(kg/萬元)'] = df.apply(
                    lambda r: r['供應商總碳排_kg_CO2e'] / (r['總採購金額'] / 10000) if r['總採購金額'] > 0 else 0,
                    axis=1
                )
                
                # 計算是否為高排碳 (前 20%)
                threshold = df['供應商總碳排_kg_CO2e'].quantile(0.8)
                def get_warning(row):
                    if row['風險等級'] == '高' and row['供應商總碳排_kg_CO2e'] >= threshold:
                        return "🚨 雙重風險"
                    elif row['風險等級'] == '高':
                        return "⚠️ 高風險"
                    elif row['供應商總碳排_kg_CO2e'] >= threshold:
                        return "🔥 高碳排"
                    return "✅ 正常"
                    
                df['風險狀態'] = df.apply(get_warning, axis=1)
                
                # Metrics
                high_risk_count = len(df[df['風險狀態'] == '🚨 雙重風險'])
                col1, col2, col3 = st.columns(3)
                col1.metric("供應商總數", len(df))
                col2.metric("🚨 雙重風險供應商數", high_risk_count)
                col3.metric("總供應鏈碳排", f"{df['供應商總碳排_kg_CO2e'].sum():,.0f} kg")
                
                st.markdown("#### 📋 供應商碳排與風險清單")
                st.dataframe(
                    df.style.format({
                        "總採購金額": "${:,.0f}", 
                        "供應商總碳排_kg_CO2e": "{:,.1f}", 
                        "碳強度(kg/萬元)": "{:,.1f}"
                    }),
                    use_container_width=True, hide_index=True
                )
                
                # ── 80/20 高風險供應商替換機制 ──
                st.markdown("---")
                st.markdown("#### 🔄 高風險與雙重風險供應商替換 (80/20 法則)")
                high_risk_df = df[df['風險狀態'].isin(['🚨 雙重風險', '⚠️ 高風險'])]
                if not high_risk_df.empty:
                    opts = [f"{r['供應商代號']} - {r['供應商名稱']} ({r['風險狀態']})" for _, r in high_risk_df.iterrows()]
                    to_replace = st.selectbox("選擇要替換的高風險供應商：", opts)
                    target_sup_id = to_replace.split(" - ")[0]
                    
                    st.caption("系統為您推薦同供貨能力中，非高風險且碳排總計更低的候補供應商：")
                    # 抓取候補供應商
                    q_cand = """
                    SELECT s.supplier_id as 代號, s.name as 名稱, s.risk_level as 風險等級, s_tot.tot_carbon as 碳排總和
                    FROM suppliers s
                    JOIN (SELECT supplier_id, SUM(carbon_factor) as tot_carbon FROM supplier_products GROUP BY supplier_id) s_tot ON s.supplier_id = s_tot.supplier_id
                    WHERE s.is_official = 0 AND s.risk_level IN ('低', '中')
                    ORDER BY 碳排總和 ASC
                    LIMIT 5
                    """
                    df_cand = pd.read_sql_query(q_cand, conn)
                    if not df_cand.empty:
                        st.dataframe(df_cand.style.format({"碳排總和": "{:.2f}"}), use_container_width=True, hide_index=True)
                        cand_opts = [f"{r['代號']} - {r['名稱']} (碳排: {r['碳排總和']:.2f}, 風險: {r['風險等級']})" for _, r in df_cand.iterrows()]
                        new_sup = st.selectbox("選擇替換為：", cand_opts)
                        new_sup_id = new_sup.split(" - ")[0]
                        reason = st.text_input("替換原因 (選填)")
                        if st.button("執行替換", type="primary"):
                            run_query("UPDATE suppliers SET is_official = 0 WHERE supplier_id=?", (target_sup_id,), fetch=False)
                            run_query("UPDATE suppliers SET is_official = 1 WHERE supplier_id=?", (new_sup_id,), fetch=False)
                            run_query("INSERT INTO supplier_replacement_logs (original_supplier_id, new_supplier_id, replaced_at, reason, executed_by) VALUES (?, ?, ?, ?, ?)", (target_sup_id, new_sup_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), reason, "Admin"), fetch=False)
                            st.success(f"已成功將 {target_sup_id} 替換為 {new_sup_id}！")
                            import time
                            time.sleep(1)
                            st.rerun()
                    else:
                        st.warning("目前無合適的低/中風險備位供應商。")
                else:
                    st.success("目前正式名單中無高風險或雙重風險供應商！")

                # 建議二：AI 應對策略與替代方案推薦
                st.markdown("#### 🤖 AI 供應商優化與減排策略推演")
                dangerous_suppliers = df[df['風險狀態'] == '🚨 雙重風險'].to_dict(orient="records")
                if high_risk_count > 0:
                    if st.button("生成「雙重風險」供應商優化方案", type="primary"):
                        if True:  # issue #27：統一 LLM 入口（.env 驅動）
                            try:
                                from backend.llm_client import complete_text
                                prompt = f"以下是我司目前的「雙重風險」供應商名單（高排碳且具備高地緣風險）：\n{dangerous_suppliers}\n\n請做為永續供應鏈顧問，提供：\n1. 短期應對策略與替代備案尋找方向；\n2. 議價與減排輔導方案建議。\n\n以繁體中文且具備行動力的方式條列回覆。"
                                with st.spinner("AI 策略生成中..."):
                                    _text = complete_text(prompt, temperature=0.3,
                                                          tag="analysis:supplier_risk")
                                st.success("策略已產生！")
                                st.markdown(_text)
                            except Exception as e:
                                show_error("AI 生成失敗", e)
                else:
                    st.success("目前沒有雙重風險的供應商，供應鏈健康！")

                st.markdown("---")
                
                # 建議三：雙層地圖視覺化 (Geo-Spatial Mapping)
                st.markdown("#### 🌍 全球供應商風險與碳排地圖")
                merged_map = pd.merge(df, df_loc, left_on="供應商代號", right_on="supplier_id", how="inner")
                if not merged_map.empty:
                    fig_map = px.scatter_geo(
                        merged_map,
                        lat="latitude",
                        lon="longitude",
                        color="風險等級",
                        size="供應商總碳排_kg_CO2e",
                        hover_name="供應商名稱",
                        hover_data={"風險狀態": True, "總採購金額": ":,.0f", "碳強度(kg/萬元)": ":,.1f", "latitude": False, "longitude": False, "供應商代號": False, "supplier_id": False, "country": False, "region": False, "風險等級": False},
                        color_discrete_map={"高": "red", "中": "orange", "低": "green", "未評估": "gray"},
                        title="圓圈大小代表碳排量，顏色代表風險等級"
                    )
                    fig_map.update_traces(marker=dict(line=dict(width=1, color="darkred")))
                    fig_map.update_layout(
                        geo=dict(showland=True, landcolor="#EAEAEA", showocean=True, oceancolor="#E0F7FA"),
                        margin=dict(l=0, r=0, t=40, b=0)
                    )
                    st.plotly_chart(fig_map, use_container_width=True)
                else:
                    st.info("尚無包含緯經度座標的供應商資料，無法繪製全球地圖。")

                st.markdown("---")
                
                # 建議四：時間維度與趨勢追蹤
                st.markdown("#### 📈 供應商碳排趨勢監控 (近半年)")
                # 即時反映各期間採購單所帶來的碳排變動
                q_trend = """
                SELECT 
                    s.name as 供應商名稱, 
                    strftime('%Y-%m', po.order_date) as 月份,
                    SUM(poi.qty * cf.kg_co2_per_unit) as 碳排量
                FROM purchase_orders po
                JOIN purchase_order_items poi ON po.po_id = poi.po_id
                JOIN suppliers s ON po.supplier_id = s.supplier_id
                JOIN carbon_factors cf ON poi.product_id = cf.product_id
                WHERE po.status != '已取消' AND po.order_date >= date('now', '-6 month')
                GROUP BY s.name, strftime('%Y-%m', po.order_date)
                ORDER BY 月份 ASC
                """
                df_trend = pd.read_sql_query(q_trend, conn)
                if not df_trend.empty:
                    fig_trend = px.line(
                        df_trend, x="月份", y="碳排量", color="供應商名稱", markers=True,
                        title="主要供應商每月碳排產生量變化"
                    )
                    fig_trend.update_layout(xaxis_tickangle=-45)
                    st.plotly_chart(fig_trend, use_container_width=True)
                else:
                    st.caption("近半年內尚無足夠的資料繪製趨勢圖。")

                st.markdown("---")
                st.markdown("#### 📊 採購金額 vs 總碳排矩陣 (散佈圖)")
                
                fig = px.scatter(
                    df, 
                    x="總採購金額", 
                    y="供應商總碳排_kg_CO2e", 
                    color="風險等級",
                    color_discrete_map={"高": "red", "中": "orange", "低": "green", "未評估": "gray"},
                    size="碳強度(kg/萬元)",
                    hover_name="供應商名稱",
                    title="供應商採購金額 vs 總碳排矩陣 (圓斑大小: 碳強度)"
                )
                
                # 加入高碳排檻線
                fig.add_hline(y=threshold, line_dash="dash", line_color="gray", annotation_text="高碳排閾值 (Top 20%)")
                st.plotly_chart(fig, use_container_width=True)
                
            else:
                st.info("尚無供應商採購碳排資料，請確認已有帶碳係數的採購單。")
                
        except Exception as e:
            show_error("分析發生錯誤", e)
        finally:
            conn.close()

    else:  # ESG 報告
        st.subheader("ESG 報告產出", divider="blue")
        st.caption("一鍵產生碳排放與供應鏈風險摘要，供永續報告使用。")
        report_year = st.text_input("報告年度", value=datetime.now().strftime("%Y"), key="esg_yr")
        if st.button("產生 ESG 報告"):
            conn = sqlite3.connect(DB_FILE)
            lines = []
            lines.append("=" * 50)
            lines.append(f"ESG 永續報告摘要 — {report_year} 年")
            lines.append("=" * 50)
            q_c = """SELECT cf.scope, SUM(o.quantity * cf.kg_co2_per_unit) as kg 
                  FROM orders o JOIN carbon_factors cf ON cf.product_id=o.product_id 
                  WHERE strftime('%Y', o.order_date)=? AND o.status != '已取消' AND cf.scope IN (1, 2) GROUP BY cf.scope
                  UNION ALL
                  SELECT 3 as scope, SUM(poi.qty * sp.carbon_factor) as kg
                  FROM purchase_orders po JOIN purchase_order_items poi ON po.po_id = poi.po_id
                  JOIN supplier_products sp ON po.supplier_id = sp.supplier_id AND poi.product_id = sp.product_id
                  WHERE strftime('%Y', po.order_date)=? AND (po.status != '已取消' OR po.status IS NULL)"""
            df_c = pd.read_sql_query(q_c, conn, params=(report_year, report_year))
            total_co2 = df_c['kg'].sum() if not df_c.empty else 0
            lines.append(f"\n【溫室氣體排放】\n總碳排放（產品銷售）：{total_co2:,.1f} kg CO₂e")
            if not df_c.empty:
                for _, r in df_c.iterrows():
                    lines.append(f"  Scope {int(r['scope'])}：{r['kg']:,.1f} kg CO₂e")
            events = pd.read_sql_query("SELECT event_type, region, country, impact_days, description, created_at FROM supply_chain_events WHERE created_at LIKE ? ORDER BY id DESC LIMIT 10", conn, params=(f"{report_year}%",))
            lines.append(f"\n【供應鏈風險事件】\n共 {len(events)} 筆")
            if not events.empty:
                for _, r in events.iterrows():
                    lines.append(f"  {r['event_type']} — {r['region'] or r['country'] or '-'}（+{r['impact_days']} 天）{r['description'] or ''}")
            try:
                high_risk = pd.read_sql_query("SELECT COUNT(*) as c FROM suppliers WHERE risk_level='高'", conn)
                lines.append(f"\n【供應商風險】\n高風險供應商家數：{high_risk['c'].iloc[0] if not high_risk.empty else 0}")
            except Exception:
                pass
            lines.append("\n" + "=" * 50)
            conn.close()
            
            # 將數據存入 session_state 以便後續 AI 生成與顯示
            st.session_state.esg_report_text = "\n".join(lines)
            st.session_state.esg_total_co2 = total_co2
            st.session_state.esg_df_c = df_c
            st.session_state.esg_events = events

        # 若已產生報告，則顯示內容與 AI 按鈕
        if st.session_state.get("esg_report_text"):
            report_text = st.session_state.esg_report_text
            st.text_area("報告內容", report_text, height=300)

            # ── AI ESG 永續績效敘述 (獨立於報告按鈕外) ──────────────────────────────────
            st.markdown("---")
            if st.button("🤖 生成 AI 永續績效敘述"):
                if True:  # issue #27：統一 LLM 入口（.env 驅動）
                    try:
                        from backend.llm_client import complete_text

                        # 從 session_state 取回數據
                        total_co2 = st.session_state.get("esg_total_co2", 0)
                        df_c = st.session_state.get("esg_df_c", pd.DataFrame())
                        events = st.session_state.get("esg_events", pd.DataFrame())

                        esg_prompt = f"""
                        請根據以下 ESG 數據摘要，撰寫一段專業的「年度永續績效敘述」（繁體中文）：
                        【年度】：{report_year}
                        【碳排放數據】：
                        - 總排放：{total_co2:,.1f} kg CO₂e
                        - 範疇分布：{df_c.to_string(index=False) if not df_c.empty else "無細節"}
                        
                        【供應鏈與社會面】：
                        - 風險事件總數：{len(events)} 筆
                        - 重點事件摘要：{events['event_type'].value_counts().to_dict() if not events.empty else "無"}
                        
                        請輸出：
                        1. 一段具備「前瞻性」與「責任感」的績效總結（約 300 字）。
                        2. 該年度最值得表彰的 ESG 亮點。
                        3. 語氣需莊重、專業，模擬永續長 (CSO) 的口吻。
                        """
                        
                        with st.spinner("AI 正在撰寫永續敘事..."):
                            _text = complete_text(esg_prompt, temperature=0.4,
                                                  tag="analysis:esg_narrative")
                        st.success("AI 永續敘事產出完成！")
                        st.markdown(_text)
                    except Exception as e:
                        show_error("AI 敘事生成失敗", e)

            st.download_button("下載報告（文字檔）", report_text, file_name=f"ESG_Report_{report_year}.txt", mime="text/plain", key="dl_esg")
