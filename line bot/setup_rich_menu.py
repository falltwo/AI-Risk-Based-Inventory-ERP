import os
import requests
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    RichMenuRequest,
    RichMenuArea,
    RichMenuBounds,
    RichMenuSize,
    MessageAction,
)

# 導入 Token
from bot_server import LINE_CHANNEL_ACCESS_TOKEN

def create_rich_menu():
    configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        print("[1] 建立 Rich Menu ...")
        rich_menu_to_create = RichMenuRequest(
            size=RichMenuSize(width=2500, height=1686),
            selected=True,
            name="ERP 智能管理選單",
            chat_bar_text="打開選單",
            areas=[
                # Row 1
                RichMenuArea(
                    bounds=RichMenuBounds(x=0, y=0, width=833, height=843),
                    action=MessageAction(label="庫存查詢", text="請幫我列出全部產品的庫存狀態")
                ),
                RichMenuArea(
                    bounds=RichMenuBounds(x=833, y=0, width=833, height=843),
                    action=MessageAction(label="智慧補貨", text="請根據過去銷售計算動態安全庫存，並提供 AI 智慧補貨建議")
                ),
                RichMenuArea(
                    bounds=RichMenuBounds(x=1666, y=0, width=834, height=843),
                    action=MessageAction(label="訂單管理", text="查詢最近的訂單狀態及應收帳款")
                ),
                # Row 2
                RichMenuArea(
                    bounds=RichMenuBounds(x=0, y=843, width=833, height=843),
                    action=MessageAction(label="財務總覽", text="顯示財務總覽與資產負債概況")
                ),
                RichMenuArea(
                    bounds=RichMenuBounds(x=833, y=843, width=833, height=843),
                    action=MessageAction(label="ESG碳排", text="提供碳足跡報告與碳排趨勢")
                ),
                RichMenuArea(
                    bounds=RichMenuBounds(x=1666, y=843, width=834, height=843),
                    action=MessageAction(label="全球風險", text="列出目前全球供應鏈風險事件與受影響訂單")
                )
            ]
        )

        rich_menu_id = line_bot_api.create_rich_menu(rich_menu_to_create).rich_menu_id
        print(f"Rich Menu ID: {rich_menu_id}")

        print("[2] 調整並上傳 Rich Menu 圖片 ...")
        image_path = r"C:\Users\User\.gemini\antigravity\brain\6c4ac423-da28-4a64-b17b-cbd59b41e3a3\erp_rich_menu_flat_1775320445371.png"
        
        try:
            from PIL import Image
            with Image.open(image_path) as img:
                img_resized = img.resize((2500, 1686)).convert('RGB')
                resized_path = image_path.replace(".png", "_resized.jpg")
                img_resized.save(resized_path, "JPEG", quality=85)
        except ImportError:
            print("請先安裝 Pillow: pip install Pillow")
            return

        headers = {
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "image/jpeg",
        }
        with open(resized_path, "rb") as f:
            response = requests.post(
                f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
                headers=headers,
                data=f
            )
            print(f"Upload Image Result: {response.status_code} {response.text}")
            
            if response.status_code != 200:
                print("❌ 圖片上傳失敗，已終止後續設定")
                return

        print("[3] 設定為預設 Rich Menu ...")
        line_bot_api.set_default_rich_menu(rich_menu_id)
        print("✅ 完成設定！現在進入 LINE Bot，就可以看到 6 格圖文選單了。")

if __name__ == "__main__":
    create_rich_menu()
