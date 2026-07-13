"""
frontend/page_sales.py
💰 銷售管理（報價單、銷售單、客戶消費視覺化、個人消費分析、收款管理）
"""

import sqlite3
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime
from backend import DB_FILE, run_query


def render(sub_menu: str, api_key: str):
    st.markdown("<div class='premium-title'>💰 銷售管理</div>", unsafe_allow_html=True)
    menus = ['報價單', '銷售單', '客戶消費視覺化', '客戶個人消費分析', '收款管理']
    styled_menus = [f"🌟 **{m}**" if m == sub_menu else f"{m}" for m in menus]
    st.markdown(" ｜ ".join(styled_menus))

    if sub_menu == "報價單":
        st.subheader("報價單", divider="blue")
        with st.expander("➕ 建立報價單"):
            with st.form("add_quote"):
                qid = st.text_input("報價單號", value=f"QT-{datetime.now().strftime('%Y%m%d%H%M')}")
                df_c = pd.read_sql_query("SELECT customer_id, name FROM customers", sqlite3.connect(DB_FILE))
                c_opts = list(df_c['customer_id']) if not df_c.empty else []
                cust = st.selectbox("客戶", c_opts) if c_opts else st.text_input("客戶代號")
                df_p = pd.read_sql_query("SELECT product_id, name, price FROM inventory", sqlite3.connect(DB_FILE))
                p_opts = list(df_p['product_id']) if not df_p.empty else []
                prod = st.selectbox("品項", p_opts) if p_opts else None
                qty = st.number_input("數量", min_value=1)
                default_price = 0
                if prod and not df_p.empty:
                    row = df_p[df_p['product_id'] == prod]
                    if not row.empty:
                        default_price = int(row['price'].iloc[0])
                price = st.number_input("單價", min_value=0, value=default_price)
                valid = st.date_input("報價有效至")
                if st.form_submit_button("建立") and qid and cust and prod:
                    total = qty * price
                    try:
                        run_query("INSERT INTO quotations VALUES (?,?,?,?,?,?)", (qid, cust, datetime.now().strftime("%Y-%m-%d"), "有效", total, valid.strftime("%Y-%m-%d")), fetch=False)
                        run_query("INSERT INTO quotation_items (quote_id, product_id, qty, unit_price) VALUES (?,?,?,?)", (qid, prod, qty, float(price)), fetch=False)
                        st.success(f"報價單 {qid} 已建立，金額 {total:,.0f} 元")
                    except sqlite3.IntegrityError:
                        st.error("報價單號已存在")
        try:
            df = pd.read_sql_query(
                """SELECT q.quote_id as 報價單號, c.name as 客戶, q.quote_date as 日期, q.status as 狀態, q.total_amount as 金額 FROM quotations q LEFT JOIN customers c ON q.customer_id=c.customer_id""",
                sqlite3.connect(DB_FILE),
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception:
            st.info("尚無報價單")

    elif sub_menu == "銷售單":
        st.subheader("銷售單 (訂單管理)", divider="blue")
        with st.expander("➕ 建立銷售單"):
            with st.form("add_order_form", clear_on_submit=True):
                o_id = st.text_input("訂單編號", value=f"ORD-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
                df_c = pd.read_sql_query("SELECT customer_id, name FROM customers", sqlite3.connect(DB_FILE))
                c_opts = {r['customer_id']: f"{r['customer_id']} - {r['name']}" for _, r in df_c.iterrows()} if not df_c.empty else {}
                cust = st.selectbox("客戶", list(c_opts.keys()), format_func=lambda x: c_opts.get(x, x)) if c_opts else st.text_input("客戶代號", "C001")
                df_inv = pd.read_sql_query("SELECT product_id, name, price FROM inventory", sqlite3.connect(DB_FILE))
                prod_opts = {r['product_id']: f"{r['product_id']} - {r['name']} (${r['price']})" for _, r in df_inv.iterrows()} if not df_inv.empty else {}
                p_id = st.selectbox("產品", list(prod_opts.keys()), format_func=lambda x: prod_opts.get(x, x)) if prod_opts else None
                o_qty = st.number_input("數量", min_value=1)
                o_status = st.selectbox("狀態", ["處理中", "已出貨", "已取消"])
                if st.form_submit_button("送出") and o_id and p_id:
                    res = run_query("SELECT stock, name, price FROM inventory WHERE product_id=?", (p_id,))
                    if not res:
                        st.error("找不到產品")
                    else:
                        stock, pname, up = res[0]
                        total = o_qty * up
                        if o_status != "已取消" and stock < o_qty:
                            st.error(f"庫存不足，目前 {stock} 件")
                        else:
                            try:
                                run_query(
                                    "INSERT INTO orders (order_id, customer_id, product_id, quantity, status, order_date, total_amount) VALUES (?,?,?,?,?,?,?)",
                                    (o_id, cust, p_id, o_qty, o_status, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), total),
                                    fetch=False,
                                )
                                if o_status != "已取消":
                                    run_query("UPDATE inventory SET stock=? WHERE product_id=?", (stock - o_qty, p_id), fetch=False)
                                st.success(f"訂單 {o_id} 已建立，金額 {total:,.0f} 元")
                            except sqlite3.IntegrityError:
                                st.error("訂單編號已存在")
        with st.expander("🔄 更新訂單狀態（消除沙漏 ⏳／逾期 🚨）"):
            st.caption("警示說明：⏳ 處理中（未滿 3 天）｜🚨 逾期（處理中超過 3 天）｜✅ 已出貨／已取消。將訂單改為「已出貨」或「已取消」後，警示會顯示 ✅。")
            ord_list = run_query("SELECT order_id, status FROM orders ORDER BY order_date DESC LIMIT 100")
            if ord_list:
                ord_opts = {r[0]: f"{r[0]} ({r[1]})" for r in ord_list}
                with st.form("update_order_status"):
                    sel_ord = st.selectbox("選擇訂單", list(ord_opts.keys()), format_func=lambda x: ord_opts.get(x, x))
                    new_status = st.selectbox("新狀態", ["已出貨", "已取消", "處理中"])
                    if st.form_submit_button("更新狀態") and sel_ord:
                        run_query("UPDATE orders SET status=? WHERE order_id=?", (new_status, sel_ord), fetch=False)
                        st.success(f"訂單 {sel_ord} 已更新為「{new_status}」。重新整理後警示將顯示 ✅。")
            else:
                st.info("尚無訂單")
        # ── 銷售單查詢與篩選面板 (可摺疊) ──
        if "form_version" not in st.session_state:
            st.session_state.form_version = 0
            
        if "search_oid" not in st.session_state:
            st.session_state.search_oid = ""
        if "date_start" not in st.session_state:
            st.session_state.date_start = datetime(2023, 1, 1).date()
        if "date_end" not in st.session_state:
            st.session_state.date_end = datetime.now().date()
        if "search_status" not in st.session_state:
            st.session_state.search_status = "全部"

        def reset_filters():
            st.session_state.search_oid = ""
            st.session_state.date_start = datetime(2023, 1, 1).date()
            st.session_state.date_end = datetime.now().date()
            st.session_state.search_status = "全部"
            # 遞增版本號，強制 Streamlit 銷毀並重新生成 Form (這能保證狀態徹底重置)
            st.session_state.form_version = st.session_state.get("form_version", 0) + 1

        with st.expander("🔍 進階查詢", expanded=False):
            # 使用動態 Key 確保重置時 Form 被銷毀重構
            with st.form(f"search_form_v{st.session_state.form_version}"):
                f_col1, f_col2, f_col3, f_col4 = st.columns([2, 2, 2, 2])
                with f_col1:
                    search_id = st.text_input("搜尋單號", placeholder="輸入單號關鍵字...", key="search_oid")
                with f_col2:
                    d_start = st.date_input("開始日期", key="date_start")
                with f_col3:
                    d_end = st.date_input("結束日期", key="date_end")
                with f_col4:
                    status_opts = ["全部", "處理中", "已出貨", "已取消"]
                    search_status = st.selectbox("訂單狀態", status_opts, key="search_status")
                
                st.form_submit_button("🔍 執行搜尋", use_container_width=True)
            
            st.button("🔄 重設過濾條件", use_container_width=True, on_click=reset_filters)

        # 動態構建 SQL 查詢
        query_base = """
            SELECT o.order_id as 訂單編號, c.name as 客戶, i.name as 產品, 
                   o.quantity as 數量, o.status as 狀態, o.total_amount as 金額, o.order_date as 日期 
            FROM orders o 
            LEFT JOIN inventory i ON o.product_id=i.product_id 
            LEFT JOIN customers c ON o.customer_id=c.customer_id
            WHERE 1=1
        """
        params = []
        if search_id:
            query_base += " AND o.order_id LIKE ?"
            params.append(f"%{search_id}%")
        
        # 處理日期區間 (SQLite 的日期字串比較)
        query_base += " AND DATE(o.order_date) >= ? AND DATE(o.order_date) <= ?"
        params.append(d_start.strftime('%Y-%m-%d'))
        params.append(d_end.strftime('%Y-%m-%d'))

        if search_status != "全部":
            query_base += " AND o.status = ?"
            params.append(search_status)

        query_base += " ORDER BY o.order_date DESC"

        try:
            df_ord = pd.read_sql_query(query_base, sqlite3.connect(DB_FILE), params=params)
            
            if not df_ord.empty:
                def check_overdue(row):
                    if row['狀態'] in ['已出貨', '已取消']:
                        return "✅"
                    try:
                        # 使用 pd.to_datetime 並轉為 Python datetime 對照
                        dt = pd.to_datetime(row['日期']).to_pydatetime()
                        if (datetime.now() - dt).days >= 3:
                            return "🚨 逾期"
                    except Exception:
                        pass
                    return "⏳"
                df_ord['警示'] = df_ord.apply(check_overdue, axis=1)
                
                # 重新排序列順序，讓警示顯眼一點
                cols = list(df_ord.columns)
                if '警示' in cols:
                    cols.insert(0, cols.pop(cols.index('警示')))
                    df_ord = df_ord[cols]
                
            st.dataframe(df_ord, use_container_width=True, hide_index=True)
            if df_ord.empty:
                st.info("💡 找不到符合條件的訂單。")
            else:
                # ── 動態視覺化分析區塊 (依篩選結果) ──
                st.markdown("---")
                st.markdown("📊 **此篩選區間的動態數據分析**")
                
                # 1. 關鍵指標 KPIs
                total_amt = df_ord["金額"].sum()
                num_orders = len(df_ord)
                total_qty = df_ord["數量"].sum()
                avg_val = total_amt / num_orders if num_orders > 0 else 0
                
                k_col1, k_col2, k_col3, k_col4 = st.columns(4)
                k_col1.metric("篩選總營收", f"${total_amt:,.0f}")
                k_col2.metric("訂單總數", f"{num_orders:,.0f} 筆")
                k_col3.metric("總銷售數量", f"{total_qty:,.0f} 件")
                k_col4.metric("平均客單價", f"${avg_val:,.0f}")
                
                # 2. 視覺化圖表
                c_col1, c_col2 = st.columns(2)
                
                with c_col1:
                    # 訂單狀態分布 (圓餅圖)
                    st.markdown("**訂單狀態分布**")
                    df_status = df_ord["狀態"].value_counts().reset_index()
                    df_status.columns = ["狀態", "筆數"]
                    fig_status = px.pie(df_status, values="筆數", names="狀態", 
                                       color_discrete_map={"已出貨": "#10B981", "處理中": "#F59E0B", "已取消": "#EF4444"})
                    fig_status.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10))
                    st.plotly_chart(fig_status, use_container_width=True)
                
                with c_col2:
                    # 每日銷售趨勢 (折線圖)
                    st.markdown("**每日銷售趨勢**")
                    # 將日期格式化為 YYYY-MM-DD 以便群組，使用 errors='coerce' 避免解析失敗中斷
                    df_trend = df_ord.copy()
                    df_trend["日"] = pd.to_datetime(df_trend["日期"], errors='coerce').dt.strftime('%Y-%m-%d')
                    df_daily = df_trend.groupby("日")["金額"].sum().reset_index()
                    
                    fig_trend = px.line(df_daily, x="日", y="金額", markers=True, 
                                       line_shape="linear", color_discrete_sequence=["#3B82F6"])
                    fig_trend.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), 
                                           xaxis_title="日期", yaxis_title="金額")
                    st.plotly_chart(fig_trend, use_container_width=True)

                st.markdown("**篩選範圍內之熱銷商品排行 (Top 10)**")
                df_prod_rank = df_ord.groupby("產品")["數量"].sum().reset_index().sort_values("數量", ascending=False).head(10)
                fig_prod_rank = px.bar(df_prod_rank, x="產品", y="數量", 
                                      color_discrete_sequence=["#8B5CF6"])
                fig_prod_rank.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10),
                                            xaxis={'categoryorder':'total descending'})
                st.plotly_chart(fig_prod_rank, use_container_width=True)

        except Exception as e:
            st.error(f"查詢出錯: {e}")


    elif sub_menu == "客戶消費視覺化":
        st.subheader("客戶消費視覺化", divider="blue")
        st.caption("依公司檢視本月累積、當年度累積消費量與總消費金額，並以圖表呈現當年度趨勢。")
        conn = sqlite3.connect(DB_FILE)
        sel_year = st.sidebar.number_input("年度", min_value=2020, max_value=2030, value=datetime.now().year, key="viz_year")
        df_cust = pd.read_sql_query("SELECT customer_id, name FROM customers ORDER BY name", conn)
        if df_cust.empty:
            st.info("尚無客戶資料。")
        else:
            cust_opts = ["全部公司"] + list(df_cust["customer_id"])
            cust_labels = ["全部公司"] + [f"{r['name']} ({r['customer_id']})" for _, r in df_cust.iterrows()]
            sel_idx = st.sidebar.selectbox("選擇公司", range(len(cust_opts)), format_func=lambda i: cust_labels[i], key="viz_cust")
            cid = None if sel_idx == 0 else cust_opts[sel_idx]
            y_str = str(sel_year)
            now = datetime.now()
            cur_month = now.strftime("%Y-%m")

            # 訂單資料：customer_id, order_date, quantity, total_amount，排除已取消
            if cid:
                q_orders = """SELECT o.customer_id, c.name, o.order_date, o.quantity as qty, o.total_amount as amt
                    FROM orders o LEFT JOIN customers c ON o.customer_id=c.customer_id
                    WHERE o.status != '已取消' AND o.customer_id=? """
                df_m = pd.read_sql_query(q_orders, conn, params=(cid,))
            else:
                q_orders = """SELECT o.customer_id, c.name, o.order_date, o.quantity as qty, o.total_amount as amt
                    FROM orders o LEFT JOIN customers c ON o.customer_id=c.customer_id
                    WHERE o.status != '已取消' """
                df_m = pd.read_sql_query(q_orders, conn)

            if not df_m.empty:
                # 在 Python 端處理日期，避開複雜的 SQL strftime 格式問題
                df_m['dt'] = pd.to_datetime(df_m['order_date'], errors='coerce')
                df_m['year'] = df_m['dt'].dt.year
                df_m['ym'] = df_m['dt'].dt.strftime('%Y-%m')
                # 過濾年度
                df_m = df_m[df_m['year'] == sel_year]

            if df_m.empty:
                st.warning(f"{y_str} 年尚無訂單資料（或所選公司無訂單）。")
            else:
                df_cur_month = df_m[df_m["ym"] == cur_month]
                month_qty = df_cur_month["qty"].sum()
                month_amt = df_cur_month["amt"].sum()
                ytd_qty = df_m["qty"].sum()
                ytd_amt = df_m["amt"].sum()

                col1, col2, col3 = st.columns(3)
                col1.metric("本月累積消費量（件）", f"{month_qty:,.0f}")
                col2.metric("當年度累積消費量（件）", f"{ytd_qty:,.0f}")
                col3.metric("當年度總消費金額（NTD）", f"{ytd_amt:,.0f}")

                st.markdown("---")
                st.markdown(f"#### {y_str} 年各月消費趨勢（所選公司）")
                df_plot = df_m.groupby("ym").agg({"qty": "sum", "amt": "sum"}).reset_index().sort_values("ym")

                # 補齊 12 個月
                all_months = [f"{y_str}-{str(m).zfill(2)}" for m in range(1, 13)]
                df_all_months = pd.DataFrame({"ym": all_months})
                df_plot = pd.merge(df_all_months, df_plot, on="ym", how="left").fillna(0)

                tab1, tab2 = st.tabs(["各月消費金額", "各月消費數量"])
                with tab1:
                    fig_amt = px.bar(df_plot, x="ym", y="amt", title=f"{y_str} 年各月消費金額 (NTD)", labels={"ym": "月份", "amt": "金額 (NTD)"})
                    fig_amt.update_layout(xaxis_tickangle=-45)
                    st.plotly_chart(fig_amt, use_container_width=True)
                with tab2:
                    fig_qty = px.bar(df_plot, x="ym", y="qty", title=f"{y_str} 年各月消費數量 (件)", labels={"ym": "月份", "qty": "數量"})
                    fig_qty.update_layout(xaxis_tickangle=-45)
                    st.plotly_chart(fig_qty, use_container_width=True)

                st.markdown("#### 各公司當年度消費金額比較")
                by_cust = df_m.groupby(["customer_id", "name"]).agg({"amt": "sum", "qty": "sum"}).reset_index().sort_values("amt", ascending=False)
                if len(by_cust) > 0:
                    fig_cust = px.bar(by_cust, x="name", y="amt", title=f"{y_str} 年各公司總消費金額 (NTD)", labels={"name": "公司", "amt": "金額 (NTD)"})
                    fig_cust.update_layout(xaxis_tickangle=-45)
                    st.plotly_chart(fig_cust, use_container_width=True)
        conn.close()

    elif sub_menu == "客戶個人消費分析":
        st.markdown("<div class='premium-title'>💹 客戶個人消費分析</div>", unsafe_allow_html=True)

        with sqlite3.connect(DB_FILE) as conn:
            df_cust = pd.read_sql_query("SELECT customer_id, name, contact, phone, email FROM customers ORDER BY name", conn)
            if df_cust.empty:
                st.info("尚無客戶資料。")
            else:
                # ── 搜尋卡片（客戶 + 年份同列）──────────────────────────────
                with st.container(border=True):
                    col_cust, col_year = st.columns([3, 1])
                    with col_cust:
                        cust_labels = [f"{r['name']}　({r['customer_id']})" for _, r in df_cust.iterrows()]
                        sel_label = st.selectbox(
                            "🔍 搜尋客戶（可直接輸入名稱或代號）",
                            options=cust_labels,
                            key="personal_cust",
                        )
                        # 從標籤解析回 customer_id
                        sel_idx = cust_labels.index(sel_label)
                        cid = list(df_cust["customer_id"])[sel_idx]
                    with col_year:
                        sel_year = st.number_input(
                            "📅 分析年度",
                            min_value=2020, max_value=2030,
                            value=datetime.now().year,
                            key="personal_year",
                        )

                row = df_cust[df_cust["customer_id"] == cid].iloc[0]
                st.markdown(f"#### {row['name']}（{row['customer_id']}）")
                st.caption(f"聯絡人：{row['contact'] or '—'}　電話：{row['phone'] or '—'}　Email：{row['email'] or '—'}")

                y_str = str(sel_year)
                cur_month = datetime.now().strftime("%Y-%m")

                # ── 抓取此客戶的所有訂單，用於計算歷年總額與年度趨勢 ──
                df_all_orders = pd.read_sql_query(
                    "SELECT order_date, quantity as qty, total_amount as amt FROM orders WHERE TRIM(customer_id) = TRIM(?) COLLATE NOCASE AND status != '已取消'",
                    conn, params=(cid,)
                )
                
                df_yr = pd.DataFrame()
                if not df_all_orders.empty:
                    df_all_orders['dt'] = pd.to_datetime(df_all_orders['order_date'], errors='coerce')
                    df_all_orders['year'] = df_all_orders['dt'].dt.year
                    df_all_orders['ym'] = df_all_orders['dt'].dt.strftime('%Y-%m')
                    df_yr = df_all_orders[df_all_orders['year'] == sel_year].groupby('ym').agg({'qty':'sum', 'amt':'sum'}).reset_index()

                # 歷年總計（不限年度）
                total_lifetime_qty = df_all_orders["qty"].sum() if not df_all_orders.empty else 0
                total_lifetime_amt = df_all_orders["amt"].sum() if not df_all_orders.empty else 0

                month_qty = month_amt = ytd_qty = ytd_amt = 0
                if not df_yr.empty:
                    df_cur = df_yr[df_yr["ym"] == cur_month]
                    month_qty = df_cur["qty"].sum()
                    month_amt = df_cur["amt"].sum()
                    ytd_qty = df_yr["qty"].sum()
                    ytd_amt = df_yr["amt"].sum()

                # 補齊 12 個月，確保沒有資料的月份顯示 0
                all_months = [f"{y_str}-{str(m).zfill(2)}" for m in range(1, 13)]
                df_all_months = pd.DataFrame({"ym": all_months})
                if not df_yr.empty:
                    df_yr = pd.merge(df_all_months, df_yr, on="ym", how="left").fillna(0)
                else:
                    df_yr = df_all_months.copy()
                    df_yr["qty"] = 0
                    df_yr["amt"] = 0

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("本月累積消費量（件）", f"{month_qty:,.0f}")
                col2.metric("當年度累積消費量（件）", f"{ytd_qty:,.0f}")
                col3.metric("當年度總消費金額（NTD）", f"{ytd_amt:,.0f}")
                col4.metric("歷年總消費金額（NTD）", f"{total_lifetime_amt:,.0f}")

                st.markdown("---")

                # 使用兩欄兩列的方式讓四張圖變小並排顯示
                col_chart1, col_chart2 = st.columns(2)
                col_chart3, col_chart4 = st.columns(2)

                with col_chart1:
                    df_yr = df_yr.sort_values("ym")
                    tab1, tab2 = st.tabs([f"📈 {y_str} 年消費金額", "📦 消費數量"])
                    with tab1:
                        fig_amt = px.line(df_yr, x="ym", y="amt", title=f"{y_str} 年各月消費金額", labels={"ym": "月份", "amt": "金額 (NTD)"}, markers=True)
                        fig_amt.update_layout(xaxis_tickangle=-45, yaxis_title="金額 (NTD)", height=280, margin=dict(l=20, r=20, t=40, b=20))
                        st.plotly_chart(fig_amt, use_container_width=True)
                    with tab2:
                        fig_qty = px.line(df_yr, x="ym", y="qty", title=f"{y_str} 年各月消費數量", labels={"ym": "月份", "qty": "數量"}, markers=True)
                        fig_qty.update_layout(xaxis_tickangle=-45, yaxis_title="數量", height=280, margin=dict(l=20, r=20, t=40, b=20))
                        st.plotly_chart(fig_qty, use_container_width=True)

                with col_chart2:
                    df_prod = pd.read_sql_query(
                        """SELECT i.name as product_name, SUM(o.quantity) as qty, SUM(o.total_amount) as amt
                        FROM orders o LEFT JOIN inventory i ON o.product_id=i.product_id
                        WHERE TRIM(o.customer_id) = TRIM(?) COLLATE NOCASE AND o.status != '已取消'
                        GROUP BY o.product_id, i.name HAVING SUM(o.total_amount)>0 ORDER BY amt DESC""",
                        conn, params=(cid,),
                    )
                    if not df_prod.empty:
                        df_prod["product_name"] = df_prod["product_name"].fillna("未命名")
                        st.markdown("**產品別消費金額占比**")
                        fig_pie = px.pie(df_prod, values="amt", names="product_name")
                        fig_pie.update_traces(domain=dict(x=[0, 0.72], y=[0.0, 0.82]))
                        fig_pie.update_layout(
                            height=270,
                            margin=dict(l=5, r=5, t=35, b=5),
                            showlegend=True,
                            legend=dict(orientation="v", x=0.75, y=0.5),
                        )
                        st.plotly_chart(fig_pie, use_container_width=True)
                    else:
                        st.caption("尚無產品消費資料。")

                with col_chart3:
                    df_status = pd.read_sql_query(
                        """SELECT status as 狀態, COUNT(*) as 筆數, SUM(total_amount) as 金額 FROM orders WHERE TRIM(customer_id) = TRIM(?) COLLATE NOCASE GROUP BY status""",
                        conn, params=(cid,),
                    )
                    if not df_status.empty:
                        st.markdown("**訂單狀態筆數分布**")
                        fig_status = px.pie(df_status, values="筆數", names="狀態")
                        fig_status.update_traces(domain=dict(x=[0, 0.72], y=[0.0, 0.82]))
                        fig_status.update_layout(
                            height=270,
                            margin=dict(l=5, r=5, t=35, b=5),
                            showlegend=True,
                            legend=dict(orientation="v", x=0.75, y=0.5),
                        )
                        st.plotly_chart(fig_status, use_container_width=True)
                    else:
                        st.caption("尚無訂單。")

                with col_chart4:
                    df_by_year = pd.read_sql_query(
                        """SELECT strftime('%Y', order_date) as 年度, SUM(quantity) as qty, SUM(total_amount) as amt
                        FROM orders WHERE TRIM(customer_id) = TRIM(?) COLLATE NOCASE AND status != '已取消' GROUP BY strftime('%Y', order_date) ORDER BY 年度""",
                        conn, params=(cid,),
                    )
                    if not df_by_year.empty:
                        min_yr = int(df_by_year["年度"].min())
                        max_yr = int(df_by_year["年度"].max())
                        all_years = [str(y) for y in range(min_yr, max_yr + 1)]
                        df_all_years = pd.DataFrame({"年度": all_years})
                        df_by_year = pd.merge(df_all_years, df_by_year, on="年度", how="left").fillna(0)

                        fig_yr = px.line(df_by_year, x="年度", y="amt", title="歷年消費金額 (NTD)", markers=True)
                        fig_yr.update_layout(yaxis_title="金額 (NTD)", xaxis_type="category", height=280, margin=dict(l=20, r=20, t=40, b=20))
                        st.plotly_chart(fig_yr, use_container_width=True)
                    else:
                        st.caption("尚無歷年訂單。")
                
                # 先準備訂單總覽資料
                df_orders_list = pd.read_sql_query(
                    """SELECT o.order_date as 訂單日期, o.order_id as 訂單編號, i.name as 產品名稱,
                              o.quantity as 數量, o.total_amount as 金額, o.status as 狀態
                       FROM orders o
                       LEFT JOIN inventory i ON o.product_id=i.product_id
                       WHERE TRIM(o.customer_id) = TRIM(?) COLLATE NOCASE
                       ORDER BY o.order_date DESC""",
                    conn, params=(cid,)
                )

                st.markdown("---")
                st.markdown("#### 🤖 AI 智慧分析")

                if st.button("產生 AI 客戶分析", key="gen_customer_ai_analysis"):
                    try:
                        if not api_key or not api_key.strip():
                            st.warning("請先在系統原本的 API Key 欄位輸入金鑰。")
                        else:
                            top_products_text = "無"
                            if 'df_prod' in locals() and not df_prod.empty:
                                top_n = df_prod.head(5).copy()
                                top_products_text = "\n".join(
                                    [
                                        f"{idx+1}. {r['product_name']}：數量 {float(r['qty']):,.0f}，金額 {float(r['amt']):,.0f} NTD"
                                        for idx, (_, r) in enumerate(top_n.iterrows())
                                    ]
                                )

                            order_status_text = "無"
                            if 'df_status' in locals() and not df_status.empty:
                                order_status_text = "\n".join(
                                    [
                                        f"- {r['狀態']}：{int(r['筆數'])} 筆，金額 {float(r['金額'] or 0):,.0f} NTD"
                                        for _, r in df_status.iterrows()
                                    ]
                                )

                            monthly_trend_text = "無"
                            if not df_yr.empty:
                                monthly_rows = df_yr[["ym", "qty", "amt"]].copy()
                                monthly_trend_text = "\n".join(
                                    [
                                        f"- {r['ym']}：數量 {float(r['qty']):,.0f}，金額 {float(r['amt']):,.0f} NTD"
                                        for _, r in monthly_rows.iterrows()
                                    ]
                                )

                            yearly_trend_text = "無"
                            if 'df_by_year' in locals() and not df_by_year.empty:
                                yearly_trend_text = "\n".join(
                                    [
                                        f"- {r['年度']}：數量 {float(r['qty']):,.0f}，金額 {float(r['amt']):,.0f} NTD"
                                        for _, r in df_by_year.iterrows()
                                    ]
                                )

                            recent_orders_text = "無"
                            if not df_orders_list.empty:
                                recent_n = df_orders_list.head(5).copy()
                                recent_orders_text = "\n".join(
                                    [
                                        f"- {r['訂單日期']}｜{r['訂單編號']}｜{r['產品名稱']}｜數量 {r['數量']}｜金額 {float(r['金額']):,.0f}｜狀態 {r['狀態']}"
                                        for _, r in recent_n.iterrows()
                                    ]
                                )

                            # issue #27：統一 LLM 入口（.env 驅動）
                            from backend.llm_client import complete_text

                            prompt = f"""
你是一位企業 CRM 與銷售分析顧問，請根據以下客戶資料，用繁體中文撰寫「客戶個人消費 AI 智慧分析」。

【客戶基本資料】
- 客戶名稱：{row['name']}
- 客戶代號：{row['customer_id']}
- 聯絡人：{row['contact'] or '—'}
- 電話：{row['phone'] or '—'}
- Email：{row['email'] or '—'}
- 分析年度：{y_str}

【核心指標】
- 本月累積消費量：{month_qty:,.0f} 件
- 本月消費金額：{month_amt:,.0f} NTD
- 當年度累積消費量：{ytd_qty:,.0f} 件
- 當年度總消費金額：{ytd_amt:,.0f} NTD
- 歷年總消費量：{total_lifetime_qty:,.0f} 件
- 歷年總消費金額：{total_lifetime_amt:,.0f} NTD

【年度每月消費趨勢】
{monthly_trend_text}

【歷年消費趨勢】
{yearly_trend_text}

【主要產品消費】
{top_products_text}

【訂單狀態分布】
{order_status_text}

【最近 5 筆訂單】
{recent_orders_text}

請輸出：
1. 客戶消費輪廓摘要
2. 本年度消費趨勢判讀
3. 客戶偏好產品與可能需求
4. 風險提醒
5. 可執行的銷售建議（至少 3 點）
6. 客戶價值分級（高 / 中 / 低）與理由

請用繁體中文、條列、精簡、可直接呈現在 ERP 畫面上。
"""

                            ai_text = (complete_text(prompt, temperature=0.3,
                                                     tag="analysis:crm_customer") or "").strip()

                            if ai_text:
                                st.markdown(ai_text)
                            else:
                                st.info("AI 沒有回傳內容，請稍後再試。")

                    except Exception as e:
                        st.error(f"AI 分析產生失敗：{e}")
                
                st.markdown("---")
                st.markdown("#### 訂單總覽")
                if not df_orders_list.empty:
                    st.dataframe(df_orders_list, use_container_width=True, hide_index=True)
                else:
                    st.caption("尚無訂單紀錄。")


    elif sub_menu == "收款管理":
        st.subheader("收款管理", divider="blue")
        with st.expander("➕ 登記收款"):
            with st.form("add_payment"):
                ref_type = st.selectbox("類型", ["銷售訂單", "其他"])
                ref_id = st.text_input("單據編號 (如訂單號)")
                amount = st.number_input("收款金額", min_value=0.0)
                pay_date = st.text_input("日期", value=datetime.now().strftime("%Y-%m-%d"))
                note = st.text_input("備註")
                if st.form_submit_button("登記") and ref_id and amount > 0:
                    run_query("INSERT INTO payments (ref_type, ref_id, amount, payment_date, note) VALUES (?,?,?,?,?)", (ref_type, ref_id, amount, pay_date, note or None), fetch=False)
                    st.success("已登記收款")
        try:
            df = pd.read_sql_query(
                "SELECT payment_id as 序號, ref_type as 類型, ref_id as 單據, amount as 金額, payment_date as 日期, note as 備註 FROM payments ORDER BY payment_id DESC LIMIT 50",
                sqlite3.connect(DB_FILE),
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception:
            st.info("尚無收款記錄")
