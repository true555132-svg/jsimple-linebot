"""
ai_images_api.py — AI 圖片中心 Blueprint

Routes:
    GET  /admin/ai-images
    POST /api/ai-images/prompt
    POST /api/ai-images/generate
    GET  /api/ai-images/tasks
    GET  /api/ai-images/tasks/<id>
    POST /api/ai-images/tasks/<id>/regenerate

Env vars used:
    OPENAI_API_KEY   — OpenAI API key (required)
    IMAGE_MODEL      — image model, default gpt-image-2
    IMAGE_QUALITY    — default quality, default high
    DATABASE_URL     — PostgreSQL URL
"""

import os, io, json, time, base64, sys
from flask import Blueprint, request, jsonify, render_template_string

from products_api import (
    upload_image_to_supabase,
    _pg_conn,
    check_auth,
    auth_required,
    LOGIN_HTML,
)

ai_images_bp = Blueprint("ai_images", __name__)

# ── Config ────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
IMAGE_MODEL    = os.getenv("IMAGE_MODEL", "gpt-image-2")
IMAGE_QUALITY  = os.getenv("IMAGE_QUALITY", "high")
_DB            = os.getenv("DATABASE_URL", "")

# ── Image-type presets ────────────────────────────────────────────────
_PRESETS = {
    "shopee_main": {
        "label": "蝦皮商品主圖",
        "default_size": "1024x1024",
        "platform": "Shopee Taiwan e-commerce main image",
        "scene": (
            "Clean minimal background. Pure white or very light warm white. "
            "Soft natural drop shadow beneath the product."
        ),
        "composition": (
            "Product centered, filling 65-75% of frame. "
            "Front-facing or 3/4 angle for slight depth. No distracting props."
        ),
        "photography": (
            "Professional studio product photography. "
            "Even soft lighting, no harsh shadows. Clean commercial e-commerce style."
        ),
    },
    "website_banner": {
        "label": "官網橫幅 Banner",
        "default_size": "1536x1024",
        "platform": "Taiwan brand website hero banner (horizontal)",
        "scene": (
            "Lifestyle or interior environment fitting the product category. "
            "Clean editorial atmosphere. Leave open space on one side for optional text overlay."
        ),
        "composition": (
            "Product placed on one side using rule of thirds. "
            "Spacious background fills remaining area. Wide cinematic framing."
        ),
        "photography": (
            "Editorial lifestyle photography. Natural or warm studio lighting. "
            "Premium brand feel, magazine quality."
        ),
    },
    "product_white_bg": {
        "label": "商品白底圖",
        "default_size": "1024x1024",
        "platform": "Clean white background product shot",
        "scene": (
            "Pure white background only. No scene, no props, no texture, no floor pattern. "
            "Gentle soft shadow beneath the product only."
        ),
        "composition": (
            "Product perfectly centered with clean equal margins on all sides. "
            "No cropping of product edges."
        ),
        "photography": (
            "White infinity backdrop product photography. "
            "Soft diffused studio lighting. Crisp clean edges."
        ),
    },
    "product_scene_bg": {
        "label": "商品情境背景",
        "default_size": "1024x1024",
        "platform": "Lifestyle scene product photo",
        "scene": (
            "Appropriate lifestyle environment complementing the product category. "
            "Clean and uncluttered. Tasteful minimal props if contextually needed."
        ),
        "composition": (
            "Product as the hero element. Scene provides context without competing with product. "
            "Balanced natural composition."
        ),
        "photography": (
            "Lifestyle editorial photography. Natural lighting preferred. "
            "Clean background even within scene."
        ),
    },
    "fb_ad": {
        "label": "FB / IG 廣告圖",
        "default_size": "1024x1024",
        "platform": "Facebook / Instagram advertising image",
        "scene": (
            "Eye-catching yet clean background. Bold but professional. "
            "Clear open space for optional text overlay."
        ),
        "composition": (
            "Product prominent and highly visible. High visual impact composition. "
            "Clear focal point."
        ),
        "photography": (
            "Commercial advertising photography. Vibrant but tasteful. "
            "High clarity and contrast."
        ),
    },
    "seo_cover": {
        "label": "SEO 文章封面",
        "default_size": "1536x1024",
        "platform": "Blog article / SEO cover image (horizontal)",
        "scene": (
            "Relevant contextual environment for article topic. "
            "Clean and informative. Open space for title text overlay."
        ),
        "composition": (
            "Wide horizontal format. Visual storytelling. Rule of thirds. "
            "One side open for typography."
        ),
        "photography": "Editorial blog photography style. Clean and professional. Bright and inviting.",
    },
    "infographic": {
        "label": "規格 / 賣點資訊圖",
        "default_size": "1024x1024",
        "platform": "Product specification infographic base image",
        "scene": (
            "Very clean minimal background. Plenty of negative space around the product. "
            "Structured technical presentation feel."
        ),
        "composition": (
            "Product clearly visible from best angle showing key features. "
            "Clean grid-like feel with space for user-added text specs."
        ),
        "photography": "Technical precision product photography. Clean, crisp, and precise.",
    },
}

_SIZE_LABELS = {
    "1024x1024": "1024 × 1024 — 正方形（蝦皮主圖、商品圖、廣告圖）",
    "1536x1024": "1536 × 1024 — 橫式（官網 Banner、SEO 封面）",
    "1024x1536": "1024 × 1536 — 直式（廣告圖、手機版）",
}

# ── Brand seed data ───────────────────────────────────────────────────
_BRAND_SEEDS = [
    {
        "brand_key": "jsimple",
        "name": "JS家具",
        "website_url": "https://www.jsimple.tw/",
        "category": "家具 / 空間收納 / 辦公家具",
        "style": "北歐風、工業風",
        "tone": "專業、實用、設計感",
        "style_keywords": "北歐風、工業風、黑白灰、淺木色、乾淨、實用、空間規劃感",
        "color_style": "black, white, gray, light oak wood, clean neutral tone",
        "image_style": "premium Taiwan furniture website, realistic interior photography, clean composition",
        "negative_rules": "不要廉價淘寶感、不要比例錯誤、不要背景凌亂、不要過度粗獷美式工業風、不要亂加家具零件",
        "prompt_rules": "保留商品比例、結構、材質與顏色，呈現台灣品牌官網質感",
    },
    {
        "brand_key": "lander",
        "name": "朗德 LIGHT+",
        "website_url": "https://www.langdelight.com.tw/",
        "category": "燈具 / 居家照明 / 戶外太陽能燈",
        "style": "現代精品住宅、北歐簡約",
        "tone": "有畫面感，讓人想像燈光裝上後的居家氛圍",
        "style_keywords": "現代精品住宅、北歐簡約、溫暖燈光、乾淨外觀、居家雜誌感",
        "color_style": "warm light, gray concrete, natural wood, greenery accent, premium neutral tone",
        "image_style": "luxury home lighting photography, warm evening atmosphere, clean premium composition",
        "negative_rules": "不要過亮曝光、不要廉價LED感、不要雜亂花園、不要錯誤燈具結構、不要淘寶廣告感",
        "prompt_rules": "燈具本體比例正確，光線自然真實，背景乾淨高級",
    },
    {
        "brand_key": "filterbreath",
        "name": "LuAir 濾呼吸",
        "website_url": "",
        "category": "空氣清淨機濾網 / 除濕機濾網",
        "style": "Clean Tech、專業、乾淨",
        "tone": "用具體數字說話，像健康產品的專業建議，不誇大",
        "style_keywords": "Clean Tech、白綠色、乾淨、專業、空氣流動感、濾網科技感",
        "color_style": "white, soft green, light gray, clean tech minimal palette",
        "image_style": "clean technology product photography, bright white background, soft green accents",
        "negative_rules": "不要太醫療恐怖感、不要髒污誇張、不要亂改濾網結構、不要廉價副廠感",
        "prompt_rules": "濾網結構、摺數、外框比例要穩定，呈現乾淨專業感",
    },
    {
        "brand_key": "chengguang",
        "name": "澄光窗簾",
        "website_url": "",
        "category": "窗簾 / 蜂巢簾 / 調光簾 / 捲簾",
        "style": "北歐簡約、明亮居家",
        "tone": "自然、柔和、生活感",
        "style_keywords": "明亮居家、北歐簡約、柔和日光、乾淨室內、溫暖生活感",
        "color_style": "warm white, beige, light natural wood, soft golden sunlight",
        "image_style": "bright Scandinavian home interior photography, soft natural daylight, clean minimal composition",
        "negative_rules": "不要過暗、不要老氣厚重、不要花色太亂、不要廉價窗簾賣場感",
        "prompt_rules": "窗簾垂墜與材質自然，室內空間乾淨明亮，窗邊採光美麗",
    },
]

