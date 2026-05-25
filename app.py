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
import db as _db
from knowledge_base import (
    BRAND_INFO, LINE_ENABLED, FB_ENABLED, INTENT_LABELS,
    LINE_REPLIES, FB_REPLIES, LINE_KEYWORDS, FB_KEYWORDS,
    LINE_IMAGE_URLS, FB_IMAGE_URLS,
    LINE_ENABLED_INTENTS, FB_ENABLED_INTENTS,
    FB_COMMENT_REPLIES, FB_COMMENT_KEYWORDS, FB_COMMENT_ENABLED_INTENTS,
    FB_COMMENT_PRIVATE_REPLIES
)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

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

def _load_history_from_sheets():
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return
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
        loaded = 0
        for row in rows[-800:]:
            if len(row) < 3:
                continue
            time_str = row[0] if len(row) > 0 else ""
            if not time_str or not any(ch.isdigit() for ch in time_str):
                continue
            platform = row[1] if len(row) > 1 else ""
            user_id = row[2] if len(row) > 2 else ""
            if not user_id or not platform:
                continue
            entry = {
                "time": time_str,
                "platform": platform,
                "user_id": user_id,
                "msg": row[3] if len(row) > 3 else "",
                "intent": row[4] if len(row) > 4 else "",
                "reply": row[5] if len(row) > 5 else "",
                "replied": (row[6] if len(row) > 6 else "") == "已回覆",
            }
            message_log.appendleft(entry)
            loaded += 1
        print(f"[Sheets] loaded {loaded} messages from history")
    except Exception as e:
        print(f"[Sheets] load history error: {e}")

threading.Thread(target=_load_history_from_sheets, daemon=True).start()

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
ALL_TAGS = ["高架床","雙層床","客製需求","詢價中","高意願","已報價","待回訪","已成交","小坪數","樓梯款","爬梯款","書桌需求","收納需求"]
STATUS_OPTIONS = ["bot","human","waiting","followup","closed","sold"]

# 啟動時從 db 同步 human 狀態到 manual_takeover
def _sync_status_from_db():
    try:
        statuses = _db.get_all_statuses()
        for key, status in statuses.items():
            if status == "human":
                manual_takeover.add(key)
            else:
                manual_takeover.discard(key)
    except Exception:
        pass
threading.Thread(target=_sync_status_from_db, daemon=True).start()

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
                if not sid or not msg:
                    continue
                # 忽略 bot 自己發出的 echo
                if event.get("message", {}).get("is_echo"):
                    continue
                now = time.strftime("%Y/%m/%d %H:%M:%S", time.gmtime(time.time() + 8*3600))
                if "text" in msg:
                    text, reply_img = get_reply(msg["text"].strip(), sid, "fb")
                    if text:
                        fb_send(sid, text)
                    if reply_img:
                        fb_send_image(sid, reply_img)
                else:
                    # 圖片/貼圖/其他附件 - 只記錄不回覆
                    attachments = msg.get("attachments", [])
                    img_url = ""
                    msg_type = "[附件]"
                    if attachments:
                        att = attachments[0]
                        att_type = att.get("type", "")
                        payload = att.get("payload", {})
                        if att_type == "image":
                            img_url = payload.get("url", "")
                            msg_type = "[圖片]"
                        elif att_type == "video":
                            img_url = payload.get("url", "")
                            msg_type = "[影片]"
                        elif att_type == "audio":
                            msg_type = "[語音]"
                        elif att_type == "sticker":
                            img_url = payload.get("url", "")
                            msg_type = "[貼圖]"
                    log_message({"time": now, "platform": "FB", "user_id": sid,
                                 "msg": msg_type, "intent": "media", "reply": "", "replied": False,
                                 "image_url": img_url})
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

def fb_send_video(psid: str, video_url: str):
    if not FB_PAGE_ACCESS_TOKEN:
        return
    url = f"https://graph.facebook.com/v22.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
    payload = json.dumps({"recipient": {"id": psid}, "message": {
        "attachment": {"type": "video", "payload": {"url": video_url, "is_reusable": True}}
    }}).encode()
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

def line_push_video(user_id: str, video_url: str, preview_url: str):
    url = "https://api.line.me/v2/bot/message/push"
    payload = json.dumps({"to": user_id, "messages": [{"type": "video",
        "originalContentUrl": video_url, "previewImageUrl": preview_url}]}).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"})
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
    candidates = [
        request.args.get("admin_key", ""),
        request.args.get("key", ""),
    ]
    try:
        body = request.get_json(silent=True, force=True) or {}
        candidates += [body.get("admin_key", ""), body.get("key", "")]
    except Exception:
        pass
    for k in candidates:
        if k and k == ADMIN_PASSWORD:
            return True, k
    return False, ""

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
<title>JSIMPLE CRM</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;color:#1a1a1a;height:100vh;overflow:hidden}
.crm-wrap{display:grid;grid-template-columns:300px 1fr 320px;height:100vh}

