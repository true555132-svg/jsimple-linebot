"""
J SIMPLE 高架床 Bot
- LINE + FB Messenger Webhook
- 關鍵字自動回覆 + 5 分鐘冷卻
- /admin?key=密碼  後台（回覆/關鍵字/測試/紀錄）
"""

import os
import json
import base64
import urllib.request
import time
from collections import deque
from flask import Flask, request, abort, render_template_string, redirect, jsonify
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from knowledge_base import REPLIES as DEFAULT_REPLIES, BRAND_INFO

app = Flask(__name__)

LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "ed4319138fed1c6db548b60327e2d69d")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "S/R1BB9ByxtJ5CXr4kbbj51Xkz7S9kfxYIjzYsqDvjzAYHXLc6aOJQq6eDO5j7Me3SVGrkkpPeX0OH5tUHYnjGyO/S4WDRYlOWoIPIJplSUUCNX0FmeCnPhizFaUSnPNIw2uyvV016cyuO1jtO5dZQdB04t89/1O/w1cDnyilFU=")
FB_PAGE_ACCESS_TOKEN      = os.getenv("FB_PAGE_ACCESS_TOKEN", "")
FB_VERIFY_TOKEN           = "jsimple2024"
ADMIN_PASSWORD            = os.getenv("ADMIN_PASSWORD", "jsimple2024")
GITHUB_TOKEN              = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO               = "true555132-svg/jsimple-linebot"
GITHUB_FILE               = "knowledge_base.py"

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

custom_replies = dict(DEFAULT_REPLIES)

COOLDOWN_SECONDS = 300
line_cooldown: dict = {}
fb_cooldown: dict = {}

message_log = deque(maxlen=100)

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

custom_keywords = {
    "price":    ["價格", "多少錢", "費用", "報價", "貴不貴", "預算", "幾千", "便宜", "優惠", "折扣", "特價"],
    "custom":   ["訂製", "客製", "客製化", "尺寸訂做", "特殊尺寸", "訂做", "可以改", "能不能改", "調整"],
    "shipping": ["運費", "運送", "安裝費", "搬運", "配送", "送貨", "怎麼安裝", "自己裝", "組裝", "師傅"],
    "size":     ["尺寸", "幾尺", "多大", "寬度", "長度", "高度", "天花板", "幾公分", "cm", "幾米", "空間", "放得下"],
    "delivery": ["幾天", "出貨", "交期", "到貨", "多久", "等多久", "快嗎", "現貨", "庫存", "有貨", "現在有"],
    "warranty": ["保固", "保證", "壞掉", "維修", "故障", "生鏽", "鏽", "螺絲鬆"],
    "material": ["材質", "鋼管", "鐵", "木", "板材", "幾mm", "厚度", "堅固", "穩", "晃", "承重", "幾公斤", "kg", "重量限制", "安全"],
    "color":    ["顏色", "黑色", "白色", "什麼色", "有哪些色", "款式", "外觀", "樣式", "型號"],
    "payment":  ["付款", "匯款", "刷卡", "信用卡", "轉帳", "分期", "Line Pay", "linepay", "pay", "怎麼付"],
    "return":   ["退貨", "換貨", "退款", "不喜歡", "不符合", "取消", "退"],
    "greeting": ["你好", "您好", "hi", "hello", "詢問", "想問", "請問", "想了解", "看一下", "查詢"],
}

# ── 共用邏輯 ─────────────────────────────────────────────

def classify_intent(text: str) -> str:
    text_lower = text.lower()
    for intent, words in custom_keywords.items():
        if any(w in text_lower for w in words):
            return intent
    return "default"

def get_reply(text: str, user_id: str, cooldown_store: dict, channel: str = "") -> str | None:
    intent = classify_intent(text)
    now = time.time()
    user_times = cooldown_store.setdefault(user_id, {})
    cooled = now - user_times.get(intent, 0) < COOLDOWN_SECONDS
    reply_text = custom_replies.get(intent, custom_replies["default"])
    message_log.appendleft({
        "time": time.strftime("%m/%d %H:%M", time.localtime(now)),
        "channel": channel,
        "msg": text[:40],
        "intent": intent,
        "replied": not cooled,
    })
    if cooled:
        return None
    user_times[intent] = now
    return reply_text

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
def handle_line_message(event):
    reply_text = get_reply(event.message.text.strip(), event.source.user_id, line_cooldown, "LINE")
    if reply_text is None:
        return
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

# ── FB Messenger Webhook ──────────────────────────────────

@app.route("/fb-webhook", methods=["GET"])
def fb_verify():
    if (request.args.get("hub.mode") == "subscribe" and
            request.args.get("hub.verify_token") == FB_VERIFY_TOKEN):
        return request.args.get("hub.challenge", ""), 200
    abort(403)

