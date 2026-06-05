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
DATABASE_URL = os.getenv("DATABASE_URL", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://lrslleetqyaerstrlbap.supabase.co")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_BUCKET = "chat-images"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

_db_lock = threading.Lock()

def _pg_conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)

def _init_messages_db():
    if not DATABASE_URL:
        return
    try:
        with _db_lock:
            conn = _pg_conn()
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id          SERIAL PRIMARY KEY,
                    time        TEXT,
                    platform    TEXT,
                    user_id     TEXT,
                    msg         TEXT,
                    intent      TEXT DEFAULT '',
                    reply       TEXT DEFAULT '',
                    replied     BOOLEAN DEFAULT FALSE,
                    image_url   TEXT DEFAULT '',
                    sticker_url TEXT DEFAULT '',
                    sent_by     TEXT DEFAULT ''
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_pf_uid ON messages(platform, user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_time   ON messages(time)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS templates (
                    id         TEXT PRIMARY KEY,
                    category   TEXT NOT NULL,
                    text       TEXT DEFAULT '',
                    image_url  TEXT DEFAULT '',
                    sort_order INTEGER DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_data (
                    key        TEXT PRIMARY KEY,
                    status     TEXT DEFAULT 'bot',
                    note       TEXT DEFAULT '',
                    tags       TEXT DEFAULT '[]',
                    last_seen  FLOAT DEFAULT 0,
                    extra      TEXT DEFAULT '{}'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS product_jobs (
                    id          SERIAL PRIMARY KEY,
                    url         TEXT DEFAULT '',
                    platform    TEXT DEFAULT '',
                    status      TEXT DEFAULT 'pending',
                    raw_title   TEXT DEFAULT '',
                    raw_desc    TEXT DEFAULT '',
                    raw_images  TEXT DEFAULT '[]',
                    raw_price   TEXT DEFAULT '',
                    ai_name     TEXT DEFAULT '',
                    ai_desc     TEXT DEFAULT '',
                    ai_keywords TEXT DEFAULT '',
                    error_msg        TEXT DEFAULT '',
                    created_at       FLOAT DEFAULT 0,
                    updated_at       FLOAT DEFAULT 0
                )
            """)
            # migration：補 Phase 2 欄位（已存在則忽略）
            for col_sql in [
                "ALTER TABLE product_jobs ADD COLUMN IF NOT EXISTS processed_images TEXT DEFAULT '[]'",
                "ALTER TABLE product_jobs ADD COLUMN IF NOT EXISTS img_status TEXT DEFAULT ''",
            ]:
                try: cur.execute(col_sql)
                except Exception: pass
            conn.commit()
            cur.close()
            conn.close()
    except Exception as e:
        import sys; print(f"[DB Init Error] {e}", file=sys.stderr)

_init_messages_db()

# ── Supabase 客戶資料存取 ─────────────────────────────────

def _pg_upsert_customer(key, **fields):
    if not DATABASE_URL or not fields: return
    try:
        cols = ", ".join(fields.keys())
        vals = ", ".join(["%s"] * len(fields))
        updates = ", ".join(f"{k}=EXCLUDED.{k}" for k in fields)
        with _db_lock:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute(
                f"INSERT INTO customer_data (key,{cols}) VALUES (%s,{vals}) ON CONFLICT (key) DO UPDATE SET {updates}",
                [key] + list(fields.values())
            )
            conn.commit(); cur.close(); conn.close()
    except Exception as e:
        import sys; print(f"[PG Upsert Error] {e}", file=sys.stderr)

def _pg_get_customer(key):
    if not DATABASE_URL: return {}
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT status,note,tags,last_seen,extra FROM customer_data WHERE key=%s", (key,))
        row = cur.fetchone(); cur.close(); conn.close()
        if not row: return {}
        return {"status": row[0] or "bot", "note": row[1] or "", "tags": json.loads(row[2] or "[]"),
                "last_seen": row[3] or 0, "extra": json.loads(row[4] or "{}")}
    except Exception:
        return {}

def _pg_load_all_customers():
    if not DATABASE_URL: return {}
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT key,status,note,tags,last_seen,extra FROM customer_data")
        rows = cur.fetchall(); cur.close(); conn.close()
        result = {}
        for r in rows:
            result[r[0]] = {"status": r[1] or "bot", "note": r[2] or "",
                            "tags": json.loads(r[3] or "[]"), "last_seen": r[4] or 0,
                            "extra": json.loads(r[5] or "{}")}
        return result
    except Exception:
        return {}

_customer_cache = {}

def _load_customer_cache():
    global _customer_cache
    _customer_cache = _pg_load_all_customers()

threading.Thread(target=_load_customer_cache, daemon=True).start()

def _pg_get_status(key):
    return _customer_cache.get(key, {}).get("status", "bot")

def _pg_set_status(key, status):
    _customer_cache.setdefault(key, {})["status"] = status
    threading.Thread(target=_pg_upsert_customer, args=(key,), kwargs={"status": status}, daemon=True).start()

def _pg_get_tags(key):
    return _customer_cache.get(key, {}).get("tags", [])

def _pg_set_tags(key, tags):
    _customer_cache.setdefault(key, {})["tags"] = tags
    threading.Thread(target=_pg_upsert_customer, args=(key,), kwargs={"tags": json.dumps(tags, ensure_ascii=False)}, daemon=True).start()

def _pg_get_note(key):
    return _customer_cache.get(key, {}).get("note", "")

def _pg_set_note(key, note):
    _customer_cache.setdefault(key, {})["note"] = note
    threading.Thread(target=_pg_upsert_customer, args=(key,), kwargs={"note": note}, daemon=True).start()

def _pg_get_last_seen(key):
    return _customer_cache.get(key, {}).get("last_seen", 0)

def _pg_set_last_seen(key, ts):
    _customer_cache.setdefault(key, {})["last_seen"] = ts
    threading.Thread(target=_pg_upsert_customer, args=(key,), kwargs={"last_seen": ts}, daemon=True).start()

def _pg_get_all_statuses():
    return {k: v.get("status","bot") for k, v in _customer_cache.items()}

def _pg_get_customer_extra(key):
    return _customer_cache.get(key, {}).get("extra", {})

def _pg_save_customer_extra(key, pf, uid, data):
    _customer_cache.setdefault(key, {})["extra"] = data
    threading.Thread(target=_pg_upsert_customer, args=(key,),
                     kwargs={"extra": json.dumps(data, ensure_ascii=False)}, daemon=True).start()

_DEFAULT_TEMPLATES = [
    ("打招呼","你好，我是JSIMPLE高架床專員，請問有什麼可以幫您？",""),
    ("打招呼","感謝您的詢問，請問您的需求是？",""),
    ("報價","我們的高架床系列售價從NT$7,000起，依尺寸和材質不同，我幫您報正確的價格。",""),
    ("報價","請問需要的尺寸是單人(90cm)、標準(120cm)還是雙人(150cm)？",""),
    ("尺寸","標準房間建議90x190或120x190，需要我幫您確認空間適合哪種嗎？",""),
    ("交期","現貨商品約2-5個工作天可出貨，訂製款需要4-6週。",""),
    ("跟進","您好，上次有詢問高架床，請問有決定了嗎？有任何問題都可以告訴我。",""),
    ("成交","感謝您的訂購，我馬上幫您安排出貨，請確認收件地址是否正確。",""),
]

def _seed_templates():
    if not DATABASE_URL:
        return
    try:
        import uuid as _uuid
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM templates")
        if cur.fetchone()[0] == 0:
            for i, (cat, txt, img) in enumerate(_DEFAULT_TEMPLATES):
                cur.execute(
                    "INSERT INTO templates (id,category,text,image_url,sort_order) VALUES (%s,%s,%s,%s,%s)",
                    (str(_uuid.uuid4()), cat, txt, img, i)
                )
            conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        import sys; print(f"[Templates seed] {e}", file=sys.stderr)

threading.Thread(target=_seed_templates, daemon=True).start()

def _db_insert_message(entry):
    if not DATABASE_URL:
        return
    try:
        with _db_lock:
            conn = _pg_conn()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO messages
                  (time,platform,user_id,msg,intent,reply,replied,image_url,sticker_url,sent_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                entry.get("time",""),
                entry.get("platform",""),
                entry.get("user_id",""),
                entry.get("msg",""),
                entry.get("intent",""),
                entry.get("reply",""),
                bool(entry.get("replied")),
                entry.get("image_url",""),
                entry.get("sticker_url",""),
                entry.get("sent_by",""),
            ))
            conn.commit()
            cur.close()
            conn.close()
    except Exception as e:
        import sys; print(f"[DB Insert Error] {e}", file=sys.stderr)

def _load_logs_from_db():
    if not DATABASE_URL:
        return deque()
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT time,platform,user_id,msg,intent,reply,replied,image_url,sticker_url,sent_by
            FROM messages ORDER BY id DESC
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        d = deque()
        for r in rows:
            d.append({
                "time": r[0], "platform": r[1], "user_id": r[2],
                "msg": r[3], "intent": r[4], "reply": r[5],
                "replied": bool(r[6]), "image_url": r[7] or "",
                "sticker_url": r[8] or "", "sent_by": r[9] or "",
            })
        return d
    except Exception as e:
        import sys; print(f"[DB Load Error] {e}", file=sys.stderr)
        return deque()

def _migrate_json_to_db():
    if not DATABASE_URL:
        return
    try:
        with open(LOGS_FILE, "r", encoding="utf-8") as f:
            entries = json.load(f)
        with _db_lock:
            conn = _pg_conn()
            cur = conn.cursor()
            for entry in reversed(entries):
                cur.execute("""
                    INSERT INTO messages
                      (time,platform,user_id,msg,intent,reply,replied,image_url,sticker_url,sent_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    entry.get("time",""), entry.get("platform",""), entry.get("user_id",""),
                    entry.get("msg",""), entry.get("intent",""), entry.get("reply",""),
                    bool(entry.get("replied")),
                    entry.get("image_url",""), entry.get("sticker_url",""), entry.get("sent_by",""),
                ))
            conn.commit()
            cur.close()
            conn.close()
        print(f"[DB] Migrated {len(entries)} messages from logs.json")
    except Exception as e:
        print(f"[DB] Migration skipped: {e}")

def _load_logs():
    d = _load_logs_from_db()
    if not d:
        _migrate_json_to_db()
        d = _load_logs_from_db()
    if not d:
        try:
            with open(LOGS_FILE, "r", encoding="utf-8") as f:
                return deque(json.load(f))
        except Exception:
            pass
    return d

message_log = _load_logs()

def _save_logs():
    try:
        with open(LOGS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(message_log)[:500], f, ensure_ascii=False)
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
    threading.Thread(target=_db_insert_message, args=(entry,), daemon=True).start()
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

# 啟動時從 Supabase 同步 human 狀態到 manual_takeover
def _sync_status_from_db():
    import time as _t; _t.sleep(2)  # 等 customer_cache 載入
    try:
        statuses = _pg_get_all_statuses()
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

# 用戶備註 — 從 Supabase customer_cache 讀寫
def _get_note(key):
    return _pg_get_note(key)

def _set_note(key, note):
    _pg_set_note(key, note)

# 貼文指定回覆 {post_id: {"reply": str, "image_url": str, "enabled": bool}}
fb_post_replies: dict = {}


# 標籤與 last_seen 皆由 Supabase customer_cache 提供
last_seen = {}  # 保留為 in-memory 快取，寫入由 _pg_set_last_seen 非同步處理

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
        if SUPABASE_SERVICE_KEY:
            image_url, _ = upload_image_to_supabase(filename, data)
        else:
            image_url, _ = upload_image_to_github(filename, data)
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

def line_push_file(user_id: str, file_url: str, filename: str, file_size: int = 0):
    url = "https://api.line.me/v2/bot/message/push"
    payload = json.dumps({"to": user_id, "messages": [{
        "type": "file",
        "originalContentUrl": file_url,
        "fileName": filename,
        "fileSize": file_size
    }]}).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    })
    try:
        urllib.request.urlopen(req)
    except Exception:
        # fallback: send as text link
        line_push(user_id, f"📎 {filename}\n{file_url}")

