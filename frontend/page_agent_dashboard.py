"""
frontend/page_agent_dashboard.py
🕵️ Agent Dashboard (座席管理界面)
職責：展示 8 個專責 Agent 狀態、總管派工紀錄、工具呼叫記錄、以及待審批清單 (串接真實資料庫 + 審批與重試操作)
"""

import streamlit as st
import pandas as pd
import json
from datetime import datetime
from backend.agent_registry import AGENTS, get_tools_for_agent, get_agent_for_tool
from backend.agent_logger import (
    get_pending_list,
    get_action_logs,
    approve_action,
    reject_action,
    get_pending_approvals,
    write_action_log,
)
from backend.database import run_query


def format_parameters_to_chinese(tool_name: str, args) -> str:
    """將工具呼叫參數轉換為易讀的中文說明"""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            pass
            
    if not isinstance(args, dict):
        return str(args)
        
    if not args:
        return "無參數"
        
    # 特殊處理常用工具以呈現自然流暢的中文
    if tool_name == "update_inventory":
        pid = args.get("product_id", "")
        qty = args.get("quantity_change")
        if qty is not None:
            try:
                qty_val = float(qty)
                if qty_val.is_integer():
                    qty_val = int(qty_val)
                if qty_val > 0:
                    return f"{pid} 進貨 {qty_val} 件"
                elif qty_val < 0:
                    return f"{pid} 出貨 {abs(qty_val)} 件"
                else:
                    return f"{pid} 庫存異動量為 0"
            except (ValueError, TypeError):
                return f"{pid} 庫存異動 {qty}"
        return f"商品 ID: {pid}"
        
    elif tool_name == "create_order":
        pid = args.get("product_id", "")
        cust_id = args.get("customer_id", "")
        qty = args.get("quantity")
        cust_str = f" (客戶: {cust_id})" if cust_id else ""
        if qty is not None:
            return f"{pid} 銷售 {qty} 件{cust_str}"
        return f"商品 ID: {pid}{cust_str}"

    elif tool_name == "create_purchase_order":
        po_id = args.get("po_id", "")
        supplier_id = args.get("supplier_id", "")
        product_id = args.get("product_id", "")
        qty = args.get("qty", "")
        unit_price = args.get("unit_price")
        try:
            price_text = f"｜單價 NT${float(unit_price):,.2f}"
        except (TypeError, ValueError):
            price_text = f"｜單價 {unit_price}" if unit_price not in (None, "") else ""
        return (
            f"採購單 {po_id}｜供應商 {supplier_id}｜"
            f"商品 {product_id} × {qty}{price_text}"
        )
        
    # 其他工具的參數 key 對照表
    key_mapping = {
        "product_id": "商品 ID",
        "quantity_change": "庫存異動量",
        "customer_id": "客戶 ID",
        "quantity": "數量",
        "reason": "原因",
        "supplier_id": "供應商 ID",
        "employee_id": "員工 ID",
        "month": "月份",
        "year": "年份",
        "formula": "計算公式",
    }
    
    parts = []
    for k, v in args.items():
        k_zh = key_mapping.get(k, k)
        parts.append(f"{k_zh}: {v}")
    return ", ".join(parts)


