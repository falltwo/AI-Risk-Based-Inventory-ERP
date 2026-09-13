"""管理者安全稽核頁：只讀取登入事件，不暴露密碼資料。"""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from backend.security_audit import EVENT_LABELS, list_auth_events


def render(*, username: str) -> None:
    st.title("🔐 安全稽核紀錄")
    st.caption("僅顯示登入安全事件；系統不會在此頁顯示密碼或密碼雜湊。")

    today = date.today()
    col_user, col_event, col_dates = st.columns([1, 1, 1.5])
    with col_user:
        target_username = st.text_input("帳號篩選", placeholder="例如：admin")
    with col_event:
        selected_label = st.selectbox("事件類型", ["全部", *EVENT_LABELS.values()])
    with col_dates:
        date_range = st.date_input(
            "日期範圍",
            value=(today - timedelta(days=30), today),
            max_value=today,
        )

    label_to_event = {label: event for event, label in EVENT_LABELS.items()}
    event_type = "" if selected_label == "全部" else label_to_event[selected_label]
    start_date = date_range[0] if isinstance(date_range, tuple) and date_range else None
    end_date = date_range[1] if isinstance(date_range, tuple) and len(date_range) == 2 else start_date

    try:
        events = list_auth_events(
            username,
            target_username=target_username,
            event_type=event_type,
            start_date=start_date,
            end_date=end_date,
        )
    except PermissionError:
        st.error("你沒有查看安全稽核紀錄的權限。")
        st.stop()

    total = len(events)
    locked = sum(event["事件"] == EVENT_LABELS["login_locked"] for event in events)
    failed = sum(event["事件"] == EVENT_LABELS["login_failed"] for event in events)
    metric_total, metric_locked, metric_failed = st.columns(3)
    metric_total.metric("符合篩選的事件", total)
    metric_locked.metric("帳號鎖定事件", locked)
    metric_failed.metric("登入失敗事件", failed)

    if events:
        st.dataframe(events, use_container_width=True, hide_index=True)
    else:
        st.info("此篩選條件下尚無安全事件。")
