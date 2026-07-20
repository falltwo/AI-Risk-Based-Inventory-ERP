import os
import sys
import asyncio
from dotenv import load_dotenv

# 將目前與上一層資料夾加入環境變數，讓程式能順利 import LINE helpers / backend
LINE_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(LINE_BOT_DIR)
for import_path in (LINE_BOT_DIR, BASE_DIR):
    if import_path not in sys.path:
        sys.path.append(import_path)

# 載入 .env
load_dotenv(os.path.join(BASE_DIR, ".env"))

from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    PushMessageRequest,
    FlexMessage,
    FlexBubble,
    FlexBox,
    FlexText,
    FlexBubbleStyles,
    FlexBlockStyle,
    FlexSeparator,
    ImageMessage,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    AudioMessageContent,
    PostbackEvent
)
from google import genai
from google.genai import types

from backend import ALL_TOOLS, init_db
from backend.tool_gateway import gateway
from backend.tool_registry import registry
from backend.flex_builder import build_low_stock_flex, build_risk_events_flex
from backend.chart_builder import build_carbon_trend_chart, build_finance_pie_chart
from line_access import (
    build_line_tools,
    env_flag,
    is_line_tool_allowed,
    parse_line_user_ids,
)

# 確保資料庫初始化
init_db()

app = FastAPI()

# 開放圖床目錄
STATIC_DIR = os.path.join(BASE_DIR, "static_images")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/images", StaticFiles(directory=STATIC_DIR), name="images")

# 存放目前 Ngrok 公開網址
GLOBAL_BASE_URL = ""

import json
import urllib.request
def get_ngrok_url():
    try:
        req = urllib.request.Request("http://127.0.0.1:4040/api/tunnels")
        with urllib.request.urlopen(req, timeout=1) as response:
            data = json.loads(response.read().decode())
            for t in data.get('tunnels', []):
                if t.get('public_url', '').startswith('https://'):
                    return t['public_url']
    except Exception:
        pass
    return None

# =================================================================
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
LINE_BRIEFING_ENABLED = env_flag(os.getenv("LINE_BRIEFING_ENABLED"), default=False)
LINE_BRIEFING_USER_IDS = parse_line_user_ids(os.getenv("LINE_BRIEFING_USER_IDS"))
# =================================================================

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

client = genai.Client(api_key=GEMINI_API_KEY)

# Flex：單一 text 元件最多約 2000 字元；整則 Flex JSON 有大小上限，保守分段
_FLEX_CHUNK = 1400
_FLEX_MAX_CHUNKS = 5
_LINE_GATEWAY_DEFAULT_ROLE = "warehouse"


def _get_line_user_role(line_user_id: str) -> str:
    """查詢 LINE 用戶的 ERP 角色，預設為 warehouse（受限角色）"""
    try:
        from backend.database import get_line_user_role
        return get_line_user_role(line_user_id)
    except Exception:
        return _LINE_GATEWAY_DEFAULT_ROLE


def _build_gateway_function_response(tool_name: str, args: dict, role: str = None) -> tuple[dict, bool]:
    if role is None:
        role = _LINE_GATEWAY_DEFAULT_ROLE
    if not is_line_tool_allowed(tool_name, registry, role):
        return (
            {
                "status": "denied",
                "error": "LINE 入口僅允許唯讀或建議工具；寫入操作請由已登入的 Web 介面送審。",
            },
            False,
        )
    gw_result = gateway.call(tool_name, args or {}, role=role)
    payload = gw_result.to_dict()

    if gw_result.is_ok():
        payload["result"] = gw_result.data
        return payload, True

    if gw_result.status == "pending":
        payload["result"] = f"已送審批：{gw_result.message}"
        return payload, False

    payload["error"] = gw_result.message or f"Gateway returned status: {gw_result.status}"
    return payload, False


def _gateway_payload_to_reply(payload: dict) -> str:
    if payload.get("status") == "ok":
        return str(payload.get("result") or payload.get("data") or "")
    return str(
        payload.get("error")
        or payload.get("result")
        or payload.get("message")
        or "Gateway returned no response."
    )


