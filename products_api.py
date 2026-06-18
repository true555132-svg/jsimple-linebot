"""
products_api.py — AI 商品搬運中心 Blueprint
從 app.py 拆出的所有商品相關路由與邏輯。
包含：/admin/products、/admin/products/store-scan、/admin/brand-settings、
      /api/products/*、/api/store-scan/*、/api/brand-profiles/*
LINE Bot / FB Messenger / CRM 完全不在此檔案中。
"""
import os, json, time, threading, urllib.request, io, base64
from flask import Blueprint, request, jsonify, render_template_string, Response

# ── 設定（從環境變數，與 app.py 一致）────────────────────────────
DATABASE_URL         = os.getenv("DATABASE_URL", "")
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
SUPABASE_URL         = os.getenv("SUPABASE_URL", "https://lrslleetqyaerstrlbap.supabase.co")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_BUCKET      = "chat-images"
GITHUB_TOKEN         = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO          = "true555132-svg/jsimple-linebot"
ADMIN_PASSWORD       = os.getenv("ADMIN_PASSWORD", "jsimple2024")

_db_lock = threading.Lock()

products_bp = Blueprint("products", __name__)

# ── DB 連線 ──────────────────────────────────────────────────────
def _pg_conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)

# ── Auth（與 app.py 保持一致，避免 circular import）──────────────
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

def check_auth():
    key = request.args.get("key", "")
    return key == ADMIN_PASSWORD, key

# ── Login HTML（複製自 app.py，避免 circular import）─────────────
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

# ── Upload helpers（複製自 app.py）───────────────────────────────
def upload_image_to_supabase(filename: str, data: bytes, content_type: str = "image/jpeg") -> tuple:
    if not SUPABASE_SERVICE_KEY:
        return "", "SUPABASE_SERVICE_KEY 未設定"
    try:
        upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
        req = urllib.request.Request(
            upload_url, data=data, method="POST",
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
            json.loads(r.read())
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

# ── 品牌設定 ──────────────────────────────────────────────────────
BRAND_PROFILES = {
    "jsimple": {
        "name": "JSIMPLE",
        "category": "高架床、系統家具",
        "style": "簡潔、功能導向、現代風格。強調空間利用、承重規格、材質安全。",
        "tone": "直接說明功能與規格，像設計師推薦，不像業務推銷。",
    },
    "lander": {
        "name": "朗德燈具",
        "category": "燈具、照明",
        "style": "質感、設計感、氛圍營造。強調光線效果、設計美感、節能規格（W數、流明）。",
        "tone": "有畫面感，讓人想像裝上後的居家氛圍。",
    },
    "filterbreath": {
        "name": "濾呼吸",
        "category": "空氣濾網、淨化設備",
        "style": "健康、數據導向、信任感。強調過濾效率（等級）、適用機型、更換週期。",
        "tone": "用具體數字說話，像健康產品的專業建議，不誇大。",
    },
}

# ── Brand profile DB helpers ──────────────────────────────────────
def _bp_all():
    if not DATABASE_URL:
        return [{"brand_key": k, **v, "custom_prompt": ""} for k, v in BRAND_PROFILES.items()]
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT brand_key,name,category,style,tone,custom_prompt FROM brand_profiles ORDER BY brand_key")
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"brand_key": r[0], "name": r[1], "category": r[2],
                 "style": r[3], "tone": r[4], "custom_prompt": r[5] or ""} for r in rows]
    except Exception:
        return [{"brand_key": k, **v, "custom_prompt": ""} for k, v in BRAND_PROFILES.items()]

def _bp_get(brand_key):
    if not DATABASE_URL:
        return BRAND_PROFILES.get(brand_key, {})
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("SELECT name,category,style,tone,custom_prompt FROM brand_profiles WHERE brand_key=%s", (brand_key,))
        row = cur.fetchone(); cur.close(); conn.close()
        if row:
            return {"name": row[0], "category": row[1], "style": row[2],
                    "tone": row[3], "custom_prompt": row[4] or ""}
    except Exception:
        pass
    return BRAND_PROFILES.get(brand_key, {})

def _bp_save(brand_key, name, category, style, tone, custom_prompt=""):
    if not DATABASE_URL:
        return False
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO brand_profiles(brand_key,name,category,style,tone,custom_prompt,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(brand_key) DO UPDATE
            SET name=%s,category=%s,style=%s,tone=%s,custom_prompt=%s,updated_at=%s
        """, (brand_key, name, category, style, tone, custom_prompt, time.time(),
              name, category, style, tone, custom_prompt, time.time()))
        conn.commit(); cur.close(); conn.close()
        return True
    except Exception as e:
        import sys; print(f"[BP Save] {e}", file=sys.stderr)
        return False

# ── Product job DB helpers ────────────────────────────────────────
def _pj_insert(url, platform, brand=""):
    if not DATABASE_URL:
        return None
    try:
        with _db_lock:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute(
                "INSERT INTO product_jobs (url,platform,brand,status,created_at,updated_at) VALUES (%s,%s,%s,'pending',%s,%s) RETURNING id",
                (url, platform, brand, time.time(), time.time())
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
            "SELECT id,url,platform,status,raw_title,ai_name,ai_desc,ai_keywords,error_msg,created_at,brand FROM product_jobs ORDER BY created_at DESC LIMIT %s",
            (limit,)
        )
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id":r[0],"url":r[1],"platform":r[2],"status":r[3],"raw_title":r[4],"ai_name":r[5],"ai_desc":r[6],"ai_keywords":r[7],"error_msg":r[8],"created_at":r[9],"brand":r[10] or ""} for r in rows]
    except Exception:
        return []

def _pj_get(job_id):
    if not DATABASE_URL:
        return None
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id,url,platform,status,raw_title,raw_desc,raw_images,raw_price,ai_name,ai_desc,ai_keywords,error_msg,created_at,processed_images,img_status,raw_extra,brand,COALESCE(translated_images,'[]'),COALESCE(translate_status,'') FROM product_jobs WHERE id=%s",
            (job_id,)
        )
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close(); return None
        result = {"id":row[0],"url":row[1],"platform":row[2],"status":row[3],"raw_title":row[4],"raw_desc":row[5],"raw_images":json.loads(row[6] or "[]"),"raw_price":row[7],"ai_name":row[8],"ai_desc":row[9],"ai_keywords":row[10],"error_msg":row[11],"created_at":row[12],"processed_images":json.loads(row[13] or "[]"),"img_status":row[14] or "","raw_extra":json.loads(row[15] or "{}"),"brand":row[16] or "","translated_images":json.loads(row[17] or "[]"),"translate_status":row[18] or ""}
        try:
            cur.execute("SELECT product_images FROM product_jobs WHERE id=%s", (job_id,))
            pi_row = cur.fetchone()
            result["product_images"] = json.loads((pi_row[0] if pi_row else None) or "{}")
        except Exception:
            result["product_images"] = {}
        cur.close(); conn.close()
        return result
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

# ── Store Scan DB helpers ─────────────────────────────────────────
def _ss_insert(url, platform):
    if not DATABASE_URL:
        return None
    try:
        with _db_lock:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute(
                "INSERT INTO store_scan_jobs (url,platform,status,created_at,updated_at) VALUES (%s,%s,'pending',%s,%s) RETURNING id",
                (url, platform, time.time(), time.time())
            )
            job_id = cur.fetchone()[0]
            conn.commit(); cur.close(); conn.close()
            return job_id
    except Exception as e:
        import sys; print(f"[SS Insert] {e}", file=sys.stderr)
        return None

def _ss_update(job_id, **fields):
    if not DATABASE_URL or not fields:
        return
    try:
        fields["updated_at"] = time.time()
        set_clause = ", ".join(f"{k}=%s" for k in fields)
        with _db_lock:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute(f"UPDATE store_scan_jobs SET {set_clause} WHERE id=%s", list(fields.values()) + [job_id])
            conn.commit(); cur.close(); conn.close()
    except Exception as e:
        import sys; print(f"[SS Update] {e}", file=sys.stderr)

def _ss_get(job_id):
    if not DATABASE_URL:
        return None
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id,url,platform,status,item_count,error_msg,created_at FROM store_scan_jobs WHERE id=%s",
            (job_id,)
        )
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close(); return None
        result = {"id":row[0],"url":row[1],"platform":row[2],"status":row[3],"item_count":row[4],"error_msg":row[5],"created_at":row[6]}
        cur.execute(
            "SELECT id,title,url,image,price,shop_name,platform,scraped_at,added_to_queue FROM store_scan_items WHERE scan_job_id=%s ORDER BY id ASC",
            (job_id,)
        )
        result["items"] = [{"id":r[0],"title":r[1],"url":r[2],"image":r[3],"price":r[4],"shop_name":r[5],"platform":r[6],"scraped_at":r[7],"added_to_queue":bool(r[8])} for r in cur.fetchall()]
        cur.close(); conn.close()
        return result
    except Exception:
        return None

def _ss_list(limit=20):
    if not DATABASE_URL:
        return []
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id,url,platform,status,item_count,created_at FROM store_scan_jobs ORDER BY created_at DESC LIMIT %s",
            (limit,)
        )
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id":r[0],"url":r[1],"platform":r[2],"status":r[3],"item_count":r[4],"created_at":r[5]} for r in rows]
    except Exception:
        return []

def _ss_insert_items(scan_job_id, items):
    if not DATABASE_URL or not items:
        return 0
    try:
        with _db_lock:
            conn = _pg_conn(); cur = conn.cursor()
            now = time.time()
            for it in items:
                cur.execute(
                    "INSERT INTO store_scan_items (scan_job_id,title,url,image,price,shop_name,platform,scraped_at,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (scan_job_id, it.get("title","")[:200], it.get("url",""), it.get("image",""), it.get("price","")[:50], it.get("shop_name","")[:100], it.get("platform",""), it.get("scraped_at",""), now)
                )
            conn.commit(); cur.close(); conn.close()
            return len(items)
    except Exception as e:
        import sys; print(f"[SS InsertItems] {e}", file=sys.stderr)
        return 0

def _ss_mark_added(item_ids):
    if not DATABASE_URL or not item_ids:
        return
    try:
        with _db_lock:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute("UPDATE store_scan_items SET added_to_queue=TRUE WHERE id=ANY(%s)", (item_ids,))
            conn.commit(); cur.close(); conn.close()
    except Exception as e:
        import sys; print(f"[SS MarkAdded] {e}", file=sys.stderr)

# ── 圖片處理工具 ──────────────────────────────────────────────────
def _clean_images(urls):
    import re
    seen = set()
    skip_patterns = re.compile(r'icon|logo|_\d{2}x\d{2}[_.]|_30x|_50x|_60x|_80x|\.ico$', re.IGNORECASE)
    priority_patterns = re.compile(r'_800x|_600x|_790x|_750x|_700x|_[89]\d{2}x|imgextra|mainimg', re.IGNORECASE)
    cleaned = []
    for u in urls:
        u = u.strip()
        if not u: continue
        if u.startswith("//"): u = "https:" + u
        if not u.startswith("http"): continue
        if skip_patterns.search(u): continue
        if u not in seen:
            seen.add(u); cleaned.append(u)
    priority = [u for u in cleaned if priority_patterns.search(u)]
    others   = [u for u in cleaned if not priority_patterns.search(u)]
    return (priority + others)[:10]

def _extract_meta_img(html):
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
    import re
    pattern = re.compile(
        r'(?:https?:)?//[^"\'<>\s]*?\.alicdn\.com/[^"\'<>\s]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\'<>\s]*)?',
        re.IGNORECASE
    )
    return list(pattern.findall(html))

def _scrape_images(html):
    urls = []
    urls += _extract_meta_img(html)
    urls += _extract_alicdn_imgs(html)
    return _clean_images(urls)

def _detect_platform(url):
    if "1688.com" in url: return "1688"
    if "taobao.com" in url: return "taobao"
    return "unknown"

def _extract_meta_text(html, prop):
    import re
    for pat in [
        rf'<meta[^>]+(?:property|name)=["\']?{re.escape(prop)}["\']?[^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']?{re.escape(prop)}["\']?',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m: return m.group(1).strip()
    return ""

def _extract_title_tag(html):
    import re
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    return m.group(1).strip() if m else ""

def _fetch_html(url):
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
    import re
    result = {"title": "", "desc": "", "images": [], "price": "", "error": ""}
    try:
        html = _fetch_html(url)
        result["title"] = _extract_meta_text(html, "og:title") or _extract_title_tag(html)
        for suffix in ["-1688.com", "- 1688", "- 淘寶網", "-淘寶網", "- Taobao", "_淘寶搜索"]:
            if result["title"].endswith(suffix):
                result["title"] = result["title"][:-len(suffix)].strip()
        result["desc"] = _extract_meta_text(html, "og:description") or _extract_meta_text(html, "description")
        result["images"] = _scrape_images(html)
        price_m = re.search(r'["\']price["\']\s*:\s*["\']?([\d.,]+)["\']?', html)
        if price_m: result["price"] = price_m.group(1)
    except Exception as e:
        result["error"] = str(e)
    return result

# ── Claude AI 改寫 ────────────────────────────────────────────────
def _ai_rewrite(raw_title, raw_desc, price="", brand=""):
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY 未設定"}
    bp = _bp_get(brand) if brand else {}
    brand_name     = bp.get("name", "台灣電商品牌")
    brand_category = bp.get("category", "商品")
    brand_style    = bp.get("style", "簡潔、專業、官網風格。")
    brand_tone     = bp.get("tone", "直接說明功能，不像業務推銷。")
    custom_prompt  = bp.get("custom_prompt", "")
    parts = []
    if raw_title: parts.append(f"原始標題：{raw_title}")
    if price:     parts.append(f"參考價格：{price}")
    if raw_desc:  parts.append(f"原始描述：{raw_desc[:1500]}")
    product_block = chr(10).join(parts)
    if custom_prompt and custom_prompt.strip():
        prompt = custom_prompt.replace("{product}", product_block)
        if "{product}" not in custom_prompt:
            prompt = custom_prompt + "\n\n" + product_block
    else:
        prompt = f"""你是「{brand_name}」品牌的文案編輯，負責{brand_category}類商品。
品牌文案風格：{brand_style}
語氣要求：{brand_tone}

請將以下中國電商商品資料改寫成台灣官網風格。

{product_block}

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
            data=req_data, method="POST",
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

# ── 背景任務 ──────────────────────────────────────────────────────
def _process_product_job(job_id, url, platform):
    _pj_update(job_id, status="scraping")
    scraped = _scrape_product(url, platform)
    if scraped.get("error") and not scraped.get("title"):
        _pj_update(job_id, status="error", error_msg=f"爬取失敗：{scraped['error']}")
        return
    raw_title  = scraped.get("title", "")
    raw_desc   = scraped.get("desc", "")
    raw_images = scraped.get("images", [])
    raw_price  = scraped.get("price", "")
    _pj_update(job_id, status="rewriting", raw_title=raw_title, raw_desc=raw_desc,
               raw_images=json.dumps(raw_images, ensure_ascii=False), raw_price=raw_price)
    if not raw_title and not raw_desc:
        _pj_update(job_id, status="error", error_msg="無法取得商品資料（頁面可能需要登入）")
        return
    ai = _ai_rewrite(raw_title, raw_desc, raw_price)
    if "error" in ai:
        _pj_update(job_id, status="error", error_msg=f"AI 改寫失敗：{ai['error']}")
        return
    _pj_update(job_id, status="done", ai_name=ai.get("name",""), ai_desc=ai.get("desc",""), ai_keywords=ai.get("keywords",""))

def _run_ai_rewrite_for_job(job_id):
    job = _pj_get(job_id)
    if not job: return
    raw_title = job.get("raw_title", "")
    raw_desc  = job.get("raw_desc", "")
    raw_price = job.get("raw_price", "")
    if not raw_title and not raw_desc:
        _pj_update(job_id, status="error", error_msg="無法取得商品資料（頁面可能需要登入）")
        return
    brand = job.get("brand", "")
    _pj_update(job_id, status="rewriting")
    ai = _ai_rewrite(raw_title, raw_desc, raw_price, brand)
    if "error" in ai:
        _pj_update(job_id, status="error", error_msg=f"AI 改寫失敗：{ai['error']}")
        return
    _pj_update(job_id, status="done",
               ai_name=ai.get("name",""), ai_desc=ai.get("desc",""), ai_keywords=ai.get("keywords",""))

# ── 圖片處理（Phase 2A）──────────────────────────────────────────
def _download_image(url):
    try:
        if url.startswith("//"): url = "https:" + url
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.1688.com/",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except Exception:
        return None

def _process_to_white_bg(img_bytes, size=800):
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
        bg = bg.convert("RGB")
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
    job = _pj_get(job_id)
    if not job: return
    raw_imgs = job.get("raw_images", [])
    if not raw_imgs:
        _pj_update(job_id, img_status="no_images"); return
    _pj_update(job_id, img_status="processing")
    processed = []
    for i, url in enumerate(raw_imgs[:10]):
        img_bytes = _download_image(url)
        if not img_bytes: continue
        result = _process_to_white_bg(img_bytes)
        if not result: continue
        filename = f"products/{job_id}_{i+1}.jpg"
        pub_url, _ = upload_image_to_supabase(filename, result, "image/jpeg")
        if pub_url: processed.append(pub_url)
    _pj_update(job_id,
               processed_images=json.dumps(processed, ensure_ascii=False),
               img_status="done" if processed else "failed")

# ── 圖片翻譯 helpers ──────────────────────────────────────────────
def _get_cjk_font(size=22):
    from PIL import ImageFont
    paths = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/tmp/NotoSansCJK.ttc",
    ]
    for p in paths:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except Exception: pass
    cache = "/tmp/NotoSansCJK.ttc"
    if not os.path.exists(cache):
        try:
            urllib.request.urlretrieve(
                "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTF/TraditionalChinese/NotoSansCJKtc-Regular.otf",
                cache
            )
            return ImageFont.truetype(cache, size)
        except Exception:
            pass
    return ImageFont.load_default()

def _sample_bg_info(img_pil, x1, y1, x2, y2):
    from PIL import ImageStat
    W, H = img_pil.size
    region = img_pil.crop((x1, y1, x2, y2))
    rw, rh = region.size
    if rw <= 0 or rh <= 0:
        return 0.0, 200.0, (255, 255, 255)
    stat_rgb = ImageStat.Stat(region)
    variance = (sum(v * v for v in stat_rgb.stddev) / len(stat_rgb.stddev)) ** 0.5
    stat_l = ImageStat.Stat(region.convert("L"))
    avg_brightness = stat_l.mean[0]
    ox1, oy1 = max(0, x1 - 3), max(0, y1 - 3)
    ox2, oy2 = min(W, x2 + 3), min(H, y2 + 3)
    stat_outer = ImageStat.Stat(img_pil.crop((ox1, oy1, ox2, oy2)))
    edge_color = tuple(int(v) for v in stat_outer.mean[:3])
    return variance, avg_brightness, edge_color