def render():
    st.markdown("<div class='premium-title'>🕵️ Agent Dashboard</div>", unsafe_allow_html=True)
    st.markdown("<p style='color: #64748b; font-size: 1.1rem; margin-bottom: 2rem;'>即時監控專責 AI Agent 的運行狀態、總管派工決策、工具呼叫記錄與敏感操作的審批管理。</p>", unsafe_allow_html=True)

    # ── 自動初始化示範用之派工紀錄與審批資料 (初次載入無資料時) ─────────────────
    _initialize_demo_data_if_empty()

    # ── 從資料庫取得最新審批資料 ──────────────────────────────
    pending_list = get_pending_list()
    pending_count = len(pending_list)

    # 讀取歷史審批紀錄
    all_approvals = get_pending_approvals()
    approval_history = []
    for app in all_approvals:
        if app["status"] != "pending":
            agent_id = get_agent_for_tool(app["tool_name"]) or "unknown_agent"
            approval_history.append({
                "id": app["approval_id"],
                "time": app["created_at"],
                "agent": agent_id,
                "tool": app["tool_name"],
                "args": str(app["parameters"]),
                "raw_args": app["parameters"],
                "role": app["requester"],
                "status": app["status"],
                "reason": app["reason"] or "",
                "processed_time": app["updated_at"],
                "operation_id": app.get("operation_id"),
            })

    # ── 頂部 Metrics 卡片 ───────────────────────────────────────────────
    total_agents = len(AGENTS)
    active_agents = 3  # 模擬運作中

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("🤖 登記 Agent 總數", f"{total_agents} 個")
    col2.metric("⚡ 運行中 Agent", f"{active_agents} 個", delta="Normal")
    col3.metric("⏳ 待審批請求", f"{pending_count} 筆", delta=f"{pending_count} 待處理", delta_color="inverse" if pending_count > 0 else "normal")
    col4.metric("📊 歷史審批總數", f"{len(approval_history)} 筆", delta="無異常")

    st.markdown("---")

    tab_monitor, tab_metrics = st.tabs(["🔮 運作監控與審批", "🛡️ 治理效益分析"])

    with tab_monitor:
        # ── 1) 代理狀態 (Agent Status) ──────────────────────────────────────
        st.markdown("### 🤖 代理狀態 (Agent Status)")
        st.markdown("檢視系統內置的所有 AI Agent 及其被授權的工具模組與權限。")

        # 顯示為精美的卡片網格 (2 列)
        agent_ids = list(AGENTS.keys())
        for i in range(0, len(agent_ids), 2):
            row_cols = st.columns(2)
            for j in range(2):
                if i + j < len(agent_ids):
                    agent_id = agent_ids[i + j]
                    meta = AGENTS[agent_id]
                    tools = get_tools_for_agent(agent_id)

                    # 模擬一些運行狀態
                    if agent_id in ("inventory_agent", "sales_agent", "risk_agent"):
                        status_badge = "🟢 Running (執行中)"
                    else:
                        status_badge = "🔵 Idle (可運作)"

                    write_badge = "🟢 允許寫入 (需審批)" if meta["can_write"] else "⚪ 唯讀 (限查詢)"

                    with row_cols[j]:
                        with st.container(border=True):
                            col_hdr1, col_hdr2 = st.columns([2.5, 1])
                            with col_hdr1:
                                st.markdown(f"##### {meta['name_zh']} `{meta['name_en']}`")
                            with col_hdr2:
                                st.caption(status_badge)

                            st.markdown(f"**職責**: {meta['description']}")
                            st.markdown(f"**寫入權限**: {write_badge}")
                            st.markdown(f"**授權工具數量**: `{len(tools)}` 個")

                            with st.expander("🔍 展開檢視授權工具清單", expanded=False):
                                if tools:
                                    st.code("\n".join(tools), language="text")
                                else:
                                    st.caption("（無專屬工具）")
            st.markdown("<div style='margin-bottom: 1rem;'></div>", unsafe_allow_html=True)

        st.markdown("---")

        # ── 2) 待審批清單 (Pending Approval List) ───────────────────────────
        st.markdown("### ⏳ 待審批清單 (Pending Approval List)")
        st.markdown("所有 `write` (寫入) 或 `dangerous` (高風險) 的工具調用皆會在此被攔截，直到管理員核准。")

        if pending_count == 0:
            st.success("🎉 目前無任何待審批的工具調用請求！")
        else:
            for idx, item in enumerate(pending_list):
                with st.container(border=True):
                    col_item1, col_item2 = st.columns([3, 2])
                    with col_item1:
                        st.markdown(f"##### 📋 申請單號：`{item['id']}`")
                        st.caption(f"🕒 請求時間：{item['time']} | 👤 申請者角色：`{item['role']}`")
                        if item.get("operation_id"):
                            st.caption(f"🔗 操作識別碼：`{item['operation_id']}`")

                        # 呈現詳情
                        st.markdown(f"**觸發 Agent**: `{item['agent']}` ({AGENTS.get(item['agent'], {}).get('name_zh', '未知')})")
                        st.markdown(f"**調用工具**: `{item['tool']}`")
                        # 將傳入參數轉為易讀中文顯示
                        readable_args = format_parameters_to_chinese(item['tool'], item['args'])
                        st.markdown(f"**傳入參數**: `{readable_args}`")

                    with col_item2:
                        st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)
                        # 檢查目前登入角色是否為 admin
                        current_role = st.session_state.get("role", "guest")
                        current_username = st.session_state.get("username", "")

                        if current_role == "admin":
                            # 輸入拒絕原因的文字框
                            rej_reason = st.text_input("請輸入拒絕原因 (核准時免填)", key=f"reason_{item['id']}", placeholder="輸入原因後點擊拒絕...")

                            # 提供核准與拒絕按鈕
                            btn_col1, btn_col2 = st.columns(2)
                            with btn_col1:
                                if st.button("✅ 核准", key=f"appr_{item['id']}", use_container_width=True):
                                    # 呼叫後端核准
                                    res = approve_action(
                                        item['id'], approver=current_username
                                    )
                                    if res.get("status") == "ok" or res.get("status") == "pending":
                                        st.toast(f"已成功核准單號 {item['id']}！")
                                    else:
                                        st.error(f"核准執行失敗：{res.get('message')}")
                                    st.rerun()
                            with btn_col2:
                                if st.button("❌ 拒絕", key=f"rej_{item['id']}", use_container_width=True):
                                    if not rej_reason:
                                        st.warning("請先填寫拒絕原因！")
                                    else:
                                        # 呼叫後端拒絕，傳入原因
                                        res = reject_action(
                                            item['id'],
                                            rej_reason,
                                            approver=current_username,
                                        )
                                        if res.get("status") == "denied":
                                            st.toast(f"已拒絕單號 {item['id']} 調用請求。")
                                        else:
                                            st.error(f"拒絕失敗：{res.get('message')}")
                                        st.rerun()
                        else:
                            st.info("🔒 唯讀：需登入管理員 (admin) 帳號進行審批")

        # 展開審批歷史紀錄 (提供核准、拒絕外，第三個按鈕「重試」)
        with st.expander("🕒 檢視審批歷史紀錄 & 重新處理 (重試)", expanded=False):
            if not approval_history:
                st.caption("尚無審批歷史紀錄。")
            else:
                current_role = st.session_state.get("role", "guest")
                for item in approval_history:
                    with st.container(border=True):
                        col_hist_info, col_hist_act = st.columns([4, 1.2])
                        with col_hist_info:
                            status_emoji = "✅" if item["status"] == "approved" else "❌"
                            status_zh = "已核准 (Approved)" if item["status"] == "approved" else "已拒絕 (Rejected)"
                            reason_msg = f" | 原因: `{item['reason']}`" if item["reason"] else ""
                            st.markdown(f"**單號**: `{item['id']}` ({status_emoji} {status_zh}{reason_msg})")
                            st.caption(f"🕒 請求時間：{item['time']} | 處理時間：{item['processed_time']} | 申請人：`{item['role']}`")
                            if item.get("operation_id"):
                                st.caption(f"🔗 操作識別碼：`{item['operation_id']}`")
                            readable_args = format_parameters_to_chinese(item['tool'], item['raw_args'])
                            st.markdown(f"**調用工具**: `{item['tool']}` | **參數**: `{readable_args}`")

                        with col_hist_act:
                            st.markdown("<div style='height: 15px;'></div>", unsafe_allow_html=True)
                            can_rollback = item["status"] == "approved" and item["tool"] in ("update_inventory", "create_order")
                            if current_role == "admin" and can_rollback:
                                # 沖銷（補償交易）：走 Gateway 執行、寫入 action log 供稽核。
                                # 不再把單號重置回 pending —— 沖銷本身已核准人一次確認，
                                # 不需要再進一次審批單讓同一位管理員自己審自己。
                                if st.button("🔄 沖銷", key=f"retry_{item['id']}", use_container_width=True):
                                    from backend.tool_gateway import gateway
                                    ok, msg = False, ""
                                    if item["tool"] == "update_inventory":
                                        args = item["raw_args"]
                                        pid = args.get("product_id")
                                        qty_change = args.get("quantity_change")
                                        if pid and qty_change is not None:
                                            qty_change = float(qty_change)
                                            res = gateway.execute_approved(
                                                "rollback_inventory",
                                                {"product_id": pid, "quantity_change": qty_change},
                                                "admin",
                                            )
                                            ok, msg = res.is_ok(), (
                                                f"🔄 已沖銷！產品 {pid} 庫存扣回 {qty_change} 件。" if res.is_ok()
                                                else f"沖銷庫存失敗：{res.message}"
                                            )
                                    elif item["tool"] == "create_order":
                                        args = item["raw_args"]
                                        pid = args.get("product_id")
                                        qty = args.get("quantity")
                                        cust_id = args.get("customer_id", "")
                                        if pid and qty is not None:
                                            cancel_args = {"product_id": pid, "quantity": int(qty)}
                                            if cust_id:
                                                cancel_args["customer_id"] = cust_id
                                            res = gateway.execute_approved("cancel_order", cancel_args, "admin")
                                            ok, msg = res.is_ok(), (
                                                f"🔄 已取消銷售訂單，並將產品 {pid} 庫存回補 {qty} 件！" if res.is_ok()
                                                else f"沖銷訂單失敗：{res.message}"
                                            )

                                    write_action_log(
                                        "retry_approval", {"approval_id": item["id"]}, "admin",
                                        msg or "沖銷未執行（缺少必要參數）", ok,
                                    )
                                    if ok:
                                        st.toast(msg)
                                    else:
                                        st.error(msg or "沖銷未執行：缺少必要參數。")
                                    st.rerun()
                            elif current_role != "admin":
                                st.caption("🔒 僅管理員可沖銷")
                            else:
                                st.caption("（已拒絕，無需沖銷）")

        st.markdown("---")

        # ── 3) B 的派工結果 (Agent Dispatch Logs) ───────────────────────────
        st.markdown("### 📋 總管派工紀錄 (Agent Dispatch Logs)")
        st.markdown("展示總管 Orchestrator 接收到自然語言任務後，如何分析、指派並調度專責 Agent 的派工結果。")

        # 從資料庫中讀取專屬的派工紀錄
        from backend.dispatch_logger import get_recent_dispatches
        dispatch_rows = get_recent_dispatches(50)

        dispatch_data = []
        for r in dispatch_rows:
            primary_agent = r["primary_agent"]
            agent_chain = r["agent_chain"]
            reason = r["reason"]
            task = r["task"]
            routed_by = r["routed_by"]
            timestamp = r["timestamp"]
            caller = r["caller"]

            primary_agent_zh = AGENTS.get(primary_agent, {}).get("name_zh", primary_agent)
            chain_zh = " ➡️ ".join([AGENTS.get(a, {}).get("name_zh", a) for a in agent_chain]) if agent_chain else primary_agent_zh

            dispatch_data.append({
                "時間": timestamp,
                "原始任務": task,
                "指派 Agent": primary_agent_zh,
                "派工工作鏈 (Chain)": chain_zh,
                "決策原因": reason,
                "路由機制": "🧠 LLM 語意" if routed_by == "llm" else "🔑 關鍵字配對",
                "發起者": caller
            })

        if not dispatch_data:
            st.info("目前資料庫中尚無派工決策紀錄。")
        else:
            df_dispatch = pd.DataFrame(dispatch_data)
            st.dataframe(df_dispatch, use_container_width=True, hide_index=True)

        st.markdown("---")

        # ── 4) 工具呼叫記錄 (Tool Call Logs) ─────────────────────────────────
        st.markdown("### 📊 工具呼叫記錄 (Tool Call Logs)")
        st.markdown("系統中所有 Agent 呼叫工具的歷史紀錄 (包含 Read/Write/Dangerous 等級，顯示最新 50 筆)。")

        # 從真實資料庫取得日誌 (排除 agent_dispatch 派工本身的紀錄，只顯示底層工具呼叫)
        real_logs = run_query(
            "SELECT timestamp, caller, tool_name, parameters, result, success FROM agent_action_logs WHERE tool_name != 'agent_dispatch' ORDER BY id DESC LIMIT 50"
        )

        logs_data = []
        for log in real_logs:
            timestamp, caller, tool_name, parameters_str, result_str, success = log
            agent_id = get_agent_for_tool(tool_name) or "unknown"
            agent_zh = AGENTS.get(agent_id, {}).get("name_zh", agent_id)
            status_str = "🟢 成功" if success else "🔴 失敗"

            logs_data.append({
                "時間": timestamp,
                "執行 Agent": agent_zh,
                "調用工具": tool_name,
                "參數": format_parameters_to_chinese(tool_name, parameters_str),
                "呼叫者角色": caller,
                "狀態": status_str,
                "執行結果": result_str[:150]
            })

        if not logs_data:
            st.info("目前資料庫中尚無底層工具呼叫記錄。")
        else:
            df_logs = pd.DataFrame(logs_data)
            st.dataframe(df_logs, use_container_width=True, hide_index=True)



    with tab_metrics:
        st.markdown("### 🛡️ AI 治理觀測指標 (Governance Observability)")
        st.markdown("觀察 AI 代理的送審比例、流程延遲、紀錄關聯與工具負載；這些數值不等同治理有效性證明。")
        
        # 讀取 4 個 SQLite View
        import sqlite3
        from backend.database import DB_FILE
        try:
            dt_row = run_query("SELECT avg_decision_time FROM view_decision_time")
            avg_dt = dt_row[0][0] if dt_row else None
            
            ratio_row = run_query("SELECT intercept_ratio FROM view_pending_intercept_ratio")
            intercept_r = ratio_row[0][0] if ratio_row else None
            
            trace_row = run_query("SELECT traceability_rate FROM view_traceability_rate")
            trace_r = trace_row[0][0] if trace_row else None
            
            tools_row = run_query("SELECT avg_tools_per_turn FROM view_avg_tools_per_turn")
            avg_tools = tools_row[0][0] if tools_row else None
        except Exception as e:
            st.error(f"讀取治理指標失敗: {e}")
            avg_dt = intercept_r = trace_r = avg_tools = None
            
        m_col1, m_col2, m_col3, m_col4 = st.columns(4)
        m_col1.metric("⏱️ 平均決策時間", "資料不足" if avg_dt is None else f"{avg_dt:.2f} 秒", help="總管派工至底層工具執行的平均時間差")
        m_col2.metric("🛡️ pending 送審比例", "資料不足" if intercept_r is None else f"{intercept_r * 100:.1f}%", help="需人工審批的敏感寫入操作佔總任務之比例；不是攻擊攔截成功率")
        m_col3.metric("🔗 紀錄關聯率", "資料不足" if trace_r is None else f"{trace_r * 100:.1f}%", help="可由 caller 與 120 秒時間窗找到可能上游派工的工具執行比例")
        m_col4.metric("⚙️ 平均帶工具數", "資料不足" if avg_tools is None else f"{avg_tools:.2f} 個", help="每次派工任務中平均呼叫的工具次數")
        
        st.markdown("---")
        st.markdown("### 📈 派工與攔截趨勢")
        
        # 繪製趨勢折線圖
        try:
            df_trend = pd.read_sql_query(
                """
                SELECT 
                  substr(timestamp, 1, 10) AS 日期,
                  COUNT(*) AS 總派工數,
                  SUM(needs_approval) AS 攔截審批數
                FROM agent_dispatch_logs
                GROUP BY 日期
                ORDER BY 日期 ASC
                """,
                sqlite3.connect(DB_FILE)
            )
            if not df_trend.empty:
                df_trend = df_trend.set_index("日期")
                st.line_chart(df_trend, color=["#3B82F6", "#EF4444"])
            else:
                st.info("💡 尚無派工趨勢資料。請先與 AI 助理進行對話以產生統計數據。")
        except Exception as e:
            st.error(f"無法載入趨勢圖表: {e}")
        st.markdown("<br><br>", unsafe_allow_html=True)

