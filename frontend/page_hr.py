"""
frontend/page_hr.py
👥 人資管理（員工資料、薪資、出勤）
"""

import sqlite3
import streamlit as st
import pandas as pd
from datetime import datetime
from backend import DB_FILE, run_query


def render(sub_menu: str):
    st.markdown("<div class='premium-title'>👥 人資管理</div>", unsafe_allow_html=True)
    menus = ['員工資料', '薪資', '出勤']
    styled_menus = [f"🌟 **{m}**" if m == sub_menu else f"{m}" for m in menus]
    st.markdown(" ｜ ".join(styled_menus))

    if sub_menu == "員工資料":
        st.subheader("員工資料", divider="blue")
        with st.expander("➕ 新進員工入職"):
            with st.form("add_hr_form", clear_on_submit=True):
                c1, c2 = st.columns(2)
                e_id = c1.text_input("員工編號")
                e_name = c2.text_input("姓名")
                c3, c4, c5 = st.columns(3)
                e_dept = c3.text_input("部門")
                e_role = c4.text_input("職位")
                e_sal = c5.number_input("月薪", min_value=0, value=0)
                if st.form_submit_button("新增"):
                    if e_id and e_name:
                        try:
                            run_query("INSERT INTO hr (employee_id, name, department, role, salary) VALUES (?,?,?,?,?)", (e_id, e_name, e_dept or "", e_role or "", e_sal or 0), fetch=False)
                            st.success(f"已為 {e_name} 建檔")
                        except sqlite3.IntegrityError:
                            st.error("員工編號已存在")
                    else:
                        st.warning("請填寫編號與姓名")
        try:
            df_hr = pd.read_sql_query("SELECT employee_id as 編號, name as 姓名, department as 部門, role as 職位, salary as 月薪 FROM hr", sqlite3.connect(DB_FILE))
            st.dataframe(df_hr, use_container_width=True, hide_index=True)
            
            # ── 刪除員工功能 ──
            if not df_hr.empty:
                with st.expander("🗑️ 員工離職/刪除"):
                    st.warning("注意：刪除員工將同步移除該員工的所有「薪資紀錄」與「出勤紀錄」，且無法復原。")
                    emp_list = df_hr['編號'].tolist()
                    emp_names = df_hr['姓名'].tolist()
                    emp_options = [f"{emp_list[i]} - {emp_names[i]}" for i in range(len(emp_list))]
                    
                    target_to_del = st.selectbox("選擇要刪除的員工", emp_options, key="del_emp_select")
                    target_id = target_to_del.split(" - ")[0]
                    
                    if st.button("確認刪除", type="primary", use_container_width=True):
                        try:
                            # 連動刪除薪資與出勤
                            run_query("DELETE FROM payroll WHERE employee_id = ?", (target_id,), fetch=False)
                            run_query("DELETE FROM attendance WHERE employee_id = ?", (target_id,), fetch=False)
                            run_query("DELETE FROM hr WHERE employee_id = ?", (target_id,), fetch=False)
                            st.success(f"員工 {target_to_del} 資料已全數移除")
                            st.rerun()
                        except Exception as e:
                            st.error(f"刪除失敗: {e}")
        except Exception:
            df_hr = pd.read_sql_query("SELECT employee_id as 編號, name as 姓名, department as 部門, role as 職位 FROM hr", sqlite3.connect(DB_FILE))
            st.dataframe(df_hr, use_container_width=True, hide_index=True)

    elif sub_menu == "薪資":
        st.subheader("薪資", divider="blue")
        with st.expander("➕ 登錄薪資", expanded=True):
            df_e = pd.read_sql_query("SELECT employee_id, name, salary FROM hr", sqlite3.connect(DB_FILE))
            if not df_e.empty:
                emp_opts = list(df_e['employee_id'])
                emp_labels = [f"{r['name']} ({r['employee_id']})" for _, r in df_e.iterrows()]
                
                # 將員工選單放在 form 外面，這樣切換員工時才能即時更新底下的預設薪資
                sel = st.selectbox("選擇員工", range(len(emp_opts)), format_func=lambda i: emp_labels[i])
                emp = emp_opts[sel]
                
                # 撈取目前選擇員工的預設本薪
                expected_salary = df_e.iloc[sel]['salary']
                if pd.isna(expected_salary):
                    expected_salary = 0.0
                
                with st.form("add_payroll"):
                    period = st.text_input("薪資月份", value=datetime.now().strftime("%Y-%m"))
                    base = st.number_input("本薪", min_value=0.0, value=float(expected_salary))
                    bonus = st.number_input("獎金", min_value=0.0, value=0.0)
                    deduction = st.number_input("扣款", min_value=0.0, value=0.0)
                    
                    if st.form_submit_button("登錄"):
                        run_query(
                            "INSERT INTO payroll (employee_id, period, base_salary, bonus, deduction) VALUES (?,?,?,?,?)",
                            (emp, period, base, bonus, deduction), fetch=False
                        )
                        st.success(f"已登錄 {emp_labels[sel]} 的 {period} 薪資")
            else:
                st.info("尚無員工資料，請先至「員工資料」建檔。")
        try:
            df = pd.read_sql_query(
                """SELECT p.period as 月份, p.employee_id as 員工, h.name as 姓名, p.base_salary as 本薪, p.bonus as 獎金, p.deduction as 扣款, (p.base_salary+p.bonus-p.deduction) as 實領 
                FROM payroll p LEFT JOIN hr h ON p.employee_id=h.employee_id ORDER BY p.period DESC""",
                sqlite3.connect(DB_FILE),
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception:
            st.info("尚無薪資資料")

    elif sub_menu == "出勤":
        st.subheader("出勤", divider="blue")
        with st.expander("➕ 登記出勤"):
            with st.form("add_attendance"):
                df_e = pd.read_sql_query("SELECT employee_id, name FROM hr", sqlite3.connect(DB_FILE))
                emp_opts = list(df_e['employee_id']) if not df_e.empty else []
                emp = st.selectbox("員工", emp_opts) if emp_opts else None
                work_date = st.text_input("日期", value=datetime.now().strftime("%Y-%m-%d"))
                check_in = st.text_input("上班", value="09:00")
                check_out = st.text_input("下班", value="18:00")
                status = st.selectbox("狀態", ["正常", "請假", "曠職", "加班"])
                if st.form_submit_button("登錄") and emp:
                    run_query("INSERT INTO attendance (employee_id, work_date, check_in, check_out, status) VALUES (?,?,?,?,?)", (emp, work_date, check_in, check_out, status), fetch=False)
                    st.success("已登錄出勤")
        try:
            df = pd.read_sql_query(
                "SELECT work_date as 日期, employee_id as 員工, check_in as 上班, check_out as 下班, status as 狀態 FROM attendance ORDER BY work_date DESC LIMIT 50",
                sqlite3.connect(DB_FILE),
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception:
            st.info("尚無出勤資料")


