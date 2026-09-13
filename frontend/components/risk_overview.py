import sqlite3

import pandas as pd
import streamlit as st

from backend.database import DB_FILE
from backend.erp_exchange import (
    build_purchase_order_template_csv,
    parse_purchase_order_csv,
)
from backend.l1_monitoring import (
    ALERT_KIND_CANDIDATE,
    ALERT_KIND_CONFIRMED,
    ALERT_STATUS_NOTIFIED_L2,
    CANDIDATE_STATUS_OPTIONS,
    CONFIRMED_STATUS_OPTIONS,
    get_latest_event_alerts,
    get_latest_risk_summary,
    load_open_purchase_rows,
    map_purchase_rows_to_events,
    set_alert_status,
)
from backend.supply_chain_risk import (
    get_risk_events_list,
    get_supply_chain_summary_kpis,
)
from frontend.components.supply_map import render_risk_heatmap
from frontend.ui_utils import show_error


_ALERT_WINDOW_OPTIONS = {"近 7 天": 7, "近 30 天": 30, "近 90 天": 90}
_ALERT_LIMIT = 10
_SEVERITY_ICONS = {"高": "🔴 高", "中": "🟠 中", "低": "🟡 低", "無": "⚪ 無"}


_L1_DISPLAY_COLUMNS = {
    "po_id": "採購單",
    "supplier_id": "供應商",
    "product_id": "物料",
    "supplier_country": "國家",
    "supplier_region": "地區",
    "event_type": "命中事件",
    "impact_days": "預估延遲天數",
    "match_status": "對映結果",
    "notification_status": "通知狀態",
}


def _load_supplier_context(supplier_ids: set[str]) -> dict[str, dict]:
    if not supplier_ids:
        return {}
    placeholders = ",".join("?" for _ in supplier_ids)
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT supplier_id, country, region, risk_level "
            f"FROM suppliers WHERE supplier_id IN ({placeholders})",
            tuple(sorted(supplier_ids)),
        ).fetchall()
    return {row["supplier_id"]: dict(row) for row in rows}


def _location_label(item: dict) -> str:
    parts = [part for part in (item.get("country"), item.get("region")) if part]
    return "／".join(parts) or "未設定"