def _initialize_demo_data_if_empty():
    """若資料庫相關表為空，寫入幾筆寫實的示範數據以美化 Demo 展示。"""
    try:
        # 1. 檢查並寫入待審批項目
        pending_count = run_query("SELECT COUNT(*) FROM pending_approvals")[0][0]
        if pending_count == 0:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 建立兩筆待審批
            run_query(
                "INSERT INTO pending_approvals (approval_id, tool_name, parameters, requester, status, approver, created_at, updated_at, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("PENDING-20260604-001", "update_inventory", '{"product_id": "P001", "quantity_change": 120}', "warehouse", "pending", None, now_str, now_str, None),
                fetch=False
            )
            run_query(
                "INSERT INTO pending_approvals (approval_id, tool_name, parameters, requester, status, approver, created_at, updated_at, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("PENDING-20260604-002", "create_order", '{"customer_id": "C001", "product_id": "P003", "quantity": 15}', "sales", "pending", None, now_str, now_str, None),
                fetch=False
            )

        # 2. 檢查並寫入派工決策紀錄 (B 的派工)
        from backend.dispatch_logger import ensure_table, write_dispatch_log
        ensure_table()
        dispatch_count = run_query("SELECT COUNT(*) FROM agent_dispatch_logs")[0][0]
        if dispatch_count == 0:
            write_dispatch_log(
                routing={"task_type": "single", "primary_agent": "inventory_agent", "agent_chain": ["inventory_agent"], "routed_by": "llm", "needs_approval": False, "reason": "分析意圖為庫存查詢"},
                task="幫我檢查高階筆電還有多少庫存？",
                caller="warehouse"
            )
            write_dispatch_log(
                routing={"task_type": "multi", "primary_agent": "cs_agent", "agent_chain": ["inventory_agent", "sales_agent", "finance_agent", "risk_agent"], "routed_by": "llm", "needs_approval": False, "reason": "跨部門資訊綜整，鏈式調度後由客服Agent彙整"},
                task="老闆早報：查看今天整體的營業狀態",
                caller="admin"
            )
    except Exception as e:
        import sys
        sys.stderr.write(f"[DASHBOARD INIT ERROR] Failed to initialize demo records: {e}\n")
