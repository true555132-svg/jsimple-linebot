"""
J SIMPLE 高架床 Bot
- LINE + FB Messenger 分開管理
- 動態新增/刪除意圖、關鍵字
- 各平台獨立開關
- /admin        總覽
- /admin/line   LINE 管理
- /admin/fb     FB 管理
"""

import os, json, base64, urllib.request, time, threading
from collections import deque
from flask import Flask, request, abort, render_template_string, redirect, jsonify
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, ImageMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from knowledge_base import (
    BRAND_INFO, LINE_ENABLED, FB_ENABLED, INTENT_LABELS,
    LINE_REPLIES, FB_REPLIES, LINE_KEYWORDS, FB_KEYWORDS,
    LINE_IMAGE_URLS, FB_IMAGE_URLS,
    LINE_ENABLED_INTENTS, FB_ENABLED_INTENTS,
    FB_COMMENT_REPLIES, FB_COMMENT_KEYWORDS, FB_COMMENT_ENABLED_INTENTS
)

app = Flask(__name__)

LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "ed4319138fed1c6db548b60327e2d69d")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "S/R1BB9ByxtJ5CXr4kbbj51Xkz7S9kfxYIjzYsqDvjzAYHXLc6aOJQq6eDO5j7Me3SVGrkkpPeX0OH5tUHYnjGyO/S4WDRYlOWoIPIJplSUUCNX0FmeCnPhizFaUSnPNIw2uyvV016cyuO1jtO5dZQdB04t89/1O/w1cDnyilFU=")
FB_PAGE_ACCESS_TOKEN      = os.getenv("FB_PAGE_ACCESS_TOKEN", "EAALpgIiggJkBRQRZCRKJVGJr8y6gqvoOjiZAVdqBnXwf5ebL3EwdC0S02dB11KZBOtPigUYsvm9KgDgtB3ndo97Vc9h82g4yZCyRzEm9SSHg34CAzY9ZBuEuD0F8Ben3RIp6EI8ogXANcV4nVcmnzzmv2b13XiY6gvffykwgbrydDs7ouZAdgPX8xUVuoLSZBoWNB6f")
FB_VERIFY_TOKEN           = "jsimple2024"
ADMIN_PASSWORD            = os.getenv("ADMIN_PASSWORD", "jsimple2024")
GITHUB_TOKEN              = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO               = "true555132-svg/jsimple-linebot"
GITHUB_FILE               = "knowledge_base.py"
RENDER_DEPLOY_HOOK        = os.getenv("RENDER_DEPLOY_HOOK", "https://api.render.com/deploy/srv-d7k3ri9j2pic73dpbe10?key=08mC1cciu1E")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

COOLDOWN_SECONDS = 300
LOGS_FILE = "logs.json"
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")

def _load_logs():
    try:
        with open(LOGS_FILE, "r", encoding="utf-8") as f:
            return deque(json.load(f), maxlen=500)
    except Exception:
        return deque(maxlen=500)

message_log = _load_logs()

def _save_logs():
    try:
        with open(LOGS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(message_log), f, ensure_ascii=False)
    except Exception:
        pass

def _append_to_sheets(entry):
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        ws = gspread.authorize(creds).open_by_key(GOOGLE_SHEET_ID).sheet1
        ws.append_row([
            entry["time"], entry["platform"], entry["msg"],
            entry["intent"], "已回覆" if entry["replied"] else "冷卻中"
        ])
    except Exception:
        pass

def log_message(entry):
    message_log.appendleft(entry)
    _save_logs()
    if GOOGLE_SHEET_ID:
        threading.Thread(target=_append_to_sheets, args=(entry,), daemon=True).start()

