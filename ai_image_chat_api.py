"""
ai_image_chat_api.py — AI 對話生成 Blueprint

Routes:
    GET  /admin/ai-image-chat
    GET  /api/ai-image-chat/sessions
    POST /api/ai-image-chat/sessions
    GET  /api/ai-image-chat/sessions/<id>/messages
    POST /api/ai-image-chat/message
    POST /api/ai-image-chat/regenerate

Env vars:
    OPENAI_API_KEY, ANTHROPIC_API_KEY, IMAGE_MODEL, IMAGE_QUALITY, DATABASE_URL
"""

import os, io, json, time, base64, sys
import requests as _req
from flask import Blueprint, request, jsonify, render_template_string

from products_api import (
    upload_image_to_supabase,
    _pg_conn,
    check_auth,
    auth_required,
    LOGIN_HTML,
)

ai_image_chat_bp = Blueprint("ai_image_chat", __name__)

# ── Config ─────────────────────────────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
IMAGE_MODEL       = os.getenv("IMAGE_MODEL", "gpt-image-2")
IMAGE_QUALITY     = os.getenv("IMAGE_QUALITY", "high")
_DB               = os.getenv("DATABASE_URL", "")

# ── DB Migration ────────────────────────────────────────────────────────
def _chat_migrate():
    ddl = [
        """CREATE TABLE IF NOT EXISTS ai_image_chat_sessions (
            id         SERIAL PRIMARY KEY,
            title      TEXT DEFAULT '新對話',
            brand_key  TEXT DEFAULT '',
            created_at BIGINT DEFAULT 0,
            updated_at BIGINT DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS ai_image_chat_messages (
            id                         SERIAL PRIMARY KEY,
            session_id                 INTEGER NOT NULL,
            role                       TEXT DEFAULT 'user',
            message_text               TEXT DEFAULT '',
            prompt_text                TEXT DEFAULT '',
            image_urls_json            TEXT DEFAULT '[]',
            reference_image_urls_json  TEXT DEFAULT '[]',
            size                       TEXT DEFAULT '',
            quality                    TEXT DEFAULT '',
            status                     TEXT DEFAULT 'ok',
            created_at                 BIGINT DEFAULT 0
        )""",
    ]
    try:
        conn = _pg_conn(); cur = conn.cursor()
        for sql in ddl:
            cur.execute(sql)
        conn.commit(); cur.close(); conn.close()
        print("[AI_CHAT] DB migration OK", file=sys.stderr)
    except Exception as e:
        print(f"[AI_CHAT] migration error: {e}", file=sys.stderr)


# ── DB helpers ──────────────────────────────────────────────────────────
def _brands_list():
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT brand_key, name, style_keywords, color_style, negative_rules "
            "FROM brand_profiles ORDER BY brand_key"
        )
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"brand_key": r[0], "name": r[1],
                 "style_keywords": r[2] or "", "color_style": r[3] or "",
                 "negative_rules": r[4] or ""} for r in rows]
    except Exception:
        return []


def _get_brand(brand_key):
    if not brand_key:
        return None
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT name, style_keywords, color_style, negative_rules, "
            "prompt_rules, image_style, tone FROM brand_profiles WHERE brand_key=%s LIMIT 1",
            (brand_key,)
        )
        row = cur.fetchone(); cur.close(); conn.close()
        if not row:
            return None
        return dict(zip(["name","style_keywords","color_style","negative_rules",
                          "prompt_rules","image_style","tone"], row))
    except Exception as e:
        print(f"[AI_CHAT] get_brand error: {e}", file=sys.stderr)
        return None


def _brand_context(brand):
    if not brand:
        return ""
    parts = []
    if brand.get("image_style"):   parts.append(f"Image style: {brand['image_style']}")
    if brand.get("style_keywords"):parts.append(f"Style: {brand['style_keywords']}")
    if brand.get("color_style"):   parts.append(f"Colors: {brand['color_style']}")
    if brand.get("tone"):          parts.append(f"Tone: {brand['tone']}")
    if brand.get("negative_rules"):parts.append(f"Avoid: {brand['negative_rules']}")
    if brand.get("prompt_rules"):  parts.append(f"Rules: {brand['prompt_rules']}")
    return "\n".join(parts)


def _get_sessions():
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT id,title,brand_key,updated_at FROM ai_image_chat_sessions ORDER BY updated_at DESC LIMIT 60")
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id":r[0],"title":r[1],"brand_key":r[2],"updated_at":r[3]} for r in rows]
    except Exception as e:
        print(f"[AI_CHAT] get_sessions: {e}", file=sys.stderr)
        return []


def _create_session(title="新對話", brand_key=""):
    now = int(time.time())
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute(
        "INSERT INTO ai_image_chat_sessions (title,brand_key,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s) RETURNING id",
        (title, brand_key, now, now)
    )
    sid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    return sid


def _get_messages(session_id):
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id,role,message_text,prompt_text,image_urls_json,"
            "reference_image_urls_json,size,quality,status,created_at "
            "FROM ai_image_chat_messages WHERE session_id=%s ORDER BY created_at ASC,id ASC",
            (session_id,)
        )
        rows = cur.fetchall(); cur.close(); conn.close()
        keys = ["id","role","message_text","prompt_text","image_urls_json",
                "reference_image_urls_json","size","quality","status","created_at"]
        result = []
        for row in rows:
            m = dict(zip(keys, row))
            m["image_urls"]           = json.loads(m.pop("image_urls_json") or "[]")
            m["reference_image_urls"] = json.loads(m.pop("reference_image_urls_json") or "[]")
            result.append(m)
        return result
    except Exception as e:
        print(f"[AI_CHAT] get_messages: {e}", file=sys.stderr)
        return []