def _translate_images_job(job_id, img_urls):
    import re as _re3, traceback
    try:
        import requests as _req
        from PIL import Image, ImageDraw
    except ImportError as e:
        print(f"[translate] import error: {e}")
        _pj_update(job_id, translate_status="failed")
        return

    STABILITY_KEY = os.getenv("STABILITY_API_KEY", "")
    if not STABILITY_KEY:
        print("[translate] STABILITY_API_KEY not set — complex blocks will fallback to Pillow")

    OCR_PROMPT = (
        '請偵測圖片中所有文字區塊，直接輸出 JSON（不要 markdown）。\n'
        '格式：{"blocks":[{"text":"原文","language":"zh-CN/zh-TW/ja/en/number/mixed",'
        '"should_translate":true/false,"translated_text":"繁體（false時留空字串）",'
        '"bbox":[x1,y1,x2,y2],"font_role":"title/subtitle/body/label/number",'
        '"background_type":"white/solid/complex"}]}\n'
        '規則（嚴格遵守）：\n'
        '1. language分類：zh-CN=簡體中文漢字，zh-TW=繁體中文，ja=日文（含ひらがな/カタカナ），en=英文，number=純數字百分比\n'
        '2. should_translate只有純zh-CN才true，其他全部false\n'
        '3. translated_text：簡體→台灣繁體，混合文字只翻中文部分，英文/數字原樣保留\n'
        '4. bbox：[左%,上%,右%,下%]，圖片寬高各為100\n'
        '5. font_role：title=主標題大字，subtitle=副標題，body=說明文字，label=小標籤，number=數字\n'
        '6. background_type：white=白色背景，solid=純色背景，complex=照片/漸層/複雜背景\n'
        '範例（此圖色卡）：\n'
        '- 「海盐蓝」→zh-CN,true,「海鹽藍」,title,white\n'
        '- 「クリームホワイト」→ja,false,\"\",label,white\n'
        '- 「Shading：80%」→en,false,\"\",number,white\n'
        '- 「遮光率：80%」→zh-CN,true,「遮光率：80%」,label,white（數字保留不翻）\n'
        '只輸出JSON，不要說明文字。'
    )

    def _pct2px(pct, dim):
        return max(0, min(dim, int(pct / 100 * dim)))

    def _sample_bg(base_img, x1, y1, x2, y2):
        W, H = base_img.size
        samples = []
        for px, py in [(max(0,x1-8),max(0,y1-8)), (min(W-1,x2+8),max(0,y1-8)),
                       (max(0,x1-8),min(H-1,y2+8)), (min(W-1,x2+8),min(H-1,y2+8))]:
            try:
                s = base_img.getpixel((px, py))
                samples.append(s[:3] if len(s) > 3 else s)
            except Exception:
                pass
        if not samples: return (255, 255, 255)
        return tuple(sum(s[i] for s in samples) // len(samples) for i in range(3))

    def _fit_and_draw(draw, text, x1, y1, x2, y2, role, text_color=(15, 15, 15)):
        from PIL import ImageFont
        box_w, box_h = x2 - x1, y2 - y1
        start_sz = int(box_h * (0.80 if role == "title" else 0.75))
        start_sz = max(10, min(start_sz, 120))
        font, chosen_sz = None, start_sz
        for sz in range(start_sz, 7, -2):
            try:
                f = _get_cjk_font(sz)
                try:
                    bb = f.getbbox(text)
                    tw = bb[2] - bb[0]
                except Exception:
                    tw = len(text) * sz * 0.65
                if tw <= box_w * 1.05:
                    font, chosen_sz = f, sz
                    break
            except Exception:
                pass
        if font is None:
            font = _get_cjk_font(10)
        try:
            bb = font.getbbox(text)
            tw, th = bb[2]-bb[0], bb[3]-bb[1]
        except Exception:
            tw, th = len(text)*chosen_sz, chosen_sz
        if role in ("title", "subtitle"):
            tx = x1 + max(0, (box_w - tw) // 2)
        else:
            tx = x1 + 4
        ty = y1 + max(0, (box_h - th) // 2)
        draw.text((tx, ty), text, fill=text_color, font=font)

    translated_urls = []
    stats = dict(total=0, translated=0, skip_ja=0, skip_en=0, skip_num=0,
                 skip_tw=0, pillow=0, stab=0, stab_fail=0)

    for img_idx, url in enumerate(img_urls):
        print(f"[translate] {img_idx+1}/{len(img_urls)} {url[:70]}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://www.1688.com/"})
            with urllib.request.urlopen(req, timeout=20) as r:
                img_bytes = r.read()
            media_type = "image/jpeg"
            if img_bytes[:8] == b'\x89PNG\r\n\x1a\n': media_type = "image/png"
            elif img_bytes[:4] == b'RIFF': media_type = "image/webp"

            img_b64 = base64.standard_b64encode(img_bytes).decode()
            claude_resp = urllib.request.urlopen(
                urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=json.dumps({
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 3000,
                        "messages": [{"role":"user","content":[
                            {"type":"image","source":{"type":"base64","media_type":media_type,"data":img_b64}},
                            {"type":"text","text":OCR_PROMPT}
                        ]}]
                    }).encode(),
                    headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
                    method="POST"
                ), timeout=45
            )
            raw_text = json.loads(claude_resp.read())["content"][0]["text"].strip()
            m = _re3.search(r'\{[\s\S]*\}', raw_text)
            if not m:
                print("  [OCR] no JSON"); translated_urls.append(url); continue

            blocks = json.loads(m.group()).get("blocks", [])
            stats['total'] += len(blocks)
            print(f"  [OCR] {len(blocks)} 區塊")

            to_do = []
            for b in blocks:
                lang = b.get("language","")
                if not b.get("should_translate") or not b.get("translated_text","") or lang != "zh-CN":
                    if lang=="ja": stats['skip_ja']+=1
                    elif lang=="en": stats['skip_en']+=1
                    elif lang=="number": stats['skip_num']+=1
                    elif lang=="zh-TW": stats['skip_tw']+=1
                    continue
                stats['translated']+=1
                to_do.append(b)

            if not to_do:
                print("  [OCR] 無需翻譯"); translated_urls.append(url); continue

            from PIL import Image
            img_pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            W, H = img_pil.size
            for b in to_do:
                bx = b.get("bbox", [0, 0, 100, 100])
                b['_px'] = (_pct2px(bx[0],W), _pct2px(bx[1],H), _pct2px(bx[2],W), _pct2px(bx[3],H))

            simple, complex_ = [], []
            for b in to_do:
                x1, y1, x2, y2 = b['_px']
                variance, brightness, edge_color = _sample_bg_info(img_pil, x1, y1, x2, y2)
                b['_variance'] = variance
                b['_brightness'] = brightness
                b['_edge_color'] = edge_color
                b['_text_color'] = (255, 255, 255) if brightness < 128 else (15, 15, 15)
                color_label = "white" if brightness < 128 else "dark"
                bg_hint = b.get("background_type", "white")
                if variance < 20:
                    b['_pad'] = 2; simple.append(b)
                    print(f"    [{b.get('text','')[:12]}] var={variance:.1f} bright={brightness:.0f} ({bg_hint}) → pillow | {color_label}")
                elif variance < 50:
                    b['_pad'] = 6; simple.append(b)
                    print(f"    [{b.get('text','')[:12]}] var={variance:.1f} bright={brightness:.0f} ({bg_hint}) → pillow+pad | {color_label}")
                else:
                    complex_.append(b)
                    print(f"    [{b.get('text','')[:12]}] var={variance:.1f} bright={brightness:.0f} ({bg_hint}) → stability | {color_label}")

            stability_saved = len(simple)
            print(f"  [classify] pillow={len(simple)} stability={len(complex_)} | 本張省下 {stability_saved}/{len(to_do)} 次 Stability 呼叫")

            result_img = img_pil.copy()

            if simple:
                draw = ImageDraw.Draw(result_img)
                for b in simple:
                    x1,y1,x2,y2 = b['_px']
                    role = b.get("font_role","body")
                    pad = b.get('_pad', 4)
                    rx1,ry1 = max(0,x1-pad), max(0,y1-pad)
                    rx2,ry2 = min(W,x2+pad), min(H,y2+pad)
                    bg = b.get('_edge_color') or _sample_bg(img_pil, rx1,ry1,rx2,ry2)
                    draw.rectangle([rx1,ry1,rx2,ry2], fill=bg)
                    _fit_and_draw(draw, b.get("translated_text",""), rx1,ry1,rx2,ry2, role, b['_text_color'])
                    stats['pillow']+=1
                    clabel = "white" if b.get('_brightness', 200) < 128 else "dark"
                    print(f"    Pillow [{role}]: '{b.get('text','')}' → '{b.get('translated_text','')}' | {clabel}")

            if complex_ and not STABILITY_KEY:
                draw_fb = ImageDraw.Draw(result_img)
                for b in complex_:
                    x1,y1,x2,y2 = b['_px']
                    role = b.get("font_role","body")
                    pad = 6
                    rx1,ry1 = max(0,x1-pad),max(0,y1-pad)
                    rx2,ry2 = min(W,x2+pad),min(H,y2+pad)
                    bg = b.get('_edge_color') or _sample_bg(img_pil,rx1,ry1,rx2,ry2)
                    draw_fb.rectangle([rx1,ry1,rx2,ry2],fill=bg)
                    _fit_and_draw(draw_fb, b.get("translated_text",""), rx1,ry1,rx2,ry2, role, b['_text_color'])
                    stats['pillow']+=1
                complex_ = []
                print(f"  [Stability] skipped (no key) — fell back to Pillow for all blocks")

            if complex_:
                from PIL import ImageDraw as _ID2
                mask = Image.new("L",(W,H),0)
                dm = _ID2.Draw(mask)
                for b in complex_:
                    x1,y1,x2,y2 = b['_px']
                    mp = 8
                    dm.rectangle([max(0,x1-mp),max(0,y1-mp),min(W,x2+mp),min(H,y2+mp)],fill=255)
                ib = io.BytesIO(); result_img.save(ib,"PNG"); ib.seek(0)
                mb = io.BytesIO(); mask.convert("RGB").save(mb,"PNG"); mb.seek(0)
                inpaint_ok = False
                try:
                    sr = _req.post(
                        "https://api.stability.ai/v2beta/stable-image/edit/erase",
                        headers={"Authorization":f"Bearer {STABILITY_KEY}","Accept":"image/*"},
                        files={"image":("i.png",ib.getvalue(),"image/png"),
                               "mask":("m.png",mb.getvalue(),"image/png")},
                        data={"output_format":"png"}, timeout=60
                    )
                    if sr.status_code == 200:
                        result_img = Image.open(io.BytesIO(sr.content)).convert("RGB")
                        stats['stab']+=len(complex_)
                        inpaint_ok = True
                        print(f"    Stability: {len(complex_)} 區塊")
                    else:
                        print(f"    Stability {sr.status_code} → Pillow fallback")
                        stats['stab_fail']+=len(complex_)
                except Exception as se:
                    print(f"    Stability error: {se} → Pillow fallback")
                    stats['stab_fail']+=len(complex_)

                if not inpaint_ok:
                    draw_fb = ImageDraw.Draw(result_img)
                    for b in complex_:
                        x1,y1,x2,y2 = b['_px']
                        bg = b.get('_edge_color') or _sample_bg(img_pil,x1,y1,x2,y2)
                        draw_fb.rectangle([x1,y1,x2,y2],fill=bg)
                    stats['pillow']+=len(complex_)

                draw_c = ImageDraw.Draw(result_img)
                for b in complex_:
                    x1,y1,x2,y2 = b['_px']
                    role = b.get("font_role","body")
                    _fit_and_draw(draw_c, b.get("translated_text",""),
                                  x1,y1,x2,y2, role, b.get('_text_color', (15,15,15)))

            print(f"  [stats] OCR:{stats['total']} 翻:{stats['translated']} 跳日:{stats['skip_ja']} 跳英:{stats['skip_en']} 跳數:{stats['skip_num']} 跳繁:{stats['skip_tw']} Pillow:{stats['pillow']} Stab:{stats['stab']} Stab失敗:{stats['stab_fail']}")

            out = io.BytesIO()
            result_img.save(out,"JPEG",quality=93)
            fname = f"translated_{job_id}_{img_idx+1}.jpg"
            turl, _ = upload_image_to_supabase(fname, out.getvalue(), "image/jpeg")
            if not turl:
                turl, _ = upload_image_to_github(fname, out.getvalue())
            translated_urls.append(turl or url)
            print(f"  [done] {(turl or url)[:70]}")

        except Exception as e:
            print(f"  [ERROR] {e}")
            traceback.print_exc()
            translated_urls.append(url)

    _pj_update(job_id,
               translated_images=json.dumps(translated_urls, ensure_ascii=False),
               translate_status="done")
    print(f"[translate] 完成 {len(translated_urls)}/{len(img_urls)} 張，stats={stats}")

# ── 匯出 helpers ──────────────────────────────────────────────────
def _pj_list_done():
    if not DATABASE_URL: return []
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id,url,platform,raw_title,raw_price,ai_name,ai_desc,ai_keywords,raw_images,processed_images,created_at FROM product_jobs WHERE status='done' ORDER BY created_at DESC"
        )
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id":r[0],"url":r[1],"platform":r[2],"raw_title":r[3],"raw_price":r[4],
                 "ai_name":r[5],"ai_desc":r[6],"ai_keywords":r[7],
                 "raw_images":json.loads(r[8] or "[]"),
                 "processed_images":json.loads(r[9] or "[]"),
                 "created_at":r[10]} for r in rows]
    except Exception:
        return []