# 各平台獨立狀態
platforms = {
    "line": {
        "enabled":         LINE_ENABLED,
        "replies":         dict(LINE_REPLIES),
        "keywords":        {k: list(v) for k, v in LINE_KEYWORDS.items()},
        "labels":          dict(INTENT_LABELS),
        "image_urls":      dict(LINE_IMAGE_URLS),
        "enabled_intents": dict(LINE_ENABLED_INTENTS),
    },
    "fb": {
        "enabled":         FB_ENABLED,
        "replies":         dict(FB_REPLIES),
        "keywords":        {k: list(v) for k, v in FB_KEYWORDS.items()},
        "labels":          dict(INTENT_LABELS),
        "image_urls":      dict(FB_IMAGE_URLS),
        "enabled_intents": dict(FB_ENABLED_INTENTS),
    },
    "fb_comment": {
        "enabled":         True,
        "replies":         dict(FB_COMMENT_REPLIES),
        "keywords":        {k: list(v) for k, v in FB_COMMENT_KEYWORDS.items()},
        "labels":          dict(INTENT_LABELS),
        "image_urls":      {},
        "enabled_intents": dict(FB_COMMENT_ENABLED_INTENTS),
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

def get_reply(text: str, user_id: str, platform: str) -> tuple:
    cfg = platforms[platform]
    intent = classify_intent(text, platform)
    intent_on = cfg["enabled_intents"].get(intent, True)
    now = time.time()
    store = cooldowns[platform]
    user_times = store.setdefault(user_id, {})
    cooled = now - user_times.get(intent, 0) < COOLDOWN_SECONDS
    log_message({
        "time": time.strftime("%m/%d %H:%M", time.localtime(now)),
        "platform": platform.upper(),
        "msg": text[:40],
        "intent": intent,
        "replied": not cooled and cfg["enabled"] and intent_on,
    })
    if not cfg["enabled"] or cooled or not intent_on:
        return None, None
    user_times[intent] = now
    reply_text = cfg["replies"].get(intent, cfg["replies"].get("default", ""))
    image_url = cfg["image_urls"].get(intent, "")
    return reply_text, image_url

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
    text, image_url = get_reply(event.message.text.strip(), event.source.user_id, "line")
    if not text and not image_url:
        return
    messages = []
    if text:
        messages.append(TextMessage(text=text))
    if image_url:
        messages.append(ImageMessage(original_content_url=image_url, preview_image_url=image_url))
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=messages)
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
            # Messenger 私訊
            for event in entry.get("messaging", []):
                sid = event.get("sender", {}).get("id", "")
                msg = event.get("message", {})
                if sid and "text" in msg:
                    text, image_url = get_reply(msg["text"].strip(), sid, "fb")
                    if text:
                        fb_send(sid, text)
                    if image_url:
                        fb_send_image(sid, image_url)
            # 貼文留言
            for change in entry.get("changes", []):
                if change.get("field") == "feed":
                    val = change.get("value", {})
                    if val.get("item") == "comment" and val.get("verb") == "add":
                        if "parent_id" not in val:  # 只回頂層留言，不回留言的留言
                            fb_handle_comment(val)
    return "OK", 200

def fb_handle_comment(val):
    comment_id = val.get("comment_id", "")
    user_id = val.get("from", {}).get("id", "")
    text = val.get("message", "").strip()
    if not comment_id or not text:
        return
    if not platforms["fb_comment"]["enabled"]:
        return
    reply_text, _ = get_reply(text, user_id, "fb_comment")
    if not reply_text:
        return
    fb_reply_comment(comment_id, reply_text)
    private_msg = platforms["fb_comment"]["replies"].get("default", "您好！感謝留言，詳細說明已私訊您 😊")
    fb_private_reply(comment_id, private_msg)

def fb_reply_comment(comment_id: str, text: str):
    if not FB_PAGE_ACCESS_TOKEN:
        return
    url = f"https://graph.facebook.com/v19.0/{comment_id}/comments?access_token={FB_PAGE_ACCESS_TOKEN}"
    payload = json.dumps({"message": text}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass

def fb_private_reply(comment_id: str, text: str):
    if not FB_PAGE_ACCESS_TOKEN:
        return
    url = f"https://graph.facebook.com/v19.0/{comment_id}/private_replies?access_token={FB_PAGE_ACCESS_TOKEN}"
    payload = json.dumps({"message": text}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass

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

def fb_send_image(psid: str, image_url: str):
    if not FB_PAGE_ACCESS_TOKEN:
        return
    url = f"https://graph.facebook.com/v19.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
    payload = json.dumps({"recipient": {"id": psid}, "message": {
        "attachment": {"type": "image", "payload": {"url": image_url, "is_reusable": True}}
    }}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass

def upload_image_to_github(filename: str, data: bytes) -> str:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/images/{filename}"
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        })
        sha = None
        try:
            with urllib.request.urlopen(req) as r:
                sha = json.loads(r.read()).get("sha")
        except Exception:
            pass
        body = {"message": f"upload image: {filename}", "content": base64.b64encode(data).decode()}
        if sha:
            body["sha"] = sha
        req2 = urllib.request.Request(url, data=json.dumps(body).encode(), method="PUT", headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
        })
        urllib.request.urlopen(req2)
        return f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/images/{filename}"
    except Exception as e:
        return ""

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

@app.route("/admin/<platform>/upload-image", methods=["POST"])
def upload_image(platform):
    if platform not in platforms:
        abort(404)
    ok, _ = check_auth()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    if not GITHUB_TOKEN:
        return jsonify({"error": "GITHUB_TOKEN 未設定"}), 500
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    import re, time as _time
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", f.filename)
    filename = f"{int(_time.time())}_{safe}"
    image_url = upload_image_to_github(filename, f.read())
    if not image_url:
        return jsonify({"error": "上傳失敗"}), 500
    intent_key = request.form.get("intent_key", "")
    if intent_key:
        platforms[platform]["image_urls"][intent_key] = image_url
    return jsonify({"url": image_url})

@app.route("/api/render-deploy", methods=["POST"])
def api_render_deploy():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    if not RENDER_DEPLOY_HOOK:
        return jsonify({"error": "no hook"}), 500
    try:
        urllib.request.urlopen(urllib.request.Request(RENDER_DEPLOY_HOOK, method="POST"))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
  <a class="card" href="/admin/fb_comment?key={{ key }}">
    <div class="card-left">
      <div class="icon" style="background:#fce4ec">💬</div>
      <div>
        <div class="card-title">FB 留言自動回覆</div>
        <div class="card-sub">逸雅傢俱貼文留言</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span class="status on">開啟</span>
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
.badge{font-size:11px;color:#999;background:#f0f0f0;padding:2px 8px;border-radius:8px;font-weight:600}
textarea{width:100%;border:1px solid #e0e0e0;border-radius:8px;padding:10px;font-size:14px;line-height:1.75;resize:vertical;font-family:inherit}
textarea:focus{outline:none;border-color:var(--ac);box-shadow:0 0 0 3px rgba(var(--ac-rgb),.08)}
input[type=text]{width:100%;border:1px solid #e0e0e0;border-radius:8px;padding:9px 11px;font-size:14px;font-family:inherit}
input[type=text]:focus{outline:none;border-color:var(--ac)}
.hint{font-size:12px;color:#bbb;margin-top:5px}
/* ── 意圖卡片（雙欄） ── */
.intent-card{background:#fff;border-radius:12px;margin-bottom:10px;box-shadow:0 1px 4px rgba(0,0,0,.07);overflow:hidden;transition:opacity .2s}
.intent-card.disabled{opacity:.38}
.ic-head{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:#fafafa;border-bottom:1px solid #efefef}
.ic-name{display:flex;align-items:center;gap:7px;font-size:14px;font-weight:700}
.ic-body{display:grid;grid-template-columns:1fr 1.7fr}
.ic-left{padding:14px;border-right:1px solid #f0f0f0;display:flex;flex-direction:column;gap:12px}
.ic-right{padding:14px;display:flex;flex-direction:column;gap:8px}
.col-label{font-size:11px;color:#aaa;font-weight:700;letter-spacing:.5px;text-transform:uppercase;margin-bottom:5px}
.ic-del{display:flex;justify-content:flex-end;margin-top:4px}
.del-btn{border:none;background:none;color:#e57373;cursor:pointer;font-size:12px;padding:4px 8px;border-radius:6px}
.del-btn:hover{background:#fdecea}
/* ── 기타 카드 (테스트/로그) ── */
.card{background:#fff;border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.card-title{font-size:15px;font-weight:700;margin-bottom:10px}
/* ── 新增 ── */
.add-card{border:2px dashed #e0e0e0;border-radius:12px;padding:18px;margin-bottom:12px;background:none}
.add-card:hover{border-color:var(--ac)}
.add-title{font-size:14px;font-weight:700;color:#888;margin-bottom:12px}
.field{margin-bottom:10px}
.field label{font-size:13px;color:#888;display:block;margin-bottom:4px}
.btn-add{width:100%;padding:11px;background:var(--ac);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;margin-top:4px}
.btn-add:hover{opacity:.85}
/* ── 底部按鈕 ── */
.btn-row{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #eee;padding:10px 14px;display:flex;gap:10px;justify-content:center;z-index:100}
.btn{padding:10px 22px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.btn:hover{opacity:.85}
.btn-save{background:var(--ac);color:#fff}
/* ── 測試 ── */
.test-input{width:100%;border:1px solid #ddd;border-radius:8px;padding:11px;font-size:15px;font-family:inherit}
.test-input:focus{outline:none;border-color:var(--ac)}
.btn-test{width:100%;margin-top:10px;padding:11px;background:var(--ac);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.test-result{margin-top:14px;display:none}
.intent-badge{display:inline-block;padding:3px 10px;border-radius:10px;font-size:12px;font-weight:600;margin-bottom:8px;background:rgba(var(--ac-rgb),.12);color:var(--ac)}
.reply-pre{background:#f5f5f5;border-radius:8px;padding:12px;font-size:14px;line-height:1.7;white-space:pre-wrap}
/* ── 紀錄 ── */
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
/* ── 開關 ── */
.mini-toggle{position:relative;width:38px;height:22px;flex-shrink:0}
.mini-toggle input{opacity:0;width:0;height:0;position:absolute}
.m-slider{position:absolute;inset:0;border-radius:22px;background:#ccc;cursor:pointer;transition:.25s}
.m-slider:before{content:"";position:absolute;width:16px;height:16px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.25s;box-shadow:0 1px 3px rgba(0,0,0,.2)}
.mini-toggle input:checked+.m-slider{background:var(--ac)}
.mini-toggle input:checked+.m-slider:before{transform:translateX(16px)}
@media(max-width:640px){
  .ic-body{grid-template-columns:1fr}
  .ic-left{border-right:none;border-bottom:1px solid #f0f0f0}
  .tab{padding:9px 12px;font-size:13px}
  .btn{padding:9px 14px;font-size:13px}
}
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
  <div class="tab active" onclick="switchTab('replies',this)">💬 回覆管理</div>
  <div class="tab" onclick="switchTab('add',this)">➕ 新增</div>
  <div class="tab" onclick="switchTab('test',this)">🧪 測試</div>
  <div class="tab" onclick="switchTab('logs',this)">📋 紀錄</div>
</div>

<div class="container">
  {% if flash %}<div class="flash {{ flash_type }}">{{ flash }}</div>{% endif %}

  <!-- 回覆管理（雙欄） -->
  <div id="tab-replies" class="tab-content active">
    <form method="POST" action="/admin/{{ platform }}/save?key={{ key }}">
      {% for id, label in cfg.labels.items() %}
      {% set intent_on = cfg.enabled_intents.get(id, True) %}
      {% set img = cfg.image_urls.get(id,'') %}
      <div class="intent-card{{ '' if intent_on else ' disabled' }}" id="card-{{ id }}">
        <!-- 標題列 -->
        <div class="ic-head">
          <div class="ic-name">
            <span class="badge">{{ id }}</span>
            {{ label }}
          </div>
          <label class="mini-toggle">
            <input type="checkbox" name="enabled_{{ id }}" {{ 'checked' if intent_on }} onchange="toggleCard('{{ id }}',this)">
            <span class="m-slider"></span>
          </label>
        </div>
        <!-- 左右欄 -->
        <div class="ic-body">
          <!-- 左：關鍵字 + 圖片 -->
          <div class="ic-left">
            <div>
              <div class="col-label">觸發關鍵字</div>
              {% if id != 'default' %}
              <input type="text" name="kw_{{ id }}" value="{{ cfg.keywords.get(id,[])|join(', ') }}" placeholder="關鍵字1, 關鍵字2, ...">
              <div class="hint">逗號分隔，不分大小寫</div>
              {% else %}
              <div style="font-size:13px;color:#bbb;padding:6px 0">無關鍵字命中時自動觸發</div>
              {% endif %}
            </div>
            <div>
              <div class="col-label">圖片回覆（選填）</div>
              {% if img %}
              <div id="img-preview-{{ id }}" style="margin-bottom:8px">
                <img src="{{ img }}" style="max-width:90px;max-height:64px;border-radius:6px;border:1px solid #eee;display:block;margin-bottom:5px">
                <button type="button" onclick="clearImage('{{ id }}')" style="border:none;background:none;color:#e57373;cursor:pointer;font-size:12px">✕ 移除圖片</button>
              </div>
              {% else %}
              <div id="img-preview-{{ id }}" style="display:none;margin-bottom:8px">
                <img id="img-thumb-{{ id }}" src="" style="max-width:90px;max-height:64px;border-radius:6px;border:1px solid #eee;display:block;margin-bottom:5px">
                <button type="button" onclick="clearImage('{{ id }}')" style="border:none;background:none;color:#e57373;cursor:pointer;font-size:12px">✕ 移除圖片</button>
              </div>
              {% endif %}
              <input type="hidden" name="img_{{ id }}" id="img-url-{{ id }}" value="{{ img }}">
              <label style="display:inline-flex;align-items:center;gap:5px;background:#f5f5f5;border:1px solid #ddd;border-radius:7px;padding:6px 11px;cursor:pointer;font-size:12px;color:#555">
                📷 上傳圖片
                <input type="file" accept="image/*" style="display:none" onchange="uploadImg('{{ id }}',this)">
              </label>
            </div>
          </div>
          <!-- 右：回覆內容 -->
          <div class="ic-right">
            <div>
              <div class="col-label">自動回覆內容</div>
              <textarea name="reply_{{ id }}" style="min-height:160px">{{ cfg.replies.get(id,'') }}</textarea>
            </div>
            {% if id not in builtin_intents %}
            <div class="ic-del">
              <button type="button" class="del-btn" onclick="delIntent('{{ id }}')">✕ 刪除類別</button>
            </div>
            {% endif %}
          </div>
        </div>
      </div>
      {% endfor %}
      <div class="btn-row">
        <button class="btn btn-save" type="submit" name="action" value="save">💾 儲存</button>
        <button class="btn btn-save" type="submit" name="action" value="deploy" style="background:#1a1a1a">🚀 部署</button>
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
function uploadImg(intentKey, input){
  const file = input.files[0];
  if(!file) return;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('intent_key', intentKey);
  fetch('/admin/'+PLATFORM+'/upload-image?key='+KEY, {method:'POST', body:fd})
    .then(r=>r.json()).then(d=>{
      if(d.url){
        const prev = document.getElementById('img-preview-'+intentKey);
        const thumb = document.getElementById('img-thumb-'+intentKey);
        if(thumb) thumb.src = d.url;
        prev.style.display = 'block';
        document.getElementById('img-url-'+intentKey).value = d.url;
      } else alert('上傳失敗：'+(d.error||''));
    });
}
function clearImage(intentKey){
  document.getElementById('img-url-'+intentKey).value='';
  const prev=document.getElementById('img-preview-'+intentKey);
  prev.style.display='none';
}
function delIntent(key){
  if(!confirm('確定刪除「'+key+'」這個類別？'))return;
  document.getElementById('del-key').value=key;
  document.getElementById('del-form').submit();
}
function toggleCard(id,cb){
  const card=document.getElementById('card-'+id);
  if(card){card.classList.toggle('disabled',!cb.checked);}
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
    "line":       {"name": "LINE Bot 管理",       "ac": "#00c300", "ac_rgb": "0,195,0"},
    "fb":         {"name": "FB Messenger 管理",   "ac": "#1877f2", "ac_rgb": "24,119,242"},
    "fb_comment": {"name": "FB 留言自動回覆",      "ac": "#e91e63", "ac_rgb": "233,30,99"},
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
        if f"reply_{k}" in request.form:
            cfg["replies"][k] = request.form[f"reply_{k}"]
        img_val = request.form.get(f"img_{k}", "")
        if img_val:
            cfg["image_urls"][k] = img_val
        elif f"img_{k}" in request.form and not img_val:
            cfg["image_urls"].pop(k, None)
        cfg["enabled_intents"][k] = f"enabled_{k}" in request.form
        if k != "default" and f"kw_{k}" in request.form:
            cfg["keywords"][k] = [w.strip() for w in request.form[f"kw_{k}"].split(",") if w.strip()]
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
        if RENDER_DEPLOY_HOOK:
            try:
                urllib.request.urlopen(urllib.request.Request(RENDER_DEPLOY_HOOK, method="POST"))
            except Exception:
                pass
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
    out.append("LINE_IMAGE_URLS = {\n" + dict_to_py(lp["image_urls"]) + "}\n\n")
    out.append("FB_IMAGE_URLS = {\n" + dict_to_py(fp["image_urls"]) + "}\n\n")

    def bool_dict_to_py(d, indent=4):
        sp = " " * indent
        lines = []
        for k, v in d.items():
            lines.append(f'{sp}"{k}": {v},\n')
        return "".join(lines)

    out.append("LINE_ENABLED_INTENTS = {\n" + bool_dict_to_py(lp["enabled_intents"]) + "}\n\n")
    out.append("FB_ENABLED_INTENTS = {\n" + bool_dict_to_py(fp["enabled_intents"]) + "}\n\n")
    out.append("_BASE_REPLIES = LINE_REPLIES\n")
    out.append("_BASE_KEYWORDS = LINE_KEYWORDS\n")
    return "".join(out)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
