"""
frontend/page_procurement.py
🛒 採購管理（採購單、供應商管理、進貨成本、採購歷史）
"""

import sqlite3
import uuid
import streamlit as st
import pandas as pd
from datetime import datetime
from backend import DB_FILE, run_query
from backend.agent_logger import get_pending_approval_by_id
from backend.tool_gateway import gateway


def ensure_po_operation_id(state) -> str:
    """Keep one operation ID stable across Streamlit reruns for the same draft."""
    operation_id = state.get("po_operation_id")
    if not operation_id:
        operation_id = uuid.uuid4().hex
        state["po_operation_id"] = operation_id
    return operation_id


def start_new_po_operation(state) -> str:
    """Rotate the idempotency key only after the user explicitly starts a new PO."""
    operation_id = uuid.uuid4().hex
    state["po_operation_id"] = operation_id
    state.pop("po_last_approval_id", None)
    return operation_id


def resolve_po_approval_state(
    state, approval_lookup=get_pending_approval_by_id
) -> tuple[str | None, str | None]:
    """Resolve the durable approval state instead of trusting stale UI state."""
    approval_id = state.get("po_last_approval_id")
    if not approval_id:
        return None, None
    approval = approval_lookup(approval_id)
    if not approval:
        return approval_id, "missing"
    return approval_id, approval.get("status")