def _write_line_dispatch_log(user_task: str, tool_name: str, args: dict):
    try:
        from backend.agent_registry import get_agent_for_tool, get_agent
        from backend.dispatch_logger import write_dispatch_log
        from backend.tool_registry import registry

        agent_id = get_agent_for_tool(tool_name) or "cs_agent"
        tool_info = registry.get_tool_info(tool_name) or {}
        risk_level = tool_info.get("risk_level", "")
        needs_approval = risk_level in ("write", "dangerous")
        agent_name = (get_agent(agent_id) or {}).get("name_zh", agent_id)
        routing = {
            "task_type": "single",
            "primary_agent": agent_id,
            "agent_chain": [agent_id],
            "routed_by": "line_gateway",
            "needs_approval": needs_approval,
            "reason": f"LINE function call `{tool_name}` routed to {agent_name}; args={args}",
        }
        write_dispatch_log(routing, user_task, caller="line_bot")
    except Exception as e:
        print(f"Error writing LINE dispatch log: {e}")


LINE_TOOLS = build_line_tools(ALL_TOOLS, registry, role=_LINE_GATEWAY_DEFAULT_ROLE)


def _chunk_reply_for_flex(text: str) -> list[str]:
    text = (text or "").strip() or "（無內容）"
    max_total = _FLEX_MAX_CHUNKS * _FLEX_CHUNK
    if len(text) > max_total:
        text = text[: max_total - 35].rstrip() + "\n…（內容過長已省略，請縮短提問）"
        
    chunks = []
    while len(text) > _FLEX_CHUNK:
        # 尋找 _FLEX_CHUNK 範圍內最後一個換行符號來作智慧切割
        split_idx = text.rfind('\n', 0, _FLEX_CHUNK)
        if split_idx == -1:
            split_idx = _FLEX_CHUNK
        chunks.append(text[:split_idx].strip())
        text = text[split_idx:].strip()
        
    if text:
        chunks.append(text.strip())
        
    return chunks


def _flex_alt_text(reply_text: str) -> str:
    one_line = " ".join((reply_text or "").split())
    if len(one_line) <= 400:
        return one_line or "助理回覆"
    return one_line[:397] + "..."


def reply_text_to_flex_message(reply_text: str, title="進銷存助理") -> FlexMessage:
    """將純文字回覆包成單一 Bubble Flex，方便在 LINE 上好讀、有標題區塊。"""
    err = (reply_text or "").lstrip().startswith("❌")
    title = "系統提示" if err else title
    header_bg = "#C62828" if err else "#00B900"
    title_color = "#FFFFFF"

    chunks = _chunk_reply_for_flex(reply_text)
    body_contents = []
    for i, chunk in enumerate(chunks):
        if i:
            body_contents.append(FlexSeparator(margin="md"))
        body_contents.append(
            FlexText(
                text=chunk,
                size="sm",
                wrap=True,
                color="#333333",
            )
        )

    bubble = FlexBubble(
        size="mega",
        styles=FlexBubbleStyles(
            header=FlexBlockStyle(background_color=header_bg),
        ),
        header=FlexBox(
            layout="vertical",
            contents=[
                FlexText(
                    text=title,
                    weight="bold",
                    color=title_color,
                    size="md",
                )
            ],
            padding_all="12px",
        ),
        body=FlexBox(
            layout="vertical",
            contents=body_contents,
            padding_all="16px",
            spacing="sm",
        ),
    )
    return FlexMessage(alt_text=_flex_alt_text(reply_text), contents=bubble)


