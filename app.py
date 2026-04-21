"""
J SIMPLE 高架床 LINE Bot
- Flask webhook 伺服器
- Claude AI 智慧判斷問題類型
- 自動回覆客戶詢問
"""

import os
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from anthropic import Anthropic
from knowledge_base import REPLIES, BRAND_INFO

app = Flask(__name__)

LINE_CHANNEL_SECRET      = os.getenv("LINE_CHANNEL_SECRET", "ed4319138fed1c6db548b60327e2d69d")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "S/R1BB9ByxtJ5CXr4kbbj51Xkz7S9kfxYIjzYsqDvjzAYHXLc6aOJQq6eDO5j7Me3SVGrkkpPeX0OH5tUHYnjGyO/S4WDRYlOWoIPIJplSUUCNX0FmeCnPhizFaUSnPNIw2uyvV016cyuO1jtO5dZQdB04t89/1O/w1cDnyilFU=")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
anthropic_client = Anthropic()

SYSTEM_PROMPT = f"""你是 J SIMPLE 高架床品牌的客服助理，專門回答高架床相關問題。

品牌資訊：
- LINE ID：{BRAND_INFO['line_id']}
- 官網：{BRAND_INFO['website']}
- 價格範圍：{BRAND_INFO['price_range']}
- 保固：{BRAND_INFO['warranty']}
- 現貨交期：{BRAND_INFO['delivery_stock']}
- 訂製交期：{BRAND_INFO['delivery_custom']}

回覆原則：
1. 繁體中文，語氣親切但專業
2. 盡量在 150 字以內
3. 複雜問題（尺寸規劃、訂製報價）引導加 LINE @JSIMPLE
4. 不知道的問題不要亂猜，引導加 LINE
5. 結尾適時附上官網或 LINE 連結

可回答範圍：價格、尺寸、材質、運費、出貨、保固、訂製說明。
"""

def classify_intent(text: str) -> str:
    keywords = {
        "price":    ["價格", "多少錢", "費用", "報價", "貴不貴", "預算"],
        "custom":   ["訂製", "客製", "客製化", "尺寸訂做", "特殊尺寸"],
        "shipping": ["運費", "運送", "安裝", "搬運", "配送", "送貨"],
        "size":     ["尺寸", "幾尺", "多大", "寬度", "長度", "高度", "天花板"],
        "delivery": ["幾天", "出貨", "交期", "到貨", "多久"],
        "warranty": ["保固", "保證", "壞掉", "維修"],
        "greeting": ["你好", "您好", "hi", "hello", "詢問", "想問"],
    }
    text_lower = text.lower()
    for intent, words in keywords.items():
        if any(w in text_lower for w in words):
            return intent
    return "ai"

def get_ai_reply(user_message: str) -> str:
    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )
        return response.content[0].text
    except Exception:
        return REPLIES["default"]

def get_reply(user_message: str) -> str:
    intent = classify_intent(user_message)
    if intent == "ai":
        return get_ai_reply(user_message)
    return REPLIES.get(intent, REPLIES["default"])

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    reply_text = get_reply(user_text)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
