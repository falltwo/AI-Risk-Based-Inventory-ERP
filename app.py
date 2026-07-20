"""
app.py
進銷存安全系統 — 主程式入口
職責：頁面設定、全域 CSS、登入驗證、側邊欄導覽、頁面路由
所有商業邏輯均位於 backend/，所有 UI 頁面均位於 frontend/
"""

import streamlit as st

# ── 載入 .env（模型設定 LLM_MODEL / 各供應商金鑰，issue #25）────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from backend import init_db, check_login
from backend.database import is_demo_mode_enabled
from backend.access_control import load_principal
from frontend.access_navigation import (
    ROLE_NAMES,
    build_menu_structure,
    clear_identity_session_state,
    effective_product_levels,
    normalize_navigation_state,
)

# ── 初始化資料庫 ────────────────────────────────────────────────────
init_db()

# ── 頁面設定 ────────────────────────────────────────────────────────
st.set_page_config(page_title="進銷存安全系統", page_icon="🛡️", layout="wide")

# ── 全域 CSS ────────────────────────────────────────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;800&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }
    
    /* 美化 Metrics 卡片 */
    div[data-testid="stMetric"] {
        background: linear-gradient(145deg, rgba(255,255,255,0.05) 0%, rgba(0,0,0,0.02) 100%);
        border: 1px solid rgba(128, 128, 128, 0.2);
        border-radius: 15px;
        padding: 24px;
        box-shadow: 0 8px 16px rgba(0,0,0,0.05);
        backdrop-filter: blur(10px);
        transition: all 0.3s ease;
    }
    div[data-testid="stMetric"]:hover {
        transform: translateY(-4px);
        box-shadow: 0 12px 20px rgba(0,0,0,0.1);
        border-color: #3b82f6;
    }
    [data-testid="stMetricValue"] {
        font-size: 2rem !important;
        font-weight: 800 !important;
        color: #1e293b;
    }
    [data-testid="stMetricLabel"] {
        font-size: 1rem !important;
        font-weight: 600 !important;
        color: #64748b;
    }
    
    /* 美化按鈕 */
    .stButton>button {
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.3s ease;
        border: 1px solid rgba(128,128,128,0.3);
    }
    .stButton>button:hover {
        border-color: #3b82f6;
        color: #3b82f6;
        box-shadow: 0 4px 12px rgba(59, 130, 246, 0.2);
    }
    
    /* DataFrame 表格外觀 */
    [data-testid="stDataFrame"] {
        border-radius: 12px;
        overflow: hidden;
        box-shadow: 0 4px 12px rgba(0,0,0,0.05);
        border: 1px solid rgba(128,128,128,0.2);
    }
    
    /* Premium Title 漸層大標題 */
    .premium-title {
        font-weight: 800;
        font-size: 2.2rem;
        background: -webkit-linear-gradient(45deg, #1e293b, #3b82f6);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 0.5rem;
        padding-top: 1rem;
    }
    
    /* 美化側邊欄與 Form 容器 */
    [data-testid="stForm"] {
        border-radius: 12px;
        border: 1px solid rgba(128,128,128,0.2);
        box-shadow: 0 4px 12px rgba(0,0,0,0.03);
    }
    /* 快捷對話按鈕 */
    .quick-btn {
        margin-right: 8px;
        margin-bottom: 8px;
        border-radius: 20px !important;
        font-size: 0.85rem !important;
        background-color: transparent !important;
        border: 1px solid #3b82f6 !important;
        color: #3b82f6 !important;
        padding: 4px 12px !important;
    }
    .quick-btn:hover {
        background-color: #eff6ff !important;
    }
    /* 行動進銷存：觸控友善、大按鈕與間距 */
    .mobile-erp-section { padding: 1rem 0; }
    @media (max-width: 768px) {
        .mobile-erp-section .stSelectbox, .mobile-erp-section .stNumberInput { min-height: 48px; }
        .mobile-erp-section .stButton > button { min-height: 48px; font-size: 1rem; padding: 12px 20px; }
    }
</style>
""", unsafe_allow_html=True)

# ── Session 初始化 ───────────────────────────────────────────────────
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
if 'menu_selection' not in st.session_state:
    st.session_state.menu_selection = "📊 營運分析看板"
if 'sub_menu' not in st.session_state:
    st.session_state.sub_menu = None
if 'gemini_key' not in st.session_state:
    st.session_state.gemini_key = ""
if 'gnews_key' not in st.session_state:
    st.session_state.gnews_key = ""

# ── 登入頁 ──────────────────────────────────────────────────────────
# ... (此處保留原有的 118-143 行邏輯)
if not st.session_state.logged_in:
    st.markdown(
        "<h1 style='text-align: center; margin-bottom: 2rem; font-weight: 800; "
        "background: -webkit-linear-gradient(45deg, #3b82f6, #8b5cf6); "
        "-webkit-background-clip: text; -webkit-text-fill-color: transparent;'>"
        "🛡️ 進銷存安全系統</h1>",
        unsafe_allow_html=True,
    )
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        if is_demo_mode_enabled():
            st.info(
                "💡 **分層 Demo 帳號 / 密碼**：\n"
                "- L1 風險觀測：`viewer / viewer`\n"
                "- L2 決策規劃：`planner / planner`\n"
                "- L3 核准執行：`approver / approver`\n\n"
                "**既有測試帳號**：`admin / admin`、`wh1 / wh1`、"
                "`sales1 / sales1`、`hr1 / hr1`"
            )
        else:
            st.info("請使用已由系統管理者配置的帳號登入。")
        with st.form("login_form"):
            username = st.text_input("使用者帳號")
            password = st.text_input("密碼", type="password")
            submit = st.form_submit_button("登入系統", use_container_width=True)
            if submit:
                result = check_login(username, password)
                if result:
                    st.session_state.logged_in = True
                    st.session_state.username = username
                    st.session_state.role = result["role"]
                    st.session_state.name = result["name"]
                    st.rerun()
                else:
                    st.error("❌ 帳號或密碼錯誤！")
    st.stop()

# ── 登出 ────────────────────────────────────────────────────────────
def logout():
    clear_identity_session_state(st.session_state)
    st.rerun()


# 每次 Streamlit rerun 都從資料庫重新解析身分、角色與有效 entitlement。
principal = load_principal(st.session_state.get("username", ""))
if principal is None:
    clear_identity_session_state(st.session_state)
    st.error("登入身分已失效，請重新登入。")
    st.rerun()
st.session_state.role = principal.role
st.session_state.name = principal.name

# ── CSS 選單優化 ───────────────────────────────────────────────────
st.markdown("""
<style>
    /* 子選單容器縮排與壓縮 */
    .sub-menu-box {
        margin-left: 15px !important;
        border-left: 1px solid #3b82f6;
        padding-left: 8px;
        margin-top: -5px;
        margin-bottom: 5px;
    }
    /* 壓縮選單按鈕尺寸 */
    .stButton > button {
        text-align: left !important;
        justify-content: flex-start !important;
        padding: 4px 12px !important;
        min-height: 32px !important;
        font-size: 0.9rem !important;
        margin-bottom: 2px !important;
    }
    /* 縮小 radio 選項間距 */
    [data-testid="stSidebar"] div[data-testid="stRadio"] > div {
        gap: 0px !important;
    }
</style>
""", unsafe_allow_html=True)

# ── 側邊欄導覽 (樹狀結構) ──────────────────────────────────────────
role_names = ROLE_NAMES

st.sidebar.title(f"🛡️ {principal.name}")
st.sidebar.markdown(f"**身分**: `{role_names.get(principal.role, '未知')}`")
levels = effective_product_levels(principal)
st.sidebar.markdown(f"**有效產品層級**: `{' / '.join(levels) if levels else '無'}`")

# ── 模型/金鑰設定（issue #27）：全部由 .env 驅動，側邊欄不再輸入 API Key ──
#    LLM_MODEL / LLM_FALLBACK_MODELS / LLM_ANALYSIS_MODEL / GNEWS_API_KEY
import os as _os
api_key = ""  # 棄用：各頁 render 簽名保留此參數，實際模型設定見 .env
gemini_model = ""
gnews_api_key = _os.getenv("GNEWS_API_KEY", "")
if not _os.getenv("LLM_MODEL"):
    st.sidebar.warning("⚠️ 尚未設定模型：請在 .env 設定 LLM_MODEL（參考 .env.example）")

if st.sidebar.button("🔓 登出系統", use_container_width=True):
    logout()

st.sidebar.markdown("---")
st.sidebar.markdown("## 📋 導航選單")

# 導覽只使用本次 rerun 從資料庫取得的有效 principal。
MENU_STRUCTURE = build_menu_structure(principal)
if not MENU_STRUCTURE:
    st.error("此帳號目前沒有可用的產品權限，請聯絡管理員。")
    st.stop()

# 角色或 entitlement 變更後，立即清除不再有效的主／子選單狀態。
normalize_navigation_state(st.session_state, MENU_STRUCTURE)

for main_item, subs in MENU_STRUCTURE.items():
    is_active = (st.session_state.menu_selection == main_item)
    # 主選單按鈕
    if st.sidebar.button(
        main_item, 
        key=f"main_{main_item}", 
        use_container_width=True,
        type="primary" if is_active else "secondary"
    ):
        st.session_state.menu_selection = main_item
        st.session_state.sub_menu = subs[0] if subs else None
        if subs:
            st.session_state[f"radio_{main_item}"] = subs[0]
        st.rerun()
    
    # 如果是當前選中的主選單，且有子選單，則在下方渲染
    if is_active and subs:
        with st.sidebar.container():
            st.markdown('<div class="sub-menu-box">', unsafe_allow_html=True)
            
            # 使用 callback 讓選單狀態立馬同步，不必等待二次渲染
            def update_submenu(item_key):
                st.session_state.sub_menu = st.session_state[item_key]

            st.radio(
                f"sub_{main_item}",
                subs,
                index=subs.index(st.session_state.sub_menu) if (st.session_state.sub_menu in subs) else 0,
                label_visibility="collapsed",
                key=f"radio_{main_item}",
                on_change=update_submenu,
                args=(f"radio_{main_item}",)
            )
            # 因為已使用 on_change callback，這邊可以不用再手動檢查並 rerun
            st.markdown('</div>', unsafe_allow_html=True)

# 為了後續路由使用一致的變數名
menu_selection = st.session_state.menu_selection
sub_menu = st.session_state.sub_menu

# ── 頁面路由 ────────────────────────────────────────────────────────
from frontend.page_dashboard import render as render_dashboard
from frontend.page_inventory import render as render_inventory
from frontend.page_procurement import render as render_procurement
from frontend.page_sales import render as render_sales
from frontend.page_finance import render as render_finance
from frontend.page_hr import render as render_hr
from frontend.page_carbon import render as render_carbon
from frontend.page_supply_chain_risk import render as render_supply_chain_risk
from frontend.page_ai_assistant import render as render_ai

if menu_selection == "📊 營運分析看板":
    render_dashboard()

elif menu_selection == "🤖 AI 智能助理":
    if sub_menu == "LINE 客服記錄":
        from frontend.page_line_logs import render as render_line_logs
        render_line_logs()
    elif sub_menu == "Agent Dashboard":
        from frontend.page_agent_dashboard import render as render_agent_dashboard
        render_agent_dashboard(username=principal.username)
    else:
        render_ai(api_key=api_key, role_names=role_names)

elif menu_selection == "📦 進銷存":
    render_inventory(sub_menu=sub_menu)

elif menu_selection == "🛒 採購管理":
    render_procurement(sub_menu=sub_menu, username=principal.username)

elif menu_selection == "💰 銷售管理":
    render_sales(sub_menu=sub_menu , api_key=api_key)

elif menu_selection == "📒 財務會計":
    render_finance(sub_menu=sub_menu)

elif menu_selection == "👥 人資":
    render_hr(sub_menu=sub_menu)

elif menu_selection == "🌿 碳排放管理":
    render_carbon(sub_menu=sub_menu, api_key=api_key)

elif menu_selection == "🌱 供應鏈與風險":
    render_supply_chain_risk(
        sub_menu=sub_menu,
        api_key=api_key,
        gnews_api_key=gnews_api_key or "",
        gemini_model=gemini_model,
        username=principal.username,
    )