def get_ai_response(user_msg: str, audio_bytes: bytes = None, extra_system_prompt: str = "", user_id: str = None, erp_role: str = None) -> tuple[str, list[str]]:
    from datetime import datetime
    today_str = datetime.now().strftime("%Y-%m-%d")
    current_year = datetime.now().year
    
    system_prompt = (
        f"目前的系統時間為 {today_str}，今年是 {current_year} 年。請務必以此時間基準來解讀「今年」、「目前」等詞彙。\n"
        "你是進銷存安全系統的 LINE 專屬 AI 助理，具備自主思考與多步驟推理能力。"
        "使用者是由 LINE 傳送訊息進來的（角色設定為 LINE 遠端店長）。\n"
        "【重要排版規則】：回覆內容「嚴禁」出現 `*` 或 `#` 或 `---` 等 Markdown 符號。大標題請用 (📊、📦) 等表情符號開頭。「商品名稱」或「採購單號」這類的主項目開頭【絕對不要】加上任何黑色圓點，請直接寫出文字並用『』強調。只有主項目底下的「細項（例如庫存數量、單價、狀態）」才可以加上黑色實心小圓點『●』並內縮排。段落間請空行。\n"
        "1. 先查資料再算，可連續呼叫多個工具。\n"
        "2. 問百分比/占比/毛利請用 calculate，算式僅含數字與 +-*/()。\n"
        "3. 問庫存總價值先呼叫 get_inventory_total_value 取得精確數值。\n"
        "4. 由於介面在 LINE APP 上，請嚴格遵守【重要排版規則】來回覆。\n"
        "5. 你只能查詢 LINE 低權白名單允許的非敏感營運資料；人資、薪資、財務與寫入操作均不可用。\n"
        "6. 如果你有呼叫庫存、補貨、或是全球風險相關的查詢工具，系統會自動在文字下方附上精美的卡片(Carousel)。請順著話語回答並請他參考下方的圖表即可。\n"
        "7. 若使用者提供商品名稱但工具需要產品 ID，必須主動先呼叫查詢工具找出對應 ID，絕對禁止要求使用者提供！\n"
        "8. 執行完特定工具後，你『必須』產出一段自然語言向使用者做總結回報。\n"
        "9. 若使用者查詢「全部庫存」請呼叫 get_all_inventory；若查詢「智慧庫存管理」或「需補貨」，請同時呼叫 get_low_stock_inventory（僅列出需補貨品項）與 calculate_smart_restocking（取得最新 AI 補貨建議）來一併回報。\n"
    )
    # 防注入基線（issue #47）：LINE 為公網入口，必須界定「資料 ≠ 指令」
    from backend.prompts import PROMPT_DEFENSE_BASELINE
    system_prompt += "\n" + PROMPT_DEFENSE_BASELINE
    if extra_system_prompt:
        system_prompt += "\n" + extra_system_prompt
    
    max_retries = 3
    max_turns = 8
    reply_text = ""
    
    history = []
    
    # 注入過去 6 筆對話紀錄作為記憶（大約前三輪問答）
    if user_id:
        from backend.database import run_query
        try:
            recent_logs = run_query(
                "SELECT user_msg, ai_reply FROM line_bot_logs WHERE user_id=? ORDER BY created_at DESC LIMIT 6",
                (user_id,)
            )
            if recent_logs:
                for log in reversed(recent_logs):
                    u_m, a_r = log
                    if u_m and u_m.strip():
                        history.append({"role": "user", "parts": [{"text": u_m.strip()}]})
                    if a_r and a_r.strip():
                        history.append({"role": "model", "parts": [{"text": a_r.strip()}]})
        except Exception as e:
            print(f"Error loading past context: {e}")

    # 加入當前的問題或錄音
    if audio_bytes:
        history.append({"role": "user", "parts": [
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/mp4"),
            types.Part.from_text(text="請聆聽這段聲音，這是一位經營者透過語音交代任務或詢問資訊。請你精準辨識內容後，立即呼叫工具執行，並以『文字』總結回應處理結果。")
        ]})
    else:
        history.append({"role": "user", "parts": [{"text": user_msg.strip()}]})
        
    requested_dashboards = set()
    
    for turn in range(max_turns):
        resp = None
        for attempt in range(max_retries):
            try:
                resp = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=history,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        tools=LINE_TOOLS,
                        temperature=0.2,
                        # Match the Web orchestrator: the SDK must not execute
                        # Python tools directly; every tool call goes through
                        # _build_gateway_function_response() and ToolGateway.
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    ),
                )
                break
            except Exception as e:
                import time
                if ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)) and attempt < max_retries - 1:
                    time.sleep(5 * (2 ** attempt))
                else:
                    return f"❌ 遭遇 AI API 錯誤：{e}", list(requested_dashboards)
        
        if not resp or not getattr(resp, "function_calls", None) or not resp.function_calls:
            reply_text = (getattr(resp, "text", None) or "").strip()
            break
            
        func_res = []
        for fc in resp.function_calls:
            try:
                args = dict(fc.args or {})
                _write_line_dispatch_log(user_msg, fc.name, args)
                payload, executed = _build_gateway_function_response(fc.name, args, role=erp_role)
                # 自動攔截有視覺化的工具，主動加入儀表板
                if executed and fc.name in ["get_all_inventory", "get_low_stock_inventory", "calculate_smart_restocking"]:
                    requested_dashboards.add("low_stock")
                elif executed and fc.name in ["get_supply_chain_risk_events", "get_supply_chain_heatmap_summary"]:
                    requested_dashboards.add("risk_events")
                elif executed and fc.name in ["get_carbon_emissions_by_month", "get_carbon_emissions_by_year", "get_carbon_footprint_report", "get_esg_targets"]:
                    requested_dashboards.add("chart_carbon")
                elif executed and fc.name in ["get_financial_overview", "get_ledger_summary"]:
                    requested_dashboards.add("chart_finance")
            except Exception as e:
                payload = {"status": "error", "error": f"Gateway 呼叫錯誤：{e}"}
            func_res.append(
                types.Part.from_function_response(
                    name=fc.name,
                    response=payload,
                )
            )
            
        # 必須完整保留模型的 function_calls 回覆，不可拆成純文字，否則會報錯
        mc = resp.candidates[0].content
        history.append(mc)
        history.append({"role": "user", "parts": func_res})
        
    if not reply_text and turn >= max_turns - 1:
        reply_text = "我思考了太久，請簡化您的問題或是詳細說明喔！"
        
    # 強制清理解除 Markdown 網頁語法，並改用整齊的黑色圓點
    if reply_text:
        import re
        reply_text = reply_text.replace('**', '')
        reply_text = reply_text.replace('### ', '')
        reply_text = reply_text.replace('---', '')
        
        # 1. 先處理細項：只要前面有2個以上的各種空白（全半形/Tab），遇到任何符號都替換成 ●
        reply_text = re.sub(r'(?m)^([ \t\xA0\u3000]{2,})[*🔶🔸🔷🔹■◆✔️✅👉📍➖●○\-] *', r'\1● ', reply_text)
        
        # 2. 處理主項目：只有0到1個空白起頭的符號，通通無情刪除！
        reply_text = re.sub(r'(?m)^[ \t\xA0\u3000]{0,1}[*🔶🔸🔷🔹■◆✔️✅👉📍➖●○\-]+ *', '', reply_text)
    
    return reply_text or "（無回覆內容）", list(requested_dashboards)

