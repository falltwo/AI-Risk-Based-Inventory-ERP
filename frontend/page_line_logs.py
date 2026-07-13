import streamlit as st
import pandas as pd
from backend.database import run_query

def render():
    st.markdown('<div class="premium-title">📞 LINE 客服記錄</div>', unsafe_allow_html=True)
    st.markdown("檢視 LINE Bot 的歷史對話內容與 AI 助手回覆。左側選擇人員，右側檢視完整對話。")
    
    # 讀取所有有對話紀錄的用戶列表
    users_sql = "SELECT DISTINCT user_id, user_name FROM line_bot_logs ORDER BY created_at DESC"
    users_raw = run_query(users_sql)
    
    if not users_raw:
        st.info("💡 尚無任何 LINE 對話紀錄。請先到 LINE 傳送訊息給 Bot！")
        return
        
    user_options = {row[0]: f"{row[1]} ({row[0][:8]}...)" for row in users_raw}
    
    st.markdown("---")
    
    col1, col2 = st.columns([1, 2.5])
    
    with col1:
        st.markdown("##### 👥 使用者名單")
        # 以 radio 按鈕呈現選擇
        selected_user_id = st.radio(
            label="選擇要檢視的對話：", 
            options=list(user_options.keys()), 
            format_func=lambda x: user_options[x],
            key="line_log_user_selector",
            label_visibility="collapsed"
        )
        
        st.markdown("---")
        st.markdown("##### 📅 篩選日期範圍")
        date_range = st.date_input("選擇日期區間", [], key="line_log_date_filter")
        
    with col2:
        st.markdown(f"##### 💬 {user_options[selected_user_id]} 的歷史紀錄")
        
        # 取得該用戶的對話，並加上日期篩選
        chat_sql = "SELECT user_msg, ai_reply, created_at FROM line_bot_logs WHERE user_id = ?"
        params = [selected_user_id]
        
        if len(date_range) == 2:
            start_date, end_date = date_range
            chat_sql += " AND created_at >= ? AND created_at <= ?"
            params.extend([f"{start_date} 00:00:00", f"{end_date} 23:59:59"])
        elif len(date_range) == 1:
            start_date = date_range[0]
            chat_sql += " AND created_at LIKE ?"
            params.append(f"{start_date} %")
            
        chat_sql += " ORDER BY created_at ASC"
        chats = run_query(chat_sql, tuple(params))
        
        if not chats:
            st.warning("此用戶無對話紀錄。")
        else:
            # 加上漂亮的外框設計
            with st.container(border=True):
                for msg in chats:
                    user_msg, ai_reply, created_at = msg
                    st.caption(f"🕒 紀錄時間：{created_at}")
                    
                    with st.chat_message("user"):
                        st.write(user_msg)
                    
                    with st.chat_message("assistant"):
                        st.write(ai_reply)
                    
                    st.divider()
