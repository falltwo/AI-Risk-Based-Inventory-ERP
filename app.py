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
        st.info("💡 **測試帳號 / 密碼**：\n- 店長：`admin / admin`\n- 倉管：`wh1 / wh1`\n- 業務：`sales1 / sales1`\n- 人資：`hr1 / hr1`")
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
    st.session_state.logged_in = False
    st.session_state.menu_selection = "📊 營運分析看板"
    st.session_state.sub_menu = None
    for key in list(st.session_state.keys()):
        if key.startswith("erp_csv_"):
            del st.session_state[key]
    if "messages" in st.session_state:
        st.session_state.messages = []
    st.rerun()

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
role_names = {"admin": "系統管理員", "warehouse": "倉管部", "hr": "人資部", "sales": "業務部"}

st.sidebar.title(f"🛡️ {st.session_state.name}")
st.sidebar.markdown(f"**身分**: `{role_names.get(st.session_state.role, '未知')}`")

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

# 定義所有選單結構
FULL_MENU = {
    "📊 營運分析看板": [],
    "🤖 AI 智能助理": ["對話介面", "LINE 客服記錄", "Agent Dashboard"],
    "📦 進銷存": ["商品管理", "庫存數量", "入庫/出庫", "條碼掃描", "倉庫管理"],
    "🛒 採購管理": ["採購單", "供應商管理", "進貨成本", "採購歷史", "ERP CSV 交換"],
    "💰 銷售管理": ["報價單", "銷售單", "客戶消費視覺化", "客戶個人消費分析", "收款管理"],
    "📒 財務會計": ["應收/應付", "總帳", "成本分析", "財報"],
    "👥 人資": ["員工資料", "薪資", "出勤"],
    "🌿 碳排放管理": ["碳排放總覽", "碳足跡追蹤", "減量目標", "年度碳目標分析", "ESG 報告","供應商風險與碳排"],
    "🌱 供應鏈與風險": []
}

# 角色權限對照表
ROLE_PERMISSIONS = {
    "admin": list(FULL_MENU.keys()),
    "warehouse": ["📊 營運分析看板", "🤖 AI 智能助理", "📦 進銷存", "🛒 採購管理", "🌱 供應鏈與風險"],
    "sales": ["📊 營運分析看板", "🤖 AI 智能助理", "💰 銷售管理", "🌿 碳排放管理"],
    "hr": ["📊 營運分析看板", "🤖 AI 智能助理", "👥 人資"]
}

# 根據目前角色過濾出的選單
allowed_menus = ROLE_PERMISSIONS.get(st.session_state.role, ["📊 營運分析看板"])
MENU_STRUCTURE = {k: v for k, v in FULL_MENU.items() if k in allowed_menus}

# 若目前選中的主選單不在權限內，強制跳回第一個
if st.session_state.menu_selection not in MENU_STRUCTURE:
    st.session_state.menu_selection = list(MENU_STRUCTURE.keys())[0]
    st.session_state.sub_menu = MENU_STRUCTURE[st.session_state.menu_selection][0] if MENU_STRUCTURE[st.session_state.menu_selection] else None

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
        render_agent_dashboard()
    else:
        render_ai(api_key=api_key, role_names=role_names)

elif menu_selection == "📦 進銷存":
    render_inventory(sub_menu=sub_menu)

elif menu_selection == "🛒 採購管理":
    render_procurement(sub_menu=sub_menu)

elif menu_selection == "💰 銷售管理":
    render_sales(sub_menu=sub_menu , api_key=api_key)

elif menu_selection == "📒 財務會計":
    render_finance(sub_menu=sub_menu)

elif menu_selection == "👥 人資":
    render_hr(sub_menu=sub_menu)

elif menu_selection == "🌿 碳排放管理":
    render_carbon(sub_menu=sub_menu, api_key=api_key)

elif menu_selection == "🌱 供應鏈與風險":
    render_supply_chain_risk(sub_menu=sub_menu, api_key=api_key, gnews_api_key=gnews_api_key or "", gemini_model=gemini_model)
