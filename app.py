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
from knowledge_base import REPLIES, BRAND_INFO

app = Flask(__name__)

LINE_CHANNEL_SECRET      = os.getenv("LINE_CHANNEL_SECRET", "ed4319138fed1c6db548b60327e2d69d")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "S/R1BB9ByxtJ5CXr4kbbj51Xkz7S9kfxYIjzYsqDvjzAYHXLc6aOJQq6eDO5j7Me3SVGrkkpPeX0OH5tUHYnjGyO/S4WDRYlOWoIPIJplSUUCNX0FmeCnPhizFaUSnPNIw2uyvV016cyuO1jtO5dZQdB04t89/1O/w1cDnyilFU=")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def classify_intent(text: str) -> str:
    keywords = {
        "price":    ["價格", "多少錢", "費用", "報價", "貴不貴", "預算", "幾千", "便宜", "優惠", "折扣", "特價"],
        "custom":   ["訂製", "客製", "客製化", "尺寸訂做", "特殊尺寸", "訂做", "可以改", "能不能改", "調整"],
        "shipping": ["運費", "運送", "安裝費", "搬運", "配送", "送貨", "怎麼安裝", "自己裝", "組裝", "師傅"],
        "size":     ["尺寸", "幾尺", "多大", "寬度", "長度", "高度", "天花板", "幾公分", "cm", "幾米", "空間", "放得下"],
        "delivery": ["幾天", "出貨", "交期", "到貨", "多久", "等多久", "快嗎", "現貨"],
        "warranty": ["保固", "保證", "壞掉", "維修", "故障", "生鏽", "鏽", "螺絲鬆"],
        "material": ["材質", "鋼管", "鐵", "木", "板材", "幾mm", "厚度", "堅固", "穩", "晃", "承重", "幾公斤", "kg", "重量限制", "安全"],
        "color":    ["顏色", "黑色", "白色", "什麼色", "有哪些色", "款式", "外觀", "樣式", "型號"],
        "payment":  ["付款", "匯款", "刷卡", "信用卡", "轉帳", "分期", "Line Pay", "linepay", "pay", "怎麼付"],
        "return":   ["退貨", "換貨", "退款", "不喜歡", "不符合", "取消", "退"],
        "greeting": ["你好", "您好", "hi", "hello", "詢問", "想問", "請問", "想了解", "看一下", "查詢"],
    }
    text_lower = text.lower()
    for intent, words in keywords.items():
        if any(w in text_lower for w in words):
            return intent
    return "default"

def get_reply(user_message: str) -> str:
    intent = classify_intent(user_message)
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
