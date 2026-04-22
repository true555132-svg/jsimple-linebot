"""
J SIMPLE 高架床 Bot
- LINE + FB Messenger 分開管理
- 動態新增/刪除意圖、關鍵字
- 各平台獨立開關
- /admin        總覽
- /admin/line   LINE 管理
- /admin/fb     FB 管理
"""

import os, json, base64, urllib.request, time
from collections import deque
from flask import Flask, request, abort, render_template_string, redirect, jsonify
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from knowledge_base import (
    BRAND_INFO, LINE_ENABLED, FB_ENABLED, INTENT_LABELS,
    LINE_REPLIES, FB_REPLIES, LINE_KEYWORDS, FB_KEYWORDS
)

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

COOLDOWN_SECONDS = 300
message_log = deque(maxlen=100)

# 各平台獨立狀態
platforms = {
    "line": {
        "enabled":  LINE_ENABLED,
        "replies":  dict(LINE_REPLIES),
        "keywords": {k: list(v) for k, v in LINE_KEYWORDS.items()},
        "labels":   dict(INTENT_LABELS),
    },
    "fb": {
        "enabled":  FB_ENABLED,
        "replies":  dict(FB_REPLIES),
        "keywords": {k: list(v) for k, v in FB_KEYWORDS.items()},
        "labels":   dict(INTENT_LABELS),
    },
}

cooldowns = {"line": {}, "fb": {}}

# ── 核心邏輯 ─────────────────────────────────────────────

def classify_intent(text: str, platform: str) -> str:
    kw = platforms[platform]["keywords"]
    text_lower = text.lower()
    for intent, words in kw.items():
        if any(w in text_lower for w in words):
            return intent
    return "default"

def get_reply(text: str, user_id: str, platform: str) -> str | None:
    cfg = platforms[platform]
    if not cfg["enabled"]:
        return None
    intent = classify_intent(text, platform)
    now = time.time()
    store = cooldowns[platform]
    user_times = store.setdefault(user_id, {})
    cooled = now - user_times.get(intent, 0) < COOLDOWN_SECONDS
    message_log.appendleft({
        "time": time.strftime("%m/%d %H:%M", time.localtime(now)),
        "platform": platform.upper(),
        "msg": text[:40],
        "intent": intent,
        "replied": not cooled,
    })
    if cooled:
        return None
    user_times[intent] = now
    return cfg["replies"].get(intent, cfg["replies"].get("default", ""))

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
    reply_text = get_reply(event.message.text.strip(), event.source.user_id, "line")
    if not reply_text:
        return
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token,
                                messages=[TextMessage(text=reply_text)])
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
                sid = event.get("sender", {}).get("id", "")
                msg = event.get("message", {})
                if sid and "text" in msg:
                    reply = get_reply(msg["text"].strip(), sid, "fb")
                    if reply:
                        fb_send(sid, reply)
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

# ── API ───────────────────────────────────────────────────

def auth_required():
    key = request.args.get("key", "")
    return key == ADMIN_PASSWORD, key

@app.route("/api/test", methods=["POST"])
def api_test():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    platform = data.get("platform", "line")
    intent = classify_intent(text, platform)
    cfg = platforms[platform]
    label = cfg["labels"].get(intent, intent)
    reply = cfg["replies"].get(intent, cfg["replies"].get("default", ""))
    return jsonify({"intent": intent, "label": label, "reply": reply})

@app.route("/api/logs")
def api_logs():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    platform = request.args.get("platform", "all")
    logs = [l for l in message_log if platform == "all" or l["platform"] == platform.upper()]
    return jsonify(logs)

# ── Admin HTML ────────────────────────────────────────────