from fastapi import BackgroundTasks

@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    global GLOBAL_BASE_URL
    host = request.headers.get("host")
    proto = request.headers.get("x-forwarded-proto", "https")
    
    if host and "127.0.0.1" not in host and "localhost" not in host:
        GLOBAL_BASE_URL = f"{proto}://{host}"
    else:
        host = request.headers.get("x-forwarded-host", request.url.hostname)
        GLOBAL_BASE_URL = f"{proto}://{host}"
        
    print(f"DEBUG: WEBHOOK RECEIVED. BASE_URL set to: {GLOBAL_BASE_URL}")
    signature = request.headers.get('X-Line-Signature', '')
    body = await request.body()
    body_str = body.decode('utf-8')
    try:
        # 將處理推入背景任務，立刻回傳 200 OK 給 LINE，防止超時與卡死
        background_tasks.add_task(handler.handle, body_str, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return 'OK'

def _send_full_reply(event, line_bot_api, user_msg_log, reply_text, dashboards):
    user_id = event.source.user_id
    try:
        profile = line_bot_api.get_profile(user_id)
        user_name = profile.display_name
    except Exception:
        user_name = "Unknown"
        
    try:
        from backend.database import run_query
        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        run_query("INSERT INTO line_bot_logs (user_id, user_name, user_msg, ai_reply, created_at) VALUES (?, ?, ?, ?, ?)",
                  (user_id, user_name, user_msg_log, reply_text, now_str), fetch=False)
    except Exception as e:
        print(f"Error logging to DB: {e}")
        
    reply_msgs = [reply_text_to_flex_message(reply_text)]
    
    # [防呆補強] 若 AI 偷懶沒呼叫工具（幻覺），只要字眼有關鍵字就強行加入視覺化圖表
    dashboards_set = set(dashboards)
    if ("碳排" in user_msg_log or "探排" in user_msg_log or "ESG" in user_msg_log or "esg" in user_msg_log) and "chart_carbon" not in dashboards_set:
        dashboards_set.add("chart_carbon")
    if ("財務" in user_msg_log or "資產" in user_msg_log) and "chart_finance" not in dashboards_set:
        dashboards_set.add("chart_finance")
    if ("庫存" in user_msg_log or "補貨" in user_msg_log) and "low_stock" not in dashboards_set:
        dashboards_set.add("low_stock")
    if (
        "供應鏈" in user_msg_log
        or "風險" in user_msg_log
        or "熱點" in user_msg_log
        or "地緣" in user_msg_log
    ) and "risk_events" not in dashboards_set:
        dashboards_set.add("risk_events")
        
    for d in dashboards_set:
        if d == "low_stock":
            f_msg = build_low_stock_flex()
            if f_msg: reply_msgs.append(f_msg)
        elif d == "risk_events":
            f_msg = build_risk_events_flex()
            if f_msg:
                reply_msgs.append(f_msg)
            else:
                reply_msgs.append(TextMessage(text="ℹ️ 目前沒有可視覺化的風險事件資料，因此未產生卡片。"))
        elif d == "chart_carbon":
            try:
                img_name = build_carbon_trend_chart()
                base = get_ngrok_url() or GLOBAL_BASE_URL
                if img_name and base:
                    if base.endswith('/'): base = base[:-1] # 防止雙斜線
                    img_url = f"{base}/images/{img_name}"
                    reply_msgs.append(ImageMessage(original_content_url=img_url, preview_image_url=img_url))
                else:
                    reply_msgs.append(TextMessage(text=f"⚠️ 無法產生圖表：(img_name={img_name}, base={base})"))
            except Exception as e:
                reply_msgs.append(TextMessage(text=f"⚠️ 製圖引擎錯誤：{e}"))
        elif d == "chart_finance":
            try:
                img_name = build_finance_pie_chart()
                base = get_ngrok_url() or GLOBAL_BASE_URL
                if img_name and base:
                    if base.endswith('/'): base = base[:-1]
                    img_url = f"{base}/images/{img_name}"
                    reply_msgs.append(ImageMessage(original_content_url=img_url, preview_image_url=img_url))
                else:
                    reply_msgs.append(TextMessage(text=f"⚠️ 無法產生圖表：(img_name={img_name}, base={base})"))
            except Exception as e:
                reply_msgs.append(TextMessage(text=f"⚠️ 製圖引擎錯誤：{e}"))

    try:
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=reply_msgs[:5], # LINE 限定最多 5 個 Message
            )
        )
    except Exception as e:
        print(f"Error sending reply: {e}")

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        user_msg = event.message.text.strip()
        user_id = event.source.user_id
        erp_role = _get_line_user_role(user_id)

        # 強制路由：查「全部/所有」庫存時，直接走後端工具，避免被 LLM 自由改寫成舊格式
        direct_all_inventory = (
            ("庫存" in user_msg)
            and (("全部" in user_msg) or ("所有" in user_msg))
            and (("狀態" in user_msg) or ("查詢" in user_msg) or ("清單" in user_msg) or ("列表" in user_msg))
        )
        direct_smart_inventory = ("智慧庫存管理" in user_msg) or (("需補貨" in user_msg) and ("建議" in user_msg))
        
        try:
            if direct_all_inventory:
                payload, executed = _build_gateway_function_response("get_all_inventory", {}, role=erp_role)
                reply_text = _gateway_payload_to_reply(payload)
                dashboards = ["low_stock"] if executed else []
            elif direct_smart_inventory:
                low_stock_payload, low_stock_executed = _build_gateway_function_response("get_low_stock_inventory", {}, role=erp_role)
                smart_payload, smart_executed = _build_gateway_function_response("calculate_smart_restocking", {}, role=erp_role)
                low_stock_text = _gateway_payload_to_reply(low_stock_payload)
                smart_text = _gateway_payload_to_reply(smart_payload)
                reply_text = f"{low_stock_text}\n\n🤖『AI 補貨建議』\n\n{smart_text}"
                dashboards = ["low_stock"] if (low_stock_executed or smart_executed) else []
            else:
                reply_text, dashboards = get_ai_response(user_msg, user_id=user_id, erp_role=erp_role)
        except Exception as e:
            reply_text = f"❌ 抱歉，系統運作發生錯誤：{e}"
            dashboards = []
            
        _send_full_reply(event, line_bot_api, user_msg, reply_text, dashboards)


