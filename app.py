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
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent, StickerMessageContent
from knowledge_base import (
    BRAND_INFO, LINE_ENABLED, FB_ENABLED, INTENT_LABELS,
    LINE_REPLIES, FB_REPLIES, LINE_KEYWORDS, FB_KEYWORDS,
    LINE_IMAGE_URLS, FB_IMAGE_URLS,
    LINE_ENABLED_INTENTS, FB_ENABLED_INTENTS,
    FB_COMMENT_REPLIES, FB_COMMENT_KEYWORDS, FB_COMMENT_ENABLED_INTENTS,
    FB_COMMENT_PRIVATE_REPLIES
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

COOLDOWN_SECONDS = 0
LOGS_FILE = "logs.json"
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")

def _load_logs():
    try:
        with open(LOGS_FILE, "r", encoding="utf-8") as f:
            return deque(json.load(f), maxlen=2000)
    except Exception:
        return deque(maxlen=2000)

message_log = _load_logs()

def _save_logs():
    try:
        with open(LOGS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(message_log), f, ensure_ascii=False)
    except Exception:
        pass

_sheets_lock = threading.Lock()

def _append_to_sheets(entry):
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return
    with _sheets_lock:
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            creds = Credentials.from_service_account_info(
                json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            gc = gspread.Client(auth=creds)
            ws = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
            ws.append_row([
                entry["time"],
                entry["platform"],
                entry.get("user_id", ""),
                entry["msg"],
                entry["intent"],
                entry.get("reply", ""),
                "已回覆" if entry["replied"] else "冷卻中",
            ])
        except Exception as e:
            import sys
            print(f"[Sheets Error] {e}", file=sys.stderr)

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
        "replies":          dict(FB_COMMENT_REPLIES),
        "private_replies":  dict(FB_COMMENT_PRIVATE_REPLIES),
        "keywords":         {k: list(v) for k, v in FB_COMMENT_KEYWORDS.items()},
        "labels":           dict(INTENT_LABELS),
        "image_urls":       {},
        "enabled_intents":  dict(FB_COMMENT_ENABLED_INTENTS),
    },
}

cooldowns = {"line": {}, "fb": {}, "fb_comment": {}}

# 人工接手：儲存 "platform:user_id"，接手中的用戶不觸發自動回覆
manual_takeover = set()

# 用戶資料快取 {"platform:user_id": {"name": "", "avatar": ""}}
user_profiles = {}