def render(sub_menu: str):
    st.markdown("<div class='premium-title'>🛒 採購管理</div>", unsafe_allow_html=True)
    menus = ['採購單', '供應商管理', '進貨成本', '採購歷史']
    styled_menus = [f"🌟 **{m}**" if m == sub_menu else f"{m}" for m in menus]
    st.markdown(" ｜ ".join(styled_menus))

    if sub_menu == "採購單":
        st.subheader("採購單", divider="blue")
        with st.expander("➕ 建立採購單"):
            operation_id = ensure_po_operation_id(st.session_state)
            st.caption(f"本次操作識別碼：`{operation_id}`")
            last_approval_id, approval_status = resolve_po_approval_state(
                st.session_state
            )
            if last_approval_id:
                if approval_status == "pending":
                    st.info(
                        f"審批單 `{last_approval_id}` 等待核准；"
                        "採購單尚未建立。"
                    )
                elif approval_status == "approved":
                    st.success(
                        f"審批單 `{last_approval_id}` 已核准並完成採購單建立。"
                    )
                elif approval_status == "rejected":
                    st.warning(
                        f"審批單 `{last_approval_id}` 已被拒絕，未建立採購單。"
                    )
                else:
                    st.error(
                        f"無法讀取審批單 `{last_approval_id}` 的目前狀態。"
                    )
                if st.button("建立下一張採購單", key="start_next_po"):
                    start_new_po_operation(st.session_state)
                    st.rerun()

            if not last_approval_id:
                with st.form("add_po"):
                    po_id = st.text_input("採購單號", value=f"PO-{datetime.now().strftime('%Y%m%d%H%M')}")
                    df_sup = pd.read_sql_query("SELECT supplier_id, name FROM suppliers WHERE is_official = 1", sqlite3.connect(DB_FILE))
                    sup_opts = [f"{r['supplier_id']} - {r['name']}" for _, r in df_sup.iterrows()] if not df_sup.empty else []
                    sup_sel = st.selectbox("正式供應商", sup_opts) if sup_opts else None
                    sup = sup_sel.split(" - ")[0] if sup_sel else st.text_input("供應商代號")

                    df_prod = pd.read_sql_query("SELECT product_id, name FROM inventory", sqlite3.connect(DB_FILE))
                    prod_opts = [f"{r['product_id']} - {r['name']}" for _, r in df_prod.iterrows()] if not df_prod.empty else []
                    prod_sel = st.selectbox("品項", prod_opts) if prod_opts else None
                    prod = prod_sel.split(" - ")[0] if prod_sel else None
                    qty = st.number_input("數量", min_value=1)
                    unit_price = st.number_input("單價", min_value=0.0)
                    note = st.text_input("備註")
                    if st.form_submit_button("建立") and po_id and sup and prod:
                        result = gateway.call(
                            "create_purchase_order",
                            {
                                "po_id": po_id.strip(),
                                "supplier_id": sup,
                                "product_id": prod,
                                "qty": int(qty),
                                "unit_price": float(unit_price),
                                "order_date": datetime.now().strftime("%Y-%m-%d"),
                                "status": "待入庫",
                                "note": note or "",
                            },
                            role=st.session_state.get("role", "guest"),
                            agent_name="procurement_agent",
                            operation_id=operation_id,
                        )
                        if result.status == "pending":
                            st.session_state["po_last_approval_id"] = result.approval_id
                            st.success(
                                f"採購單 {po_id} 已送審（{result.approval_id}）；"
                                "目前尚未寫入 ERP。"
                            )
                        else:
                            st.error(result.message or "採購單送審失敗。")
        try:
            df = pd.read_sql_query(
                """SELECT p.po_id as 採購單號, s.name as 供應商, p.order_date as 日期, p.status as 狀態, p.total_amount as 金額 FROM purchase_orders p 
                LEFT JOIN suppliers s ON p.supplier_id = s.supplier_id ORDER BY p.order_date DESC""",
                sqlite3.connect(DB_FILE),
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception:
            st.info("尚無採購單或請檢查資料表")

    elif sub_menu == "供應商管理":
        st.subheader("供應商管理", divider="blue")
        st.caption("填寫國家、地區與風險等級後，可在「供應鏈與風險 → 供應鏈地圖」自動標註位置。")

        try:
            df = pd.read_sql_query(
                "SELECT supplier_id as 代號, name as 名稱, contact as 聯絡人, phone as 電話, country as 國家, region as 地區, risk_level as 風險, CASE WHEN is_official = 1 THEN '⭐⭐ 正式' ELSE '候補' END as 名單狀態, is_official FROM suppliers ORDER BY is_official DESC, 代號 ASC",
                sqlite3.connect(DB_FILE),
            )
            df_display = df.drop(columns=['is_official']) if 'is_official' in df.columns else df
            st.dataframe(df_display.fillna("—"), use_container_width=True, hide_index=True)
        except Exception:
            df = pd.read_sql_query(
                "SELECT supplier_id as 代號, name as 名稱, contact as 聯絡人, phone as 電話, email as Email FROM suppliers",
                sqlite3.connect(DB_FILE),
            )
            st.dataframe(df, use_container_width=True, hide_index=True)

        with st.expander("📋 風險等級說明與建議", expanded=False):
            st.markdown("""
            **風險等級判定標準（建議依下列原則勾選，避免主觀不一致）：**

            | 等級 | 適用情境 | 範例 |
            |------|----------|------|
            | **高** | 曾發生嚴重延遲／品質爭議、單一供應源且替代困難、所在地區政經不穩或常受天災影響、或「供應鏈與風險 → 風險係數管理」中該地區係數偏高 | 戰亂/高關稅地區、單一關鍵料源、過去一年內有重大延遲 |
            | **中** | 偶有延遲或需較長交期、有備援但切換成本高、地區風險係數中等 | 新供應商、跨洲運輸、部分地區天候不穩 |
            | **低** | 交期穩定、有多源或本地供應、地區風險係數低、長期合作無重大事件 | 國內穩定供應商、成熟地區多源、無重大事件紀錄 |
            """)
            st.caption("下方可依「國家／地區」查詢系統內設定的地區風險係數，作為建議參考；最終等級仍由採購人員依實際狀況勾選。")
            conn = sqlite3.connect(DB_FILE)
            try:
                region_df = pd.read_sql_query(
                    "SELECT risk_key, risk_score, weight FROM esg_risk_factors WHERE risk_type = 'region'",
                    conn,
                )
            except Exception:
                region_df = pd.DataFrame()
            conn.close()
            if not region_df.empty:
                region_df["加權分"] = region_df["risk_score"] * region_df["weight"]
                suggest_region = st.selectbox("查詢地區係數（供風險等級參考）", ["—"] + sorted(region_df["risk_key"].unique().tolist()), key="sup_region_lookup")
                if suggest_region and suggest_region != "—":
                    row = region_df[region_df["risk_key"] == suggest_region].iloc[0]
                    score = row["加權分"]
                    if score >= 70:
                        level, reason = "高", "地區加權分 ≥ 70，建議列為高關注"
                    elif score >= 40:
                        level, reason = "中", "地區加權分 40–69，建議列為一般關注"
                    else:
                        level, reason = "低", "地區加權分 < 40，建議列為低關注"
                    st.info(f"**{suggest_region}** 地區係數加權分：**{score:.0f}** → 建議風險等級：**{level}**（{reason}）。新增供應商時可依此選擇。")
            else:
                st.info("尚未設定地區風險係數。請至「供應鏈與風險 → 風險係數管理」載入預設範本或新增地區（如東亞、日本、越南），即可依係數建議風險等級。")

        with st.expander("➕ 新增供應商", expanded=False):
            with st.form("add_supplier"):
                s_id = st.text_input("供應商代號")
                s_name = st.text_input("公司名稱")
                s_contact = st.text_input("聯絡人")
                s_phone = st.text_input("電話")
                s_email = st.text_input("Email")
                c1, c2 = st.columns(2)
                s_country = c1.text_input("國家（供應鏈地圖）")
                s_region = c2.text_input("地區（如東亞、日本）")
                c3, c4 = st.columns(2)
                # s_lat, s_lon 隱藏，設為 None 讓後端依國家自動帶入
                s_lat, s_lon = None, None
                s_risk = c3.selectbox("風險等級", ["", "低", "中", "高"], format_func=lambda x: x or "—", help="可先於上方「風險等級說明與建議」查詢地區係數再選擇")
                if st.form_submit_button("新增") and s_id and s_name:
                    try:
                        run_query(
                            """INSERT INTO suppliers (supplier_id, name, contact, phone, email, country, region, latitude, longitude, risk_level) 
                            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (s_id, s_name, s_contact or "", s_phone or "", s_email or "", s_country or None, s_region or None, s_lat, s_lon, s_risk or None),
                            fetch=False,
                        )
                        st.success("已新增供應商")
                        st.rerun()
                    except sqlite3.OperationalError:
                        run_query(
                            "INSERT INTO suppliers (supplier_id, name, contact, phone, email) VALUES (?,?,?,?,?)",
                            (s_id, s_name, s_contact or "", s_phone or "", s_email or ""),
                            fetch=False,
                        )
                        st.success("已新增供應商（舊版表結構）")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("代號已存在")

    elif sub_menu == "進貨成本":
        st.subheader("進貨成本", divider="blue")
        try:
            df = pd.read_sql_query(
                "SELECT product_id as 品號, name as 品名, cost as 成本, price as 售價 FROM inventory",
                sqlite3.connect(DB_FILE),
            )
            if not df.empty:
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.info("尚無品項")
        except Exception:
            st.info("請確認 inventory 表含 cost 欄位")

    else:  # 採購歷史
        st.subheader("採購歷史", divider="blue")
        try:
            df = pd.read_sql_query(
                """SELECT poi.po_id as 採購單號, p.order_date as 日期, s.name as 供應商, inv.name as 品名, poi.qty as 數量, poi.unit_price as 單價, (poi.qty*poi.unit_price) as 小計 
                FROM purchase_order_items poi JOIN purchase_orders p ON poi.po_id=p.po_id 
                LEFT JOIN suppliers s ON p.supplier_id=s.supplier_id LEFT JOIN inventory inv ON poi.product_id=inv.product_id ORDER BY p.order_date DESC LIMIT 100""",
                sqlite3.connect(DB_FILE),
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception:
            st.info("尚無採購歷史")