@app.route("/fb-webhook", methods=["POST"])
def fb_webhook():
    data = request.get_json(silent=True) or {}
    if data.get("object") == "page":
        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):
                sender_id = event.get("sender", {}).get("id", "")
                msg = event.get("message", {})
                if sender_id and "text" in msg:
                    reply_text = get_reply(msg["text"].strip(), sender_id, fb_cooldown, "FB")
                    if reply_text:
                        fb_send(sender_id, reply_text)
    return "OK", 200

def fb_send(psid: str, text: str):
    if not FB_PAGE_ACCESS_TOKEN:
        return
    url = f"https://graph.facebook.com/v19.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
    payload = json.dumps({"recipient": {"id": psid}, "message": {"text": text}}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass

# ── Admin API ─────────────────────────────────────────────

@app.route("/api/test", methods=["POST"])
def api_test():
    key = request.args.get("key", "")
    if key != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 403
    text = (request.get_json(silent=True) or {}).get("text", "")
    intent = classify_intent(text)
    reply = custom_replies.get(intent, custom_replies["default"])
    return jsonify({"intent": intent, "label": REPLY_LABELS.get(intent, intent), "reply": reply})

@app.route("/api/logs", methods=["GET"])
def api_logs():
    key = request.args.get("key", "")
    if key != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(list(message_log))

# ── Admin 後台 ────────────────────────────────────────────

ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>J SIMPLE Bot 後台</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333;font-size:15px}
.header{background:#1a1a1a;color:#fff;padding:14px 20px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.header h1{font-size:17px;font-weight:700}
.badges{display:flex;gap:6px}
.badge{font-size:11px;padding:3px 9px;border-radius:12px;font-weight:600}
.badge-line{background:#00c300;color:#fff}
.badge-fb{background:#1877f2;color:#fff}
.tabs{display:flex;background:#fff;border-bottom:2px solid #eee;overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{padding:12px 20px;cursor:pointer;white-space:nowrap;font-weight:600;color:#888;border-bottom:3px solid transparent;margin-bottom:-2px;transition:.15s}
.tab.active{color:#00c300;border-bottom-color:#00c300}
.tab-content{display:none}
.tab-content.active{display:block}
.container{max-width:860px;margin:20px auto;padding:0 14px 90px}
.card{background:#fff;border-radius:12px;padding:18px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.card-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.card-title{font-size:15px;font-weight:700}
.card-label{font-size:11px;color:#bbb;background:#f5f5f5;padding:2px 8px;border-radius:8px}
textarea{width:100%;border:1px solid #e0e0e0;border-radius:8px;padding:11px;font-size:14px;line-height:1.75;resize:vertical;min-height:100px;font-family:inherit;transition:.15s}
textarea:focus{outline:none;border-color:#00c300;box-shadow:0 0 0 3px rgba(0,195,0,.08)}
.kw-input{width:100%;border:1px solid #e0e0e0;border-radius:8px;padding:9px 11px;font-size:14px;font-family:inherit}
.kw-input:focus{outline:none;border-color:#00c300}
.kw-hint{font-size:12px;color:#aaa;margin-top:5px}
.btn-row{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #eee;padding:11px 16px;display:flex;gap:10px;justify-content:center;z-index:100}
.btn{padding:11px 24px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:.15s}
.btn:hover{opacity:.85}
.btn-save{background:#00c300;color:#fff}
.btn-deploy{background:#1a1a1a;color:#fff}
.btn-test-send{background:#1877f2;color:#fff;width:100%;margin-top:10px;padding:12px}
.flash{padding:11px 18px;border-radius:8px;margin-bottom:14px;font-size:14px}
.ok{background:#e8f5e9;color:#2e7d32}
.err{background:#fdecea;color:#c62828}
.login-wrap{max-width:340px;margin:80px auto;background:#fff;border-radius:16px;padding:36px;box-shadow:0 2px 16px rgba(0,0,0,.1);text-align:center}
.login-wrap h2{margin-bottom:22px;font-size:19px}
.login-wrap input{width:100%;padding:11px;border:1px solid #ddd;border-radius:8px;font-size:15px;margin-bottom:14px}
.login-wrap .btn{width:100%;background:#00c300;color:#fff}
.err-msg{color:red;margin-top:10px;font-size:13px}
.test-box{background:#fff;border-radius:12px;padding:18px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.test-input{width:100%;border:1px solid #ddd;border-radius:8px;padding:11px;font-size:15px;font-family:inherit}
.test-input:focus{outline:none;border-color:#1877f2}
.test-result{margin-top:14px;display:none}
.test-result .intent-badge{display:inline-block;background:#e3f2fd;color:#1565c0;padding:3px 10px;border-radius:10px;font-size:12px;font-weight:600;margin-bottom:8px}
.test-result .reply-text{background:#f5f5f5;border-radius:8px;padding:12px;font-size:14px;line-height:1.7;white-space:pre-wrap}
.log-table{width:100%;border-collapse:collapse;font-size:13px}
.log-table th{text-align:left;padding:8px 10px;background:#f9f9f9;color:#888;font-weight:600;border-bottom:2px solid #eee}
.log-table td{padding:9px 10px;border-bottom:1px solid #f0f0f0;vertical-align:top}
.log-table tr:last-child td{border-bottom:none}
.ch-line{color:#00c300;font-weight:700;font-size:12px}
.ch-fb{color:#1877f2;font-weight:700;font-size:12px}
.replied-yes{color:#aaa;font-size:12px}
.replied-no{color:#00c300;font-weight:600;font-size:12px}
.empty-log{text-align:center;color:#bbb;padding:40px;font-size:14px}
@media(max-width:600px){
  .tab{padding:10px 14px;font-size:13px}
  .btn{padding:10px 16px;font-size:13px}
}
</style>
</head>
<body>
{% if not auth %}
<div class="login-wrap">
  <h2>🔐 J SIMPLE 後台</h2>
  <form method="POST" action="{{ login_action }}">
    <input type="password" name="password" placeholder="請輸入密碼" autofocus>
    <button class="btn" type="submit">登入</button>
  </form>
  {% if error %}<p class="err-msg">{{ error }}</p>{% endif %}
</div>
{% else %}
<div class="header">
  <h1>⚡ Bot 後台</h1>
  <div class="badges">
    <span class="badge badge-line">LINE</span>
    <span class="badge badge-fb">FB Messenger</span>
  </div>
</div>
<div class="tabs">
  <div class="tab active" onclick="switchTab('replies',this)">💬 回覆內容</div>
  <div class="tab" onclick="switchTab('keywords',this)">🏷️ 關鍵字</div>
  <div class="tab" onclick="switchTab('test',this)">🧪 測試</div>
  <div class="tab" onclick="switchTab('logs',this)" id="logs-tab">📋 紀錄</div>
</div>

<div class="container">
  {% if flash %}<div class="flash {{ flash_type }}">{{ flash }}</div>{% endif %}

  <!-- 回覆內容 -->
  <div id="tab-replies" class="tab-content active">
    <form method="POST" action="{{ save_action }}" id="replies-form">
      {% for id, label in labels.items() %}
      <div class="card">
        <div class="card-head">
          <div class="card-title">{{ label }}</div>
          <span class="card-label">{{ id }}</span>
        </div>
        <textarea name="{{ id }}">{{ replies[id] }}</textarea>
      </div>
      {% endfor %}
      <div class="btn-row">
        <button class="btn btn-save" type="submit" name="action" value="save">💾 儲存</button>
        <button class="btn btn-deploy" type="submit" name="action" value="deploy">🚀 部署</button>
      </div>
    </form>
  </div>

  <!-- 關鍵字 -->
  <div id="tab-keywords" class="tab-content">
    <form method="POST" action="{{ kw_save_action }}" id="kw-form">
      {% for id, label in labels.items() %}
      {% if id != 'default' %}
      <div class="card">
        <div class="card-head">
          <div class="card-title">{{ label }}</div>
          <span class="card-label">{{ id }}</span>
        </div>
        <input class="kw-input" type="text" name="{{ id }}" value="{{ keywords[id]|join(', ') }}" placeholder="關鍵字1, 關鍵字2, ...">
        <div class="kw-hint">逗號分隔，不分大小寫</div>
      </div>
      {% endif %}
      {% endfor %}
      <div class="btn-row">
        <button class="btn btn-save" type="submit">💾 儲存關鍵字</button>
      </div>
    </form>
  </div>

  <!-- 測試 -->
  <div id="tab-test" class="tab-content">
    <div class="test-box">
      <div class="card-title" style="margin-bottom:12px">模擬用戶訊息</div>
      <input class="test-input" id="test-input" type="text" placeholder="輸入訊息，例如：這個有現貨嗎" onkeydown="if(event.key==='Enter')runTest()">
      <button class="btn btn-test-send" onclick="runTest()">送出測試</button>
      <div class="test-result" id="test-result">
        <div class="intent-badge" id="test-intent"></div>
        <div class="reply-text" id="test-reply"></div>
      </div>
    </div>
  </div>

  <!-- 紀錄 -->
  <div id="tab-logs" class="tab-content">
    <div class="card" style="padding:0;overflow:hidden">
      <div id="log-content"><div class="empty-log">載入中...</div></div>
    </div>
  </div>
</div>

<script>
const KEY = "{{ key }}";
function switchTab(name, el) {
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  el.classList.add('active');
  if(name==='logs') loadLogs();
}
function runTest() {
  const text = document.getElementById('test-input').value.trim();
  if(!text) return;
  fetch('/api/test?key='+KEY, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})})
    .then(r=>r.json()).then(d=>{
      document.getElementById('test-intent').textContent = d.label + '（' + d.intent + '）';
      document.getElementById('test-reply').textContent = d.reply;
      document.getElementById('test-result').style.display = 'block';
    });
}
function loadLogs() {
  fetch('/api/logs?key='+KEY).then(r=>r.json()).then(logs=>{
    if(!logs.length){
      document.getElementById('log-content').innerHTML='<div class="empty-log">尚無訊息紀錄</div>';
      return;
    }
    let html = '<table class="log-table"><thead><tr><th>時間</th><th>來源</th><th>訊息</th><th>意圖</th><th>回覆</th></tr></thead><tbody>';
    logs.forEach(l=>{
      const ch = l.channel==='LINE' ? '<span class="ch-line">LINE</span>' : '<span class="ch-fb">FB</span>';
      const rep = l.replied ? '<span class="replied-no">✓ 回覆</span>' : '<span class="replied-yes">冷卻中</span>';
      html += `<tr><td>${l.time}</td><td>${ch}</td><td>${l.msg}</td><td>${l.intent}</td><td>${rep}</td></tr>`;
    });
    html += '</tbody></table>';
    document.getElementById('log-content').innerHTML = html;
  });
}
</script>
{% endif %}
</body>
</html>"""

_flash_store = {}

def render_admin(key, login_action, save_action, kw_save_action="", error=None):
    auth = (key == ADMIN_PASSWORD)
    flash_data = _flash_store.pop("msg", "")
    flash_type = _flash_store.pop("type", "ok")
    return render_template_string(ADMIN_HTML,
        auth=auth, key=key,
        replies=custom_replies,
        labels=REPLY_LABELS,
        keywords=custom_keywords,
        flash=flash_data,
        flash_type=flash_type,
        error=error,
        login_action=login_action,
        save_action=save_action,
        kw_save_action=kw_save_action)

@app.route("/admin")
def admin():
    key = request.args.get("key", "")
    return render_admin(key, "/admin/login", f"/admin/save?key={key}", f"/admin/kw-save?key={key}")

@app.route("/admin/login", methods=["POST"])
def admin_login():
    pw = request.form.get("password", "")
    if pw == ADMIN_PASSWORD:
        return redirect(f"/admin?key={pw}")
    return render_admin("", "/admin/login", "/admin/save", error="密碼錯誤")

def do_save(key, redirect_base):
    if key != ADMIN_PASSWORD:
        return redirect(f"/{redirect_base}")
    for k in REPLY_LABELS:
        if k in request.form:
            custom_replies[k] = request.form[k]
    if request.form.get("action") == "deploy" and GITHUB_TOKEN:
        ok, msg = commit_to_github()
        _flash_store["msg"] = msg
        _flash_store["type"] = "ok" if ok else "err"
    else:
        _flash_store["msg"] = "✅ 已儲存（重啟後還原，請點「🚀 部署」永久生效）"
        _flash_store["type"] = "ok"
    return redirect(f"/{redirect_base}?key={key}")

def do_kw_save(key, redirect_base):
    if key != ADMIN_PASSWORD:
        return redirect(f"/{redirect_base}")
    for k in custom_keywords:
        if k in request.form:
            raw = request.form[k]
            custom_keywords[k] = [w.strip() for w in raw.split(",") if w.strip()]
    _flash_store["msg"] = "✅ 關鍵字已更新（重啟後還原，回「回覆內容」點「🚀 部署」永久生效）"
    _flash_store["type"] = "ok"
    return redirect(f"/{redirect_base}?key={key}")

@app.route("/admin/save", methods=["POST"])
def admin_save():
    return do_save(request.args.get("key", ""), "admin")

@app.route("/admin/kw-save", methods=["POST"])
def admin_kw_save():
    return do_kw_save(request.args.get("key", ""), "admin")

def commit_to_github():
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
        return True, "🚀 已送出部署！Render 重新部署中（約 2 分鐘後生效）"
    except Exception as e:
        return False, f"❌ 部署失敗：{e}"

def build_knowledge_base_py() -> str:
    lines = ['"""\nJ SIMPLE 高架床 Bot 知識庫\n"""\n\n']
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