def _render_latest_event_alerts(*, actor: str) -> None:
    st.markdown("#### 🚨 最新事件告警")
    header_left, header_right = st.columns([3, 1])
    with header_left:
        st.caption(
            "已確認事件來自 L2 登錄；「AI 偵測待確認」來自排程或 L2 更新新聞後、"
            "尚未登錄為正式事件的情報。每次重新整理都會直接讀取最新資料。"
        )
    with header_right:
        window_label = st.selectbox(
            "告警時間範圍",
            list(_ALERT_WINDOW_OPTIONS),
            index=1,
            key="l1_alert_window",
            label_visibility="collapsed",
        )
    since_days = _ALERT_WINDOW_OPTIONS[window_label]

    try:
        feed = get_latest_event_alerts(
            actor=actor, since_days=since_days, limit=_ALERT_LIMIT
        )
    except PermissionError:
        st.error("此帳號沒有讀取事件告警的權限。")
        return
    except sqlite3.Error as exc:
        show_error("事件告警讀取失敗", exc)
        return

    metric_a, metric_b, metric_c = st.columns(3)
    metric_a.metric("已確認事件", f"{feed['confirmed_count']} 筆")
    metric_b.metric("AI 偵測待確認", f"{feed['candidate_count']} 筆")
    metric_c.metric("最高嚴重度", _SEVERITY_ICONS.get(feed["highest_severity"], feed["highest_severity"]))
    st.caption(f"統計區間自 {feed['since']} 起 ・ 更新時間 {feed['generated_at']}")

    st.markdown("**已確認事件**")
    if not feed["confirmed"]:
        st.info("此區間內尚無已登錄的供應鏈風險事件。")
    else:
        unread = sum(1 for item in feed["confirmed"] if item["ack_status"] == "未讀")
        st.caption(f"未讀 {unread} 筆 ・ 狀態改完按「儲存狀態」，重新整理不會歸零。「替代提案」為 L3 對此事件提案的核准進度。")
        confirmed_rows = [
            {
                "處理狀態": item["ack_status"],
                "嚴重度": _SEVERITY_ICONS.get(item["severity"], item["severity"]),
                "事件": item["event_type"],
                "國家／地區": _location_label(item),
                "預估延遲": f"{item['impact_days']} 天",
                "替代提案": _proposal_label(item.get("proposals") or {}),
                "登錄時間": item["created_at"] or "未記錄",
                "來源": item["source"],
                "來源新聞": item["news_title"] or "—",
                "原文連結": item["news_url"] or "",
                "事件說明": item["description"] or "未提供",
                "備註": item.get("ack_note") or "",
                "_id": item["id"],
            }
            for item in feed["confirmed"]
        ]
        edited = st.data_editor(
            pd.DataFrame(confirmed_rows),
            width="stretch",
            hide_index=True,
            key=f"l1_confirmed_editor_{since_days}",
            disabled=[c for c in confirmed_rows[0] if c not in ("處理狀態", "備註")],
            column_config={
                "處理狀態": st.column_config.SelectboxColumn("處理狀態", options=list(CONFIRMED_STATUS_OPTIONS), required=True),
                "備註": st.column_config.TextColumn("備註", width="medium"),
                "原文連結": st.column_config.LinkColumn("原文連結", display_text="開啟"),
                "_id": None,
            },
        )
        changed = [
            (int(row["_id"]), row["處理狀態"], row["備註"])
            for (_, row), original in zip(edited.iterrows(), confirmed_rows)
            if row["處理狀態"] != original["處理狀態"] or (row["備註"] or "") != (original["備註"] or "")
        ]
        if st.button(f"💾 儲存狀態（{len(changed)} 筆異動）", key="l1_save_confirmed", disabled=not changed):
            try:
                for event_id, status, note in changed:
                    set_alert_status(ALERT_KIND_CONFIRMED, event_id, status, actor=actor, note=note or "")
            except PermissionError:
                st.error("此帳號沒有標記告警狀態的權限。")
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.toast(f"已更新 {len(changed)} 筆告警狀態", icon="💾")
                st.rerun()

    st.markdown("**AI 偵測待確認**")
    if not feed["candidates"]:
        st.success("此區間內沒有尚未登錄的高風險情報。")
    else:
        notified = sum(1 for item in feed["candidates"] if item["ack_status"] == ALERT_STATUS_NOTIFIED_L2)
        st.caption(f"已通知 L2 {notified} 筆。勾選後按「通知 L2」，L2「情報與決策」頁頂端會列出這些情報；L2 登錄成事件後自動從這裡消失。")
        candidate_rows = [
            {
                "通知 L2": item["ack_status"] == ALERT_STATUS_NOTIFIED_L2,
                "處理狀態": item["ack_status"],
                "嚴重度": _SEVERITY_ICONS.get(item["severity"], item["severity"]),
                "類型": item["event_type"],
                "國家／地區": _location_label(item),
                "預估延遲": f"{item['impact_days']} 天",
                "情報時間": item["observed_at"] or "未記錄",
                "新聞標題": item["title"] or "（無標題）",
                "原文連結": item["url"] or "",
                "狀態": item["status"],
                "備註": item.get("ack_note") or "",
                "_news_id": item["news_id"],
            }
            for item in feed["candidates"]
        ]
        edited = st.data_editor(
            pd.DataFrame(candidate_rows),
            width="stretch",
            hide_index=True,
            key=f"l1_candidate_editor_{since_days}",
            disabled=[c for c in candidate_rows[0] if c not in ("通知 L2", "處理狀態", "備註")],
            column_config={
                "通知 L2": st.column_config.CheckboxColumn("通知 L2"),
                "處理狀態": st.column_config.SelectboxColumn("處理狀態", options=list(CANDIDATE_STATUS_OPTIONS), required=True),
                "備註": st.column_config.TextColumn("備註", width="medium"),
                "原文連結": st.column_config.LinkColumn("原文連結", display_text="開啟"),
                "_news_id": None,
            },
        )
        changed = []
        for (_, row), original in zip(edited.iterrows(), candidate_rows):
            status = ALERT_STATUS_NOTIFIED_L2 if bool(row["通知 L2"]) else row["處理狀態"]
            if status == ALERT_STATUS_NOTIFIED_L2 and not bool(row["通知 L2"]):
                status = "已讀"   # 取消勾選 → 退回已讀
            if status != original["處理狀態"] or (row["備註"] or "") != (original["備註"] or ""):
                changed.append((int(row["_news_id"]), status, row["備註"]))
        notify_count = sum(1 for _, status, _ in changed if status == ALERT_STATUS_NOTIFIED_L2)
        label = f"📨 通知 L2（{notify_count} 則）" if notify_count else f"💾 儲存狀態（{len(changed)} 筆異動）"
        if st.button(label, key="l1_save_candidates", disabled=not changed):
            try:
                for news_id, status, note in changed:
                    set_alert_status(ALERT_KIND_CANDIDATE, news_id, status, actor=actor, note=note or "")
            except PermissionError:
                st.error("此帳號沒有標記告警狀態的權限。")
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.toast(f"已更新 {len(changed)} 筆情報狀態", icon="📨")
                st.rerun()
        st.caption("待確認情報需由具 L2 權限的人員在「情報與決策」頁登錄後，才會成為正式事件並進入對映。")