# 用戶備註 {"platform:user_id": "備註內容"}
NOTES_FILE = "notes.json"
def _load_notes():
    try:
        with open(NOTES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
user_notes = _load_notes()
def _save_notes():
    try:
        with open(NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(user_notes, f, ensure_ascii=False)
    except Exception:
        pass

# 貼文指定回覆 {post_id: {"reply": str, "image_url": str, "enabled": bool}}
fb_post_replies: dict = {}

# 快速回覆模板 [{id, title, text, image_url, price}]
TEMPLATES_FILE = "templates.json"
def _load_templates():
    try:
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []
quick_reply_templates = _load_templates()
def _save_templates():
    try:
        with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
            json.dump(quick_reply_templates, f, ensure_ascii=False)
    except Exception:
        pass

# 對話標籤 {"platform:user_id": ["待跟進", "已成交", ...]}
TAGS_FILE = "conv_tags.json"
def _load_tags():
    try:
        with open(TAGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
conv_tags = _load_tags()
def _save_tags():
    try:
        with open(TAGS_FILE, "w", encoding="utf-8") as f:
            json.dump(conv_tags, f, ensure_ascii=False)
    except Exception:
        pass

# 最後查看時間 {"platform:user_id": timestamp}
last_seen = {}

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
    replied = not cooled and cfg["enabled"] and intent_on
    reply_text = cfg["replies"].get(intent, cfg["replies"].get("default", "")) if replied else ""
    image_url = cfg["image_urls"].get(intent, "") if replied else ""
    log_message({
        "time": time.strftime("%Y/%m/%d %H:%M:%S", time.gmtime(now + 8 * 3600)),
        "platform": platform.upper(),
        "user_id": user_id,
        "msg": text,
        "intent": intent,
        "reply": reply_text,
        "replied": replied,
    })
    if not replied or f"{platform}:{user_id}" in manual_takeover:
        return None, None
    user_times[intent] = now
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


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_line_image(event):
    msg_id = event.message.id
    user_id = event.source.user_id
    image_url = ""
    try:
        dl_url = f"https://api-data.line.me/v2/bot/message/{msg_id}/content"
        req = urllib.request.Request(dl_url, headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        filename = f"{int(time.time())}_{msg_id}.jpg"
        image_url = upload_image_to_github(filename, data) or ""
    except Exception:
        pass
    now = time.strftime("%Y/%m/%d %H:%M:%S", time.gmtime(time.time() + 8*3600))
    log_message({"time": now, "platform": "LINE", "user_id": user_id,
                 "msg": "[圖片]", "intent": "image", "reply": "", "replied": False, "image_url": image_url})

@handler.add(MessageEvent, message=StickerMessageContent)
def handle_line_sticker(event):
    user_id = event.source.user_id
    stk_id = event.message.sticker_id
    sticker_url = f"https://stickershop.line-scdn.net/stickershop/v1/sticker/{stk_id}/iPhone/sticker@2x.png"
    now = time.strftime("%Y/%m/%d %H:%M:%S", time.gmtime(time.time() + 8*3600))
    log_message({"time": now, "platform": "LINE", "user_id": user_id,
                 "msg": "[貼圖]", "intent": "sticker", "reply": "", "replied": False, "image_url": sticker_url})
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
    post_id = val.get("post_id", "").split("_")[-1]  # 取純數字 post_id
    user_id = val.get("from", {}).get("id", "")
    text = val.get("message", "").strip()
    if not comment_id or not text:
        return
    if not platforms["fb_comment"]["enabled"]:
        return
    # 先查貼文指定回覆
    post_cfg = fb_post_replies.get(post_id, {})
    if post_cfg and post_cfg.get("enabled", True):
        reply_text = post_cfg.get("reply", "")
        image_url = post_cfg.get("image_url", "")
    else:
        reply_text, _ = get_reply(text, user_id, "fb_comment")
        if not reply_text:
            return
        intent = classify_intent(text, "fb_comment")
        image_url = platforms["fb_comment"]["image_urls"].get(intent, "")
    fb_reply_comment(comment_id, reply_text, image_url)
    intent = classify_intent(text, "fb_comment")
    private_msg = platforms["fb_comment"]["private_replies"].get(intent, platforms["fb_comment"]["private_replies"].get("default", ""))
    if private_msg:
        fb_private_reply(comment_id, private_msg, image_url)

def fb_reply_comment(comment_id: str, text: str, image_url: str = ""):
    if not FB_PAGE_ACCESS_TOKEN:
        return
    url = f"https://graph.facebook.com/v22.0/{comment_id}/comments?access_token={FB_PAGE_ACCESS_TOKEN}"
    body: dict = {"message": text}
    if image_url:
        body["attachment_url"] = image_url
    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass

def fb_private_reply(comment_id: str, text: str, image_url: str = ""):
    if not FB_PAGE_ACCESS_TOKEN:
        return
    url = f"https://graph.facebook.com/v22.0/{comment_id}/private_replies?access_token={FB_PAGE_ACCESS_TOKEN}"
    body: dict = {"message": text}
    if image_url:
        body["attachment"] = {"type": "image", "payload": {"url": image_url, "is_reusable": True}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass

def fb_send(psid: str, text: str):
    if not FB_PAGE_ACCESS_TOKEN:
        return
    url = f"https://graph.facebook.com/v22.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
    payload = json.dumps({"recipient": {"id": psid}, "message": {"text": text}}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass

def fb_send_image(psid: str, image_url: str):
    if not FB_PAGE_ACCESS_TOKEN:
        return
    url = f"https://graph.facebook.com/v22.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
    payload = json.dumps({"recipient": {"id": psid}, "message": {
        "attachment": {"type": "image", "payload": {"url": image_url, "is_reusable": True}}
    }}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass

def get_user_profile(platform: str, user_id: str) -> dict:
    key = f"{platform}:{user_id}"
    if key in user_profiles:
        return user_profiles[key]
    profile = {"name": "", "avatar": ""}
    try:
        if platform == "LINE":
            url = f"https://api.line.me/v2/bot/profile/{user_id}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"})
            with urllib.request.urlopen(req, timeout=5) as r:
                d = json.loads(r.read())
                profile = {"name": d.get("displayName",""), "avatar": d.get("pictureUrl","")}
        elif platform == "FB":
            url = f"https://graph.facebook.com/v22.0/{user_id}?fields=name,picture.type(large)&access_token={FB_PAGE_ACCESS_TOKEN}"
            with urllib.request.urlopen(url, timeout=5) as r:
                d = json.loads(r.read())
                avatar = (d.get("picture") or {}).get("data", {}).get("url", "")
                profile = {"name": d.get("name", ""), "avatar": avatar}
    except Exception:
        pass
    user_profiles[key] = profile
    return profile

def line_push(user_id: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    payload = json.dumps({"to": user_id, "messages": [{"type": "text", "text": text}]}).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    })
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass

def line_push_image(user_id: str, image_url: str):
    url = "https://api.line.me/v2/bot/message/push"
    payload = json.dumps({"to": user_id, "messages": [{"type": "image", "originalContentUrl": image_url, "previewImageUrl": image_url}]}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"})
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

def _load_from_sheets():
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return []
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gc = gspread.Client(auth=creds)
        ws = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
        rows = ws.get_all_values()
        logs = []
        for r in reversed(rows):
            if len(r) >= 7:
                logs.append({
                    "time": r[0], "platform": r[1], "user_id": r[2],
                    "msg": r[3], "intent": r[4], "reply": r[5],
                    "replied": r[6] == "已回覆"
                })
        return logs[:500]
    except Exception as e:
        import sys
        print(f"[Sheets Read Error] {e}", file=sys.stderr)
        return []

def _preload_from_sheets():
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return
    if len(message_log) > 0:
        return
    logs = _load_from_sheets()
    for entry in reversed(logs):
        message_log.appendleft(entry)

threading.Thread(target=_preload_from_sheets, daemon=True).start()

@app.route("/api/logs")
def api_logs():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    platform = request.args.get("platform", "all")
    fmt = request.args.get("format", "json")
    logs = list(message_log)
    if not logs and GOOGLE_SHEET_ID:
        logs = _load_from_sheets()
    if platform != "all":
        logs = [l for l in logs if l.get("platform", "") == platform.upper()]
    if fmt == "csv":
        import io, csv
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["時間", "平台", "用戶ID", "用戶訊息", "意圖", "Bot回覆", "狀態"])
        for l in logs:
            w.writerow([
                l.get("time", ""), l.get("platform", ""), l.get("user_id", ""),
                l.get("msg", ""), l.get("intent", ""), l.get("reply", ""),
                "已回覆" if l.get("replied") else "冷卻中"
            ])
        from flask import Response
        return Response(out.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=logs.csv"})
    return jsonify(logs)

# ── Admin HTML ────────────────────────────────────────────

INBOX_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>訊息收件匣</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f0f2f5;height:100vh;display:flex;flex-direction:column}
.header{background:#1a1a1a;color:#fff;padding:13px 18px;display:flex;align-items:center;gap:12px;flex-shrink:0}
.back{color:#aaa;text-decoration:none;font-size:20px}
.header h1{font-size:16px;font-weight:700;flex:1}
.main{display:flex;flex:1;overflow:hidden}
/* 左側對話列表 */
.sidebar{width:320px;background:#fff;border-right:1px solid #e0e0e0;display:flex;flex-direction:column;flex-shrink:0}
.sidebar-head{padding:12px 14px;border-bottom:1px solid #f0f0f0;font-size:13px;font-weight:700;color:#888}
.conv-list{overflow-y:auto;flex:1}
.conv-item{padding:12px 14px;border-bottom:1px solid #f7f7f7;cursor:pointer;display:flex;gap:10px;align-items:flex-start}
.conv-item:hover{background:#f7f9fc}
.conv-item.active{background:#e8f4fd}
.conv-avatar{width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
.conv-info{flex:1;min-width:0}
.conv-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:3px}
.conv-name{font-size:13px;font-weight:700;color:#333;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.conv-time{font-size:11px;color:#bbb;flex-shrink:0}
.conv-preview{font-size:12px;color:#888;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.conv-note{font-size:11px;color:#f5a623;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pf-badge{font-size:10px;font-weight:700;padding:1px 6px;border-radius:6px;margin-right:4px}
.pf-line{background:#e8f5e9;color:#00a000}
.pf-fb{background:#e3f2fd;color:#1877f2}
.pf-fb_comment{background:#fce4ec;color:#e91e63}
.manual-badge{font-size:10px;background:#fff3e0;color:#e65100;padding:1px 6px;border-radius:6px;margin-left:4px}
/* 右側聊天區 */
.chat{flex:1;display:flex;flex-direction:column;min-width:0}
.chat-head{padding:12px 16px;background:#fff;border-bottom:1px solid #e0e0e0;display:flex;align-items:center;justify-content:space-between}
.chat-title{font-size:14px;font-weight:700}
.takeover-btn{padding:6px 14px;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer}
.takeover-on{background:#fff3e0;color:#e65100}
.takeover-off{background:#e8f5e9;color:#2e7d32}
.chat-messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.msg-row{display:flex;gap:8px;max-width:75%}
.msg-row.user{align-self:flex-start}
.msg-row.bot{align-self:flex-end;flex-direction:row-reverse}
.bubble{padding:10px 14px;border-radius:16px;font-size:14px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
.msg-row.user .bubble{background:#fff;border:1px solid #e0e0e0;color:#333;border-top-left-radius:4px}
.msg-row.bot .bubble{background:#1a1a1a;color:#fff;border-top-right-radius:4px}
.msg-time{font-size:10px;color:#bbb;margin-top:4px;text-align:center;align-self:flex-end}
.empty-chat{flex:1;display:flex;align-items:center;justify-content:center;color:#ccc;font-size:14px}
.chat-input{padding:12px 16px;background:#fff;border-top:1px solid #e0e0e0;display:flex;gap:10px}
.chat-input textarea{flex:1;border:1px solid #e0e0e0;border-radius:10px;padding:10px 14px;font-size:14px;font-family:inherit;resize:none;height:60px;line-height:1.5}
.chat-input textarea:focus{outline:none;border-color:#1a1a1a}
.send-btn{padding:0 20px;background:#1a1a1a;color:#fff;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;flex-shrink:0}
.send-btn:hover{opacity:.85}
.send-btn:disabled{opacity:.4;cursor:not-allowed}
.note-bar{padding:8px 16px;background:#fffbf0;border-top:1px solid #ffe082;display:flex;gap:8px;align-items:center}
.note-bar input{flex:1;border:1px solid #ffe082;border-radius:8px;padding:6px 10px;font-size:13px;font-family:inherit;background:#fff}
.note-bar input:focus{outline:none;border-color:#f5a623}
.note-save-btn{padding:6px 14px;background:#f5a623;color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap}
.user-avatar{width:38px;height:38px;border-radius:50%;object-fit:cover;flex-shrink:0}
.user-avatar-fallback{width:38px;height:38px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0}
/* 標籤 */
.tag{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px;cursor:pointer;user-select:none}
.tag-待跟進{background:#fff3e0;color:#e65100}
.tag-已報價{background:#e3f2fd;color:#1565c0}
.tag-已成交{background:#e8f5e9;color:#2e7d32}
.tag-VIP{background:#f3e5f5;color:#7b1fa2}
.tag-問題客{background:#fdecea;color:#c62828}
.tag-inactive{opacity:.35}
/* 未讀紅點 */
.unread-dot{display:inline-flex;align-items:center;justify-content:center;background:#e53935;color:#fff;font-size:10px;font-weight:700;border-radius:10px;min-width:18px;height:18px;padding:0 4px;margin-left:4px}
/* 模板面板 */
.tpl-panel{position:absolute;bottom:140px;left:0;right:0;background:#fff;border-top:2px solid #e0e0e0;max-height:340px;overflow-y:auto;z-index:50;display:none;flex-direction:column}
.tpl-panel.open{display:flex}
.tpl-head{padding:10px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #f0f0f0;flex-shrink:0}
.tpl-head-title{font-size:13px;font-weight:700;color:#555}
.tpl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;padding:12px 16px}
.tpl-card{border:1px solid #e8e8e8;border-radius:10px;overflow:hidden;cursor:pointer;transition:box-shadow .2s}
.tpl-card:hover{box-shadow:0 3px 12px rgba(0,0,0,.12)}
.tpl-img{width:100%;height:100px;object-fit:cover;background:#f5f5f5;display:block}
.tpl-img-empty{width:100%;height:100px;background:#f5f5f5;display:flex;align-items:center;justify-content:center;color:#ccc;font-size:24px}
.tpl-body{padding:8px 10px}
.tpl-title{font-size:13px;font-weight:700;color:#333;margin-bottom:2px}
.tpl-price{font-size:12px;color:#e53935;font-weight:700}
.tpl-actions{display:flex;gap:6px;margin-top:6px}
.tpl-use-btn{flex:1;padding:5px;background:#1a1a1a;color:#fff;border:none;border-radius:6px;font-size:11px;cursor:pointer;font-weight:600}
.tpl-del-btn{padding:5px 8px;background:#fdecea;color:#c62828;border:none;border-radius:6px;font-size:11px;cursor:pointer}
.tpl-add-card{border:2px dashed #e0e0e0;border-radius:10px;display:flex;align-items:center;justify-content:center;min-height:160px;cursor:pointer;color:#bbb;font-size:13px;font-weight:600;gap:6px}
.tpl-add-card:hover{border-color:#1a1a1a;color:#333}
/* 新增模板表單 */
.tpl-form{padding:14px 16px;border-top:1px solid #f0f0f0;display:none;flex-direction:column;gap:8px}
.tpl-form.open{display:flex}
.tpl-form input,.tpl-form textarea{border:1px solid #e0e0e0;border-radius:8px;padding:8px 10px;font-size:13px;font-family:inherit}
.tpl-form textarea{min-height:70px;resize:vertical}
.tpl-btn-row{display:flex;gap:8px}
.tpl-confirm-btn{flex:1;padding:8px;background:#1a1a1a;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer}
.tpl-cancel-btn{padding:8px 16px;background:#f5f5f5;color:#555;border:none;border-radius:8px;font-size:13px;cursor:pointer}
@media(max-width:640px){.sidebar{width:100%;display:none}.sidebar.show{display:flex}.chat{display:none}.chat.show{display:flex}}
</style></head>
<body>
<div class="header">
  <a class="back" href="/admin?key={{ key }}">‹</a>
  <h1>📬 訊息收件匣</h1>
</div>
<div class="main">
  <div class="sidebar">
    <div class="sidebar-head" style="display:flex;align-items:center;justify-content:space-between">
      <span>所有對話</span>
      <button onclick="showTemplateManager()" style="font-size:12px;padding:4px 10px;background:#1a1a1a;color:#fff;border:none;border-radius:6px;cursor:pointer">⚡ 管理模板</button>
    </div>
    <div class="conv-list" id="conv-list"><div style="padding:30px;text-align:center;color:#ccc;font-size:13px">載入中...</div></div>
  </div>
  <div class="chat" id="chat-panel">
    <div class="empty-chat" id="empty-chat">← 選擇一個對話開始</div>

    <!-- 模板管理頁面 -->
    <div id="tpl-manager" style="display:none;flex:1;flex-direction:column;overflow:hidden">
      <div class="chat-head">
        <div><div class="chat-title">⚡ 快速回覆模板</div><div style="font-size:11px;color:#aaa;margin-top:2px">點「新增」建立模板，發送時一鍵帶入</div></div>
      </div>
      <div style="padding:16px;overflow-y:auto;flex:1">
        <!-- 新增按鈕列 -->
        <div style="display:flex;gap:8px;margin-bottom:16px">
          <button onclick="showAddForm('image')" style="flex:1;padding:10px;background:#1a1a1a;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer">🖼️ 圖片＋文字</button>
          <button onclick="showAddForm('text')" style="flex:1;padding:10px;background:#444;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer">💬 純文字</button>
        </div>
        <!-- 新增表單 -->
        <div id="mgr-form" style="display:none;background:#f9f9f9;border-radius:10px;padding:14px;margin-bottom:16px;border:1px solid #e0e0e0">
          <div id="mgr-form-title" style="font-size:13px;font-weight:700;margin-bottom:10px;color:#333"></div>
          <div style="display:flex;flex-direction:column;gap:8px">
            <input type="text" id="mgr-title" placeholder="模板名稱（例如：加購床墊）" style="border:1px solid #e0e0e0;border-radius:8px;padding:8px 10px;font-size:13px">
            <input type="text" id="mgr-price" placeholder="價格標示（選填，例如：NT$2,999）" style="border:1px solid #e0e0e0;border-radius:8px;padding:8px 10px;font-size:13px">
            <input type="text" id="mgr-image" placeholder="圖片網址（選填）" style="border:1px solid #e0e0e0;border-radius:8px;padding:8px 10px;font-size:13px" id="mgr-image-field">
            <textarea id="mgr-text" placeholder="回覆文字內容" style="border:1px solid #e0e0e0;border-radius:8px;padding:8px 10px;font-size:13px;min-height:80px;resize:vertical;font-family:inherit"></textarea>
            <div style="display:flex;gap:8px">
              <button onclick="submitAddTemplate()" style="flex:1;padding:9px;background:#00c300;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer">✓ 儲存模板</button>
              <button onclick="document.getElementById('mgr-form').style.display='none'" style="padding:9px 16px;background:#f0f0f0;border:none;border-radius:8px;font-size:13px;cursor:pointer">取消</button>
            </div>
          </div>
        </div>
        <!-- 模板列表 -->
        <div id="mgr-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px"></div>
      </div>
    </div>
    <div id="chat-main" style="display:none;flex:1;flex-direction:column;overflow:hidden;display:none">
      <div class="chat-head">
        <div style="flex:1;min-width:0">
          <div class="chat-title" id="chat-title">—</div>
          <div style="font-size:11px;color:#aaa;margin-top:2px" id="chat-uid">—</div>
          <div id="tag-bar" style="display:flex;gap:5px;flex-wrap:wrap;margin-top:6px"></div>
        </div>
        <button class="takeover-btn takeover-off" id="takeover-btn" onclick="toggleTakeover()">自動回覆中</button>
      </div>
      <div class="chat-messages" id="chat-messages"></div>
      <div class="note-bar">
        <span style="font-size:12px;color:#f5a623;font-weight:700;white-space:nowrap">📝 備註</span>
        <input type="text" id="note-input" placeholder="輸入客戶備註（例如：已報價、等回覆）">
        <button class="note-save-btn" onclick="saveNote()">儲存</button>
      </div>
      <div style="position:relative">
        <div class="tpl-panel" id="tpl-panel">
          <div class="tpl-head">
            <span class="tpl-head-title">⚡ 快速回覆模板</span>
            <div style="display:flex;gap:8px">
              <button onclick="showTplForm()" style="font-size:12px;padding:4px 10px;background:#1a1a1a;color:#fff;border:none;border-radius:6px;cursor:pointer">＋ 新增</button>
              <button onclick="toggleTpl()" style="font-size:18px;background:none;border:none;cursor:pointer;color:#aaa">×</button>
            </div>
          </div>
          <div class="tpl-form" id="tpl-form">
            <input type="text" id="tpl-title" placeholder="模板名稱（例如：加購床墊）">
            <input type="text" id="tpl-price" placeholder="價格（例如：NT$2,999）">
            <input type="text" id="tpl-image" placeholder="圖片網址（選填）">
            <textarea id="tpl-text" placeholder="回覆文字內容"></textarea>
            <div class="tpl-btn-row">
              <button class="tpl-confirm-btn" onclick="addTemplate()">新增模板</button>
              <button class="tpl-cancel-btn" onclick="hideTplForm()">取消</button>
            </div>
          </div>
          <div class="tpl-grid" id="tpl-grid"></div>
        </div>
      </div>
      <div class="chat-input">
        <button onclick="toggleTpl()" style="padding:0 12px;background:#f5f5f5;border:1px solid #e0e0e0;border-radius:10px;font-size:13px;cursor:pointer;white-space:nowrap" title="快速回覆模板">⚡ 模板</button>
        <input type="file" id="img-file-input" accept="image/*" style="display:none" onchange="uploadAndSendImage(this)">
        <button onclick="document.getElementById('img-file-input').click()" style="padding:0 12px;background:#f5f5f5;border:1px solid #e0e0e0;border-radius:10px;font-size:18px;cursor:pointer" title="傳送圖片">🖼️</button>
        <textarea id="reply-input" placeholder="輸入回覆訊息... (Ctrl+Enter 送出)" onkeydown="if(event.ctrlKey&&event.key==='Enter')sendReply()"></textarea>
        <button class="send-btn" id="send-btn" onclick="sendReply()">送出</button>
      </div>
    </div>
  </div>
</div>
<script>
const KEY="{{ key }}";
let currentConv=null, convData={};

async function loadConversations(){
  const list=document.getElementById('conv-list');
  try{
  const res=await fetch('/api/conversations?key='+KEY);
  if(!res.ok){list.innerHTML='<div style="padding:30px;text-align:center;color:#e53935;font-size:13px">載入失敗（'+res.status+'）<br><a href="" style="color:#1877f2">重新整理</a></div>';return;}
  const data=await res.json();
  convData=data;
  if(!Array.isArray(data)||!data.length){list.innerHTML='<div style="padding:30px;text-align:center;color:#ccc;font-size:13px">尚無對話記錄</div>';return;}
  list.innerHTML=data.map((c,i)=>{
    const pfClass=c.platform==='LINE'?'pf-line':c.platform==='FB'?'pf-fb':'pf-fb_comment';
    const pfIcon=c.platform==='LINE'?'💬':c.platform==='FB'?'📘':'💬';
    const avatarBg=c.platform==='LINE'?'#e8f5e9':c.platform==='FB'?'#e3f2fd':'#fce4ec';
    const manualBadge=c.manual?'<span class="manual-badge">人工</span>':'';
    const displayName=c.name||c.user_id.slice(0,12)+'...';
    const avatar=c.avatar?`<img class="user-avatar" src="${c.avatar}" onerror="this.style.display='none'">`:
      `<div class="user-avatar-fallback" style="background:${avatarBg}">${pfIcon}</div>`;
    const notePreview=c.note?`<div class="conv-note">📝 ${c.note}</div>`:'';
    const unreadBadge=c.unread>0?`<span class="unread-dot">${c.unread}</span>`:'';
    const tagPills=(c.tags||[]).map(t=>`<span class="tag tag-${t}">${t}</span>`).join('');
    return `<div class="conv-item" id="conv-${i}" onclick="openConv(${i})">
      ${avatar}
      <div class="conv-info">
        <div class="conv-top">
          <div class="conv-name"><span class="pf-badge ${pfClass}">${c.platform}</span>${displayName}${manualBadge}${unreadBadge}</div>
          <div class="conv-time">${c.last_time.slice(5,16)}</div>
        </div>
        <div class="conv-preview">${c.last_msg}</div>
        ${tagPills?`<div style="margin-top:4px;display:flex;gap:4px;flex-wrap:wrap">${tagPills}</div>`:''}
        ${notePreview}
      </div>
    </div>`;
  }).join('');
  }catch(e){list.innerHTML='<div style="padding:30px;text-align:center;color:#e53935;font-size:13px">載入錯誤：'+e.message+'<br><a href="" style="color:#1877f2">重新整理</a></div>';}
}

function openConv(i){
  document.querySelectorAll('.conv-item').forEach(e=>e.classList.remove('active'));
  document.getElementById('conv-'+i).classList.add('active');
  currentConv=convData[i];
  document.getElementById('empty-chat').style.display='none';
  const main=document.getElementById('chat-main');
  main.style.display='flex';
  main.style.flexDirection='column';
  document.getElementById('chat-title').textContent=currentConv.name||currentConv.user_id;
  document.getElementById('chat-uid').textContent=currentConv.platform+' · '+currentConv.user_id;
  document.getElementById('note-input').value=currentConv.note||'';
  renderTags(currentConv.tags||[]);
  fetch('/api/seen?key='+KEY,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({platform:currentConv.platform,user_id:currentConv.user_id})});
  const btn=document.getElementById('takeover-btn');
  if(currentConv.manual){btn.textContent='人工接手中';btn.className='takeover-btn takeover-on';}
  else{btn.textContent='自動回覆中';btn.className='takeover-btn takeover-off';}
  renderMessages(currentConv.messages);
}

function renderMessages(msgs){
  const el=document.getElementById('chat-messages');
  el.innerHTML=msgs.map(m=>{
    let mc=m.msg;
    if(m.image_url&&(m.msg==='[圖片]'||m.msg==='[手動圖片]'))
      mc=`<img src="${m.image_url}" style="max-width:200px;max-height:200px;border-radius:8px;display:block;cursor:pointer" onclick="window.open('${m.image_url}','_blank')">`
    else if(m.image_url&&m.msg==='[貼圖]')
      mc=`<img src="${m.image_url}" style="width:90px;height:90px;object-fit:contain;display:block">`;
    const userRow=`<div class="msg-row user"><div><div class="bubble">${mc}</div><div class="msg-time">${m.time}</div></div></div>`;
    const botRow=m.reply?`<div class="msg-row bot"><div><div class="bubble">${m.reply}</div></div></div>`:'';
    return userRow+botRow;
  }).join('');
  el.scrollTop=el.scrollHeight;
}

async function toggleTakeover(){
  if(!currentConv)return;
  const res=await fetch('/api/takeover?key='+KEY,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({platform:currentConv.platform,user_id:currentConv.user_id,active:!currentConv.manual})});
  const d=await res.json();
  currentConv.manual=d.active;
  const btn=document.getElementById('takeover-btn');
  if(d.active){btn.textContent='人工接手中';btn.className='takeover-btn takeover-on';}
  else{btn.textContent='自動回覆中';btn.className='takeover-btn takeover-off';}
  await loadConversations();
}

async function sendReply(){
  if(!currentConv)return;
  const text=document.getElementById('reply-input').value.trim();
  if(!text)return;
  const btn=document.getElementById('send-btn');
  btn.disabled=true;
  const res=await fetch('/api/reply?key='+KEY,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({platform:currentConv.platform,user_id:currentConv.user_id,text})});
  const d=await res.json();
  if(d.ok){
    document.getElementById('reply-input').value='';
    currentConv.messages.push({time:new Date().toLocaleTimeString('zh-TW'),msg:'（你）'+text,reply:''});
    renderMessages(currentConv.messages);
  } else alert('發送失敗：'+(d.error||''));
  btn.disabled=false;
}


async function uploadAndSendImage(input){
  if(!currentConv||!input.files[0])return;
  const btn=document.getElementById('send-btn');
  btn.disabled=true;
  try{
    const fd=new FormData();
    fd.append('file',input.files[0]);
    const up=await fetch('/admin/line/upload-image?key='+KEY,{method:'POST',body:fd});
    const ud=await up.json();
    if(!ud.url){alert('圖片上傳失敗：'+(ud.error||''));btn.disabled=false;input.value='';return;}
    const res=await fetch('/api/reply?key='+KEY,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({platform:currentConv.platform,user_id:currentConv.user_id,image_url:ud.url})});
    const d=await res.json();
    if(d.ok){
      currentConv.messages.push({time:new Date().toLocaleTimeString('zh-TW'),msg:'[手動圖片]',reply:'',image_url:ud.url});
      renderMessages(currentConv.messages);
    }else alert('發送失敗：'+(d.error||''));
  }catch(e){alert('錯誤：'+e.message);}
  input.value='';
  btn.disabled=false;
}

const ALL_TAGS=['待跟進','已報價','已成交','VIP','問題客'];
function renderTags(active){
  const bar=document.getElementById('tag-bar');
  bar.innerHTML=ALL_TAGS.map(t=>{
    const on=active.includes(t);
    return `<span class="tag tag-${t}${on?'':' tag-inactive'}" onclick="toggleTag('${t}')">${t}</span>`;
  }).join('');
}
async function toggleTag(tag){
  if(!currentConv)return;
  const tags=currentConv.tags||[];
  const idx=tags.indexOf(tag);
  if(idx>=0)tags.splice(idx,1);else tags.push(tag);
  currentConv.tags=tags;
  renderTags(tags);
  await fetch('/api/tags?key='+KEY,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({platform:currentConv.platform,user_id:currentConv.user_id,tags})});
  await loadConversations();
}

let templates=[], mgrMode='text';
async function loadTemplates(){
  const res=await fetch('/api/templates?key='+KEY);
  templates=await res.json();
  renderTplGrid();
  renderMgrGrid();
}
function renderTplGrid(){
  const grid=document.getElementById('tpl-grid');
  if(!grid)return;
  grid.innerHTML=templates.map(t=>{
    const img=t.image_url?`<img class="tpl-img" src="${t.image_url}" onerror="this.parentElement.innerHTML='<div class=tpl-img-empty>🖼️</div>'">`:
      `<div class="tpl-img-empty">💬</div>`;
    return `<div class="tpl-card">
      ${img}<div class="tpl-body">
        <div class="tpl-title">${t.title}</div>
        ${t.price?`<div class="tpl-price">${t.price}</div>`:''}
        <div class="tpl-actions">
          <button class="tpl-use-btn" onclick="useTpl('${t.id}')">插入</button>
          <button class="tpl-use-btn" style="background:#00c300" onclick="sendTpl('${t.id}')">發送</button>
        </div>
      </div></div>`;
  }).join('');
}
function renderMgrGrid(){
  const grid=document.getElementById('mgr-grid');
  if(!grid)return;
  if(!templates.length){grid.innerHTML='<div style="grid-column:1/-1;text-align:center;color:#ccc;padding:30px;font-size:13px">尚無模板，點上方按鈕新增</div>';return;}
  grid.innerHTML=templates.map(t=>{
    const img=t.image_url?`<img class="tpl-img" src="${t.image_url}" onerror="this.style.display='none'">`:
      `<div class="tpl-img-empty">💬</div>`;
    return `<div class="tpl-card">
      ${img}<div class="tpl-body">
        <div class="tpl-title">${t.title}</div>
        ${t.price?`<div class="tpl-price">${t.price}</div>`:''}
        <div style="font-size:11px;color:#aaa;margin-top:4px;max-height:36px;overflow:hidden">${t.text.slice(0,40)}${t.text.length>40?'…':''}</div>
        <button class="tpl-del-btn" style="width:100%;margin-top:6px" onclick="delTpl('${t.id}')">✕ 刪除</button>
      </div></div>`;
  }).join('');
}
function showTemplateManager(){
  document.getElementById('empty-chat').style.display='none';
  document.getElementById('tpl-manager').style.display='flex';
  const m=document.getElementById('chat-main');
  m.style.display='none';
  loadTemplates();
}
function showAddForm(mode){
  mgrMode=mode;
  document.getElementById('mgr-form').style.display='block';
  document.getElementById('mgr-form-title').textContent=mode==='image'?'🖼️ 新增圖片＋文字模板':'💬 新增純文字模板';
  document.getElementById('mgr-image').style.display=mode==='image'?'block':'none';
}
async function submitAddTemplate(){
  const title=document.getElementById('mgr-title').value.trim();
  const text=document.getElementById('mgr-text').value.trim();
  const price=document.getElementById('mgr-price').value.trim();
  const image_url=mgrMode==='image'?document.getElementById('mgr-image').value.trim():'';
  if(!title||!text){alert('請填寫名稱和內容');return;}
  await fetch('/api/templates?key='+KEY,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'add',title,text,price,image_url})});
  ['mgr-title','mgr-text','mgr-price','mgr-image'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('mgr-form').style.display='none';
  loadTemplates();
}
async function delTpl(id){
  if(!confirm('刪除這個模板？'))return;
  await fetch('/api/templates?key='+KEY,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'delete',id})});
  loadTemplates();
}
function toggleTpl(){
  const p=document.getElementById('tpl-panel');
  p.classList.toggle('open');
  if(p.classList.contains('open'))loadTemplates();
}
function useTpl(id){
  const t=templates.find(x=>x.id===id);
  if(!t)return;
  let msg=t.text;
  if(t.price)msg+='\\n\\n💰 '+t.price;
  document.getElementById('reply-input').value=msg;
  document.getElementById('tpl-panel').classList.remove('open');
  document.getElementById('reply-input').focus();
}
async function sendTpl(id){
  const t=templates.find(x=>x.id===id);
  if(!t||!currentConv)return;
  let msg=t.text;
  if(t.price)msg+='\\n\\n💰 '+t.price;
  document.getElementById('reply-input').value=msg;
  document.getElementById('tpl-panel').classList.remove('open');
  await sendReply();
  if(t.image_url){
    await fetch('/api/reply?key='+KEY,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({platform:currentConv.platform,user_id:currentConv.user_id,text:t.image_url,type:'image'})});
  }
}

async function saveNote(){
  if(!currentConv)return;
  const note=document.getElementById('note-input').value.trim();
  await fetch('/api/note?key='+KEY,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({platform:currentConv.platform,user_id:currentConv.user_id,note})});
  currentConv.note=note;
  await loadConversations();
}

loadConversations();
setInterval(loadConversations, 15000);
</script>
</body></html>"""

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
  <a class="card" href="/admin/inbox?key={{ key }}">
    <div class="card-left">
      <div class="icon" style="background:#e8f0fe">📬</div>
      <div>
        <div class="card-title">訊息收件匣</div>
        <div class="card-sub">LINE + FB 統一回覆</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span class="arrow">›</span>
    </div>
  </a>
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
        <div class="card-sub">關鍵字比對回覆</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span class="status on">開啟</span>
      <span class="arrow">›</span>
    </div>
  </a>
  <a class="card" href="/admin/fb-posts?key={{ key }}">
    <div class="card-left">
      <div class="icon" style="background:#fce4ec">📌</div>
      <div>
        <div class="card-title">FB 貼文指定回覆</div>
        <div class="card-sub">針對特定貼文設定回覆</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span class="arrow">›</span>
    </div>
  </a>
  <div style="margin:8px 0 4px;font-size:12px;color:#aaa;font-weight:700;letter-spacing:.5px">工具</div>
  <a class="card" href="https://image-processor-t1gd.onrender.com" target="_blank">
    <div class="card-left">
      <div class="icon" style="background:#f3e5f5">🖼️</div>
      <div>
        <div class="card-title">商品圖片處理</div>
        <div class="card-sub">去背 ＋ 白底 ＋ AI 標題建議</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span style="font-size:11px;background:#f3e5f5;color:#7b1fa2;padding:3px 8px;border-radius:8px;font-weight:700">外部工具</span>
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
.log-table{width:100%;border-collapse:collapse;font-size:12px}
.log-table th{text-align:left;padding:8px 10px;background:#f9f9f9;color:#888;font-weight:600;border-bottom:2px solid #eee;white-space:nowrap}
.log-table td{padding:8px 10px;border-bottom:1px solid #f0f0f0;vertical-align:top;word-break:break-all}
.log-table tr:last-child td{border-bottom:none}
.replied-yes{color:#00b050;font-weight:600;font-size:12px}
.replied-no{color:#bbb;font-size:12px}
.log-reply{color:#555;font-size:11px;background:#f7f7f7;padding:4px 6px;border-radius:4px;margin-top:3px;max-height:60px;overflow:hidden;white-space:pre-wrap}
.log-uid{font-size:10px;color:#bbb;font-family:monospace}
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
            {% if platform == 'fb_comment' %}
            <div>
              <div class="col-label">公開回覆（所有人看得到）</div>
              <textarea name="reply_{{ id }}" style="min-height:90px">{{ cfg.replies.get(id,'') }}</textarea>
            </div>
            <div>
              <div class="col-label">私訊內容（只有留言者收到）</div>
              <textarea name="private_reply_{{ id }}" style="min-height:110px">{{ cfg.get('private_replies', {}).get(id,'') }}</textarea>
            </div>
            {% else %}
            <div>
              <div class="col-label">自動回覆內容</div>
              <textarea name="reply_{{ id }}" style="min-height:160px">{{ cfg.replies.get(id,'') }}</textarea>
            </div>
            {% endif %}
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
    let h='<table class="log-table"><thead><tr><th>時間</th><th>平台</th><th>用戶</th><th>用戶訊息</th><th>意圖</th><th>Bot 回覆</th><th>狀態</th></tr></thead><tbody>';
    logs.forEach(l=>{
      const rep=l.replied?'<span class="replied-yes">✓ 已回覆</span>':'<span class="replied-no">冷卻中</span>';
      const uid=l.user_id?`<div class="log-uid">${l.user_id.slice(0,12)}...</div>`:'';
      const reply=l.reply?`<div class="log-reply">${l.reply.slice(0,80)}${l.reply.length>80?'…':''}</div>`:'<span style="color:#ddd">—</span>';
      const pf=l.platform||'';
      const pfColor=pf==='LINE'?'#00c300':pf==='FB'?'#1877f2':'#e91e63';
      const pfBadge=`<span style="font-size:11px;font-weight:700;color:${pfColor}">${pf}</span>`;
      h+=`<tr><td style="white-space:nowrap">${l.time}</td><td>${pfBadge}</td><td>${uid}</td><td>${l.msg}</td><td>${l.intent}</td><td>${reply}</td><td>${rep}</td></tr>`;
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

FB_POSTS_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FB 貼文指定回覆</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333;font-size:15px}
.header{background:#1a1a1a;color:#fff;padding:13px 18px;display:flex;align-items:center;gap:12px}
.back{color:#aaa;text-decoration:none;font-size:20px}
.header h1{font-size:16px;font-weight:700}
.container{max-width:700px;margin:20px auto;padding:0 14px 100px}
.card{background:#fff;border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.card-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.card-title{font-size:14px;font-weight:700}
.badge{font-size:11px;color:#999;background:#f0f0f0;padding:2px 8px;border-radius:8px;font-family:monospace}
label{font-size:12px;color:#888;display:block;margin-bottom:4px;margin-top:10px}
textarea,input[type=text]{width:100%;border:1px solid #e0e0e0;border-radius:8px;padding:9px 11px;font-size:14px;font-family:inherit}
textarea{min-height:80px;resize:vertical;line-height:1.7}
textarea:focus,input[type=text]:focus{outline:none;border-color:#e91e63}
.hint{font-size:12px;color:#bbb;margin-top:4px}
.del-btn{border:none;background:none;color:#e57373;cursor:pointer;font-size:12px;padding:4px 8px;border-radius:6px}
.del-btn:hover{background:#fdecea}
.add-card{border:2px dashed #e0e0e0;border-radius:12px;padding:18px;margin-bottom:12px}
.add-card:hover{border-color:#e91e63}
.add-title{font-size:14px;font-weight:700;color:#888;margin-bottom:12px}
.btn-row{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #eee;padding:10px 14px;display:flex;gap:10px;justify-content:center;z-index:100}
.btn{padding:10px 22px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.btn-save{background:#e91e63;color:#fff}
.btn-add{width:100%;padding:11px;background:#e91e63;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;margin-top:8px}
.flash{padding:10px 16px;border-radius:8px;margin-bottom:12px;font-size:14px}
.ok{background:#e8f5e9;color:#2e7d32}
.err{background:#fdecea;color:#c62828}
.toggle{position:relative;width:40px;height:22px}
.toggle input{opacity:0;width:0;height:0;position:absolute}
.t-slider{position:absolute;inset:0;border-radius:22px;background:#ccc;cursor:pointer;transition:.25s}
.t-slider:before{content:"";position:absolute;width:16px;height:16px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.25s}
.toggle input:checked+.t-slider{background:#e91e63}
.toggle input:checked+.t-slider:before{transform:translateX(18px)}
.row{display:flex;align-items:center;gap:8px}
</style></head>
<body>
<div class="header">
  <a class="back" href="/admin?key={{ key }}">‹</a>
  <h1>📌 FB 貼文指定回覆</h1>
</div>
<div class="container">
  {% if flash %}<div class="flash ok">{{ flash }}</div>{% endif %}

  {% for pid, cfg in posts.items() %}
  <form method="POST" action="/admin/fb-posts/save?key={{ key }}">
    <input type="hidden" name="post_id" value="{{ pid }}">
    <div class="card">
      <div class="card-head">
        <div>
          <div class="card-title">貼文 ID</div>
          <span class="badge">{{ pid }}</span>
        </div>
        <div class="row">
          <label class="toggle" style="margin:0">
            <input type="checkbox" name="enabled" {{ 'checked' if cfg.enabled }}>
            <span class="t-slider"></span>
          </label>
          <button type="button" class="del-btn" onclick="delPost('{{ pid }}')">✕ 刪除</button>
        </div>
      </div>
      <label>公開回覆內容（所有人看得到）</label>
      <textarea name="reply">{{ cfg.reply }}</textarea>
      <label>圖片網址（選填，公開回覆和私訊都會附上）</label>
      <input type="text" name="image_url" value="{{ cfg.image_url }}" placeholder="https://...">
      <div class="hint">貼文網址範例：facebook.com/逸雅傢俱/posts/<b>貼文ID</b></div>
      <button type="submit" class="btn btn-add" style="margin-top:12px">💾 儲存此貼文</button>
    </div>
  </form>
  {% else %}
  <div style="text-align:center;color:#bbb;padding:40px;font-size:14px">尚未設定任何貼文，新增後即可指定回覆</div>
  {% endfor %}

  <!-- 新增貼文 -->
  <form method="POST" action="/admin/fb-posts/add?key={{ key }}">
    <div class="add-card">
      <div class="add-title">➕ 新增貼文指定回覆</div>
      <label>貼文 ID（從貼文網址最後一串數字）</label>
      <input type="text" name="post_id" placeholder="例如：1234567890123456" required>
      <label>公開回覆內容</label>
      <textarea name="reply" placeholder="有人留言此貼文時自動回覆..." required></textarea>
      <label>圖片網址（選填）</label>
      <input type="text" name="image_url" placeholder="https://...">
      <button type="submit" class="btn-add">新增</button>
    </div>
  </form>
</div>
<form id="del-form" method="POST" action="/admin/fb-posts/del?key={{ key }}">
  <input type="hidden" name="post_id" id="del-pid">
</form>
<script>
function delPost(pid){
  if(!confirm('確定刪除此貼文設定？'))return;
  document.getElementById('del-pid').value=pid;
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
    if platform == "inbox":
        return admin_inbox()
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
        if "private_replies" in cfg and f"private_reply_{k}" in request.form:
            cfg["private_replies"][k] = request.form[f"private_reply_{k}"]
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

# ── FB 貼文指定回覆 ───────────────────────────────────────

@app.route("/admin/fb-posts")
def fb_posts_admin():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next="/admin/fb-posts", error=None)
    flash_msg = _flash.pop("msg", "")
    return render_template_string(FB_POSTS_HTML, key=key, posts=fb_post_replies, flash=flash_msg)

@app.route("/admin/fb-posts/add", methods=["POST"])
def fb_posts_add():
    ok, key = check_auth()
    if not ok:
        abort(403)
    pid = request.form.get("post_id", "").strip()
    reply = request.form.get("reply", "").strip()
    image_url = request.form.get("image_url", "").strip()
    if pid and reply:
        fb_post_replies[pid] = {"reply": reply, "image_url": image_url, "enabled": True}
        _flash["msg"] = f"✅ 已新增貼文 {pid}"
    return redirect(f"/admin/fb-posts?key={key}")

@app.route("/admin/fb-posts/save", methods=["POST"])
def fb_posts_save():
    ok, key = check_auth()
    if not ok:
        abort(403)
    pid = request.form.get("post_id", "").strip()
    if pid and pid in fb_post_replies:
        fb_post_replies[pid]["reply"] = request.form.get("reply", "")
        fb_post_replies[pid]["image_url"] = request.form.get("image_url", "")
        fb_post_replies[pid]["enabled"] = "enabled" in request.form
        _flash["msg"] = "✅ 已儲存"
    return redirect(f"/admin/fb-posts?key={key}")

@app.route("/admin/fb-posts/del", methods=["POST"])
def fb_posts_del():
    ok, key = check_auth()
    if not ok:
        abort(403)
    pid = request.form.get("post_id", "").strip()
    fb_post_replies.pop(pid, None)
    _flash["msg"] = f"✅ 已刪除"
    return redirect(f"/admin/fb-posts?key={key}")

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
    cp = platforms["fb_comment"]
    out.append("FB_COMMENT_REPLIES = {\n" + dict_to_py(cp["replies"]) + "}\n\n")
    out.append("FB_COMMENT_PRIVATE_REPLIES = {\n" + dict_to_py(cp["private_replies"]) + "}\n\n")
    out.append("FB_COMMENT_KEYWORDS = {\n" + dict_to_py(cp["keywords"]) + "}\n\n")
    out.append("FB_COMMENT_ENABLED_INTENTS = {\n" + bool_dict_to_py(cp["enabled_intents"]) + "}\n\n")
    out.append("_BASE_REPLIES = LINE_REPLIES\n")
    out.append("_BASE_KEYWORDS = LINE_KEYWORDS\n")
    return "".join(out)

# ── 收件匣 ───────────────────────────────────────────────

@app.route("/admin/inbox")
def admin_inbox():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next="/admin/inbox", error=None)
    return render_template_string(INBOX_HTML, key=key)

@app.route("/api/conversations")
def api_conversations():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    logs = list(message_log)
    convs = {}
    for l in reversed(logs):
        uid = l.get("user_id", "")
        pf = l.get("platform", "")
        if not uid or not pf or pf == "FB_COMMENT" or uid == "ADMIN":
            continue
        key = f"{pf}:{uid}"
        if key not in convs:
            profile = user_profiles.get(key, {"name": "", "avatar": ""})
            if not profile.get("name"):
                threading.Thread(target=get_user_profile, args=(pf, uid), daemon=True).start()
            convs[key] = {"platform": pf, "user_id": uid, "messages": [],
                          "last_time": l.get("time",""), "last_msg": l.get("msg",""),
                          "manual": key in manual_takeover,
                          "name": profile.get("name",""),
                          "avatar": profile.get("avatar",""),
                          "note": user_notes.get(key,""),
                          "tags": conv_tags.get(key,[]),
                          "unread": 0}
        convs[key]["messages"].append(l)
        convs[key]["last_time"] = l.get("time","")
        convs[key]["last_msg"] = l.get("msg","")
        seen_ts = last_seen.get(key, 0)
        try:
            msg_ts = time.mktime(time.strptime(l.get("time",""), "%Y/%m/%d %H:%M:%S"))
            if msg_ts > seen_ts and l.get("user_id","") != "ADMIN":
                convs[key]["unread"] += 1
        except Exception:
            pass
    result = sorted(convs.values(), key=lambda x: x["last_time"], reverse=True)
    return jsonify(result)

@app.route("/api/note", methods=["POST"])
def api_note():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    platform = data.get("platform","").upper()
    user_id = data.get("user_id","")
    note = data.get("note","")
    if not user_id:
        return jsonify({"error": "missing user_id"}), 400
    key = f"{platform}:{user_id}"
    user_notes[key] = note
    _save_notes()
    return jsonify({"ok": True})

@app.route("/api/reply", methods=["POST"])
def api_reply():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    platform = data.get("platform","").upper()
    user_id = data.get("user_id","")
    text = data.get("text","").strip()
    image_url = data.get("image_url","").strip()
    if not user_id or (not text and not image_url):
        return jsonify({"error": "missing fields"}), 400
    try:
        if platform == "LINE":
            if text: line_push(user_id, text)
            if image_url: line_push_image(user_id, image_url)
        elif platform == "FB":
            if text: fb_send(user_id, text)
            if image_url: fb_send_image(user_id, image_url)
        else:
            return jsonify({"error": "unsupported platform"}), 400
        now = time.strftime("%Y/%m/%d %H:%M:%S", time.gmtime(time.time() + 8*3600))
        if text:
            log_message({"time": now, "platform": platform, "user_id": "ADMIN",
                         "msg": f"[手動] {text}", "intent": "manual", "reply": "", "replied": True})
        if image_url:
            log_message({"time": now, "platform": platform, "user_id": "ADMIN",
                         "msg": "[手動圖片]", "intent": "manual", "reply": "", "replied": True, "image_url": image_url})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/takeover", methods=["POST"])
def api_takeover():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    platform = data.get("platform","").upper()
    user_id = data.get("user_id","")
    active = data.get("active", True)
    key = f"{platform}:{user_id}"
    if active:
        manual_takeover.add(key)
    else:
        manual_takeover.discard(key)
    return jsonify({"ok": True, "active": active})

# ── 快速回覆模板 API ──────────────────────────────────────

@app.route("/api/templates", methods=["GET"])
def api_templates_get():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(quick_reply_templates)

@app.route("/api/templates", methods=["POST"])
def api_templates_post():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    action = data.get("action", "add")
    if action == "add":
        import uuid
        tpl = {
            "id": str(uuid.uuid4())[:8],
            "title": data.get("title", "").strip(),
            "text": data.get("text", "").strip(),
            "image_url": data.get("image_url", "").strip(),
            "price": data.get("price", "").strip(),
        }
        if not tpl["title"] or not tpl["text"]:
            return jsonify({"error": "title and text required"}), 400
        quick_reply_templates.append(tpl)
        _save_templates()
        return jsonify({"ok": True, "tpl": tpl})
    elif action == "delete":
        tid = data.get("id", "")
        for i, t in enumerate(quick_reply_templates):
            if t["id"] == tid:
                quick_reply_templates.pop(i)
                _save_templates()
                return jsonify({"ok": True})
        return jsonify({"error": "not found"}), 404
    return jsonify({"error": "unknown action"}), 400

# ── 標籤 API ─────────────────────────────────────────────

@app.route("/api/tags", methods=["POST"])
def api_tags():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    platform = data.get("platform", "").upper()
    user_id = data.get("user_id", "")
    tags = data.get("tags", [])
    key = f"{platform}:{user_id}"
    conv_tags[key] = tags
    _save_tags()
    return jsonify({"ok": True})

# ── 已讀標記 API ─────────────────────────────────────────

@app.route("/api/seen", methods=["POST"])
def api_seen():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    platform = data.get("platform", "").upper()
    user_id = data.get("user_id", "")
    key = f"{platform}:{user_id}"
    last_seen[key] = time.time()
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