# ── DB helpers ────────────────────────────────────────────────────────
def _ait_migrate():
    if not _DB:
        return
    try:
        conn = _pg_conn(); cur = conn.cursor()

        # Extend brand_profiles
        for col in [
            "website_url TEXT DEFAULT ''",
            "style_keywords TEXT DEFAULT ''",
            "color_style TEXT DEFAULT ''",
            "negative_rules TEXT DEFAULT ''",
            "prompt_rules TEXT DEFAULT ''",
            "reference_image_urls_json TEXT DEFAULT '[]'",
        ]:
            try:
                cur.execute(f"ALTER TABLE brand_profiles ADD COLUMN IF NOT EXISTS {col}")
                conn.commit()
            except Exception:
                conn.rollback()

        # ai_product_memory
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_product_memory (
                id                     SERIAL PRIMARY KEY,
                brand_key              TEXT NOT NULL DEFAULT '',
                product_name           TEXT NOT NULL DEFAULT '',
                sku                    TEXT DEFAULT '',
                category               TEXT DEFAULT '',
                specs_json             TEXT DEFAULT '{}',
                selling_points         TEXT DEFAULT '',
                material               TEXT DEFAULT '',
                color                  TEXT DEFAULT '',
                source_image_urls_json TEXT DEFAULT '[]',
                reference_notes        TEXT DEFAULT '',
                created_at             BIGINT DEFAULT 0,
                updated_at             BIGINT DEFAULT 0
            )
        """)

        # ai_image_tasks
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_image_tasks (
                id                     SERIAL PRIMARY KEY,
                brand_key              TEXT DEFAULT '',
                product_memory_id      INTEGER DEFAULT NULL,
                product_name           TEXT DEFAULT '',
                sku                    TEXT DEFAULT '',
                image_type             TEXT DEFAULT '',
                user_request           TEXT DEFAULT '',
                final_prompt           TEXT DEFAULT '',
                negative_prompt        TEXT DEFAULT '',
                model                  TEXT DEFAULT '',
                size                   TEXT DEFAULT '',
                quality                TEXT DEFAULT '',
                output_format          TEXT DEFAULT '',
                input_image_urls_json  TEXT DEFAULT '[]',
                output_image_urls_json TEXT DEFAULT '[]',
                supabase_paths_json    TEXT DEFAULT '[]',
                usage_json             TEXT DEFAULT '{}',
                status                 TEXT DEFAULT 'pending',
                error_message          TEXT DEFAULT '',
                created_at             BIGINT DEFAULT 0,
                updated_at             BIGINT DEFAULT 0
            )
        """)

        conn.commit(); cur.close(); conn.close()
        print("[AI Images] DB migration OK", file=sys.stderr)
    except Exception as e:
        print(f"[AI Images] DB migration failed: {e}", file=sys.stderr)


def _seed_brands():
    if not _DB:
        return
    try:
        conn = _pg_conn(); cur = conn.cursor()
        now = int(time.time())
        for s in _BRAND_SEEDS:
            cur.execute("""
                INSERT INTO brand_profiles
                    (brand_key, name, website_url, category, style, tone,
                     style_keywords, color_style, image_style,
                     negative_rules, prompt_rules, reference_image_urls_json,
                     updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'[]',%s)
                ON CONFLICT (brand_key) DO UPDATE SET
                    website_url    = CASE WHEN COALESCE(brand_profiles.website_url,'')=''
                                         THEN EXCLUDED.website_url
                                         ELSE brand_profiles.website_url END,
                    style_keywords = CASE WHEN COALESCE(brand_profiles.style_keywords,'')=''
                                         THEN EXCLUDED.style_keywords
                                         ELSE brand_profiles.style_keywords END,
                    color_style    = CASE WHEN COALESCE(brand_profiles.color_style,'')=''
                                         THEN EXCLUDED.color_style
                                         ELSE brand_profiles.color_style END,
                    image_style    = CASE WHEN COALESCE(brand_profiles.image_style,'')=''
                                         THEN EXCLUDED.image_style
                                         ELSE brand_profiles.image_style END,
                    negative_rules = CASE WHEN COALESCE(brand_profiles.negative_rules,'')=''
                                         THEN EXCLUDED.negative_rules
                                         ELSE brand_profiles.negative_rules END,
                    prompt_rules   = CASE WHEN COALESCE(brand_profiles.prompt_rules,'')=''
                                         THEN EXCLUDED.prompt_rules
                                         ELSE brand_profiles.prompt_rules END,
                    updated_at     = EXCLUDED.updated_at
            """, (
                s["brand_key"], s["name"], s["website_url"], s["category"],
                s["style"], s["tone"], s["style_keywords"], s["color_style"],
                s["image_style"], s["negative_rules"], s["prompt_rules"], now,
            ))
        conn.commit(); cur.close(); conn.close()
        print("[AI Images] Brand seed OK", file=sys.stderr)
    except Exception as e:
        print(f"[AI Images] Brand seed failed: {e}", file=sys.stderr)


def _get_all_brands():
    if not _DB:
        return [{"brand_key": s["brand_key"], "name": s["name"]} for s in _BRAND_SEEDS]
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT brand_key, name FROM brand_profiles ORDER BY brand_key")
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"brand_key": r[0], "name": r[1]} for r in rows]
    except Exception:
        return [{"brand_key": s["brand_key"], "name": s["name"]} for s in _BRAND_SEEDS]


def _get_brand(brand_key):
    if not _DB or not brand_key:
        return {}
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("""
            SELECT brand_key, name, category, style, tone,
                   COALESCE(image_style,''), COALESCE(style_keywords,''),
                   COALESCE(color_style,''), COALESCE(negative_rules,''),
                   COALESCE(prompt_rules,''), COALESCE(website_url,'')
            FROM brand_profiles WHERE brand_key=%s
        """, (brand_key,))
        r = cur.fetchone(); cur.close(); conn.close()
        if not r:
            return {}
        return {
            "brand_key": r[0], "name": r[1], "category": r[2],
            "style": r[3] or "", "tone": r[4] or "", "image_style": r[5],
            "style_keywords": r[6], "color_style": r[7],
            "negative_rules": r[8], "prompt_rules": r[9], "website_url": r[10],
        }
    except Exception as e:
        print(f"[AI Images] _get_brand: {e}", file=sys.stderr)
        return {}