def _render_latest_ai_summary(*, actor: str) -> None:
    """L2／排程最近一次產生的 AI 風險摘要（唯讀）。"""
    st.markdown("#### 🤖 最新 AI 風險摘要")
    try:
        latest = get_latest_risk_summary(actor=actor)
    except PermissionError:
        st.error("此帳號沒有讀取 AI 風險摘要的權限。")
        return
    except sqlite3.Error as exc:
        show_error("AI 風險摘要讀取失敗", exc)
        return
    if not latest:
        st.info("尚未產生 AI 風險摘要；由 L2 在「情報與決策」按「產生／更新即時風險摘要」或排程更新新聞後產生。")
        return
    st.caption(
        f"產生時間 {latest['generated_at']} ・ 由 {latest.get('actor') or '排程'} 產生 ・ "
        f"依據 {latest['news_count']} 則新聞、{latest['event_count']} 筆已登錄事件"
    )
    with st.container(border=True):
        st.markdown(latest["summary"])
    if latest["events"]:
        st.markdown("**AI 建議事件（待 L2 確認）**")
        st.dataframe(
            pd.DataFrame([
                {
                    "類型": e.get("event_type") or "其他",
                    "國家／地區": _location_label(e),
                    "預估延遲": f"{e.get('impact_days') or 0} 天",
                    "說明": e.get("description") or "",
                }
                for e in latest["events"]
            ]),
            width="stretch",
            hide_index=True,
        )
    if latest["audit"]:
        with st.expander(f"證據檢核：{len(latest['audit'])} 項 AI 建議被略過或調整"):
            for item in latest["audit"]:
                st.caption(f"{item.get('kind')}「{item.get('name')}」{item.get('action')}：{item.get('reason')}")


def _proposal_label(counts: dict) -> str:
    parts = []
    if counts.get("approved"):
        parts.append(f"✅ 核准 {counts['approved']}")
    if counts.get("pending"):
        parts.append(f"⏳ 待審 {counts['pending']}")
    if counts.get("rejected"):
        parts.append(f"❌ 拒絕 {counts['rejected']}")
    return "、".join(parts) or "—"