def fb_send_file(psid: str, file_url: str):
    url = f"https://graph.facebook.com/v22.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
    payload = json.dumps({"recipient": {"id": psid}, "message": {
        "attachment": {"type": "file", "payload": {"url": file_url, "is_reusable": True}}
    }}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass

def upload_image_to_supabase(filename: str, data: bytes, content_type: str = "image/jpeg") -> tuple:
    """Upload image to Supabase Storage. Returns (public_url, error_msg)."""
    if not SUPABASE_SERVICE_KEY:
        return "", "SUPABASE_SERVICE_KEY 未設定"
    try:
        upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
        req = urllib.request.Request(
            upload_url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": content_type,
                "x-upsert": "true",
            }
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
        return public_url, ""
    except Exception as e:
        return "", str(e)

def upload_image_to_github(filename: str, data: bytes) -> tuple:
    """Returns (url, error_msg). url is empty string on failure."""
    import sys
    if not GITHUB_TOKEN:
        return "", "GITHUB_TOKEN 未設定"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/images/{filename}"
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        })
        sha = None
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
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
        with urllib.request.urlopen(req2, timeout=30) as r:
            resp = json.loads(r.read())
        raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/images/{filename}"
        return raw_url, ""
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors="ignore")
        msg = f"GitHub HTTP {e.code}: {body_err[:200]}"
        print(f"[GitHub Upload Error] {msg}", file=sys.stderr)
        return "", msg
    except Exception as e:
        msg = str(e)
        print(f"[GitHub Upload Error] {msg}", file=sys.stderr)
        return "", msg

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
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    import re, time as _time
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", f.filename)
    filename = f"{int(_time.time())}_{safe}"
    if SUPABASE_SERVICE_KEY:
        image_url, err = upload_image_to_supabase(filename, f.read())
    else:
        image_url, err = upload_image_to_github(filename, f.read())
    if not image_url:
        return jsonify({"error": err or "上傳失敗"}), 500
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
.mobile-back{display:none}
@media(max-width:820px){
  body{overflow:hidden}
  .crm-wrap{display:flex;flex-direction:row;width:200vw;height:100vh;transition:transform .25s ease;grid-template-columns:unset}
  .crm-wrap.mobile-chat{transform:translateX(-100vw)}
  .sidebar{width:100vw;min-width:100vw;max-width:100vw;flex-shrink:0;overflow-y:auto}
  .chat-main{width:100vw;min-width:100vw;max-width:100vw;flex-shrink:0;display:flex!important}
  .right-panel{display:none!important}
  .mobile-back{display:flex;align-items:center;justify-content:center;width:36px;height:36px;border:none;background:none;font-size:26px;cursor:pointer;flex-shrink:0;color:#333;line-height:1}
  .chat-header{padding:8px 10px;gap:6px;flex-wrap:nowrap}
  .status-select{font-size:11px;padding:3px 5px;max-width:80px}
  .takeover-btn{font-size:11px;padding:4px 8px}
  .btn-icon{width:34px;height:34px;font-size:15px}
  .input-area{padding:6px 8px;gap:4px}
}

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
.unread-badge{background:#e53935;color:#fff;border-radius:10px;padding:1px 6px;font-size:10px;font-weight:700;min-width:18px;text-align:center;display:inline-block;line-height:16px}
.conv-item.has-unread .conv-name{font-weight:800;color:#0d0d0d}
.conv-item.has-unread .conv-preview{color:#555;font-weight:500}
.read-toggle{width:10px;height:10px;border-radius:50%;border:2px solid #9aa0a6;background:transparent;cursor:pointer;flex-shrink:0;padding:0;margin-left:4px;transition:background 0.15s,border-color 0.15s}
.read-toggle.is-unread{background:#e53935;border-color:#e53935}
.read-toggle:hover{border-color:#0d6efd}

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

.tpl-panel{background:#fff;border-top:1px solid #e8eaed;display:none;flex-direction:column;max-height:280px}
.tpl-panel.open{display:flex}
.tpl-header{display:flex;align-items:center;justify-content:space-between;padding:6px 12px;border-bottom:1px solid #f0f2f5;flex-shrink:0}
.tpl-header-title{font-size:11px;font-weight:700;color:#555}
.tpl-header-btns{display:flex;gap:6px}
.tpl-hbtn{font-size:10px;padding:2px 8px;border-radius:10px;border:1.5px solid #ddd;background:#fff;cursor:pointer;color:#555}
.tpl-hbtn:hover{border-color:#0d6efd;color:#0d6efd}
.tpl-hbtn.active{background:#0d6efd;border-color:#0d6efd;color:#fff}
.tpl-cats{display:flex;gap:4px;padding:6px 12px;overflow-x:auto;border-bottom:1px solid #f0f2f5;flex-shrink:0}
.tpl-cats::-webkit-scrollbar{height:3px}
.tpl-cats::-webkit-scrollbar-thumb{background:#ddd}
.tcat{padding:3px 10px;border-radius:12px;font-size:11px;cursor:pointer;border:1.5px solid #ddd;color:#555;white-space:nowrap}
.tcat.active{background:#0d6efd;border-color:#0d6efd;color:#fff}
.tpl-list{overflow-y:auto;flex:1}
.tpl-item{padding:7px 14px;cursor:pointer;border-bottom:1px solid #f5f6f8;font-size:12px;line-height:1.5;color:#333;display:flex;align-items:center;gap:6px}
.tpl-item:hover{background:#f0f8ff}
.tpl-item-body{flex:1;min-width:0}
.tpl-item-cat{font-size:10px;color:#9aa0a6;margin-bottom:1px}
.tpl-item-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tpl-item-img{width:28px;height:28px;border-radius:4px;object-fit:cover;flex-shrink:0}
.tpl-item-actions{display:none;gap:4px;flex-shrink:0}
.tpl-edit-mode .tpl-item-actions{display:flex}
.tpl-act{font-size:11px;padding:2px 6px;border-radius:6px;border:1px solid #ddd;background:#fff;cursor:pointer}
.tpl-act:hover{background:#f0f8ff}
.tpl-modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:9999;align-items:center;justify-content:center}
.tpl-modal-overlay.open{display:flex}
.tpl-modal{background:#fff;border-radius:12px;padding:20px;width:360px;max-width:95vw;box-shadow:0 8px 32px rgba(0,0,0,.18)}
.tpl-modal h3{font-size:14px;font-weight:700;margin-bottom:14px}
.tpl-form-row{margin-bottom:10px}
.tpl-form-row label{display:block;font-size:11px;color:#555;margin-bottom:3px;font-weight:600}
.tpl-form-row input,.tpl-form-row textarea,.tpl-form-row select{width:100%;padding:6px 8px;border:1.5px solid #ddd;border-radius:6px;font-size:12px;outline:none;box-sizing:border-box}
.tpl-form-row input:focus,.tpl-form-row textarea:focus{border-color:#0d6efd}
.tpl-form-row textarea{resize:vertical;min-height:70px}
.tpl-img-preview{width:60px;height:60px;border-radius:6px;object-fit:cover;margin-top:6px;display:block}
.tpl-modal-footer{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
.tpl-btn-save{padding:6px 16px;background:#0d6efd;color:#fff;border:none;border-radius:8px;font-size:12px;cursor:pointer;font-weight:600}
.tpl-btn-cancel{padding:6px 16px;background:#f0f2f5;color:#555;border:none;border-radius:8px;font-size:12px;cursor:pointer}

.input-area{background:#fff;border-top:1px solid #e8eaed;padding:10px 12px;display:flex;gap:8px;align-items:flex-end;flex-shrink:0}
.input-area textarea{flex:1;border:1.5px solid #e8eaed;border-radius:12px;padding:8px 12px;font-size:13px;resize:none;outline:none;font-family:inherit;line-height:1.5;max-height:120px}
.input-area textarea:focus{border-color:#0d6efd}
.btn-icon{width:36px;height:36px;border-radius:50%;border:1.5px solid #e8eaed;background:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;transition:.15s}
.btn-icon:hover{background:#f0f8ff;border-color:#0d6efd}
.file-bubble{display:flex;align-items:center;gap:8px;background:#f0f4ff;border:1px solid #c5d5fb;border-radius:10px;padding:8px 12px;max-width:220px;cursor:pointer;text-decoration:none;color:#1a1a1a}
.file-bubble-icon{font-size:22px;flex-shrink:0}
.file-bubble-info{min-width:0}
.file-bubble-name{font-size:12px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-bubble-size{font-size:10px;color:#9aa0a6}
.img-lib-panel{display:none;border-top:1px solid #e8eaed;padding:10px 12px;background:#fff;max-height:220px;overflow-y:auto}
.img-lib-panel.open{display:block}
.img-lib-grid{display:flex;flex-wrap:wrap;gap:8px}
.img-lib-item{position:relative;width:80px;height:80px;border-radius:8px;overflow:hidden;cursor:pointer;border:2px solid transparent;transition:.15s}
.img-lib-item:hover{border-color:#0d6efd}
.img-lib-item img{width:100%;height:100%;object-fit:cover}
.img-lib-del{position:absolute;top:2px;right:2px;background:rgba(0,0,0,.55);color:#fff;border:none;border-radius:50%;width:18px;height:18px;font-size:11px;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0}
.img-lib-add{width:80px;height:80px;border-radius:8px;border:2px dashed #c5cdd6;display:flex;flex-direction:column;align-items:center;justify-content:center;cursor:pointer;color:#9aa0a6;font-size:22px;background:#fafafa;transition:.15s;flex-shrink:0}
.img-lib-add:hover{border-color:#0d6efd;color:#0d6efd}
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
    <button class="mobile-back" onclick="mobileBack()" title="返回列表">&#8249;</button>
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
    <div class="tpl-header">
      <span class="tpl-header-title">⚡ 快速回覆</span>
      <div class="tpl-header-btns">
        <button class="tpl-hbtn" onclick="openTplModal(null)">＋ 新增</button>
        <button class="tpl-hbtn" id="tplEditBtn" onclick="toggleTplEditMode()">✏️ 管理</button>
      </div>
    </div>
    <div class="tpl-cats" id="tplCats"></div>
    <div class="tpl-list" id="tplList"></div>
  </div>
  <div class="tpl-modal-overlay" id="tplModal">
    <div class="tpl-modal">
      <h3 id="tplModalTitle">新增快捷模板</h3>
      <div class="tpl-form-row">
        <label>分類名稱</label>
        <input id="tplFormCat" placeholder="例：報價、跟進、成交" list="tplCatList">
        <datalist id="tplCatList"></datalist>
      </div>
      <div class="tpl-form-row">
        <label>回覆文字（可空白）</label>
        <textarea id="tplFormText" placeholder="輸入要發送的文字..."></textarea>
      </div>
      <div class="tpl-form-row">
        <label>附加圖片（可選）</label>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="tpl-hbtn" onclick="pickTplImage()" style="flex-shrink:0">從圖庫選</button>
          <button class="tpl-hbtn" onclick="clearTplImage()" id="tplImgClearBtn" style="display:none">清除圖片</button>
        </div>
        <img id="tplFormImgPreview" class="tpl-img-preview" style="display:none">
        <input type="hidden" id="tplFormImgUrl">
      </div>
      <div class="tpl-modal-footer">
        <button class="tpl-btn-cancel" onclick="closeTplModal()">取消</button>
        <button class="tpl-btn-save" onclick="saveTpl()">儲存</button>
      </div>
    </div>
  </div>
  <div class="img-lib-panel" id="imgLibPanel">
    <div class="img-lib-grid" id="imgLibGrid"></div>
    <input type="file" id="imgLibInput" accept="image/*" style="display:none" onchange="saveToLibrary(this)">
  </div>
  <div class="input-area">
    <button class="btn-icon" onclick="toggleTpl()" title="快速回覆">&#9889;</button>
    <button class="btn-icon" onclick="toggleImgLib()" title="快速圖庫">&#128247;</button>
    <label class="btn-icon" title="傳送圖片/影片" style="cursor:pointer;font-size:13px">
      &#128190;
      <input type="file" id="imgInput" accept="image/*,video/*" style="display:none" onchange="uploadImg(this)">
    </label>
    <label class="btn-icon" title="傳送檔案（PDF等）" style="cursor:pointer;font-size:15px">
      &#128206;
      <input type="file" id="fileInput" accept="*/*" style="display:none" onchange="uploadFile(this)">
    </label>
    <textarea id="replyInput" placeholder="輸入訊息..." rows="1"
      oninput="autoResize(this)"
      onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendReply()}"
      onpaste="handlePaste(event)"></textarea>
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

function fmtConvTime(ts){
  if(!ts) return '';
  const d = new Date(ts*1000);
  const now = new Date();
  const isToday = d.getFullYear()===now.getFullYear() && d.getMonth()===now.getMonth() && d.getDate()===now.getDate();
  if(isToday) return d.toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit'});
  return d.getFullYear()+'/'+(d.getMonth()+1)+'/'+d.getDate();
}

let allConvs = [], curKey = null, curStatus = 'bot', filterStatus = 'all', filterTag = null, searchQ = '';
let curTags = [], curCustomer = {}, noteTimer = null;

// INIT
const isMobile = ()=> window.innerWidth <= 820;

function mobileShowChat(){
  if(!isMobile()) return;
  document.querySelector('.crm-wrap').classList.add('mobile-chat');
}

function mobileBack(){
  document.querySelector('.crm-wrap').classList.remove('mobile-chat');
}

async function init(){
  buildTagFilter();
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
    const unread = c.unread||0;
    const tags = (c.tags||[]).slice(0,3).map(t=>`<span class="tag-pill">${t}</span>`).join('');
    const pb = c.platform?.toLowerCase()==='fb'?'<span class="platform-badge pb-fb">FB</span>':'<span class="platform-badge pb-line">LINE</span>';
    const timeStr = c.last_time ? fmtConvTime(c.last_time) : '';
    const avatarEl = c.user_avatar
      ? `<img class="conv-avatar" src="${c.user_avatar}" onerror="this.style.display='none'">`
      : `<div class="conv-avatar-placeholder">&#128100;</div>`;
    const unreadBadge = unread>0 ? `<span class="unread-badge">${unread}</span>` : '';
    const hasUnread = unread>0 ? ' has-unread' : '';
    const toggleClass = unread>0 ? ' is-unread' : '';
    const safeKey = c.key.replace(/'/g,"\\'");
    return `<div class="conv-item${c.key===curKey?' active':''}${hasUnread}" onclick="openConv('${safeKey}')">
      ${avatarEl}
      <div class="conv-info">
        <div class="conv-name-row">
          <span class="conv-name">${escHtml(c.user_name||c.user_id||'未知用戶')}</span>
          <div style="display:flex;align-items:center;gap:4px;flex-shrink:0">
            ${unreadBadge}
            <button class="read-toggle${toggleClass}" title="${unread>0?'標為已讀':'標為未讀'}" onclick="toggleRead(event,'${safeKey}')"></button>
            <span class="conv-time">${timeStr}</span>
          </div>
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

async function toggleRead(evt, key){
  evt.stopPropagation();
  const c = allConvs.find(x=>x.key===key);
  if(!c) return;
  if(c.unread>0){
    // 標為已讀
    await fetch('/api/seen',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key,admin_key:KEY})});
    c.unread=0;
  } else {
    // 標為未讀
    await fetch('/api/mark-unread',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key,admin_key:KEY})});
    c.unread=1;
  }
  renderList();
}

async function openConv(key){
  curKey = key;
  mobileShowChat();
  document.querySelectorAll('.conv-item').forEach(el=>el.classList.remove('active'));
  document.querySelector(`.conv-item[onclick="openConv('${key}')"]`)?.classList.add('active');

  const conv = allConvs.find(c=>c.key===key);
  curStatus = conv?.status || 'bot';

  // show header
  const hdr = document.getElementById('chatHeader');
  hdr.style.display = 'flex';
  const name = conv?.user_name || conv?.user_id || '未知用戶';
  document.getElementById('hdrName').textContent = name;
  document.getElementById('hdrSub').textContent = conv?.platform?.toLowerCase()==='fb'?'Facebook Messenger':'LINE';
  const hdrAv = document.getElementById('hdrAvatar');
  if(conv?.user_avatar){
    hdrAv.outerHTML = `<img class="chat-header-avatar" id="hdrAvatar" src="${conv.user_avatar}">`;
  }
  updateHeaderStatus(curStatus);

  // mark as read
  fetch('/api/seen',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({key:key,admin_key:KEY})});
  const c2 = allConvs.find(x=>x.key===key);
  if(c2){ c2.unread=0; renderList(); }
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
    const fileMatch = (m.content||'').match(/^\[檔案\] (.+)$/);
    if(fileMatch && m.role==='admin'){
      // admin sent file - show link from log
      content = `<span>📎 ${escHtml(fileMatch[1])}</span>`;
    } else if(m.image_url){
      const imgUrl = m.image_url;
      const isVid=/\.(mp4|mov|avi|webm|m4v)(\?|$)/i.test(imgUrl);
      const isFile=/\/(files)\//i.test(imgUrl) || /\.(pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|txt|csv)(\?|$)/i.test(imgUrl);
      if(isVid) content=`<video src="${imgUrl}" controls style="max-width:220px;border-radius:12px;display:block"></video>`;
      else if(isFile){
        const fname = imgUrl.split('/').pop().split('?')[0].replace(/^\d+_/,'');
        const ext = fname.split('.').pop().toUpperCase();
        const icons = {PDF:'📕',DOC:'📘',DOCX:'📘',XLS:'📗',XLSX:'📗',PPT:'📙',PPTX:'📙',ZIP:'🗜️',RAR:'🗜️',TXT:'📄',CSV:'📊'};
        const icon = icons[ext]||'📎';
        content=`<a href="${imgUrl}" target="_blank" class="file-bubble">
          <span class="file-bubble-icon">${icon}</span>
          <div class="file-bubble-info"><div class="file-bubble-name">${escHtml(fname)}</div><div class="file-bubble-size">${ext} 點擊下載</div></div>
        </a>`;
      }
      else content=`<img class="msg-img" src="${imgUrl}" onclick="window.open(this.src)">`;
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

async function compressImage(file, maxW=1600, maxH=1600, quality=0.82){
  if(!file.type.startsWith('image/') || file.type==='image/gif') return file;
  return new Promise(resolve=>{
    const img = new Image();
    const blobUrl = URL.createObjectURL(file);
    img.onload = ()=>{
      let w = img.naturalWidth, h = img.naturalHeight;
      URL.revokeObjectURL(blobUrl);
      if(w<=maxW && h<=maxH){ resolve(file); return; }
      const ratio = Math.min(maxW/w, maxH/h);
      w = Math.round(w*ratio); h = Math.round(h*ratio);
      const canvas = document.createElement('canvas');
      canvas.width = w; canvas.height = h;
      canvas.getContext('2d').drawImage(img, 0, 0, w, h);
      canvas.toBlob(blob=>{
        resolve(new File([blob], file.name.replace(/\.\w+$/, '.jpg'), {type:'image/jpeg'}));
      }, 'image/jpeg', quality);
    };
    img.onerror = ()=>{ URL.revokeObjectURL(blobUrl); resolve(file); };
    img.src = blobUrl;
  });
}

async function uploadImg(input){
  if(!input.files[0]||!curKey) return;
  let file=input.files[0];
  const isVideo=file.type.startsWith('video/');
  if(!isVideo) file = await compressImage(file);
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

async function uploadFile(input){
  if(!input.files[0]||!curKey) return;
  const file = input.files[0];
  toast('檔案上傳中...');
  const reader = new FileReader();
  reader.onload = async e=>{
    const b64 = e.target.result.split(',')[1];
    try{
      const up = await fetch('/api/upload_file',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:curKey,admin_key:KEY,filename:file.name,content:b64})});
      const ud = await up.json();
      if(!ud.url){ toast('上傳失敗：'+(ud.error||'未知錯誤')); return; }
      await fetch('/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:curKey,admin_key:KEY,file_url:ud.url,filename:ud.filename||file.name,file_size:ud.size||0})});
      await loadMsgs(curKey);
      toast('檔案已傳送');
    }catch(e){ toast('傳送失敗：'+e.message); }
  };
  reader.readAsDataURL(file);
  input.value='';
}

async function handlePaste(e){
  const items = e.clipboardData?.items;
  if(!items) return;
  for(const item of items){
    if(item.type.startsWith('image/')){
      e.preventDefault();
      if(!curKey){ toast('請先選擇對話'); return; }
      let file = item.getAsFile();
      if(!file) return;
      file = await compressImage(file);
      toast('圖片上傳中...');
      const reader = new FileReader();
      reader.onload = async re=>{
        const b64 = re.target.result.split(',')[1];
        try{
          const up = await fetch('/api/upload_image',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({key:curKey,admin_key:KEY,filename:'paste.jpg',content:b64})});
          const ud = await up.json();
          if(!ud.url){ toast('上傳失敗：'+(ud.error||'')); return; }
          await fetch('/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({key:curKey,admin_key:KEY,image_url:ud.url})});
          await loadMsgs(curKey);
        }catch(err){ toast('上傳失敗：'+err.message); }
      };
      reader.readAsDataURL(file);
      return;
    }
  }
}

// ── 快捷模板 ──────────────────────────────────────────────
let allTpls = [], curTplCat = '', tplEditMode = false, tplEditId = null, imgLibForTpl = false;

async function loadTemplates(){
  try{
    const r = await fetch(`/api/templates?key=${KEY}`);
    allTpls = await r.json();
  }catch(e){ allTpls = []; }
}

function getTplCats(){
  return [...new Set(allTpls.map(t=>t.category))];
}

function buildTplCats(){
  loadTemplates().then(()=>{
    const cats = getTplCats();
    if(!curTplCat || !cats.includes(curTplCat)) curTplCat = cats[0]||'';
    renderTplCats();
    renderTplList();
    // datalist for modal
    document.getElementById('tplCatList').innerHTML = cats.map(c=>`<option value="${escHtml(c)}">`).join('');
  });
}

function renderTplCats(){
  const cats = getTplCats();
  document.getElementById('tplCats').innerHTML = cats.map(c=>
    `<div class="tcat${c===curTplCat?' active':''}" onclick="selectTplCat('${escAttr(c)}',this)">${escHtml(c)}</div>`
  ).join('');
}

function selectTplCat(cat, el){
  curTplCat = cat;
  document.querySelectorAll('.tcat').forEach(e=>e.classList.remove('active'));
  el.classList.add('active');
  renderTplList();
}

function renderTplList(){
  const items = allTpls.filter(t=>t.category===curTplCat);
  document.getElementById('tplList').innerHTML = items.length ? items.map(t=>{
    const imgEl = t.image_url ? `<img class="tpl-item-img" src="${t.image_url}">` : '';
    const combo = t.image_url ? ' 🖼️' : '';
    return `<div class="tpl-item"
      data-tpl-text="${escAttr(t.text||'')}"
      data-tpl-img="${escAttr(t.image_url||'')}"
      data-tpl-id="${escAttr(t.id)}"
      onclick="sendTpl(this)">
      <div class="tpl-item-body">
        <div class="tpl-item-cat">${escHtml(t.category)}${combo}</div>
        <div class="tpl-item-text">${escHtml(t.text||'（僅圖片）')}</div>
      </div>
      ${imgEl}
      <div class="tpl-item-actions">
        <button class="tpl-act" onclick="event.stopPropagation();openTplModal('${escAttr(t.id)}')">✏️</button>
        <button class="tpl-act" onclick="event.stopPropagation();deleteTpl('${escAttr(t.id)}')" style="color:#dc3545">🗑️</button>
      </div>
    </div>`;
  }).join('') : '<div style="padding:16px;text-align:center;color:#9aa0a6;font-size:12px">這個分類沒有模板，點「新增」加入</div>';
}

async function sendTpl(el){
  const text = el.dataset.tplText || '';
  const imgUrl = el.dataset.tplImg || '';
  const id = el.dataset.tplId || '';
  document.getElementById('tplPanel').classList.remove('open');
  if(imgUrl && text){
    if(!curKey){ toast('請先選擇對話'); return; }
    toast('傳送中...');
    try{
      await fetch('/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:curKey,admin_key:KEY,image_url:imgUrl})});
      await new Promise(r=>setTimeout(r,600));
      const r = await fetch('/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:curKey,admin_key:KEY,message:text})});
      const d = await r.json();
      if(!d.ok) toast('傳送失敗：'+(d.error||''));
    }catch(e){ toast('傳送失敗：'+e.message); }
    await loadMsgs(curKey);
  } else if(imgUrl){
    if(!curKey){ toast('請先選擇對話'); return; }
    toast('傳送中...');
    try{
      await fetch('/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:curKey,admin_key:KEY,image_url:imgUrl})});
    }catch(e){ toast('傳送失敗：'+e.message); }
    await loadMsgs(curKey);
  } else if(text){
    const inp = document.getElementById('replyInput');
    inp.value = text;
    autoResize(inp);
    inp.focus();
  }
}

function toggleTplEditMode(){
  tplEditMode = !tplEditMode;
  document.getElementById('tplPanel').classList.toggle('tpl-edit-mode', tplEditMode);
  document.getElementById('tplEditBtn').classList.toggle('active', tplEditMode);
  renderTplList();
}

function toggleTpl(){
  const p = document.getElementById('tplPanel');
  const isOpen = p.classList.contains('open');
  p.classList.toggle('open');
  document.getElementById('imgLibPanel').classList.remove('open');
  if(!isOpen) buildTplCats();
}

async function useTpl(id){
  const tpl = allTpls.find(t=>t.id===id);
  if(!tpl) return;
  document.getElementById('tplPanel').classList.remove('open');
  if(tpl.image_url && tpl.text){
    // 圖文整套：直接發送
    if(!curKey){ toast('請先選擇對話'); return; }
    toast('傳送中...');
    try{
      await fetch('/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:curKey,admin_key:KEY,image_url:tpl.image_url})});
      await new Promise(r=>setTimeout(r,600));
      const r = await fetch('/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:curKey,admin_key:KEY,message:tpl.text})});
      const d = await r.json();
      if(!d.ok) toast('文字傳送失敗：'+(d.error||''));
    }catch(e){ toast('傳送失敗：'+e.message); }
    await loadMsgs(curKey);
  } else if(tpl.image_url){
    // 純圖片：直接發送
    if(!curKey){ toast('請先選擇對話'); return; }
    toast('傳送中...');
    try{
      await fetch('/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:curKey,admin_key:KEY,image_url:tpl.image_url})});
    }catch(e){ toast('傳送失敗：'+e.message); }
    await loadMsgs(curKey);
  } else {
    const inp = document.getElementById('replyInput');
    inp.value = tpl.text;
    autoResize(inp);
    inp.focus();
  }
}

function openTplModal(id){
  tplEditId = id;
  const tpl = id ? allTpls.find(t=>t.id===id) : null;
  document.getElementById('tplModalTitle').textContent = id ? '編輯快捷模板' : '新增快捷模板';
  document.getElementById('tplFormCat').value = tpl ? tpl.category : (curTplCat||'');
  document.getElementById('tplFormText').value = tpl ? tpl.text : '';
  const imgUrl = tpl ? (tpl.image_url||'') : '';
  document.getElementById('tplFormImgUrl').value = imgUrl;
  const preview = document.getElementById('tplFormImgPreview');
  const clearBtn = document.getElementById('tplImgClearBtn');
  if(imgUrl){ preview.src=imgUrl; preview.style.display='block'; clearBtn.style.display=''; }
  else { preview.style.display='none'; clearBtn.style.display='none'; }
  // refresh datalist
  const cats = getTplCats();
  document.getElementById('tplCatList').innerHTML = cats.map(c=>`<option value="${escHtml(c)}">`).join('');
  document.getElementById('tplModal').classList.add('open');
}

function closeTplModal(){
  document.getElementById('tplModal').classList.remove('open');
  tplEditId = null;
}

function clearTplImage(){
  document.getElementById('tplFormImgUrl').value = '';
  document.getElementById('tplFormImgPreview').style.display='none';
  document.getElementById('tplImgClearBtn').style.display='none';
}

function pickTplImage(){
  imgLibForTpl = true;
  document.getElementById('tplModal').classList.remove('open');
  document.getElementById('imgLibPanel').classList.add('open');
  loadQuickImages();
}

async function saveTpl(){
  const cat = document.getElementById('tplFormCat').value.trim();
  const text = document.getElementById('tplFormText').value.trim();
  const image_url = document.getElementById('tplFormImgUrl').value.trim();
  if(!cat){ toast('請填寫分類名稱'); return; }
  if(!text && !image_url){ toast('文字或圖片至少填一個'); return; }
  const body = {admin_key:KEY, category:cat, text, image_url};
  const url = tplEditId ? `/api/templates/${tplEditId}` : '/api/templates';
  const method = tplEditId ? 'PUT' : 'POST';
  try{
    const r = await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d = await r.json();
    if(d.ok||d.id){ toast(tplEditId?'已更新':'已新增'); closeTplModal(); await reloadTpls(); }
    else toast('儲存失敗：'+(d.error||''));
  }catch(e){ toast('儲存失敗：'+e.message); }
}

async function deleteTpl(id){
  if(!confirm('確定刪除這個模板？')) return;
  try{
    await fetch(`/api/templates/${id}`,{method:'DELETE',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({admin_key:KEY})});
    await reloadTpls();
  }catch(e){ toast('刪除失敗'); }
}

async function reloadTpls(){
  await loadTemplates();
  if(!getTplCats().includes(curTplCat)) curTplCat = getTplCats()[0]||'';
  renderTplCats();
  renderTplList();
}

// ── 快速圖庫 ──────────────────────────────────────────────
let quickImages = [];

async function toggleImgLib(){
  const panel = document.getElementById('imgLibPanel');
  const isOpen = panel.classList.contains('open');
  panel.classList.toggle('open');
  document.getElementById('tplPanel').classList.remove('open');
  if(!isOpen) await loadQuickImages();
}

async function loadQuickImages(){
  try{
    const r = await fetch(`/api/quick-images?key=${KEY}`);
    quickImages = await r.json();
    renderImgLib();
  }catch(e){ console.error(e); }
}

function renderImgLib(){
  const grid = document.getElementById('imgLibGrid');
  const items = quickImages.map(img=>`
    <div class="img-lib-item" onclick="sendQuickImg('${img.url}')">
      <img src="${img.url}" loading="lazy">
      <button class="img-lib-del" onclick="deleteQuickImg(event,'${img.name}')" title="刪除">✕</button>
    </div>`).join('');
  grid.innerHTML = items + `<div class="img-lib-add" onclick="document.getElementById('imgLibInput').click()">
    <span>＋</span><span style="font-size:10px;margin-top:2px">新增</span></div>`;
}

async function sendQuickImg(url){
  document.getElementById('imgLibPanel').classList.remove('open');
  if(imgLibForTpl){
    imgLibForTpl = false;
    document.getElementById('tplFormImgUrl').value = url;
    const preview = document.getElementById('tplFormImgPreview');
    preview.src = url; preview.style.display='block';
    document.getElementById('tplImgClearBtn').style.display='';
    document.getElementById('tplModal').classList.add('open');
    return;
  }
  if(!curKey){ toast('請先選擇對話'); return; }
  toast('傳送中...');
  try{
    await fetch('/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:curKey,admin_key:KEY,image_url:url})});
    await loadMsgs(curKey);
  }catch(e){ toast('傳送失敗：'+e.message); }
}

async function saveToLibrary(input){
  if(!input.files[0]) return;
  const file = input.files[0];
  toast('儲存圖片中...');
  const reader = new FileReader();
  reader.onload = async e=>{
    const b64 = e.target.result.split(',')[1];
    try{
      const r = await fetch('/api/save-quick-image',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({admin_key:KEY,filename:file.name,content:b64})});
      const d = await r.json();
      if(d.ok){ toast('已儲存到圖庫'); await loadQuickImages(); }
      else toast('儲存失敗：'+(d.error||''));
    }catch(e){ toast('儲存失敗：'+e.message); }
  };
  reader.readAsDataURL(file);
  input.value='';
}

async function deleteQuickImg(evt, name){
  evt.stopPropagation();
  if(!confirm('確定刪除這張圖片？')) return;
  try{
    await fetch('/api/delete-quick-image',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({admin_key:KEY,name})});
    await loadQuickImages();
  }catch(e){ toast('刪除失敗'); }
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
  const platform = conv?.platform?.toLowerCase()==='fb'?'Facebook':'LINE';
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
  <a class="card" href="/admin/products?key={{ key }}">
    <div class="card-left">
      <div class="icon" style="background:#e8f5e9">🛒</div>
      <div>
        <div class="card-title">AI 商品搬運中心</div>
        <div class="card-sub">1688 / 淘寶 → AI 改寫台灣文案</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span class="arrow">›</span>
    </div>
  </a>
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
.card{background:#fff;border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.card-title{font-size:15px;font-weight:700;margin-bottom:10px}
.add-card{border:2px dashed #e0e0e0;border-radius:12px;padding:18px;margin-bottom:12px;background:none}
.add-card:hover{border-color:var(--ac)}
.add-title{font-size:14px;font-weight:700;color:#888;margin-bottom:12px}
.field{margin-bottom:10px}
.field label{font-size:13px;color:#888;display:block;margin-bottom:4px}
.btn-add{width:100%;padding:11px;background:var(--ac);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;margin-top:4px}
.btn-add:hover{opacity:.85}
.btn-row{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #eee;padding:10px 14px;display:flex;gap:10px;justify-content:center;z-index:100}
.btn{padding:10px 22px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.btn:hover{opacity:.85}
.btn-save{background:var(--ac);color:#fff}
.test-input{width:100%;border:1px solid #ddd;border-radius:8px;padding:11px;font-size:15px;font-family:inherit}
.test-input:focus{outline:none;border-color:var(--ac)}
.btn-test{width:100%;margin-top:10px;padding:11px;background:var(--ac);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.test-result{margin-top:14px;display:none}
.intent-badge{display:inline-block;padding:3px 10px;border-radius:10px;font-size:12px;font-weight:600;margin-bottom:8px;background:rgba(var(--ac-rgb),.12);color:var(--ac)}
.reply-pre{background:#f5f5f5;border-radius:8px;padding:12px;font-size:14px;line-height:1.7;white-space:pre-wrap}
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

  <div id="tab-replies" class="tab-content active">
    <form method="POST" action="/admin/{{ platform }}/save?key={{ key }}">
      {% for id, label in cfg.labels.items() %}
      {% set intent_on = cfg.enabled_intents.get(id, True) %}
      {% set img = cfg.image_urls.get(id,'') %}
      <div class="intent-card{{ '' if intent_on else ' disabled' }}" id="card-{{ id }}">
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
        <div class="ic-body">
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

  <div id="tab-logs" class="tab-content">
    <div class="card" style="padding:0;overflow:hidden">
      <div id="log-content"><div class="empty">載入中...</div></div>
    </div>
  </div>
</div>

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
      <label>圖片網址（選填）</label>
      <input type="text" name="image_url" value="{{ cfg.image_url }}" placeholder="https://...">
      <button type="submit" class="btn btn-add" style="margin-top:12px">💾 儲存此貼文</button>
    </div>
  </form>
  {% else %}
  <div style="text-align:center;color:#bbb;padding:40px;font-size:14px">尚未設定任何貼文，新增後即可指定回覆</div>
  {% endfor %}

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
    _pg_set_status(key, status)
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
        return jsonify(_pg_get_customer_extra(key))
    data = request.get_json(silent=True) or {}
    parts = key.split(":",1)
    pf = parts[0] if len(parts)>1 else ""
    uid = parts[1] if len(parts)>1 else key
    _pg_save_customer_extra(key, pf, uid, data)
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
    _pg_set_tags(key, tags)
    return jsonify({"ok": True})

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
            ts = int(time.mktime(time.strptime(l.get("time", ""), "%Y/%m/%d %H:%M:%S")) - 8*3600)
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
                          "status": _pg_get_status(key),
                          "name": profile.get("name",""),
                          "user_name": profile.get("name","") or uid,
                          "avatar": profile.get("avatar",""),
                          "user_avatar": profile.get("avatar",""),
                          "note": _pg_get_note(key),
                          "tags": _pg_get_tags(key),
                          "unread": 0}
        convs[key]["messages"].append(l)
        convs[key]["last_time"] = l.get("time","")
        convs[key]["last_msg"] = l.get("msg","")
        convs[key]["last_message"] = l.get("msg","")
        seen_ts = _pg_get_last_seen(key)
        try:
            msg_ts = time.mktime(time.strptime(l.get("time",""), "%Y/%m/%d %H:%M:%S")) - 8*3600
            if msg_ts > seen_ts and l.get("user_id","") != "ADMIN" and l.get("sent_by","") != "admin":
                convs[key]["unread"] += 1
        except Exception:
            pass
    for v in convs.values():
        try:
            ts = time.mktime(time.strptime(v.get("last_time",""), "%Y/%m/%d %H:%M:%S")) - 8*3600
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
    key = data.get("key","") or f"{platform}:{user_id}"
    _set_note(key, note)
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
    text = (data.get("message", "") or data.get("text", "")).strip()
    image_url = data.get("image_url", "").strip()
    video_url = data.get("video_url", "").strip()
    preview_url = data.get("preview_url", "").strip()
    file_url = data.get("file_url", "").strip()
    filename = data.get("filename", "檔案").strip()
    file_size = int(data.get("file_size", 0))
    if not user_id or (not text and not image_url and not video_url and not file_url):
        return jsonify({"error": "missing fields"}), 400
    try:
        if platform == "LINE":
            if text: line_push(user_id, text)
            if image_url: line_push_image(user_id, image_url)
            if video_url: line_push_video(user_id, video_url, preview_url or video_url)
            if file_url: line_push_file(user_id, file_url, filename, file_size)
        elif platform == "FB":
            if text: fb_send(user_id, text)
            if image_url: fb_send_image(user_id, image_url)
            if video_url: fb_send_video(user_id, video_url)
            if file_url: fb_send_file(user_id, file_url)
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
        if file_url:
            log_message({"time": now, "platform": platform, "user_id": user_id,
                         "msg": "", "intent": "manual", "reply": f"[檔案] {filename}", "replied": True,
                         "sent_by": "admin"})
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
    url, err = upload_image_to_github(fname, raw)
    if not url:
        return jsonify({"error": err or "上傳失敗"}), 500
    return jsonify({"url": url})

@app.route("/api/upload_file", methods=["POST"])
def api_upload_file():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "file")
    content_b64 = data.get("content", "")
    if not content_b64:
        return jsonify({"error": "no content"}), 400
    import re as _re, mimetypes as _mt
    safe = _re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    fname = f"files/{int(time.time())}_{safe}"
    try:
        raw = base64.b64decode(content_b64)
    except Exception:
        return jsonify({"error": "invalid base64"}), 400
    ct = _mt.guess_type(filename)[0] or "application/octet-stream"
    url, err = upload_image_to_supabase(fname, raw, ct)
    if not url:
        return jsonify({"error": err or "上傳失敗"}), 500
    return jsonify({"url": url, "filename": safe, "size": len(raw)})

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
    _pg_set_tags(key, tags)
    return jsonify({"ok": True})

# ── 已讀標記 API ─────────────────────────────────────────

@app.route("/api/seen", methods=["POST"])
def api_seen():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    key = data.get("key", "")
    if not key:
        platform = data.get("platform", "").upper()
        user_id = data.get("user_id", "")
        key = f"{platform}:{user_id}"
    if key:
        _pg_set_last_seen(key, time.time())
    return jsonify({"ok": True})

@app.route("/api/mark-unread", methods=["POST"])
def api_mark_unread():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    key = data.get("key", "")
    if not key:
        platform = data.get("platform", "").upper()
        user_id = data.get("user_id", "")
        key = f"{platform}:{user_id}"
    if key:
        _pg_set_last_seen(key, 0)
    return jsonify({"ok": True})

@app.route("/api/github-diag")
def api_github_diag():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    result = {"token_set": bool(GITHUB_TOKEN), "repo": GITHUB_REPO}
    if not GITHUB_TOKEN:
        return jsonify(result)
    try:
        # 測試 token 是否有效
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}",
            headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
            result["repo_accessible"] = True
            result["repo_name"] = d.get("full_name", "")
    except urllib.error.HTTPError as e:
        result["repo_accessible"] = False
        result["repo_error"] = f"HTTP {e.code}: {e.read().decode(errors='ignore')[:200]}"
    except Exception as e:
        result["repo_accessible"] = False
        result["repo_error"] = str(e)
    # 試著上傳一個小測試檔
    test_url, test_err = upload_image_to_github("diag_test.txt", b"test")
    result["upload_test_ok"] = bool(test_url)
    result["upload_test_error"] = test_err
    return jsonify(result)

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

# ── 快捷模板 CRUD API ─────────────────────────────────────

@app.route("/api/templates", methods=["GET"])
def api_get_templates():
    ok, _ = auth_required()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    if not DATABASE_URL: return jsonify([])
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT id,category,text,image_url,sort_order FROM templates ORDER BY sort_order,id")
        rows = cur.fetchall(); cur.close(); conn.close()
        return jsonify([{"id":r[0],"category":r[1],"text":r[2],"image_url":r[3] or ""} for r in rows])
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/templates", methods=["POST"])
def api_create_template():
    ok, _ = auth_required()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    category = data.get("category","").strip()
    text = data.get("text","").strip()
    image_url = data.get("image_url","").strip()
    if not category: return jsonify({"error":"category required"}), 400
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM templates")
        order = cur.fetchone()[0]
        cur.execute("INSERT INTO templates (id,category,text,image_url,sort_order) VALUES (%s,%s,%s,%s,%s)",
                    (tid, category, text, image_url, order))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"ok":True,"id":tid})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/templates/<tid>", methods=["PUT"])
def api_update_template(tid):
    ok, _ = auth_required()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    category = data.get("category","").strip()
    text = data.get("text","").strip()
    image_url = data.get("image_url","").strip()
    if not category: return jsonify({"error":"category required"}), 400
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("UPDATE templates SET category=%s,text=%s,image_url=%s WHERE id=%s",
                    (category, text, image_url, tid))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/templates/<tid>", methods=["DELETE"])
def api_delete_template(tid):
    ok, _ = auth_required()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("DELETE FROM templates WHERE id=%s", (tid,))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ── 快速圖庫 API ──────────────────────────────────────────

@app.route("/api/quick-images", methods=["GET"])
def api_quick_images():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    if not SUPABASE_SERVICE_KEY:
        return jsonify([])
    try:
        list_url = f"{SUPABASE_URL}/storage/v1/object/list/{SUPABASE_BUCKET}"
        req = urllib.request.Request(
            list_url,
            data=json.dumps({"prefix": "library/", "limit": 200}).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            files = json.loads(r.read())
        result = []
        for f in files:
            name = f.get("name", "")
            if not name:
                continue
            url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/library/{name}"
            result.append({"name": name, "url": url})
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/save-quick-image", methods=["POST"])
def api_save_quick_image():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "")
    content_b64 = data.get("content", "")
    if not filename or not content_b64:
        return jsonify({"error": "missing params"}), 400
    import re as _re
    safe = _re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    img_data = base64.b64decode(content_b64)
    ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else "jpg"
    ct = "image/jpeg" if ext in ("jpg","jpeg") else f"image/{ext}"
    url, err = upload_image_to_supabase(f"library/{safe}", img_data, ct)
    if not url:
        return jsonify({"error": err}), 500
    return jsonify({"ok": True, "url": url, "name": safe})

@app.route("/api/delete-quick-image", methods=["POST"])
def api_delete_quick_image():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    if not name or not SUPABASE_SERVICE_KEY:
        return jsonify({"error": "missing params"}), 400
    try:
        del_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}"
        req = urllib.request.Request(
            del_url,
            data=json.dumps({"prefixes": [f"library/{name}"]}).encode(),
            method="DELETE",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ═══════════════════════════════════════════════════════════
# AI 商品搬運中心 — Phase 1
# ═══════════════════════════════════════════════════════════

# ── DB helpers ───────────────────────────────────────────────

def _pj_insert(url, platform):
    if not DATABASE_URL:
        return None
    try:
        with _db_lock:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute(
                "INSERT INTO product_jobs (url,platform,status,created_at,updated_at) VALUES (%s,%s,'pending',%s,%s) RETURNING id",
                (url, platform, time.time(), time.time())
            )
            job_id = cur.fetchone()[0]
            conn.commit(); cur.close(); conn.close()
            return job_id
    except Exception as e:
        import sys; print(f"[PJ Insert] {e}", file=sys.stderr)
        return None

def _pj_update(job_id, **fields):
    if not DATABASE_URL or not fields:
        return
    try:
        fields["updated_at"] = time.time()
        set_clause = ", ".join(f"{k}=%s" for k in fields)
        with _db_lock:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute(f"UPDATE product_jobs SET {set_clause} WHERE id=%s", list(fields.values()) + [job_id])
            conn.commit(); cur.close(); conn.close()
    except Exception as e:
        import sys; print(f"[PJ Update] {e}", file=sys.stderr)

def _pj_list(limit=50):
    if not DATABASE_URL:
        return []
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id,url,platform,status,raw_title,ai_name,ai_desc,ai_keywords,error_msg,created_at FROM product_jobs ORDER BY created_at DESC LIMIT %s",
            (limit,)
        )
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id":r[0],"url":r[1],"platform":r[2],"status":r[3],"raw_title":r[4],"ai_name":r[5],"ai_desc":r[6],"ai_keywords":r[7],"error_msg":r[8],"created_at":r[9]} for r in rows]
    except Exception:
        return []

def _pj_get(job_id):
    if not DATABASE_URL:
        return None
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id,url,platform,status,raw_title,raw_desc,raw_images,raw_price,ai_name,ai_desc,ai_keywords,error_msg,created_at,processed_images,img_status FROM product_jobs WHERE id=%s",
            (job_id,)
        )
        row = cur.fetchone(); cur.close(); conn.close()
        if not row:
            return None
        return {"id":row[0],"url":row[1],"platform":row[2],"status":row[3],"raw_title":row[4],"raw_desc":row[5],"raw_images":json.loads(row[6] or "[]"),"raw_price":row[7],"ai_name":row[8],"ai_desc":row[9],"ai_keywords":row[10],"error_msg":row[11],"created_at":row[12],"processed_images":json.loads(row[13] or "[]"),"img_status":row[14] or ""}
    except Exception:
        return None

def _pj_delete(job_id):
    if not DATABASE_URL:
        return
    try:
        with _db_lock:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute("DELETE FROM product_jobs WHERE id=%s", (job_id,))
            conn.commit(); cur.close(); conn.close()
    except Exception:
        pass

# ── 圖片處理工具 ─────────────────────────────────────────────

def _clean_images(urls):
    """去重、補 https:、過濾 icon/小圖，最多保留 10 張。"""
    import re
    seen = set()
    result = []
    skip_patterns = re.compile(r'icon|logo|_\d{2}x\d{2}[_.]|_30x|_50x|_60x|_80x|\.ico$', re.IGNORECASE)
    priority_patterns = re.compile(r'_800x|_600x|_790x|_750x|_700x|_[89]\d{2}x|imgextra|mainimg', re.IGNORECASE)

    cleaned = []
    for u in urls:
        u = u.strip()
        if not u:
            continue
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith("http"):
            continue
        if skip_patterns.search(u):
            continue
        if u not in seen:
            seen.add(u)
            cleaned.append(u)

    # 優先排高解析度
    priority = [u for u in cleaned if priority_patterns.search(u)]
    others   = [u for u in cleaned if not priority_patterns.search(u)]
    return (priority + others)[:10]

# ── 圖片爬取（三層 fallback）────────────────────────────────

def _extract_meta_img(html):
    """第一層：og:image, twitter:image"""
    import re
    results = []
    for prop in ("og:image", "twitter:image"):
        for pat in [
            rf'<meta[^>]+(?:property|name)=["\']?{re.escape(prop)}["\']?[^>]+content=["\']([^"\']+)["\']',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']?{re.escape(prop)}["\']?',
        ]:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                results.append(m.group(1).strip())
                break
    return results

def _extract_alicdn_imgs(html):
    """第二層：regex 掃 alicdn.com 系列圖片 URL"""
    import re
    pattern = re.compile(
        r'(?:https?:)?//[^"\'<>\s]*?\.alicdn\.com/[^"\'<>\s]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\'<>\s]*)?',
        re.IGNORECASE
    )
    return list(pattern.findall(html))

def _scrape_images(html):
    """執行三層 fallback，回傳清理後的圖片 URL 列表。"""
    urls = []
    urls += _extract_meta_img(html)          # 第一層
    urls += _extract_alicdn_imgs(html)        # 第二層
    return _clean_images(urls)

# ── 爬取函數 ─────────────────────────────────────────────────

def _detect_platform(url):
    if "1688.com" in url:
        return "1688"
    if "taobao.com" in url:
        return "taobao"
    return "unknown"

def _extract_meta_text(html, prop):
    import re
    for pat in [
        rf'<meta[^>]+(?:property|name)=["\']?{re.escape(prop)}["\']?[^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']?{re.escape(prop)}["\']?',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""

def _extract_title_tag(html):
    import re
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    return m.group(1).strip() if m else ""

def _fetch_html(url):
    """GET 頁面 HTML，帶 browser-like headers。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.google.com/",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        charset = "utf-8"
        ct = r.headers.get("Content-Type", "")
        if "charset=" in ct:
            charset = ct.split("charset=")[-1].strip().split(";")[0].strip()
        return r.read().decode(charset, errors="ignore")

def _scrape_product(url, platform):
    """爬取 1688 或 淘寶 商品頁面，回傳原始資料 dict。"""
    import re
    result = {"title": "", "desc": "", "images": [], "price": "", "error": ""}
    try:
        html = _fetch_html(url)

        # 標題
        result["title"] = (
            _extract_meta_text(html, "og:title")
            or _extract_title_tag(html)
        )
        # 移除標題末尾的網站名稱（常見於淘寶/1688）
        for suffix in ["-1688.com", "- 1688", "- 淘寶網", "-淘寶網", "- Taobao", "_淘寶搜索"]:
            if result["title"].endswith(suffix):
                result["title"] = result["title"][:-len(suffix)].strip()

        # 描述
        result["desc"] = _extract_meta_text(html, "og:description") or _extract_meta_text(html, "description")

        # 圖片
        result["images"] = _scrape_images(html)

        # 價格（簡單 regex）
        price_m = re.search(r'["\']price["\']\s*:\s*["\']?([\d.,]+)["\']?', html)
        if price_m:
            result["price"] = price_m.group(1)

    except Exception as e:
        result["error"] = str(e)
    return result

# ── Claude AI 改寫 ────────────────────────────────────────────

def _ai_rewrite(raw_title, raw_desc, price=""):
    """呼叫 Claude API，改寫成官網專業風格。回傳 dict。"""
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY 未設定"}

    parts = []
    if raw_title:
        parts.append(f"原始標題：{raw_title}")
    if price:
        parts.append(f"參考價格：{price}")
    if raw_desc:
        parts.append(f"原始描述：{raw_desc[:1500]}")

    prompt = f"""你是台灣家具電商品牌的文案編輯，請將以下中國電商商品資料改寫成台灣官網風格。

{chr(10).join(parts)}

輸出格式（只輸出 JSON，不要其他文字）：
{{
  "name": "商品名稱（簡潔、專業、官網感，30字以內，繁體中文，不要堆砌關鍵字）",
  "desc": "商品描述（200-400字，條列式，繁體中文，口語但不隨便，用具體數字，不要感嘆號堆疊，不要：喔、恩、那個、就是說、其實、基本上、保證、一定、絕對）",
  "keywords": "關鍵字1,關鍵字2,關鍵字3,關鍵字4,關鍵字5"
}}"""

    try:
        req_data = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=req_data,
            method="POST",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode())
        text = resp["content"][0]["text"].strip()
        import re
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            result = json.loads(m.group())
            return {"name": result.get("name",""), "desc": result.get("desc",""), "keywords": result.get("keywords","")}
        return {"error": f"AI 回傳格式錯誤: {text[:200]}"}
    except Exception as e:
        return {"error": str(e)}

# ── 背景任務處理 ──────────────────────────────────────────────

def _process_product_job(job_id, url, platform):
    """背景 thread：爬取 → AI 改寫 → 寫回 DB。"""
    _pj_update(job_id, status="scraping")
    scraped = _scrape_product(url, platform)

    if scraped.get("error") and not scraped.get("title"):
        _pj_update(job_id, status="error", error_msg=f"爬取失敗：{scraped['error']}")
        return

    raw_title  = scraped.get("title", "")
    raw_desc   = scraped.get("desc", "")
    raw_images = scraped.get("images", [])
    raw_price  = scraped.get("price", "")

    _pj_update(job_id,
        status="rewriting",
        raw_title=raw_title,
        raw_desc=raw_desc,
        raw_images=json.dumps(raw_images, ensure_ascii=False),
        raw_price=raw_price,
    )

    if not raw_title and not raw_desc:
        _pj_update(job_id, status="error", error_msg="無法取得商品資料（頁面可能需要登入）")
        return

    ai = _ai_rewrite(raw_title, raw_desc, raw_price)
    if "error" in ai:
        _pj_update(job_id, status="error", error_msg=f"AI 改寫失敗：{ai['error']}")
        return

    _pj_update(job_id, status="done", ai_name=ai.get("name",""), ai_desc=ai.get("desc",""), ai_keywords=ai.get("keywords",""))

# ── 圖片處理（Phase 2A）────────────────────────────────────────

def _download_image(url):
    """下載圖片，回傳 bytes 或 None。"""
    try:
        if url.startswith("//"):
            url = "https:" + url
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.1688.com/",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except Exception:
        return None

def _process_to_white_bg(img_bytes, size=800):
    """將圖片加白底、正方形裁切、壓縮。回傳 JPEG bytes 或 None。"""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        # 建立白色底圖
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
        bg = bg.convert("RGB")
        # 等比例縮放到 size x size
        bg.thumbnail((size, size), Image.LANCZOS)
        canvas = Image.new("RGB", (size, size), (255, 255, 255))
        offset = ((size - bg.width) // 2, (size - bg.height) // 2)
        canvas.paste(bg, offset)
        out = io.BytesIO()
        canvas.save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue()
    except Exception:
        return None

def _process_images_for_job(job_id):
    """背景 thread：下載 raw_images → 白底處理 → 上傳 Supabase → 存 processed_images。"""
    job = _pj_get(job_id)
    if not job:
        return
    raw_imgs = job.get("raw_images", [])
    if not raw_imgs:
        _pj_update(job_id, img_status="no_images")
        return

    _pj_update(job_id, img_status="processing")
    processed = []
    for i, url in enumerate(raw_imgs[:10]):
        img_bytes = _download_image(url)
        if not img_bytes:
            continue
        result = _process_to_white_bg(img_bytes)
        if not result:
            continue
        filename = f"products/{job_id}_{i+1}.jpg"
        pub_url, err = upload_image_to_supabase(filename, result, "image/jpeg")
        if pub_url:
            processed.append(pub_url)

    _pj_update(job_id,
        processed_images=json.dumps(processed, ensure_ascii=False),
        img_status="done" if processed else "failed"
    )

# ── HTML 模板 ─────────────────────────────────────────────────

PRODUCTS_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 商品搬運中心</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
.header{background:#1a1a1a;color:#fff;padding:14px 20px;display:flex;align-items:center;gap:14px}
.header a{color:#888;text-decoration:none;font-size:14px;flex-shrink:0}
.header a:hover{color:#fff}
.header-title{font-size:17px;font-weight:700;flex:1}
.header-status{font-size:12px;color:#666}
.wrap{max-width:800px;margin:0 auto;padding:20px 16px}
/* input card */
.card{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.card h3{font-size:14px;font-weight:700;color:#666;margin-bottom:12px;letter-spacing:.3px}
.url-row{display:flex;gap:10px}
.url-row input{flex:1;border:1.5px solid #ddd;border-radius:10px;padding:10px 14px;font-size:14px;outline:none;font-family:-apple-system,sans-serif}
.url-row input:focus{border-color:#1a1a1a}
.btn-primary{background:#1a1a1a;color:#fff;border:none;border-radius:10px;padding:10px 22px;font-size:14px;font-weight:700;cursor:pointer;white-space:nowrap;font-family:-apple-system,sans-serif}
.btn-primary:hover{background:#333}
.btn-primary:disabled{background:#aaa;cursor:default}
.err-msg{color:#c62828;font-size:13px;margin-top:8px;display:none}
/* filter */
.filter-row{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.fbtn{border:1.5px solid #ddd;background:#fff;border-radius:20px;padding:5px 14px;font-size:13px;cursor:pointer;font-family:-apple-system,sans-serif}
.fbtn.active{background:#1a1a1a;color:#fff;border-color:#1a1a1a}
/* job cards */
.job{background:#fff;border-radius:12px;padding:15px 16px;margin-bottom:9px;box-shadow:0 1px 3px rgba(0,0,0,.07);cursor:pointer;border:1.5px solid transparent;transition:all .15s}
.job:hover{border-color:#e0e0e0;box-shadow:0 3px 10px rgba(0,0,0,.1)}
.job-top{display:flex;align-items:center;gap:8px}
.badge{font-size:11px;font-weight:700;padding:3px 8px;border-radius:8px;flex-shrink:0}
.pf-1688{background:#fff0f0;color:#c62828}
.pf-taobao{background:#fff4e5;color:#e65100}
.st-pending{background:#f5f5f5;color:#999}
.st-scraping{background:#e3f2fd;color:#1565c0}
.st-rewriting{background:#fff8e1;color:#f57f17}
.st-done{background:#e8f5e9;color:#2e7d32}
.st-error{background:#fdecea;color:#c62828}
.job-title{flex:1;font-size:14px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.job-url{font-size:12px;color:#bbb;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:4px}
.job-time{font-size:11px;color:#ccc;margin-top:3px}
.del-btn{background:none;border:none;color:#ddd;cursor:pointer;font-size:18px;line-height:1;padding:2px 4px;border-radius:6px;flex-shrink:0}
.del-btn:hover{color:#e53935;background:#fdecea}
.empty{text-align:center;padding:50px 20px;color:#ccc;font-size:15px}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid #ddd;border-top-color:#666;border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle;margin-right:4px}
@keyframes spin{to{transform:rotate(360deg)}}
/* modal */
.backdrop{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:100;display:flex;align-items:center;justify-content:center;padding:16px}
.modal{background:#fff;border-radius:16px;width:100%;max-width:660px;max-height:90vh;overflow-y:auto;box-shadow:0 8px 40px rgba(0,0,0,.2)}
.modal-hd{padding:18px 20px;border-bottom:1px solid #f0f0f0;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;background:#fff;z-index:1}
.modal-title{font-size:16px;font-weight:700}
.close-btn{background:none;border:none;font-size:24px;cursor:pointer;color:#bbb;line-height:1;padding:0 2px}
.modal-body{padding:20px}
.section{margin-bottom:20px}
.slabel{font-size:11px;font-weight:700;color:#aaa;letter-spacing:.5px;text-transform:uppercase;margin-bottom:6px}
.rbox{background:#f8f8f8;border-radius:10px;padding:14px;font-size:14px;line-height:1.7;white-space:pre-wrap;word-break:break-all;position:relative;padding-right:70px}
.copy-btn{position:absolute;top:8px;right:8px;background:#1a1a1a;color:#fff;border:none;border-radius:8px;padding:4px 10px;font-size:12px;cursor:pointer;opacity:.7;font-family:-apple-system,sans-serif}
.copy-btn:hover{opacity:1}
.copy-btn.ok{background:#2e7d32}
.kw-row{display:flex;gap:6px;flex-wrap:wrap}
.kw{background:#e3f2fd;color:#1565c0;border-radius:12px;padding:4px 12px;font-size:13px;font-weight:600}
.imgs-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.imgs-row img{width:80px;height:80px;object-fit:cover;border-radius:8px;border:1px solid #eee}
.divider{border:none;border-top:1px solid #f0f0f0;margin:16px 0}
.err-box{background:#fdecea;color:#c62828;border-radius:10px;padding:12px 14px;font-size:13px;margin-bottom:14px}
.raw-toggle{background:none;border:none;color:#bbb;font-size:12px;cursor:pointer;font-family:-apple-system,sans-serif;text-decoration:underline;padding:0}
/* 手動補圖 */
.img-form{margin-top:14px}
.img-form textarea{width:100%;border:1.5px solid #ddd;border-radius:10px;padding:10px;font-size:13px;height:90px;resize:vertical;font-family:-apple-system,sans-serif;outline:none}
.img-form textarea:focus{border-color:#1a1a1a}
.btn-sm{background:#333;color:#fff;border:none;border-radius:8px;padding:6px 14px;font-size:13px;cursor:pointer;font-family:-apple-system,sans-serif;margin-top:6px}
.btn-sm:hover{background:#555}
.hidden{display:none}
.toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%);background:#333;color:#fff;padding:9px 20px;border-radius:20px;font-size:14px;z-index:999;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}
/* 編輯模式 */
.edit-ta{width:100%;border:1.5px solid #ddd;border-radius:10px;padding:10px 12px;font-size:14px;line-height:1.6;resize:vertical;font-family:-apple-system,sans-serif;outline:none;background:#fff}
.edit-ta:focus{border-color:#1a1a1a}
.edit-ta.name{height:52px}
.edit-ta.desc{height:200px}
.edit-ta.kw{height:52px}
.edit-bar{display:flex;gap:8px;margin-top:10px}
.btn-save{background:#1a1a1a;color:#fff;border:none;border-radius:9px;padding:8px 18px;font-size:13px;font-weight:700;cursor:pointer;font-family:-apple-system,sans-serif}
.btn-save:hover{background:#333}
.btn-cancel{background:#f5f5f5;color:#555;border:none;border-radius:9px;padding:8px 16px;font-size:13px;cursor:pointer;font-family:-apple-system,sans-serif}
.btn-cancel:hover{background:#eee}
.btn-edit{background:#f0f0f0;color:#333;border:none;border-radius:9px;padding:6px 14px;font-size:13px;cursor:pointer;font-family:-apple-system,sans-serif;margin-left:auto}
.btn-edit:hover{background:#e0e0e0}
/* 批次輸入 */
.url-ta{width:100%;border:1.5px solid #ddd;border-radius:10px;padding:10px 14px;font-size:13px;height:90px;resize:vertical;font-family:-apple-system,sans-serif;outline:none}
.url-ta:focus{border-color:#1a1a1a}
.batch-hint{font-size:12px;color:#aaa;margin-top:6px}
.progress-bar-wrap{background:#f0f0f0;border-radius:10px;height:6px;margin-top:10px;overflow:hidden;display:none}
.progress-bar{background:#1a1a1a;height:100%;border-radius:10px;transition:width .3s}
</style>
</head>
<body>

<div class="header">
  <a href="/admin?key={{ key }}">← 返回</a>
  <div class="header-title">AI 商品搬運中心</div>
  <div class="header-status" id="hStatus"></div>
</div>

<div class="wrap">
  <div class="card">
    <h3>貼上商品連結（一行一個，可多筆）</h3>
    <textarea class="url-ta" id="urlInput" placeholder="https://detail.1688.com/offer/xxx.html&#10;https://item.taobao.com/item.htm?id=xxx&#10;（一行一個連結）"></textarea>
    <div class="batch-hint" id="batchHint"></div>
    <div class="progress-bar-wrap" id="progressWrap"><div class="progress-bar" id="progressBar" style="width:0%"></div></div>
    <div style="margin-top:10px;display:flex;gap:10px;align-items:center">
      <button class="btn-primary" id="addBtn" onclick="submitUrl()">開始搬運</button>
      <div class="err-msg" id="addErr" style="margin:0"></div>
    </div>
  </div>

  <div class="filter-row">
    <button class="fbtn active" onclick="setFilter('all',this)">全部</button>
    <button class="fbtn" onclick="setFilter('done',this)">完成</button>
    <button class="fbtn" onclick="setFilter('error',this)">失敗</button>
    <button class="fbtn" onclick="setFilter('processing',this)">進行中</button>
  </div>

  <div id="jobList"></div>
  <div id="emptyMsg" class="empty hidden">還沒有任務，貼上連結開始搬運</div>
</div>

<!-- Detail Modal -->
<div class="backdrop hidden" id="modal" onclick="bgClose(event)">
  <div class="modal" id="modalBox">
    <div class="modal-hd">
      <div class="modal-title" id="modalTitle">商品詳情</div>
      <button class="close-btn" onclick="closeModal()">×</button>
    </div>
    <div class="modal-body" id="modalBody"></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const KEY = "{{ key }}";
let jobs = [], filter = "all", pollTimer = null, openId = null;

const api = (url, opts={}) => fetch(url+(url.includes("?")?"&":"?")+"key="+KEY, {headers:{"Content-Type":"application/json"},...opts}).then(r=>r.json());

async function loadJobs(){
  try{
    const d = await api("/api/products");
    jobs = d.jobs||[];
    render();
    schedulePoll();
  }catch(e){console.error(e)}
}

function schedulePoll(){
  clearTimeout(pollTimer);
  const active = jobs.some(j=>["pending","scraping","rewriting"].includes(j.status));
  document.getElementById("hStatus").textContent = active ? "更新中..." : "";
  if(active) pollTimer = setTimeout(loadJobs, 2500);
}

function render(){
  const list = document.getElementById("jobList");
  const empty = document.getElementById("emptyMsg");
  const filtered = jobs.filter(j=>{
    if(filter==="all") return true;
    if(filter==="processing") return ["pending","scraping","rewriting"].includes(j.status);
    return j.status===filter;
  });
  if(!filtered.length){ list.innerHTML=""; empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  list.innerHTML = filtered.map(jobCard).join("");
}

function jobCard(j){
  const pfLabel = {1688:"1688",taobao:"淘寶"}[j.platform]||j.platform;
  const stLabel = {pending:"等待中",scraping:"爬取中",rewriting:"改寫中",done:"完成",error:"失敗"}[j.status]||j.status;
  const isActive = ["pending","scraping","rewriting"].includes(j.status);
  const spin = isActive ? '<span class="spinner"></span>' : "";
  const title = esc(j.ai_name||j.raw_title||j.url);
  const urlShort = j.url.length>65 ? j.url.slice(0,65)+"…" : j.url;
  const ts = j.created_at ? new Date(j.created_at*1000).toLocaleString("zh-TW",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}) : "";
  return `<div class="job" onclick="openJob(${j.id})">
    <div class="job-top">
      <span class="badge pf-${j.platform}">${pfLabel}</span>
      <span class="badge st-${j.status}">${spin}${stLabel}</span>
      <div class="job-title">${title}</div>
      <button class="del-btn" onclick="delJob(event,${j.id})" title="刪除">×</button>
    </div>
    <div class="job-url">${esc(urlShort)}</div>
    ${ts?`<div class="job-time">${ts}</div>`:""}
  </div>`;
}

function setFilter(f,btn){
  filter=f;
  document.querySelectorAll(".fbtn").forEach(b=>b.classList.remove("active"));
  btn.classList.add("active");
  render();
}

async function submitUrl(){
  const ta=document.getElementById("urlInput");
  const errEl=document.getElementById("addErr");
  const btn=document.getElementById("addBtn");
  const hint=document.getElementById("batchHint");
  const pWrap=document.getElementById("progressWrap");
  const pBar=document.getElementById("progressBar");
  errEl.style.display="none";
  const urls=ta.value.split("\\n").map(s=>s.trim()).filter(Boolean);
  if(!urls.length){errEl.textContent="請貼上商品連結";errEl.style.display="block";return;}
  btn.disabled=true;
  let done=0,failed=0;
  pWrap.style.display="block";
  for(const url of urls){
    btn.textContent=`搬運中 ${done+failed+1}/${urls.length}...`;
    hint.textContent=`已提交 ${done} 筆${failed?`，失敗 ${failed} 筆`:""}`;
    pBar.style.width=((done+failed)/urls.length*100)+"%";
    try{
      const r=await api("/api/products",{method:"POST",body:JSON.stringify({url})});
      if(r.error) failed++;
      else done++;
    }catch(e){failed++;}
  }
  pBar.style.width="100%";
  hint.textContent=`完成：${done} 筆${failed?`，失敗：${failed} 筆`:""}`;
  if(done>0) ta.value="";
  btn.disabled=false; btn.textContent="開始搬運";
  loadJobs();
  setTimeout(()=>{pWrap.style.display="none"; hint.textContent="";},4000);
}

async function openJob(id){
  openId=id;
  document.getElementById("modal").classList.remove("hidden");
  document.getElementById("modalBody").innerHTML='<div style="text-align:center;padding:40px"><span class="spinner" style="width:18px;height:18px"></span></div>';
  try{
    const j=await api("/api/products/"+id);
    renderModal(j);
  }catch(e){document.getElementById("modalBody").innerHTML='<div class="err-box">載入失敗</div>';}
}

function renderModal(j, editMode=false){
  document.getElementById("modalTitle").textContent = j.ai_name||j.raw_title||"商品詳情";
  let h="";
  if(j.status==="error"){
    h+=`<div class="err-box">${esc(j.error_msg||"未知錯誤")}</div>`;
    if(j.raw_title) h+=`<div class="section"><div class="slabel">原始標題</div><div class="rbox">${esc(j.raw_title)}</div></div>`;
    h+=imgFormHtml(j.id,[]);
    document.getElementById("modalBody").innerHTML=h; return;
  }
  if(["pending","scraping","rewriting"].includes(j.status)){
    const msg={pending:"等待爬取...",scraping:"正在爬取商品資料...",rewriting:"AI 正在改寫文案，請稍候..."};
    h+=`<div style="text-align:center;padding:30px;color:#888"><span class="spinner" style="width:16px;height:16px;border-top-color:#555"></span> ${msg[j.status]}</div>`;
    document.getElementById("modalBody").innerHTML=h;
    if(openId===j.id) setTimeout(()=>{if(openId===j.id)openJob(j.id);},2000);
    return;
  }
  // done — 編輯模式 / 檢視模式
  if(editMode){
    h+=`<div class="section"><div class="slabel">商品名稱</div><textarea class="edit-ta name" id="edit_name">${esc(j.ai_name||"")}</textarea></div>`;
    h+=`<div class="section"><div class="slabel">SEO 關鍵字（逗號分隔）</div><textarea class="edit-ta kw" id="edit_kw">${esc(j.ai_keywords||"")}</textarea></div>`;
    h+=`<div class="section"><div class="slabel">商品描述</div><textarea class="edit-ta desc" id="edit_desc">${esc(j.ai_desc||"")}</textarea></div>`;
    h+=`<div class="edit-bar"><button class="btn-save" onclick="saveEdit(${j.id})">儲存</button><button class="btn-cancel" onclick="openJob(${j.id})">取消</button></div>`;
  } else {
    const editBtn=`<button class="btn-edit" onclick="enterEdit(${j.id})">編輯文案</button>`;
    if(j.ai_name) h+=`<div class="section"><div class="slabel" style="display:flex;align-items:center">商品名稱 ${editBtn}</div><div class="rbox">${esc(j.ai_name)}<button class="copy-btn" onclick='cp(this,${JSON.stringify(j.ai_name)})'>複製</button></div></div>`;
    if(j.ai_keywords){
      const kws=j.ai_keywords.split(",").map(s=>s.trim()).filter(Boolean);
      h+=`<div class="section"><div class="slabel">SEO 關鍵字</div><div class="kw-row">${kws.map(k=>`<span class="kw">${esc(k)}</span>`).join("")}</div><div style="margin-top:8px"><button class="btn-sm" onclick='cp(this,${JSON.stringify(j.ai_keywords)})'>複製全部</button></div></div>`;
    }
    if(j.ai_desc) h+=`<div class="section"><div class="slabel">商品描述</div><div class="rbox" style="max-height:320px;overflow-y:auto">${esc(j.ai_desc)}<button class="copy-btn" onclick='cp(this,${JSON.stringify(j.ai_desc)})'>複製</button></div></div>`;
  }
  if(j.raw_images&&j.raw_images.length){
    const imgStatusMap={"processing":"處理中...","done":"處理完成","failed":"處理失敗","no_images":"無圖片"};
    const imgStatusLabel=imgStatusMap[j.img_status]||"";
    const processBtn=j.img_status==="processing"
      ? `<span style="font-size:12px;color:#888"><span class="spinner"></span>${imgStatusLabel}</span>`
      : `<button class="btn-sm" onclick="processImgs(${j.id})" style="margin-left:8px">白底處理</button>${imgStatusLabel?`<span style="font-size:12px;color:#888;margin-left:6px">${imgStatusLabel}</span>`:""}`;
    h+=`<div class="section"><div class="slabel" style="display:flex;align-items:center;gap:6px">原始圖片（${j.raw_images.length} 張）${processBtn}</div><div class="imgs-row">${j.raw_images.map(img=>`<img src="${esc(img)}" loading="lazy" onerror="this.style.display='none'">`).join("")}</div></div>`;
  }
  if(j.processed_images&&j.processed_images.length){
    h+=`<div class="section"><div class="slabel">已處理圖片（白底，${j.processed_images.length} 張）</div><div class="imgs-row">${j.processed_images.map(img=>`<a href="${esc(img)}" target="_blank"><img src="${esc(img)}" loading="lazy" onerror="this.style.display='none'"></a>`).join("")}</div></div>`;
  }
  h+=`<hr class="divider">`;
  h+=imgFormHtml(j.id, j.raw_images||[]);
  h+=`<br><button class="raw-toggle" onclick="toggleRaw(this)">顯示原始資料 ▾</button>`;
  h+=`<div id="rawSec" class="hidden" style="margin-top:12px">`;
  if(j.raw_title) h+=`<div class="section"><div class="slabel">原始標題</div><div class="rbox">${esc(j.raw_title)}</div></div>`;
  if(j.raw_price) h+=`<div class="section"><div class="slabel">原始價格</div><div class="rbox">${esc(j.raw_price)}</div></div>`;
  if(j.raw_desc)  h+=`<div class="section"><div class="slabel">原始描述</div><div class="rbox" style="max-height:180px;overflow-y:auto">${esc(j.raw_desc.slice(0,1200))}${j.raw_desc.length>1200?"…":""}</div></div>`;
  h+=`</div>`;
  document.getElementById("modalBody").innerHTML=h;
}

async function enterEdit(id){
  const j=await api("/api/products/"+id);
  renderModal(j, true);
}

async function saveEdit(id){
  const name=document.getElementById("edit_name").value.trim();
  const kw=document.getElementById("edit_kw").value.trim();
  const desc=document.getElementById("edit_desc").value.trim();
  const r=await api("/api/products/"+id,{method:"PUT",body:JSON.stringify({ai_name:name,ai_keywords:kw,ai_desc:desc})});
  if(r.ok){toast("已儲存");openJob(id);}
  else toast("儲存失敗："+r.error);
}

function imgFormHtml(id, existing){
  return `<div class="section img-form">
    <div class="slabel">手動補圖片 URL（第三層，一行一張）</div>
    <textarea id="imgTa" placeholder="https://img.alicdn.com/...">${existing.join("\\n")}</textarea>
    <button class="btn-sm" onclick="saveImgs(${id})">儲存圖片</button>
  </div>`;
}

async function saveImgs(id){
  const ta=document.getElementById("imgTa");
  const urls=ta.value.split("\\n").map(s=>s.trim()).filter(Boolean);
  const r=await api("/api/products/"+id+"/images",{method:"POST",body:JSON.stringify({urls})});
  if(r.ok) toast("圖片已儲存");
  else toast("儲存失敗："+r.error);
}

async function processImgs(id){
  const r=await api("/api/products/"+id+"/process-images",{method:"POST",body:"{}"});
  if(r.ok){toast("圖片處理開始，請稍候...");setTimeout(()=>openJob(id),3000);}
  else toast("失敗："+r.error);
}

function toggleRaw(btn){
  const el=document.getElementById("rawSec");
  if(el.classList.contains("hidden")){el.classList.remove("hidden");btn.textContent="隱藏原始資料 ▴";}
  else{el.classList.add("hidden");btn.textContent="顯示原始資料 ▾";}
}

function bgClose(e){if(e.target===document.getElementById("modal"))closeModal();}
function closeModal(){document.getElementById("modal").classList.add("hidden");openId=null;}

async function delJob(e,id){
  e.stopPropagation();
  if(!confirm("確定刪除這筆任務？"))return;
  await api("/api/products/"+id,{method:"DELETE"});
  jobs=jobs.filter(j=>j.id!==id);
  render();
  toast("已刪除");
}

function cp(btn,text){
  navigator.clipboard.writeText(text).then(()=>{
    const orig=btn.textContent;
    btn.textContent="已複製";btn.classList.add("ok");
    setTimeout(()=>{btn.textContent=orig;btn.classList.remove("ok");},1500);
    toast("已複製");
  });
}
function toast(msg){
  const t=document.getElementById("toast");
  t.textContent=msg;t.classList.add("show");
  setTimeout(()=>t.classList.remove("show"),2000);
}
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}

document.getElementById("urlInput").addEventListener("keydown",e=>{if(e.key==="Enter")submitUrl();});
loadJobs();
</script>
</body></html>"""

# ── 後台路由 ──────────────────────────────────────────────────

@app.route("/admin/products")
def admin_products():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next="/admin/products", error=None)
    return render_template_string(PRODUCTS_HTML, key=key)

# ── API 路由 ──────────────────────────────────────────────────

@app.route("/api/products", methods=["POST"])
def api_products_add():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "請提供商品連結"}), 400
    platform = _detect_platform(url)
    if platform == "unknown":
        return jsonify({"error": "目前只支援 1688 和 淘寶 連結"}), 400
    job_id = _pj_insert(url, platform)
    if not job_id:
        return jsonify({"error": "建立任務失敗，請確認資料庫連線"}), 500
    threading.Thread(target=_process_product_job, args=(job_id, url, platform), daemon=True).start()
    return jsonify({"ok": True, "id": job_id, "platform": platform})

@app.route("/api/products", methods=["GET"])
def api_products_list():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({"jobs": _pj_list(50)})

@app.route("/api/products/<int:job_id>", methods=["GET"])
def api_products_get(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    job = _pj_get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)

@app.route("/api/products/<int:job_id>", methods=["DELETE"])
def api_products_delete(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    _pj_delete(job_id)
    return jsonify({"ok": True})

@app.route("/api/products/<int:job_id>/images", methods=["POST"])
def api_products_images(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])
    cleaned = _clean_images(urls)
    _pj_update(job_id, raw_images=json.dumps(cleaned, ensure_ascii=False))
    return jsonify({"ok": True, "count": len(cleaned)})

@app.route("/api/products/<int:job_id>", methods=["PUT"])
def api_products_update(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    allowed = {"ai_name", "ai_desc", "ai_keywords"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return jsonify({"error": "no valid fields"}), 400
    _pj_update(job_id, **fields)
    return jsonify({"ok": True})

@app.route("/api/products/<int:job_id>/process-images", methods=["POST"])
def api_products_process_images(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    job = _pj_get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    if not job.get("raw_images"):
        return jsonify({"error": "沒有圖片可處理"}), 400
    try:
        from PIL import Image  # 確認 Pillow 可用
    except ImportError:
        return jsonify({"error": "Pillow 未安裝，請在 requirements.txt 加上 Pillow"}), 500
    threading.Thread(target=_process_images_for_job, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
