from linebot.v3.messaging import (
    FlexMessage,
    FlexContainer,
    FlexCarousel,
    FlexBubble,
    FlexBox,
    FlexText,
    FlexIcon,
    FlexSpan,
    FlexButton,
    FlexSeparator,
    PostbackAction,
)
from .database import run_query
import json

def build_low_stock_flex() -> FlexMessage:
    """產生低於安全水位的庫存 Flex Carousel"""
    res = run_query("SELECT product_id, name, stock, reorder_point FROM inventory WHERE stock <= reorder_point")
    
    if not res:
        return None # 沒有危險庫存
        
    bubbles = []
    for r in res:
        p_id, name, stock, reorder = r
        percentage = int(stock / reorder * 100) if reorder > 0 else 0
        
        # 決定顏色
        bar_color = "#FF334B" if percentage < 50 else "#FF7B52"
        
        bubble = FlexBubble(
            size="kilo",
            body=FlexBox(
                layout="vertical",
                contents=[
                    FlexText(text="🚨 庫存告急", weight="bold", color=bar_color, size="sm"),
                    FlexText(text=name, weight="bold", size="xl", margin="md", wrap=True),
                    FlexText(text=f"編號: {p_id}", color="#aaaaaa", size="xs"),
                    FlexSeparator(margin="xxl"),
                    FlexBox(
                        layout="vertical",
                        margin="xxl",
                        spacing="sm",
                        contents=[
                            FlexBox(
                                layout="horizontal",
                                contents=[
                                    FlexText(text="剩餘庫存", size="sm", color="#555555", flex=0),
                                    FlexText(text=f"{stock} 件", size="sm", color="#111111", align="end")
                                ]
                            ),
                            FlexBox(
                                layout="horizontal",
                                contents=[
                                    FlexText(text="安全水位", size="sm", color="#555555", flex=0),
                                    FlexText(text=f"{reorder} 件", size="sm", color="#111111", align="end")
                                ]
                            ),
                            # ProgressBar
                            FlexBox(
                                layout="vertical",
                                margin="md",
                                contents=[
                                    FlexBox(
                                        layout="vertical",
                                        width=f"{min(100, percentage)}%",
                                        height="6px",
                                        background_color=bar_color,
                                        contents=[]
                                    )
                                ],
                                background_color="#e2e2e2",
                                height="6px",
                                corner_radius="3px"
                            )
                        ]
                    ),
                    FlexSeparator(margin="xl"),
                    FlexButton(
                        style="primary",
                        color=bar_color,
                        margin="xl",
                        action=PostbackAction(
                            label="一鍵補貨支援",
                            data=f"一鍵執行: 立即為 {name}({p_id}) 補貨",
                            display_text=f"自動補貨: {name}"
                        )
                    )
                ]
            )
        )
        bubbles.append(bubble)
        if len(bubbles) >= 12: # LINE Carousel 最大 12 個
            break
            
    carousel = FlexCarousel(contents=bubbles)
    return FlexMessage(alt_text="低庫存警報清單", contents=carousel)

def build_risk_events_flex() -> FlexMessage:
    """產生全球風險事件 Flex Carousel"""
    res = run_query("SELECT event_type, region, country, impact_days, description FROM supply_chain_events ORDER BY impact_days DESC LIMIT 10")
    if not res:
        return None
        
    bubbles = []
    for r in res:
        e_type, region, country, impact, desc = r
        
        bubble = FlexBubble(
            size="kilo",
            body=FlexBox(
                layout="vertical",
                contents=[
                    FlexText(text="🌍 全球供應鏈風險", weight="bold", color="#FFA500", size="sm"),
                    FlexText(text=e_type, weight="bold", size="xl", margin="md", wrap=True),
                    FlexBox(
                        layout="baseline",
                        margin="md",
                        contents=[
                            FlexText(text="地點", color="#aaaaaa", size="sm", flex=1),
                            FlexText(text=f"{region} - {country}", weight="bold", size="sm", color="#333333", flex=4)
                        ]
                    ),
                    FlexBox(
                        layout="baseline",
                        margin="md",
                        contents=[
                            FlexText(text="延遲", color="#aaaaaa", size="sm", flex=1),
                            FlexText(text=f"約 {impact} 天", weight="bold", size="sm", color="#F03A17", flex=4)
                        ]
                    ),
                    FlexText(text=desc, margin="lg", size="xs", color="#666666", wrap=True, max_lines=3)
                ]
            )
        )
        bubbles.append(bubble)
        
    carousel = FlexCarousel(contents=bubbles)
    return FlexMessage(alt_text="全球供應鏈風險快報", contents=carousel)

def show_visual_dashboard(dashboard_type: str) -> str:
    """呼叫此工具可在 LINE 上顯示指定庫存或風險的精美視覺化卡片(Carousel)。
    支援的 dashboard_type:
    - 'low_stock' : 列出所有低於安全水位需緊急補貨的危險庫存卡片。
    - 'risk_events' : 列出全球供應鏈風險事件地圖卡片。
    當使用者詢問危險庫存、低水位庫存，或是全球供應鏈風險時，請主動呼叫此工具來顯示更好的視覺介面給使用者。
    """
    return f"FLEX_DASHBOARD_REQUESTED:{dashboard_type}"