DASH_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>J SIMPLE Bot 總覽</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
.header{background:#1a1a1a;color:#fff;padding:14px 20px;font-size:17px;font-weight:700}
.container{max-width:500px;margin:30px auto;padding:0 16px}
.card{background:#fff;border-radius:14px;padding:22px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08);display:flex;align-items:center;justify-content:space-between;text-decoration:none;color:#333}
.card:hover{box-shadow:0 3px 12px rgba(0,0,0,.13)}
.card-left{display:flex;align-items:center;gap:14px}
.icon{width:46px;height:46px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px}
.icon-line{background:#e8f5e9}
.icon-fb{background:#e3f2fd}
.card-title{font-size:16px;font-weight:700}
.card-sub{font-size:13px;color:#888;margin-top:2px}
.status{font-size:12px;font-weight:700;padding:4px 10px;border-radius:10px}
.on{background:#e8f5e9;color:#2e7d32}
.off{background:#fdecea;color:#c62828}
.arrow{color:#ccc;font-size:20px}
</style></head><body>
<div class="header">⚡ J SIMPLE Bot 後台總覽</div>
<div class="container">
  <a class="card" href="/admin/line?key={{ key }}">
    <div class="card-left">
      <div class="icon icon-line">💬</div>
      <div>
        <div class="card-title">LINE Bot 管理</div>
        <div class="card-sub">@JSIMPLE</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span class="status {{ 'on' if line_on else 'off' }}">{{ '開啟' if line_on else '關閉' }}</span>
      <span class="arrow">›</span>
    </div>
  </a>
  <a class="card" href="/admin/fb?key={{ key }}">
    <div class="card-left">
      <div class="icon icon-fb">📘</div>
      <div>
        <div class="card-title">FB Messenger 管理</div>
        <div class="card-sub">逸雅傢俱</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span class="status {{ 'on' if fb_on else 'off' }}">{{ '開啟' if fb_on else '關閉' }}</span>
      <span class="arrow">›</span>
    </div>
  </a>
</div>
</body></html>"""

PLATFORM_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ pname }} 管理</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333;font-size:15px}
.header{background:#1a1a1a;color:#fff;padding:13px 18px;display:flex;align-items:center;gap:12px}
.back{color:#aaa;text-decoration:none;font-size:20px;line-height:1}
.header h1{font-size:16px;font-weight:700;flex:1}
.toggle-wrap{display:flex;align-items:center;gap:8px}
.toggle-label{font-size:13px}
.toggle{position:relative;width:46px;height:26px}
.toggle input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;border-radius:26px;background:#555;cursor:pointer;transition:.3s}
.slider:before{content:"";position:absolute;width:20px;height:20px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s}
input:checked+.slider{background:#00c300}
input:checked+.slider:before{transform:translateX(20px)}
.tabs{display:flex;background:#fff;border-bottom:2px solid #eee;overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{padding:11px 18px;cursor:pointer;white-space:nowrap;font-weight:600;color:#999;border-bottom:3px solid transparent;margin-bottom:-2px;font-size:14px}
.tab.active{color:var(--ac);border-bottom-color:var(--ac)}
.tab-content{display:none}
.tab-content.active{display:block}
.container{max-width:860px;margin:18px auto;padding:0 14px 90px}
.card{background:#fff;border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.card-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.card-title{font-size:15px;font-weight:700}
.card-meta{display:flex;align-items:center;gap:8px}
.badge{font-size:11px;color:#bbb;background:#f5f5f5;padding:2px 8px;border-radius:8px}
.del-btn{border:none;background:none;color:#e57373;cursor:pointer;font-size:18px;padding:2px 6px;border-radius:6px}
.del-btn:hover{background:#fdecea}
textarea{width:100%;border:1px solid #e0e0e0;border-radius:8px;padding:10px;font-size:14px;line-height:1.75;resize:vertical;min-height:90px;font-family:inherit}
textarea:focus{outline:none;border-color:var(--ac);box-shadow:0 0 0 3px rgba(var(--ac-rgb),.08)}
.kw-input,input[type=text]{width:100%;border:1px solid #e0e0e0;border-radius:8px;padding:9px 11px;font-size:14px;font-family:inherit}
.kw-input:focus,input[type=text]:focus{outline:none;border-color:var(--ac)}
.hint{font-size:12px;color:#bbb;margin-top:5px}
.add-card{border:2px dashed #e0e0e0;border-radius:12px;padding:18px;margin-bottom:12px;background:none}
.add-card:hover{border-color:var(--ac)}
.add-title{font-size:14px;font-weight:700;color:#888;margin-bottom:12px}
.field{margin-bottom:10px}
.field label{font-size:13px;color:#888;display:block;margin-bottom:4px}
.btn-row{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #eee;padding:10px 14px;display:flex;gap:10px;justify-content:center;z-index:100}
.btn{padding:10px 22px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.btn:hover{opacity:.85}
.btn-save{background:var(--ac);color:#fff}
.btn-back{background:#f0f0f0;color:#555}
.btn-add{width:100%;padding:11px;background:var(--ac);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;margin-top:4px}
.btn-add:hover{opacity:.85}
.test-input{width:100%;border:1px solid #ddd;border-radius:8px;padding:11px;font-size:15px;font-family:inherit}
.test-input:focus{outline:none;border-color:var(--ac)}
.btn-test{width:100%;margin-top:10px;padding:11px;background:var(--ac);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.test-result{margin-top:14px;display:none}
.intent-badge{display:inline-block;padding:3px 10px;border-radius:10px;font-size:12px;font-weight:600;margin-bottom:8px;background:rgba(var(--ac-rgb),.12);color:var(--ac)}
.reply-pre{background:#f5f5f5;border-radius:8px;padding:12px;font-size:14px;line-height:1.7;white-space:pre-wrap}
.log-table{width:100%;border-collapse:collapse;font-size:13px}
.log-table th{text-align:left;padding:8px 10px;background:#f9f9f9;color:#888;font-weight:600;border-bottom:2px solid #eee}
.log-table td{padding:8px 10px;border-bottom:1px solid #f0f0f0;vertical-align:top}
.log-table tr:last-child td{border-bottom:none}
.replied-yes{color:#00b050;font-weight:600;font-size:12px}
.replied-no{color:#bbb;font-size:12px}
.empty{text-align:center;color:#ccc;padding:40px;font-size:14px}
.flash{padding:10px 16px;border-radius:8px;margin-bottom:12px;font-size:14px}
.ok{background:#e8f5e9;color:#2e7d32}
.err{background:#fdecea;color:#c62828}
@media(max-width:600px){.tab{padding:9px 12px;font-size:13px}.btn{padding:9px 14px;font-size:13px}}
</style>
</head>
<body style="--ac:{{ ac }};--ac-rgb:{{ ac_rgb }}">
<div class="header">
  <a class="back" href="/admin?key={{ key }}">‹</a>
  <h1>{{ pname }}</h1>
  <div class="toggle-wrap">
    <span class="toggle-label">{{ '開啟' if cfg.enabled else '關閉' }}</span>
    <form method="POST" action="/admin/{{ platform }}/toggle?key={{ key }}" style="display:inline">
      <label class="toggle">
        <input type="checkbox" onchange="this.form.submit()" {{ 'checked' if cfg.enabled }}>
        <span class="slider"></span>
      </label>
    </form>
  </div>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('replies',this)">💬 回覆內容</div>
  <div class="tab" onclick="switchTab('keywords',this)">🏷️ 關鍵字</div>
  <div class="tab" onclick="switchTab('add',this)">➕ 新增</div>
  <div class="tab" onclick="switchTab('test',this)">🧪 測試</div>
  <div class="tab" onclick="switchTab('logs',this)">📋 紀錄</div>
</div>

<div class="container">
  {% if flash %}<div class="flash {{ flash_type }}">{{ flash }}</div>{% endif %}

  <!-- 回覆內容 -->
  <div id="tab-replies" class="tab-content active">
    <form method="POST" action="/admin/{{ platform }}/save?key={{ key }}">
      {% for id, label in cfg.labels.items() %}
      <div class="card">
        <div class="card-head">
          <div class="card-title">{{ label }}</div>
          <div class="card-meta">
            <span class="badge">{{ id }}</span>
            {% if id not in builtin_intents %}
            <button type="button" class="del-btn" onclick="delIntent('{{ id }}')">×</button>
            {% endif %}
          </div>
        </div>
        <textarea name="reply_{{ id }}">{{ cfg.replies.get(id,'') }}</textarea>
      </div>
      {% endfor %}
      <div class="btn-row">
        <button class="btn btn-save" type="submit" name="action" value="save">💾 儲存</button>
        <button class="btn btn-save" type="submit" name="action" value="deploy" style="background:#1a1a1a">🚀 部署</button>
      </div>
    </form>
  </div>

  <!-- 關鍵字 -->
  <div id="tab-keywords" class="tab-content">
    <form method="POST" action="/admin/{{ platform }}/kw-save?key={{ key }}">
      {% for id, label in cfg.labels.items() %}
      {% if id != 'default' %}
      <div class="card">
        <div class="card-head">
          <div class="card-title">{{ label }}</div>
          <span class="badge">{{ id }}</span>
        </div>
        <input class="kw-input" type="text" name="kw_{{ id }}" value="{{ cfg.keywords.get(id,[])|join(', ') }}" placeholder="關鍵字1, 關鍵字2, ...">
        <div class="hint">逗號分隔，不分大小寫</div>
      </div>
      {% endif %}
      {% endfor %}
      <div class="btn-row">
        <button class="btn btn-save" type="submit">💾 儲存關鍵字</button>
      </div>
    </form>
  </div>

  <!-- 新增意圖 -->
  <div id="tab-add" class="tab-content">
    <form method="POST" action="/admin/{{ platform }}/add-intent?key={{ key }}">
      <div class="add-card">
        <div class="add-title">➕ 新增回覆類別</div>
        <div class="field">
          <label>識別碼（英文，不可重複）</label>
          <input type="text" name="intent_key" placeholder="例如：assembly" required pattern="[a-z_]+">
        </div>
        <div class="field">
          <label>顯示名稱</label>
          <input type="text" name="intent_label" placeholder="例如：安裝教學" required>
        </div>
        <div class="field">
          <label>觸發關鍵字（逗號分隔）</label>
          <input type="text" name="intent_keywords" placeholder="例如：怎麼裝,安裝步驟,組裝說明" required>
        </div>
        <div class="field">
          <label>回覆內容</label>
          <textarea name="intent_reply" style="min-height:80px" placeholder="輸入回覆文字..." required></textarea>
        </div>
        <button type="submit" class="btn-add">新增</button>
      </div>
    </form>
  </div>

  <!-- 測試 -->
  <div id="tab-test" class="tab-content">
    <div class="card">
      <div class="card-title" style="margin-bottom:12px">模擬用戶訊息</div>
      <input class="test-input" id="test-input" type="text" placeholder="輸入訊息，例如：這個有現貨嗎" onkeydown="if(event.key==='Enter')runTest()">
      <button class="btn-test" onclick="runTest()">送出測試</button>
      <div class="test-result" id="test-result">
        <div class="intent-badge" id="test-intent"></div>
        <div class="reply-pre" id="test-reply"></div>
      </div>
    </div>
  </div>

  <!-- 紀錄 -->
  <div id="tab-logs" class="tab-content">
    <div class="card" style="padding:0;overflow:hidden">
      <div id="log-content"><div class="empty">載入中...</div></div>
    </div>
  </div>
</div>

<!-- 刪除意圖 form（hidden） -->
<form id="del-form" method="POST" action="/admin/{{ platform }}/del-intent?key={{ key }}">
  <input type="hidden" name="intent_key" id="del-key">
</form>

<script>
const KEY="{{ key }}",PLATFORM="{{ platform }}";
function switchTab(name,el){
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  el.classList.add('active');
  if(name==='logs')loadLogs();
}
function runTest(){
  const text=document.getElementById('test-input').value.trim();
  if(!text)return;
  fetch('/api/test?key='+KEY,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,platform:PLATFORM})})
    .then(r=>r.json()).then(d=>{
      document.getElementById('test-intent').textContent=d.label+'（'+d.intent+'）';
      document.getElementById('test-reply').textContent=d.reply;
      document.getElementById('test-result').style.display='block';
    });
}
function loadLogs(){
  fetch('/api/logs?key='+KEY+'&platform='+PLATFORM).then(r=>r.json()).then(logs=>{
    if(!logs.length){document.getElementById('log-content').innerHTML='<div class="empty">尚無訊息紀錄</div>';return;}
    let h='<table class="log-table"><thead><tr><th>時間</th><th>訊息</th><th>意圖</th><th>狀態</th></tr></thead><tbody>';
    logs.forEach(l=>{
      const rep=l.replied?'<span class="replied-yes">✓ 已回覆</span>':'<span class="replied-no">冷卻中</span>';
      h+=`<tr><td>${l.time}</td><td>${l.msg}</td><td>${l.intent}</td><td>${rep}</td></tr>`;
    });
    h+='</tbody></table>';
    document.getElementById('log-content').innerHTML=h;
  });
}
function delIntent(key){
  if(!confirm('確定刪除「'+key+'」這個類別？'))return;
  document.getElementById('del-key').value=key;
  document.getElementById('del-form').submit();
}
</script>
</body></html>"""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>J SIMPLE Bot 後台</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5}
.wrap{max-width:340px;margin:80px auto;background:#fff;border-radius:16px;padding:36px;box-shadow:0 2px 16px rgba(0,0,0,.1);text-align:center}
h2{margin-bottom:22px;font-size:19px;color:#333}
input{width:100%;padding:11px;border:1px solid #ddd;border-radius:8px;font-size:15px;margin-bottom:14px}
button{width:100%;background:#00c300;color:#fff;border:none;border-radius:8px;padding:12px;font-size:15px;font-weight:600;cursor:pointer}
.err{color:red;margin-top:10px;font-size:13px}
</style></head><body>
<div class="wrap">
  <h2>🔐 J SIMPLE 後台</h2>
  <form method="POST" action="/admin/login">
    <input type="hidden" name="next" value="{{ next }}">
    <input type="password" name="password" placeholder="請輸入密碼" autofocus>
    <button type="submit">登入</button>
  </form>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
</div>
</body></html>"""

_flash = {}
BUILTIN_INTENTS = {"greeting","price","custom","shipping","size","delivery","warranty","material","color","payment","return","default"}

PLATFORM_META = {
    "line": {"name": "LINE Bot 管理", "ac": "#00c300", "ac_rgb": "0,195,0"},
    "fb":   {"name": "FB Messenger 管理", "ac": "#1877f2", "ac_rgb": "24,119,242"},
}

def check_auth():
    key = request.args.get("key", "")
    return key == ADMIN_PASSWORD, key

def render_platform(platform, key):
    meta = PLATFORM_META[platform]
    cfg = platforms[platform]
    flash_msg = _flash.pop("msg", "")
    flash_type = _flash.pop("type", "ok")
    return render_template_string(PLATFORM_HTML,
        platform=platform, pname=meta["name"],
        ac=meta["ac"], ac_rgb=meta["ac_rgb"],
        key=key, cfg=cfg,
        builtin_intents=BUILTIN_INTENTS,
        flash=flash_msg, flash_type=flash_type)

# ── Admin Routes ──────────────────────────────────────────

@app.route("/admin")
def admin_dash():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next="/admin", error=None)
    return render_template_string(DASH_HTML, key=key,
        line_on=platforms["line"]["enabled"],
        fb_on=platforms["fb"]["enabled"])

@app.route("/admin/login", methods=["POST"])
def admin_login():
    pw = request.form.get("password", "")
    next_url = request.form.get("next", "/admin")
    if pw == ADMIN_PASSWORD:
        return redirect(f"{next_url}?key={pw}")
    return render_template_string(LOGIN_HTML, next=next_url, error="密碼錯誤")

@app.route("/admin/<platform>")
def platform_admin(platform):
    if platform not in platforms:
        abort(404)
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next=f"/admin/{platform}", error=None)
    return render_platform(platform, key)

@app.route("/admin/<platform>/toggle", methods=["POST"])
def platform_toggle(platform):
    if platform not in platforms:
        abort(404)
    ok, key = check_auth()
    if not ok:
        abort(403)
    platforms[platform]["enabled"] = not platforms[platform]["enabled"]
    state = "開啟" if platforms[platform]["enabled"] else "關閉"
    _flash["msg"] = f"✅ {PLATFORM_META[platform]['name']} 已{state}"
    _flash["type"] = "ok"
    return redirect(f"/admin/{platform}?key={key}")

@app.route("/admin/<platform>/save", methods=["POST"])
def platform_save(platform):
    if platform not in platforms:
        abort(404)
    ok, key = check_auth()
    if not ok:
        abort(403)
    cfg = platforms[platform]
    for k in cfg["labels"]:
        field = f"reply_{k}"
        if field in request.form:
            cfg["replies"][k] = request.form[field]
    if request.form.get("action") == "deploy" and GITHUB_TOKEN:
        success, msg = commit_to_github()
        _flash["msg"] = msg
        _flash["type"] = "ok" if success else "err"
    else:
        _flash["msg"] = "✅ 已儲存（點「🚀 部署」永久生效）"
        _flash["type"] = "ok"
    return redirect(f"/admin/{platform}?key={key}")

@app.route("/admin/<platform>/kw-save", methods=["POST"])
def platform_kw_save(platform):
    if platform not in platforms:
        abort(404)
    ok, key = check_auth()
    if not ok:
        abort(403)
    cfg = platforms[platform]
    for k in cfg["labels"]:
        if k == "default":
            continue
        field = f"kw_{k}"
        if field in request.form:
            cfg["keywords"][k] = [w.strip() for w in request.form[field].split(",") if w.strip()]
    _flash["msg"] = "✅ 關鍵字已更新"
    _flash["type"] = "ok"
    return redirect(f"/admin/{platform}?key={key}")

@app.route("/admin/<platform>/add-intent", methods=["POST"])
def platform_add_intent(platform):
    if platform not in platforms:
        abort(404)
    ok, key = check_auth()
    if not ok:
        abort(403)
    cfg = platforms[platform]
    k = request.form.get("intent_key", "").strip().lower().replace(" ", "_")
    label = request.form.get("intent_label", "").strip()
    kws = [w.strip() for w in request.form.get("intent_keywords", "").split(",") if w.strip()]
    reply = request.form.get("intent_reply", "").strip()
    if k and label and kws and reply and k not in cfg["labels"]:
        cfg["labels"][k] = label
        cfg["keywords"][k] = kws
        cfg["replies"][k] = reply
        _flash["msg"] = f"✅ 已新增「{label}」類別"
        _flash["type"] = "ok"
    else:
        _flash["msg"] = "❌ 新增失敗（識別碼重複或欄位未填）"
        _flash["type"] = "err"
    return redirect(f"/admin/{platform}?key={key}#tab-add")

@app.route("/admin/<platform>/del-intent", methods=["POST"])
def platform_del_intent(platform):
    if platform not in platforms:
        abort(404)
    ok, key = check_auth()
    if not ok:
        abort(403)
    k = request.form.get("intent_key", "")
    cfg = platforms[platform]
    if k and k not in BUILTIN_INTENTS and k in cfg["labels"]:
        cfg["labels"].pop(k)
        cfg["keywords"].pop(k, None)
        cfg["replies"].pop(k, None)
        _flash["msg"] = f"✅ 已刪除「{k}」類別"
        _flash["type"] = "ok"
    return redirect(f"/admin/{platform}?key={key}")

# ── GitHub Deploy ─────────────────────────────────────────

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
            "message": "admin: update platform config",
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
    lp = platforms["line"]
    fp = platforms["fb"]

    def dict_to_py(d, indent=4):
        sp = " " * indent
        lines = []
        for k, v in d.items():
            if isinstance(v, str):
                esc = v.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
                lines.append(f'{sp}"{k}": """{esc}""",\n')
            elif isinstance(v, list):
                items = ", ".join(f'"{x}"' for x in v)
                lines.append(f'{sp}"{k}": [{items}],\n')
        return "".join(lines)

    out = ['"""\nJ SIMPLE 高架床 Bot 知識庫\n"""\n\n']
    out.append("BRAND_INFO = {\n")
    for k, v in BRAND_INFO.items():
        out.append(f'    "{k}": "{v}",\n')
    out.append("}\n\n")
    out.append('SHIPPING = {"north":1000,"central_south":1300,"floor_surcharge":300,"elevator_surcharge":300}\n\n')
    out.append(f'LINE_ENABLED = {lp["enabled"]}\n')
    out.append(f'FB_ENABLED = {fp["enabled"]}\n\n')
    out.append("INTENT_LABELS = {\n" + dict_to_py(lp["labels"]) + "}\n\n")
    out.append("LINE_REPLIES = {\n" + dict_to_py(lp["replies"]) + "}\n\n")
    out.append("FB_REPLIES = {\n" + dict_to_py(fp["replies"]) + "}\n\n")
    out.append("LINE_KEYWORDS = {\n" + dict_to_py(lp["keywords"]) + "}\n\n")
    out.append("FB_KEYWORDS = {\n" + dict_to_py(fp["keywords"]) + "}\n\n")
    out.append("_BASE_REPLIES = LINE_REPLIES\n")
    out.append("_BASE_KEYWORDS = LINE_KEYWORDS\n")
    return "".join(out)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
