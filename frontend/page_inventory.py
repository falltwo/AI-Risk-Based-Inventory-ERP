"""
frontend/page_inventory.py
📦 進銷存（商品管理、庫存數量、入庫/出庫、條碼掃描、倉庫管理）
"""

import sqlite3
import streamlit as st
import pandas as pd
from datetime import datetime
from backend import DB_FILE, run_query


def render(sub_menu: str):
    st.markdown("<div class='premium-title'>📦 進銷存</div>", unsafe_allow_html=True)
    menus = ['商品管理', '庫存數量', '入庫/出庫', '條碼掃描', '倉庫管理']
    styled_menus = [f"🌟 **{m}**" if m == sub_menu else f"{m}" for m in menus]
    st.markdown(" ｜ ".join(styled_menus))

    if sub_menu == "商品管理":
        st.subheader("商品管理", divider="blue")
        with st.expander("➕ 新增商品", expanded=False):
            with st.form("add_product_form", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                new_id = c1.text_input("產品編號")
                new_name = c2.text_input("產品名稱")
                new_barcode = c3.text_input("條碼 (選填)")
                c4, c5, c6, c7 = st.columns(4)
                new_price = c4.number_input("售價 (NTD)", min_value=0)
                new_cost = c5.number_input("成本 (NTD)", min_value=0.0, value=0.0)
                new_stock = c6.number_input("初始庫存", min_value=0)
                new_reorder = c7.number_input("安全庫存", min_value=0)
                df_wh = pd.read_sql_query("SELECT warehouse_id, name FROM warehouses", sqlite3.connect(DB_FILE))
                wh_opts = list(df_wh['warehouse_id']) if not df_wh.empty else []
                new_wh = st.selectbox("倉庫", wh_opts) if wh_opts else st.text_input("倉庫代號", "WH01")
                new_daily = st.number_input("預估日均銷量", min_value=0)
                if st.form_submit_button("新增"):
                    if new_id and new_name:
                        try:
                            run_query(
                                """INSERT INTO inventory (product_id, name, stock, price, cost, reorder_point, baseline_reorder_point, daily_sales, barcode, warehouse_id) 
                                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                (new_id, new_name, new_stock, new_price, new_cost or 0, new_reorder, new_reorder, new_daily, new_barcode or None, new_wh),
                                fetch=False,
                            )
                            st.success(f"已新增商品【{new_name}】")
                        except sqlite3.IntegrityError:
                            st.error("產品編號已存在")
                    else:
                        st.warning("請填寫編號與名稱")
        try:
            df = pd.read_sql_query(
                "SELECT product_id as 編號, name as 名稱, barcode as 條碼, stock as 庫存, price as 售價, cost as 成本, reorder_point as 目前水位, baseline_reorder_point as 基準水位, warehouse_id as 倉庫 FROM inventory",
                sqlite3.connect(DB_FILE),
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(str(e))

    elif sub_menu == "庫存數量":
        st.subheader("庫存數量總覽", divider="blue")
        try:
            df = pd.read_sql_query(
                """SELECT i.product_id as 編號, i.name as 名稱, i.stock as 庫存, 
                          i.reorder_point as 安全線, i.baseline_reorder_point as 基準線, i.warehouse_id as 倉庫,
                          CASE 
                            WHEN i.stock <= i.reorder_point THEN '🔴 需補貨' 
                            WHEN i.reorder_point > i.baseline_reorder_point THEN '🤖 AI 調整中'
                            ELSE '🟢 正常' 
                          END as 狀態 
                   FROM inventory i""",
                sqlite3.connect(DB_FILE),
            )
            if not df.empty:
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.info("尚無庫存資料")
        except Exception as e:
            st.error(str(e))

        st.markdown("---")
        with st.expander("💡 智慧補貨與動態安全庫存建議", expanded=False):
            st.caption("系統將根據過去 30 天的「實際銷售訂單」計算日均銷量，並依照「前置天數(7天)」與「緩衝天數(3天)」動態建議您最佳的 **安全庫存水位** 與 **補貨數量**。")
            
            # Smart Restocking 計算邏輯
            days = 30
            lead_time_days = 7
            buffer_days = 3
            
            conn = sqlite3.connect(DB_FILE)
            date_limit = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            df_sales = pd.read_sql_query(
                "SELECT product_id, SUM(quantity) as total_qty FROM orders WHERE status != '已取消' AND order_date >= ? GROUP BY product_id",
                conn, params=(date_limit,)
            )
            df_inv_full = pd.read_sql_query("SELECT product_id, name, stock, reorder_point, baseline_reorder_point, daily_sales, price FROM inventory", conn)
            
            if not df_inv_full.empty:
                # 合併銷量
                df_smart = pd.merge(df_inv_full, df_sales, on="product_id", how="left").fillna({"total_qty": 0})
                
                # 計算
                df_smart["日均銷量 (目前)"] = df_smart["total_qty"].apply(lambda x: round(x / days if x > 0 else 0.1, 1))
                df_smart["建議安全庫存"] = df_smart["日均銷量 (目前)"].apply(lambda x: max(int(x * (lead_time_days + buffer_days)), 1))
                
                def calc_po_qty(row):
                    if row["stock"] <= row["建議安全庫存"]:
                        qty = row["建議安全庫存"] - row["stock"] + int(row["日均銷量 (目前)"] * 7) # 多補一週的量
                        return max(qty, 1)
                    return 0
                
                df_smart["建議進貨量"] = df_smart.apply(calc_po_qty, axis=1)
                
                # 準備顯示用的表格
                df_display = df_smart[["product_id", "name", "stock", "reorder_point", "建議安全庫存", "日均銷量 (目前)", "建議進貨量"]].copy()
                df_display.columns = ["產品編號", "產品名稱", "目前庫存", "原安全庫存", "⭐建議安全庫存", "近30天日均銷", "🛒建議進貨量"]
                
                st.dataframe(df_display, use_container_width=True, hide_index=True)
                
                col_btn1, col_btn2 = st.columns(2)
                with col_btn1:
                    if st.button("🔄 套用所有建議的安全庫存水位", use_container_width=True):
                        try:
                            c = conn.cursor()
                            for _, row in df_smart.iterrows():
                                c.execute("UPDATE inventory SET reorder_point=?, baseline_reorder_point=?, daily_sales=? WHERE product_id=?", 
                                         (row["建議安全庫存"], row["建議安全庫存"], row["日均銷量 (目前)"], row["product_id"]))
                            conn.commit()
                            st.success("已成功將所有產品的安全庫存與預估日均銷量更新至最新狀態！請重新整理畫面。")
                        except Exception as e:
                            st.error(f"更新失敗: {e}")
                            
                with col_btn2:
                    if st.button("🛒 一鍵產生建議採購單", use_container_width=True):
                        df_buy = df_smart[df_smart["建議進貨量"] > 0]
                        if df_buy.empty:
                            st.info("目前所有庫存皆高於建議安全水位，無須採購。")
                        else:
                            try:
                                c = conn.cursor()
                                # 取得一間預設供應商
                                c.execute("SELECT supplier_id FROM suppliers LIMIT 1")
                                sup_res = c.fetchone()
                                default_sup = sup_res[0] if sup_res else "SUP_UNKNOWN"
                                
                                new_po_id = f"PO-AI-{datetime.now().strftime('%Y%m%d%H%M')}"
                                total_amt = 0
                                
                                # 寫入明細 & 計算總額
                                for _, row in df_buy.iterrows():
                                    unit_price = row["price"] * 0.7 # 假設進貨成本是定價 7 折 (mock)
                                    qty = row["建議進貨量"]
                                    total_amt += unit_price * qty
                                    c.execute(
                                        "INSERT INTO purchase_order_items (po_id, product_id, qty, unit_price) VALUES (?,?,?,?)",
                                        (new_po_id, row["product_id"], qty, unit_price)
                                    )
                                
                                # 寫入主單
                                c.execute(
                                    "INSERT INTO purchase_orders (po_id, supplier_id, order_date, status, total_amount, note) VALUES (?,?,?,?,?,?)",
                                    (new_po_id, default_sup, datetime.now().strftime("%Y-%m-%d"), "草稿", total_amt, "AI 智慧自動生成補貨單")
                                )
                                conn.commit()
                                st.success(f"已成功為 {len(df_buy)} 項產品建立採購單「{new_po_id}」（狀態：草稿），請至「採購管理」查看！")
                            except Exception as e:
                                st.error(f"建立採購單失敗: {e}")
            else:
                st.info("尚無產品或訂單資料可供分析。")
            conn.close()

    elif sub_menu == "入庫/出庫":
        st.subheader("入庫 / 出庫", divider="blue")
        with st.form("stock_move_form", clear_on_submit=True):
            df_inv = pd.read_sql_query("SELECT product_id, name FROM inventory", sqlite3.connect(DB_FILE))
            opts = {r['product_id']: f"{r['product_id']} - {r['name']}" for _, r in df_inv.iterrows()} if not df_inv.empty else {}
            product = st.selectbox("商品", list(opts.keys()), format_func=lambda x: opts.get(x, x)) if opts else None
            move_type = st.radio("類型", ["入庫", "出庫"])
            qty = st.number_input("數量", min_value=1)
            ref_no = st.text_input("單據編號 (選填)")
            note = st.text_input("備註")
            if st.form_submit_button("確認") and product:
                q = qty if move_type == "入庫" else -qty
                res = run_query("SELECT stock, name FROM inventory WHERE product_id=?", (product,))
                if res:
                    current, name = res[0]
                    if current + q < 0:
                        st.error("庫存不足")
                    else:
                        run_query("UPDATE inventory SET stock=? WHERE product_id=?", (current + q, product), fetch=False)
                        run_query(
                            "INSERT INTO stock_moves (product_id, warehouse_id, qty, move_type, ref_no, move_date, note) VALUES (?, (SELECT warehouse_id FROM inventory WHERE product_id=? LIMIT 1), ?, ?, ?, ?, ?)",
                            (product, product, q, move_type, ref_no or None, datetime.now().strftime("%Y-%m-%d %H:%M"), note or None),
                            fetch=False,
                        )
                        st.success(f"已{move_type} {qty} 件 {name}，目前庫存 {current + q}")
        st.markdown("#### 最近異動")
        try:
            df = pd.read_sql_query(
                "SELECT move_id as 序號, product_id as 商品, qty as 數量, move_type as 類型, ref_no as 單據, move_date as 日期, note as 備註 FROM stock_moves ORDER BY move_id DESC LIMIT 30",
                sqlite3.connect(DB_FILE),
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception:
            pass

    elif sub_menu == "條碼掃描":
        st.subheader("條碼掃描查詢", divider="blue")
        barcode_input = st.text_input("掃描或輸入條碼", placeholder="請掃描條碼或手動輸入")
        if barcode_input:
            res = run_query(
                "SELECT product_id, name, stock, price FROM inventory WHERE barcode=? OR product_id=?",
                (barcode_input.strip(), barcode_input.strip()),
            )
            if res:
                pid, name, stock, price = res[0]
                st.success(f"**{name}** ({pid}) · 庫存：{stock} · 售價：${price:,}")
            else:
                st.warning("找不到此條碼對應的商品")

    else:  # 倉庫管理
        st.subheader("倉庫管理", divider="blue")
        with st.expander("➕ 新增倉庫"):
            with st.form("add_warehouse"):
                wh_id = st.text_input("倉庫代號")
                wh_name = st.text_input("倉庫名稱")
                wh_addr = st.text_input("地址")
                if st.form_submit_button("新增") and wh_id and wh_name:
                    try:
                        run_query("INSERT INTO warehouses VALUES (?,?,?)", (wh_id, wh_name, wh_addr or ""), fetch=False)
                        st.success("已新增倉庫")
                    except sqlite3.IntegrityError:
                        st.error("代號已存在")
        try:
            df = pd.read_sql_query(
                "SELECT warehouse_id as 代號, name as 名稱, address as 地址 FROM warehouses",
                sqlite3.connect(DB_FILE),
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(str(e))