def _create_task(d, status="pending"):
    if not _DB:
        return None
    try:
        conn = _pg_conn(); cur = conn.cursor()
        now = int(time.time())
        cur.execute("""
            INSERT INTO ai_image_tasks
                (brand_key, product_name, sku, image_type, user_request,
                 final_prompt, negative_prompt, model, size, quality,
                 output_format, input_image_urls_json, status, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            d.get("brand_key",""), d.get("product_name",""), d.get("sku",""),
            d.get("image_type",""), d.get("user_request",""),
            d.get("final_prompt",""), d.get("negative_prompt",""),
            d.get("model",""), d.get("size",""), d.get("quality",""),
            d.get("output_format",""), json.dumps(d.get("input_image_urls",[])),
            status, now, now,
        ))
        tid = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return tid
    except Exception as e:
        print(f"[AI Images] _create_task: {e}", file=sys.stderr)
        return None


def _update_task(tid, status, output_urls=None, supabase_paths=None, usage=None, error=""):
    if not _DB or not tid:
        return
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("""
            UPDATE ai_image_tasks SET
                status=%s, output_image_urls_json=%s, supabase_paths_json=%s,
                usage_json=%s, error_message=%s, updated_at=%s
            WHERE id=%s
        """, (
            status, json.dumps(output_urls or []), json.dumps(supabase_paths or []),
            json.dumps(usage or {}), error, int(time.time()), tid,
        ))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[AI Images] _update_task: {e}", file=sys.stderr)


def _get_tasks(brand_key=None, limit=40):
    if not _DB:
        return []
    try:
        conn = _pg_conn(); cur = conn.cursor()
        if brand_key:
            cur.execute("""
                SELECT id, brand_key, product_name, image_type, size, model, status,
                       output_image_urls_json, final_prompt, created_at, error_message
                FROM ai_image_tasks WHERE brand_key=%s
                ORDER BY created_at DESC LIMIT %s
            """, (brand_key, limit))
        else:
            cur.execute("""
                SELECT id, brand_key, product_name, image_type, size, model, status,
                       output_image_urls_json, final_prompt, created_at, error_message
                FROM ai_image_tasks ORDER BY created_at DESC LIMIT %s
            """, (limit,))
        rows = cur.fetchall(); cur.close(); conn.close()
        out = []
        for r in rows:
            urls = json.loads(r[7] or "[]")
            out.append({
                "id": r[0], "brand_key": r[1], "product_name": r[2],
                "image_type": r[3], "size": r[4], "model": r[5], "status": r[6],
                "output_urls": urls, "thumb": urls[0] if urls else "",
                "prompt_preview": (r[8] or "")[:100],
                "created_at": r[9], "error_message": r[10] or "",
            })
        return out
    except Exception as e:
        print(f"[AI Images] _get_tasks: {e}", file=sys.stderr)
        return []


def _get_task(tid):
    if not _DB:
        return None
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("""
            SELECT id, brand_key, product_name, sku, image_type, user_request,
                   final_prompt, negative_prompt, model, size, quality, output_format,
                   input_image_urls_json, output_image_urls_json, supabase_paths_json,
                   usage_json, status, error_message, created_at
            FROM ai_image_tasks WHERE id=%s
        """, (tid,))
        r = cur.fetchone(); cur.close(); conn.close()
        if not r:
            return None
        return {
            "id": r[0], "brand_key": r[1], "product_name": r[2], "sku": r[3],
            "image_type": r[4], "user_request": r[5], "final_prompt": r[6],
            "negative_prompt": r[7], "model": r[8], "size": r[9],
            "quality": r[10], "output_format": r[11],
            "input_image_urls": json.loads(r[12] or "[]"),
            "output_urls": json.loads(r[13] or "[]"),
            "supabase_paths": json.loads(r[14] or "[]"),
            "usage": json.loads(r[15] or "{}"),
            "status": r[16], "error_message": r[17] or "", "created_at": r[18],
        }
    except Exception as e:
        print(f"[AI Images] _get_task: {e}", file=sys.stderr)
        return None


# ── Prompt Builder ────────────────────────────────────────────────────
def build_image_prompt(brand_key="", product_name="", sku="", category="",
                       specs="", material="", color="", selling_points="",
                       image_type="shopee_main", user_request="", size="1024x1024"):
    brand  = _get_brand(brand_key) if brand_key else {}
    preset = _PRESETS.get(image_type, _PRESETS["shopee_main"])
    parts  = []

    # [Brand Memory]
    if brand:
        bm = f"Brand: {brand.get('name','')}"
        if brand.get("style_keywords"): bm += f"\nStyle Keywords: {brand['style_keywords']}"
        if brand.get("color_style"):    bm += f"\nColor Palette: {brand['color_style']}"
        if brand.get("tone"):           bm += f"\nBrand Tone: {brand['tone']}"
        if brand.get("image_style"):    bm += f"\nVisual Direction: {brand['image_style']}"
        parts.append(f"[Brand Memory]\n{bm}")

    # [Product Information]
    if product_name:
        pi = f"Product: {product_name}"
        if category:       pi += f"\nCategory: {category}"
        if sku:            pi += f"\nSKU: {sku}"
        if specs:          pi += f"\nSpecifications / Dimensions: {specs}"
        if material:       pi += f"\nMaterial: {material}"
        if color:          pi += f"\nColor: {color}"
        if selling_points: pi += f"\nKey Selling Points: {selling_points}"
        parts.append(f"[Product Information]\n{pi}")

    # [Image Purpose]
    parts.append(
        f"[Image Purpose]\n"
        f"Purpose: {preset['label']} — {preset['platform']}\n"
        f"Output Size: {size}"
    )

    # [Scene / Background]
    scene = preset["scene"]
    if user_request:
        scene += f"\nAdditional user requirement: {user_request}"
    parts.append(f"[Scene / Background]\n{scene}")

    # [Composition]
    parts.append(f"[Composition]\n{preset['composition']}")

    # [Photography Style]
    parts.append(f"[Photography Style]\n{preset['photography']}")

    # [Strict Product Rules]
    parts.append(
        "[Strict Product Rules]\n"
        "CRITICAL — preserve the EXACT product:\n"
        "- Shape, dimensions, and proportions must remain identical to the description.\n"
        "- Do NOT add components, parts, or accessories not mentioned.\n"
        "- Do NOT alter color, material, texture, or surface finish.\n"
        "- Do NOT distort or stretch the product in any way.\n"
        "- Product must look photorealistic and true to the description above."
    )

    # [Forbidden]
    forbidden_lines = [
        "No logo, brand name, or watermark (unless explicitly requested).",
        "No random text, labels, or overlays on the product or background.",
        "No distorted, warped, or unrealistic product shape.",
        "No cheap marketplace or Taobao/1688 advertising style.",
        "No messy, cluttered, or overly busy background.",
        "No obvious AI artifacts, floating objects, or incorrect lighting.",
        "No text generation on the image — first version is image only.",
    ]
    if brand.get("negative_rules"):
        forbidden_lines.append(f"Brand-specific: {brand['negative_rules']}")
    parts.append("[Forbidden]\n" + "\n".join(forbidden_lines))

    # [Brand Guidance]
    if brand.get("prompt_rules"):
        parts.append(f"[Brand Guidance]\n{brand['prompt_rules']}")

    return "\n\n".join(parts)


# ── OpenAI API calls ──────────────────────────────────────────────────
def _openai_generate(prompt, size, quality, model):
    """Text-to-image via /v1/images/generations."""
    import requests as _req
    payload = {"model": model, "prompt": prompt, "n": 1, "size": size}
    if "gpt-image-2" in model:
        payload["quality"] = quality
    r = _req.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload, timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:400]}")
    data = r.json()
    item = data["data"][0]
    usage = data.get("usage", {})
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"]), usage
    return _req.get(item["url"], timeout=30).content, usage


def _openai_edit(prompt, img_bytes, quality, model):
    """Image-to-image via /v1/images/edits (1024x1024 only)."""
    import requests as _req
    # Convert to RGBA PNG
    try:
        from PIL import Image as PILImage
        pil = PILImage.open(io.BytesIO(img_bytes)).convert("RGBA")
        buf = io.BytesIO(); pil.save(buf, format="PNG"); png = buf.getvalue()
    except Exception:
        png = img_bytes
    r = _req.post(
        "https://api.openai.com/v1/images/edits",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        files={"image": ("product.png", io.BytesIO(png), "image/png")},
        data={"model": model, "prompt": prompt, "n": "1", "size": "1024x1024"},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI edits {r.status_code}: {r.text[:400]}")
    data = r.json()
    item = data["data"][0]
    usage = data.get("usage", {})
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"]), usage
    return _req.get(item["url"], timeout=30).content, usage


# ── Routes ────────────────────────────────────────────────────────────
@ai_images_bp.route("/admin/ai-images")
def admin_ai_images():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next="/admin/ai-images", error=None)
    brands = _get_all_brands()
    return render_template_string(
        AI_IMAGES_HTML, key=key, brands=brands,
        presets=_PRESETS, size_labels=_SIZE_LABELS,
    )


@ai_images_bp.route("/api/ai-images/prompt", methods=["POST"])
def api_ai_prompt():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    d = request.get_json(force=True) or {}
    try:
        image_type = d.get("image_type", "shopee_main")
        size       = d.get("size", "1024x1024")
        prompt = build_image_prompt(
            brand_key=d.get("brand_key",""),
            product_name=d.get("product_name",""),
            sku=d.get("sku",""),
            category=d.get("category",""),
            specs=d.get("specs",""),
            material=d.get("material",""),
            color=d.get("color",""),
            selling_points=d.get("selling_points",""),
            image_type=image_type,
            user_request=d.get("user_request",""),
            size=size,
        )
        print("[AI_IMAGES] prompt only, no OpenAI image API call", file=sys.stderr)
        task_id = _create_task({
            "brand_key": d.get("brand_key",""),
            "product_name": d.get("product_name",""),
            "sku": d.get("sku",""),
            "image_type": image_type,
            "user_request": d.get("user_request",""),
            "final_prompt": prompt,
            "model": IMAGE_MODEL, "size": size, "quality": "", "output_format": "",
        }, status="prompt_draft")
        return jsonify({"ok": True, "prompt": prompt, "task_id": task_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# 4 組方案預覽 — 一次組出 A/B/C/D，不呼叫 OpenAI
_VARIANT_TYPES = [
    ("product_white_bg",  "A"),
    ("product_scene_bg",  "B"),
    ("website_banner",    "C"),
    ("fb_ad",             "D"),
]

@ai_images_bp.route("/api/ai-images/prompt-variants", methods=["POST"])
def api_ai_prompt_variants():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    d = request.get_json(force=True) or {}
    try:
        variants = []
        for vtype, letter in _VARIANT_TYPES:
            preset = _PRESETS.get(vtype, {})
            size   = preset.get("default_size", "1024x1024")
            prompt = build_image_prompt(
                brand_key=d.get("brand_key",""),
                product_name=d.get("product_name",""),
                sku=d.get("sku",""),
                category=d.get("category",""),
                specs=d.get("specs",""),
                material=d.get("material",""),
                color=d.get("color",""),
                selling_points=d.get("selling_points",""),
                image_type=vtype,
                user_request=d.get("user_request",""),
                size=size,
            )
            task_id = _create_task({
                "brand_key": d.get("brand_key",""),
                "product_name": d.get("product_name",""),
                "sku": d.get("sku",""),
                "image_type": vtype,
                "user_request": d.get("user_request",""),
                "final_prompt": prompt,
                "model": IMAGE_MODEL, "size": size, "quality": "", "output_format": "",
            }, status="prompt_draft")
            variants.append({
                "letter": letter,
                "type": vtype,
                "label": preset.get("label", vtype),
                "size": size,
                "prompt": prompt,
                "task_id": task_id,
            })
        print(f"[AI_IMAGES] prompt only, no OpenAI image API call (variants x{len(variants)})", file=sys.stderr)
        return jsonify({"ok": True, "variants": variants})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@ai_images_bp.route("/api/ai-images/generate", methods=["POST"])
def api_ai_generate():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized"}), 403

    is_mp = request.content_type and "multipart" in request.content_type
    if is_mp:
        d = request.form
        ref_file = request.files.get("reference_image")
        ref_bytes = ref_file.read() if ref_file and ref_file.filename else None
    else:
        d = request.get_json(force=True) or {}
        ref_bytes = None

    final_prompt = (d.get("final_prompt") or "").strip()
    if not final_prompt:
        return jsonify({"ok": False, "error": "請先選擇一組 Prompt 方案，或手動輸入 Prompt"})
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "error": "未設定 OPENAI_API_KEY"})

    brand_key    = d.get("brand_key", "")
    product_name = d.get("product_name", "")
    sku          = d.get("sku", "")
    image_type   = d.get("image_type", "shopee_main")
    user_request = d.get("user_request", "")
    size         = d.get("size", "1024x1024") or "1024x1024"
    quality      = d.get("quality", IMAGE_QUALITY) or "high"
    out_fmt      = d.get("output_format", "png") or "png"
    neg_prompt   = d.get("negative_prompt", "")
    model        = IMAGE_MODEL
    # ID of the prompt_draft task that was selected (to upgrade its status)
    try:
        draft_task_id = int(d.get("draft_task_id") or 0)
    except (ValueError, TypeError):
        draft_task_id = 0

    task_id = _create_task({
        "brand_key": brand_key, "product_name": product_name, "sku": sku,
        "image_type": image_type, "user_request": user_request,
        "final_prompt": final_prompt, "negative_prompt": neg_prompt,
        "model": model, "size": size, "quality": quality, "output_format": out_fmt,
    })

    try:
        print("[AI_IMAGES] generate image, OpenAI image API called", file=sys.stderr)
        if ref_bytes:
            img_bytes, usage = _openai_edit(final_prompt, ref_bytes, quality, model)
        else:
            img_bytes, usage = _openai_generate(final_prompt, size, quality, model)

        ext  = "jpg" if out_fmt == "jpeg" else out_fmt
        ts   = int(time.time())
        path = f"ai-images/{brand_key or 'general'}/{image_type}_{ts}.{ext}"
        mime = f"image/{out_fmt}"
        pub_url, err = upload_image_to_supabase(path, img_bytes, mime)
        if not pub_url:
            _update_task(task_id, "failed", error=f"Upload failed: {err}")
            return jsonify({"ok": False, "error": f"圖片上傳失敗: {err}"})

        _update_task(task_id, "generated",
                     output_urls=[pub_url], supabase_paths=[path], usage=usage)
        # Upgrade the originating prompt_draft task to generated too
        if draft_task_id:
            _update_task(draft_task_id, "generated",
                         output_urls=[pub_url], supabase_paths=[path], usage=usage)
        return jsonify({"ok": True, "url": pub_url, "task_id": task_id, "usage": usage})

    except Exception as e:
        msg = str(e)
        print(f"[AI_IMAGES] generate error: {msg}", file=sys.stderr)
        _update_task(task_id, "failed", error=msg)
        return jsonify({"ok": False, "error": msg})


@ai_images_bp.route("/api/ai-images/tasks", methods=["GET"])
def api_ai_tasks():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    bk = request.args.get("brand_key", "")
    return jsonify({"ok": True, "tasks": _get_tasks(bk or None)})


@ai_images_bp.route("/api/ai-images/tasks/<int:task_id>", methods=["GET"])
def api_ai_task_get(task_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    t = _get_task(task_id)
    if not t:
        return jsonify({"ok": False, "error": "不存在"}), 404
    return jsonify({"ok": True, "task": t})


@ai_images_bp.route("/api/ai-images/tasks/<int:task_id>/regenerate", methods=["POST"])
def api_ai_regenerate(task_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    t = _get_task(task_id)
    if not t:
        return jsonify({"ok": False, "error": "任務不存在"}), 404
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "error": "未設定 OPENAI_API_KEY"})
    model = IMAGE_MODEL
    new_id = _create_task({
        "brand_key": t["brand_key"], "product_name": t["product_name"], "sku": t["sku"],
        "image_type": t["image_type"], "user_request": t["user_request"],
        "final_prompt": t["final_prompt"], "negative_prompt": t["negative_prompt"],
        "model": model, "size": t["size"] or "1024x1024",
        "quality": t["quality"] or "high", "output_format": t["output_format"] or "png",
    })
    try:
        img_bytes, usage = _openai_generate(
            t["final_prompt"], t["size"] or "1024x1024", t["quality"] or "high", model
        )
        ext  = "jpg" if (t["output_format"] or "png") == "jpeg" else (t["output_format"] or "png")
        ts   = int(time.time())
        path = f"ai-images/{t['brand_key'] or 'general'}/{t['image_type']}_{ts}.{ext}"
        mime = f"image/{t['output_format'] or 'png'}"
        pub_url, err = upload_image_to_supabase(path, img_bytes, mime)
        if not pub_url:
            _update_task(new_id, "failed", error=f"Upload failed: {err}")
            return jsonify({"ok": False, "error": f"上傳失敗: {err}"})
        _update_task(new_id, "generated", output_urls=[pub_url], supabase_paths=[path], usage=usage)
        return jsonify({"ok": True, "url": pub_url, "task_id": new_id})
    except Exception as e:
        msg = str(e)
        _update_task(new_id, "failed", error=msg)
        return jsonify({"ok": False, "error": msg})


# ── HTML Template ─────────────────────────────────────────────────────
AI_IMAGES_HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 圖片中心</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f4f6;color:#1a1a1a;min-height:100vh;font-size:14px}
.topbar{background:#fff;border-bottom:1px solid #e5e5e5;padding:0 20px;height:50px;display:flex;align-items:center;gap:8px;position:sticky;top:0;z-index:200}
.topbar a{color:#666;text-decoration:none;font-size:13px}.topbar a:hover{color:#000}
.topbar .sep{color:#d0d0d0}.topbar h1{font-size:14px;font-weight:700}
/* Flow bar */
.flow-bar{background:#fff;border-bottom:1px solid #e5e5e5;padding:8px 20px;display:flex;align-items:center;gap:6px;flex-wrap:wrap;position:sticky;top:50px;z-index:100}
.flow-step{font-size:12px;color:#aaa;font-weight:500;padding:4px 10px;border-radius:20px;background:#f4f4f6;white-space:nowrap}
.flow-step.active{color:#1a1a1a;background:#e0e0e0;font-weight:700}
.flow-step .bfree{background:#d4edda;color:#155724;padding:1px 5px;border-radius:6px;font-size:10px;font-weight:700;margin-left:3px}
.flow-step .bcost{background:#fff3cd;color:#856404;padding:1px 5px;border-radius:6px;font-size:10px;font-weight:700;margin-left:3px}
.flow-arrow{color:#ccc;font-size:12px}
/* Main grid */
.main-grid{display:grid;grid-template-columns:30fr 40fr 30fr;align-items:start}
/* Left column */
.col-left{background:#fff;border-right:1px solid #e5e5e5;padding:14px;display:flex;flex-direction:column;gap:10px;position:sticky;top:96px;max-height:calc(100vh - 96px);overflow-y:auto}
.lcard{border:1px solid #ebebeb;border-radius:9px;padding:12px;background:#fafafa}
.lcard-title{font-size:11px;font-weight:700;color:#555;letter-spacing:.6px;text-transform:uppercase;margin-bottom:9px;display:flex;align-items:center;gap:5px}
.step-num{width:17px;height:17px;background:#1a1a1a;color:#fff;border-radius:50%;font-size:10px;display:inline-flex;align-items:center;justify-content:center;font-weight:800;flex-shrink:0}
.fl{display:block;font-size:11px;font-weight:600;color:#555;margin-bottom:2px;margin-top:7px}
.fl:first-of-type{margin-top:0}
select,input[type=text],textarea{width:100%;border:1px solid #e0e0e0;border-radius:6px;padding:6px 8px;font-size:12px;font-family:inherit;outline:none;transition:border .15s;background:#fff;color:#1a1a1a}
select:focus,input:focus,textarea:focus{border-color:#1a1a1a}
textarea{resize:vertical;min-height:48px;line-height:1.5}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.ref-zone{border:1.5px dashed #d0d0d0;border-radius:7px;padding:10px;text-align:center;cursor:pointer;position:relative;background:#fff;transition:.15s;margin-top:2px}
.ref-zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.ref-zone:hover{border-color:#888;background:#f9f9f9}
.ref-icon{font-size:16px}.ref-hint{font-size:10px;color:#aaa;margin-top:1px}.ref-name{font-size:10px;font-weight:600;color:#333;margin-top:1px;word-break:break-all}
/* Middle column */
.col-mid{padding:14px;display:flex;flex-direction:column;gap:10px;overflow-y:auto}
.btn-variants-main{width:100%;padding:13px;background:#1a1a1a;color:#fff;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;transition:.15s;display:flex;align-items:center;justify-content:center;gap:8px}
.btn-variants-main:hover{background:#333}.btn-variants-main:disabled{background:#999;cursor:not-allowed}
.mid-info{font-size:12px;color:#3949ab;background:#e8eaf6;border-radius:8px;padding:10px 13px;line-height:1.65}
.variant-list{display:flex;flex-direction:column;gap:10px}
.vcard{border:2px solid #e8e8e8;border-radius:10px;padding:13px;background:#fff;transition:border-color .15s}
.vcard:hover{border-color:#bbb}
.vcard.selected{border-color:#e53935;background:#fff9f9}
.vcard-header{display:flex;align-items:center;gap:7px;margin-bottom:7px}
.vcard-letter{font-size:10px;font-weight:800;background:#1a1a1a;color:#fff;border-radius:4px;padding:2px 6px;flex-shrink:0}
.vcard-label{font-size:13px;font-weight:700;flex:1}
.vcard-size{font-size:10px;color:#888;background:#f0f0f0;padding:2px 6px;border-radius:4px;white-space:nowrap}
.vcard-meta{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:7px}
.vtag{font-size:10px;color:#555;background:#f0f0f0;padding:2px 6px;border-radius:8px}
.vtag-free{background:#fff3e0;color:#e65100;font-weight:700}
.vcard-preview{display:none;font-size:10px;font-family:Consolas,monospace;color:#555;background:#f7f7f7;border-radius:6px;padding:8px;max-height:120px;overflow-y:auto;line-height:1.5;white-space:pre-wrap;word-break:break-all;margin-bottom:7px}
.vcard-preview.open{display:block}
.vcard-btns{display:flex;gap:7px}
.btn-view{flex:1;padding:5px;border:1px solid #d0d0d0;background:#fff;border-radius:6px;font-size:11px;cursor:pointer;transition:.15s;font-weight:500}
.btn-view:hover{border-color:#888}
.btn-pick{flex:2;padding:5px;border:1.5px solid #1a1a1a;background:#fff;border-radius:6px;font-size:12px;font-weight:700;cursor:pointer;transition:.15s}
.btn-pick:hover,.vcard.selected .btn-pick{background:#1a1a1a;color:#fff}
/* Right column */
.col-right{padding:14px;border-left:1px solid #e5e5e5;background:#fff;display:flex;flex-direction:column;gap:10px;position:sticky;top:96px;max-height:calc(100vh - 96px);overflow-y:auto}
.rp-section{display:none}
.rp-section.active{display:flex;flex-direction:column;gap:10px}
.rp-empty{align-items:center;justify-content:center;min-height:260px;text-align:center;border:2px dashed #e0e0e0;border-radius:12px;padding:28px;background:#fafafa}
.rp-empty-icon{font-size:34px;margin-bottom:8px}
.rp-empty-title{font-size:14px;font-weight:700;color:#bbb;margin-bottom:6px}
.rp-empty-hint{font-size:12px;color:#ccc;line-height:1.7}
.rp-ready-card{border:1.5px solid #e5e5e5;border-radius:10px;padding:15px;display:flex;flex-direction:column;gap:9px}
.rp-chosen-label{font-size:10px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.5px}
.rp-chosen-name{font-size:15px;font-weight:700}
.rp-chosen-meta{font-size:12px;color:#666}
.rp-cost{font-size:12px;font-weight:700;background:#f4f4f6;padding:7px 10px;border-radius:7px;display:flex;align-items:center;gap:6px}
.rp-warn{font-size:11px;color:#e65100;background:#fff8f0;border-radius:6px;padding:7px 10px;line-height:1.5}
.btn-gen{width:100%;padding:13px;background:#e53935;color:#fff;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;transition:.15s;display:flex;align-items:center;justify-content:center;gap:8px}
.btn-gen:hover{background:#c62828}.btn-gen:disabled{background:#bbb;cursor:not-allowed}
.spin{width:15px;height:15px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;display:none}
@keyframes spin{to{transform:rotate(360deg)}}
.err-box{background:#fff3f3;border:1px solid #ffd0d0;border-radius:7px;padding:8px;font-size:12px;color:#c00;display:none}
.result-img{width:100%;border-radius:10px;border:1px solid #eee;object-fit:contain;display:block;max-height:380px}
.result-actions{display:flex;gap:7px;flex-wrap:wrap}
.btn-act{padding:7px 13px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;border:1.5px solid #1a1a1a;background:#fff;color:#1a1a1a;text-decoration:none;display:inline-flex;align-items:center;gap:4px;transition:.15s}
.btn-act:hover{background:#1a1a1a;color:#fff}
.btn-act.filled{background:#1a1a1a;color:#fff}.btn-act.filled:hover{background:#333}
/* Collapsible prompt edit */
.prompt-collapse-wrap{background:#fff;border-top:1px solid #e5e5e5;border-bottom:1px solid #e5e5e5}
.prompt-collapse-btn{width:100%;padding:11px 20px;background:none;border:none;cursor:pointer;text-align:left;font-size:13px;font-weight:700;display:flex;align-items:center;gap:7px;color:#1a1a1a;transition:background .15s}
.prompt-collapse-btn:hover{background:#f5f5f5}
.collapse-hint{font-size:11px;color:#aaa;font-weight:400}
.prompt-collapse-body{padding:0 20px 14px}
.prompt-area{width:100%;min-height:180px;border:1px solid #e0e0e0;border-radius:8px;padding:11px;font-size:12px;font-family:Consolas,monospace;line-height:1.6;resize:vertical;outline:none;background:#fafafa;color:#333}
.prompt-area:focus{border-color:#1a1a1a;background:#fff}
.prompt-actions{display:flex;gap:7px;margin-top:7px;align-items:center}
.btn-sm{padding:6px 11px;border:1px solid #ddd;border-radius:6px;font-size:12px;cursor:pointer;background:#fff;transition:.15s;font-weight:500}
.btn-sm:hover{border-color:#888}
/* History */
.history-wrap{padding:18px 20px;background:#f4f4f6}
.history-hd{font-size:14px;font-weight:700;margin-bottom:11px;display:flex;align-items:center;justify-content:space-between}
.history-hd button{padding:5px 11px;background:#fff;border:1px solid #ddd;border-radius:6px;font-size:12px;cursor:pointer;transition:.15s}
.history-hd button:hover{border-color:#999}
.gallery-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:7px;margin-bottom:14px}
.g-item{aspect-ratio:1;border-radius:8px;overflow:hidden;border:1px solid #e5e5e5;background:#e8e8e8;position:relative}
.g-item img{width:100%;height:100%;object-fit:cover;display:block}
.g-item .g-overlay{position:absolute;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;opacity:0;transition:.2s}
.g-item:hover .g-overlay{opacity:1}
.g-overlay a{color:#fff;font-size:18px;text-decoration:none}
.htable{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;border:1px solid #e8e8e8;font-size:12px}
.htable th{background:#f7f7f7;font-size:11px;font-weight:700;color:#888;padding:8px 12px;text-align:left;border-bottom:1px solid #eee}
.htable td{padding:7px 12px;border-bottom:1px solid #f5f5f5;vertical-align:middle}
.htable tr:last-child td{border-bottom:none}.htable tr:hover td{background:#fafafa}
.h-thumb{width:42px;height:42px;object-fit:cover;border-radius:6px;border:1px solid #eee}
.sd{background:#e8f5e9;color:#2e7d32;font-weight:700;font-size:10px;padding:2px 7px;border-radius:8px}
.sf{background:#ffebee;color:#c62828;font-weight:700;font-size:10px;padding:2px 7px;border-radius:8px}
.sp{background:#fff3e0;color:#e65100;font-weight:700;font-size:10px;padding:2px 7px;border-radius:8px}
.sg{background:#e8eaf6;color:#3949ab;font-weight:700;font-size:10px;padding:2px 7px;border-radius:8px}
.empty-history{text-align:center;padding:32px;color:#bbb;font-size:13px}
@media(max-width:900px){
  .main-grid{grid-template-columns:1fr}
  .col-left,.col-right{position:static;max-height:none;border:none;border-bottom:1px solid #e5e5e5}
  .col-right{border-left:none}
  .gallery-grid{grid-template-columns:repeat(3,1fr)}
  .flow-bar{position:static;display:none}
}
</style>
</head>
<body>

<div class="topbar">
  <a href="/admin?key={{ key }}">← 後台</a><span class="sep">/</span>
  <h1>AI 圖片中心</h1>
</div>

<div class="flow-bar">
  <div class="flow-step active">① 填商品資料</div>
  <div class="flow-arrow">→</div>
  <div class="flow-step">② 產生 4 組 Prompt<span class="bfree">不扣點</span></div>
  <div class="flow-arrow">→</div>
  <div class="flow-step">③ 選定方案</div>
  <div class="flow-arrow">→</div>
  <div class="flow-step">④ 生成圖片<span class="bcost">才扣點</span></div>
</div>

<div class="main-grid">

<!-- ══ LEFT COLUMN ══ -->
<div class="col-left">

  <div class="lcard">
    <div class="lcard-title"><span class="step-num">1</span>品牌 &amp; 設定</div>
    <label class="fl">品牌</label>
    <select id="brandSel" onchange="onBrandChange()">
      <option value="">— 不選品牌 —</option>
      {% for b in brands %}
      <option value="{{ b.brand_key }}">{{ b.name }}</option>
      {% endfor %}
    </select>
    <label class="fl">圖片用途</label>
    <select id="imageType" onchange="onTypeChange()">
      {% for pt, pv in presets.items() %}
      <option value="{{ pt }}" {% if pt=='shopee_main' %}selected{% endif %}>{{ pv.label }}</option>
      {% endfor %}
    </select>
    <div class="g2" style="margin-top:7px">
      <div>
        <label class="fl" style="margin-top:0">尺寸</label>
        <select id="genSize">
          {% for s, sl in size_labels.items() %}
          <option value="{{ s }}" {% if s=='1024x1024' %}selected{% endif %}>{{ s }}</option>
          {% endfor %}
        </select>
      </div>
      <div>
        <label class="fl" style="margin-top:0">品質</label>
        <select id="genQuality">
          <option value="high" selected>high（高）</option>
          <option value="auto">auto</option>
        </select>
      </div>
    </div>
    <label class="fl">輸出格式</label>
    <select id="genFormat">
      <option value="png" selected>PNG</option>
      <option value="webp">WebP</option>
      <option value="jpeg">JPEG</option>
    </select>
  </div>

  <div class="lcard">
    <div class="lcard-title"><span class="step-num">2</span>商品資訊</div>
    <label class="fl">商品名稱 <span style="color:#e53935">*</span></label>
    <input type="text" id="productName" placeholder="例：JS 高架床 黑色 單人 6尺">
    <div class="g2">
      <div><label class="fl">SKU</label><input type="text" id="sku" placeholder="JS-B001"></div>
      <div><label class="fl">分類</label><input type="text" id="category" placeholder="高架床"></div>
    </div>
    <label class="fl">規格 / 尺寸</label>
    <input type="text" id="specs" placeholder="例：單人 90×190cm，床高 150cm">
    <div class="g2">
      <div><label class="fl">材質</label><input type="text" id="material" placeholder="方管鐵管"></div>
      <div><label class="fl">顏色</label><input type="text" id="color" placeholder="消光黑"></div>
    </div>
    <label class="fl">賣點（逗號分隔）</label>
    <textarea id="sellingPoints" rows="2" placeholder="強化螺絲、承重150kg、台灣設計"></textarea>
  </div>

  <div class="lcard">
    <div class="lcard-title"><span class="step-num">3</span>需求 &amp; 參考圖</div>
    <label class="fl">這次需求說明</label>
    <textarea id="userRequest" rows="2" placeholder="例：北歐風客廳，木地板，窗邊自然光"></textarea>
    <label class="fl">參考圖（選填，上傳後套用 AI 編輯）</label>
    <div class="ref-zone" id="refZone">
      <input type="file" id="refImg" accept="image/*" onchange="onRefFile(this)">
      <div class="ref-icon">🖼️</div>
      <div class="ref-hint">點擊或拖曳參考圖</div>
      <div class="ref-name" id="refName"></div>
    </div>
  </div>

</div>

<!-- ══ MIDDLE COLUMN ══ -->
<div class="col-mid">

  <button class="btn-variants-main" id="variantsBtn" onclick="doVariants()">
    <div class="spin" id="vBtnSpin" style="border-color:rgba(255,255,255,.3);border-top-color:#fff"></div>
    <span id="vBtnText">✦ 產生 4 組 Prompt 方案（不扣點）</span>
  </button>

  <div class="mid-info">
    ℹ️ 點擊上方按鈕，一次生成 A/B/C/D 四組針對不同用途優化的 Prompt。<br>
    生成方案<strong>不消耗圖片點數</strong>，選定方案後按「開始生成圖片」才計費。
  </div>

  <div class="variant-list" id="variantList"></div>

</div>

<!-- ══ RIGHT COLUMN ══ -->
<div class="col-right">

  <div class="rp-section active rp-empty" id="rp-empty">
    <div class="rp-empty-icon">🎨</div>
    <div class="rp-empty-title">尚未生成</div>
    <div class="rp-empty-hint">
      左側填寫商品資料<br>
      中間點擊產生 4 組方案<br>
      選定方案後點擊生成圖片
    </div>
  </div>

  <div class="rp-section" id="rp-ready">
    <div class="rp-ready-card">
      <div class="rp-chosen-label">已選方案</div>
      <div class="rp-chosen-name" id="rpReadyName">—</div>
      <div class="rp-chosen-meta" id="rpReadyMeta"></div>
      <div class="rp-cost">💰 預估：1 點（生成後才扣除）</div>
      <div class="rp-warn">⚠️ 按下後才會呼叫圖片 API 並產生成本</div>
      <button class="btn-gen" id="genBtn" onclick="doGenerate()">
        <div class="spin" id="genSpin"></div>
        <span id="genBtnText">▶ 開始生成圖片</span>
      </button>
      <div class="err-box" id="genErr"></div>
    </div>
  </div>

  <div class="rp-section" id="rp-done">
    <img class="result-img" id="resultImg" src="" alt="生成結果">
    <div class="result-actions">
      <a class="btn-act filled" id="dlBtn" download="ai_result.png">⬇ 下載</a>
      <button class="btn-act" onclick="copyResultPrompt()">📋 複製 Prompt</button>
      <button class="btn-act" onclick="doRegenerate()">🔄 重新生成</button>
    </div>
  </div>

</div>
</div>

<!-- ══ COLLAPSIBLE PROMPT EDIT ══ -->
<div class="prompt-collapse-wrap">
  <button class="prompt-collapse-btn" onclick="togglePromptEdit()">
    <span id="collapseIcon">▶</span>
    已選 Prompt / 手動修改
    <span class="collapse-hint">（默認收合，展開可查看或手動修改）</span>
  </button>
  <div class="prompt-collapse-body" id="promptCollapseBody" style="display:none">
    <textarea class="prompt-area" id="promptArea" placeholder="選擇方案後自動填入，也可直接在此輸入 Prompt..."></textarea>
    <div class="prompt-actions">
      <button class="btn-sm" onclick="doBuildPrompt()">⚙ 重新組裝</button>
      <button class="btn-sm" onclick="copyPromptText()">📋 複製</button>
      <button class="btn-sm" onclick="document.getElementById('promptArea').value=''">✕ 清空</button>
      <span id="charCount" style="margin-left:auto;font-size:11px;color:#aaa"></span>
    </div>
  </div>
</div>

<!-- ══ HISTORY ══ -->
<div class="history-wrap">
  <div class="history-hd">
    🗂 歷史紀錄
    <button onclick="loadHistory()">↻ 重新整理</button>
  </div>
  <div class="gallery-grid" id="galleryGrid"></div>
  <div id="historyArea"><div class="empty-history">尚無生成紀錄</div></div>
</div>

<script>
const KEY  = '{{ key }}';
const PMAP = {{ presets | tojson }};
let currentTaskId   = null;
let selectedDraftId = null;

// ── Right-panel state machine ────────────────────────────────────────────
function setRpState(s){
  const states = ['empty','ready','done'];
  states.forEach(name=>{
    const el = document.getElementById('rp-'+name);
    if(!el) return;
    let cls = 'rp-section';
    if(name === s)   cls += ' active';
    if(name === 'empty') cls += ' rp-empty';
    el.className = cls;
  });
}

// ── Collapsible prompt ───────────────────────────────────────────────────
function togglePromptEdit(){
  const body = document.getElementById('promptCollapseBody');
  const icon = document.getElementById('collapseIcon');
  const open = body.style.display === 'block';
  body.style.display = open ? 'none' : 'block';
  icon.textContent   = open ? '▶' : '▼';
}
function openPromptEdit(){
  document.getElementById('promptCollapseBody').style.display = 'block';
  document.getElementById('collapseIcon').textContent = '▼';
}

// ── Form data ────────────────────────────────────────────────────────────
function formData(){
  return {
    brand_key:      document.getElementById('brandSel').value,
    product_name:   document.getElementById('productName').value,
    sku:            document.getElementById('sku').value,
    category:       document.getElementById('category').value,
    specs:          document.getElementById('specs').value,
    material:       document.getElementById('material').value,
    color:          document.getElementById('color').value,
    selling_points: document.getElementById('sellingPoints').value,
    image_type:     document.getElementById('imageType').value,
    user_request:   document.getElementById('userRequest').value,
    size:           document.getElementById('genSize').value,
  };
}

// ── Brand / type change ──────────────────────────────────────────────────
function onBrandChange(){ loadHistory(); }
function onTypeChange(){
  const t  = document.getElementById('imageType').value;
  const sz = PMAP[t] && PMAP[t].default_size;
  if(sz) document.getElementById('genSize').value = sz;
}

// ── Ref image ────────────────────────────────────────────────────────────
const rz = document.getElementById('refZone');
rz.addEventListener('dragover', e=>{e.preventDefault();rz.style.borderColor='#666';});
rz.addEventListener('dragleave', ()=>rz.style.borderColor='');
rz.addEventListener('drop', e=>{
  e.preventDefault(); rz.style.borderColor='';
  const f = e.dataTransfer.files[0];
  if(f && f.type.startsWith('image/')){
    const dt = new DataTransfer(); dt.items.add(f);
    document.getElementById('refImg').files = dt.files;
    document.getElementById('refName').textContent = f.name;
  }
});
function onRefFile(i){ document.getElementById('refName').textContent = i.files[0] ? i.files[0].name : ''; }

// ── Generate 4 variants ──────────────────────────────────────────────────
async function doVariants(){
  const btn  = document.getElementById('variantsBtn');
  const spin = document.getElementById('vBtnSpin');
  const txt  = document.getElementById('vBtnText');
  btn.disabled=true; spin.style.display='block'; txt.textContent='組裝中…';
  document.getElementById('variantList').innerHTML = '';
  try{
    const r = await fetch('/api/ai-images/prompt-variants?key='+KEY,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(formData()),
    });
    const j = await r.json();
    if(!j.ok){ alert('方案生成失敗: '+(j.error||'')); return; }
    const list = document.getElementById('variantList');
    list.innerHTML = j.variants.map(function(v){
      const preset = PMAP[v.type] || {};
      const platform = preset.platform || '';
      const vj = JSON.stringify(v).replace(/\\/g,'\\\\').replace(/"/g,'&quot;');
      const promptHtml = v.prompt.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return '<div class="vcard" id="vc_'+v.type+'">'
        +'<div class="vcard-header">'
        +'<span class="vcard-letter">'+v.letter+'</span>'
        +'<span class="vcard-label">'+v.label+'</span>'
        +'<span class="vcard-size">'+v.size+'</span>'
        +'</div>'
        +'<div class="vcard-meta">'
        +(platform?'<span class="vtag">'+platform.slice(0,35)+'</span>':'')
        +'<span class="vtag vtag-free">此方案 0 點</span>'
        +'</div>'
        +'<div class="vcard-preview" id="vp_'+v.type+'">'+promptHtml+'</div>'
        +'<div class="vcard-btns">'
        +'<button class="btn-view" onclick="toggleVP(''+v.type+'',this)">查看完整 Prompt</button>'
        +'<button class="btn-pick" onclick='pickVariant('+vj+')'>選擇此方案 →</button>'
        +'</div></div>';
    }).join('');
    loadHistory();
  }catch(e){ alert('連線錯誤: '+e.message); }
  btn.disabled=false; spin.style.display='none'; txt.textContent='✦ 產生 4 組 Prompt 方案（不扣點）';
}

function toggleVP(type, btn){
  const el = document.getElementById('vp_'+type);
  if(!el) return;
  el.classList.toggle('open');
  btn.textContent = el.classList.contains('open') ? '收起 ▲' : '查看完整 Prompt';
}

// ── Pick variant ──────────────────────────────────────────────────────────
function pickVariant(v){
  document.getElementById('promptArea').value = v.prompt;
  updateCharCount();
  openPromptEdit();
  document.getElementById('imageType').value = v.type;
  document.getElementById('genSize').value   = v.size;
  selectedDraftId = v.task_id || null;
  document.querySelectorAll('.vcard').forEach(function(c){c.classList.remove('selected');});
  var card = document.getElementById('vc_'+v.type);
  if(card) card.classList.add('selected');
  document.getElementById('rpReadyName').textContent = v.letter+' — '+v.label;
  document.getElementById('rpReadyMeta').textContent = '尺寸：'+v.size;
  setRpState('ready');
}

// ── Build single prompt ───────────────────────────────────────────────────
async function doBuildPrompt(){
  try{
    const r = await fetch('/api/ai-images/prompt?key='+KEY,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(formData()),
    });
    const j = await r.json();
    if(j.ok){
      document.getElementById('promptArea').value = j.prompt;
      updateCharCount();
      openPromptEdit();
      selectedDraftId = j.task_id || null;
      loadHistory();
    } else alert('Prompt 組裝失敗: '+(j.error||''));
  }catch(e){ alert('連線錯誤: '+e.message); }
}

function updateCharCount(){
  const v = document.getElementById('promptArea').value;
  document.getElementById('charCount').textContent = v.length+' 字元';
}
document.getElementById('promptArea').addEventListener('input', updateCharCount);

// ── Generate image ────────────────────────────────────────────────────────
async function doGenerate(){
  const prompt      = document.getElementById('promptArea').value.trim();
  const productName = document.getElementById('productName').value.trim();
  const brandKey    = document.getElementById('brandSel').value;
  const imageType   = document.getElementById('imageType').value;

  if(!prompt){
    showGenErr('請先選擇一組 Prompt 方案，或手動輸入 Prompt');
    return;
  }
  var missing = [];
  if(!productName) missing.push('商品名稱');
  if(!brandKey)    missing.push('品牌');
  if(!imageType)   missing.push('圖片用途');
  if(missing.length){
    showGenErr('請填寫必填欄位：'+missing.join('、'));
    return;
  }

  const btn  = document.getElementById('genBtn');
  const spin = document.getElementById('genSpin');
  const txt  = document.getElementById('genBtnText');
  btn.disabled=true; spin.style.display='block'; txt.textContent='生成中…';
  hideGenErr();

  const fd = new FormData();
  fd.append('final_prompt',  prompt);
  fd.append('brand_key',     brandKey);
  fd.append('product_name',  productName);
  fd.append('sku',           document.getElementById('sku').value);
  fd.append('image_type',    imageType);
  fd.append('user_request',  document.getElementById('userRequest').value);
  fd.append('size',          document.getElementById('genSize').value);
  fd.append('quality',       document.getElementById('genQuality').value);
  fd.append('output_format', document.getElementById('genFormat').value);
  if(selectedDraftId) fd.append('draft_task_id', selectedDraftId);
  const refFile = document.getElementById('refImg').files[0];
  if(refFile) fd.append('reference_image', refFile);

  try{
    const r = await fetch('/api/ai-images/generate?key='+KEY,{method:'POST',body:fd});
    const j = await r.json();
    if(j.ok){
      currentTaskId   = j.task_id;
      selectedDraftId = null;
      document.getElementById('resultImg').src = j.url;
      document.getElementById('dlBtn').href    = j.url;
      setRpState('done');
      loadHistory();
    } else {
      showGenErr('生成失敗：'+(j.error||'未知錯誤'));
    }
  }catch(e){
    showGenErr('連線錯誤: '+e.message);
  }
  btn.disabled=false; spin.style.display='none'; txt.textContent='▶ 開始生成圖片';
}

function showGenErr(msg){
  const eb = document.getElementById('genErr');
  eb.textContent=msg; eb.style.display='block';
}
function hideGenErr(){
  document.getElementById('genErr').style.display='none';
}

// ── Regenerate ────────────────────────────────────────────────────────────
async function doRegenerate(){
  if(!currentTaskId){ doGenerate(); return; }
  const r = await fetch('/api/ai-images/tasks/'+currentTaskId+'/regenerate?key='+KEY,{method:'POST'});
  const j = await r.json();
  if(j.ok){
    currentTaskId = j.task_id;
    document.getElementById('resultImg').src = j.url;
    document.getElementById('dlBtn').href    = j.url;
    setRpState('done');
    loadHistory();
  } else alert('重新生成失敗: '+(j.error||''));
}

// ── Copy ──────────────────────────────────────────────────────────────────
function copyPromptText(){
  const v = document.getElementById('promptArea').value;
  navigator.clipboard.writeText(v).then(function(){ alert('Prompt 已複製！'); });
}
function copyResultPrompt(){ copyPromptText(); }

// ── History ───────────────────────────────────────────────────────────────
async function loadHistory(){
  const bk  = document.getElementById('brandSel').value;
  const url = '/api/ai-images/tasks?key='+KEY+(bk?'&brand_key='+bk:'');
  try{
    const r = await fetch(url);
    const j = await r.json();
    renderHistory(j.tasks||[]);
  }catch(e){ console.error(e); }
}

function renderHistory(tasks){
  // Gallery: up to 6 generated images
  const genTasks = tasks.filter(function(t){return t.thumb;});
  const gEl = document.getElementById('galleryGrid');
  if(genTasks.length){
    gEl.innerHTML = genTasks.slice(0,6).map(function(t){
      return '<div class="g-item"><img src="'+t.thumb+'" loading="lazy" alt="'+( t.product_name||'')+'">'
        +'<div class="g-overlay"><a href="'+t.thumb+'" download>⬇</a></div></div>';
    }).join('');
  } else {
    gEl.innerHTML = '';
  }

  // Table
  const el = document.getElementById('historyArea');
  if(!tasks.length){ el.innerHTML='<div class="empty-history">尚無生成紀錄</div>'; return; }
  const SC = {generated:'sd',failed:'sf',pending:'sp',prompt_draft:'sg'};
  const ST = {generated:'已生成',failed:'失敗',pending:'處理中',prompt_draft:'草稿'};
  let html = '<table class="htable"><thead><tr>'
    +'<th>縮圖</th><th>品牌</th><th>商品</th><th>用途</th>'
    +'<th>尺寸</th><th>狀態</th><th>時間</th><th>操作</th>'
    +'</tr></thead><tbody>';
  tasks.forEach(function(t){
    const sc  = SC[t.status]||'sp';
    const st  = ST[t.status]||t.status;
    const lbl = (PMAP[t.image_type]&&PMAP[t.image_type].label)||t.image_type;
    const dt  = t.created_at ? new Date(t.created_at*1000).toLocaleString('zh-TW',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '-';
    const thumb = t.thumb
      ? '<img class="h-thumb" src="'+t.thumb+'" loading="lazy">'
      : '<div style="width:42px;height:42px;background:#f0f0f0;border-radius:6px"></div>';
    html += '<tr>'
      +'<td>'+thumb+'</td>'
      +'<td>'+(t.brand_key||'-')+'</td>'
      +'<td style="max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+(t.product_name||'-')+'</td>'
      +'<td>'+lbl+'</td>'
      +'<td style="font-size:11px;white-space:nowrap">'+(t.size||'-')+'</td>'
      +'<td><span class="'+sc+'">'+st+'</span>'
      +(t.error_message?'<br><span style="color:#999;font-size:10px">'+t.error_message.slice(0,40)+'</span>':'')
      +'</td>'
      +'<td style="font-size:11px;color:#999;white-space:nowrap">'+dt+'</td>'
      +'<td style="white-space:nowrap">'
      +(t.thumb?'<a href="'+t.thumb+'" download style="font-size:12px;color:#1a1a1a;text-decoration:none;font-weight:600;margin-right:7px">⬇</a>':'')
      +'<button onclick="regenFromHistory('+t.id+')" style="font-size:11px;border:1px solid #ddd;background:#fff;border-radius:5px;padding:3px 7px;cursor:pointer">🔄</button>'
      +'</td></tr>';
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

async function regenFromHistory(tid){
  const r = await fetch('/api/ai-images/tasks/'+tid+'/regenerate?key='+KEY,{method:'POST'});
  const j = await r.json();
  if(j.ok){
    currentTaskId = j.task_id;
    document.getElementById('resultImg').src = j.url;
    document.getElementById('dlBtn').href    = j.url;
    setRpState('done');
    loadHistory();
  } else alert('重新生成失敗: '+(j.error||''));
}

// ── Init ──────────────────────────────────────────────────────────────────
onTypeChange();
loadHistory();
</script>
</body>
</html>"""

# ── Init (run once at import) ─────────────────────────────────────────
_ait_migrate()
_seed_brands()
