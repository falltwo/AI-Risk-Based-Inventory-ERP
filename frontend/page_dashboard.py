"""
frontend/page_dashboard.py
📊 營運分析看板
"""

import sqlite3
import streamlit as st
from frontend.ui_utils import show_error
import pandas as pd
from backend import DB_FILE, run_query


def render():
    st.markdown("<div class='premium-title'>📊 營運分析看板</div>", unsafe_allow_html=True)
    st.markdown("<p style='color: #64748b; font-size: 1.1rem; margin-bottom: 2rem;'>快速瀏覽企業當前營運狀況與核心指標。</p>", unsafe_allow_html=True)

    # 頂部四大指標 Metrics
    col1, col2, col3, col4 = st.columns(4)

    # 庫存總價值（以成本計算）
    inv_val = run_query("SELECT SUM(stock * COALESCE(cost, 0)) FROM inventory")
    inv_val = (inv_val[0][0] or 0) if inv_val else 0

    # 本月營收與訂單數（排除已取消訂單）
    this_month_revenue_row = run_query(
        "SELECT COALESCE(SUM(total_amount), 0) FROM orders "
        "WHERE status != '已取消' AND strftime('%Y-%m', order_date) = strftime('%Y-%m','now')"
    )
    this_month_revenue = this_month_revenue_row[0][0] if this_month_revenue_row else 0

    this_month_orders_row = run_query(
        "SELECT COUNT(*) FROM orders "
        "WHERE status != '已取消' AND strftime('%Y-%m', order_date) = strftime('%Y-%m','now')"
    )
    this_month_orders = this_month_orders_row[0][0] if this_month_orders_row else 0

    # 低庫存商品數（庫存小於等於安全庫存）
    low_stock_row = run_query(
        "SELECT COUNT(*) FROM inventory WHERE reorder_point IS NOT NULL AND reorder_point > 0 AND stock <= reorder_point"
    )
    low_stock_count = low_stock_row[0][0] if low_stock_row else 0

    col1.metric("💰 本月營收 (NTD)", f"${this_month_revenue:,.0f}")
    col2.metric("🧾 本月訂單數", f"{this_month_orders} 筆")
    col3.metric("📦 庫存總價值 (NTD)", f"${inv_val:,.0f}")
    
    # ── 可點擊的低庫存 KPI (精準匹配原生 Metric 尺寸) ──
    with col4:
        st.markdown(f"""
        <style>
        /* 針對 popover 本身的按鈕進行深度美化，匹配原生 Metric */
        div[data-testid="stPopover"] > button {{
            background-color: white !important;
            border: 1px solid rgb(230, 235, 245) !important;
            border-radius: 8px !important;
            height: 88px !important; /* 標準 Metric 高度 */
            width: 100% !important;
            padding: 12px 14px !important;
            box-shadow: none !important;
            text-align: left !important;
            display: flex !important;
            flex-direction: column !important;
            justify-content: center !important;
            align-items: flex-start !important;
            white-space: pre-wrap !important;
        }}
        /* 隱藏原生箭頭 */
        div[data-testid="stPopover"] > button svg {{ 
            display: none !important; 
        }}
        /* 讓按鈕內的內容佔滿寬度並對齊 */
        div[data-testid="stPopover"] > button > div {{
            width: 100%;
            text-align: left !important;
        }}
        div[data-testid="stPopover"] div[data-testid="stMarkdownContainer"] p {{
            margin: 0 !important;
            line-height: 1.25 !important;
        }}
        /* 仿製 Metric 標籤樣式 */
        div[data-testid="stPopover"] div[data-testid="stMarkdownContainer"] p:first-child {{
            color: #64748b !important;
            font-size: 14px !important;
            font-weight: 400 !important;
            margin-bottom: 2px !important;
        }}
        /* 仿製 Metric 數值樣式 */
        div[data-testid="stPopover"] div[data-testid="stMarkdownContainer"] p:last-child {{
            color: #31333F !important;
            font-size: 24px !important;
            font-weight: 600 !important;
        }}
        </style>
        """, unsafe_allow_html=True)
        
        # 標籤使用雙換行以產生獨立的 p 標籤供 CSS 選擇器使用
        pop_label = f"⚠️ 低庫存商品數\n\n{low_stock_count} 品項"
        
        with st.popover(pop_label, use_container_width=True):
             st.markdown("#### 🚨 低庫存商品清單")
             try:
                conn_tmp = sqlite3.connect(DB_FILE)
                df_low_items = pd.read_sql_query(
                    "SELECT product_id as 編號, name as 名稱, stock as 目前庫存, reorder_point as 安全線 FROM inventory WHERE reorder_point IS NOT NULL AND reorder_point > 0 AND stock <= reorder_point",
                    conn_tmp
                )
                conn_tmp.close()
                if not df_low_items.empty:
                    st.dataframe(df_low_items, use_container_width=True, hide_index=True)
                    st.info("💡 建議盡快安排採購以維持庫存水位。")
                else:
                    st.success("🎉 目前無任何庫存警報！")
             except Exception as e:
                show_error("資料讀取失敗", e)

    st.markdown("---")
    col_chart1, col_chart2 = st.columns(2)

    # 圖表 1: 庫存健康度
    with col_chart1:
        st.markdown("### 📦 庫存水位 vs 安全庫存")
        try:
            import plotly.graph_objects as go
            df_inv = pd.read_sql_query("SELECT product_id, name, stock, reorder_point FROM inventory ORDER BY product_id", sqlite3.connect(DB_FILE))
            if not df_inv.empty:
                # 建立 [編號] 名稱 的組合標籤
                df_inv['display_name'] = df_inv['product_id'] + " - " + df_inv['name']
                
                fig = go.Figure()
                # 現有庫存：一般藍色長條
                fig.add_trace(go.Bar(
                    x=df_inv['display_name'],
                    y=df_inv['stock'],
                    name='現有庫存',
                    marker_color='#3B82F6'
                ))
                # 安全庫存：紅色虛線
                fig.add_trace(go.Scatter(
                    x=df_inv['display_name'],
                    y=df_inv['reorder_point'],
                    name='安全庫存 (警戒線)',
                    mode='lines+markers',
                    line=dict(color='#EF4444', width=3, dash='dash'),
                    marker=dict(size=8)
                ))
                
                fig.update_layout(
                    margin=dict(l=20, r=20, t=20, b=20),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    hovermode="x unified",
                    height=350
                )
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("尚無庫存資料")
        except Exception as e:
            show_error("無法載入圖表", e)

    # 圖表 2: 訂單狀態
    with col_chart2:
        st.markdown("### 🧾 訂單執行狀態分佈")
        try:
            df_ord = pd.read_sql_query("SELECT status, COUNT(*) as count FROM orders GROUP BY status", sqlite3.connect(DB_FILE))
            if not df_ord.empty:
                # 把 status 當作 index 繪製長條圖
                st.bar_chart(df_ord.set_index('status'), color="#3B82F6")
        except Exception as e:
            show_error("無法載入圖表", e)

    st.markdown("---")

    # 圖表 3 & 4: 商品銷售排行 / 每月營收趨勢
    col_chart3, col_chart4 = st.columns(2)

    # 圖表 3: 商品銷售排行 (限當月，按銷售數量)
    with col_chart3:
        st.markdown("### 🏆 本月商品銷量排行")
        try:
            import plotly.express as px
            conn = sqlite3.connect(DB_FILE)
            df_rank = pd.read_sql_query(
                """
                SELECT i.name AS 商品, 
                       SUM(o.quantity) AS 銷售數量, 
                       SUM(o.total_amount) AS 銷售金額
                FROM orders o
                LEFT JOIN inventory i ON o.product_id = i.product_id
                WHERE o.status != '已取消' 
                  AND strftime('%Y-%m', o.order_date) = strftime('%Y-%m', 'now')
                GROUP BY o.product_id, i.name
                ORDER BY 銷售數量 DESC
                LIMIT 10
                """,
                conn,
            )
            conn.close()
            if not df_rank.empty:
                # 使用 px.bar 並按銷量排序
                fig_rank = px.bar(
                    df_rank,
                    x="商品",
                    y="銷售數量",
                    hover_data=["銷售金額"],
                    color_discrete_sequence=["#10B981"]
                )
                fig_rank.update_layout(
                    margin=dict(l=20, r=20, t=20, b=20),
                    height=350,
                    xaxis={'categoryorder':'total descending'}
                )
                st.plotly_chart(fig_rank, use_container_width=True)
            else:
                st.info("目前還沒有銷售資料。")
        except Exception as e:
            show_error("無法載入圖表", e)

    # 圖表 4: 每月營收趨勢圖
    with col_chart4:
        st.markdown("### 📈 每月營收趨勢")
        try:
            conn = sqlite3.connect(DB_FILE)
            df_rev = pd.read_sql_query(
                """
                SELECT strftime('%Y-%m', order_date) AS 月份,
                       SUM(total_amount) AS 營收
                FROM orders
                WHERE status != '已取消'
                GROUP BY strftime('%Y-%m', order_date)
                ORDER BY 月份
                """,
                conn,
            )
            conn.close()
            if not df_rev.empty:
                df_rev = df_rev.set_index("月份")
                st.line_chart(df_rev["營收"], color="#6366F1")
            else:
                st.info("目前還沒有營收資料。")
        except Exception as e:
            show_error("無法載入圖表", e)
