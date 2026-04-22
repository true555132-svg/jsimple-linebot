"""
J SIMPLE 高架床 LINE Bot
- Flask webhook 伺服器
- 關鍵字自動回覆
- /admin 後台可線上編輯回覆文案
"""

import os
import json
import base64
import urllib.request
import urllib.parse
from flask import Flask, request, abort, render_template_string, redirect, url_for, session
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from knowledge_base import REPLIES as DEFAULT_REPLIES, BRAND_INFO

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "jsimple-admin-2024")

LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "ed4319138fed1c6db548b60327e2d69d")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "S/R1BB9ByxtJ5CXr4kbbj51Xkz7S9kfxYIjzYsqDvjzAYHXLc6aOJQq6eDO5j7Me3SVGrkkpPeX0OH5tUHYnjGyO/S4WDRYlOWoIPIJplSUUCNX0FmeCnPhizFaUSnPNIw2uyvV016cyuO1jtO5dZQdB04t89/1O/w1cDnyilFU=")
ADMIN_PASSWORD            = os.getenv("ADMIN_PASSWORD", "jsimple2024")
GITHUB_TOKEN              = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO               = "true555132-svg/jsimple-linebot"
GITHUB_FILE               = "knowledge_base.py"

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 執行期間可覆寫的回覆字典（重啟後還原，除非已 commit 到 GitHub）
custom_replies = dict(DEFAULT_REPLIES)

REPLY_LABELS = {
    "greeting": "打招呼",
    "price":    "價格詢問",
    "custom":   "訂製/客製",
    "shipping": "運費/安裝",
    "size":     "尺寸詢問",
    "delivery": "出貨時間",
    "warranty": "保固",
    "material": "材質/安全",
    "color":    "顏色/款式",
    "payment":  "付款方式",
    "return":   "退換貨",
    "default":  "預設回覆",
}

# ── 關鍵字分類 ────────────────────────────────────────────

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
    return custom_replies.get(intent, custom_replies["default"])

# ── LINE Webhook ──────────────────────────────────────────

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