def _save_msg(session_id, role, text, prompt="", image_urls=None,
              ref_urls=None, size="", quality="", status="ok"):
    now = int(time.time())
    conn = _pg_conn(); cur = conn.cursor()
    cur.execute(
        "INSERT INTO ai_image_chat_messages "
        "(session_id,role,message_text,prompt_text,image_urls_json,"
        "reference_image_urls_json,size,quality,status,created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (session_id, role, text, prompt,
         json.dumps(image_urls or []), json.dumps(ref_urls or []),
         size, quality, status, now)
    )
    mid = cur.fetchone()[0]
    cur.execute("UPDATE ai_image_chat_sessions SET updated_at=%s WHERE id=%s", (now, session_id))
    conn.commit(); cur.close(); conn.close()
    return mid


# ── AI helpers ──────────────────────────────────────────────────────────
def _enhance_prompt(user_text, brand_ctx="", prev_user_msgs=None):
    """Claude Haiku turns casual Chinese into an image prompt."""
    if not ANTHROPIC_API_KEY:
        return (f"{brand_ctx}\n\n{user_text}" if brand_ctx else user_text)

    ctx = ""
    if prev_user_msgs:
        recent = prev_user_msgs[-3:]
        ctx = "Previous requests in this conversation:\n" + "\n".join(f"- {m}" for m in recent) + "\n\n"
    user_block = ctx
    if brand_ctx:
        user_block += f"Brand context:\n{brand_ctx}\n\n"
    user_block += f"User request: {user_text}\n\nOutput a detailed English image generation prompt:"

    try:
        r = _req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 600,
                "system": (
                    "You are a professional image prompt writer for gpt-image-2. "
                    "Given a Chinese user request, output ONLY a concise, vivid English "
                    "image generation prompt. No explanation, no quotes."
                ),
                "messages": [{"role": "user", "content": user_block}],
            },
            timeout=30,
        )
        if r.status_code == 200:
            return r.json()["content"][0]["text"].strip()
        print(f"[AI_CHAT] Claude {r.status_code}: {r.text[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"[AI_CHAT] Claude error: {e}", file=sys.stderr)
    return user_text


def _generate_images(prompt, size, quality, model, n=1):
    """Call gpt-image-2 generate; return list of public Supabase URLs."""
    payload = {"model": model, "prompt": prompt, "size": size, "quality": quality, "n": n}
    r = _req.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload, timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:400]}")
    urls = []
    for item in r.json().get("data", []):
        img_bytes = (base64.b64decode(item["b64_json"]) if item.get("b64_json")
                     else _req.get(item["url"], timeout=30).content)
        ts   = int(time.time())
        path = f"ai-images/chat_{ts}_{len(urls)}.png"
        urls.append(upload_image_to_supabase(img_bytes, path, "image/png"))
    return urls


def _edit_image(prompt, img_bytes, size, quality, model):
    """Call gpt-image-2 edits endpoint; return list of public URLs."""
    try:
        from PIL import Image as _PIL
        pil = _PIL.open(io.BytesIO(img_bytes)).convert("RGBA")
        buf = io.BytesIO(); pil.save(buf, "PNG"); img_bytes = buf.getvalue()
    except Exception:
        pass
    r = _req.post(
        "https://api.openai.com/v1/images/edits",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        files={"image": ("ref.png", io.BytesIO(img_bytes), "image/png")},
        data={"model": model, "prompt": prompt, "n": "1", "size": "1024x1024", "quality": quality},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI edits {r.status_code}: {r.text[:400]}")
    urls = []
    for item in r.json().get("data", []):
        b = (base64.b64decode(item["b64_json"]) if item.get("b64_json")
             else _req.get(item["url"], timeout=30).content)
        ts   = int(time.time())
        path = f"ai-images/chat_edit_{ts}_{len(urls)}.png"
        urls.append(upload_image_to_supabase(b, path, "image/png"))
    return urls


# ── Routes ──────────────────────────────────────────────────────────────
@ai_image_chat_bp.route("/admin/ai-image-chat")
def page_ai_image_chat():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next="/admin/ai-image-chat", error=None)
    brands = _brands_list()
    return render_template_string(AI_IMAGE_CHAT_HTML, key=key, brands=brands, brands_data=brands)


@ai_image_chat_bp.route("/api/ai-image-chat/sessions", methods=["GET"])
def api_sessions_list():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    return jsonify({"ok": True, "sessions": _get_sessions()})


@ai_image_chat_bp.route("/api/ai-image-chat/sessions", methods=["POST"])
def api_sessions_create():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    d = request.get_json(silent=True) or {}
    sid = _create_session(d.get("title", "新對話"), d.get("brand_key", ""))
    return jsonify({"ok": True, "session_id": sid})