def _export_csv(jobs):
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["平台","AI商品名稱","AI商品描述","SEO關鍵字","原始標題","原始價格","來源URL","圖片URL_1","圖片URL_2","圖片URL_3"])
    for j in jobs:
        imgs = j.get("processed_images") or j.get("raw_images",[])
        w.writerow([
            j["platform"], j["ai_name"], j["ai_desc"], j["ai_keywords"],
            j["raw_title"], j["raw_price"], j["url"],
            imgs[0] if len(imgs)>0 else "",
            imgs[1] if len(imgs)>1 else "",
            imgs[2] if len(imgs)>2 else "",
        ])
    output = buf.getvalue().encode("utf-8-sig")
    return Response(output, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=products.csv"})

def _export_xlsx(jobs):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        import openpyxl.utils
    except ImportError:
        return jsonify({"error": "openpyxl 未安裝"}), 500
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "商品資料"
    headers = ["平台","AI商品名稱","AI商品描述","SEO關鍵字","原始標題","原始價格","來源URL","圖片URL_1","圖片URL_2","圖片URL_3"]
    hfill = PatternFill("solid", fgColor="1a1a1a")
    hfont = Font(color="FFFFFF", bold=True)
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.fill = hfill; cell.font = hfont
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    col_widths = [10, 35, 60, 30, 35, 12, 55, 55, 55, 55]
    for i, cw in enumerate(col_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = cw
    for ri, j in enumerate(jobs, 2):
        imgs = j.get("processed_images") or j.get("raw_images",[])
        values = [
            j["platform"], j["ai_name"], j["ai_desc"], j["ai_keywords"],
            j["raw_title"], j["raw_price"], j["url"],
            imgs[0] if len(imgs)>0 else "",
            imgs[1] if len(imgs)>1 else "",
            imgs[2] if len(imgs)>2 else "",
        ]
        for ci, v in enumerate(values, 1):
            cell = ws.cell(row=ri, column=ci, value=v)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[ri].height = 60
    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    return Response(buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=products.xlsx"})


STORE_SCAN_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>店鋪選品掃描</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
.header{background:#1a1a1a;color:#fff;padding:14px 20px;display:flex;align-items:center;gap:14px}
.header a{color:#888;text-decoration:none;font-size:14px}
.header a:hover{color:#fff}
.header-title{font-size:17px;font-weight:700;flex:1}
.wrap{max-width:960px;margin:0 auto;padding:20px 16px}
.card{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.card h3{font-size:14px;font-weight:700;color:#666;margin-bottom:12px}
.url-row{display:flex;gap:10px}
.url-row input{flex:1;border:1.5px solid #ddd;border-radius:10px;padding:10px 14px;font-size:14px;outline:none;font-family:inherit}
.url-row input:focus{border-color:#1a1a1a}
.btn-primary{background:#1a1a1a;color:#fff;border:none;border-radius:10px;padding:10px 22px;font-size:14px;font-weight:700;cursor:pointer;white-space:nowrap;font-family:inherit}
.btn-primary:hover{background:#333}
.btn-primary:disabled{background:#aaa;cursor:default}
.status-bar{margin-top:12px;font-size:13px;color:#666;min-height:20px}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid #ddd;border-top-color:#666;border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle;margin-right:4px}
@keyframes spin{to{transform:rotate(360deg)}}
/* history */
.history-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.hist-chip{background:#f0f0f0;border:1.5px solid transparent;border-radius:20px;padding:4px 12px;font-size:12px;cursor:pointer;font-family:inherit}
.hist-chip:hover{border-color:#1a1a1a}
.hist-chip.active{background:#1a1a1a;color:#fff}
.hist-chip .hst{font-size:10px;color:#aaa;margin-left:4px}
.hist-chip.active .hst{color:#888}
/* grid */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-top:4px}
.item-card{background:#fff;border-radius:12px;border:2px solid transparent;cursor:pointer;overflow:hidden;transition:border-color .15s;position:relative;display:flex;flex-direction:column}
.item-card:hover{border-color:#ddd}
.item-card.checked{border-color:#1a1a1a}
.item-card input[type=checkbox]{position:absolute;top:8px;left:8px;width:16px;height:16px;cursor:pointer;accent-color:#1a1a1a;z-index:2}
.item-card img{width:100%;aspect-ratio:1;object-fit:cover;background:#f8f8f8}
.item-info{padding:8px 10px 10px;flex:1;display:flex;flex-direction:column;gap:4px}
.item-title{font-size:12px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-all}
.item-price{font-size:13px;font-weight:700;color:#c62828}
.item-shop{font-size:11px;color:#aaa;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item-added{font-size:10px;font-weight:700;color:#2e7d32;background:#e8f5e9;border-radius:6px;padding:1px 6px;align-self:flex-start}
.empty{text-align:center;padding:50px 20px;color:#ccc;font-size:14px}
/* action bar */
.action-bar{position:sticky;bottom:0;background:#fff;border-top:1px solid #eee;padding:12px 20px;display:none;align-items:center;gap:12px;z-index:10}
.brand-sel{border:1.5px solid #ddd;border-radius:10px;padding:8px 12px;font-size:13px;font-family:inherit;outline:none;background:#fff;cursor:pointer}
.brand-sel:focus{border-color:#1a1a1a}
.sel-count{font-size:13px;color:#666;flex:1}
.btn-add{background:#2e7d32;color:#fff;border:none;border-radius:10px;padding:10px 22px;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit}
.btn-add:hover{background:#1b5e20}
.btn-add:disabled{background:#aaa;cursor:default}
.sel-all-row{display:flex;gap:10px;align-items:center;margin-bottom:10px;font-size:13px}
.sel-all-row button{background:none;border:1.5px solid #ddd;border-radius:20px;padding:3px 12px;font-size:12px;cursor:pointer;font-family:inherit}
.sel-all-row button:hover{background:#f5f5f5}
.toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:#333;color:#fff;padding:9px 20px;border-radius:20px;font-size:14px;z-index:999;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}
.pf-badge{font-size:11px;font-weight:700;padding:2px 7px;border-radius:7px}
.pf-1688{background:#fff0f0;color:#c62828}
.pf-taobao{background:#fff4e5;color:#e65100}
</style>
</head>
<body>

<div class="header">
  <a href="/admin/products?key={{ key }}">← 商品搬運</a>
  <div class="header-title">店鋪選品掃描 <span style="font-size:12px;font-weight:400;color:#666">Phase 1</span></div>
</div>

<div class="wrap">

  <div class="card">
    <h3>貼上店鋪 / 分類頁連結</h3>
    <div class="url-row">
      <input type="text" id="urlInput" placeholder="https://shop.1688.com/... 或 https://shop.taobao.com/...">
      <button class="btn-primary" id="scanBtn" onclick="startScan()">開始掃描</button>
    </div>
    <div class="status-bar" id="statusBar"></div>
    <div id="historyWrap" style="margin-top:14px;display:none">
      <div style="font-size:12px;color:#aaa;margin-bottom:6px">最近掃描</div>
      <div class="history-row" id="historyRow"></div>
    </div>
  </div>

  <div class="card" id="itemsCard" style="display:none">
    <div class="sel-all-row">
      <span id="scanTitle" style="flex:1;font-size:13px;color:#666"></span>
      <button onclick="toggleAll(true)">全選</button>
      <button onclick="toggleAll(false)">全不選</button>
    </div>
    <div class="grid" id="itemGrid"></div>
  </div>

</div>

<div class="action-bar" id="actionBar">
  <select class="brand-sel" id="brandSel">
    <option value="">不指定品牌</option>
    <option value="jsimple">JSIMPLE — 高架床</option>
    <option value="lander">朗德燈具 — 燈具</option>
    <option value="filterbreath">濾呼吸 — 濾網</option>
  </select>
  <span class="sel-count" id="selCount">已選 0 筆</span>
  <button class="btn-add" id="addBtn" onclick="addToQueue()">加入待上架</button>
</div>

<div class="toast" id="toast"></div>

<script>
const KEY = '{{ key }}';
let currentJobId = null;
let pollTimer = null;

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

async function api(path, opts){
  const sep = path.includes('?')?'&':'?';
  const r = await fetch(path+sep+'key='+KEY, opts);
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}

function showToast(msg){
  const t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2800);
}

async function loadHistory(){
  try{
    const d = await api('/api/store-scan');
    const jobs = d.jobs||[];
    if(!jobs.length) return;
    const row = document.getElementById('historyRow');
    row.innerHTML = jobs.slice(0,8).map(j=>`
      <span class="hist-chip" onclick="loadJob(${j.id})" data-jid="${j.id}">
        <span class="pf-badge pf-${esc(j.platform)}">${esc(j.platform)}</span>
        ${esc((j.url||'').split('/').slice(-2).join('/').slice(-30))}
        <span class="hst">${j.item_count||0}筆</span>
      </span>
    `).join('');
    document.getElementById('historyWrap').style.display='';
  }catch(e){}
}

async function loadJob(jobId){
  currentJobId = jobId;
  document.querySelectorAll('.hist-chip').forEach(el=>el.classList.toggle('active',+el.dataset.jid===jobId));
  document.getElementById('statusBar').innerHTML='<span class="spinner"></span> 載入中...';
  try{
    const job = await api('/api/store-scan/'+jobId);
    updateUI(job);
    if(job.status==='pending'||job.status==='scanning') startPoll();
  }catch(e){
    document.getElementById('statusBar').textContent='載入失敗：'+e.message;
  }
}

async function startScan(){
  const url = document.getElementById('urlInput').value.trim();
  if(!url){alert('請輸入店鋪/分類頁連結');return;}
  document.getElementById('scanBtn').disabled=true;
  document.getElementById('statusBar').innerHTML='<span class="spinner"></span> 建立掃描任務...';
  document.getElementById('itemsCard').style.display='none';
  document.getElementById('actionBar').style.display='none';
  clearTimeout(pollTimer);
  try{
    const r = await api('/api/store-scan',{method:'POST',body:JSON.stringify({url}),headers:{'Content-Type':'application/json'}});
    currentJobId = r.id;
    await loadHistory();
    startPoll();
  }catch(e){
    document.getElementById('statusBar').textContent='錯誤：'+e.message;
    document.getElementById('scanBtn').disabled=false;
  }
}

function startPoll(){
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async()=>{
    try{
      const job = await api('/api/store-scan/'+currentJobId);
      updateUI(job);
      if(job.status==='pending'||job.status==='scanning') startPoll();
      else document.getElementById('scanBtn').disabled=false;
    }catch(e){ startPoll(); }
  },2500);
}

function updateUI(job){
  const sb = document.getElementById('statusBar');
  if(job.status==='pending'){
    sb.innerHTML='<span class="spinner"></span> 等待 local_worker 處理...（請確認 worker 正在執行）';
  }else if(job.status==='scanning'){
    sb.innerHTML='<span class="spinner"></span> 掃描中...';
  }else if(job.status==='done'){
    sb.textContent='掃描完成，共 '+job.item_count+' 筆商品';
    renderItems(job.items||[], job);
  }else if(job.status==='error'){
    sb.textContent='錯誤：'+(job.error_msg||'未知');
  }
}

function renderItems(items, job){
  const card = document.getElementById('itemsCard');
  const grid = document.getElementById('itemGrid');
  const scanTitle = document.getElementById('scanTitle');
  const bar = document.getElementById('actionBar');
  card.style.display='';
  if(job) scanTitle.innerHTML='<span class="pf-badge pf-'+esc(job.platform)+'">'+esc(job.platform)+'</span> '+job.item_count+' 筆商品';
  if(!items.length){
    grid.innerHTML='<div class="empty">未抓到商品。<br>可能需要手動登入，或頁面格式不支援。<br><br>請用 python local_worker.py --login 先登入，再重試。</div>';
    bar.style.display='none';
    return;
  }
  grid.innerHTML = items.map(it=>`
    <label class="item-card${it.added_to_queue?' checked':''}" onclick="updateCount()">
      <input type="checkbox" value="${it.id}" class="item-cb"${it.added_to_queue?' disabled checked':''}>
      <img src="${esc(it.image)}" onerror="this.style.background='#f0f0f0';this.style.display='block'" alt="">
      <div class="item-info">
        <div class="item-title">${esc(it.title)}</div>
        ${it.price?'<div class="item-price">￥'+esc(it.price)+'</div>':''}
        ${it.shop_name?'<div class="item-shop">'+esc(it.shop_name)+'</div>':''}
        ${it.added_to_queue?'<div class="item-added">已加入</div>':''}
      </div>
    </label>
  `).join('');
  bar.style.display='flex';
  updateCount();
}

function updateCount(){
  const n = document.querySelectorAll('.item-cb:not(:disabled):checked').length;
  document.getElementById('selCount').textContent='已選 '+n+' 筆';
}

function toggleAll(v){
  document.querySelectorAll('.item-cb:not(:disabled)').forEach(el=>{el.checked=v;});
  document.querySelectorAll('.item-card').forEach(el=>{ const cb=el.querySelector('.item-cb'); if(cb&&!cb.disabled) el.classList.toggle('checked',v); });
  updateCount();
}

async function addToQueue(){
  const checked=[...document.querySelectorAll('.item-cb:not(:disabled):checked')].map(el=>parseInt(el.value));
  if(!checked.length){alert('請勾選商品');return;}
  const brand=document.getElementById('brandSel').value;
  document.getElementById('addBtn').disabled=true;
  try{
    const r=await api('/api/store-scan/to-queue',{method:'POST',body:JSON.stringify({item_ids:checked,brand}),headers:{'Content-Type':'application/json'}});
    showToast('已加入 '+r.added+' 筆到商品搬運中心');
    const job=await api('/api/store-scan/'+currentJobId);
    renderItems(job.items||[],job);
  }catch(e){
    alert('錯誤：'+e.message);
  }finally{
    document.getElementById('addBtn').disabled=false;
  }
}

loadHistory();
</script>
</body>
</html>"""


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
.export-btn{background:#2d2d2d;color:#fff;text-decoration:none;border-radius:8px;padding:5px 12px;font-size:12px;font-weight:700;white-space:nowrap}
.export-btn:hover{background:#444}
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
/* 選圖 UI */
.img-zone-hd{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.img-zone-hd .slabel{margin-bottom:0;flex:none}
.sel-btn{background:#f0f0f0;color:#555;border:none;border-radius:6px;padding:3px 9px;font-size:11px;cursor:pointer;font-family:-apple-system,sans-serif}
.sel-btn:hover{background:#e0e0e0}
.img-grid{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.img-thumb{position:relative;cursor:pointer;flex-shrink:0;width:128px;text-align:center}
.img-thumb input[type=checkbox]{position:absolute;top:4px;left:4px;width:16px;height:16px;cursor:pointer;z-index:2;accent-color:#1a73e8}
.img-thumb img{width:120px;height:120px;object-fit:cover;border-radius:6px;border:2px solid #eee;transition:border-color .15s;display:block;margin:0 auto}
.img-thumb.checked img{border-color:#1a73e8;box-shadow:0 0 0 1px #1a73e8}
/* category colours */
.img-thumb.cat-sku input[type=checkbox]{accent-color:#f57c00}
.img-thumb.cat-sku.checked img{border-color:#ff9800;box-shadow:0 0 0 1px #ff9800}
.img-thumb.cat-detail input[type=checkbox]{accent-color:#7b1fa2}
.img-thumb.cat-review input[type=checkbox]{accent-color:#795548}
.img-thumb.cat-review.checked img{border-color:#795548;box-shadow:0 0 0 1px #795548}
.img-thumb.cat-detail.checked img{border-color:#9c27b0;box-shadow:0 0 0 1px #9c27b0}
.img-size{font-size:9px;color:#999;text-align:center;margin:3px 0 1px;min-height:13px;line-height:1.3}
.img-label{font-size:9px;color:#666;text-align:center;max-width:120px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;margin-bottom:2px}
.thumb-actions{display:flex;justify-content:center;gap:3px;margin-top:3px}
.thumb-act{background:#f0f0f0;border:none;border-radius:4px;padding:2px 6px;font-size:11px;cursor:pointer;color:#555;text-decoration:none;display:inline-block;line-height:1.5}
.thumb-act:hover{background:#ddd;color:#333}
.sel-action-bar{position:sticky;bottom:0;background:#fff;border-top:1px solid #f0f0f0;padding:12px 0 4px;display:flex;gap:8px;align-items:center;margin-top:12px}
.sel-count{font-size:12px;color:#888;flex:1}
.btn-confirm{background:#1a1a1a;color:#fff;border:none;border-radius:9px;padding:8px 18px;font-size:13px;font-weight:700;cursor:pointer;font-family:-apple-system,sans-serif}
.btn-confirm:hover{background:#333}
.btn-zip{background:#e3f2fd;color:#1565c0;border:none;border-radius:9px;padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;font-family:-apple-system,sans-serif}
.btn-zip:hover{background:#bbdefb}
.btn-translate{background:#e8f5e9;color:#2e7d32;border:none;border-radius:9px;padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;font-family:-apple-system,sans-serif}
.btn-translate:hover{background:#c8e6c9}
.btn-translate:disabled{background:#f5f5f5;color:#bbb;cursor:default}
/* Lightbox */
.lb-overlay{position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:9999;display:flex;align-items:center;justify-content:center}
.lb-content{max-width:90vw;max-height:90vh;display:flex;flex-direction:column;align-items:center}
.lb-content img{max-width:88vw;max-height:80vh;object-fit:contain;border-radius:8px}
.lb-info{color:#fff;font-size:13px;margin-top:10px;display:flex;gap:16px;align-items:center}
.lb-label{font-weight:600;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lb-count{color:#aaa;font-size:12px}
.lb-nav{position:fixed;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.15);border:none;color:#fff;font-size:40px;cursor:pointer;padding:10px 18px;border-radius:8px;line-height:1;z-index:10000}
.lb-nav:hover{background:rgba(255,255,255,.3)}
.lb-prev{left:12px}
.lb-next{right:12px}
.lb-close{position:fixed;top:14px;right:18px;background:none;border:none;color:#fff;font-size:28px;cursor:pointer;z-index:10000;line-height:1}
.lb-close:hover{color:#ddd}
.sel-btn-green{background:#e8f5e9;color:#2e7d32}
.sel-btn-green:hover{background:#c8e6c9}
.sel-btn-white{background:#f3e5f5;color:#6a1b9a}
.sel-btn-white:hover{background:#e1bee7}
.upload-tr-sec{background:#fff;border-radius:12px;padding:14px 16px;margin-top:10px;border:1.5px dashed #b39ddb}
.upload-tr-sec .slabel{font-size:12px;font-weight:700;color:#6a1b9a;margin-bottom:10px}
.btn-upload-lbl{display:inline-block;background:#ede7f6;color:#4527a0;border-radius:8px;padding:6px 14px;font-size:13px;font-weight:600;cursor:pointer;border:1.5px solid #b39ddb}
.btn-upload-lbl:hover{background:#d1c4e9}
.tr-type-sec{margin-top:10px;padding:10px 0 4px;border-top:1px solid #f0f0f0}
.tr-type-label{font-size:12px;font-weight:700;color:#555;margin-bottom:6px}
.translated-sec{background:#f0fdf4;border:1px solid #86efac;border-radius:10px;padding:12px;margin-top:10px}
.translated-sec .slabel{color:#16a34a}
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
.brand-sel{border:1.5px solid #ddd;border-radius:10px;padding:8px 12px;font-size:13px;font-family:-apple-system,sans-serif;outline:none;background:#fff;color:#333;cursor:pointer;width:100%}
.brand-sel:focus{border-color:#1a1a1a}
.brand-badge{font-size:10px;font-weight:700;padding:2px 7px;border-radius:7px;background:#f3e5f5;color:#7b1fa2;flex-shrink:0}
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
  <div style="display:flex;gap:8px">
    <a class="export-btn" href="/admin/products/store-scan?key={{ key }}" title="店鋪選品掃描" style="background:#e8f5e9;color:#2e7d32">店鋪選品</a>
    <a class="export-btn" href="/admin/brand-settings?key={{ key }}" title="品牌文案設定" style="background:#e8f0fe;color:#1967d2">品牌設定</a>
    <a class="export-btn" href="/api/products/export?format=xlsx&key={{ key }}" title="匯出 Excel">⬇ Excel</a>
    <a class="export-btn" href="/api/products/export?format=csv&key={{ key }}" title="匯出 CSV">⬇ CSV</a>
  </div>
</div>

<div class="wrap">
  <div class="card">
    <h3>貼上商品連結（一行一個，可多筆）</h3>
    <textarea class="url-ta" id="urlInput" placeholder="https://detail.1688.com/offer/xxx.html&#10;https://item.taobao.com/item.htm?id=xxx&#10;（一行一個連結）"></textarea>
    <div style="margin-top:8px">
      <select id="brandSel" class="brand-sel">
        <option value="">不指定品牌（通用風格）</option>
        <option value="jsimple">JSIMPLE — 高架床 / 家具</option>
        <option value="lander">朗德燈具 — 燈具 / 照明</option>
        <option value="filterbreath">濾呼吸 — 空氣濾網</option>
      </select>
    </div>
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
let jobs = [], filter = "all", pollTimer = null, openId = null, _selImgs = new Set(), _lbImgs = [], _lbIdx = 0, _curJobId = 0;

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
  pollTimer = setTimeout(loadJobs, active ? 2500 : 8000);
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
  const stLabel = {pending:"等待 Worker",scraping:"爬取中",rewriting:"改寫中",done:"完成",error:"失敗"}[j.status]||j.status;
  const isActive = ["pending","scraping","rewriting"].includes(j.status);
  const spin = isActive ? '<span class="spinner"></span>' : "";
  const brandLabel = {jsimple:"JSIMPLE",lander:"朗德",filterbreath:"濾呼吸"}[j.brand]||"";
  const brandBadge = brandLabel ? `<span class="brand-badge">${brandLabel}</span>` : "";
  const title = esc(j.ai_name||j.raw_title||j.url);
  const urlShort = j.url.length>65 ? j.url.slice(0,65)+"…" : j.url;
  const ts = j.created_at ? new Date(j.created_at*1000).toLocaleString("zh-TW",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}) : "";
  return `<div class="job" onclick="openJob(${j.id})">
    <div class="job-top">
      <span class="badge pf-${j.platform}">${pfLabel}</span>
      <span class="badge st-${j.status}">${spin}${stLabel}</span>
      ${brandBadge}
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
  if(!urls.length){toast("請貼上商品連結");errEl.textContent="請貼上商品連結";errEl.style.display="block";return;}
  btn.disabled=true;
  let done=0,failed=0;
  pWrap.style.display="block";
  for(const url of urls){
    btn.textContent=`搬運中 ${done+failed+1}/${urls.length}...`;
    hint.textContent=`已提交 ${done} 筆${failed?`，失敗 ${failed} 筆`:""}`;
    pBar.style.width=((done+failed)/urls.length*100)+"%";
    try{
      const brand=document.getElementById("brandSel").value;
      const r=await api("/api/products",{method:"POST",body:JSON.stringify({url,brand})});
      if(r.error){failed++;toast(`連結格式錯誤：${r.error}`);}
      else done++;
    }catch(e){failed++;toast("網路錯誤，請稍後再試");}
  }
  pBar.style.width="100%";
  hint.textContent=`完成：${done} 筆${failed?`，失敗：${failed} 筆`:""}`;
  if(done>0){ta.value="";toast(`成功提交 ${done} 筆任務`);}
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
  // 圖片選取 UI（分區勾選）
  const pi = j.product_images || {};
  const mainImgs   = pi.main_images   || [];
  const detailImgs = pi.detail_images || [];
  const skuImgs    = pi.sku_images    || [];
  const videoUrls  = pi.video_urls    || [];
  _selImgs = new Set(j.raw_images || []);
  _curJobId = j.id;
  _lbImgs = [];
  const _addLb = (srcs, labels, cat) => srcs.forEach((s,i) => _lbImgs.push({src:s, label:labels&&labels[i]?labels[i]:cat, cat}));
  const imgStatusMap={"pending_images":"等待處理...","processing":"處理中...","done":"白底完成","failed":"處理失敗","no_images":"無圖片"};
  const imgStatusLabel=imgStatusMap[j.img_status]||"";
  const mainSrcs = mainImgs.length ? mainImgs.map(i=>typeof i==='object'?i.src:i) : (j.raw_images||[]);
  if(mainSrcs.length){_addLb(mainSrcs,[],"主圖");h+=imgCatHtml("main","主圖",mainSrcs,[]);}
  if(videoUrls.length){
    h+=`<div class="section"><div class="slabel">影片（${videoUrls.length} 個）</div><div>${videoUrls.map(v=>`<a href="${esc(v)}" target="_blank" style="font-size:12px;display:block;margin:2px 0;color:#1a73e8;word-break:break-all">${esc(v.slice(0,80))}</a>`).join("")}</div></div>`;
  }
  if(skuImgs.length){
    const srcs=skuImgs.map(i=>typeof i==='object'?i.src:i);
    const labels=skuImgs.map(i=>typeof i==='object'?(i.label||''):'');
    _addLb(srcs,labels,"SKU");
    h+=imgCatHtml("sku","SKU 規格圖",srcs,labels);
  }
  if(detailImgs.length){
    const srcs=detailImgs.map(i=>typeof i==='object'?i.src:i);
    _addLb(srcs,[],"詳情");
    h+=imgCatHtml("detail","詳情圖",srcs,[]);
  }
  const reviewImgs=pi.review_images||[];
  if(reviewImgs.length){
    const rvSrcs=reviewImgs.map(i=>typeof i==='object'?i.src:i);
    _addLb(rvSrcs,[],"評價圖");
    h+=imgCatHtml("review","買家評價圖",rvSrcs,[]);
  }
  if(j.processed_images&&j.processed_images.length){
    const zipUrl=`/api/products/${j.id}/images/zip?key=${KEY}`;
    h+=`<div class="section"><div class="slabel" style="display:flex;align-items:center;gap:8px">已處理圖片（白底，${j.processed_images.length} 張）<a href="${zipUrl}" class="export-btn" style="font-size:11px">⬇ ZIP</a></div><div class="img-grid">${j.processed_images.map(img=>`<div class="img-thumb"><img src="${esc(img)}" loading="lazy" onerror="this.style.display='none'"></div>`).join("")}</div></div>`;
  }
  h+=`<hr class="divider">`;
  const trImgs=j.translated_images||[];
  if(trImgs.length){
    h+=`<div class="translated-sec"><div class="slabel">翻譯完成（${trImgs.length} 張）</div>`
      +`<div class="img-grid">${trImgs.map((src,i)=>`<div class="img-thumb"><img src="${esc(src)}" loading="lazy" onerror="this.style.display='none'"><div class="thumb-actions"><a href="${esc(src)}" target="_blank" class="thumb-act">⬇</a><button class="thumb-act" onclick="event.stopPropagation();cp(this,'${esc(src)}')">📋</button></div></div>`).join("")}</div>`
      +`<button class="sel-btn" style="margin-top:8px" onclick="useTranslated(${j.id})">✓ 以翻譯圖作為輸出</button>`
      +`</div>`;
  }
  // 翻譯後圖片（手動上傳）
  const piTr = j.product_images || {};
  const trTypeMap = {main:'主圖', detail:'詳情圖', sku:'SKU圖'};
  ['main','detail','sku'].forEach(t => {
    const tImgs = piTr['tr_'+t+'_images'] || [];
    if (!tImgs.length) return;
    h += `<div class="tr-type-sec"><div class="tr-type-label">翻譯圖（${trTypeMap[t]}，${tImgs.length} 張）</div><div class="img-grid">${tImgs.map((src,i)=>`<div class="img-thumb"><img src="${esc(src)}" loading="lazy" onerror="this.style.display='none'"><div class="thumb-actions"><a href="${esc(src)}" target="_blank" class="thumb-act">⬇</a><button class="thumb-act" onclick="event.stopPropagation();cp(this,'${esc(src)}')">📋</button></div></div>`).join("")}</div></div>`;
  });
  // 上傳已翻譯圖片
  h += `<div class="upload-tr-sec">
    <div class="slabel">上傳已翻譯圖片（客優雲翻譯後）</div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <select id="upType_${j.id}" style="border:1.5px solid #b39ddb;border-radius:8px;padding:6px 10px;font-size:13px;color:#4527a0">
        <option value="main">主圖</option>
        <option value="detail">詳情圖</option>
        <option value="sku">SKU圖</option>
      </select>
      <label class="btn-upload-lbl" for="upFiles_${j.id}">選擇圖片</label>
      <input type="file" id="upFiles_${j.id}" multiple accept="image/*" style="display:none" onchange="prevUpload(${j.id})">
      <span id="upCount_${j.id}" style="font-size:12px;color:#666"></span>
      <button class="btn-primary" id="upBtn_${j.id}" onclick="doUpload(${j.id})" disabled style="padding:6px 16px;font-size:13px">上傳</button>
    </div>
    <div id="upPreview_${j.id}" class="img-grid" style="margin-top:8px;max-height:180px;overflow-y:auto"></div>
  </div>`;
  h+=`<div class="sel-action-bar"><span class="sel-count" id="selCount">已選 ${_selImgs.size} 張</span><button class="btn-translate" id="btnTr_${j.id}" onclick="translateSelected(${j.id})">文A 翻譯選取</button><button class="btn-zip" onclick="downloadZipSelected(${j.id})">⬇ ZIP 下載</button><button class="btn-confirm" onclick="confirmSelect(${j.id})">確認選圖</button></div>`;
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

function imgCatHtml(catId, label, srcs, labels){
  const catCls={main:'',sku:' cat-sku',detail:' cat-detail',review:' cat-review'}[catId]||'';
  const thumbs=srcs.map((src,idx)=>{
    const checked=_selImgs.has(src);
    const lbl=labels[idx]||'';
    return `<div class="img-thumb${catCls}${checked?" checked":""}" onclick="imgThumbClick(event,this,'${catId}_${idx}')">`
      +`<input type="checkbox" id="ck_${catId}_${idx}" data-url="${esc(src)}"${checked?" checked":""}>`
      +`<img src="${esc(src)}" loading="lazy" onerror="this.style.display='none'" onload="imgSizeLoad(this)" title="${esc(src)}">`
      +`<div class="img-size"></div>`
      +(lbl?`<div class="img-label">${esc(lbl)}</div>`:'')
      +`<div class="thumb-actions">`
      +`<a href="${esc(src)}" target="_blank" class="thumb-act" onclick="event.stopPropagation()" title="開新分頁">⬇</a>`
      +`<button class="thumb-act" onclick="event.stopPropagation();cp(this,'${esc(src)}')" title="複製URL">📋</button>`
      +`<button class="thumb-act" onclick="event.stopPropagation();openLightbox('${esc(src)}')" title="放大檢視">⛶</button>`
      +`</div></div>`;
  }).join("");
  return `<div class="section" id="cat_${catId}"><div class="img-zone-hd"><div class="slabel">${label}（${srcs.length} 張）</div><button class="sel-btn" onclick="toggleAllInCat('${catId}',true)">全選</button><button class="sel-btn" onclick="toggleAllInCat('${catId}',false)">取消</button><button class="sel-btn sel-btn-green" onclick="translateCat('${catId}')">文A 翻譯此區</button><button class="sel-btn sel-btn-white" onclick="whitebgCat('${catId}',openId)">⬜ 生成白底圖</button></div><div class="img-grid">${thumbs}</div></div>`;
}
function imgSizeLoad(img){
  const w=img.naturalWidth, h=img.naturalHeight;
  const wrap=img.closest('.img-thumb');
  if(w>0 && h>0 && (w<100 || h<100)){wrap.style.display='none';return;}
  const el=wrap.querySelector('.img-size');
  if(el&&w>0) el.textContent=w+' × '+h;
}
function imgThumbClick(e,wrap,ckId){
  if(e.target.type==="checkbox") return;
  const cb=document.getElementById("ck_"+ckId);
  if(!cb) return;
  cb.checked=!cb.checked;
  syncThumb(cb,wrap);
}
function syncThumb(cb,wrap){
  const url=cb.dataset.url;
  if(cb.checked){_selImgs.add(url);wrap.classList.add("checked");}
  else{_selImgs.delete(url);wrap.classList.remove("checked");}
  const el=document.getElementById("selCount");
  if(el) el.textContent=`已選 ${_selImgs.size} 張`;
}
function toggleAllInCat(catId,checked){
  document.querySelectorAll(`#cat_${catId} .img-thumb`).forEach(wrap=>{
    const cb=wrap.querySelector("input[type=checkbox]");
    if(!cb) return;
    cb.checked=checked;
    syncThumb(cb,wrap);
  });
}
async function pollTranslate(id, tries){
  if(tries>24){toast("翻譯超時，請重試");return;}
  const j=await api("/api/products/"+id);
  if(j.translate_status==="done"||((j.translated_images||[]).length>0&&j.translate_status!=="processing")){
    toast("翻譯完成！");
    if(openId===id) openJob(id);
  } else {
    setTimeout(()=>pollTranslate(id,tries+1),5000);
  }
}
async function useTranslated(id){
  const j=await api("/api/products/"+id);
  const urls=j.translated_images||[];
  if(!urls.length){toast("沒有翻譯圖片");return;}
  const r=await api("/api/products/"+id+"/select-images",{method:"POST",body:JSON.stringify({urls})});
  if(r.ok) toast("已設定翻譯圖為輸出圖");
}
function openLightbox(src){
  const idx=_lbImgs.findIndex(i=>i.src===src);
  _lbIdx=idx>=0?idx:0;
  _showLb();
}
function _showLb(){
  const img=_lbImgs[_lbIdx];
  if(!img) return;
  document.getElementById('lbImg').src=img.src;
  document.getElementById('lbLabel').textContent=img.label||'';
  document.getElementById('lbCount').textContent=(_lbIdx+1)+' / '+_lbImgs.length;
  document.getElementById('lbBox').classList.remove('hidden');
}
function lbPrev(){_lbIdx=(_lbIdx-1+_lbImgs.length)%_lbImgs.length;_showLb();}
function lbNext(){_lbIdx=(_lbIdx+1)%_lbImgs.length;_showLb();}
function lbClose(){document.getElementById('lbBox').classList.add('hidden');}
document.addEventListener('keydown',e=>{
  if(document.getElementById('lbBox').classList.contains('hidden')) return;
  if(e.key==='ArrowLeft') lbPrev();
  else if(e.key==='ArrowRight') lbNext();
  else if(e.key==='Escape') lbClose();
});
async function translateCat(catId){
  const el=document.getElementById('cat_'+catId);
  if(!el) return;
  const urls=[...el.querySelectorAll('.img-thumb input[type=checkbox]')].map(cb=>cb.dataset.url).filter(Boolean);
  if(!urls.length){toast('此區沒有圖片');return;}
  await _doTranslate(_curJobId,urls);
}
async function translateSelected(id){
  const urls=[..._selImgs];
  await _doTranslate(id,urls);
}
async function _doTranslate(id,urls){
  if(!urls.length){toast('請先選取要翻譯的圖片');return;}
  const btn=document.getElementById('btnTr_'+id);
  if(btn){btn.disabled=true;btn.textContent='翻譯中...';}
  const r=await api('/api/products/'+id+'/translate-images',{method:'POST',body:JSON.stringify({urls})});
  if(r.ok){
    toast('翻譯開始，約需 1-3 分鐘...');
    setTimeout(()=>pollTranslate(id,0),5000);
  } else {
    toast('翻譯失敗：'+(r.error||''));
    if(btn){btn.disabled=false;btn.textContent='文A 翻譯選取';}
  }
}
async function whitebgCat(catId, id){
  if(!id){toast('請先開啟商品');return;}
  const el=document.getElementById('cat_'+catId);
  if(!el) return;
  const checked=[...el.querySelectorAll('.img-thumb input[type=checkbox]:checked')].map(cb=>cb.dataset.url).filter(Boolean);
  const all=[...el.querySelectorAll('.img-thumb input[type=checkbox]')].map(cb=>cb.dataset.url).filter(Boolean);
  const urls=checked.length?checked:all;
  if(!urls.length){toast('此區沒有圖片');return;}
  toast('送出 '+urls.length+' 張，處理中...');
  try{
    const r=await api('/api/products/'+id+'/whitebg-selected',{method:'POST',body:JSON.stringify({urls}),headers:{'Content-Type':'application/json'}});
    if(r.ok){
      toast('白底圖處理中（'+r.count+' 張）...');
      pollWhitebg(id, 0);
    } else { toast('失敗：'+(r.error||'')); }
  }catch(e){ toast('錯誤：'+e.message); }
}
async function pollWhitebg(id, tries){
  if(tries>20){toast('白底圖處理超時，請至 Render log 查看錯誤');return;}
  try{
    const j=await api('/api/products/'+id);
    if(j.img_status==='done'){
      toast('白底圖完成！');
      if(openId===id) openJob(id);
    } else if(j.img_status==='failed'){
      toast('白底圖處理失敗，請查 Render log');
    } else {
      setTimeout(()=>pollWhitebg(id,tries+1),4000);
    }
  }catch(e){ setTimeout(()=>pollWhitebg(id,tries+1),4000); }
}
async function confirmSelect(id){
  const urls=[..._selImgs];
  const r=await api("/api/products/"+id+"/select-images",{method:"POST",body:JSON.stringify({urls})});
  if(r.ok) toast(`已確認 ${r.count} 張圖片為輸出圖`);
  else toast("儲存失敗："+r.error);
}
async function downloadZipSelected(id){
  const items=[];
  const cc={};
  ['main','sku','detail','review'].forEach(catId=>{
    const el=document.getElementById('cat_'+catId);
    if(!el) return;
    el.querySelectorAll('.img-thumb input[type=checkbox]:checked').forEach(cb=>{
      cc[catId]=(cc[catId]||0)+1;
      items.push({url:cb.dataset.url,cat:catId,idx:cc[catId]});
    });
  });
  // fallback: _selImgs not in any category
  const seen=new Set(items.map(i=>i.url)); let ex=0;
  [..._selImgs].forEach(url=>{ if(!seen.has(url)){ex++;items.push({url,cat:'img',idx:ex});} });
  if(!items.length){toast("請先勾選圖片");return;}
  toast("打包中，請稍候...");
  try{
    const res=await fetch(`/api/products/${id}/images/zip-selected?key=${KEY}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({items})});
    if(!res.ok){toast("下載失敗");return;}
    const blob=await res.blob();
    const a=document.createElement("a");
    a.href=URL.createObjectURL(blob);
    a.download=`product_${id}_images.zip`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast("下載完成");
  }catch(e){toast("下載失敗");}
}

function prevUpload(id){
  const inp=document.getElementById('upFiles_'+id);
  const preview=document.getElementById('upPreview_'+id);
  const count=document.getElementById('upCount_'+id);
  const btn=document.getElementById('upBtn_'+id);
  const files=[...inp.files];
  count.textContent=files.length+' 張已選';
  btn.disabled=files.length===0;
  preview.innerHTML=files.map(f=>{
    const url=URL.createObjectURL(f);
    return `<div class="img-thumb"><img src="${url}" style="object-fit:cover;width:80px;height:80px"></div>`;
  }).join('');
}
async function doUpload(id){
  const inp=document.getElementById('upFiles_'+id);
  const type=document.getElementById('upType_'+id).value;
  const btn=document.getElementById('upBtn_'+id);
  const files=[...inp.files];
  if(!files.length){toast('請選擇圖片');return;}
  btn.disabled=true; btn.textContent='上傳中...';
  const fd=new FormData();
  fd.append('type',type);
  files.forEach(f=>fd.append('files',f));
  try{
    const res=await fetch(`/api/products/${id}/upload-translated?key=${KEY}`,{method:'POST',body:fd});
    const j2=await res.json();
    if(j2.ok){
      toast(`上傳完成，${j2.added} 張`);
      inp.value='';
      document.getElementById('upCount_'+id).textContent='';
      document.getElementById('upPreview_'+id).innerHTML='';
      btn.disabled=true;
      if(openId===id) openJob(id);
    } else { toast('上傳失敗：'+(j2.error||'')); }
  }catch(e){ toast('錯誤：'+e.message); }
  finally{ btn.disabled=false; btn.textContent='上傳'; }
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
<div id="lbBox" class="lb-overlay hidden" onclick="lbClose()">
  <button class="lb-close" onclick="lbClose()">✕</button>
  <button class="lb-nav lb-prev" onclick="event.stopPropagation();lbPrev()">&#8249;</button>
  <div class="lb-content" onclick="event.stopPropagation()">
    <img id="lbImg" src="" alt="">
    <div class="lb-info"><span class="lb-label" id="lbLabel"></span><span class="lb-count" id="lbCount"></span></div>
  </div>
  <button class="lb-nav lb-next" onclick="event.stopPropagation();lbNext()">&#8250;</button>
</div>
</body></html>"""


# ═══════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════

# ── 後台路由 ──────────────────────────────────────────────────

@products_bp.route("/admin/products")
def admin_products():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next="/admin/products", error=None)
    return render_template_string(PRODUCTS_HTML, key=key)

@products_bp.route("/admin/products/store-scan")
def admin_store_scan():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next="/admin/products/store-scan", error=None)
    return render_template_string(STORE_SCAN_HTML, key=key)

BRAND_SETTINGS_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>品牌設定</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
.header{background:#1a1a1a;color:#fff;padding:14px 20px;display:flex;align-items:center;gap:14px}
.header a{color:#888;text-decoration:none;font-size:14px}
.header a:hover{color:#fff}
.header-title{font-size:17px;font-weight:700;flex:1}
.wrap{max-width:760px;margin:0 auto;padding:24px 16px}
.card{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.card-head{display:flex;align-items:center;gap:10px;margin-bottom:16px}
.brand-key{font-size:12px;font-weight:700;background:#1a1a1a;color:#fff;border-radius:6px;padding:2px 8px;letter-spacing:.5px}
.brand-name{font-size:16px;font-weight:700}
label{display:block;font-size:12px;color:#666;font-weight:600;margin-bottom:4px;margin-top:12px}
input[type=text],textarea{width:100%;border:1.5px solid #ddd;border-radius:8px;padding:9px 12px;font-size:14px;font-family:-apple-system,sans-serif;outline:none;resize:vertical}
input[type=text]:focus,textarea:focus{border-color:#1a1a1a}
.btn-save{background:#1a1a1a;color:#fff;border:none;border-radius:8px;padding:9px 22px;font-size:14px;font-weight:700;cursor:pointer;margin-top:14px}
.btn-save:hover{background:#333}
.btn-save:disabled{background:#aaa;cursor:default}
.toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%);background:#222;color:#fff;padding:10px 22px;border-radius:20px;font-size:14px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999}
.toast.show{opacity:1}
</style>
</head>
<body>
<div class="header">
  <a href="/admin/products?key={{ key }}">← 商品搬運</a>
  <div class="header-title">品牌設定</div>
</div>
<div class="wrap">
{% for p in profiles %}
<div class="card" id="card_{{ p.brand_key }}">
  <div class="card-head">
    <span class="brand-key">{{ p.brand_key }}</span>
    <span class="brand-name">{{ p.name }}</span>
  </div>
  <label>品牌名稱</label>
  <input type="text" id="name_{{ p.brand_key }}" value="{{ p.name }}">
  <label>商品分類</label>
  <input type="text" id="category_{{ p.brand_key }}" value="{{ p.category }}">
  <label>文案風格</label>
  <textarea id="style_{{ p.brand_key }}" rows="3">{{ p.style }}</textarea>
  <label>文案語氣</label>
  <textarea id="tone_{{ p.brand_key }}" rows="2">{{ p.tone }}</textarea>
  <label>自訂 Prompt（選填）</label>
  <textarea id="custom_prompt_{{ p.brand_key }}" rows="3">{{ p.custom_prompt }}</textarea>
  <button class="btn-save" onclick="save('{{ p.brand_key }}', this)">儲存</button>
</div>
{% endfor %}
</div>
<div class="toast" id="toast"></div>
<script>
const KEY = '{{ key }}';
function toast(msg){
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'), 2200);
}
async function save(bk, btn){
  btn.disabled = true;
  const body = {
    name:          document.getElementById('name_'+bk).value.trim(),
    category:      document.getElementById('category_'+bk).value.trim(),
    style:         document.getElementById('style_'+bk).value.trim(),
    tone:          document.getElementById('tone_'+bk).value.trim(),
    custom_prompt: document.getElementById('custom_prompt_'+bk).value.trim(),
  };
  try{
    const r = await fetch('/api/brand-profiles/'+bk+'?key='+KEY, {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    const j = await r.json();
    if(j.ok) toast('✓ '+bk+' 已儲存');
    else toast('儲存失敗');
  }catch(e){ toast('錯誤：'+e.message); }
  finally{ btn.disabled = false; }
}
</script>
</body>
</html>"""


@products_bp.route("/admin/brand-settings")
def admin_brand_settings():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next="/admin/brand-settings", error=None)
    profiles = _bp_all()
    return render_template_string(BRAND_SETTINGS_HTML, key=key, profiles=profiles)

@products_bp.route("/api/brand-profiles", methods=["GET"])
def api_brand_profiles_list():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({"profiles": _bp_all()})

@products_bp.route("/api/brand-profiles/<brand_key>", methods=["PUT"])
def api_brand_profiles_save(brand_key):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    success = _bp_save(
        brand_key,
        data.get("name", ""),
        data.get("category", ""),
        data.get("style", ""),
        data.get("tone", ""),
        data.get("custom_prompt", ""),
    )
    return jsonify({"ok": success})

# ── API 路由 ──────────────────────────────────────────────────

@products_bp.route("/api/products", methods=["POST"])
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
    brand = (data.get("brand") or "").strip()
    job_id = _pj_insert(url, platform, brand)
    if not job_id:
        return jsonify({"error": "建立任務失敗，請確認資料庫連線"}), 500
    return jsonify({"ok": True, "id": job_id, "platform": platform, "brand": brand})

@products_bp.route("/api/products", methods=["GET"])
def api_products_list():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({"jobs": _pj_list(50)})

@products_bp.route("/api/products/<int:job_id>", methods=["GET"])
def api_products_get(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    job = _pj_get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)

@products_bp.route("/api/products/<int:job_id>", methods=["DELETE"])
def api_products_delete(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    _pj_delete(job_id)
    return jsonify({"ok": True})

@products_bp.route("/api/products/<int:job_id>/images", methods=["POST"])
def api_products_images(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])
    cleaned = _clean_images(urls)
    _pj_update(job_id, raw_images=json.dumps(cleaned, ensure_ascii=False))
    return jsonify({"ok": True, "count": len(cleaned)})

@products_bp.route("/api/products/<int:job_id>", methods=["PUT"])
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

@products_bp.route("/api/products/<int:job_id>/process-images", methods=["POST"])
def api_products_process_images(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    job = _pj_get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    selected_urls = data.get("urls", [])
    target_imgs = selected_urls if selected_urls else job.get("raw_images", [])
    if not target_imgs:
        return jsonify({"error": "沒有圖片可處理"}), 400
    update_fields = {"img_status": "pending_images"}
    if selected_urls:
        update_fields["raw_images"] = json.dumps(selected_urls, ensure_ascii=False)
    _pj_update(job_id, **update_fields)
    return jsonify({"ok": True})

@products_bp.route("/api/products/<int:job_id>/select-images", methods=["POST"])
def api_products_select_images(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    job = _pj_get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True)
    urls = [u.strip() for u in (data.get("urls") or []) if u.strip()]
    _pj_update(job_id, raw_images=json.dumps(urls, ensure_ascii=False))
    return jsonify({"ok": True, "count": len(urls)})


@products_bp.route("/api/products/<int:job_id>/images/zip-selected", methods=["POST"])
def api_products_images_zip_selected(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    job = _pj_get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True)
    items = data.get("items")
    if not items:
        old_urls = data.get("urls", [])
        items = [{"url": u, "cat": "img", "idx": i+1} for i, u in enumerate(old_urls)]
    if not items:
        return jsonify({"error": "沒有選取圖片"}), 400
    import io, zipfile
    buf = io.BytesIO()
    downloaded = 0
    cat_counter = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            img_url = item.get("url", "")
            cat     = item.get("cat", "img")
            if not img_url: continue
            cat_counter[cat] = cat_counter.get(cat, 0) + 1
            idx = cat_counter[cat]
            try:
                req = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.1688.com/"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    img_data = r.read()
                ext = img_url.split(".")[-1].split("?")[0].lower()
                if ext not in ("jpg", "jpeg", "png", "webp"): ext = "jpg"
                zf.writestr(f"{cat}_{idx:02d}.{ext}", img_data)
                downloaded += 1
            except Exception:
                pass
    if downloaded == 0:
        return jsonify({"error": "圖片下載失敗"}), 500
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="product_{job_id}_images.zip"'})




def _get_cjk_font(size=22):
    """取得支援中文的字型，找不到就下載 NotoSans"""
    from PIL import ImageFont
    import os, urllib.request as _ureq
    paths = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/tmp/NotoSansCJK.ttc",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    cache = "/tmp/NotoSansCJK.ttc"
    if not os.path.exists(cache):
        try:
            _ureq.urlretrieve(
                "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTF/TraditionalChinese/NotoSansCJKtc-Regular.otf",
                cache
            )
            return ImageFont.truetype(cache, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _sample_bg_info(img_pil, x1, y1, x2, y2):
    """返回 (variance, avg_brightness, edge_color)。純 Pillow，不依賴 numpy。
    variance  = RGB 各 channel stddev 的 RMS（等效 np.std 行為）
    brightness= 灰階平均亮度
    edge_color= bbox 外圍 3px 採樣均色（避開文字像素）
    """
    from PIL import ImageStat
    W, H = img_pil.size
    region = img_pil.crop((x1, y1, x2, y2))
    rw, rh = region.size
    if rw <= 0 or rh <= 0:
        return 0.0, 200.0, (255, 255, 255)

    stat_rgb = ImageStat.Stat(region)
    variance = (sum(v * v for v in stat_rgb.stddev) / len(stat_rgb.stddev)) ** 0.5

    stat_l = ImageStat.Stat(region.convert("L"))
    avg_brightness = stat_l.mean[0]

    # 背景色：從 bbox 外圍 3px 採樣，避免文字像素干擾
    ox1, oy1 = max(0, x1 - 3), max(0, y1 - 3)
    ox2, oy2 = min(W, x2 + 3), min(H, y2 + 3)
    stat_outer = ImageStat.Stat(img_pil.crop((ox1, oy1, ox2, oy2)))
    edge_color = tuple(int(v) for v in stat_outer.mean[:3])

    return variance, avg_brightness, edge_color


def _fit_and_draw(draw, xy, text, font, text_color=(15, 15, 15)):
    """貼上翻譯文字，支援自訂文字顏色"""
    draw.text(xy, text, fill=text_color, font=font)


def _translate_images_job(job_id, img_urls):
    """
    圖片翻譯 v2：結構化 OCR → 語言過濾 → 依 variance 分流 Pillow/Stability → 動態字型
    只翻簡體中文，日文/英文/數字一律跳過。
    variance < 20 → pillow | 20-50 → pillow+pad | >= 50 → stability
    brightness < 128 → 白字 | >= 128 → 深色字
    """
    import io, base64, re as _re3, traceback
    try:
        import requests as _req
        from PIL import Image, ImageDraw
    except ImportError as e:
        print(f"[translate] import error: {e}")
        _pj_update(job_id, translate_status="failed")
        return

    STABILITY_KEY = os.getenv("STABILITY_API_KEY", "")
    if not STABILITY_KEY:
        print("[translate] STABILITY_API_KEY not set — complex blocks will fallback to Pillow")

    OCR_PROMPT = (
        '請偵測圖片中所有文字區塊，直接輸出 JSON（不要 markdown）。\n'
        '格式：{"blocks":[{"text":"原文","language":"zh-CN/zh-TW/ja/en/number/mixed",'
        '"should_translate":true/false,"translated_text":"繁體（false時留空字串）",'
        '"bbox":[x1,y1,x2,y2],"font_role":"title/subtitle/body/label/number",'
        '"background_type":"white/solid/complex"}]}\n'
        '規則（嚴格遵守）：\n'
        '1. language分類：zh-CN=簡體中文漢字，zh-TW=繁體中文，ja=日文（含ひらがな/カタカナ），en=英文，number=純數字百分比\n'
        '2. should_translate只有純zh-CN才true，其他全部false\n'
        '3. translated_text：簡體→台灣繁體，混合文字只翻中文部分，英文/數字原樣保留\n'
        '4. bbox：[左%,上%,右%,下%]，圖片寬高各為100\n'
        '5. font_role：title=主標題大字，subtitle=副標題，body=說明文字，label=小標籤，number=數字\n'
        '6. background_type：white=白色背景，solid=純色背景，complex=照片/漸層/複雜背景\n'
        '範例（此圖色卡）：\n'
        '- 「海盐蓝」→zh-CN,true,「海鹽藍」,title,white\n'
        '- 「クリームホワイト」→ja,false,\"\",label,white\n'
        '- 「Shading：80%」→en,false,\"\",number,white\n'
        '- 「遮光率：80%」→zh-CN,true,「遮光率：80%」,label,white（數字保留不翻）\n'
        '只輸出JSON，不要說明文字。'
    )

    def _pct2px(pct, dim):
        return max(0, min(dim, int(pct / 100 * dim)))

    def _sample_bg(base_img, x1, y1, x2, y2):
        W, H = base_img.size
        samples = []
        for px, py in [(max(0,x1-8),max(0,y1-8)), (min(W-1,x2+8),max(0,y1-8)),
                       (max(0,x1-8),min(H-1,y2+8)), (min(W-1,x2+8),min(H-1,y2+8))]:
            try:
                s = base_img.getpixel((px, py))
                samples.append(s[:3] if len(s) > 3 else s)
            except Exception:
                pass
        if not samples:
            return (255, 255, 255)
        return tuple(sum(s[i] for s in samples) // len(samples) for i in range(3))

    def _fit_and_draw(draw, text, x1, y1, x2, y2, role, text_color=(15, 15, 15)):
        from PIL import ImageFont
        box_w, box_h = x2 - x1, y2 - y1
        start_sz = int(box_h * (0.80 if role == "title" else 0.75))
        start_sz = max(10, min(start_sz, 120))
        font, chosen_sz = None, start_sz
        for sz in range(start_sz, 7, -2):
            try:
                f = _get_cjk_font(sz)
                try:
                    bb = f.getbbox(text)
                    tw = bb[2] - bb[0]
                except Exception:
                    tw = len(text) * sz * 0.65
                if tw <= box_w * 1.05:
                    font, chosen_sz = f, sz
                    break
            except Exception:
                pass
        if font is None:
            font = _get_cjk_font(10)
        try:
            bb = font.getbbox(text)
            tw, th = bb[2]-bb[0], bb[3]-bb[1]
        except Exception:
            tw, th = len(text)*chosen_sz, chosen_sz
        # Horizontal alignment
        if role in ("title", "subtitle"):
            tx = x1 + max(0, (box_w - tw) // 2)
        else:
            tx = x1 + 4
        ty = y1 + max(0, (box_h - th) // 2)
        draw.text((tx, ty), text, fill=text_color, font=font)

    # ── main loop ─────────────────────────────────────────────
    translated_urls = []
    stats = dict(total=0, translated=0, skip_ja=0, skip_en=0, skip_num=0,
                 skip_tw=0, pillow=0, stab=0, stab_fail=0)

    for img_idx, url in enumerate(img_urls):
        print(f"[translate] {img_idx+1}/{len(img_urls)} {url[:70]}")
        try:
            # 1. Download
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://www.1688.com/"})
            with urllib.request.urlopen(req, timeout=20) as r:
                img_bytes = r.read()
            media_type = "image/jpeg"
            if img_bytes[:8] == b'\x89PNG\r\n\x1a\n': media_type = "image/png"
            elif img_bytes[:4] == b'RIFF': media_type = "image/webp"

            # 2. Claude Vision OCR
            img_b64 = base64.standard_b64encode(img_bytes).decode()
            claude_resp = urllib.request.urlopen(
                urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=json.dumps({
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 3000,
                        "messages": [{"role":"user","content":[
                            {"type":"image","source":{"type":"base64","media_type":media_type,"data":img_b64}},
                            {"type":"text","text":OCR_PROMPT}
                        ]}]
                    }).encode(),
                    headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
                    method="POST"
                ), timeout=45
            )
            raw_text = json.loads(claude_resp.read())["content"][0]["text"].strip()
            m = _re3.search(r'\{[\s\S]*\}', raw_text)
            if not m:
                print("  [OCR] no JSON"); translated_urls.append(url); continue

            blocks = json.loads(m.group()).get("blocks", [])
            stats['total'] += len(blocks)
            print(f"  [OCR] {len(blocks)} 區塊")

            # 3. Filter
            to_do = []
            for b in blocks:
                lang = b.get("language","")
                if not b.get("should_translate") or not b.get("translated_text","") or lang != "zh-CN":
                    if lang=="ja": stats['skip_ja']+=1
                    elif lang=="en": stats['skip_en']+=1
                    elif lang=="number": stats['skip_num']+=1
                    elif lang=="zh-TW": stats['skip_tw']+=1
                    continue
                stats['translated']+=1
                to_do.append(b)

            if not to_do:
                print("  [OCR] 無需翻譯"); translated_urls.append(url); continue

            # 4. Convert bbox % → px，同時計算 variance / brightness
            from PIL import Image
            img_pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            W, H = img_pil.size
            for b in to_do:
                bx = b.get("bbox", [0, 0, 100, 100])
                b['_px'] = (_pct2px(bx[0],W), _pct2px(bx[1],H), _pct2px(bx[2],W), _pct2px(bx[3],H))

            # 5. 依 variance 分類（不盲信 Claude background_type）
            # var < 20 → pillow | 20-50 → pillow+pad | >= 50 → stability
            simple  = []
            complex_ = []
            for b in to_do:
                x1, y1, x2, y2 = b['_px']
                variance, brightness, edge_color = _sample_bg_info(img_pil, x1, y1, x2, y2)
                b['_variance'] = variance
                b['_brightness'] = brightness
                b['_edge_color'] = edge_color
                b['_text_color'] = (255, 255, 255) if brightness < 128 else (15, 15, 15)
                color_label = "white" if brightness < 128 else "dark"
                bg_hint = b.get("background_type", "white")

                if variance < 20:
                    b['_pad'] = 2; simple.append(b)
                    print(f"    [{b.get('text','')[:12]}] var={variance:.1f} bright={brightness:.0f} ({bg_hint}) → pillow | {color_label}")
                elif variance < 50:
                    b['_pad'] = 6; simple.append(b)
                    print(f"    [{b.get('text','')[:12]}] var={variance:.1f} bright={brightness:.0f} ({bg_hint}) → pillow+pad | {color_label}")
                else:
                    complex_.append(b)
                    print(f"    [{b.get('text','')[:12]}] var={variance:.1f} bright={brightness:.0f} ({bg_hint}) → stability | {color_label}")

            stability_saved = len(simple)
            print(f"  [classify] pillow={len(simple)} stability={len(complex_)} | 本張省下 {stability_saved}/{len(to_do)} 次 Stability 呼叫")

            result_img = img_pil.copy()

            # 6a. Pillow cover (white/solid/simple)
            # pad = 2~6px（來自 variance 分類），不用 bbox 百分比避免巨大方塊
            if simple:
                draw = ImageDraw.Draw(result_img)
                for b in simple:
                    x1,y1,x2,y2 = b['_px']
                    role = b.get("font_role","body")
                    pad = b.get('_pad', 4)
                    rx1,ry1 = max(0,x1-pad), max(0,y1-pad)
                    rx2,ry2 = min(W,x2+pad), min(H,y2+pad)
                    bg = b.get('_edge_color') or _sample_bg(img_pil, rx1,ry1,rx2,ry2)
                    draw.rectangle([rx1,ry1,rx2,ry2], fill=bg)
                    _fit_and_draw(draw, b.get("translated_text",""), rx1,ry1,rx2,ry2, role, b['_text_color'])
                    stats['pillow']+=1
                    clabel = "white" if b.get('_brightness', 200) < 128 else "dark"
                    print(f"    Pillow [{role}]: '{b.get('text','')}' → '{b.get('translated_text','')}' | {clabel}")

            # 6b. 無 STABILITY_KEY：complex 也走 Pillow fallback
            if complex_ and not STABILITY_KEY:
                draw_fb = ImageDraw.Draw(result_img)
                for b in complex_:
                    x1,y1,x2,y2 = b['_px']
                    role = b.get("font_role","body")
                    pad = 6
                    rx1,ry1 = max(0,x1-pad),max(0,y1-pad)
                    rx2,ry2 = min(W,x2+pad),min(H,y2+pad)
                    bg = b.get('_edge_color') or _sample_bg(img_pil,rx1,ry1,rx2,ry2)
                    draw_fb.rectangle([rx1,ry1,rx2,ry2],fill=bg)
                    _fit_and_draw(draw_fb, b.get("translated_text",""), rx1,ry1,rx2,ry2, role, b['_text_color'])
                    stats['pillow']+=1
                complex_ = []
                print(f"  [Stability] skipped (no key) — fell back to Pillow for all blocks")

            # 6c. Stability inpaint (complex photo backgrounds only)
            if complex_:
                from PIL import ImageDraw as _ID2
                mask = Image.new("L",(W,H),0)
                dm = _ID2.Draw(mask)
                for b in complex_:
                    x1,y1,x2,y2 = b['_px']
                    mp = 8  # mask padding: 略大一點讓 inpaint 邊緣乾淨
                    dm.rectangle([max(0,x1-mp),max(0,y1-mp),min(W,x2+mp),min(H,y2+mp)],fill=255)

                ib = io.BytesIO(); result_img.save(ib,"PNG"); ib.seek(0)
                mb = io.BytesIO(); mask.convert("RGB").save(mb,"PNG"); mb.seek(0)
                inpaint_ok = False
                try:
                    sr = _req.post(
                        "https://api.stability.ai/v2beta/stable-image/edit/erase",
                        headers={"Authorization":f"Bearer {STABILITY_KEY}","Accept":"image/*"},
                        files={"image":("i.png",ib.getvalue(),"image/png"),
                               "mask":("m.png",mb.getvalue(),"image/png")},
                        data={"output_format":"png"}, timeout=60
                    )
                    if sr.status_code == 200:
                        result_img = Image.open(io.BytesIO(sr.content)).convert("RGB")
                        stats['stab']+=len(complex_)
                        inpaint_ok = True
                        print(f"    Stability: {len(complex_)} 區塊")
                    else:
                        print(f"    Stability {sr.status_code} → Pillow fallback")
                        stats['stab_fail']+=len(complex_)
                except Exception as se:
                    print(f"    Stability error: {se} → Pillow fallback")
                    stats['stab_fail']+=len(complex_)

                if not inpaint_ok:
                    draw_fb = ImageDraw.Draw(result_img)
                    for b in complex_:
                        x1,y1,x2,y2 = b['_px']
                        bg = b.get('_edge_color') or _sample_bg(img_pil,x1,y1,x2,y2)
                        draw_fb.rectangle([x1,y1,x2,y2],fill=bg)
                    stats['pillow']+=len(complex_)

                # Overlay text for complex（直接用原始 bbox，Stability 已清除原文）
                draw_c = ImageDraw.Draw(result_img)
                for b in complex_:
                    x1,y1,x2,y2 = b['_px']
                    role = b.get("font_role","body")
                    _fit_and_draw(draw_c, b.get("translated_text",""),
                                  x1,y1,x2,y2, role,
                                  b.get('_text_color', (15,15,15)))

            # 7. Debug stats
            print(f"  [stats] OCR:{stats['total']} 翻:{stats['translated']} 跳日:{stats['skip_ja']} 跳英:{stats['skip_en']} 跳數:{stats['skip_num']} 跳繁:{stats['skip_tw']} Pillow:{stats['pillow']} Stab:{stats['stab']} Stab失敗:{stats['stab_fail']}")

            # 8. Upload
            out = io.BytesIO()
            result_img.save(out,"JPEG",quality=93)
            fname = f"translated_{job_id}_{img_idx+1}.jpg"
            turl,_ = upload_image_to_supabase(fname, out.getvalue(), "image/jpeg")
            if not turl:
                turl,_ = upload_image_to_github(fname, out.getvalue())
            translated_urls.append(turl or url)
            print(f"  [done] {(turl or url)[:70]}")

        except Exception as e:
            print(f"  [ERROR] {e}")
            traceback.print_exc()
            translated_urls.append(url)

    _pj_update(job_id,
               translated_images=json.dumps(translated_urls, ensure_ascii=False),
               translate_status="done")
    print(f"[translate] 完成 {len(translated_urls)}/{len(img_urls)} 張，stats={stats}")

@products_bp.route("/api/products/<int:job_id>/translate-images", methods=["POST"])
def api_translate_images(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    if not os.getenv("STABILITY_API_KEY"):
        return jsonify({"error": "STABILITY_API_KEY 未設定"}), 500
    job = _pj_get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True)
    urls = [u.strip() for u in (data.get("urls") or []) if u.strip()]
    if not urls:
        return jsonify({"error": "請選取要翻譯的圖片"}), 400
    _pj_update(job_id, translate_status="processing")
    import threading
    threading.Thread(target=_translate_images_job, args=(job_id, urls), daemon=True).start()
    return jsonify({"ok": True, "count": len(urls)})


@products_bp.route("/api/products/<int:job_id>/upload-translated", methods=["POST"])
def api_upload_translated(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    job = _pj_get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    img_type = request.form.get("type", "main")
    if img_type not in ("main", "detail", "sku"):
        img_type = "main"
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "沒有上傳圖片"}), 400
    uploaded_urls = []
    for i, f in enumerate(files[:20]):
        img_bytes = f.read()
        if not img_bytes: continue
        fname_orig = f.filename or f"img_{i+1}.jpg"
        ext = fname_orig.rsplit(".", 1)[-1].lower() if "." in fname_orig else "jpg"
        if ext not in ("jpg", "jpeg", "png", "webp"): ext = "jpg"
        filename = f"products/{job_id}_tr_{img_type}_{int(time.time())}_{i+1}.{ext}"
        pub_url, _ = upload_image_to_supabase(filename, img_bytes, f.content_type or "image/jpeg")
        if pub_url:
            uploaded_urls.append(pub_url)
    if not uploaded_urls:
        return jsonify({"error": "上傳失敗，請檢查 Supabase 設定"}), 500
    pi = job.get("product_images") or {}
    tr_key = f"tr_{img_type}_images"
    pi[tr_key] = (pi.get(tr_key) or []) + uploaded_urls
    _pj_update(job_id, product_images=json.dumps(pi, ensure_ascii=False))
    return jsonify({"ok": True, "added": len(uploaded_urls), "urls": uploaded_urls, "type": img_type})


def _download_image_robust(url):
    """Download image with multiple strategies for CDN hotlink protection."""
    import sys
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    headers_list = [
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
         "Referer": "https://www.1688.com/", "Accept": "image/webp,image/apng,image/*,*/*"},
        {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
         "Referer": "https://detail.tmall.com/", "Accept": "image/*,*/*"},
        {"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
    ]
    try:
        import requests as _req
        for hdrs in headers_list:
            try:
                r = _req.get(url, headers=hdrs, timeout=20, stream=True)
                if r.status_code == 200:
                    return r.content
            except Exception as e:
                print(f"[WhiteBG] download attempt failed: {e}", file=sys.stderr)
    except ImportError:
        pass
    # fallback urllib
    try:
        req = urllib.request.Request(url, headers=headers_list[0])
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception as e:
        print(f"[WhiteBG] urllib fallback failed: {e}", file=sys.stderr)
    return None


def _whitebg_selected_job(job_id, img_urls):
    import sys
    print(f"[WhiteBG] job={job_id} urls={len(img_urls)}", file=sys.stderr)
    job = _pj_get(job_id)
    if not job:
        print(f"[WhiteBG] job {job_id} not found", file=sys.stderr)
        return
    existing = job.get("processed_images", [])
    existing_set = set(existing)
    new_processed = list(existing)
    for i, url in enumerate(img_urls[:20]):
        print(f"[WhiteBG] [{i+1}/{len(img_urls)}] downloading {url[:80]}", file=sys.stderr)
        img_bytes = _download_image_robust(url)
        if not img_bytes:
            print(f"[WhiteBG] download failed for img {i+1}", file=sys.stderr)
            continue
        print(f"[WhiteBG] downloaded {len(img_bytes)} bytes, processing...", file=sys.stderr)
        result = _process_to_white_bg(img_bytes)
        if not result:
            print(f"[WhiteBG] PIL processing failed for img {i+1}", file=sys.stderr)
            continue
        filename = f"products/{job_id}_sel_{int(time.time())}_{i}.jpg"
        pub_url, err = upload_image_to_supabase(filename, result, "image/jpeg")
        if err:
            print(f"[WhiteBG] Supabase upload failed: {err}", file=sys.stderr)
        if pub_url and pub_url not in existing_set:
            new_processed.append(pub_url)
            existing_set.add(pub_url)
            print(f"[WhiteBG] uploaded: {pub_url[:60]}", file=sys.stderr)
    added = len(new_processed) - len(existing)
    print(f"[WhiteBG] done, added {added} new images", file=sys.stderr)
    _pj_update(job_id,
               processed_images=json.dumps(new_processed, ensure_ascii=False),
               img_status="done" if new_processed else "failed")

@products_bp.route("/api/products/<int:job_id>/whitebg-selected", methods=["POST"])
def api_whitebg_selected(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    job = _pj_get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True)
    urls = [u.strip() for u in (data.get("urls") or []) if u.strip()]
    if not urls:
        return jsonify({"error": "請選取圖片"}), 400
    import threading
    threading.Thread(target=_whitebg_selected_job, args=(job_id, urls), daemon=True).start()
    return jsonify({"ok": True, "count": len(urls)})


# ── 本機 Worker API ───────────────────────────────────────────

# ── 匯出 / 下載 ────────────────────────────────────────────────

def _pj_list_done():
    if not DATABASE_URL:
        return []
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id,url,platform,raw_title,raw_price,ai_name,ai_desc,ai_keywords,raw_images,processed_images,created_at FROM product_jobs WHERE status='done' ORDER BY created_at DESC"
        )
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id":r[0],"url":r[1],"platform":r[2],"raw_title":r[3],"raw_price":r[4],
                 "ai_name":r[5],"ai_desc":r[6],"ai_keywords":r[7],
                 "raw_images":json.loads(r[8] or "[]"),
                 "processed_images":json.loads(r[9] or "[]"),
                 "created_at":r[10]} for r in rows]
    except Exception:
        return []

def _export_csv(jobs):
    import csv, io
    from flask import Response
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["平台","AI商品名稱","AI商品描述","SEO關鍵字","原始標題","原始價格","來源URL","圖片URL_1","圖片URL_2","圖片URL_3"])
    for j in jobs:
        imgs = j.get("processed_images") or j.get("raw_images",[])
        w.writerow([
            j["platform"], j["ai_name"], j["ai_desc"], j["ai_keywords"],
            j["raw_title"], j["raw_price"], j["url"],
            imgs[0] if len(imgs)>0 else "",
            imgs[1] if len(imgs)>1 else "",
            imgs[2] if len(imgs)>2 else "",
        ])
    output = buf.getvalue().encode("utf-8-sig")
    return Response(output, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=products.csv"})

def _export_xlsx(jobs):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        import openpyxl.utils
    except ImportError:
        return jsonify({"error": "openpyxl 未安裝"}), 500
    import io
    from flask import Response
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "商品資料"
    headers = ["平台","AI商品名稱","AI商品描述","SEO關鍵字","原始標題","原始價格","來源URL","圖片URL_1","圖片URL_2","圖片URL_3"]
    hfill = PatternFill("solid", fgColor="1a1a1a")
    hfont = Font(color="FFFFFF", bold=True)
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.fill = hfill; cell.font = hfont
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    col_widths = [10, 35, 60, 30, 35, 12, 55, 55, 55, 55]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    for ri, j in enumerate(jobs, 2):
        imgs = j.get("processed_images") or j.get("raw_images",[])
        values = [
            j["platform"], j["ai_name"], j["ai_desc"], j["ai_keywords"],
            j["raw_title"], j["raw_price"], j["url"],
            imgs[0] if len(imgs)>0 else "",
            imgs[1] if len(imgs)>1 else "",
            imgs[2] if len(imgs)>2 else "",
        ]
        for ci, v in enumerate(values, 1):
            cell = ws.cell(row=ri, column=ci, value=v)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[ri].height = 60
    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    return Response(buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=products.xlsx"})

@products_bp.route("/api/products/export")
def api_products_export():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    fmt = request.args.get("format", "xlsx")
    jobs = _pj_list_done()
    if not jobs:
        return jsonify({"error": "沒有已完成的商品"}), 400
    if fmt == "csv":
        return _export_csv(jobs)
    return _export_xlsx(jobs)

@products_bp.route("/api/products/<int:job_id>/images/zip")
def api_products_images_zip(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    job = _pj_get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    img_type = request.args.get("type", "processed")
    if img_type == "raw":
        imgs = job.get("raw_images", [])
    elif img_type == "sku":
        pi = job.get("product_images", {})
        skus = pi.get("sku_images", [])
        imgs = [i["src"] if isinstance(i, dict) else i for i in skus]
    else:
        imgs = job.get("processed_images") or job.get("raw_images", [])
    if not imgs:
        return jsonify({"error": "沒有圖片可下載"}), 400
    import io, zipfile, re as _re
    from flask import Response
    buf = io.BytesIO()
    downloaded = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, img_url in enumerate(imgs[:10]):
            try:
                req = urllib.request.Request(img_url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://www.1688.com/"})
                with urllib.request.urlopen(req, timeout=12) as r:
                    data = r.read()
                ext = img_url.split(".")[-1].split("?")[0].lower()
                if ext not in ("jpg","jpeg","png","webp"): ext = "jpg"
                zf.writestr(f"img_{i+1:02d}.{ext}", data)
                downloaded += 1
            except Exception:
                pass
    if downloaded == 0:
        return jsonify({"error": "圖片下載失敗"}), 500
    buf.seek(0)
    safe = _re.sub(r'[^\w]', '_', (job.get("ai_name") or "product")[:20])
    return Response(buf.getvalue(), mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}_images.zip"'})

# ── 本機 Worker API ───────────────────────────────────────────

@products_bp.route("/api/products/pending", methods=["GET"])
def api_products_pending():
    """本機 Worker 輪詢：取得待爬取的任務列表。"""
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    if not DATABASE_URL:
        return jsonify({"jobs": []})
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id, url, platform, raw_images, img_status FROM product_jobs"
            " WHERE status='pending' OR img_status='pending_images'"
            " ORDER BY created_at ASC LIMIT 10"
        )
        rows = cur.fetchall(); cur.close(); conn.close()
        jobs = []
        for r in rows:
            job_id_r, url, platform, raw_imgs_json, img_status = r
            if img_status == "pending_images":
                jobs.append({"id": job_id_r, "url": url, "platform": platform,
                             "mode": "images_only",
                             "raw_images": json.loads(raw_imgs_json or "[]")})
            else:
                jobs.append({"id": job_id_r, "url": url, "platform": platform})
        return jsonify({"jobs": jobs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@products_bp.route("/api/products/from-extension", methods=["POST"])
def api_products_from_extension():
    """Chrome Extension 直送：不需要 Worker，直接存圖 + 觸發 AI 改寫。"""
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    url      = (data.get("url") or "").strip()
    platform = data.get("platform", "1688")
    brand    = data.get("brand", "")
    title    = data.get("title", "")
    product_imgs = data.get("product_images", {})
    if not url:
        return jsonify({"error": "缺少 url"}), 400
    job_id = _pj_insert(url, platform, brand)
    if not job_id:
        return jsonify({"error": "DB error"}), 500
    main_srcs = [i["src"] if isinstance(i, dict) else i for i in product_imgs.get("main_images", [])]
    # 核心欄位先更新（確保 status 改變、AI thread 能跑）
    _pj_update(job_id,
        status    = "scraping",
        raw_title = title,
        raw_images= json.dumps(main_srcs, ensure_ascii=False),
    )
    # product_images 欄位分開更新（若 column 不存在不影響主流程）
    _pj_update(job_id, product_images=json.dumps(product_imgs, ensure_ascii=False))
    threading.Thread(target=_run_ai_rewrite_for_job, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True, "id": job_id})

@products_bp.route("/api/products/<int:job_id>/scrape-result", methods=["POST"])
def api_products_scrape_result(job_id):
    """本機 Worker 回傳爬取結果，觸發 AI 改寫。"""
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}

    if data.get("error"):
        _pj_update(job_id, status="error", error_msg=f"本機爬取失敗：{data['error']}")
        return jsonify({"ok": True})

    if data.get("mode") == "images_only":
        processed = data.get("processed_images", [])
        _pj_update(job_id,
            processed_images = json.dumps(processed, ensure_ascii=False),
            img_status       = "done" if processed else "failed",
        )
        return jsonify({"ok": True})

    processed = data.get("processed_images", [])
    product_imgs = data.get("product_images", {})
    _pj_update(job_id,
        status="scraping",
        raw_title         = data.get("raw_title", ""),
        raw_desc          = data.get("raw_desc", ""),
        raw_images        = json.dumps(data.get("raw_images", []), ensure_ascii=False),
        raw_price         = data.get("raw_price", ""),
        raw_extra         = data.get("raw_extra", "{}"),
        processed_images  = json.dumps(processed, ensure_ascii=False),
        img_status        = "done" if processed else "",
        product_images    = json.dumps(product_imgs, ensure_ascii=False),
    )
    threading.Thread(target=_run_ai_rewrite_for_job, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True})


# ── Store Scan API ────────────────────────────────────────────

@products_bp.route("/api/store-scan", methods=["POST"])
def api_store_scan_create():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "請提供店鋪/分類頁連結"}), 400
    platform = _detect_platform(url)
    if platform == "unknown":
        return jsonify({"error": "目前只支援 1688 和 淘寶 連結"}), 400
    job_id = _ss_insert(url, platform)
    if not job_id:
        return jsonify({"error": "建立失敗，請確認資料庫連線"}), 500
    return jsonify({"ok": True, "id": job_id, "platform": platform})

@products_bp.route("/api/store-scan", methods=["GET"])
def api_store_scan_list():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({"jobs": _ss_list(20)})

@products_bp.route("/api/store-scan/pending", methods=["GET"])
def api_store_scan_pending():
    """本機 Worker 輪詢：取得待掃描任務。"""
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    if not DATABASE_URL:
        return jsonify({"jobs": []})
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id, url, platform FROM store_scan_jobs WHERE status='pending' ORDER BY created_at ASC LIMIT 3"
        )
        rows = cur.fetchall(); cur.close(); conn.close()
        return jsonify({"jobs": [{"id": r[0], "url": r[1], "platform": r[2]} for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@products_bp.route("/api/store-scan/<int:job_id>", methods=["GET"])
def api_store_scan_get(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    job = _ss_get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)

@products_bp.route("/api/store-scan/<int:job_id>/result", methods=["POST"])
def api_store_scan_result(job_id):
    """本機 Worker 回傳掃描結果。"""
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    if data.get("error"):
        _ss_update(job_id, status="error", error_msg=data["error"])
        return jsonify({"ok": True})
    items = data.get("items", [])
    count = _ss_insert_items(job_id, items)
    _ss_update(job_id, status="done", item_count=count)
    return jsonify({"ok": True, "count": count})

@products_bp.route("/api/store-scan/to-queue", methods=["POST"])
def api_store_scan_to_queue():
    """將勾選商品加入 product_jobs。"""
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    item_ids = data.get("item_ids", [])
    brand = (data.get("brand") or "").strip()
    if not item_ids:
        return jsonify({"error": "請勾選商品"}), 400
    if not DATABASE_URL:
        return jsonify({"error": "資料庫未設定"}), 500
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id,url,platform FROM store_scan_items WHERE id=ANY(%s) AND added_to_queue=FALSE",
            (item_ids,)
        )
        rows = cur.fetchall(); cur.close(); conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    added = []
    for r in rows:
        item_id, url, platform = r
        job_id = _pj_insert(url, platform, brand)
        if job_id:
            added.append({"item_id": item_id, "job_id": job_id})
    if added:
        _ss_mark_added([a["item_id"] for a in added])
    return jsonify({"ok": True, "added": len(added), "jobs": added})


# ── Product Rules DB helpers ──────────────────────────────────────

def _pr_init():
    """建立 product_rules 資料表（若不存在）。"""
    if not DATABASE_URL:
        return
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS product_rules (
                id                SERIAL PRIMARY KEY,
                brand             TEXT NOT NULL DEFAULT '',
                category          TEXT NOT NULL DEFAULT '',
                exchange_rate     NUMERIC(10,4) NOT NULL DEFAULT 4.5,
                margin_rate       NUMERIC(5,4)  NOT NULL DEFAULT 0.4,
                ad_rate           NUMERIC(5,4)  NOT NULL DEFAULT 0.1,
                sea_shipping_rate NUMERIC(5,4)  NOT NULL DEFAULT 0.05,
                tw_shipping_cost  NUMERIC(10,2) NOT NULL DEFAULT 0,
                package_cost      NUMERIC(10,2) NOT NULL DEFAULT 0,
                round_rule        TEXT NOT NULL DEFAULT 'round_10',
                created_at        FLOAT DEFAULT 0,
                updated_at        FLOAT DEFAULT 0
            )
        """)
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        import sys; print(f"[PR Init] {e}", file=sys.stderr)

def _pr_all():
    if not DATABASE_URL:
        return []
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id,brand,category,exchange_rate,margin_rate,ad_rate,"
            "sea_shipping_rate,tw_shipping_cost,package_cost,round_rule,"
            "created_at,updated_at FROM product_rules ORDER BY id DESC"
        )
        rows = cur.fetchall(); cur.close(); conn.close()
        return [{"id":r[0],"brand":r[1],"category":r[2],
                 "exchange_rate":float(r[3]),"margin_rate":float(r[4]),
                 "ad_rate":float(r[5]),"sea_shipping_rate":float(r[6]),
                 "tw_shipping_cost":float(r[7]),"package_cost":float(r[8]),
                 "round_rule":r[9],"created_at":r[10],"updated_at":r[11]} for r in rows]
    except Exception as e:
        import sys; print(f"[PR All] {e}", file=sys.stderr)
        return []

def _pr_insert(data):
    if not DATABASE_URL:
        return None
    try:
        now = time.time()
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO product_rules"
            "(brand,category,exchange_rate,margin_rate,ad_rate,"
            "sea_shipping_rate,tw_shipping_cost,package_cost,round_rule,created_at,updated_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (data.get("brand",""), data.get("category",""),
             data.get("exchange_rate", 4.5), data.get("margin_rate", 0.4),
             data.get("ad_rate", 0.1), data.get("sea_shipping_rate", 0.05),
             data.get("tw_shipping_cost", 0), data.get("package_cost", 0),
             data.get("round_rule", "round_10"), now, now)
        )
        row_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return row_id
    except Exception as e:
        import sys; print(f"[PR Insert] {e}", file=sys.stderr)
        return None

def _pr_update(rule_id, data):
    if not DATABASE_URL:
        return False
    try:
        now = time.time()
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "UPDATE product_rules SET brand=%s,category=%s,exchange_rate=%s,"
            "margin_rate=%s,ad_rate=%s,sea_shipping_rate=%s,tw_shipping_cost=%s,"
            "package_cost=%s,round_rule=%s,updated_at=%s WHERE id=%s",
            (data.get("brand",""), data.get("category",""),
             data.get("exchange_rate", 4.5), data.get("margin_rate", 0.4),
             data.get("ad_rate", 0.1), data.get("sea_shipping_rate", 0.05),
             data.get("tw_shipping_cost", 0), data.get("package_cost", 0),
             data.get("round_rule", "round_10"), now, rule_id)
        )
        conn.commit(); cur.close(); conn.close()
        return True
    except Exception as e:
        import sys; print(f"[PR Update] {e}", file=sys.stderr)
        return False

def _pr_delete(rule_id):
    if not DATABASE_URL:
        return
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute("DELETE FROM product_rules WHERE id=%s", (rule_id,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        import sys; print(f"[PR Delete] {e}", file=sys.stderr)

_pr_init()


# ── Product Rules Admin HTML ──────────────────────────────────────

PRODUCT_RULES_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>商品定價規則中心</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
.header{background:#1a1a1a;color:#fff;padding:14px 20px;display:flex;align-items:center;gap:14px}
.header a{color:#888;text-decoration:none;font-size:14px}
.header a:hover{color:#fff}
.header-title{font-size:17px;font-weight:700;flex:1}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px}
.card{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.top-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.top-row h3{font-size:15px;font-weight:700}
.btn-new{background:#1a1a1a;color:#fff;border:none;border-radius:10px;padding:9px 20px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit}
.btn-new:hover{background:#333}
.btn-edit{background:#e3f2fd;color:#1565c0;border:none;border-radius:8px;padding:5px 12px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit}
.btn-edit:hover{background:#bbdefb}
.btn-del{background:#fce4ec;color:#c62828;border:none;border-radius:8px;padding:5px 12px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit}
.btn-del:hover{background:#f8bbd9}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:9px 10px;background:#f8f8f8;font-weight:700;color:#666;border-bottom:2px solid #eee;white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid #f0f0f0;vertical-align:middle}
tr:hover td{background:#fafafa}
.rb{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700}
.rb-round_10{background:#e8f5e9;color:#2e7d32}
.rb-round_50{background:#e3f2fd;color:#1565c0}
.rb-round_100{background:#fce4ec;color:#c62828}
.rb-charm_9{background:#fff3e0;color:#e65100}
.empty{text-align:center;padding:50px;color:#bbb;font-size:14px}
.hint-card{background:#fffbf0;border:1px solid #ffe082;border-radius:12px;padding:14px 18px;margin-bottom:16px;font-size:12px;color:#795548;line-height:2}
/* Modal */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:999;align-items:center;justify-content:center}
.overlay.open{display:flex}
.modal{background:#fff;border-radius:16px;padding:24px;width:520px;max-width:95vw;max-height:90vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.18)}
.modal h3{font-size:15px;font-weight:700;margin-bottom:18px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.full{grid-column:1/-1}
.frow{display:flex;flex-direction:column;gap:4px}
.frow label{font-size:11px;font-weight:700;color:#555}
.frow input,.frow select{border:1.5px solid #ddd;border-radius:8px;padding:8px 10px;font-size:13px;outline:none;font-family:inherit;background:#fff}
.frow input:focus,.frow select:focus{border-color:#1a1a1a}
.fhint{font-size:10px;color:#bbb;margin-top:1px}
.modal-footer{display:flex;gap:8px;justify-content:flex-end;margin-top:18px;padding-top:14px;border-top:1px solid #eee}
.btn-save{background:#1a1a1a;color:#fff;border:none;border-radius:8px;padding:9px 22px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit}
.btn-cancel{background:#f0f0f0;color:#555;border:none;border-radius:8px;padding:9px 18px;font-size:13px;cursor:pointer;font-family:inherit}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#333;color:#fff;padding:9px 20px;border-radius:20px;font-size:13px;z-index:9999;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}
</style>
</head>
<body>

<div class="header">
  <a href="/admin/products?key={{ key }}">&#8592; 商品搬運</a>
  <div class="header-title">商品定價規則中心</div>
  <button class="btn-new" onclick="openModal()">&#xff0b; 新增品牌規則</button>
</div>

<div class="wrap">

  <div class="hint-card">
    <strong>進位規則：</strong>
    <span class="rb rb-round_10">round_10</span> 進到10位（179&#8594;190）&nbsp;&nbsp;
    <span class="rb rb-round_50">round_50</span> 進到50（179&#8594;200）&nbsp;&nbsp;
    <span class="rb rb-round_100">round_100</span> 進到百位（179&#8594;200）&nbsp;&nbsp;
    <span class="rb rb-charm_9">charm_9</span> 尾數9（178&#8594;179）
  </div>

  <div class="card">
    <div class="top-row">
      <h3>品牌定價規則</h3>
      <span id="ruleCount" style="font-size:12px;color:#aaa"></span>
    </div>
    <div id="tableWrap"><div class="empty">載入中...</div></div>
  </div>

</div>

<!-- Modal -->
<div class="overlay" id="overlay">
  <div class="modal">
    <h3 id="modalTitle">新增品牌規則</h3>
    <input type="hidden" id="editId" value="">
    <div class="form-grid">
      <div class="frow full">
        <label>品牌名稱 *</label>
        <input type="text" id="fBrand" placeholder="例：JSIMPLE、朗德燈具">
      </div>
      <div class="frow full">
        <label>分類</label>
        <input type="text" id="fCategory" placeholder="例：高架床、燈具">
      </div>
      <div class="frow">
        <label>匯率（RMB &#8594; TWD）</label>
        <input type="number" id="fExchange" step="0.01" min="0" value="4.5">
        <span class="fhint">1 RMB = ? TWD，例：4.5</span>
      </div>
      <div class="frow">
        <label>毛利率 (%)</label>
        <input type="number" id="fMargin" step="0.1" min="0" max="100" value="40">
        <span class="fhint">例：40 代表毛利 40%</span>
      </div>
      <div class="frow">
        <label>廣告成本率 (%)</label>
        <input type="number" id="fAd" step="0.1" min="0" max="100" value="10">
        <span class="fhint">例：10 代表廣告佔售價 10%</span>
      </div>
      <div class="frow">
        <label>海運成本率 (%)</label>
        <input type="number" id="fSea" step="0.1" min="0" max="100" value="5">
        <span class="fhint">例：5 代表海運佔成本 5%</span>
      </div>
      <div class="frow">
        <label>台灣物流成本（TWD/件）</label>
        <input type="number" id="fTwShipping" step="1" min="0" value="80">
        <span class="fhint">固定金額，例：80 元</span>
      </div>
      <div class="frow">
        <label>包材成本（TWD/件）</label>
        <input type="number" id="fPackage" step="1" min="0" value="30">
        <span class="fhint">固定金額，例：30 元</span>
      </div>
      <div class="frow full">
        <label>進位規則</label>
        <select id="fRound">
          <option value="round_10">round_10 — 進到個位數10（179 &#8594; 190）</option>
          <option value="round_50">round_50 — 進到50（179 &#8594; 200）</option>
          <option value="round_100">round_100 — 進到百位（179 &#8594; 200）</option>
          <option value="charm_9">charm_9 — 尾數9（178 &#8594; 179）</option>
        </select>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeModal()">取消</button>
      <button class="btn-save" onclick="saveRule()">儲存</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const KEY = '{{ key }}';
let rules = [];

async function api(path, opts){
  const sep = path.includes('?') ? '&' : '?';
  const r = await fetch(path + sep + 'key=' + KEY, opts);
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}

function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 2500);
}

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function pct(v){ const n = parseFloat(v)*100; return (Number.isInteger(n)?n:n.toFixed(1))+'%'; }

const RBL = {round_10:'進10', round_50:'進50', round_100:'進100', charm_9:'尾數9'};

function renderTable(){
  document.getElementById('ruleCount').textContent = rules.length + ' 條規則';
  const wrap = document.getElementById('tableWrap');
  if(!rules.length){
    wrap.innerHTML = '<div class="empty">尚無規則。點右上角「新增品牌規則」開始</div>';
    return;
  }
  const rows = rules.map(r => `<tr>
    <td><strong>${esc(r.brand||'—')}</strong></td>
    <td>${esc(r.category||'—')}</td>
    <td>${parseFloat(r.exchange_rate).toFixed(2)}</td>
    <td>${pct(r.margin_rate)}</td>
    <td>${pct(r.ad_rate)}</td>
    <td>${pct(r.sea_shipping_rate)}</td>
    <td>$${Math.round(r.tw_shipping_cost)}</td>
    <td>$${Math.round(r.package_cost)}</td>
    <td><span class="rb rb-${esc(r.round_rule)}">${esc(RBL[r.round_rule]||r.round_rule)}</span></td>
    <td style="white-space:nowrap">
      <button class="btn-edit" onclick="openEdit(${r.id})">編輯</button>
      <button class="btn-del" onclick="delRule(${r.id})">刪除</button>
    </td>
  </tr>`).join('');
  wrap.innerHTML = `<table>
    <thead><tr>
      <th>品牌</th><th>分類</th><th>匯率</th><th>毛利率</th><th>廣告</th><th>海運</th><th>物流</th><th>包材</th><th>進位</th><th></th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function loadRules(){
  try{
    const d = await api('/api/product-rules');
    rules = d.rules || [];
    renderTable();
  }catch(e){
    document.getElementById('tableWrap').innerHTML = '<div class="empty">載入失敗：'+e.message+'</div>';
  }
}

function resetForm(){
  document.getElementById('editId').value = '';
  document.getElementById('fBrand').value = '';
  document.getElementById('fCategory').value = '';
  document.getElementById('fExchange').value = '4.5';
  document.getElementById('fMargin').value = '40';
  document.getElementById('fAd').value = '10';
  document.getElementById('fSea').value = '5';
  document.getElementById('fTwShipping').value = '80';
  document.getElementById('fPackage').value = '30';
  document.getElementById('fRound').value = 'round_10';
}

function openModal(){
  document.getElementById('modalTitle').textContent = '新增品牌規則';
  resetForm();
  document.getElementById('overlay').classList.add('open');
  document.getElementById('fBrand').focus();
}

function openEdit(id){
  const r = rules.find(x=>x.id===id);
  if(!r) return;
  document.getElementById('modalTitle').textContent = '編輯品牌規則';
  document.getElementById('editId').value = id;
  document.getElementById('fBrand').value = r.brand || '';
  document.getElementById('fCategory').value = r.category || '';
  document.getElementById('fExchange').value = parseFloat(r.exchange_rate).toFixed(2);
  document.getElementById('fMargin').value = (parseFloat(r.margin_rate)*100).toFixed(1).replace(/\\.0$/,'');
  document.getElementById('fAd').value = (parseFloat(r.ad_rate)*100).toFixed(1).replace(/\\.0$/,'');
  document.getElementById('fSea').value = (parseFloat(r.sea_shipping_rate)*100).toFixed(1).replace(/\\.0$/,'');
  document.getElementById('fTwShipping').value = Math.round(r.tw_shipping_cost);
  document.getElementById('fPackage').value = Math.round(r.package_cost);
  document.getElementById('fRound').value = r.round_rule || 'round_10';
  document.getElementById('overlay').classList.add('open');
  document.getElementById('fBrand').focus();
}

function closeModal(){
  document.getElementById('overlay').classList.remove('open');
}

function getFormData(){
  const brand = document.getElementById('fBrand').value.trim();
  if(!brand){ alert('請輸入品牌名稱'); return null; }
  return {
    brand,
    category:         document.getElementById('fCategory').value.trim(),
    exchange_rate:    parseFloat(document.getElementById('fExchange').value) || 4.5,
    margin_rate:      (parseFloat(document.getElementById('fMargin').value) || 40) / 100,
    ad_rate:          (parseFloat(document.getElementById('fAd').value) || 10) / 100,
    sea_shipping_rate:(parseFloat(document.getElementById('fSea').value) || 5) / 100,
    tw_shipping_cost: parseFloat(document.getElementById('fTwShipping').value) || 0,
    package_cost:     parseFloat(document.getElementById('fPackage').value) || 0,
    round_rule:       document.getElementById('fRound').value,
  };
}

async function saveRule(){
  const data = getFormData();
  if(!data) return;
  const editId = document.getElementById('editId').value;
  try{
    if(editId){
      await api('/api/product-rules/'+editId, {method:'PUT', body:JSON.stringify(data), headers:{'Content-Type':'application/json'}});
      toast('已更新');
    }else{
      await api('/api/product-rules', {method:'POST', body:JSON.stringify(data), headers:{'Content-Type':'application/json'}});
      toast('已新增');
    }
    closeModal();
    await loadRules();
  }catch(e){
    alert('儲存失敗：'+e.message);
  }
}

async function delRule(id){
  const r = rules.find(x=>x.id===id);
  if(!confirm('確定刪除「'+(r?r.brand:id)+'」的規則？')) return;
  try{
    await api('/api/product-rules/'+id, {method:'DELETE'});
    toast('已刪除');
    await loadRules();
  }catch(e){
    alert('刪除失敗：'+e.message);
  }
}

document.getElementById('overlay').addEventListener('click', function(e){
  if(e.target === this) closeModal();
});

loadRules();
</script>
</body>
</html>"""


# ── Product Rules Routes ──────────────────────────────────────────

@products_bp.route("/admin/product-rules")
def admin_product_rules():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next="/admin/product-rules", error="")
    return render_template_string(PRODUCT_RULES_HTML, key=key)

@products_bp.route("/api/product-rules", methods=["GET"])
def api_product_rules_list():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({"rules": _pr_all()})

@products_bp.route("/api/product-rules", methods=["POST"])
def api_product_rules_create():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    if not data.get("brand","").strip():
        return jsonify({"error": "brand 必填"}), 400
    row_id = _pr_insert(data)
    if row_id is None:
        return jsonify({"error": "資料庫未設定或寫入失敗"}), 500
    return jsonify({"ok": True, "id": row_id})

@products_bp.route("/api/product-rules/<int:rule_id>", methods=["PUT"])
def api_product_rules_update(rule_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    success = _pr_update(rule_id, data)
    return jsonify({"ok": success})

@products_bp.route("/api/product-rules/<int:rule_id>", methods=["DELETE"])
def api_product_rules_delete(rule_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    _pr_delete(rule_id)
    return jsonify({"ok": True})


# ── Store Import (Batch) ─────────────────────────────────────────

def _ss_migrate():
    """Add brand / category columns to store_scan_jobs."""
    if not DATABASE_URL:
        return
    try:
        conn = _pg_conn(); cur = conn.cursor()
        for sql in [
            "ALTER TABLE store_scan_jobs ADD COLUMN IF NOT EXISTS brand    TEXT DEFAULT ''",
            "ALTER TABLE store_scan_jobs ADD COLUMN IF NOT EXISTS category TEXT DEFAULT ''",
        ]:
            try: cur.execute(sql)
            except Exception: pass
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        import sys; print(f"[SS Migrate] {e}", file=sys.stderr)

_ss_migrate()


def _ss_create(url, platform, brand="", category=""):
    """Insert store_scan_jobs with brand and category; returns job_id."""
    if not DATABASE_URL:
        return None
    try:
        with _db_lock:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute(
                "INSERT INTO store_scan_jobs "
                "(url,platform,brand,category,status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,'pending',%s,%s) RETURNING id",
                (url, platform, brand, category, time.time(), time.time())
            )
            job_id = cur.fetchone()[0]
            conn.commit(); cur.close(); conn.close()
            return job_id
    except Exception as e:
        import sys; print(f"[SS Create] {e}", file=sys.stderr)
        return None


def _parse_price_str(price_str):
    """Extract first number from price string. Returns float or None."""
    import re as _re2
    if not price_str:
        return None
    m = _re2.search(r'[\d.]+', str(price_str))
    if m:
        try: return float(m.group())
        except Exception: pass
    return None


def _ss_get_with_brand(job_id):
    """Extended _ss_get: includes brand/category and maps item fields."""
    if not DATABASE_URL:
        return None
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id,url,platform,status,item_count,error_msg,created_at,"
            "COALESCE(brand,''),COALESCE(category,'') "
            "FROM store_scan_jobs WHERE id=%s",
            (job_id,)
        )
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close(); return None
        result = {
            "id": row[0], "url": row[1], "platform": row[2],
            "status": row[3], "item_count": row[4],
            "error_msg": row[5], "created_at": row[6],
            "brand": row[7], "category": row[8],
        }
        cur.execute(
            "SELECT id,title,url,image,price,shop_name,platform,scraped_at,added_to_queue "
            "FROM store_scan_items WHERE scan_job_id=%s ORDER BY id ASC",
            (job_id,)
        )
        result["items"] = [
            {
                "id": r[0], "title": r[1],
                "url": r[2], "product_url": r[2],
                "image": r[3], "main_image": r[3],
                "price": r[4], "original_price": _parse_price_str(r[4]),
                "shop_name": r[5], "platform": r[6],
                "scraped_at": r[7], "added_to_queue": bool(r[8]),
            }
            for r in cur.fetchall()
        ]
        cur.close(); conn.close()
        return result
    except Exception as e:
        import sys; print(f"[SS GetBrand] {e}", file=sys.stderr)
        return None


def _pj_import(url, platform, brand="", raw_title="", raw_price="", raw_images=None):
    """Insert product_job pre-filled with scan data (status=pending)."""
    if not DATABASE_URL:
        return None
    try:
        now = time.time()
        imgs_json = json.dumps(raw_images or [], ensure_ascii=False)
        with _db_lock:
            conn = _pg_conn(); cur = conn.cursor()
            cur.execute(
                "INSERT INTO product_jobs "
                "(url,platform,brand,status,raw_title,raw_price,raw_images,created_at,updated_at) "
                "VALUES (%s,%s,%s,'pending',%s,%s,%s,%s,%s) RETURNING id",
                (url, platform, brand, raw_title, raw_price, imgs_json, now, now)
            )
            job_id = cur.fetchone()[0]
            conn.commit(); cur.close(); conn.close()
            return job_id
    except Exception as e:
        import sys; print(f"[PJ Import] {e}", file=sys.stderr)
        return None


# ── Store Import API Routes ───────────────────────────────────────

@products_bp.route("/api/store-scan/create", methods=["POST"])
def api_store_scan_create_v2():
    """建立店鋪掃描任務（含品牌/分類）。"""
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url 必填"}), 400
    platform = data.get("platform", "")
    if not platform:
        platform = "1688" if "1688.com" in url else "taobao" if "taobao.com" in url else "unknown"
    brand    = (data.get("brand") or "").strip()
    category = (data.get("category") or "").strip()
    job_id = _ss_create(url, platform, brand, category)
    if job_id is None:
        return jsonify({"error": "資料庫未設定或寫入失敗"}), 500
    return jsonify({"ok": True, "id": job_id})


@products_bp.route("/api/store-scan/<int:job_id>/items", methods=["GET"])
def api_store_scan_items_v2(job_id):
    """取得掃描商品清單（含 brand/category，item 欄位映射新格式）。"""
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    job = _ss_get_with_brand(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@products_bp.route("/api/store-scan/import-selected", methods=["POST"])
def api_store_scan_import_selected():
    """將勾選商品建立成 product_jobs（含預填 title/price/image）。"""
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    item_ids = data.get("item_ids", [])
    brand    = (data.get("brand") or "").strip()
    if not item_ids:
        return jsonify({"error": "請勾選商品"}), 400
    if not DATABASE_URL:
        return jsonify({"error": "資料庫未設定"}), 500
    try:
        conn = _pg_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT ssi.id, ssi.url, ssi.platform, ssi.title, ssi.price, ssi.image, "
            "COALESCE(ssj.brand,'') "
            "FROM store_scan_items ssi "
            "JOIN store_scan_jobs ssj ON ssj.id = ssi.scan_job_id "
            "WHERE ssi.id=ANY(%s) AND ssi.added_to_queue=FALSE",
            (item_ids,)
        )
        rows = cur.fetchall(); cur.close(); conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    added = []
    for r in rows:
        item_id, url, platform, title, price, image, job_brand = r
        eff_brand = brand or job_brand
        raw_images = [image] if image else []
        job_id = _pj_import(url, platform, eff_brand, title or "", price or "", raw_images)
        if job_id:
            added.append({"item_id": item_id, "job_id": job_id})
    if added:
        _ss_mark_added([a["item_id"] for a in added])
    return jsonify({"ok": True, "added": len(added), "jobs": added})


# ── Store Import Admin Page ───────────────────────────────────────

STORE_IMPORT_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>店鋪批次搬運</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333;height:100vh;display:flex;flex-direction:column;overflow:hidden}
.header{background:#1a1a1a;color:#fff;padding:13px 20px;display:flex;align-items:center;gap:14px;flex-shrink:0}
.header a{color:#888;text-decoration:none;font-size:14px}
.header a:hover{color:#fff}
.header-title{font-size:17px;font-weight:700;flex:1}
.layout{display:flex;flex:1;min-height:0}
.panel-l{width:300px;min-width:300px;background:#fff;border-right:1px solid #eee;display:flex;flex-direction:column;overflow:hidden}
.sec{padding:14px 16px;border-bottom:1px solid #f0f0f0;flex-shrink:0}
.sec h4{font-size:11px;font-weight:700;color:#888;margin-bottom:10px;text-transform:uppercase;letter-spacing:.5px}
.frow{margin-bottom:8px}
.frow label{display:block;font-size:11px;font-weight:700;color:#666;margin-bottom:3px}
.frow input,.frow select{width:100%;border:1.5px solid #e0e0e0;border-radius:8px;padding:7px 10px;font-size:13px;outline:none;font-family:inherit;background:#fff}
.frow input:focus,.frow select:focus{border-color:#1a1a1a}
.btn-scan{width:100%;background:#1a1a1a;color:#fff;border:none;border-radius:9px;padding:10px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;margin-top:2px}
.btn-scan:hover{background:#333}
.btn-scan:disabled{background:#bbb;cursor:default}
.sbar{margin-top:8px;min-height:16px;font-size:11px;color:#888}
.spinner{display:inline-block;width:9px;height:9px;border:2px solid #ddd;border-top-color:#888;border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle;margin-right:3px}
@keyframes spin{to{transform:rotate(360deg)}}
.task-scroller{flex:1;overflow-y:auto}
.task-item{padding:10px 16px;border-bottom:1px solid #f8f8f8;cursor:pointer;transition:background .1s}
.task-item:hover{background:#f8f8f8}
.task-item.active{background:#e8f4fd;border-left:3px solid #1a1a1a;padding-left:13px}
.ti-url{font-size:11px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:3px}
.ti-meta{display:flex;gap:5px;align-items:center;flex-wrap:wrap}
.pf{font-size:10px;font-weight:700;padding:1px 5px;border-radius:4px}
.pf-1688{background:#fff0f0;color:#c62828}
.pf-taobao{background:#fff4e5;color:#e65100}
.ts{font-size:10px;font-weight:700;padding:1px 6px;border-radius:10px}
.ts-pending,.ts-scanning{background:#fef3cd;color:#856404}
.ts-done{background:#d1fae5;color:#065f46}
.ts-error,.ts-failed{background:#fee2e2;color:#991b1b}
.panel-r{flex:1;display:flex;flex-direction:column;min-width:0}
.ph{padding:12px 18px;background:#fff;border-bottom:1px solid #eee;display:flex;align-items:center;gap:10px;flex-shrink:0}
.ph h3{font-size:14px;font-weight:700;flex:1}
.ph-meta{font-size:12px;color:#aaa}
.pb{flex:1;overflow-y:auto;padding:16px}
.empty-r{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:#ccc;font-size:13px;gap:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px}
.ic{background:#fff;border-radius:12px;border:2px solid transparent;overflow:hidden;cursor:pointer;transition:border-color .12s;position:relative;display:flex;flex-direction:column}
.ic:hover{border-color:#ddd}
.ic.sel{border-color:#1a1a1a}
.ic.done{opacity:.6}
.ic input[type=checkbox]{position:absolute;top:8px;left:8px;width:15px;height:15px;z-index:2;accent-color:#1a1a1a;cursor:pointer}
.ic img{width:100%;aspect-ratio:1;object-fit:cover;background:#f5f5f5}
.ic-body{padding:7px 9px 9px;flex:1;display:flex;flex-direction:column;gap:3px}
.ic-title{font-size:11px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ic-price{font-size:12px;font-weight:700;color:#c62828}
.ic-added{font-size:10px;font-weight:700;padding:1px 5px;border-radius:5px;background:#d1fae5;color:#065f46;align-self:flex-start}
.abar{background:#fff;border-top:1px solid #eee;padding:10px 18px;display:none;align-items:center;gap:10px;flex-shrink:0}
.abar.show{display:flex}
.sel-info{font-size:13px;color:#555;flex:1}
.sb-row{display:flex;gap:6px}
.sbtn{background:#f5f5f5;border:1.5px solid #e0e0e0;border-radius:8px;padding:6px 12px;font-size:12px;cursor:pointer;font-family:inherit}
.sbtn:hover{background:#eee}
.brand-pick{border:1.5px solid #e0e0e0;border-radius:8px;padding:7px 10px;font-size:12px;font-family:inherit;outline:none;background:#fff}
.btn-imp{background:#2e7d32;color:#fff;border:none;border-radius:9px;padding:9px 20px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;white-space:nowrap}
.btn-imp:hover{background:#1b5e20}
.btn-imp:disabled{background:#bbb;cursor:default}
.toast{position:fixed;bottom:60px;left:50%;transform:translateX(-50%);background:#222;color:#fff;padding:8px 18px;border-radius:18px;font-size:13px;z-index:9999;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}
</style>
</head>
<body>

<div class="header">
  <a href="/admin/products?key={{ key }}">&#8592; 商品搬運</a>
  <div class="header-title">店鋪批次搬運</div>
</div>

<div class="layout">

  <div class="panel-l">
    <div class="sec">
      <h4>新增掃描任務</h4>
      <div class="frow">
        <label>店鋪 / 分類頁網址</label>
        <input type="text" id="urlIn" placeholder="https://shop.1688.com/...">
      </div>
      <div class="frow">
        <label>平台</label>
        <select id="pfSel">
          <option value="1688">1688</option>
          <option value="taobao">淘寶</option>
        </select>
      </div>
      <div class="frow">
        <label>品牌</label>
        <select id="brandSel">
          <option value="">不指定</option>
          <option value="jsimple">JS家具</option>
          <option value="lander">朗德LIGHT+</option>
          <option value="filterbreath">濾呼吸</option>
          <option value="lander_curtain">澄光窗簾</option>
        </select>
      </div>
      <div class="frow">
        <label>分類</label>
        <input type="text" id="catIn" placeholder="例：高架床、窗簾">
      </div>
      <button class="btn-scan" id="scanBtn" onclick="startScan()">開始掃描</button>
      <div class="sbar" id="sbar"></div>
    </div>
    <div class="sec" style="padding-bottom:6px">
      <h4>掃描歷史</h4>
    </div>
    <div class="task-scroller" id="taskList">
      <div style="padding:14px 16px;font-size:12px;color:#ccc">載入中...</div>
    </div>
  </div>

  <div class="panel-r">
    <div class="ph" id="ph" style="display:none">
      <h3 id="phTitle">商品清單</h3>
      <span class="ph-meta" id="phMeta"></span>
    </div>
    <div class="pb" id="pb">
      <div class="empty-r">
        <div style="font-size:36px">&#128722;</div>
        <div>從左側選擇掃描任務，查看商品清單</div>
      </div>
    </div>
    <div class="abar" id="abar">
      <div class="sb-row">
        <button class="sbtn" onclick="selAll(true)">全選</button>
        <button class="sbtn" onclick="selAll(false)">取消</button>
      </div>
      <span class="sel-info" id="selInfo">已選 0 筆</span>
      <select class="brand-pick" id="impBrand">
        <option value="">不指定品牌</option>
        <option value="jsimple">JS家具</option>
        <option value="lander">朗德LIGHT+</option>
        <option value="filterbreath">濾呼吸</option>
        <option value="lander_curtain">澄光窗簾</option>
      </select>
      <button class="btn-imp" id="impBtn" onclick="doImport()">加入商品搬運</button>
    </div>
  </div>

</div>

<div class="toast" id="toast"></div>

<script>
const KEY = '{{ key }}';
let currentJobId = null;
let pollTimer = null;

async function api(path, opts){
  const sep = path.includes('?') ? '&' : '?';
  const r = await fetch(path + sep + 'key=' + KEY, opts);
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}
function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 2800);
}
function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function loadHistory(){
  try{
    const d = await api('/api/store-scan');
    const jobs = d.jobs||[];
    const tl = document.getElementById('taskList');
    if(!jobs.length){
      tl.innerHTML = '<div style="padding:14px 16px;font-size:11px;color:#ccc">尚無記錄</div>';
      return;
    }
    tl.innerHTML = jobs.slice(0,30).map(j=>`
      <div class="task-item${j.id===currentJobId?' active':''}" onclick="loadTask(${j.id})" data-jid="${j.id}">
        <div class="ti-url">${esc((j.url||'').replace(/https?:\\/\\//,'').slice(0,42))}</div>
        <div class="ti-meta">
          <span class="pf pf-${esc(j.platform)}">${esc(j.platform)}</span>
          <span class="ts ts-${esc(j.status)}">${esc(j.status)}</span>
          <span style="font-size:10px;color:#aaa">${j.item_count||0}筆</span>
        </div>
      </div>
    `).join('');
  }catch(e){}
}

async function loadTask(jobId){
  clearTimeout(pollTimer);
  currentJobId = jobId;
  document.querySelectorAll('.task-item').forEach(el=>el.classList.toggle('active', +el.dataset.jid===jobId));
  const ph = document.getElementById('ph');
  ph.style.display = 'flex';
  document.getElementById('phTitle').textContent = '商品清單';
  document.getElementById('phMeta').textContent = '載入中...';
  document.getElementById('pb').innerHTML = '<div style="text-align:center;padding:50px;color:#bbb;font-size:13px">載入中...</div>';
  document.getElementById('abar').classList.remove('show');
  pollTask(jobId);
}

async function pollTask(jobId){
  try{
    const job = await api('/api/store-scan/'+jobId+'/items');
    if(job.status==='pending'||job.status==='scanning'){
      document.getElementById('phMeta').textContent = '掃描中，請稍候...';
      document.getElementById('pb').innerHTML = '<div style="text-align:center;padding:50px;color:#aaa;font-size:13px"><span class="spinner"></span> 等待 local_worker 掃描中...</div>';
      pollTimer = setTimeout(()=>pollTask(jobId), 2500);
      return;
    }
    await loadHistory();
    renderItems(job);
  }catch(e){
    document.getElementById('phMeta').textContent = '載入失敗';
    pollTimer = setTimeout(()=>pollTask(jobId), 3500);
  }
}

function renderItems(job){
  const items = job.items||[];
  document.getElementById('phMeta').textContent = items.length + ' 筆';
  if(job.status==='error'||job.status==='failed'){
    document.getElementById('pb').innerHTML = '<div style="text-align:center;padding:40px;color:#c62828;font-size:13px">掃描失敗：'+esc(job.error_msg||'')+'</div>';
    document.getElementById('abar').classList.remove('show');
    return;
  }
  if(!items.length){
    document.getElementById('pb').innerHTML = '<div style="text-align:center;padding:50px;color:#bbb;font-size:13px">未抓到商品<br><br>請確認 local_worker 正在執行，<br>或此頁面格式已變更。</div>';
    document.getElementById('abar').classList.remove('show');
    return;
  }
  document.getElementById('pb').innerHTML = '<div class="grid">'+items.map(it=>`
    <label class="ic${it.added_to_queue?' done':''}" onclick="updCount()">
      <input type="checkbox" class="icb" value="${it.id}"${it.added_to_queue?' disabled checked':''}>
      <img src="${esc(it.image||it.main_image||'')}" onerror="this.style.background='#eee'" alt="">
      <div class="ic-body">
        <div class="ic-title">${esc(it.title)}</div>
        ${it.price?'<div class="ic-price">&#165;'+esc(it.price)+'</div>':''}
        ${it.added_to_queue?'<span class="ic-added">&#10003; 已加入</span>':''}
      </div>
    </label>
  `).join('')+'</div>';
  document.getElementById('abar').classList.add('show');
  if(job.brand) document.getElementById('impBrand').value = job.brand;
  updCount();
}

function updCount(){
  const n = document.querySelectorAll('.icb:not(:disabled):checked').length;
  document.getElementById('selInfo').textContent = '已選 ' + n + ' 筆';
}
function selAll(v){
  document.querySelectorAll('.icb:not(:disabled)').forEach(el=>{ el.checked=v; });
  document.querySelectorAll('.ic').forEach(el=>{ const cb=el.querySelector('.icb'); if(cb&&!cb.disabled) el.classList.toggle('sel',v); });
  updCount();
}

async function startScan(){
  const url = document.getElementById('urlIn').value.trim();
  if(!url){ alert('請輸入店鋪網址'); return; }
  const platform = document.getElementById('pfSel').value;
  const brand    = document.getElementById('brandSel').value;
  const category = document.getElementById('catIn').value.trim();
  document.getElementById('scanBtn').disabled = true;
  document.getElementById('sbar').innerHTML = '<span class="spinner"></span>建立掃描任務...';
  try{
    const r = await api('/api/store-scan/create',{
      method:'POST', body:JSON.stringify({url,platform,brand,category}),
      headers:{'Content-Type':'application/json'}
    });
    document.getElementById('sbar').textContent = '任務 #'+r.id+' 已建立，等待 worker...';
    await loadHistory();
    await loadTask(r.id);
  }catch(e){
    document.getElementById('sbar').textContent = '錯誤：'+e.message;
  }finally{
    document.getElementById('scanBtn').disabled = false;
  }
}

async function doImport(){
  const ids = [...document.querySelectorAll('.icb:not(:disabled):checked')].map(el=>parseInt(el.value));
  if(!ids.length){ alert('請勾選商品'); return; }
  const brand = document.getElementById('impBrand').value;
  document.getElementById('impBtn').disabled = true;
  try{
    const r = await api('/api/store-scan/import-selected',{
      method:'POST', body:JSON.stringify({item_ids:ids,brand}),
      headers:{'Content-Type':'application/json'}
    });
    toast('已加入 '+r.added+' 筆商品搬運任務');
    if(currentJobId) await loadTask(currentJobId);
  }catch(e){
    alert('加入失敗：'+e.message);
  }finally{
    document.getElementById('impBtn').disabled = false;
  }
}

loadHistory();
</script>
</body>
</html>"""


@products_bp.route("/admin/store-import")
def admin_store_import():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, next="/admin/store-import", error="")
    return render_template_string(STORE_IMPORT_HTML, key=key)

