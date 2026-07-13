"""
frontend/page_ai_assistant.py
🤖 AI 智能助理（功能區固定版：sticky + 浮動回頂按鈕 + 新訊息自動捲底）
"""

import streamlit as st
from frontend.ui_utils import show_error
import time
from backend import tools_mapping, ALL_TOOLS

# ══════════════════════════════════════════════
#  全域樣式
# ══════════════════════════════════════════════
_CSS = """
<style>
/* ── 功能區 sticky ── */
.func-bar {
    position: sticky;
    top: 0;
    z-index: 200;
    background: transparent;
    padding: 10px 0 14px 0;
    border-bottom: 1px solid rgba(99,102,241,.22);
    margin-bottom: 18px;
}
/* ── Tab 美化 ── */
.stTabs [data-baseweb="tab-list"] {
    gap: 6px;
    border-bottom: 1px solid rgba(99,102,241,.15);
}
.stTabs [data-baseweb="tab"] {
    border-radius: 8px 8px 0 0;
    padding: 6px 14px;
    font-size: .88rem;
}
/* ── 語音草稿標籤 ── */
.voice-draft-label {
    font-size: .85rem;
    color: #818cf8;
    margin-bottom: .3rem;
    font-weight: 500;
}
/* ── 浮動回頂按鈕 ── */
#__backtop__ {
    position: fixed;
    bottom: 76px;
    right: 22px;
    z-index: 9999;
    background: linear-gradient(135deg, #6366f1, #8b5cf6);
    color: #fff !important;
    border: none;
    border-radius: 50px;
    padding: 9px 17px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    box-shadow: 0 4px 20px rgba(99,102,241,.45);
    font-family: -apple-system, sans-serif;
    opacity: 0;
    pointer-events: none;
    transition: opacity .3s, transform .2s;
    white-space: nowrap;
    text-decoration: none !important;
}
#__backtop__:hover { transform: scale(1.06); }
#__backtop__.visible {
    opacity: 1;
    pointer-events: auto;
}
</style>

<!-- 浮動「回頂部」按鈕 DOM -->
<button id="__backtop__" onclick="
    var m=document.querySelector('section[data-testid=stMain]')||document.querySelector('.main');
    if(m)m.scrollTo({top:0,behavior:'smooth'});
">⬆ 回頂部</button>

<script>
// 監聽捲動，超過 300px 才顯示按鈕
(function(){
    var btn = document.getElementById('__backtop__');
    var main = document.querySelector('section[data-testid="stMain"]') || document.querySelector('.main');
    if (!btn || !main) return;
    main.addEventListener('scroll', function(){
        if (main.scrollTop > 300) btn.classList.add('visible');
        else btn.classList.remove('visible');
    }, {passive: true});
})();
</script>
"""

# 捲到底部的 JS（用唯一時間戳防止瀏覽器快取不執行）
def _scroll_to_bottom_js(ts: int) -> str:
    return f"""
<script id="__scroll_{ts}__">
(function(){{
    var main=document.querySelector('section[data-testid="stMain"]')||document.querySelector('.main');
    if(!main)return;
    // 等 DOM 渲染完再捲
    setTimeout(function(){{main.scrollTo({{top:main.scrollHeight,behavior:'smooth'}})}},120);
}})();
</script>
"""


def render(api_key: str, role_names: dict):

    # ── Session 初始化（放最前面，後續邏輯需要） ──
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {
                "role": "model",
                "content": f"你好，{st.session_state.get('name','使用者')}！我是進銷存安全系統的 AI 助理。你有什麼任務需要我幫忙處理嗎？",
            }
        ]
    if "_voice_transcript" not in st.session_state:
        st.session_state._voice_transcript = ""
    if "_do_scroll_bottom" not in st.session_state:
        st.session_state._do_scroll_bottom = False

    # ── 注入全域樣式 + 浮動按鈕 ──
    st.markdown(_CSS, unsafe_allow_html=True)

    # ── 若有新訊息，注入捲底 JS ──
    if st.session_state._do_scroll_bottom:
        st.session_state._do_scroll_bottom = False
        import time as _t
        st.markdown(_scroll_to_bottom_js(int(_t.time() * 1000)), unsafe_allow_html=True)

    # ══════════ STICKY 功能區 ══════════
    st.markdown("<div class='func-bar' id='func-top'>", unsafe_allow_html=True)

    st.markdown(
        "<div class='premium-title'>🤖 進銷存安全系統 · AI 智能助理</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<p style='color:#64748b;font-size:.9rem;margin:.4rem 0 .8rem'>可用自然語言查詢庫存、訂單、帳款、薪資等。"
        "<b>店長可查全部；其他角色依權限開放。</b></p>",
        unsafe_allow_html=True,
    )

    # 模型呼叫由 orchestrator 經 LiteLLM 處理（issue #25/#27）：設定全在 .env。
    import os as _os
    _env_model = _os.getenv("LLM_MODEL")
    if not _env_model:
        st.warning("⚠️ 尚未設定模型：請在 .env 設定 LLM_MODEL 與對應供應商金鑰（參考 .env.example）。")
        st.markdown("</div>", unsafe_allow_html=True)
        return
    st.caption(f"🔌 目前模型：`{_env_model}`（由 .env 設定；掉線自動切換備援模型）")

    # ── Tabs：常用指令 / 語音輸入 ──
    tab1, tab2 = st.tabs(["💡 常用指令", "🎙️ 語音輸入"])
    with tab1:
        quick_input = _render_quick_commands()
    with tab2:
        _render_voice_section()

    st.markdown("</div>", unsafe_allow_html=True)
    # ══════════ STICKY 功能區結束 ══════════

    # ── 語音確認框 ──
    voice_draft = st.session_state._voice_transcript
    if voice_draft:
        st.markdown(
            "<div class='voice-draft-label'>🎙️ 語音辨識結果（可編輯後點送出）：</div>",
            unsafe_allow_html=True,
        )
        confirmed = st.text_input(
            label="語音辨識結果",
            value=voice_draft,
            key="voice_confirm_input",
            label_visibility="collapsed",
        )
        sc, cc = st.columns([4, 1])
        if sc.button("▶️ 送出指令", use_container_width=True, type="primary"):
            if confirmed.strip():
                st.session_state._voice_transcript = ""
                _run_agent(api_key, role_names, confirmed.strip())
                return
        if cc.button("✕ 清除", use_container_width=True):
            st.session_state._voice_transcript = ""
            st.rerun()

    # ── 聊天記錄 ──
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # ── 文字輸入 ──
    user_input = st.chat_input("💬 輸入指令...（或切換至上方「語音輸入」頁籤）")
    final_input = quick_input or user_input
    if final_input:
        _run_agent(api_key, role_names, final_input)