def _render_read_only_mapping(events: list[dict], *, actor: str) -> None:
    st.markdown("#### 🔔 L1 告警與通知中心")
    st.caption(
        "對映只在記憶體中進行：把採購單依供應商地區比對已確認事件，產生通知預覽，"
        "不會寫入 ERP 或提案暫存區。"
    )
    source = st.radio(
        "採購資料來源",
        ("系統內未結採購單", "上傳 CSV"),
        horizontal=True,
        key="l1_monitor_source",
    )
    purchase_rows: list[dict] = []
    if source == "系統內未結採購單":
        try:
            purchase_rows = load_open_purchase_rows(actor=actor)
        except PermissionError:
            st.error("此帳號沒有讀取採購單的權限。")
            return
        except sqlite3.Error as exc:
            show_error("採購單讀取失敗", exc)
            return
        if not purchase_rows:
            st.info("系統內目前沒有未結採購單；可改用上傳 CSV 預覽對映。")
            return
        st.caption(f"讀取 {len(purchase_rows)} 條未結採購明細（即時，不需上傳）。")
    else:
        st.download_button(
            "下載唯讀對映 CSV 範本",
            data=build_purchase_order_template_csv(),
            file_name="l1_purchase_order_monitoring_template.csv",
            mime="text/csv",
            key="l1_monitor_download_template",
        )
        uploaded = st.file_uploader(
            "上傳採購資料 CSV",
            type=["csv"],
            key="l1_monitor_csv_upload",
            help="檔案必須為 UTF-8；上傳與對映均不會修改 ERP。",
        )
        if uploaded is None:
            st.info("可下載範本後匯入採購資料，以預覽事件對映與通知結果。")
            return
        try:
            purchase_rows = parse_purchase_order_csv(uploaded.getvalue())
        except ValueError as exc:
            st.error(f"CSV 驗證失敗：{exc}")
            return

    try:
        supplier_context = _load_supplier_context(
            {row["supplier_id"] for row in purchase_rows}
        )
        mapped_rows = map_purchase_rows_to_events(
            purchase_rows,
            supplier_context=supplier_context,
            events=events,
        )
    except ValueError as exc:
        st.error(f"CSV 驗證失敗：{exc}")
        return
    except sqlite3.Error:
        st.error("目前無法讀取供應商地區資料，請稍後再試。")
        return

    alert_rows = [row for row in mapped_rows if row["match_status"] == "需關注"]
    incomplete_rows = [
        row for row in mapped_rows if row["match_status"] == "資料待補"
    ]
    metric_a, metric_b, metric_c = st.columns(3)
    metric_a.metric("完成對映", f"{len(mapped_rows)} 筆")
    metric_b.metric("需通知", f"{len(alert_rows)} 筆")
    metric_c.metric("資料待補", f"{len(incomplete_rows)} 筆")

    display = pd.DataFrame(mapped_rows).rename(columns=_L1_DISPLAY_COLUMNS)
    st.dataframe(
        display[list(_L1_DISPLAY_COLUMNS.values())],
        width="stretch",
        hide_index=True,
    )

    st.markdown("##### 通知預覽（尚未發送）")
    if alert_rows:
        for row in alert_rows:
            st.warning(row["notification"])
    else:
        st.success("本次匯入資料未命中已登錄事件，無需發送風險通知。")

    export_rows = pd.DataFrame(
        {
            "採購單": [row.get("po_id") for row in mapped_rows],
            "供應商": [row.get("supplier_id") for row in mapped_rows],
            "對映結果": [row["match_status"] for row in mapped_rows],
            "通知狀態": [row["notification_status"] for row in mapped_rows],
            "通知內容": [row["notification"] for row in mapped_rows],
        }
    )
    st.download_button(
        "下載告警與通知清單",
        data=export_rows.to_csv(index=False).encode("utf-8-sig"),
        file_name="l1_alert_notifications.csv",
        mime="text/csv",
        key="l1_monitor_download_alerts",
    )

def render_risk_overview(*, actor: str):
    """渲染 L1 唯讀閉環：事件告警、熱圖、資料對映與通知預覽。"""
    st.markdown("#### 📊 供應鏈風險總覽 (Risk Overview)")
    
    # 取得 KPI 數據
    try:
        kpis = get_supply_chain_summary_kpis()
    except Exception as e:
        show_error("KPI 數據讀取失敗", e)
        kpis = {"event_count": 0, "supplier_count": 0, "order_count": 0}

    # 顯示 KPI 卡片
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("📡 最新風險事件 (近30天)", f"{kpis['event_count']} 宗")
        st.caption("AI 偵測並登錄之供應鏈異常事件")
        
    with col2:
        st.metric("🏭 受波及供應商", f"{kpis['supplier_count']} 家")
        st.caption("位於受災區域且有進行中採購之供應商")
        
    with col3:
        st.metric("🧾 受波及銷售訂單", f"{kpis['order_count']} 筆")
        st.caption("因原材料延遲可能面臨交期風險之訂單")

    st.markdown("<br>", unsafe_allow_html=True)
    
    # 顯示熱圖
    with st.container(border=True):
        st.markdown("**🌍 全球即時風險熱圖**")
        render_risk_heatmap(key="overview_heatmap")

    st.markdown("<br>", unsafe_allow_html=True)
    _render_latest_event_alerts(actor=actor)

    st.markdown("<br>", unsafe_allow_html=True)
    _render_latest_ai_summary(actor=actor)

    # CSV 對映只比對「已確認」事件；候選情報尚未登錄，不參與對映。
    try:
        event_frame = get_risk_events_list(limit=30)
        events = [] if event_frame is None or event_frame.empty else event_frame.to_dict("records")
    except Exception as exc:
        show_error("風險事件讀取失敗", exc)
        events = []

    st.markdown("<br>", unsafe_allow_html=True)
    _render_read_only_mapping(events, actor=actor)