# ── Admin 後台 ────────────────────────────────────────────

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>J SIMPLE Bot 後台</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #f5f5f5; color: #333; }
  .header { background: #1a1a1a; color: #fff; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  .header h1 { font-size: 18px; }
  .badge { background: #00c300; color: #fff; font-size: 12px; padding: 3px 10px; border-radius: 12px; }
  .container { max-width: 900px; margin: 24px auto; padding: 0 16px; }
  .card { background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  .card-label { font-size: 13px; color: #888; margin-bottom: 6px; }
  .card-title { font-size: 16px; font-weight: 600; margin-bottom: 12px; }
  textarea { width: 100%; border: 1px solid #ddd; border-radius: 8px; padding: 12px; font-size: 14px; line-height: 1.6; resize: vertical; min-height: 120px; font-family: inherit; }
  textarea:focus { outline: none; border-color: #00c300; }
  .btn-row { position: sticky; bottom: 0; background: #fff; border-top: 1px solid #eee; padding: 16px; text-align: center; }
  .btn { padding: 12px 32px; border: none; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; }
  .btn-save { background: #00c300; color: #fff; margin-right: 12px; }
  .btn-deploy { background: #1a1a1a; color: #fff; }
  .btn:hover { opacity: .85; }
  .flash { padding: 12px 20px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }
  .flash.ok  { background: #e8f5e9; color: #2e7d32; }
  .flash.err { background: #fdecea; color: #c62828; }
  .login-wrap { max-width: 360px; margin: 80px auto; background: #fff; border-radius: 16px; padding: 40px; box-shadow: 0 2px 16px rgba(0,0,0,.1); text-align: center; }
  .login-wrap h2 { margin-bottom: 24px; }
  .login-wrap input { width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 15px; margin-bottom: 16px; }
  .login-wrap .btn { width: 100%; background: #00c300; color: #fff; }
</style>
</head>
<body>
{% if not logged_in %}
<div class="login-wrap">
  <h2>🔐 J SIMPLE 後台登入</h2>
  <form method="POST" action="/admin/login">
    <input type="password" name="password" placeholder="請輸入密碼" autofocus>
    <button class="btn" type="submit">登入</button>
  </form>
  {% if error %}<p style="color:red;margin-top:12px">{{ error }}</p>{% endif %}
</div>
{% else %}
<div class="header">
  <h1>J SIMPLE Bot 回覆管理</h1>
  <span class="badge">LINE @jsimple</span>
</div>
<div class="container">
  {% if flash %}<div class="flash {{ flash_type }}">{{ flash }}</div>{% endif %}
  <form method="POST" action="/admin/save">
    {% for key, label in labels.items() %}
    <div class="card">
      <div class="card-label">{{ key }}</div>
      <div class="card-title">{{ label }}</div>
      <textarea name="{{ key }}">{{ replies[key] }}</textarea>
    </div>
    {% endfor %}
    <div class="btn-row">
      <button class="btn btn-save" type="submit" name="action" value="save">💾 儲存（本次生效）</button>
      <button class="btn btn-deploy" type="submit" name="action" value="deploy">🚀 儲存並部署（永久生效）</button>
    </div>
  </form>
</div>
{% endif %}
</body>
</html>
"""

@app.route("/admin")
def admin():
    logged_in = session.get("admin")
    flash = session.pop("flash", "")
    flash_type = session.pop("flash_type", "ok")
    return render_template_string(ADMIN_HTML,
        logged_in=logged_in,
        replies=custom_replies,
        labels=REPLY_LABELS,
        flash=flash,
        flash_type=flash_type,
        error=None)

@app.route("/admin/login", methods=["POST"])
def admin_login():
    if request.form.get("password") == ADMIN_PASSWORD:
        session["admin"] = True
        return redirect("/admin")
    return render_template_string(ADMIN_HTML,
        logged_in=False, error="密碼錯誤", replies={}, labels={}, flash="", flash_type="")

@app.route("/admin/save", methods=["POST"])
def admin_save():
    if not session.get("admin"):
        return redirect("/admin")
    for key in REPLY_LABELS:
        if key in request.form:
            custom_replies[key] = request.form[key]
    action = request.form.get("action", "save")
    if action == "deploy" and GITHUB_TOKEN:
        ok, msg = commit_to_github()
        session["flash"] = msg
        session["flash_type"] = "ok" if ok else "err"
    else:
        session["flash"] = "✅ 已儲存，立即生效（重啟後還原，請用「儲存並部署」永久生效）"
        session["flash_type"] = "ok"
    return redirect("/admin")

def commit_to_github() -> tuple:
    try:
        content = build_knowledge_base_py()
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        })
        with urllib.request.urlopen(req) as r:
            sha = json.loads(r.read())["sha"]
        payload = json.dumps({
            "message": "admin: update replies",
            "content": base64.b64encode(content.encode()).decode(),
            "sha": sha,
        }).encode()
        req2 = urllib.request.Request(url, data=payload, method="PUT", headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
        })
        urllib.request.urlopen(req2)
        return True, "🚀 已部署！Render 重新部署中（約 2 分鐘後生效）"
    except Exception as e:
        return False, f"❌ 部署失敗：{e}"

def build_knowledge_base_py() -> str:
    lines = ['"""\nJ SIMPLE 高架床 LINE Bot 知識庫\n"""\n\n']
    lines.append("BRAND_INFO = {\n")
    for k, v in BRAND_INFO.items():
        lines.append(f'    "{k}": "{v}",\n')
    lines.append("}\n\nSHIPPING = {\n    \"north\": 1000,\n    \"central_south\": 1300,\n    \"floor_surcharge\": 300,\n    \"elevator_surcharge\": 300,\n}\n\nREPLIES = {\n")
    for k, v in custom_replies.items():
        escaped = v.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        lines.append(f'    "{k}": """{escaped}""",\n\n')
    lines.append("}\n")
    return "".join(lines)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