# ══════════════════════════════════════════════
#  常用指令
# ══════════════════════════════════════════════
def _render_quick_commands() -> str:
    quick_input = ""
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("🏭 列出庫存表格", use_container_width=True):
        quick_input = "幫我列出全部產品的庫存，並整理成表格。"
    if c2.button("🧾 查詢最近訂單", use_container_width=True):
        quick_input = "幫我查詢最近系統上有哪些訂單？"
    if c3.button("⚠️ 庫存需補貨？", use_container_width=True):
        quick_input = "幫我檢查一下目前有沒有任何產品低於安全庫存需要緊急補貨的？"
    if c4.button("👥 查閱人事名單", use_container_width=True):
        quick_input = "請列出人資部 (HR) 現在的所有員工名單。"
    c5, c6, c7, c8 = st.columns(4)
    if c5.button("💰 應收帳款", use_container_width=True):
        quick_input = "應收帳款是多少？"
    if c6.button("📊 財務概況", use_container_width=True):
        quick_input = "請給我財務概況摘要。"
    if c7.button("📋 採購單摘要", use_container_width=True):
        quick_input = "最近採購單的摘要。"
    if c8.button("🤖 智慧補貨建議", use_container_width=True):
        quick_input = "請根據近 30 天的客戶實際消費銷售資料，幫我重新計算一次所有產品的動態安全庫存水位，並建議各產品該進貨多少數量？"
    c9, c10, c11, c12 = st.columns(4)
    if c9.button("🌍 當月碳足跡", use_container_width=True):
        from datetime import datetime
        quick_input = f"請幫我列出 {datetime.now().strftime('%Y-%m')} 所有產品的碳足跡明細。"
    if c10.button("🌪️ 供應鏈風險", use_container_width=True):
        quick_input = "目前有哪些供應鏈風險事件？如果有的話，請告訴我哪些採購單受到影響。"
    if c11.button("🎯 年度ESG目標", use_container_width=True):
        quick_input = "查詢今年設定的ESG目標與達成率建議。"
    if c12.button("🗺️ 風險熱圖", use_container_width=True):
        quick_input = "查詢目前的供應鏈風險熱地圖評估與摘要。"
    return quick_input


# ══════════════════════════════════════════════
#  語音輸入
# ══════════════════════════════════════════════
def _render_voice_section():
    try:
        from streamlit_mic_recorder import speech_to_text

        st.caption("🔹 需使用 Chrome 或 Edge 瀏覽器，並允許麥克風權限")
        transcript = speech_to_text(
            language="zh-TW",
            start_prompt="🔴 開始錄音",
            stop_prompt="⏹ 停止錄音",
            just_once=True,
            use_container_width=False,
            key="mic_stt",
        )
        st.caption("💡 錄音完成後文字會顯示於下方，可修改後再送出執行")
        if transcript and transcript != st.session_state.get("_last_voice", ""):
            st.session_state._voice_transcript = transcript
            st.session_state["_last_voice"] = transcript
            st.rerun()
    except ImportError:
        st.warning("⚠️ 請安裝：`pip install streamlit-mic-recorder`")


# ══════════════════════════════════════════════
#  AI Agent 執行
# ══════════════════════════════════════════════
def _run_agent(api_key, role_names: dict, final_input: str):
    st.session_state.messages.append({"role": "user", "content": final_input})
    with st.chat_message("user"):
        st.markdown(final_input)

    with st.chat_message("model"):
        status_box = st.empty()
        status_box.info("🧠 AI 正在思考與決策中...")
        try:
            import os
            from backend.agent_orchestrator import orchestrate

            # 呼叫總管 Orchestrator 進行派工與執行（模型設定全在 .env，issue #27）
            # history：帶入本 session 的近期對話（排除剛加入的本輪 user 訊息），
            # orchestrator 內部會做 sliding window 修剪。
            hist = st.session_state.messages[:-1]
            res = orchestrate(final_input, role=st.session_state.role, history=hist)
            reply_text = res.get("reply", "（無回覆內容）")
            
            status_box.empty()
            st.markdown(reply_text)
            st.session_state.messages.append({"role": "model", "content": reply_text})

            # ── 標記下次 rerun 需要自動捲底 ──
            st.session_state._do_scroll_bottom = True

        except Exception as e:
            status_box.empty()
            show_error("與 Agent 溝通時發生錯誤", e)