@ai_image_chat_bp.route("/api/ai-image-chat/sessions/<int:session_id>/messages", methods=["GET"])
def api_session_messages(session_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    return jsonify({"ok": True, "messages": _get_messages(session_id)})


@ai_image_chat_bp.route("/api/ai-image-chat/message", methods=["POST"])
def api_send_message():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized"}), 403

    is_mp = request.content_type and "multipart" in request.content_type
    d        = request.form if is_mp else (request.get_json(silent=True) or {})
    ref_file = request.files.get("reference_image") if is_mp else None

    session_id = int(d.get("session_id") or 0)
    user_text  = (d.get("text") or "").strip()
    brand_key  = (d.get("brand_key") or "").strip()
    size       = d.get("size") or "1024x1024"
    quality    = d.get("quality") or IMAGE_QUALITY
    try:
        count = max(1, min(4, int(d.get("count") or 1)))
    except (ValueError, TypeError):
        count = 1

    if not session_id:
        return jsonify({"ok": False, "error": "session_id required"})
    if not user_text:
        return jsonify({"ok": False, "error": "text required"})

    # Upload reference image if provided
    ref_urls     = []
    ref_img_bytes = None
    if ref_file:
        ref_img_bytes = ref_file.read()
        try:
            path    = f"ai-images/chat_ref_{int(time.time())}.png"
            ref_pub = upload_image_to_supabase(ref_img_bytes, path, "image/png")
            ref_urls = [ref_pub]
        except Exception as e:
            print(f"[AI_CHAT] ref upload: {e}", file=sys.stderr)

    # Save user message
    user_mid = _save_msg(session_id, "user", user_text, ref_urls=ref_urls, size=size, quality=quality)

    # Auto-title session from first message
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT title FROM ai_image_chat_sessions WHERE id=%s", (session_id,))
        row = cur.fetchone()
        if row and row[0] == "新對話":
            cur.execute("UPDATE ai_image_chat_sessions SET title=%s WHERE id=%s",
                        (user_text[:30], session_id))
            conn.commit()
        cur.close(); conn.close()
    except Exception:
        pass

    # Build brand context
    brand = _get_brand(brand_key)
    brand_ctx = _brand_context(brand)

    # Collect recent user messages for context
    prev_msgs = _get_messages(session_id)
    prev_user = [m["message_text"] for m in prev_msgs if m["role"] == "user" and m["id"] != user_mid]

    # Enhance prompt with Claude Haiku
    print("[AI_CHAT] enhancing prompt with Claude Haiku", file=sys.stderr)
    prompt = _enhance_prompt(user_text, brand_ctx, prev_user)

    # Generate images
    print("[AI_CHAT] calling OpenAI image API", file=sys.stderr)
    try:
        if ref_img_bytes:
            image_urls = _edit_image(prompt, ref_img_bytes, size, quality, IMAGE_MODEL)
        else:
            image_urls = _generate_images(prompt, size, quality, IMAGE_MODEL, n=count)

        if not image_urls:
            raise RuntimeError("No images returned")

        brand_note = f"（品牌：{brand['name']}）" if brand else ""
        n_gen = len(image_urls)
        asst_text = (f"已生成 {n_gen} 張圖片{brand_note} ✨" if n_gen > 1
                     else f"已根據你的需求生成圖片{brand_note} ✨")

        asst_mid = _save_msg(session_id, "assistant", asst_text, prompt=prompt,
                             image_urls=image_urls, size=size, quality=quality)

        return jsonify({
            "ok": True,
            "session_id": session_id,
            "user_message_id": user_mid,
            "assistant_message_id": asst_mid,
            "assistant_text": asst_text,
            "prompt_text": prompt,
            "image_urls": image_urls,
        })

    except Exception as e:
        msg = str(e)
        print(f"[AI_CHAT] generate error: {msg}", file=sys.stderr)
        _save_msg(session_id, "assistant", f"生成失敗：{msg[:200]}", status="failed")
        return jsonify({"ok": False, "error": msg[:300]})


@ai_image_chat_bp.route("/api/ai-image-chat/regenerate", methods=["POST"])
def api_regenerate():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized"}), 403

    d          = request.get_json(silent=True) or {}
    session_id = int(d.get("session_id") or 0)
    message_id = int(d.get("message_id") or 0)

    if not session_id or not message_id:
        return jsonify({"ok": False, "error": "session_id and message_id required"})

    # Get original assistant message
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT prompt_text, size, quality FROM ai_image_chat_messages "
            "WHERE id=%s AND session_id=%s LIMIT 1",
            (message_id, session_id)
        )
        row = cur.fetchone(); cur.close(); conn.close()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    if not row:
        return jsonify({"ok": False, "error": "message not found"})

    prompt, size, quality = row
    size    = size or "1024x1024"
    quality = quality or IMAGE_QUALITY

    print("[AI_CHAT] regenerate, calling OpenAI image API", file=sys.stderr)
    try:
        image_urls = _generate_images(prompt, size, quality, IMAGE_MODEL, n=1)
        if not image_urls:
            raise RuntimeError("No images returned")

        asst_text = "已重新生成圖片 🔄"
        asst_mid  = _save_msg(session_id, "assistant", asst_text, prompt=prompt,
                              image_urls=image_urls, size=size, quality=quality)

        return jsonify({
            "ok": True,
            "assistant_message_id": asst_mid,
            "assistant_text": asst_text,
            "prompt_text": prompt,
            "image_urls": image_urls,
        })
    except Exception as e:
        msg = str(e)
        print(f"[AI_CHAT] regen error: {msg}", file=sys.stderr)
        return jsonify({"ok": False, "error": msg[:300]})


