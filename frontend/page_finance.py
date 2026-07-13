"""
frontend/page_finance.py
📒 財務會計（應收/應付、總帳、成本分析、財報）
"""

import sqlite3
import streamlit as st
import pandas as pd
from datetime import datetime
from backend import DB_FILE, run_query


def render(sub_menu: str):
    st.markdown("<div class='premium-title'>📒 財務會計</div>", unsafe_allow_html=True)
    menus = ['應收/應付', '總帳', '成本分析', '財報']
    styled_menus = [f"🌟 **{m}**" if m == sub_menu else f"{m}" for m in menus]
    st.markdown(" ｜ ".join(styled_menus))

    if sub_menu == "應收/應付":
        st.subheader("應收 / 應付", divider="blue")
        tab1, tab2, tab3 = st.tabs(["應收帳款", "應付帳款", "簡易總覽"])
        with tab1:
            try:
                df = pd.read_sql_query(
                    """SELECT o.order_id as 單據, c.name as 客戶, o.total_amount as 應收, o.status as 狀態 FROM orders o 
                    LEFT JOIN customers c ON o.customer_id=c.customer_id WHERE o.status NOT IN ('已取消') ORDER BY o.order_date DESC""",
                    sqlite3.connect(DB_FILE),
                )
                if not df.empty:
                    st.metric("應收總額 (訂單)", f"${df['應收'].sum():,.0f}" if '應收' in df.columns else 0)
                    st.dataframe(df, use_container_width=True, hide_index=True)
                else:
                    st.info("尚無應收資料")
            except Exception:
                st.info("尚無訂單或欄位不符")
        with tab2:
            try:
                df = pd.read_sql_query(
                    """SELECT p.po_id as 單據, s.name as 供應商, p.total_amount as 應付, p.status as 狀態 FROM purchase_orders p 
                    LEFT JOIN suppliers s ON p.supplier_id=s.supplier_id""",
                    sqlite3.connect(DB_FILE),
                )
                if not df.empty:
                    st.metric("應付總額 (採購單)", f"${df['應付'].sum():,.0f}" if '應付' in df.columns else 0)
                    st.dataframe(df, use_container_width=True, hide_index=True)
                else:
                    st.info("尚無應付資料")
            except Exception:
                st.info("尚無採購單")
        with tab3:
            try:
                rec = run_query("SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE status NOT IN ('已取消','已出貨')")
                pay = run_query("SELECT COALESCE(SUM(total_amount),0) FROM purchase_orders WHERE status = '待入庫'")
                st.metric("應收 (未出貨訂單)", f"${rec[0][0]:,.0f}" if rec else "$0")
                st.metric("應付 (待入庫採購)", f"${pay[0][0]:,.0f}" if pay else "$0")
            except Exception:
                pass

    elif sub_menu == "總帳":
        st.subheader("總帳", divider="blue")
        with st.expander("➕ 新增總帳分錄"):
            with st.form("add_ledger"):
                ld = st.text_input("日期", value=datetime.now().strftime("%Y-%m-%d"))
                acc = st.text_input("會計科目")
                deb = st.number_input("借方", min_value=0.0, value=0.0)
                cred = st.number_input("貸方", min_value=0.0, value=0.0)
                desc = st.text_input("說明")
                if st.form_submit_button("新增") and acc and (deb > 0 or cred > 0):
                    run_query("INSERT INTO general_ledger (ledger_date, account, debit, credit, description) VALUES (?,?,?,?,?)", (ld, acc, deb, cred, desc or ""), fetch=False)
                    st.success("已新增分錄")
        try:
            df = pd.read_sql_query(
                "SELECT id as 序號, ledger_date as 日期, account as 科目, debit as 借方, credit as 貸方, description as 說明 FROM general_ledger ORDER BY id DESC LIMIT 100",
                sqlite3.connect(DB_FILE),
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception:
            st.info("尚無總帳資料")

    elif sub_menu == "成本分析":
        st.subheader("成本分析", divider="blue")
        try:
            df = pd.read_sql_query(
                """SELECT product_id as 品號, name as 品名, cost as 成本, price as 售價, 
                (price - cost) as 毛利, ROUND((price - cost)*100.0/NULLIF(price,0),1) as 毛利率 FROM inventory""",
                sqlite3.connect(DB_FILE),
            )
            if not df.empty:
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.info("尚無品項")
        except Exception:
            df = pd.read_sql_query("SELECT product_id as 品號, name as 品名, cost as 成本, price as 售價 FROM inventory", sqlite3.connect(DB_FILE))
            st.dataframe(df, use_container_width=True, hide_index=True)

    else:  # 財報
        st.subheader("財報摘要", divider="blue")
        try:
            inv_val = run_query("SELECT SUM(stock * COALESCE(cost, 0)) FROM inventory")
            inv_val = (inv_val[0][0] or 0) if inv_val else 0
            sales = run_query("SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE status = '已出貨'")
            sales = sales[0][0] if sales else 0
            st.metric("庫存成本總額", f"${inv_val:,.0f}")
            st.metric("已出貨銷售額", f"${sales:,.0f}")
        except Exception:
            pass