@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        blob_api = MessagingApiBlob(api_client)
        user_id = event.source.user_id
        erp_role = _get_line_user_role(user_id)
        try:
            message_content = blob_api.get_message_content(event.message.id)
            reply_text, dashboards = get_ai_response("", audio_bytes=message_content, user_id=user_id, erp_role=erp_role)
        except Exception as e:
            reply_text = f"❌ 抱歉，語音處理發生錯誤：{e}"
            dashboards = []
            
        _send_full_reply(event, line_bot_api, "[語音訊息交辦]", reply_text, dashboards)


@handler.add(PostbackEvent)
def handle_postback(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        data = event.postback.data
        user_id = event.source.user_id
        erp_role = _get_line_user_role(user_id)
        try:
            # 讓 AI 理解這是由按鈕觸發的指令
            reply_text, dashboards = get_ai_response(data, extra_system_prompt="使用者剛按下了一鍵觸發按鈕，請根據該按鈕指令執行相關作業並回報。", user_id=user_id, erp_role=erp_role)
        except Exception as e:
            reply_text = f"❌ 抱歉，快捷觸發發生錯誤：{e}"
            dashboards = []
            
        _send_full_reply(event, line_bot_api, f"[按鈕觸發] {data}", reply_text, dashboards)


async def execute_morning_briefing():
    if not LINE_BRIEFING_ENABLED:
        print("Morning briefing skipped: LINE_BRIEFING_ENABLED is off.")
        return {"status": "skipped", "reason": "disabled", "sent": 0}
    if not LINE_BRIEFING_USER_IDS:
        print("Morning briefing skipped: LINE_BRIEFING_USER_IDS is empty.")
        return {"status": "skipped", "reason": "no_recipients", "sent": 0}

    try:
        print("Starting morning briefing generation...")
        prompt = "這是固定的每日早報排程。請只使用 LINE 低權白名單工具，彙整庫存狀態、採購情形、風險事件與碳排，產出【今日營運總結早報】。不得查詢或推測人資、薪資或財務資料。請主動列出應注意的風險或低庫存品項。"
        
        reply_text, dashboards = await asyncio.to_thread(get_ai_response, prompt)
        
        if not reply_text:
            reply_text = "今日暫無早報資訊可提供。"
            
        reply_msgs = [reply_text_to_flex_message(reply_text, title="☀️ 營運早報主動推播")]
        
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            sent = 0
            failed = 0
            for user_id in LINE_BRIEFING_USER_IDS:
                try:
                    line_bot_api.push_message(
                        PushMessageRequest(to=user_id, messages=reply_msgs[:5])
                    )
                    sent += 1
                except Exception as e:
                    failed += 1
                    print(f"Morning briefing push failed for one allowlisted user: {e}")
            print(f"Morning briefing finished: sent={sent}, failed={failed}.")
            return {"status": "completed", "sent": sent, "failed": failed}
                
    except Exception as e:
        print(f"Error in execute_morning_briefing: {e}")
        return {"status": "error", "sent": 0, "error": str(e)}

@app.get("/trigger_morning_briefing")
async def trigger_morning_briefing():
    """測試端點：只會推給明確設定的內部 allowlist。"""
    if not LINE_BRIEFING_ENABLED or not LINE_BRIEFING_USER_IDS:
        raise HTTPException(status_code=503, detail="Morning briefing is disabled or has no recipients.")
    # 建立背景任務避免阻擋 HTTP Response
    asyncio.create_task(execute_morning_briefing())
    return {
        "status": "success",
        "message": "Morning briefing task has been scheduled for allowlisted internal users.",
        "recipient_count": len(LINE_BRIEFING_USER_IDS),
    }

@app.on_event("startup")
async def startup_event():
    if not LINE_BRIEFING_ENABLED or not LINE_BRIEFING_USER_IDS:
        print("Morning briefing scheduler is disabled or has no allowlisted recipients.")
        return

    async def schedule_loop():
        while True:
            from datetime import datetime
            now = datetime.now()
            # 每天早上 08:30 推播
            if now.hour == 8 and now.minute == 30:
                await execute_morning_briefing()
                # 睡 60 秒避免同一分鐘內重複觸發
                await asyncio.sleep(60)
            await asyncio.sleep(30)
            
    asyncio.create_task(schedule_loop())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