# ── HTML Template ────────────────────────────────────────────────────────
AI_IMAGE_CHAT_HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 對話生成</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#fff;color:#1a1a1a;height:100vh;display:flex;flex-direction:column;overflow:hidden}
/* Topbar */
.topbar{background:#fff;border-bottom:1px solid #e8e8e8;padding:0 18px;height:48px;display:flex;align-items:center;gap:8px;flex-shrink:0;z-index:50}
.topbar a{color:#666;text-decoration:none;font-size:13px}.topbar a:hover{color:#000}
.topbar .sep{color:#d0d0d0}.topbar h1{font-size:14px;font-weight:700}
/* Layout */
.chat-layout{display:flex;flex:1;overflow:hidden}
/* Sidebar */
.sidebar{width:240px;background:#1a1a1a;display:flex;flex-direction:column;flex-shrink:0;overflow:hidden}
.sidebar-top{padding:12px;flex-shrink:0}
.btn-new-chat{width:100%;padding:10px 14px;background:transparent;border:1.5px solid rgba(255,255,255,.25);color:#fff;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;transition:.15s;text-align:left;display:flex;align-items:center;gap:8px}
.btn-new-chat:hover{background:rgba(255,255,255,.1);border-color:rgba(255,255,255,.5)}
.session-list{flex:1;overflow-y:auto;padding:4px 8px 12px}
.session-list::-webkit-scrollbar{width:3px}
.session-list::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:2px}
.sess-item{padding:9px 10px;border-radius:7px;cursor:pointer;transition:.15s;margin-bottom:3px}
.sess-item:hover{background:rgba(255,255,255,.07)}
.sess-item.active{background:rgba(255,255,255,.14)}
.sess-title{font-size:12px;font-weight:600;color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:3px}
.sess-meta{display:flex;align-items:center;gap:5px}
.sess-brand{font-size:10px;background:rgba(255,255,255,.15);color:rgba(255,255,255,.7);padding:1px 6px;border-radius:5px}
.sess-time{font-size:10px;color:rgba(255,255,255,.35)}
.sess-empty{font-size:12px;color:rgba(255,255,255,.3);text-align:center;padding:24px 12px}
/* Main */
.chat-main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.chat-header{padding:12px 20px;border-bottom:1px solid #f0f0f0;display:flex;align-items:center;gap:10px;flex-shrink:0;min-height:48px}
.chat-title-text{font-size:14px;font-weight:700;color:#666}
.chat-title-text.has-session{color:#1a1a1a}
.brand-badge{font-size:11px;background:#e8eaf6;color:#3949ab;padding:2px 8px;border-radius:6px;font-weight:600}
/* Chat area */
.chat-area{flex:1;overflow-y:auto;position:relative;display:flex;flex-direction:column}
.chat-area::-webkit-scrollbar{width:5px}
.chat-area::-webkit-scrollbar-thumb{background:#e0e0e0;border-radius:3px}
/* Welcome */
.welcome-screen{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;padding:32px;text-align:center;gap:10px}
.welcome-icon{font-size:40px}
.welcome-title{font-size:20px;font-weight:700;color:#1a1a1a}
.welcome-sub{font-size:13px;color:#888;line-height:1.6;max-width:360px}
.example-chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:8px}
.chip{font-size:12px;padding:7px 14px;border:1.5px solid #e0e0e0;border-radius:20px;cursor:pointer;transition:.15s;color:#555;background:#fff}
.chip:hover{border-color:#1a1a1a;color:#1a1a1a}
/* Messages */
.chat-messages{padding:16px 20px;display:flex;flex-direction:column;gap:14px}
.msg-row{display:flex;gap:10px}
.user-row{justify-content:flex-end}
.ai-row{justify-content:flex-start}
.ai-avatar{width:30px;height:30px;background:#1a1a1a;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;margin-top:2px}
.ai-content{display:flex;flex-direction:column;gap:8px;max-width:75%}
.msg-bubble{padding:10px 14px;border-radius:12px;font-size:13px;line-height:1.6;max-width:520px;word-break:break-word}
.user-bubble{background:#1a1a1a;color:#fff;border-radius:12px 12px 2px 12px}
.ai-bubble{background:#f2f2f2;color:#1a1a1a;border-radius:12px 12px 12px 2px}
.ref-img-bubble{display:block;max-width:120px;border-radius:7px;margin-bottom:6px;border:1px solid rgba(255,255,255,.2)}
/* Image card */
.img-card{background:#fff;border:1px solid #e8e8e8;border-radius:10px;overflow:hidden;max-width:420px}
.chat-img{width:100%;display:block;cursor:pointer;transition:.15s}
.chat-img:hover{opacity:.92}
.img-card-btns{display:flex;gap:0;border-top:1px solid #f0f0f0}
.btn-card{flex:1;padding:8px 4px;border:none;background:#fff;font-size:11px;font-weight:600;cursor:pointer;color:#555;transition:.15s;display:flex;align-items:center;justify-content:center;gap:3px;text-decoration:none;border-right:1px solid #f0f0f0}
.btn-card:last-child{border-right:none}
.btn-card:hover{background:#f7f7f7;color:#1a1a1a}
.prompt-toggle{border-top:1px solid #f0f0f0}
.prompt-toggle-btn{width:100%;padding:6px 12px;background:none;border:none;font-size:11px;color:#aaa;cursor:pointer;text-align:left;transition:.15s}
.prompt-toggle-btn:hover{color:#555}
.prompt-pre{font-size:10px;font-family:Consolas,monospace;color:#555;background:#fafafa;padding:10px 12px;white-space:pre-wrap;word-break:break-all;line-height:1.5;border-top:1px solid #f0f0f0;max-height:160px;overflow-y:auto}
/* Loading */
.loading-bubble{display:flex;align-items:center;gap:8px;color:#888}
.typing-dots{display:flex;gap:3px}
.typing-dots span{width:5px;height:5px;border-radius:50%;background:#aaa;animation:dot .9s infinite}
.typing-dots span:nth-child(2){animation-delay:.2s}
.typing-dots span:nth-child(3){animation-delay:.4s}
@keyframes dot{0%,80%,100%{opacity:.2}40%{opacity:1}}
/* Input */
.chat-input-area{border-top:1px solid #e8e8e8;background:#fff;padding:12px 16px;flex-shrink:0}
.ref-preview{display:flex;align-items:center;gap:8px;background:#f5f5f5;border-radius:7px;padding:6px 10px;margin-bottom:8px}
.ref-preview img{width:36px;height:36px;object-fit:cover;border-radius:5px}
.ref-fname{font-size:11px;color:#555;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ref-preview button{background:none;border:none;color:#aaa;cursor:pointer;font-size:14px;padding:2px}
.ref-preview button:hover{color:#e53935}
.input-opts{display:flex;align-items:center;gap:6px;margin-bottom:8px;flex-wrap:wrap}
.opt-icon{width:32px;height:32px;border:1px solid #e0e0e0;border-radius:7px;background:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:16px;transition:.15s;position:relative}
.opt-icon:hover{border-color:#888;background:#f9f9f9}
.opt-icon input{position:absolute;inset:0;opacity:0;cursor:pointer}
.opt-sel{padding:5px 8px;border:1px solid #e0e0e0;border-radius:7px;font-size:12px;background:#fff;color:#1a1a1a;cursor:pointer;outline:none;transition:.15s}
.opt-sel:focus{border-color:#1a1a1a}
.input-row{display:flex;gap:8px;align-items:flex-end}
.chat-input{flex:1;border:1.5px solid #e0e0e0;border-radius:10px;padding:9px 12px;font-size:13px;font-family:inherit;outline:none;resize:none;line-height:1.55;transition:.15s;max-height:160px;overflow-y:auto}
.chat-input:focus{border-color:#1a1a1a}
.btn-send{padding:9px 18px;background:#1a1a1a;color:#fff;border:none;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;transition:.15s;display:flex;align-items:center;gap:6px;flex-shrink:0;height:40px}
.btn-send:hover{background:#333}.btn-send:disabled{background:#aaa;cursor:not-allowed}
.spin{width:14px;height:14px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;display:none}
@keyframes spin{to{transform:rotate(360deg)}}
/* Cost banner */
.cost-banner{background:#fff8e1;border-bottom:1px solid #ffe082;padding:7px 18px;font-size:12px;color:#5d4037;display:flex;align-items:baseline;gap:6px;flex-shrink:0;flex-wrap:wrap}
.cost-banner .cb-sub{color:#888;font-size:11px}
.cost-banner .cb-sub a{color:#3949ab;text-decoration:none}
.cost-banner .cb-sub a:hover{text-decoration:underline}
/* Brand hint */
.brand-hint{background:#e8eaf6;border-radius:7px;padding:7px 11px;margin-bottom:7px;display:none;align-items:flex-start;gap:7px}
.bh-body{display:flex;flex-direction:column;gap:4px;flex:1}
.bh-name{font-size:12px;color:#3949ab;font-weight:700}
.bh-tags{display:flex;gap:5px;flex-wrap:wrap}
.bh-tag{font-size:10px;background:rgba(57,73,171,.12);color:#3949ab;padding:2px 8px;border-radius:8px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Input error */
.input-err{font-size:12px;color:#e53935;margin-top:5px;padding-left:2px;display:none}
/* Fail bubble */
.fail-bubble{background:#fff3f3;border:1px solid #ffcdd2;color:#c62828}
.fail-badge{display:inline-block;font-size:10px;font-weight:700;background:#e53935;color:#fff;padding:2px 7px;border-radius:6px;margin-right:6px;vertical-align:middle}
.fail-detail{font-size:12px;color:#c62828;margin-top:5px;line-height:1.5}
@media(max-width:700px){
  .sidebar{width:100%;height:auto;max-height:160px}
  .chat-layout{flex-direction:column}
  .input-opts{flex-wrap:wrap}
}
</style>
</head>
<body>

<div class="topbar">
  <a href="/admin?key={{ key }}">← 後台</a><span class="sep">/</span>
  <h1>AI 對話生成</h1>
</div>

<div class="cost-banner">
  ⚠️ 此頁為快速生圖模式，送出後會直接呼叫圖片 API 並產生成本。
  <span class="cb-sub">若要先免費比較 4 組 Prompt 方案，請使用<a href="/admin/ai-images?key={{ key }}">AI 圖片中心</a>。</span>
</div>

<div class="chat-layout">

  <!-- ══ SIDEBAR ══ -->
  <div class="sidebar">
    <div class="sidebar-top">
      <button class="btn-new-chat" onclick="createSession()">
        ＋ 新對話
      </button>
    </div>
    <div class="session-list" id="sessionList">
      <div class="sess-empty">尚無對話紀錄</div>
    </div>
  </div>

  <!-- ══ MAIN ══ -->
  <div class="chat-main">
    <div class="chat-header">
      <div class="chat-title-text" id="chatTitle">選擇或建立對話</div>
      <div class="brand-badge" id="chatBrandBadge" style="display:none"></div>
    </div>

    <div class="chat-area" id="chatArea">
      <!-- Welcome -->
      <div class="welcome-screen" id="welcomeScreen">
        <div class="welcome-icon">🎨</div>
        <div class="welcome-title">AI 對話生成</div>
        <div class="welcome-sub">像 ChatGPT 一樣輸入文字，直接生成圖片<br>支援品牌模式、參考圖上傳、多張生成</div>
        <div class="example-chips">
          <div class="chip" onclick="fillExample('幫我做一張朗德太陽能戶外壁燈的官網情境圖，放在現代感建築外牆，夜晚暖光氛圍')">朗德壁燈官網情境圖</div>
          <div class="chip" onclick="fillExample('JS高架床 黑色 單人，北歐風室內實拍感情境圖，木地板，暖光，簡潔現代')">JS高架床情境圖</div>
          <div class="chip" onclick="fillExample('LuAir 空氣清淨機濾網，純白背景商品圖，專業科技感，光線清晰')">LuAir 白底商品圖</div>
        </div>
      </div>
      <!-- Messages injected here -->
      <div class="chat-messages" id="chatMessages" style="display:none"></div>
    </div>

    <!-- ── INPUT AREA ── -->
    <div class="chat-input-area">
      <div class="ref-preview" id="refPreview">
        <img id="refThumb" src="" alt="">
        <span id="refFileName" class="ref-fname"></span>
        <button onclick="clearRef()">✕</button>
      </div>
      <div class="brand-hint" id="brandHint">
        <span style="font-size:16px">🏷️</span>
        <div class="bh-body">
          <div class="bh-name">已套用品牌記憶：<span id="bhName"></span></div>
          <div class="bh-tags" id="bhTags"></div>
        </div>
      </div>
      <div class="input-opts">
        <label class="opt-icon" title="上傳參考圖">
          📎<input type="file" id="refImgInput" accept="image/*" onchange="onRefSelect(this)">
        </label>
        <select id="chatBrand" class="opt-sel" onchange="onBrandChange()">
          <option value="">一般模式</option>
          {% for b in brands %}
          <option value="{{ b.brand_key }}">{{ b.name }}</option>
          {% endfor %}
        </select>
        <select id="chatSize" class="opt-sel">
          <option value="1024x1024">1:1 正方形</option>
          <option value="1536x1024">橫 3:2</option>
          <option value="1024x1536">直 2:3</option>
        </select>
        <select id="chatQuality" class="opt-sel">
          <option value="high">高品質</option>
          <option value="auto">自動</option>
        </select>
        <select id="chatCount" class="opt-sel">
          <option value="1">1 張</option>
          <option value="2">2 張</option>
          <option value="3">3 張</option>
          <option value="4">4 張</option>
        </select>
      </div>
      <div class="input-row">
        <textarea class="chat-input" id="chatInput" rows="2"
          placeholder="請描述你想生成的圖片... 例：幫我做一張朗德太陽能壁燈的情境圖"
          onkeydown="onInputKey(event)" oninput="clearInputErr()"></textarea>
        <button class="btn-send" id="sendBtn" onclick="sendMessage()">
          <div class="spin" id="sendSpin"></div>
          <span id="sendBtnText">送出</span>
        </button>
      </div>
      <div class="input-err" id="inputErr"></div>
    </div>
  </div>
</div>

<script>
const KEY        = '{{ key }}';
const BRANDS_DATA = {{ brands_data | tojson }};
let currentSessionId = null;

// ── Brand hint (item 4) ───────────────────────────────────────────────
function onBrandChange(){
  const bk   = document.getElementById('chatBrand').value;
  const hint = document.getElementById('brandHint');
  if(!bk){ hint.style.display='none'; return; }
  const bd = BRANDS_DATA.find(b=>b.brand_key===bk);
  if(!bd){ hint.style.display='none'; return; }
  document.getElementById('bhName').textContent = bd.name;
  const tags = [];
  if(bd.style_keywords) tags.push('風格：'+(bd.style_keywords.slice(0,28)+(bd.style_keywords.length>28?'…':'')));
  if(bd.color_style)    tags.push('色系：'+(bd.color_style.slice(0,28)+(bd.color_style.length>28?'…':'')));
  if(bd.negative_rules) tags.push('禁止：'+(bd.negative_rules.slice(0,30)+(bd.negative_rules.length>30?'…':'')));
  document.getElementById('bhTags').innerHTML = tags.map(t=>`<span class="bh-tag">${t}</span>`).join('');
  hint.style.display = 'flex';
}

// ── Sidebar ──────────────────────────────────────────────────────────
async function loadSessions(){
  try{
    const r = await fetch('/api/ai-image-chat/sessions?key='+KEY);
    const j = await r.json();
    renderSessions(j.sessions || []);
  }catch(e){ console.error(e); }
}

function renderSessions(sessions){
  const el = document.getElementById('sessionList');
  if(!sessions.length){
    el.innerHTML='<div class="sess-empty">尚無對話紀錄</div>';
    return;
  }
  el.innerHTML = sessions.map(s=>{
    const dt = s.updated_at ? new Date(s.updated_at*1000).toLocaleString('zh-TW',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
    const active = s.id === currentSessionId ? ' active' : '';
    const bk = s.brand_key ? `<span class="sess-brand">${s.brand_key}</span>` : '';
    const safeTitle = (s.title||'新對話').replace(/</g,'&lt;');
    return `<div class="sess-item${active}" onclick="switchSession(${s.id})">
      <div class="sess-title">${safeTitle}</div>
      <div class="sess-meta">${bk}<span class="sess-time">${dt}</span></div>
    </div>`;
  }).join('');
}

async function createSession(){
  const brand = document.getElementById('chatBrand').value;
  try{
    const r = await fetch('/api/ai-image-chat/sessions?key='+KEY,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({brand_key: brand}),
    });
    const j = await r.json();
    if(j.ok){
      currentSessionId = j.session_id;
      updateHeader('新對話', brand);
      showMessages([]);
      await loadSessions();
      document.getElementById('chatInput').focus();
    }
  }catch(e){ alert('建立對話失敗: '+e.message); }
}

async function switchSession(sid){
  currentSessionId = sid;
  // Find session title from DOM
  const items = document.querySelectorAll('.sess-item');
  items.forEach(el=>{
    const isActive = el.getAttribute('onclick') === `switchSession(${sid})`;
    el.classList.toggle('active', isActive);
    if(isActive){
      const title = el.querySelector('.sess-title')?.textContent || '';
      const brand = el.querySelector('.sess-brand')?.textContent || '';
      updateHeader(title, brand);
    }
  });
  try{
    const r = await fetch('/api/ai-image-chat/sessions/'+sid+'/messages?key='+KEY);
    const j = await r.json();
    showMessages(j.messages || []);
  }catch(e){ console.error(e); }
}

function updateHeader(title, brandKey){
  const t = document.getElementById('chatTitle');
  t.textContent = title;
  t.className = 'chat-title-text has-session';
  const bb = document.getElementById('chatBrandBadge');
  if(brandKey){ bb.textContent = brandKey; bb.style.display='block'; }
  else { bb.style.display='none'; }
}

// ── Messages rendering ───────────────────────────────────────────────
function showMessages(msgs){
  const ws  = document.getElementById('welcomeScreen');
  const mc  = document.getElementById('chatMessages');
  if(!msgs.length){
    ws.style.display  = 'flex';
    mc.style.display  = 'none';
    mc.innerHTML = '';
    return;
  }
  ws.style.display = 'none';
  mc.style.display = 'flex';
  mc.innerHTML = msgs.map(m=> m.role==='user' ? renderUserMsg(m) : renderAIMsg(m)).join('');
  scrollBottom();
}

function renderUserMsg(m){
  const refHtml = (m.reference_image_urls && m.reference_image_urls.length)
    ? `<img src="${m.reference_image_urls[0]}" class="ref-img-bubble">` : '';
  return `<div class="msg-row user-row">
    <div class="msg-bubble user-bubble">${refHtml}${esc(m.message_text).replace(/\\n/g,'<br>')}</div>
  </div>`;
}

function renderAIMsg(m){
  if(m.status === 'failed'){
    return `<div class="msg-row ai-row">
      <div class="ai-avatar">🤖</div>
      <div class="ai-content">
        <div class="msg-bubble ai-bubble fail-bubble">
          <span class="fail-badge">⚠️ 生成失敗</span>
          <div class="fail-detail">${esc(m.message_text)}</div>
        </div>
      </div>
    </div>`;
  }
  const cards = (m.image_urls||[]).map((url,i)=>renderImgCard(url, m.prompt_text, m.id, i)).join('');
  return `<div class="msg-row ai-row">
    <div class="ai-avatar">🤖</div>
    <div class="ai-content">
      <div class="msg-bubble ai-bubble">${esc(m.message_text).replace(/\\n/g,'<br>')}</div>
      ${cards}
    </div>
  </div>`;
}

function renderImgCard(url, prompt, msgId, idx){
  const pid = 'pp_'+msgId+'_'+idx;
  const safeP = (prompt||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  return `<div class="img-card">
    <img class="chat-img" src="${url}" onclick="window.open('${url}','_blank')" loading="lazy">
    <div class="img-card-btns">
      <a class="btn-card" href="${url}" download>⬇ 下載</a>
      <button class="btn-card" onclick="copyText(\`${(prompt||'').replace(/`/g,'\\`')}\`,this)">📋 複製 Prompt</button>
      <button class="btn-card" onclick="regenMessage(${msgId})">🔄 重新生成</button>
    </div>
    <div class="prompt-toggle">
      <button class="prompt-toggle-btn" onclick="togglePromptView('${pid}',this)">查看 Prompt ▼</button>
      <pre class="prompt-pre" id="${pid}" style="display:none">${safeP}</pre>
    </div>
  </div>`;
}

function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function togglePromptView(pid, btn){
  const el = document.getElementById(pid);
  if(!el) return;
  const open = el.style.display !== 'none';
  el.style.display = open ? 'none' : 'block';
  btn.textContent  = open ? '查看 Prompt ▼' : '收起 ▲';
}

async function copyText(text, btn){
  try{
    await navigator.clipboard.writeText(text);
    const orig = btn.textContent; btn.textContent='已複製！';
    setTimeout(()=>btn.textContent=orig, 1500);
  }catch(e){ alert(text.slice(0,100)); }
}

function scrollBottom(){
  const el = document.getElementById('chatArea');
  if(el) el.scrollTop = el.scrollHeight;
}

// ── Input error helpers ───────────────────────────────────────────────
function showInputErr(msg){
  const el = document.getElementById('inputErr');
  el.textContent = msg; el.style.display = 'block';
}
function clearInputErr(){
  document.getElementById('inputErr').style.display = 'none';
}

// ── Send ─────────────────────────────────────────────────────────────
async function sendMessage(){
  const text = document.getElementById('chatInput').value.trim();
  // Item 3: empty message block
  if(!text){
    showInputErr('請輸入想生成的圖片內容');
    document.getElementById('chatInput').focus();
    return;
  }
  clearInputErr();

  const count = parseInt(document.getElementById('chatCount').value) || 1;
  // Item 2: cost confirmation for count > 1
  if(count > 1){
    if(!confirm(`將生成 ${count} 張圖片，會依張數產生成本，確定送出嗎？`)) return;
  }

  if(!currentSessionId){
    await createSession();
    if(!currentSessionId){ alert('無法建立對話'); return; }
  }

  const refFile   = document.getElementById('refImgInput').files[0];
  const refThumb  = document.getElementById('refThumb').src;
  const brand     = document.getElementById('chatBrand').value;
  const size      = document.getElementById('chatSize').value;
  const quality   = document.getElementById('chatQuality').value;

  // Append user bubble
  const ws = document.getElementById('welcomeScreen');
  const mc = document.getElementById('chatMessages');
  ws.style.display = 'none';
  mc.style.display = 'flex';

  const refHtml = (refFile && refThumb) ? `<img src="${refThumb}" class="ref-img-bubble">` : '';
  mc.insertAdjacentHTML('beforeend',`<div class="msg-row user-row">
    <div class="msg-bubble user-bubble">${refHtml}${esc(text).replace(/\\n/g,'<br>')}</div>
  </div>`);

  // Clear input & ref
  document.getElementById('chatInput').value = '';
  clearRef();
  scrollBottom();

  // Loading bubble
  const lid = 'load_'+Date.now();
  mc.insertAdjacentHTML('beforeend',`<div class="msg-row ai-row" id="${lid}">
    <div class="ai-avatar">🤖</div>
    <div class="ai-content">
      <div class="msg-bubble ai-bubble loading-bubble">
        <div class="typing-dots"><span></span><span></span><span></span></div>
        AI 正在生成圖片，請稍候…
      </div>
    </div>
  </div>`);
  scrollBottom();
  setSending(true);

  const fd = new FormData();
  fd.append('session_id', currentSessionId);
  fd.append('text',       text);
  fd.append('brand_key',  brand);
  fd.append('size',       size);
  fd.append('quality',    quality);
  fd.append('count',      count);
  if(refFile) fd.append('reference_image', refFile);

  try{
    const r = await fetch('/api/ai-image-chat/message?key='+KEY, {method:'POST', body:fd});
    const j = await r.json();
    document.getElementById(lid)?.remove();
    if(j.ok){
      mc.insertAdjacentHTML('beforeend', renderAIMsg({
        role:'assistant', message_text: j.assistant_text,
        image_urls: j.image_urls, prompt_text: j.prompt_text,
        id: j.assistant_message_id, status:'ok',
        reference_image_urls:[],
      }));
      loadSessions();
    } else {
      mc.insertAdjacentHTML('beforeend', renderAIMsg({
        role:'assistant', message_text:'生成失敗：'+(j.error||'未知錯誤'),
        image_urls:[], prompt_text:'', id:0, status:'failed', reference_image_urls:[],
      }));
    }
  }catch(e){
    document.getElementById(lid)?.remove();
    mc.insertAdjacentHTML('beforeend', renderAIMsg({
      role:'assistant', message_text:'連線錯誤：'+e.message,
      image_urls:[], prompt_text:'', id:0, status:'failed', reference_image_urls:[],
    }));
  }
  setSending(false);
  scrollBottom();
}

function setSending(on){
  const btn  = document.getElementById('sendBtn');
  const spin = document.getElementById('sendSpin');
  const txt  = document.getElementById('sendBtnText');
  btn.disabled = on;
  spin.style.display = on ? 'block' : 'none';
  txt.textContent    = on ? '生成中…' : '送出';
}

function onInputKey(e){
  if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); sendMessage(); }
}

// ── Regenerate ───────────────────────────────────────────────────────
async function regenMessage(msgId){
  if(!msgId || !currentSessionId) return;
  setSending(true);
  try{
    const r = await fetch('/api/ai-image-chat/regenerate?key='+KEY,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: currentSessionId, message_id: msgId}),
    });
    const j = await r.json();
    if(j.ok){
      const mc = document.getElementById('chatMessages');
      mc.insertAdjacentHTML('beforeend', renderAIMsg({
        role:'assistant', message_text: j.assistant_text,
        image_urls: j.image_urls, prompt_text: j.prompt_text,
        id: j.assistant_message_id, status:'ok', reference_image_urls:[],
      }));
      scrollBottom();
      loadSessions();
    } else {
      alert('重新生成失敗：'+(j.error||''));
    }
  }catch(e){ alert('連線錯誤：'+e.message); }
  setSending(false);
}

// ── Ref image ────────────────────────────────────────────────────────
function onRefSelect(input){
  const file = input.files[0];
  if(!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    document.getElementById('refThumb').src = e.target.result;
    document.getElementById('refFileName').textContent = file.name;
    document.getElementById('refPreview').style.display = 'flex';
  };
  reader.readAsDataURL(file);
}

function clearRef(){
  document.getElementById('refImgInput').value = '';
  document.getElementById('refPreview').style.display = 'none';
  document.getElementById('refThumb').src = '';
}

function fillExample(text){
  document.getElementById('chatInput').value = text;
  document.getElementById('chatInput').focus();
}

// ── Init ─────────────────────────────────────────────────────────────
document.getElementById('refPreview').style.display = 'none';
loadSessions();
</script>
</body>
</html>"""

# ── Init ────────────────────────────────────────────────────────────────
_chat_migrate()