/* LEFT SIDEBAR */
.sidebar{background:#fff;border-right:1px solid #e8eaed;display:flex;flex-direction:column;overflow:hidden}
.sidebar-top{padding:14px 12px 10px;border-bottom:1px solid #e8eaed}
.sidebar-top h2{font-size:15px;font-weight:700;color:#1a1a1a;margin-bottom:10px}
.search-box{display:flex;align-items:center;background:#f5f6f8;border-radius:8px;padding:6px 10px;gap:6px}
.search-box input{border:none;background:none;outline:none;font-size:13px;width:100%;color:#1a1a1a}
.search-box input::placeholder{color:#9aa0a6}

.status-tabs{display:flex;gap:4px;padding:8px 12px;border-bottom:1px solid #e8eaed;overflow-x:auto;flex-shrink:0}
.status-tabs::-webkit-scrollbar{height:3px}
.status-tabs::-webkit-scrollbar-thumb{background:#ddd;border-radius:2px}
.stab{padding:4px 10px;border-radius:20px;font-size:11px;font-weight:600;cursor:pointer;white-space:nowrap;border:1.5px solid transparent;color:#666;background:#f5f6f8}
.stab.active{color:#fff}
.stab[data-s="all"].active{background:#555;border-color:#555}
.stab[data-s="bot"].active{background:#6c757d;border-color:#6c757d}
.stab[data-s="human"].active{background:#0d6efd;border-color:#0d6efd}
.stab[data-s="waiting"].active{background:#fd7e14;border-color:#fd7e14}
.stab[data-s="followup"].active{background:#6f42c1;border-color:#6f42c1}
.stab[data-s="closed"].active{background:#dc3545;border-color:#dc3545}
.stab[data-s="sold"].active{background:#198754;border-color:#198754}

.tag-filter{padding:8px 12px;border-bottom:1px solid #e8eaed;display:flex;flex-wrap:wrap;gap:4px;flex-shrink:0}
.tag-chip-f{padding:2px 8px;border-radius:12px;font-size:10px;cursor:pointer;border:1.5px solid #ddd;color:#555;background:#fff;white-space:nowrap}
.tag-chip-f.active{background:#e8f4fd;border-color:#0d6efd;color:#0d6efd}

.conv-list{flex:1;overflow-y:auto}
.conv-list::-webkit-scrollbar{width:4px}
.conv-list::-webkit-scrollbar-thumb{background:#ddd;border-radius:2px}
.conv-item{padding:10px 12px;cursor:pointer;border-bottom:1px solid #f0f2f5;transition:background .15s;display:flex;gap:8px;align-items:flex-start}
.conv-item:hover{background:#f8f9fa}
.conv-item.active{background:#e8f4fd}
.conv-avatar{width:38px;height:38px;border-radius:50%;object-fit:cover;flex-shrink:0;background:#e8eaed}
.conv-avatar-placeholder{width:38px;height:38px;border-radius:50%;background:#e0e4e8;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}
.conv-info{flex:1;min-width:0}
.conv-name-row{display:flex;align-items:center;gap:4px;margin-bottom:2px}
.conv-name{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.conv-time{font-size:10px;color:#9aa0a6;white-space:nowrap}
.conv-preview{font-size:11px;color:#9aa0a6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:4px}
.conv-meta{display:flex;gap:3px;flex-wrap:wrap;align-items:center}
.status-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.s-bot{background:#6c757d}.s-human{background:#0d6efd}.s-waiting{background:#fd7e14}
.s-followup{background:#6f42c1}.s-closed{background:#dc3545}.s-sold{background:#198754}
.tag-pill{padding:1px 6px;border-radius:10px;font-size:9px;font-weight:600;background:#e8f4fd;color:#0d6efd}
.platform-badge{font-size:9px;padding:1px 5px;border-radius:4px;font-weight:700}
.pb-line{background:#06C755;color:#fff}.pb-fb{background:#1877F2;color:#fff}

/* MIDDLE CHAT */
.chat-main{display:flex;flex-direction:column;overflow:hidden;background:#f8f9fa}
.chat-header{background:#fff;border-bottom:1px solid #e8eaed;padding:10px 16px;display:flex;align-items:center;gap:10px;flex-shrink:0}
.chat-header-avatar{width:36px;height:36px;border-radius:50%;object-fit:cover;background:#e8eaed}
.chat-header-info{flex:1;min-width:0}
.chat-header-name{font-size:14px;font-weight:700}
.chat-header-sub{font-size:11px;color:#9aa0a6}
.status-select{padding:4px 8px;border:1.5px solid #ddd;border-radius:20px;font-size:11px;font-weight:600;cursor:pointer;outline:none;color:#555;background:#fff}
.takeover-btn{padding:5px 12px;border-radius:20px;font-size:11px;font-weight:700;border:none;cursor:pointer}
.takeover-on{background:#0d6efd;color:#fff}.takeover-off{background:#e8eaed;color:#555}

.msg-area{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.msg-area::-webkit-scrollbar{width:4px}
.msg-area::-webkit-scrollbar-thumb{background:#ddd;border-radius:2px}
.msg-row{display:flex;gap:8px;align-items:flex-end}
.msg-row.me{flex-direction:row-reverse}
.msg-bubble{max-width:72%;padding:8px 12px;border-radius:16px;font-size:13px;line-height:1.5;word-break:break-word}
.msg-row.them .msg-bubble{background:#fff;border-bottom-left-radius:4px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
.msg-row.me .msg-bubble{background:#0d6efd;color:#fff;border-bottom-right-radius:4px}
.msg-img{max-width:220px;border-radius:12px;cursor:pointer}
.msg-sticker{width:100px}
.msg-time{font-size:10px;color:#9aa0a6;white-space:nowrap}
.msg-avatar{width:28px;height:28px;border-radius:50%;object-fit:cover;background:#e8eaed;flex-shrink:0}
.sys-msg{text-align:center;font-size:11px;color:#9aa0a6;padding:4px 0}

.tpl-panel{background:#fff;border-top:1px solid #e8eaed;display:none;flex-direction:column;max-height:240px}
.tpl-panel.open{display:flex}
.tpl-cats{display:flex;gap:4px;padding:8px 12px;overflow-x:auto;border-bottom:1px solid #f0f2f5;flex-shrink:0}
.tpl-cats::-webkit-scrollbar{height:3px}
.tpl-cats::-webkit-scrollbar-thumb{background:#ddd}
.tcat{padding:3px 10px;border-radius:12px;font-size:11px;cursor:pointer;border:1.5px solid #ddd;color:#555;white-space:nowrap}
.tcat.active{background:#0d6efd;border-color:#0d6efd;color:#fff}
.tpl-list{overflow-y:auto;flex:1}
.tpl-item{padding:8px 14px;cursor:pointer;border-bottom:1px solid #f5f6f8;font-size:12px;line-height:1.5;color:#333}
.tpl-item:hover{background:#f0f8ff}
.tpl-item strong{display:block;font-size:10px;color:#9aa0a6;margin-bottom:2px}

.input-area{background:#fff;border-top:1px solid #e8eaed;padding:10px 12px;display:flex;gap:8px;align-items:flex-end;flex-shrink:0}
.input-area textarea{flex:1;border:1.5px solid #e8eaed;border-radius:12px;padding:8px 12px;font-size:13px;resize:none;outline:none;font-family:inherit;line-height:1.5;max-height:120px}
.input-area textarea:focus{border-color:#0d6efd}
.btn-icon{width:36px;height:36px;border-radius:50%;border:1.5px solid #e8eaed;background:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;transition:.15s}
.btn-icon:hover{background:#f0f8ff;border-color:#0d6efd}
.btn-send{background:#0d6efd;border-color:#0d6efd;color:#fff}
.btn-send:hover{background:#0b5ed7;border-color:#0b5ed7}

/* RIGHT PANEL */
.right-panel{background:#fff;border-left:1px solid #e8eaed;display:flex;flex-direction:column;overflow-y:auto}
.right-panel::-webkit-scrollbar{width:4px}
.right-panel::-webkit-scrollbar-thumb{background:#ddd;border-radius:2px}
.rp-section{padding:14px 16px;border-bottom:1px solid #f0f2f5}
.rp-section h4{font-size:12px;font-weight:700;color:#9aa0a6;text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px}
.rp-name{font-size:16px;font-weight:700;margin-bottom:2px}
.rp-sub{font-size:11px;color:#9aa0a6}
.rp-last{font-size:11px;color:#555;margin-top:6px;padding:6px 8px;background:#f8f9fa;border-radius:6px}

.tags-wrap{display:flex;flex-wrap:wrap;gap:5px}
.tag-chip-r{padding:3px 10px;border-radius:12px;font-size:11px;cursor:pointer;border:1.5px solid #ddd;color:#555;background:#fff;transition:.15s}
.tag-chip-r.on{background:#e8f4fd;border-color:#0d6efd;color:#0d6efd;font-weight:600}

.note-area{width:100%;border:1.5px solid #e8eaed;border-radius:8px;padding:8px;font-size:12px;resize:vertical;min-height:80px;outline:none;font-family:inherit;color:#333}
.note-area:focus{border-color:#0d6efd}
.note-saved{font-size:10px;color:#198754;margin-top:4px;display:none}

.needs-form{display:flex;flex-direction:column;gap:8px}
.needs-row{display:flex;flex-direction:column;gap:3px}
.needs-row label{font-size:11px;color:#9aa0a6;font-weight:600}
.needs-row input,.needs-row select{border:1.5px solid #e8eaed;border-radius:6px;padding:5px 8px;font-size:12px;outline:none;width:100%;color:#333}
.needs-row input:focus,.needs-row select:focus{border-color:#0d6efd}
.needs-row .radio-row{display:flex;gap:12px}
.needs-row .radio-row label{font-size:12px;color:#333;font-weight:400;display:flex;align-items:center;gap:4px;cursor:pointer}
.save-btn{padding:6px 0;background:#0d6efd;color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;width:100%;margin-top:2px}
.save-btn:hover{background:#0b5ed7}

.ai-box{background:#f8f9fa;border-radius:8px;padding:10px;font-size:12px;color:#333;line-height:1.6;min-height:60px}
.ai-btn{margin-top:8px;padding:5px 0;background:#6f42c1;color:#fff;border:none;border-radius:8px;font-size:11px;font-weight:700;cursor:pointer;width:100%}
.ai-btn:hover{background:#5a2fa0}

.no-conv{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;color:#9aa0a6;gap:8px}
.no-conv .icon{font-size:48px}

/* TOAST */
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1a1a1a;color:#fff;padding:8px 20px;border-radius:20px;font-size:13px;z-index:9999;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}
</style></head>
<body>
<div class="crm-wrap">
<!-- LEFT SIDEBAR -->
<div class="sidebar">
  <div class="sidebar-top">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <h2 style="margin-bottom:0">&#128172; JSIMPLE CRM</h2>
      <a id="homeBtn" href="#" style="font-size:11px;color:#0d6efd;text-decoration:none;padding:3px 8px;border:1px solid #0d6efd;border-radius:12px">&#127968; 首頁</a>
    </div>
    <div class="search-box">
      <span>&#128269;</span>
      <input type="text" id="searchInput" placeholder="搜尋對話...">
    </div>
  </div>
  <div class="status-tabs" id="statusTabs">
    <div class="stab active" data-s="all">全部</div>
    <div class="stab" data-s="bot">Bot</div>
    <div class="stab" data-s="human">人工</div>
    <div class="stab" data-s="waiting">等待</div>
    <div class="stab" data-s="followup">追蹤</div>
    <div class="stab" data-s="closed">結案</div>
    <div class="stab" data-s="sold">成交</div>
  </div>
  <div class="tag-filter" id="tagFilter"></div>
  <div class="conv-list" id="convList"></div>
</div>

<!-- MIDDLE CHAT -->
<div class="chat-main">
  <div class="chat-header" id="chatHeader" style="display:none">
    <div class="chat-header-avatar" id="hdrAvatar"></div>
    <div class="chat-header-info">
      <div class="chat-header-name" id="hdrName"></div>
      <div class="chat-header-sub" id="hdrSub"></div>
    </div>
    <select class="status-select" id="statusSelect" onchange="updateStatus(this.value)">
      <option value="bot">&#129302; Bot</option>
      <option value="human">&#128100; 人工</option>
      <option value="waiting">&#9203; 等待</option>
      <option value="followup">&#128276; 追蹤</option>
      <option value="closed">&#10060; 結案</option>
      <option value="sold">&#127881; 成交</option>
    </select>
    <button class="takeover-btn takeover-off" id="takeoverBtn" onclick="toggleTakeover()">接管</button>
  </div>
  <div class="msg-area" id="msgArea">
    <div class="no-conv">
      <div class="icon">&#128172;</div>
      <div>選擇一個對話開始</div>
    </div>
  </div>
  <div class="tpl-panel" id="tplPanel">
    <div class="tpl-cats" id="tplCats"></div>
    <div class="tpl-list" id="tplList"></div>
  </div>
  <div class="input-area">
    <button class="btn-icon" onclick="toggleTpl()" title="快速回覆">&#9889;</button>
    <label class="btn-icon" title="傳送圖片/影片" style="cursor:pointer">
      &#128247;
      <input type="file" id="imgInput" accept="image/*,video/*" style="display:none" onchange="uploadImg(this)">
    </label>
    <textarea id="replyInput" placeholder="輸入訊息..." rows="1"
      oninput="autoResize(this)"
      onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendReply()}"></textarea>
    <button class="btn-icon btn-send" onclick="sendReply()" title="送出">&#10148;</button>
  </div>
</div>

<!-- RIGHT PANEL -->
<div class="right-panel" id="rightPanel">
  <div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;color:#9aa0a6;padding:24px;text-align:center">
    <div style="font-size:40px;margin-bottom:8px">&#128100;</div>
    <div style="font-size:13px">選擇對話後顯示客戶資訊</div>
  </div>
</div>
</div>

<div class="toast" id="toast"></div>

<script>
const KEY = new URLSearchParams(location.search).get('key') || '';
document.getElementById('homeBtn').href = '/admin?key=' + KEY;
const ALL_TAGS = ['有興趣','已報價','猶豫中','要比較','問尺寸','問材質','問交期','問安裝','詢問保固','重要客戶','需要跟進','已下訂','垃圾訊息'];
const STATUS_COLORS = {bot:'#6c757d',human:'#0d6efd',waiting:'#fd7e14',followup:'#6f42c1',closed:'#dc3545',sold:'#198754'};
const TPL_DATA = {
  '打招呼':['你好，我是JSIMPLE高架床專員，請問有什麼可以幫您？','感謝您的詢問，請問您的需求是？'],
  '報價':['我們的高架床系列售價從NT$8,800起，依尺寸和材質不同，我幫您報正確的價格。','請問需要的尺寸是單人(90cm)、標準(120cm)還是雙人(150cm)？'],
  '尺寸':['標準房間建議90x190或120x190，需要我幫您確認空間適合哪種嗎？','請問您的房間長寬大約是多少？方便我幫您規劃最合適的床架配置。'],
  '材質':['我們提供實木、鋼管、系統板三種材質，各有不同優缺點，您比較在意哪個方向？','鋼管款式耐重又好清潔，實木款則更有質感，預算和使用習慣都可以協助建議。'],
  '交期':['現貨商品約3-5個工作天可出貨，訂製款需要10-15天。','請問您方便收貨的時間？我們可以安排合適的配送日期。'],
  '安裝':['我們有提供到府安裝服務，費用NT$800，台北、新北、桃園可預約。','安裝通常需要1-2小時，請確保有成人在家接待師傅。'],
  '跟進':['您好，上次有詢問高架床，請問有決定了嗎？有任何問題都可以告訴我。','您好，想再確認一下，我們的限時優惠還有兩天，需要幫您保留嗎？'],
  '成交':['感謝您的訂購，我馬上幫您安排出貨，請確認收件地址是否正確。','訂單已確認，預計X月X日出貨，有任何問題請隨時告訴我。']
};

let allConvs = [], curKey = null, curStatus = 'bot', filterStatus = 'all', filterTag = null, searchQ = '';
let curTags = [], curCustomer = {}, noteTimer = null;

// INIT
async function init(){
  buildTagFilter();
  buildTplCats();
  await loadConvs();
  setInterval(loadConvs, 8000);
}

function buildTagFilter(){
  const el = document.getElementById('tagFilter');
  el.innerHTML = ALL_TAGS.map(t=>
    `<div class="tag-chip-f" data-tag="${t}" onclick="filterByTag('${t}')">${t}</div>`
  ).join('');
}

function filterByTag(tag){
  filterTag = filterTag === tag ? null : tag;
  document.querySelectorAll('.tag-chip-f').forEach(el=>{
    el.classList.toggle('active', el.dataset.tag === filterTag);
  });
  renderList();
}

document.querySelectorAll('.stab').forEach(el=>{
  el.addEventListener('click',()=>{
    filterStatus = el.dataset.s;
    document.querySelectorAll('.stab').forEach(e=>e.classList.remove('active'));
    el.classList.add('active');
    renderList();
  });
});

document.getElementById('searchInput').addEventListener('input', e=>{
  searchQ = e.target.value.trim().toLowerCase();
  renderList();
});

async function loadConvs(){
  try{
    const r = await fetch(`/api/conversations?key=${KEY}`);
    const d = await r.json();
    if(Array.isArray(d)) allConvs = d;
    else if(d.conversations) allConvs = d.conversations;
    renderList();
    if(curKey) {
      const cur = allConvs.find(c=>c.key===curKey);
      if(cur) updateHeaderStatus(cur.status||'bot');
    }
  }catch(e){console.error(e)}
}

function renderList(){
  let list = allConvs.filter(c=>{
    if(filterStatus !== 'all' && (c.status||'bot') !== filterStatus) return false;
    if(filterTag && !(c.tags||[]).includes(filterTag)) return false;
    if(searchQ){
      const name = (c.user_name||c.user_id||'').toLowerCase();
      const last = (c.last_message||'').toLowerCase();
      if(!name.includes(searchQ) && !last.includes(searchQ)) return false;
    }
    return true;
  });
  const el = document.getElementById('convList');
  if(!list.length){el.innerHTML='<div style="padding:20px;text-align:center;color:#9aa0a6;font-size:13px">沒有符合的對話</div>';return}
  el.innerHTML = list.map(c=>{
    const s = c.status||'bot';
    const tags = (c.tags||[]).slice(0,3).map(t=>`<span class="tag-pill">${t}</span>`).join('');
    const pb = c.platform==='fb'?'<span class="platform-badge pb-fb">FB</span>':'<span class="platform-badge pb-line">LINE</span>';
    const time = c.last_time ? new Date(c.last_time*1000).toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit'}) : '';
    const avatarEl = c.user_avatar
      ? `<img class="conv-avatar" src="${c.user_avatar}" onerror="this.style.display='none'">`
      : `<div class="conv-avatar-placeholder">${c.platform==='fb'?'&#128100;':'&#128100;'}</div>`;
    return `<div class="conv-item${c.key===curKey?' active':''}" onclick="openConv('${c.key}')">
      ${avatarEl}
      <div class="conv-info">
        <div class="conv-name-row">
          <span class="conv-name">${escHtml(c.user_name||c.user_id||'未知用戶')}</span>
          <span class="conv-time">${time}</span>
        </div>
        <div class="conv-preview">${escHtml((c.last_message||'').slice(0,40))}</div>
        <div class="conv-meta">
          <div class="status-dot s-${s}"></div>
          ${pb}${tags}
        </div>
      </div>
    </div>`;
  }).join('');
}

async function openConv(key){
  curKey = key;
  document.querySelectorAll('.conv-item').forEach(el=>el.classList.remove('active'));
  document.querySelector(`.conv-item[onclick="openConv('${key}')"]`)?.classList.add('active');

  const conv = allConvs.find(c=>c.key===key);
  curStatus = conv?.status || 'bot';

  // show header
  const hdr = document.getElementById('chatHeader');
  hdr.style.display = 'flex';
  const name = conv?.user_name || conv?.user_id || '未知用戶';
  document.getElementById('hdrName').textContent = name;
  document.getElementById('hdrSub').textContent = conv?.platform==='fb'?'Facebook Messenger':'LINE';
  const hdrAv = document.getElementById('hdrAvatar');
  if(conv?.user_avatar){
    hdrAv.outerHTML = `<img class="chat-header-avatar" id="hdrAvatar" src="${conv.user_avatar}">`;
  }
  updateHeaderStatus(curStatus);

  // load messages
  await loadMsgs(key);
  // load right panel
  await loadInfoPanel(key, conv);
}

async function loadMsgs(key){
  try{
    const r = await fetch(`/api/messages?key=${key}&admin_key=${KEY}`);
    const d = await r.json();
    renderMsgs(d.messages||[]);
  }catch(e){console.error(e)}
}

function renderMsgs(msgs){
  const area = document.getElementById('msgArea');
  if(!msgs.length){area.innerHTML='<div class="sys-msg">沒有訊息記錄</div>';return}
  area.innerHTML = msgs.map(m=>{
    const isMe = m.role==='admin';
    const time = m.ts ? new Date(m.ts*1000).toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit'}) : '';
    let content = '';
    if(m.image_url){
      const isVid=/\.(mp4|mov|avi|webm|m4v)(\?|$)/i.test(m.image_url);
      if(isVid) content=`<video src="${m.image_url}" controls style="max-width:220px;border-radius:12px;display:block"></video>`;
      else content=`<img class="msg-img" src="${m.image_url}" onclick="window.open(this.src)">`;
    } else if(m.sticker_url) content = `<img class="msg-sticker" src="${m.sticker_url}">`;
    else content = escHtml(m.content||'');
    return `<div class="msg-row ${isMe?'me':'them'}">
      <div class="msg-bubble">${content}</div>
      <span class="msg-time">${time}</span>
    </div>`;
  }).join('');
  area.scrollTop = area.scrollHeight;
}

function updateHeaderStatus(s){
  curStatus = s;
  const sel = document.getElementById('statusSelect');
  if(sel) sel.value = s;
  const btn = document.getElementById('takeoverBtn');
  if(btn){
    if(s==='human'){btn.textContent='接管中';btn.className='takeover-btn takeover-on';}
    else{btn.textContent='接管';btn.className='takeover-btn takeover-off';}
  }
}

async function updateStatus(s){
  if(!curKey) return;
  try{
    await fetch('/api/status',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:curKey,admin_key:KEY,status:s})});
    curStatus = s;
    updateHeaderStatus(s);
    // update in list
    const c = allConvs.find(x=>x.key===curKey);
    if(c) c.status = s;
    renderList();
    toast('狀態已更新');
  }catch(e){toast('更新失敗')}
}

async function toggleTakeover(){
  const newS = curStatus==='human' ? 'bot' : 'human';
  await updateStatus(newS);
}

async function sendReply(){
  if(!curKey) return;
  const inp = document.getElementById('replyInput');
  const txt = inp.value.trim();
  if(!txt) return;
  inp.value = '';
  inp.style.height = '';
  try{
    const r = await fetch('/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:curKey,admin_key:KEY,message:txt})});
    const d = await r.json();
    if(d.ok) await loadMsgs(curKey);
    else toast('發送失敗：'+(d.error||''));
  }catch(e){toast('發送失敗')}
}

async function generateVideoThumb(file){
  return new Promise(res=>{
    const vid=document.createElement('video');
    const burl=URL.createObjectURL(file);
    vid.preload='metadata'; vid.src=burl; vid.muted=true;
    vid.onloadeddata=()=>{ vid.currentTime=0.5; };
    vid.onseeked=()=>{
      const c=document.createElement('canvas');
      c.width=vid.videoWidth||640; c.height=vid.videoHeight||360;
      c.getContext('2d').drawImage(vid,0,0);
      c.toBlob(b=>{
        if(!b){ URL.revokeObjectURL(burl); res(null); return; }
        const r=new FileReader();
        r.onload=e=>{ URL.revokeObjectURL(burl); res(e.target.result.split(',')[1]); };
        r.readAsDataURL(b);
      },'image/jpeg',0.8);
    };
    vid.onerror=()=>{ URL.revokeObjectURL(burl); res(null); };
  });
}

async function uploadImg(input){
  if(!input.files[0]||!curKey) return;
  const file=input.files[0];
  const isVideo=file.type.startsWith('video/');
  toast(isVideo?'影片上傳中...':'圖片上傳中...');
  const reader=new FileReader();
  reader.onload=async e=>{
    const b64=e.target.result.split(',')[1];
    try{
      const up=await fetch('/api/upload_image',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:curKey,admin_key:KEY,filename:file.name,content:b64})});
      const ud=await up.json();
      if(!ud.url){ toast('上傳失敗：'+(ud.error||'未知錯誤')); return; }
      if(isVideo){
        let previewUrl=ud.url;
        const thumb=await generateVideoThumb(file);
        if(thumb){
          const tname='thumb_'+file.name.replace(/\.[^.]+$/,'.jpg');
          const tu=await fetch('/api/upload_image',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({key:curKey,admin_key:KEY,filename:tname,content:thumb})});
          const td=await tu.json();
          if(td.url) previewUrl=td.url;
        }
        await fetch('/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({key:curKey,admin_key:KEY,video_url:ud.url,preview_url:previewUrl})});
      }else{
        await fetch('/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({key:curKey,admin_key:KEY,image_url:ud.url})});
      }
      await loadMsgs(curKey);
    }catch(e){ toast('上傳失敗：'+e.message); }
  };
  reader.readAsDataURL(file);
  input.value='';
}

// TEMPLATES
function buildTplCats(){
  const cats = Object.keys(TPL_DATA);
  document.getElementById('tplCats').innerHTML = cats.map((c,i)=>
    `<div class="tcat${i===0?' active':''}" onclick="selectCat('${c}',this)">${c}</div>`
  ).join('');
  showTplCat(cats[0]);
}

function selectCat(cat, el){
  document.querySelectorAll('.tcat').forEach(e=>e.classList.remove('active'));
  el.classList.add('active');
  showTplCat(cat);
}

function showTplCat(cat){
  const items = TPL_DATA[cat]||[];
  document.getElementById('tplList').innerHTML = items.map(t=>
    `<div class="tpl-item" onclick="useTpl(this.dataset.t)" data-t="${escAttr(t)}"><strong>${cat}</strong>${escHtml(t)}</div>`
  ).join('');
}

function toggleTpl(){
  const p = document.getElementById('tplPanel');
  p.classList.toggle('open');
}

function useTpl(text){
  const inp = document.getElementById('replyInput');
  inp.value = text;
  autoResize(inp);
  document.getElementById('tplPanel').classList.remove('open');
  inp.focus();
}

function autoResize(el){
  el.style.height = '';
  el.style.height = Math.min(el.scrollHeight, 120)+'px';
}

// RIGHT PANEL
async function loadInfoPanel(key, conv){
  const panel = document.getElementById('rightPanel');
  const name = conv?.user_name || conv?.user_id || '未知用戶';
  const platform = conv?.platform==='fb'?'Facebook':'LINE';
  const lastTime = conv?.last_time ? new Date(conv.last_time*1000).toLocaleDateString('zh-TW') : '—';
  const status = conv?.status || 'bot';

  // fetch customer data
  let cust = {};
  try{
    const r = await fetch(`/api/customer?key=${key}&admin_key=${KEY}`);
    cust = await r.json();
  }catch(e){}
  curCustomer = cust;
  curTags = conv?.tags || [];

  panel.innerHTML = `
    <div class="rp-section">
      <h4>客戶資訊</h4>
      <div class="rp-name">${escHtml(name)}</div>
      <div class="rp-sub">${platform}</div>
      <div class="rp-last">最後訊息：${lastTime}</div>
    </div>
    <div class="rp-section">
      <h4>標籤</h4>
      <div class="tags-wrap" id="tagsWrap">
        ${ALL_TAGS.map(t=>`<div class="tag-chip-r${curTags.includes(t)?' on':''}" onclick="toggleTag('${t}',this)">${t}</div>`).join('')}
      </div>
    </div>
    <div class="rp-section">
      <h4>備註</h4>
      <textarea class="note-area" id="noteArea" placeholder="輸入備註...">${escHtml(cust.note||'')}</textarea>
      <div class="note-saved" id="noteSaved">已儲存</div>
    </div>
    <div class="rp-section">
      <h4>需求資訊</h4>
      <div class="needs-form">
        <div class="needs-row">
          <label>房間尺寸</label>
          <input type="text" id="nRoomSize" placeholder="例：10坪" value="${escAttr(cust.room_size||'')}">
        </div>
        <div class="needs-row">
          <label>床型</label>
          <select id="nBedType">
            <option value="">請選擇</option>
            <option value="single"${cust.bed_type==='single'?' selected':''}>單人 90cm</option>
            <option value="standard"${cust.bed_type==='standard'?' selected':''}>標準 120cm</option>
            <option value="double"${cust.bed_type==='double'?' selected':''}>雙人 150cm</option>
          </select>
        </div>
        <div class="needs-row">
          <label>訂製</label>
          <div class="radio-row">
            <label><input type="radio" name="isCustom" value="1"${cust.is_custom?'checked':''}> 是</label>
            <label><input type="radio" name="isCustom" value="0"${!cust.is_custom?'checked':''}> 否</label>
          </div>
        </div>
        <div class="needs-row">
          <label>預算</label>
          <input type="text" id="nBudget" placeholder="例：15000" value="${escAttr(cust.budget||'')}">
        </div>
        <div class="needs-row">
          <label>下次跟進</label>
          <input type="date" id="nFollowup" value="${escAttr(cust.next_followup||'')}">
        </div>
        <button class="save-btn" onclick="saveCustomer()">儲存客戶資料</button>
      </div>
    </div>
    <div class="rp-section">
      <h4>AI 摘要</h4>
      <div class="ai-box" id="aiBox">點擊下方按鈕分析對話</div>
      <button class="ai-btn" onclick="runAI()">&#129302; 分析對話</button>
    </div>
  `;

  // note auto-save
  document.getElementById('noteArea').addEventListener('input', ()=>{
    clearTimeout(noteTimer);
    noteTimer = setTimeout(()=>saveNote(), 1500);
  });
}

async function toggleTag(tag, el){
  if(!curKey) return;
  el.classList.toggle('on');
  if(curTags.includes(tag)) curTags = curTags.filter(t=>t!==tag);
  else curTags.push(tag);
  try{
    await fetch('/api/tag',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:curKey,admin_key:KEY,tags:curTags})});
    const c = allConvs.find(x=>x.key===curKey);
    if(c) c.tags = [...curTags];
    renderList();
  }catch(e){}
}

async function saveNote(){
  if(!curKey) return;
  const note = document.getElementById('noteArea')?.value || '';
  curCustomer.note = note;
  try{
    await fetch('/api/customer',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:curKey,admin_key:KEY,...curCustomer,note})});
    const el = document.getElementById('noteSaved');
    if(el){el.style.display='block';setTimeout(()=>el.style.display='none',2000)}
  }catch(e){}
}

async function saveCustomer(){
  if(!curKey) return;
  const data = {
    room_size: document.getElementById('nRoomSize')?.value||'',
    bed_type: document.getElementById('nBedType')?.value||'',
    is_custom: document.querySelector('input[name="isCustom"]:checked')?.value==='1',
    budget: document.getElementById('nBudget')?.value||'',
    next_followup: document.getElementById('nFollowup')?.value||'',
    note: document.getElementById('noteArea')?.value||''
  };
  curCustomer = {...curCustomer,...data};
  try{
    await fetch('/api/customer',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:curKey,admin_key:KEY,...curCustomer})});
    toast('客戶資料已儲存');
  }catch(e){toast('儲存失敗')}
}

async function runAI(){
  if(!curKey) return;
  const box = document.getElementById('aiBox');
  box.textContent = '分析中...';
  try{
    const r = await fetch(`/api/messages?key=${curKey}&admin_key=${KEY}`);
    const d = await r.json();
    const msgs = (d.messages||[]).slice(-20);
    const keywords = [];
    const text = msgs.map(m=>m.content||'').join(' ').toLowerCase();
    if(text.includes('尺寸')||text.includes('cm')) keywords.push('詢問尺寸');
    if(text.includes('價格')||text.includes('多少錢')||text.includes('報價')) keywords.push('詢問價格');
    if(text.includes('材質')||text.includes('木')||text.includes('鋼')) keywords.push('詢問材質');
    if(text.includes('安裝')||text.includes('組裝')) keywords.push('詢問安裝');
    if(text.includes('訂')||text.includes('購買')||text.includes('下訂')) keywords.push('有購買意向');
    if(text.includes('比較')||text.includes('其他家')) keywords.push('正在比較');
    if(text.includes('考慮')||text.includes('想想')) keywords.push('猶豫中');
    const summary = keywords.length
      ? `關鍵意圖：${keywords.join('、')}
對話共 ${msgs.length} 則，客戶${keywords.includes('有購買意向')?'購買意向明確':'尚在評估中'}。`
      : `對話共 ${msgs.length} 則，尚未偵測到明確購買信號。`;
    box.textContent = summary;
  }catch(e){box.textContent='分析失敗，請稍後再試'}
}

function toast(msg){
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'),2500);
}

function escHtml(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escAttr(s){
  return String(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

init();
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

@app.route("/api/status", methods=["POST"])
def api_status():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    key = data.get("key","")
    if not key:
        pf = data.get("platform","").upper()
        uid = data.get("user_id","")
        key = f"{pf}:{uid}" if uid else ""
    status = data.get("status","bot")
    if not key or status not in STATUS_OPTIONS:
        return jsonify({"error": "invalid"}), 400
    _db.set_status(key, status)
    if status == "human":
        manual_takeover.add(key)
    else:
        manual_takeover.discard(key)
    return jsonify({"ok": True, "status": status})

@app.route("/api/customer", methods=["GET","POST"])
def api_customer():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    key = request.args.get("key","") or (request.get_json(silent=True) or {}).get("key","")
    if not key:
        pf = request.args.get("platform","").upper()
        uid = request.args.get("user_id","")
        key = f"{pf}:{uid}" if uid else ""
    if not key:
        return jsonify({"error": "missing key"}), 400
    if request.method == "GET":
        return jsonify(_db.get_customer(key))
    data = request.get_json(silent=True) or {}
    parts = key.split(":",1)
    pf = parts[0] if len(parts)>1 else ""
    uid = parts[1] if len(parts)>1 else key
    _db.save_customer(key, pf, uid, data)
    return jsonify({"ok": True})

@app.route("/api/tag", methods=["POST"])
def api_tag():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    key = data.get("key","")
    if not key:
        pf = data.get("platform","").upper()
        uid = data.get("user_id","")
        key = f"{pf}:{uid}" if uid else ""
    tags = data.get("tags", [])
    if not key:
        return jsonify({"error": "missing key"}), 400
    _db.save_tags(key, tags)
    conv_tags[key] = tags
    return jsonify({"ok": True})

@app.route("/api/templates", methods=["GET"])
def api_templates():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(list(quick_reply_templates))

@app.route("/admin/inbox")
def admin_inbox():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next="/admin/inbox", error=None)
    return render_template_string(INBOX_HTML, key=key)

@app.route("/api/messages")
def api_messages():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    key = request.args.get("key", "")
    if not key:
        return jsonify({"messages": []})
    parts = key.split(":", 1)
    pf = parts[0] if len(parts) > 1 else ""
    uid = parts[1] if len(parts) > 1 else key
    msgs = []
    for l in reversed(list(message_log)):
        if l.get("platform", "") != pf or l.get("user_id", "") != uid:
            continue
        ts = 0
        try:
            ts = int(time.mktime(time.strptime(l.get("time", ""), "%Y/%m/%d %H:%M:%S")))
        except Exception:
            pass
        if l.get("sent_by") == "admin":
            content = l.get("reply", "")
            img = l.get("image_url", "")
            if content or img:
                msgs.append({"role": "admin", "content": content, "ts": ts, "image_url": img})
        else:
            if l.get("msg"):
                msgs.append({"role": "user", "content": l["msg"], "ts": ts,
                             "image_url": l.get("image_url", ""), "sticker_url": l.get("sticker_url", "")})
            if l.get("reply") and l.get("replied"):
                msgs.append({"role": "admin", "content": l["reply"], "ts": ts + 1})
    return jsonify({"messages": msgs})

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
            convs[key] = {"key": key, "platform": pf, "user_id": uid, "messages": [],
                          "last_time": l.get("time",""), "last_msg": l.get("msg",""),
                          "last_message": l.get("msg",""),
                          "manual": key in manual_takeover,
                          "status": _db.get_status(key),
                          "name": profile.get("name",""),
                          "user_name": profile.get("name","") or uid,
                          "avatar": profile.get("avatar",""),
                          "user_avatar": profile.get("avatar",""),
                          "note": user_notes.get(key,""),
                          "tags": _db.get_tags(key),
                          "unread": 0}
        convs[key]["messages"].append(l)
        convs[key]["last_time"] = l.get("time","")
        convs[key]["last_msg"] = l.get("msg","")
        convs[key]["last_message"] = l.get("msg","")
        seen_ts = last_seen.get(key, 0)
        try:
            msg_ts = time.mktime(time.strptime(l.get("time",""), "%Y/%m/%d %H:%M:%S"))
            if msg_ts > seen_ts and l.get("user_id","") != "ADMIN":
                convs[key]["unread"] += 1
        except Exception:
            pass
    for v in convs.values():
        try:
            ts = time.mktime(time.strptime(v.get("last_time",""), "%Y/%m/%d %H:%M:%S"))
            v["last_time"] = int(ts)
        except Exception:
            v["last_time"] = 0
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
    # support {key: "LINE:Uxxxxxx"} or {platform, user_id}
    key = data.get("key", "")
    if key and ":" in key:
        parts = key.split(":", 1)
        platform = parts[0].upper()
        user_id = parts[1]
    else:
        platform = data.get("platform", "").upper()
        user_id = data.get("user_id", "")
    # support both 'message' and 'text' fields
    text = (data.get("message", "") or data.get("text", "")).strip()
    image_url = data.get("image_url", "").strip()
    video_url = data.get("video_url", "").strip()
    preview_url = data.get("preview_url", "").strip()
    if not user_id or (not text and not image_url and not video_url):
        return jsonify({"error": "missing fields"}), 400
    try:
        if platform == "LINE":
            if text: line_push(user_id, text)
            if image_url: line_push_image(user_id, image_url)
            if video_url: line_push_video(user_id, video_url, preview_url or video_url)
        elif platform == "FB":
            if text: fb_send(user_id, text)
            if image_url: fb_send_image(user_id, image_url)
            if video_url: fb_send_video(user_id, video_url)
        else:
            return jsonify({"error": "unsupported platform"}), 400
        now = time.strftime("%Y/%m/%d %H:%M:%S", time.gmtime(time.time() + 8*3600))
        if text:
            log_message({"time": now, "platform": platform, "user_id": user_id,
                         "msg": "", "intent": "manual", "reply": text, "replied": True, "sent_by": "admin"})
        if image_url:
            log_message({"time": now, "platform": platform, "user_id": user_id,
                         "msg": "", "intent": "manual", "reply": "", "replied": True,
                         "image_url": image_url, "sent_by": "admin"})
        if video_url:
            log_message({"time": now, "platform": platform, "user_id": user_id,
                         "msg": "", "intent": "manual", "reply": "", "replied": True,
                         "image_url": video_url, "sent_by": "admin"})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/upload_image", methods=["POST"])
def api_upload_image():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    if not GITHUB_TOKEN:
        return jsonify({"error": "GITHUB_TOKEN 未設定"}), 500
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "upload.jpg")
    content_b64 = data.get("content", "")
    if not content_b64:
        return jsonify({"error": "no content"}), 400
    import re as _re
    safe = _re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    fname = f"{int(time.time())}_{safe}"
    try:
        raw = base64.b64decode(content_b64)
    except Exception:
        return jsonify({"error": "invalid base64"}), 400
    url = upload_image_to_github(fname, raw)
    if not url:
        return jsonify({"error": "上傳失敗"}), 500
    return jsonify({"url": url})

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

@app.route("/api/fb-diag")
def api_fb_diag():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    result = {"token_set": bool(FB_PAGE_ACCESS_TOKEN), "webhook_url": "/fb-webhook"}
    # 測試 token 是否有效
    try:
        url = f"https://graph.facebook.com/v22.0/me?access_token={FB_PAGE_ACCESS_TOKEN}"
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.loads(r.read())
            result["page_name"] = d.get("name", "")
            result["page_id"] = d.get("id", "")
            result["token_ok"] = True
    except Exception as e:
        result["token_ok"] = False
        result["token_error"] = str(e)
    # 最近 FB 訊息數
    fb_msgs = [l for l in message_log if l.get("platform") == "FB"]
    result["fb_messages_in_log"] = len(fb_msgs)
    result["latest_fb"] = fb_msgs[0] if fb_msgs else None
    return jsonify(result)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
