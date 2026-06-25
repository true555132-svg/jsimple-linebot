"""
seo_admin.py — SEO 內容管理後台 Blueprint
完全獨立檔案，不更動 LINE Bot / FB Bot / 商品搬運的任何邏輯。
管理範圍：標題庫（待寫/已寫/已發布）→ 文章草稿（含Meta、AI摘要）→ 成效追蹤記錄
掛載方式（app.py 只需加這兩行，不動其他程式碼）：
    from seo_admin import seo_bp, init_seo_db
    app.register_blueprint(seo_bp)
    init_seo_db()
"""
import os, json, time, threading, urllib.request, re
from flask import Blueprint, request, jsonify, render_template_string, redirect, abort

DATABASE_URL      = os.getenv("DATABASE_URL", "")
ADMIN_PASSWORD    = os.getenv("ADMIN_PASSWORD", "jsimple2024")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

seo_bp = Blueprint("seo", __name__)
_db_lock = threading.Lock()

TITLE_STATUS  = ["待寫", "已寫", "已發布"]
ARTICLE_STATUS = ["draft", "published"]

# ── DB ───────────────────────────────────────────────────────────

def _pg_conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)

def init_seo_db():
    if not DATABASE_URL:
        return
    try:
        with _db_lock:
            conn = _pg_conn()
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_titles (
                    id          SERIAL PRIMARY KEY,
                    topic       TEXT DEFAULT '',
                    title       TEXT NOT NULL,
                    status      TEXT DEFAULT '待寫',
                    slug        TEXT DEFAULT '',
                    notes       TEXT DEFAULT '',
                    created_at  FLOAT DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_articles (
                    id               SERIAL PRIMARY KEY,
                    title_id         INTEGER DEFAULT NULL,
                    title            TEXT NOT NULL DEFAULT '',
                    slug             TEXT DEFAULT '',
                    meta_title       TEXT DEFAULT '',
                    meta_description TEXT DEFAULT '',
                    content          TEXT DEFAULT '',
                    ai_summary       TEXT DEFAULT '',
                    status           TEXT DEFAULT 'draft',
                    created_at       FLOAT DEFAULT 0,
                    updated_at       FLOAT DEFAULT 0,
                    published_at     FLOAT DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_tracking (
                    id               SERIAL PRIMARY KEY,
                    article_id       INTEGER NOT NULL,
                    record_date      TEXT DEFAULT '',
                    ranking          TEXT DEFAULT '',
                    clicks           INTEGER DEFAULT 0,
                    impressions      INTEGER DEFAULT 0,
                    ai_overview_cited BOOLEAN DEFAULT FALSE,
                    chatgpt_cited     BOOLEAN DEFAULT FALSE,
                    notes            TEXT DEFAULT '',
                    created_at       FLOAT DEFAULT 0
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_seo_tracking_article ON seo_tracking(article_id)")
            # migration：補欄位（已存在則忽略）
            for col_sql in [
                "ALTER TABLE seo_articles ADD COLUMN IF NOT EXISTS brand_key TEXT DEFAULT ''",
                "ALTER TABLE seo_articles ADD COLUMN IF NOT EXISTS category TEXT DEFAULT ''",
                "ALTER TABLE seo_tracking ADD COLUMN IF NOT EXISTS line_inquiries INTEGER DEFAULT 0",
                "ALTER TABLE seo_tracking ADD COLUMN IF NOT EXISTS orders INTEGER DEFAULT 0",
                "ALTER TABLE seo_tracking ADD COLUMN IF NOT EXISTS revenue NUMERIC DEFAULT 0",
                "ALTER TABLE seo_tracking ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'manual'",
            ]:
                try: cur.execute(col_sql)
                except Exception: pass
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_ai_suggestions (
                    id           SERIAL PRIMARY KEY,
                    content      TEXT DEFAULT '',
                    generated_at FLOAT DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_generate_jobs (
                    id           SERIAL PRIMARY KEY,
                    status       TEXT DEFAULT 'pending',
                    article_id   INTEGER DEFAULT NULL,
                    error_msg    TEXT DEFAULT '',
                    created_at   FLOAT DEFAULT 0,
                    updated_at   FLOAT DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_prompt_templates (
                    key          TEXT PRIMARY KEY,
                    content      TEXT DEFAULT '',
                    updated_at   FLOAT DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_knowledge (
                    id           SERIAL PRIMARY KEY,
                    brand        TEXT DEFAULT '',
                    category     TEXT DEFAULT '',
                    type         TEXT DEFAULT 'spec',
                    title        TEXT NOT NULL DEFAULT '',
                    content      TEXT DEFAULT '',
                    tags         TEXT DEFAULT '',
                    allow_ai     BOOLEAN DEFAULT TRUE,
                    created_at   FLOAT DEFAULT 0,
                    updated_at   FLOAT DEFAULT 0
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_seo_knowledge_filter ON seo_knowledge(brand, category, type)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_knowledge_import_jobs (
                    id           SERIAL PRIMARY KEY,
                    status       TEXT DEFAULT 'pending',
                    result       TEXT DEFAULT '',
                    error_msg    TEXT DEFAULT '',
                    created_at   FLOAT DEFAULT 0,
                    updated_at   FLOAT DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_opportunities (
                    id               SERIAL PRIMARY KEY,
                    brand            TEXT DEFAULT '',
                    category         TEXT DEFAULT '',
                    topic            TEXT NOT NULL DEFAULT '',
                    search_intent    TEXT DEFAULT '',
                    target_customer  TEXT DEFAULT '',
                    seo_score        INTEGER DEFAULT 0,
                    geo_score        INTEGER DEFAULT 0,
                    conversion_score INTEGER DEFAULT 0,
                    difficulty       INTEGER DEFAULT 0,
                    reason           TEXT DEFAULT '',
                    status           TEXT DEFAULT 'idea',
                    created_at       FLOAT DEFAULT 0,
                    updated_at       FLOAT DEFAULT 0
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_seo_opportunities_filter ON seo_opportunities(brand, category, status)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_opportunity_jobs (
                    id             SERIAL PRIMARY KEY,
                    status         TEXT DEFAULT 'pending',
                    inserted_count INTEGER DEFAULT 0,
                    skipped_count  INTEGER DEFAULT 0,
                    error_msg      TEXT DEFAULT '',
                    created_at     FLOAT DEFAULT 0,
                    updated_at     FLOAT DEFAULT 0
                )
            """)
            conn.commit()
            cur.close()
            conn.close()
    except Exception as e:
        import sys; print(f"[SEO DB Init Error] {e}", file=sys.stderr)

def _q(sql, params=(), fetch=None):
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    result = None
    if fetch == "one":
        result = cur.fetchone()
    elif fetch == "all":
        result = cur.fetchall()
    elif fetch == "id":
        result = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return result

# ── 品牌資料（讀取既有 brand_profiles 表，與商品搬運中心共用同一份品牌設定）──

def _list_brands():
    if not DATABASE_URL:
        return []
    try:
        rows = _q("SELECT brand_key,name,category,style,tone FROM brand_profiles ORDER BY brand_key", fetch="all") or []
        return [{"key": r[0], "name": r[1], "category": r[2], "style": r[3], "tone": r[4]} for r in rows]
    except Exception:
        return []

def _get_brand(brand_key):
    if not DATABASE_URL or not brand_key:
        return {}
    try:
        row = _q("SELECT brand_key,name,category,style,tone,custom_prompt FROM brand_profiles WHERE brand_key=%s",
                 (brand_key,), fetch="one")
        if not row:
            return {}
        return {"key": row[0], "name": row[1], "category": row[2], "style": row[3], "tone": row[4], "custom_prompt": row[5]}
    except Exception:
        return {}

# ── 知識庫（讓AI生成文章時引用真實品牌資料，提升EEAT、避免虛構）──

KNOWLEDGE_TYPES = [
    ("spec", "商品規格"),
    ("faq", "FAQ"),
    ("case", "案例"),
    ("brand_feature", "品牌特色"),
]
KNOWLEDGE_TYPE_LABELS = dict(KNOWLEDGE_TYPES)

def _list_knowledge(brand="", category="", ktype=""):
    if not DATABASE_URL:
        return []
    try:
        where = []
        params = []
        if brand:
            where.append("brand=%s"); params.append(brand)
        if category:
            where.append("category=%s"); params.append(category)
        if ktype:
            where.append("type=%s"); params.append(ktype)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        rows = _q(f"""SELECT id,brand,category,type,title,content,tags,allow_ai,updated_at
                      FROM seo_knowledge{where_sql} ORDER BY updated_at DESC""", tuple(params), fetch="all") or []
        return [{
            "id": r[0], "brand": r[1], "category": r[2], "type": r[3], "title": r[4],
            "content": r[5], "tags": r[6], "allow_ai": r[7],
            "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[8])) if r[8] else "",
        } for r in rows]
    except Exception as e:
        import sys; print(f"[SEO Knowledge] 讀取清單失敗：{e}", file=sys.stderr)
        return []

def _get_knowledge_for_prompt(brand, category, limit=10):
    """生成文章前呼叫：依品牌+品類取最多limit筆 allow_ai=true 的知識庫資料"""
    if not DATABASE_URL:
        return []
    try:
        where = ["allow_ai = TRUE"]
        params = []
        if brand:
            where.append("brand=%s"); params.append(brand)
        if category:
            where.append("category=%s"); params.append(category)
        params.append(limit)
        rows = _q(f"""SELECT type,title,content FROM seo_knowledge
                      WHERE {' AND '.join(where)}
                      ORDER BY updated_at DESC LIMIT %s""", tuple(params), fetch="all") or []
        return [{"type": r[0], "title": r[1], "content": r[2]} for r in rows]
    except Exception as e:
        import sys; print(f"[SEO Knowledge] 讀取AI引用資料失敗：{e}", file=sys.stderr)
        return []

def _knowledge_block(items):
    """把知識庫資料轉成丟進Prompt的文字區塊"""
    if not items:
        return "（目前知識庫沒有符合此品牌/品類的資料，請用一般專業說明，不要編造具體數據、案例或認證）"
    lines = []
    for i, it in enumerate(items, 1):
        label = KNOWLEDGE_TYPE_LABELS.get(it["type"], it["type"])
        lines.append(f"{i}. [{label}] {it['title']}：{it['content']}")
    return "\n".join(lines)

def _knowledge_import_prompt(raw_text):
    return f"""你是品牌知識庫整理專家。以下是使用者貼上的原始資料（可能是商品介紹、FAQ、客服對話紀錄、案例內容等，格式可能很亂）：

━━━ 原始資料 ━━━
{raw_text[:12000]}
━━━ 原始資料結束 ━━━

請仔細閱讀，把裡面有價值的資訊拆成多筆「知識庫條目」，每筆歸類成以下4種類型之一：
- spec（商品規格）：具體規格數據，例如尺寸、承重、材質、保固、價格
- faq（FAQ）：常見問題與回答
- brand_feature（品牌特色）：品牌優勢、設計理念、服務特色
- case（案例）：實際客戶案例、使用情境、施工/安裝紀錄

規則：
1. 只整理原始資料裡「真實存在」的資訊，絕對不要新增、推論、誇大或虛構原文沒有的內容
2. 每筆條目要獨立完整，標題簡短（15字以內、具體），內容具體（30~150字）
3. 同一主題不要拆成太多筆瑣碎條目，但不相關的資訊也不要硬塞在一起
4. 如果原始資料裡沒有某種類型的內容，就不要硬生出那個類型，該類型可以完全沒有
5. 標籤（tags）用2~4個關鍵字，逗號分隔

輸出格式（只輸出JSON陣列，不要其他文字，不要markdown code block）：
[
  {{"type": "spec", "title": "標題", "content": "內容", "tags": "標籤1,標籤2"}}
]

如果原始資料完全沒有可用資訊，輸出空陣列 []"""

def _knowledge_upsert(brand, category, items):
    """依 brand+category+title（去頭尾空白、不分大小寫）比對，重複就更新、沒有就新增。回傳 (inserted, updated) 數量。"""
    inserted = 0
    updated = 0
    now = time.time()
    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        ktype = it.get("type") if it.get("type") in KNOWLEDGE_TYPE_LABELS else "spec"
        content = it.get("content") or ""
        tags = it.get("tags") or ""
        allow_ai = bool(it.get("allow_ai", True))
        existing = _q("""SELECT id FROM seo_knowledge
                          WHERE brand=%s AND category=%s AND LOWER(TRIM(title))=LOWER(TRIM(%s))""",
                       (brand, category, title), fetch="one")
        if existing:
            _q("""UPDATE seo_knowledge SET type=%s, title=%s, content=%s, tags=%s, allow_ai=%s, updated_at=%s
                  WHERE id=%s""", (ktype, title, content, tags, allow_ai, now, existing[0]))
            updated += 1
        else:
            _q("""INSERT INTO seo_knowledge (brand,category,type,title,content,tags,allow_ai,created_at,updated_at)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
               (brand, category, ktype, title, content, tags, allow_ai, now, now))
            inserted += 1
    return inserted, updated

# ── SEO Opportunity 主題池 ─────────────────────────────────────

OPPORTUNITY_STATUS = ["idea", "selected", "generated", "published"]
OPPORTUNITY_STATUS_LABELS = {"idea": "待評估", "selected": "已選定", "generated": "已生成", "published": "已發布"}

def _opportunity_prompt(brand, category, knowledge_items):
    return f"""你是台灣SEO/GEO/AEO內容策略專家。請根據以下品牌資訊，產生20個有價值的SEO文章主題。

品牌：{brand.get('name','')}（{brand.get('category','')}）
品牌風格：{brand.get('style','')}
品類：{category}

品牌知識庫參考（真實資料，主題應該盡量貼近這些內容，不要憑空想像不存在的功能或案例）：
{_knowledge_block(knowledge_items)}

請產生20個SEO文章主題，要求：
1. 是真實使用者會搜尋的問題，不要空泛的標題
2. 涵蓋不同類型：價格型、比較型、商業型、資訊型都要有，不要全部都一樣
3. 盡量能對應到上面知識庫的真實商品/案例/特色

每個主題請評估：
- seo_score（1~10）：搜尋量與排名機會
- geo_score（1~10）：適合被Google AI Overview / ChatGPT引用的程度
- conversion_score（1~10）：帶來詢價/成交的機會
- difficulty（1~10）：競爭難度，10代表最難
- reason：50字以內，說明為什麼推薦這個主題

輸出格式（只輸出JSON陣列，不要其他文字，不要markdown code block）：
[
  {{"topic": "主題", "search_intent": "搜尋意圖簡述", "target_customer": "目標客群",
    "seo_score": 8, "geo_score": 7, "conversion_score": 9, "difficulty": 4, "reason": "推薦原因"}}
]"""

def _opportunity_insert_batch(brand, category, items):
    """依 brand+category+topic（去頭尾空白、不分大小寫）比對，重複就跳過（不覆蓋既有狀態與分數）。回傳 (inserted, skipped)。"""
    inserted = 0
    skipped = 0
    now = time.time()
    for it in items:
        topic = (it.get("topic") or "").strip()
        if not topic:
            continue
        existing = _q("""SELECT id FROM seo_opportunities
                          WHERE brand=%s AND category=%s AND LOWER(TRIM(topic))=LOWER(TRIM(%s))""",
                       (brand, category, topic), fetch="one")
        if existing:
            skipped += 1
            continue
        _q("""INSERT INTO seo_opportunities
              (brand,category,topic,search_intent,target_customer,seo_score,geo_score,conversion_score,
               difficulty,reason,status,created_at,updated_at)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
           (brand, category, topic, it.get("search_intent", ""), it.get("target_customer", ""),
            int(it.get("seo_score", 0) or 0), int(it.get("geo_score", 0) or 0),
            int(it.get("conversion_score", 0) or 0), int(it.get("difficulty", 0) or 0),
            it.get("reason", ""), "idea", now, now))
        inserted += 1
    return inserted, skipped

def _list_opportunities(brand="", category="", status=""):
    if not DATABASE_URL:
        return []
    try:
        where = []
        params = []
        if brand:
            where.append("brand=%s"); params.append(brand)
        if category:
            where.append("category=%s"); params.append(category)
        if status:
            where.append("status=%s"); params.append(status)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        rows = _q(f"""SELECT id,brand,category,topic,search_intent,target_customer,
                      seo_score,geo_score,conversion_score,difficulty,reason,status,updated_at
                      FROM seo_opportunities{where_sql}
                      ORDER BY (seo_score+geo_score+conversion_score-difficulty) DESC, id DESC""",
                   tuple(params), fetch="all") or []
        return [{
            "id": r[0], "brand": r[1], "category": r[2], "topic": r[3], "search_intent": r[4],
            "target_customer": r[5], "seo_score": r[6], "geo_score": r[7], "conversion_score": r[8],
            "difficulty": r[9], "reason": r[10], "status": r[11],
            "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[12])) if r[12] else "",
        } for r in rows]
    except Exception as e:
        import sys; print(f"[SEO Opportunities] 讀取清單失敗：{e}", file=sys.stderr)
        return []

# ── Claude AI 呼叫 ──────────────────────────────────────────────

def _ai_call(prompt, model="claude-haiku-4-5-20251001", max_tokens=2000):
    if not ANTHROPIC_API_KEY:
        return None, "ANTHROPIC_API_KEY 未設定"
    try:
        req_data = json.dumps({
            "model": model,
            "max_tokens": max_tokens,
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
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode())
        text = resp["content"][0]["text"].strip()
        return text, ""
    except Exception as e:
        return None, str(e)

def _ai_call_json(prompt, model="claude-sonnet-4-6", max_tokens=8000):
    text, err = _ai_call(prompt, model=model, max_tokens=max_tokens)
    if err:
        return None, err
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        return None, f"AI 回傳格式錯誤：{text[:300]}"
    try:
        return json.loads(m.group()), ""
    except Exception as e:
        return None, f"JSON 解析失敗：{e}；原文：{text[:300]}"

def _ai_call_json_array(prompt, model="claude-sonnet-4-6", max_tokens=6000):
    text, err = _ai_call(prompt, model=model, max_tokens=max_tokens)
    if err:
        return None, err
    m = re.search(r'\[[\s\S]*\]', text)
    if not m:
        return None, f"AI 回傳格式錯誤：{text[:300]}"
    try:
        return json.loads(m.group()), ""
    except Exception as e:
        return None, f"JSON 解析失敗：{e}；原文：{text[:300]}"

# ── AI Prompt 模板（可在 /admin/seo-settings 頁面編輯，存DB後立即生效，不需重新部署）──
# 模板用 [[TOKEN]] 代表變數，避免跟JSON輸出格式裡的 { } 符號衝突（不是用 str.format）

DEFAULT_ANALYZE_PROMPT = """你是台灣SEO/GEO/AEO內容策略專家。

品牌：[[BRAND_NAME]]（[[BRAND_CATEGORY]]）
品牌風格：[[BRAND_STYLE]]
品類：[[CATEGORY]]
主題：[[TOPIC]]

請分析這個主題的搜尋意圖，輸出繁體中文、台灣用語，不要寫成英文翻譯腔：
1. 搜尋者是誰
2. 遇到什麼問題
3. 為什麼搜尋
4. 想得到什麼答案
5. 最後可能購買什麼產品

再列出「客群 × 場景 × 問題」矩陣，各至少5項。

直接輸出分析內容，不要加開頭結尾的客套話。"""

DEFAULT_GENERATE_PROMPT = """你是台灣SEO/GEO/AEO內容策略專家與文案編輯，為「[[BRAND_NAME]]」（[[BRAND_CATEGORY]]）撰寫一篇繁體中文SEO文章。

品牌風格：[[BRAND_STYLE]]
語氣要求：[[BRAND_TONE]]
品類：[[CATEGORY]]
主題：[[TOPIC]]

搜尋意圖分析參考：
[[ANALYSIS]]

品牌知識庫（真實資料，請優先引用）：
[[KNOWLEDGE]]

━━━ 知識庫引用規則（重要） ━━━
1. 優先引用上面「品牌知識庫」的內容（規格、FAQ、案例、品牌特色），不要憑空想像
2. 不得虛構案例、數據、認證——如果知識庫沒有相關資料，就用一般專業說明帶過，不要假裝有具體數據或案例
3. 如果知識庫顯示「沒有符合此品牌/品類的資料」，文章仍要寫完，只是不要編造具體數字或案例去填補
4. 文章最後（FAQ與CTA之間或CTA之後）新增一個小節，標題為「本篇引用知識庫」：如果有引用，列出引用了哪幾筆資料的標題；如果完全沒有可引用的資料，就寫「本篇未引用品牌知識庫資料，內容為一般專業說明」

━━━ 第一步：判斷文章類型 ━━━
依主題自動判斷，優先順序：價格型 > 比較型 > 商業型 > 資訊型
- 價格型（含「費用」「價格」「多少錢」）→ 報價表＋影響因素，語氣直接、數字導向
- 比較型（含「比較」「vs」「哪個好」）→ 對比表為核心，語氣中立有結論
- 商業型（含「服務」「推薦」「找哪家」）→ FAQ＋服務說明，解決問題導向
- 資訊型（含「是什麼」「怎麼做」）→ 定義段落＋步驟，教學口語風格
文章類型只影響語氣與GEO元素密度，不增減架構段落數。

━━━ 第二步：規劃架構並寫完整文章 ━━━
從搜尋意圖挑最值得寫、問題導向、適合Google AI Overview與ChatGPT引用的標題。
主關鍵字必須出現在：H1標題、文章開頭第一段、至少1個H2小標。

字數目標公式：(H2數量 + H3數量) × 200字 ± 25%
範例：4個H2 + 4個H3 = 目標約1600字；6個H2 + 6個H3 = 目標約2400字
不要為了湊字數填廢話，寧可精簡也不要膨脹。

━━━ GEO結構元素（每篇必要） ━━━
1. 至少1個比較表或數據表（Markdown table格式，AI可直接引用）
2. 至少2個定義段落，格式：> **詞彙**：解釋其實際意義與用途
3. 至少2個條列清單（步驟、重點、注意事項，每點一個概念）
4. 3~5個FAQ問答，用###H3寫問題，直接回答不繞彎，每題80~120字
5. 倒金字塔結構：每個H2開頭先給結論，再展開說明

━━━ EEAT佔位符規則 ━━━
僅在真的缺乏具體資料時使用，每1000字最多1個，一般性陳述不需要。
三種格式，依情境選一：
【待補充：實際數據——例：價格區間、市場行情】
【待補充：第一手觀點——例：專業建議、施工經驗】
【待補充：法規資訊——例：適用法規、官方來源】

━━━ 語言與品質規範 ━━━
- 台灣用語：「軟體」非「軟件」、「影片」非「視頻」、「品質」非「質量」
- 用具體數字代替模糊描述（「NT$3萬起」而非「價格不便宜」）
- 不寫「保證」「最好」「絕對」「100%」，不虛構數據或案例
- 一段2~4句，一段一個概念，不堆砌形容詞
- 結尾CTA至少80字，明確說明詢價或聯絡方式

段落順序：開頭直接回答問題 → 原因或背景 → 實務建議（規格/挑選/比較視主題而定）→ 商品或服務說明 → FAQ（3~5題）→ 詢價CTA

輸出格式（只輸出JSON，不要其他文字，不要markdown code block）：
{
  "title": "標題（主關鍵字在前）",
  "slug": "/blog/xxx-xxx-xxx（英文小寫，連字號）",
  "meta_title": "Meta Title（含品牌名，60字以內）",
  "meta_description": "Meta Description（120字以內，含關鍵字與品牌名）",
  "ai_summary": "AI Overview摘要，100~200字，純文字，包含1~2個關鍵數字或結論",
  "content": "完整文章內容，Markdown格式，##標示H2、###標示H3，包含表格、定義段落、條列清單、FAQ"
}"""

def _get_prompt_template(key, default):
    if not DATABASE_URL:
        return default
    try:
        row = _q("SELECT content FROM seo_prompt_templates WHERE key=%s", (key,), fetch="one")
        return row[0] if row and row[0].strip() else default
    except Exception:
        return default

def _save_prompt_template(key, content):
    now = time.time()
    _q("""INSERT INTO seo_prompt_templates (key,content,updated_at) VALUES (%s,%s,%s)
          ON CONFLICT (key) DO UPDATE SET content=EXCLUDED.content, updated_at=EXCLUDED.updated_at""",
       (key, content, now))

def _fill_tokens(template, **tokens):
    out = template
    for k, v in tokens.items():
        out = out.replace(f"[[{k}]]", v or "")
    return out

def _analyze_intent_prompt(brand, category, topic):
    tmpl = _get_prompt_template("analyze", DEFAULT_ANALYZE_PROMPT)
    return _fill_tokens(tmpl,
        BRAND_NAME=brand.get('name', ''), BRAND_CATEGORY=brand.get('category', ''),
        BRAND_STYLE=brand.get('style', ''), CATEGORY=category, TOPIC=topic)

def _generate_article_prompt(brand, category, topic, intent_analysis, knowledge_items=None):
    tmpl = _get_prompt_template("generate", DEFAULT_GENERATE_PROMPT)
    return _fill_tokens(tmpl,
        BRAND_NAME=brand.get('name', ''), BRAND_CATEGORY=brand.get('category', ''),
        BRAND_STYLE=brand.get('style', ''), BRAND_TONE=brand.get('tone', ''),
        CATEGORY=category, TOPIC=topic, ANALYSIS=intent_analysis,
        KNOWLEDGE=_knowledge_block(knowledge_items or []))

# ── Auth（複製自 app.py，避免 circular import，與既有後台共用同一支密碼）──

def check_auth():
    key = request.args.get("key", "")
    return key == ADMIN_PASSWORD, key

def auth_required():
    candidates = [request.args.get("admin_key", ""), request.args.get("key", "")]
    try:
        body = request.get_json(silent=True, force=True) or {}
        candidates += [body.get("admin_key", ""), body.get("key", "")]
    except Exception:
        pass
    for k in candidates:
        if k and k == ADMIN_PASSWORD:
            return True, k
    return False, ""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEO 內容管理後台</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5}
.wrap{max-width:340px;margin:80px auto;background:#fff;border-radius:16px;padding:36px;box-shadow:0 2px 16px rgba(0,0,0,.1);text-align:center}
h2{margin-bottom:22px;font-size:19px;color:#333}
input{width:100%;padding:11px;border:1px solid #ddd;border-radius:8px;font-size:15px;margin-bottom:14px}
button{width:100%;background:#0d6efd;color:#fff;border:none;border-radius:8px;padding:12px;font-size:15px;font-weight:600;cursor:pointer}
.err{color:red;margin-top:10px;font-size:13px}
</style></head><body>
<div class="wrap">
  <h2>🔐 SEO 內容管理後台</h2>
  <form method="GET" action="/admin/seo">
    <input type="password" name="key" placeholder="請輸入密碼" autofocus>
    <button type="submit">登入</button>
  </form>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
</div>
</body></html>"""

# ── 共用版面外殼（左側Sidebar＋頂部Breadcrumb）──────────────────

SIDEBAR_CSS = """
.app-shell{display:flex;min-height:100vh;align-items:stretch}
.sidebar{width:212px;flex-shrink:0;background:#1a1a1a;color:#fff;display:flex;flex-direction:column;padding:18px 0}
.sidebar-brand{display:flex;flex-direction:column;padding:0 20px 18px;margin-bottom:6px;border-bottom:1px solid rgba(255,255,255,.08)}
.sidebar-brand b{font-size:16px;font-weight:800}
.sidebar-brand span{font-size:11px;color:#999;font-weight:600;margin-top:2px}
.sidebar nav{display:flex;flex-direction:column;gap:2px;padding:10px}
.side-link{color:#ccc;text-decoration:none;font-size:13px;font-weight:600;padding:10px 14px;border-radius:8px;white-space:nowrap}
.side-link:hover{background:rgba(255,255,255,.08);color:#fff}
.side-link.active{background:#0d6efd;color:#fff}
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{background:#fff;padding:13px 24px;font-size:12px;color:#999;border-bottom:1px solid #e8eaed;flex-shrink:0}
.topbar a{color:#0d6efd;text-decoration:none}
.topbar a:hover{text-decoration:underline}
.topbar b{color:#333;font-weight:700}
.page-content{flex:1;min-width:0}
@media(max-width:760px){
  .app-shell{flex-direction:column}
  .sidebar{width:100%;flex-direction:row;align-items:center;padding:8px 6px;overflow-x:auto}
  .sidebar-brand{display:none}
  .sidebar nav{flex-direction:row;padding:0}
  .side-link{padding:8px 10px}
}
"""

SIDEBAR_ITEMS = [
    ("home",            "🏠 後台首頁",       "/admin"),
    ("seo-dashboard",   "📊 SEO 營運中心",   "/admin/seo-dashboard"),
    ("seo",             "📝 文章管理",       "/admin/seo"),
    ("seo-opportunities","🎯 主題機會池",    "/admin/seo-opportunities"),
    ("seo-generator",   "✨ AI 生成文章",    "/admin/seo-generator"),
    ("seo-knowledge",   "📚 知識庫管理",     "/admin/seo-knowledge"),
    ("seo-settings",    "⚙️ Prompt 設定",   "/admin/seo-settings"),
]

SHELL_CLOSE = "</div></div></div>"

def _shell_open(key, active, crumbs):
    """crumbs: list of (label, path_or_None)。最後一項視為當前頁面，不可點擊。
    回傳的字串需要搭配模板尾端的 SHELL_CLOSE 把 sidebar/main/page-content 三層div關起來。"""
    links = "".join(
        f'<a class="side-link{" active" if slug == active else ""}" href="{path}?key={key}">{label}</a>'
        for slug, label, path in SIDEBAR_ITEMS
    )
    parts = []
    for i, (label, path) in enumerate(crumbs):
        if path and i < len(crumbs) - 1:
            parts.append(f'<a href="{path}?key={key}">{label}</a>')
        else:
            parts.append(f'<b>{label}</b>')
    crumb_html = ' <span style="color:#ccc">›</span> '.join(parts)
    return (
        '<div class="app-shell">'
        '<aside class="sidebar">'
        '<div class="sidebar-brand"><b>JS</b><span>SEO 內容管理後台</span></div>'
        f'<nav>{links}</nav>'
        '</aside>'
        '<div class="main">'
        f'<div class="topbar">{crumb_html}</div>'
        '<div class="page-content">'
    )

# ── 列表頁 ─────────────────────────────────────────────────────

LIST_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEO 內容管理後台</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
""" + SIDEBAR_CSS + """
.container{max-width:1000px;margin:24px auto;padding:0 16px}
.section{background:#fff;border-radius:14px;padding:20px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.section h3{font-size:15px;margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #f0f0f0}
th{color:#888;font-weight:600;font-size:11px;text-transform:uppercase}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:700}
.b-待寫{background:#fdecea;color:#c62828}
.b-已寫{background:#fff8e1;color:#f57f17}
.b-已發布{background:#e8f5e9;color:#2e7d32}
.b-draft{background:#fff8e1;color:#f57f17}
.b-published{background:#e8f5e9;color:#2e7d32}
input[type=text],select,textarea{width:100%;border:1px solid #ddd;border-radius:6px;padding:7px 9px;font-size:13px;font-family:inherit}
.add-row{display:flex;gap:8px;margin-top:10px}
.add-row input{flex:1}
.btn{padding:7px 14px;background:#0d6efd;color:#fff;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap}
.btn-del{background:#dc3545}
.btn-sm{padding:4px 10px;font-size:11px}
.link{color:#0d6efd;text-decoration:none;font-weight:600}
form.inline{display:inline}
</style></head><body>
{{ shell|safe }}
<div class="container">

  <div class="section">
    <h3>標題庫（{{ titles|length }}）</h3>
    <table>
      <tr><th>主題</th><th>標題</th><th>狀態</th><th>Slug</th><th>操作</th></tr>
      {% for t in titles %}
      <tr>
        <td>{{ t[1] }}</td>
        <td>{{ t[2] }}</td>
        <td><span class="badge b-{{ t[3] }}">{{ t[3] }}</span></td>
        <td>{{ t[4] }}</td>
        <td>
          <form class="inline" method="POST" action="/admin/seo/title/{{ t[0] }}/status?key={{ key }}">
            <select name="status" onchange="this.form.submit()">
              {% for s in title_status %}
              <option value="{{ s }}" {{ 'selected' if s==t[3] else '' }}>{{ s }}</option>
              {% endfor %}
            </select>
          </form>
          <a class="link btn-sm" href="/admin/seo/article/new?key={{ key }}&title_id={{ t[0] }}">寫成文章</a>
          <form class="inline" method="POST" action="/admin/seo/title/{{ t[0] }}/delete?key={{ key }}" onsubmit="return confirm('刪除這個標題？')">
            <button class="btn btn-del btn-sm" type="submit">刪除</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
    <form class="add-row" method="POST" action="/admin/seo/title/add?key={{ key }}">
      <input type="text" name="topic" placeholder="主題（例如：高架床）" required>
      <input type="text" name="title" placeholder="標題" required>
      <button class="btn" type="submit">新增標題</button>
    </form>
  </div>

  <div class="section">
    <h3>文章（{{ articles|length }}）</h3>
    <table>
      <tr><th>標題</th><th>狀態</th><th>Slug</th><th>更新時間</th><th>操作</th></tr>
      {% for a in articles %}
      <tr>
        <td>{{ a[1] }}</td>
        <td><span class="badge b-{{ a[2] }}">{{ a[2] }}</span></td>
        <td>{{ a[3] }}</td>
        <td>{{ a[4] }}</td>
        <td>
          <a class="link btn-sm" href="/admin/seo/article/{{ a[0] }}?key={{ key }}">編輯</a>
          <a class="link btn-sm" href="/admin/seo/article/{{ a[0] }}/tracking?key={{ key }}">成效記錄</a>
          <form class="inline" method="POST" action="/admin/seo/article/{{ a[0] }}/delete?key={{ key }}" onsubmit="return confirm('刪除這篇文章？')">
            <button class="btn btn-del btn-sm" type="submit">刪除</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
    <a class="btn" style="display:inline-block;margin-top:10px;text-decoration:none" href="/admin/seo/article/new?key={{ key }}">+ 新增文章</a>
  </div>

</div>
""" + SHELL_CLOSE + """
</body></html>"""

ARTICLE_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>編輯文章</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
""" + SIDEBAR_CSS + """
.container{max-width:780px;margin:24px auto;padding:0 16px 80px}
.section{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
label{font-size:12px;color:#888;font-weight:700;display:block;margin-bottom:5px;margin-top:14px}
label:first-child{margin-top:0}
input[type=text],select,textarea{width:100%;border:1px solid #ddd;border-radius:8px;padding:9px 11px;font-size:14px;font-family:inherit}
textarea{resize:vertical;line-height:1.7}
.btn{padding:10px 22px;background:#0d6efd;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
</style></head><body>
{{ shell|safe }}
<div class="container">
<form method="POST" action="/admin/seo/article/save?key={{ key }}">
  <input type="hidden" name="id" value="{{ a[0] if a else '' }}">
  <div class="section">
    <label>標題</label>
    <input type="text" name="title" value="{{ a[1] if a else default_title }}" required>
    <label>URL Slug</label>
    <input type="text" name="slug" value="{{ a[2] if a else '' }}" placeholder="/blog/xxx">
    <label>狀態</label>
    <select name="status">
      {% for s in article_status %}
      <option value="{{ s }}" {{ 'selected' if a and a[7]==s else '' }}>{{ s }}</option>
      {% endfor %}
    </select>
  </div>
  <div class="section">
    <label>Meta Title</label>
    <input type="text" name="meta_title" value="{{ a[3] if a else '' }}">
    <label>Meta Description</label>
    <textarea name="meta_description" rows="2">{{ a[4] if a else '' }}</textarea>
    <label>AI Overview 摘要（100~200字）</label>
    <textarea name="ai_summary" rows="4">{{ a[6] if a else '' }}</textarea>
  </div>
  <div class="section">
    <label>文章內容</label>
    <textarea name="content" rows="24">{{ a[5] if a else '' }}</textarea>
  </div>
  <button class="btn" type="submit">儲存</button>
</form>
</div>
""" + SHELL_CLOSE + """
</body></html>"""

TRACKING_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>成效記錄</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
""" + SIDEBAR_CSS + """
.container{max-width:900px;margin:24px auto;padding:0 16px}
.section{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 6px;border-bottom:1px solid #f0f0f0}
th{color:#888;font-weight:600;font-size:11px;text-transform:uppercase}
.add-row{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap;align-items:center}
.add-row input{border:1px solid #ddd;border-radius:6px;padding:6px 8px;font-size:12px}
.btn{padding:7px 14px;background:#0d6efd;color:#fff;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer}
.yes{color:#2e7d32;font-weight:700}.no{color:#bbb}
</style></head><body>
{{ shell|safe }}
<div class="container">
  <div class="section">
    <table>
      <tr><th>日期</th><th>排名</th><th>點擊</th><th>曝光</th><th>詢價</th><th>成交</th><th>營收</th><th>AI Overview</th><th>ChatGPT</th><th>備註</th></tr>
      {% for r in records %}
      <tr>
        <td>{{ r[2] }}</td><td>{{ r[3] }}</td><td>{{ r[4] }}</td><td>{{ r[5] }}</td>
        <td>{{ r[9] }}</td><td>{{ r[10] }}</td><td>{{ r[11] }}</td>
        <td class="{{ 'yes' if r[6] else 'no' }}">{{ '✓' if r[6] else '—' }}</td>
        <td class="{{ 'yes' if r[7] else 'no' }}">{{ '✓' if r[7] else '—' }}</td>
        <td>{{ r[8] }}</td>
      </tr>
      {% endfor %}
    </table>
    <form class="add-row" method="POST" action="/admin/seo/article/{{ article_id }}/tracking/add?key={{ key }}">
      <input type="text" name="record_date" placeholder="YYYY-MM-DD" required style="width:110px">
      <input type="text" name="ranking" placeholder="排名" style="width:60px">
      <input type="text" name="clicks" placeholder="點擊" style="width:60px">
      <input type="text" name="impressions" placeholder="曝光" style="width:60px">
      <input type="text" name="line_inquiries" placeholder="LINE詢價數" style="width:80px">
      <input type="text" name="orders" placeholder="成交數" style="width:60px">
      <input type="text" name="revenue" placeholder="營收" style="width:80px">
      <label style="margin:0;font-size:12px"><input type="checkbox" name="ai_overview_cited" style="width:auto"> AI Overview</label>
      <label style="margin:0;font-size:12px"><input type="checkbox" name="chatgpt_cited" style="width:auto"> ChatGPT</label>
      <input type="text" name="notes" placeholder="備註" style="flex:1;min-width:120px">
      <button class="btn" type="submit">新增記錄</button>
    </form>
  </div>
</div>
""" + SHELL_CLOSE + """
</body></html>"""

OPPORTUNITIES_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEO 主題機會池</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
""" + SIDEBAR_CSS + """
.container{max-width:1200px;margin:24px auto;padding:0 16px 60px}
.section{background:#fff;border-radius:14px;padding:20px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.section h3{font-size:15px;margin-bottom:12px}
.gen-bar{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
.gen-bar select,.gen-bar input[type=text]{border:1px solid #ddd;border-radius:8px;padding:8px 10px;font-size:13px}
.gen-bar label{display:block;font-size:11px;color:#999;font-weight:700;margin-bottom:4px}
.btn{padding:9px 18px;background:#0d6efd;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.btn:disabled{background:#ccc;cursor:not-allowed}
.btn-sm{padding:5px 12px;font-size:11px}
.btn-outline{background:#fff;color:#0d6efd;border:1.5px solid #0d6efd}
.loading{font-size:13px;color:#888;margin-top:8px}
.err{color:#c62828;font-size:13px;margin-top:8px}
.banner{background:#fdecea;color:#c62828;border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:13px;font-weight:600}
.filter-bar{display:flex;gap:10px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.filter-bar select{border:1px solid #ddd;border-radius:8px;padding:7px 10px;font-size:13px;background:#fff}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:7px 5px;border-bottom:1px solid #f0f0f0;vertical-align:top}
th{color:#888;font-weight:600;font-size:10px;text-transform:uppercase}
td.topic-cell{max-width:220px;font-weight:700}
td.reason-cell{max-width:200px;color:#888;font-size:11px}
input.score{width:42px;text-align:center;border:1px solid #ddd;border-radius:6px;padding:3px;font-size:12px}
select.status-sel{font-size:11px;padding:4px 6px;border-radius:6px}
.b-idea{background:#fff8e1;color:#f57f17}
.b-selected{background:#e3f2fd;color:#1565c0}
.b-generated{background:#e8f5e9;color:#2e7d32}
.b-published{background:#f3e5f5;color:#7b1fa2}
form.inline{display:inline}
.row-actions{display:flex;gap:4px;flex-wrap:wrap}
</style></head><body>
{{ shell|safe }}
<div class="container">

  {% if not ai_key_set %}
  <div class="banner">⚠️ 尚未設定 ANTHROPIC_API_KEY，AI主題產生功能目前無法使用。請在 Render → Environment 加上這個環境變數後再試。</div>
  {% endif %}

  <div class="section">
    <h3>AI 產生主題池</h3>
    <div class="gen-bar">
      <div><label>品牌</label>
        <select id="gen-brand">
          <option value="">（不限品牌）</option>
          {% for b in brands %}<option value="{{ b.key }}">{{ b.name }}</option>{% endfor %}
        </select>
      </div>
      <div><label>品類</label>
        <input type="text" id="gen-category" placeholder="例如：高架床">
      </div>
      <button class="btn" id="btn-generate" onclick="doGenerate()" {{ 'disabled' if not ai_key_set else '' }}>🎯 AI產生20個主題</button>
    </div>
    <div class="loading" id="loading-generate" style="display:none">AI產生主題中，會讀取品牌資料與知識庫，可能需要1分鐘，請稍候...</div>
    <div class="err" id="err-generate"></div>
  </div>

  <div class="section">
    <form class="filter-bar" method="GET" action="/admin/seo-opportunities">
      <input type="hidden" name="key" value="{{ key }}">
      <select name="brand" onchange="this.form.submit()">
        <option value="">全部品牌</option>
        {% for b in brands %}<option value="{{ b.key }}" {{ 'selected' if b.key==brand else '' }}>{{ b.name }}</option>{% endfor %}
      </select>
      <select name="category" onchange="this.form.submit()">
        <option value="">全部品類</option>
        {% for c in categories %}<option value="{{ c }}" {{ 'selected' if c==category else '' }}>{{ c }}</option>{% endfor %}
      </select>
      <select name="status" onchange="this.form.submit()">
        <option value="">全部狀態</option>
        {% for sk in opportunity_status %}<option value="{{ sk }}" {{ 'selected' if sk==status else '' }}>{{ opportunity_status_labels[sk] }}</option>{% endfor %}
      </select>
    </form>

    <table>
      <tr>
        <th>主題</th><th>搜尋意圖</th><th>目標客群</th>
        <th>SEO</th><th>GEO</th><th>成交</th><th>難度</th>
        <th>推薦原因</th><th>狀態</th><th>操作</th>
      </tr>
      {% for o in items %}
      <tr>
        <td class="topic-cell">{{ o.topic }}<div style="font-size:10px;color:#aaa;font-weight:400">{{ o.brand }} / {{ o.category }}</div></td>
        <td>{{ o.search_intent }}</td>
        <td>{{ o.target_customer }}</td>
        <form class="inline score-form" method="POST" action="/admin/seo-opportunities/{{ o.id }}/update?key={{ key }}">
        <td><input class="score" type="number" name="seo_score" min="0" max="10" value="{{ o.seo_score }}"></td>
        <td><input class="score" type="number" name="geo_score" min="0" max="10" value="{{ o.geo_score }}"></td>
        <td><input class="score" type="number" name="conversion_score" min="0" max="10" value="{{ o.conversion_score }}"></td>
        <td><input class="score" type="number" name="difficulty" min="0" max="10" value="{{ o.difficulty }}"></td>
        <td class="reason-cell">{{ o.reason }}</td>
        <td>
          <select class="status-sel b-{{ o.status }}" name="status">
            {% for sk in opportunity_status %}<option value="{{ sk }}" {{ 'selected' if sk==o.status else '' }}>{{ opportunity_status_labels[sk] }}</option>{% endfor %}
          </select>
        </td>
        <td>
          <div class="row-actions">
            <button class="btn btn-sm" type="submit">💾 儲存</button>
        </form>
            <button class="btn btn-outline btn-sm" type="button"
               data-id="{{ o.id }}" data-brand="{{ o.brand }}" data-category="{{ o.category }}" data-topic="{{ o.topic }}"
               onclick="goGenerate(this)">用此主題生成</button>
            <form class="inline" method="POST" action="/admin/seo-opportunities/{{ o.id }}/delete?key={{ key }}" onsubmit="return confirm('刪除這個主題？')">
              <button class="btn btn-sm" style="background:#dc3545" type="submit">刪除</button>
            </form>
          </div>
        </td>
      </tr>
      {% endfor %}
    </table>
    {% if not items %}<p style="color:#999;font-size:13px;padding:14px 0">目前沒有符合篩選條件的主題，先用上面「AI產生主題池」產生一批。</p>{% endif %}
  </div>

</div>
<script>
const KEY = {{ key|tojson }};
function goGenerate(btn){
  const params = new URLSearchParams({
    key: KEY, opp_id: btn.dataset.id, brand: btn.dataset.brand,
    category: btn.dataset.category, topic: btn.dataset.topic,
  });
  window.location.href = '/admin/seo-generator?' + params.toString();
}
async function safeJson(res){
  const text = await res.text();
  try { return JSON.parse(text); }
  catch(e) { throw new Error('伺服器回應異常（可能是逾時或部署中），請稍後再試。HTTP ' + res.status); }
}
async function doGenerate(){
  const brand = document.getElementById('gen-brand').value;
  const category = document.getElementById('gen-category').value.trim();
  document.getElementById('btn-generate').disabled = true;
  document.getElementById('loading-generate').style.display = 'block';
  document.getElementById('err-generate').textContent = '';
  try {
    const res = await fetch('/admin/seo-opportunities/generate?key=' + encodeURIComponent(KEY), {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({brand, category})
    });
    const data = await safeJson(res);
    if (data.error) { document.getElementById('err-generate').textContent = data.error; document.getElementById('btn-generate').disabled = false; document.getElementById('loading-generate').style.display = 'none'; return; }
    await pollJob(data.job_id, brand, category);
  } catch(e) {
    document.getElementById('err-generate').textContent = String(e.message || e);
    document.getElementById('btn-generate').disabled = false;
    document.getElementById('loading-generate').style.display = 'none';
  }
}
async function pollJob(jobId, brand, category){
  while (true) {
    await new Promise(r => setTimeout(r, 3000));
    const res = await fetch('/admin/seo-opportunities/generate/status/' + jobId + '?key=' + encodeURIComponent(KEY));
    const data = await safeJson(res);
    if (data.status === 'pending' || data.status === 'running') continue;
    if (data.status === 'error') {
      document.getElementById('err-generate').textContent = data.error || '產生失敗';
      break;
    }
    if (data.status === 'done') {
      const url = new URL(window.location.href);
      url.searchParams.set('brand', brand);
      url.searchParams.set('category', category);
      window.location.href = url.toString();
      return;
    }
  }
  document.getElementById('btn-generate').disabled = false;
  document.getElementById('loading-generate').style.display = 'none';
}
</script>
""" + SHELL_CLOSE + """
</body></html>"""

KNOWLEDGE_LIST_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>知識庫管理</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
""" + SIDEBAR_CSS + """
.container{max-width:1100px;margin:24px auto;padding:0 16px}
.section{background:#fff;border-radius:14px;padding:20px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.section h3{font-size:15px;margin-bottom:12px}
.filter-bar{display:flex;gap:10px;align-items:center;margin-bottom:18px;flex-wrap:wrap}
.filter-bar select{border:1px solid #ddd;border-radius:8px;padding:7px 10px;font-size:13px;background:#fff}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #f0f0f0}
th{color:#888;font-weight:600;font-size:11px;text-transform:uppercase}
td.content-cell{max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#666}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:700;white-space:nowrap}
.b-spec{background:#e3f2fd;color:#1565c0}
.b-faq{background:#fff8e1;color:#f57f17}
.b-case{background:#e8f5e9;color:#2e7d32}
.b-brand_feature{background:#fce4ec;color:#ad1457}
.yes{color:#2e7d32;font-weight:700}.no{color:#bbb}
.btn{padding:8px 16px;background:#0d6efd;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
.btn-del{background:#dc3545;padding:4px 10px;font-size:11px}
.link{color:#0d6efd;text-decoration:none;font-weight:600;font-size:12px}
form.inline{display:inline}
</style></head><body>
{{ shell|safe }}
<div class="container">

  <div class="section">
    <form class="filter-bar" method="GET" action="/admin/seo-knowledge">
      <input type="hidden" name="key" value="{{ key }}">
      <select name="brand" onchange="this.form.submit()">
        <option value="">全部品牌</option>
        {% for b in brands %}<option value="{{ b.key }}" {{ 'selected' if b.key==brand else '' }}>{{ b.name }}</option>{% endfor %}
      </select>
      <select name="category" onchange="this.form.submit()">
        <option value="">全部品類</option>
        {% for c in categories %}<option value="{{ c }}" {{ 'selected' if c==category else '' }}>{{ c }}</option>{% endfor %}
      </select>
      <select name="type" onchange="this.form.submit()">
        <option value="">全部類型</option>
        {% for tk, tl in knowledge_types %}<option value="{{ tk }}" {{ 'selected' if tk==ktype else '' }}>{{ tl }}</option>{% endfor %}
      </select>
      <a class="btn" href="/admin/seo-knowledge/item/new?key={{ key }}">+ 新增資料</a>
      <a class="btn" style="background:#2e7d32" href="/admin/seo-knowledge/import?key={{ key }}">📥 批次匯入（貼上資料讓AI拆分）</a>
    </form>

    <table>
      <tr><th>品牌</th><th>品類</th><th>類型</th><th>標題</th><th>內容</th><th>標籤</th><th>AI可引用</th><th>更新時間</th><th>操作</th></tr>
      {% for it in items %}
      <tr>
        <td>{{ it.brand }}</td><td>{{ it.category }}</td>
        <td><span class="badge b-{{ it.type }}">{{ knowledge_type_labels.get(it.type, it.type) }}</span></td>
        <td>{{ it.title }}</td>
        <td class="content-cell">{{ it.content }}</td>
        <td>{{ it.tags }}</td>
        <td class="{{ 'yes' if it.allow_ai else 'no' }}">{{ '✓' if it.allow_ai else '—' }}</td>
        <td>{{ it.updated_at }}</td>
        <td>
          <a class="link" href="/admin/seo-knowledge/item/{{ it.id }}?key={{ key }}">編輯</a>
          <form class="inline" method="POST" action="/admin/seo-knowledge/item/{{ it.id }}/delete?key={{ key }}" onsubmit="return confirm('刪除這筆知識庫資料？')">
            <button class="btn btn-del" type="submit">刪除</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
    {% if not items %}<p style="color:#999;font-size:13px;padding:14px 0">目前沒有符合篩選條件的資料。</p>{% endif %}
  </div>

</div>
""" + SHELL_CLOSE + """
</body></html>"""

KNOWLEDGE_ITEM_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>知識庫資料</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
""" + SIDEBAR_CSS + """
.container{max-width:680px;margin:24px auto;padding:0 16px 80px}
.section{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
label{font-size:12px;color:#888;font-weight:700;display:block;margin-bottom:5px;margin-top:14px}
label:first-child{margin-top:0}
input[type=text],select,textarea{width:100%;border:1px solid #ddd;border-radius:8px;padding:9px 11px;font-size:14px;font-family:inherit}
textarea{resize:vertical;line-height:1.7}
.checkbox-row{display:flex;align-items:center;gap:8px;margin-top:14px}
.checkbox-row input{width:auto}
.checkbox-row label{margin:0}
.btn{padding:10px 22px;background:#0d6efd;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
</style></head><body>
{{ shell|safe }}
<div class="container">
<form method="POST" action="/admin/seo-knowledge/item/save?key={{ key }}">
  <input type="hidden" name="id" value="{{ it.id if it else '' }}">
  <div class="section">
    <label>品牌</label>
    <select name="brand">
      <option value="">（不限品牌）</option>
      {% for b in brands %}<option value="{{ b.key }}" {{ 'selected' if it and it.brand==b.key else '' }}>{{ b.name }}</option>{% endfor %}
    </select>
    <label>品類</label>
    <input type="text" name="category" value="{{ it.category if it else '' }}" placeholder="例如：高架床">
    <label>類型</label>
    <select name="type">
      {% for tk, tl in knowledge_types %}<option value="{{ tk }}" {{ 'selected' if it and it.type==tk else '' }}>{{ tl }}</option>{% endfor %}
    </select>
    <label>標題</label>
    <input type="text" name="title" value="{{ it.title if it else '' }}" required placeholder="例如：JS3026 高架床承重規格">
    <label>內容</label>
    <textarea name="content" rows="8" placeholder="實際資料，例如：承重120kg、床架厚度2mm冷軋鋼、保固3年...">{{ it.content if it else '' }}</textarea>
    <label>標籤</label>
    <input type="text" name="tags" value="{{ it.tags if it else '' }}" placeholder="逗號分隔，例如：承重,規格,鋼製">
    <div class="checkbox-row">
      <input type="checkbox" name="allow_ai" id="allow_ai" {{ 'checked' if (not it) or it.allow_ai else '' }}>
      <label for="allow_ai" style="margin:0">允許AI生成文章時引用這筆資料</label>
    </div>
  </div>
  <button class="btn" type="submit">儲存</button>
</form>
</div>
""" + SHELL_CLOSE + """
</body></html>"""

IMPORT_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>知識庫批次匯入</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
""" + SIDEBAR_CSS + """
.container{max-width:900px;margin:24px auto;padding:0 16px 80px}
.section{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.section h3{font-size:15px;margin-bottom:6px}
.hint{font-size:12px;color:#999;margin-bottom:12px;line-height:1.6}
label{font-size:12px;color:#888;font-weight:700;display:block;margin-bottom:5px;margin-top:14px}
label:first-child{margin-top:0}
input[type=text],select,textarea{width:100%;border:1px solid #ddd;border-radius:8px;padding:9px 11px;font-size:14px;font-family:inherit}
textarea{resize:vertical;line-height:1.6}
.btn{padding:10px 22px;background:#0d6efd;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.btn:disabled{background:#ccc;cursor:not-allowed}
.btn-outline{background:#fff;color:#666;border:1.5px solid #ddd}
.banner{background:#fdecea;color:#c62828;border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:13px;font-weight:600}
.loading{font-size:13px;color:#888;margin-top:8px}
.err{color:#c62828;font-size:13px;margin-top:8px}
.step{display:none}
.step.active{display:block}
.preview-row{border:1px solid #eee;border-radius:10px;padding:12px;margin-bottom:10px;background:#fafafa}
.preview-row .row-top{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.preview-row select{width:auto;font-size:12px;padding:5px 8px}
.preview-row input[type=text]{font-size:13px;font-weight:700}
.preview-row textarea{font-size:13px;min-height:60px}
.preview-row .tags-row{display:flex;gap:8px;margin-top:8px;align-items:center}
.preview-row .tags-row input{font-size:12px}
.skip-label{font-size:12px;color:#c62828;font-weight:600;white-space:nowrap;display:flex;align-items:center;gap:4px}
.result-box{font-size:14px;font-weight:700;color:#2e7d32;background:#e8f5e9;border-radius:10px;padding:14px;margin-top:10px}
</style></head><body>
{{ shell|safe }}
<div class="container">

  {% if not ai_key_set %}
  <div class="banner">⚠️ 尚未設定 ANTHROPIC_API_KEY，AI分析功能目前無法使用。請在 Render → Environment 加上這個環境變數後再試。</div>
  {% endif %}

  <div class="section" id="step-input">
    <h3>① 貼上原始資料</h3>
    <div class="hint">可以貼上商品介紹、FAQ、客服對話紀錄、案例內容等，AI會自動拆成「商品規格／FAQ／品牌特色／案例」4種類型的知識庫條目。只會整理原文真實存在的資訊，不會新增或誇大內容。</div>
    <label>品牌</label>
    <select id="brand">
      <option value="">（不限品牌）</option>
      {% for b in brands %}<option value="{{ b.key }}">{{ b.name }}</option>{% endfor %}
    </select>
    <label>品類</label>
    <input type="text" id="category" placeholder="例如：高架床">
    <label>原始資料</label>
    <textarea id="raw_text" rows="14" placeholder="貼上商品介紹、FAQ、客服對話、案例內容..."></textarea>
    <button class="btn" id="btn-analyze" onclick="doAnalyze()" {{ 'disabled' if not ai_key_set else '' }} style="margin-top:14px">AI 自動分析拆分</button>
    <div class="loading" id="loading-analyze" style="display:none">AI分析中，可能需要1分鐘，請稍候...</div>
    <div class="err" id="err-analyze"></div>
  </div>

  <div class="section step" id="step-preview">
    <h3>② 預覽並確認</h3>
    <div class="hint">可以修改每一筆內容、類型、標籤，或勾選「排除」不匯入這一筆。確認後按下方「確認匯入」會批次寫入知識庫——同品牌/品類下標題相同的資料會直接更新覆蓋，不會重複新增。</div>
    <div id="preview-list"></div>
    <button class="btn" id="btn-confirm" onclick="doConfirm()">確認匯入</button>
    <button class="btn btn-outline" onclick="location.reload()">重新開始</button>
    <div class="err" id="err-confirm"></div>
  </div>

  <div class="section step" id="step-done">
    <h3>✅ 匯入完成</h3>
    <div class="result-box" id="result-text"></div>
    <a class="btn" style="display:inline-block;margin-top:14px;text-decoration:none" href="/admin/seo-knowledge?key={{ key }}">前往知識庫列表查看</a>
  </div>

</div>
<script>
const KEY = {{ key|tojson }};
const TYPE_LABELS = {spec:"商品規格", faq:"FAQ", brand_feature:"品牌特色", case:"案例"};

async function safeJson(res){
  const text = await res.text();
  try { return JSON.parse(text); }
  catch(e) { throw new Error('伺服器回應異常（可能是逾時或部署中），請稍後再試。HTTP ' + res.status); }
}

async function doAnalyze(){
  const raw_text = document.getElementById('raw_text').value;
  if (!raw_text.trim()) { alert('請貼上要分析的內容'); return; }
  document.getElementById('btn-analyze').disabled = true;
  document.getElementById('loading-analyze').style.display = 'block';
  document.getElementById('err-analyze').textContent = '';
  try {
    const res = await fetch('/admin/seo-knowledge/import/analyze?key=' + encodeURIComponent(KEY), {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({raw_text})
    });
    const data = await safeJson(res);
    if (data.error) { document.getElementById('err-analyze').textContent = data.error; document.getElementById('btn-analyze').disabled = false; document.getElementById('loading-analyze').style.display = 'none'; return; }
    await pollImportJob(data.job_id);
  } catch(e) {
    document.getElementById('err-analyze').textContent = String(e.message || e);
    document.getElementById('btn-analyze').disabled = false;
    document.getElementById('loading-analyze').style.display = 'none';
  }
}

async function pollImportJob(jobId){
  while (true) {
    await new Promise(r => setTimeout(r, 3000));
    const res = await fetch('/admin/seo-knowledge/import/status/' + jobId + '?key=' + encodeURIComponent(KEY));
    const data = await safeJson(res);
    if (data.status === 'pending' || data.status === 'running') continue;
    if (data.status === 'error') {
      document.getElementById('err-analyze').textContent = data.error || '分析失敗';
      break;
    }
    if (data.status === 'done') {
      renderPreview(data.items || []);
      document.getElementById('step-input').style.display = 'none';
      document.getElementById('step-preview').classList.add('active');
      break;
    }
  }
  document.getElementById('btn-analyze').disabled = false;
  document.getElementById('loading-analyze').style.display = 'none';
}

function renderPreview(items){
  const list = document.getElementById('preview-list');
  if (!items.length) { list.innerHTML = '<p style="color:#999;font-size:13px">AI沒有從這段內容中找到可整理的資訊。</p>'; return; }
  list.innerHTML = items.map((it, i) => `
    <div class="preview-row" data-idx="${i}">
      <div class="row-top">
        <select class="f-type">
          ${Object.keys(TYPE_LABELS).map(k => `<option value="${k}" ${k===it.type?'selected':''}>${TYPE_LABELS[k]}</option>`).join('')}
        </select>
        <input type="text" class="f-title" value="${(it.title||'').replace(/"/g,'&quot;')}" style="flex:1">
        <label class="skip-label"><input type="checkbox" class="f-skip"> 排除</label>
      </div>
      <textarea class="f-content">${it.content||''}</textarea>
      <div class="tags-row">
        <span style="font-size:12px;color:#999">標籤</span>
        <input type="text" class="f-tags" value="${(it.tags||'').replace(/"/g,'&quot;')}">
      </div>
    </div>
  `).join('');
}

async function doConfirm(){
  const brand = document.getElementById('brand').value;
  const category = document.getElementById('category').value.trim();
  const rows = document.querySelectorAll('#preview-list .preview-row');
  const items = Array.from(rows).map(row => ({
    type: row.querySelector('.f-type').value,
    title: row.querySelector('.f-title').value,
    content: row.querySelector('.f-content').value,
    tags: row.querySelector('.f-tags').value,
    skip: row.querySelector('.f-skip').checked,
    allow_ai: true,
  }));
  document.getElementById('btn-confirm').disabled = true;
  document.getElementById('err-confirm').textContent = '';
  try {
    const res = await fetch('/admin/seo-knowledge/import/confirm?key=' + encodeURIComponent(KEY), {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({brand, category, items})
    });
    const data = await safeJson(res);
    if (data.error) { document.getElementById('err-confirm').textContent = data.error; document.getElementById('btn-confirm').disabled = false; return; }
    document.getElementById('result-text').textContent =
      `新增 ${data.inserted} 筆，更新 ${data.updated} 筆（同品牌/品類下標題相同的資料視為更新，不會重複）`;
    document.getElementById('step-preview').classList.remove('active');
    document.getElementById('step-done').classList.add('active');
  } catch(e) {
    document.getElementById('err-confirm').textContent = String(e.message || e);
    document.getElementById('btn-confirm').disabled = false;
  }
}
</script>
""" + SHELL_CLOSE + """
</body></html>"""

SETTINGS_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prompt 設定</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
""" + SIDEBAR_CSS + """
.container{max-width:860px;margin:24px auto;padding:0 16px 80px}
.section{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.section h3{font-size:15px;margin-bottom:6px}
.hint{font-size:12px;color:#999;margin-bottom:12px;line-height:1.6}
.hint code{background:#f0f0f0;padding:1px 5px;border-radius:4px;font-size:11px}
textarea{width:100%;border:1px solid #ddd;border-radius:8px;padding:10px;font-size:13px;font-family:inherit;line-height:1.6;resize:vertical}
.btn{padding:9px 18px;background:#0d6efd;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;margin-top:10px}
.btn-outline{background:#fff;color:#666;border:1.5px solid #ddd}
.btn-row{display:flex;gap:8px}
.saved-msg{font-size:12px;color:#2e7d32;font-weight:700;margin-left:10px;display:none}
</style></head><body>
{{ shell|safe }}
<div class="container">

  <div class="section">
    <h3>① 搜尋意圖分析 Prompt</h3>
    <div class="hint">可用變數：<code>[[BRAND_NAME]]</code> <code>[[BRAND_CATEGORY]]</code> <code>[[BRAND_STYLE]]</code> <code>[[CATEGORY]]</code> <code>[[TOPIC]]</code> — 存檔後立即生效，不需重新部署</div>
    <form method="POST" action="/admin/seo-settings/save?key={{ key }}" onsubmit="return true">
      <input type="hidden" name="prompt_key" value="analyze">
      <textarea name="content" rows="14">{{ analyze_prompt }}</textarea>
      <div class="btn-row">
        <button class="btn" type="submit">儲存</button>
      </div>
    </form>
    <form method="POST" action="/admin/seo-settings/reset?key={{ key }}" onsubmit="return confirm('還原成系統預設的分析Prompt？')" style="margin-top:6px">
      <input type="hidden" name="prompt_key" value="analyze">
      <button class="btn btn-outline" type="submit">還原預設值</button>
    </form>
  </div>

  <div class="section">
    <h3>② AI 生成文章 Prompt</h3>
    <div class="hint">可用變數：<code>[[BRAND_NAME]]</code> <code>[[BRAND_CATEGORY]]</code> <code>[[BRAND_STYLE]]</code> <code>[[BRAND_TONE]]</code> <code>[[CATEGORY]]</code> <code>[[TOPIC]]</code> <code>[[ANALYSIS]]</code>（搜尋意圖分析結果）<code>[[KNOWLEDGE]]</code>（<a href="/admin/seo-knowledge?key={{ key }}">知識庫</a>引用資料）— 結尾的JSON輸出格式請保留，否則文章會存不進去</div>
    <form method="POST" action="/admin/seo-settings/save?key={{ key }}">
      <input type="hidden" name="prompt_key" value="generate">
      <textarea name="content" rows="32">{{ generate_prompt }}</textarea>
      <div class="btn-row">
        <button class="btn" type="submit">儲存</button>
      </div>
    </form>
    <form method="POST" action="/admin/seo-settings/reset?key={{ key }}" onsubmit="return confirm('還原成系統預設的生成文章Prompt？')" style="margin-top:6px">
      <input type="hidden" name="prompt_key" value="generate">
      <button class="btn btn-outline" type="submit">還原預設值</button>
    </form>
  </div>

  {% if flash %}<div class="section" style="color:#2e7d32;font-weight:700">{{ flash }}</div>{% endif %}

</div>
""" + SHELL_CLOSE + """
</body></html>"""

GENERATOR_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 生成文章</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
""" + SIDEBAR_CSS + """
.container{max-width:780px;margin:24px auto;padding:0 16px 80px}
.section{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
label{font-size:12px;color:#888;font-weight:700;display:block;margin-bottom:5px;margin-top:14px}
label:first-child{margin-top:0}
input[type=text],select,textarea{width:100%;border:1px solid #ddd;border-radius:8px;padding:9px 11px;font-size:14px;font-family:inherit}
textarea{resize:vertical;line-height:1.7}
.btn{padding:10px 22px;background:#0d6efd;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.btn:disabled{background:#ccc;cursor:not-allowed}
.btn-outline{background:#fff;color:#0d6efd;border:1.5px solid #0d6efd}
.step{display:none}
.step.active{display:block}
.loading{font-size:13px;color:#888;margin-top:8px}
pre{white-space:pre-wrap;font-family:inherit;font-size:13px;line-height:1.7;background:#fafafa;border-radius:8px;padding:12px;max-height:400px;overflow-y:auto}
.err{color:#c62828;font-size:13px;margin-top:8px}
.banner{background:#fdecea;color:#c62828;border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:13px;font-weight:600}
</style></head><body>
{{ shell|safe }}
<div class="container">

  {% if not ai_key_set %}
  <div class="banner">⚠️ 尚未設定 ANTHROPIC_API_KEY，AI 分析／生成功能目前無法使用。請在 Render → Environment 加上這個環境變數後再試。</div>
  {% endif %}

  <div class="section">
    <input type="hidden" id="opp_id" value="{{ prefill_opp_id }}">
    <label>1. 選品牌</label>
    <select id="brand">
      {% for b in brands %}
      <option value="{{ b.key }}" data-category="{{ b.category }}" {{ 'selected' if b.key==prefill_brand else '' }}>{{ b.name }}</option>
      {% endfor %}
    </select>
    <label>2. 選品類</label>
    <input type="text" id="category" value="{{ prefill_category }}" placeholder="例如：高架床">
    <label>3. 輸入主題</label>
    <input type="text" id="topic" value="{{ prefill_topic }}" placeholder="例如：高架床房間最小要多大">
    <button class="btn" id="btn-analyze" onclick="doAnalyze()" {{ 'disabled' if not ai_key_set else '' }}>4. AI 分析搜尋意圖</button>
    <div class="loading" id="loading-analyze" style="display:none">分析中，請稍候...</div>
    <div class="err" id="err-analyze"></div>
  </div>

  <div class="section step" id="step-analysis">
    <label>搜尋意圖分析結果</label>
    <pre id="analysis-text"></pre>
    <button class="btn" id="btn-generate" onclick="doGenerate()" style="margin-top:14px">5. AI 生成文章</button>
    <div class="loading" id="loading-generate" style="display:none">生成文章中，可能需要1分鐘，請稍候...</div>
    <div class="err" id="err-generate"></div>
  </div>

  <div class="section step" id="step-done">
    <label>✅ 文章已生成並儲存</label>
    <div id="done-title" style="font-weight:700;margin-bottom:10px"></div>
    <a class="btn" id="link-edit" href="#">前往編輯 / 檢視文章</a>
  </div>

</div>
<script>
const KEY = {{ key|tojson }};
document.getElementById('brand').addEventListener('change', function(){
  document.getElementById('category').value = this.selectedOptions[0].dataset.category || '';
});
if (document.getElementById('brand').options.length && !document.getElementById('category').value) {
  document.getElementById('category').value = document.getElementById('brand').selectedOptions[0].dataset.category || '';
}

async function doAnalyze(){
  const brand = document.getElementById('brand').value;
  const category = document.getElementById('category').value.trim();
  const topic = document.getElementById('topic').value.trim();
  if (!topic) { alert('請輸入主題'); return; }
  document.getElementById('btn-analyze').disabled = true;
  document.getElementById('loading-analyze').style.display = 'block';
  document.getElementById('err-analyze').textContent = '';
  try {
    const res = await fetch('/admin/seo-generator/analyze?key=' + encodeURIComponent(KEY), {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({brand, category, topic})
    });
    const data = await safeJson(res);
    if (data.error) { document.getElementById('err-analyze').textContent = data.error; }
    else {
      document.getElementById('analysis-text').textContent = data.analysis;
      document.getElementById('step-analysis').classList.add('active');
      window._lastBrand = brand; window._lastCategory = category; window._lastTopic = topic;
      window._lastAnalysis = data.analysis;
    }
  } catch(e) { document.getElementById('err-analyze').textContent = String(e); }
  document.getElementById('btn-analyze').disabled = false;
  document.getElementById('loading-analyze').style.display = 'none';
}

async function safeJson(res){
  const text = await res.text();
  try { return JSON.parse(text); }
  catch(e) { throw new Error('伺服器回應異常（可能是逾時或部署中），請稍後再試。HTTP ' + res.status); }
}

async function doGenerate(){
  document.getElementById('btn-generate').disabled = true;
  document.getElementById('loading-generate').style.display = 'block';
  document.getElementById('err-generate').textContent = '';
  try {
    const res = await fetch('/admin/seo-generator/generate?key=' + encodeURIComponent(KEY), {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        brand: window._lastBrand, category: window._lastCategory,
        topic: window._lastTopic, analysis: window._lastAnalysis,
        opp_id: document.getElementById('opp_id').value
      })
    });
    const data = await safeJson(res);
    if (data.error) { document.getElementById('err-generate').textContent = data.error; document.getElementById('btn-generate').disabled = false; document.getElementById('loading-generate').style.display = 'none'; return; }
    await pollGenerateJob(data.job_id);
  } catch(e) {
    document.getElementById('err-generate').textContent = String(e.message || e);
    document.getElementById('btn-generate').disabled = false;
    document.getElementById('loading-generate').style.display = 'none';
  }
}

async function pollGenerateJob(jobId){
  while (true) {
    await new Promise(r => setTimeout(r, 3000));
    const res = await fetch('/admin/seo-generator/generate/status/' + jobId + '?key=' + encodeURIComponent(KEY));
    const data = await safeJson(res);
    if (data.status === 'pending' || data.status === 'running') continue;
    if (data.status === 'error') {
      document.getElementById('err-generate').textContent = data.error || '生成失敗';
      break;
    }
    if (data.status === 'done') {
      document.getElementById('done-title').textContent = data.title;
      document.getElementById('link-edit').href = '/admin/seo/article/' + data.article_id + '?key=' + encodeURIComponent(KEY);
      document.getElementById('step-done').classList.add('active');
      break;
    }
  }
  document.getElementById('btn-generate').disabled = false;
  document.getElementById('loading-generate').style.display = 'none';
}
</script>
""" + SHELL_CLOSE + """
</body></html>"""

# ── Routes ───────────────────────────────────────────────────────

@seo_bp.route("/admin/seo")
def seo_dashboard():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    titles = _q("SELECT id,topic,title,status,slug FROM seo_titles ORDER BY id DESC", fetch="all") or []
    articles = _q("SELECT id,title,status,slug,updated_at FROM seo_articles ORDER BY id DESC", fetch="all") or []
    articles = [(a[0], a[1], a[2], a[3], time.strftime("%Y-%m-%d %H:%M", time.localtime(a[4])) if a[4] else "") for a in articles]
    shell = _shell_open(key, "seo", [("文章管理", None)])
    return render_template_string(LIST_HTML, key=key, shell=shell, titles=titles, articles=articles, title_status=TITLE_STATUS)

@seo_bp.route("/admin/seo/title/add", methods=["POST"])
def seo_title_add():
    ok, key = check_auth()
    if not ok:
        abort(403)
    _q("INSERT INTO seo_titles (topic,title,status,created_at) VALUES (%s,%s,%s,%s)",
       (request.form.get("topic",""), request.form.get("title",""), "待寫", time.time()))
    return redirect(f"/admin/seo?key={key}")

@seo_bp.route("/admin/seo/title/<int:tid>/status", methods=["POST"])
def seo_title_status(tid):
    ok, key = check_auth()
    if not ok:
        abort(403)
    _q("UPDATE seo_titles SET status=%s WHERE id=%s", (request.form.get("status","待寫"), tid))
    return redirect(f"/admin/seo?key={key}")

@seo_bp.route("/admin/seo/title/<int:tid>/delete", methods=["POST"])
def seo_title_delete(tid):
    ok, key = check_auth()
    if not ok:
        abort(403)
    _q("DELETE FROM seo_titles WHERE id=%s", (tid,))
    return redirect(f"/admin/seo?key={key}")

@seo_bp.route("/admin/seo/article/new")
def seo_article_new():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    default_title = ""
    title_id = request.args.get("title_id", "")
    if title_id:
        row = _q("SELECT title FROM seo_titles WHERE id=%s", (title_id,), fetch="one")
        if row:
            default_title = row[0]
    shell = _shell_open(key, "seo", [("文章管理", "/admin/seo"), ("新增文章", None)])
    return render_template_string(ARTICLE_HTML, key=key, shell=shell, a=None, default_title=default_title, article_status=ARTICLE_STATUS)

@seo_bp.route("/admin/seo/article/<int:aid>")
def seo_article_edit(aid):
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    a = _q("""SELECT id,title,slug,meta_title,meta_description,content,ai_summary,status
              FROM seo_articles WHERE id=%s""", (aid,), fetch="one")
    if not a:
        abort(404)
    shell = _shell_open(key, "seo", [("文章管理", "/admin/seo"), ("編輯文章", None)])
    return render_template_string(ARTICLE_HTML, key=key, shell=shell, a=a, default_title="", article_status=ARTICLE_STATUS)

@seo_bp.route("/admin/seo/article/save", methods=["POST"])
def seo_article_save():
    ok, key = check_auth()
    if not ok:
        abort(403)
    f = request.form
    aid = f.get("id", "")
    now = time.time()
    if aid:
        published_at_sql = ", published_at=CASE WHEN status!='published' AND %s='published' THEN %s ELSE published_at END"
        _q(f"""UPDATE seo_articles SET title=%s, slug=%s, meta_title=%s, meta_description=%s,
               content=%s, ai_summary=%s, status=%s, updated_at=%s {published_at_sql}
               WHERE id=%s""",
           (f.get("title",""), f.get("slug",""), f.get("meta_title",""), f.get("meta_description",""),
            f.get("content",""), f.get("ai_summary",""), f.get("status","draft"), now,
            f.get("status","draft"), now, aid))
        new_id = aid
    else:
        new_id = _q("""INSERT INTO seo_articles
               (title,slug,meta_title,meta_description,content,ai_summary,status,created_at,updated_at,published_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
           (f.get("title",""), f.get("slug",""), f.get("meta_title",""), f.get("meta_description",""),
            f.get("content",""), f.get("ai_summary",""), f.get("status","draft"), now, now,
            now if f.get("status") == "published" else 0), fetch="id")
    return redirect(f"/admin/seo/article/{new_id}?key={key}")

@seo_bp.route("/admin/seo/article/<int:aid>/delete", methods=["POST"])
def seo_article_delete(aid):
    ok, key = check_auth()
    if not ok:
        abort(403)
    _q("DELETE FROM seo_tracking WHERE article_id=%s", (aid,))
    _q("DELETE FROM seo_articles WHERE id=%s", (aid,))
    return redirect(f"/admin/seo?key={key}")

@seo_bp.route("/admin/seo/article/<int:aid>/tracking")
def seo_tracking_view(aid):
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    art = _q("SELECT title FROM seo_articles WHERE id=%s", (aid,), fetch="one")
    if not art:
        abort(404)
    records = _q("""SELECT id,article_id,record_date,ranking,clicks,impressions,
                     ai_overview_cited,chatgpt_cited,notes,line_inquiries,orders,revenue
                     FROM seo_tracking WHERE article_id=%s ORDER BY record_date DESC""", (aid,), fetch="all") or []
    shell = _shell_open(key, "seo", [("文章管理", "/admin/seo"), (f"成效記錄 — {art[0]}", None)])
    return render_template_string(TRACKING_HTML, key=key, shell=shell, article_id=aid, article_title=art[0], records=records)

@seo_bp.route("/admin/seo/article/<int:aid>/tracking/add", methods=["POST"])
def seo_tracking_add(aid):
    ok, key = check_auth()
    if not ok:
        abort(403)
    f = request.form
    _q("""INSERT INTO seo_tracking
          (article_id,record_date,ranking,clicks,impressions,ai_overview_cited,chatgpt_cited,notes,
           line_inquiries,orders,revenue,source,created_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
       (aid, f.get("record_date",""), f.get("ranking",""),
        int(f.get("clicks") or 0), int(f.get("impressions") or 0),
        bool(f.get("ai_overview_cited")), bool(f.get("chatgpt_cited")),
        f.get("notes",""),
        int(f.get("line_inquiries") or 0), int(f.get("orders") or 0), float(f.get("revenue") or 0),
        "manual", time.time()))
    return redirect(f"/admin/seo/article/{aid}/tracking?key={key}")

# ── SEO Opportunity 主題機會池 ──────────────────────────────────

def _run_opportunity_job(job_id, brand_key, category):
    try:
        _q("UPDATE seo_opportunity_jobs SET status='running', updated_at=%s WHERE id=%s", (time.time(), job_id))
        brand = _get_brand(brand_key)
        knowledge_items = _get_knowledge_for_prompt(brand_key, category, limit=15)
        prompt = _opportunity_prompt(brand, category, knowledge_items)
        items, err = _ai_call_json_array(prompt, model="claude-sonnet-4-6", max_tokens=6000)
        if err:
            _q("UPDATE seo_opportunity_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s",
               (f"AI產生主題失敗：{err}", time.time(), job_id))
            return
        inserted, skipped = _opportunity_insert_batch(brand_key, category, items)
        _q("""UPDATE seo_opportunity_jobs SET status='done', inserted_count=%s, skipped_count=%s, updated_at=%s
              WHERE id=%s""", (inserted, skipped, time.time(), job_id))
    except Exception as e:
        import sys; print(f"[SEO Opportunity Job Error] {e}", file=sys.stderr)
        try:
            _q("UPDATE seo_opportunity_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s",
               (str(e), time.time(), job_id))
        except Exception:
            pass

@seo_bp.route("/admin/seo-opportunities")
def seo_opportunities_page():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    brand = request.args.get("brand", "")
    category = request.args.get("category", "")
    status = request.args.get("status", "")
    items = _list_opportunities(brand, category, status)
    try:
        brands = _list_brands()
        categories = _list_categories()
    except Exception:
        brands, categories = [], []
    shell = _shell_open(key, "seo-opportunities", [("主題機會池", None)])
    return render_template_string(OPPORTUNITIES_HTML, key=key, shell=shell, items=items,
        brand=brand, category=category, status=status, brands=brands, categories=categories,
        opportunity_status=OPPORTUNITY_STATUS, opportunity_status_labels=OPPORTUNITY_STATUS_LABELS,
        ai_key_set=bool(ANTHROPIC_API_KEY))

@seo_bp.route("/admin/seo-opportunities/generate", methods=["POST"])
def seo_opportunities_generate():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    brand_key = data.get("brand", "")
    category = data.get("category", "")
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "尚未設定 ANTHROPIC_API_KEY，請在 Render → Environment 加上這個環境變數才能使用AI功能"}), 200
    now = time.time()
    job_id = _q("INSERT INTO seo_opportunity_jobs (status,created_at,updated_at) VALUES (%s,%s,%s) RETURNING id",
                ("pending", now, now), fetch="id")
    threading.Thread(target=_run_opportunity_job, args=(job_id, brand_key, category), daemon=True).start()
    return jsonify({"job_id": job_id})

@seo_bp.route("/admin/seo-opportunities/generate/status/<int:job_id>")
def seo_opportunities_generate_status(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    row = _q("SELECT status,inserted_count,skipped_count,error_msg FROM seo_opportunity_jobs WHERE id=%s",
              (job_id,), fetch="one")
    if not row:
        return jsonify({"status": "error", "error": "找不到這個任務"})
    status, inserted, skipped, error_msg = row
    out = {"status": status}
    if status == "error":
        out["error"] = error_msg
    elif status == "done":
        out["inserted"] = inserted
        out["skipped"] = skipped
    return jsonify(out)

@seo_bp.route("/admin/seo-opportunities/<int:opp_id>/update", methods=["POST"])
def seo_opportunities_update(opp_id):
    ok, key = check_auth()
    if not ok:
        abort(403)
    f = request.form
    def _score(name):
        try:
            return max(0, min(10, int(f.get(name, 0) or 0)))
        except (TypeError, ValueError):
            return 0
    status = f.get("status", "idea")
    if status not in OPPORTUNITY_STATUS:
        status = "idea"
    _q("""UPDATE seo_opportunities SET seo_score=%s, geo_score=%s, conversion_score=%s, difficulty=%s,
          status=%s, updated_at=%s WHERE id=%s""",
       (_score("seo_score"), _score("geo_score"), _score("conversion_score"), _score("difficulty"),
        status, time.time(), opp_id))
    return redirect(f"/admin/seo-opportunities?key={key}")

@seo_bp.route("/admin/seo-opportunities/<int:opp_id>/delete", methods=["POST"])
def seo_opportunities_delete(opp_id):
    ok, key = check_auth()
    if not ok:
        abort(403)
    _q("DELETE FROM seo_opportunities WHERE id=%s", (opp_id,))
    return redirect(f"/admin/seo-opportunities?key={key}")

@seo_bp.route("/admin/seo-knowledge")
def seo_knowledge_page():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    brand = request.args.get("brand", "")
    category = request.args.get("category", "")
    ktype = request.args.get("type", "")
    items = _list_knowledge(brand, category, ktype)
    try:
        brands = _list_brands()
        categories = _list_categories()
    except Exception:
        brands, categories = [], []
    shell = _shell_open(key, "seo-knowledge", [("知識庫管理", None)])
    return render_template_string(KNOWLEDGE_LIST_HTML, key=key, shell=shell, items=items,
        brand=brand, category=category, ktype=ktype, brands=brands, categories=categories,
        knowledge_types=KNOWLEDGE_TYPES, knowledge_type_labels=KNOWLEDGE_TYPE_LABELS)

@seo_bp.route("/admin/seo-knowledge/item/new")
def seo_knowledge_item_new():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    brands = _list_brands()
    shell = _shell_open(key, "seo-knowledge", [("知識庫管理", "/admin/seo-knowledge"), ("新增資料", None)])
    return render_template_string(KNOWLEDGE_ITEM_HTML, key=key, shell=shell, it=None,
        brands=brands, knowledge_types=KNOWLEDGE_TYPES)

@seo_bp.route("/admin/seo-knowledge/item/<int:item_id>")
def seo_knowledge_item_edit(item_id):
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    row = _q("SELECT id,brand,category,type,title,content,tags,allow_ai FROM seo_knowledge WHERE id=%s",
              (item_id,), fetch="one")
    if not row:
        abort(404)
    it = {"id": row[0], "brand": row[1], "category": row[2], "type": row[3],
          "title": row[4], "content": row[5], "tags": row[6], "allow_ai": row[7]}
    brands = _list_brands()
    shell = _shell_open(key, "seo-knowledge", [("知識庫管理", "/admin/seo-knowledge"), ("編輯資料", None)])
    return render_template_string(KNOWLEDGE_ITEM_HTML, key=key, shell=shell, it=it,
        brands=brands, knowledge_types=KNOWLEDGE_TYPES)

@seo_bp.route("/admin/seo-knowledge/item/save", methods=["POST"])
def seo_knowledge_item_save():
    ok, key = check_auth()
    if not ok:
        abort(403)
    f = request.form
    item_id = f.get("id", "")
    now = time.time()
    if item_id:
        _q("""UPDATE seo_knowledge SET brand=%s, category=%s, type=%s, title=%s, content=%s,
              tags=%s, allow_ai=%s, updated_at=%s WHERE id=%s""",
           (f.get("brand", ""), f.get("category", ""), f.get("type", "spec"), f.get("title", ""),
            f.get("content", ""), f.get("tags", ""), bool(f.get("allow_ai")), now, item_id))
    else:
        _q("""INSERT INTO seo_knowledge (brand,category,type,title,content,tags,allow_ai,created_at,updated_at)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
           (f.get("brand", ""), f.get("category", ""), f.get("type", "spec"), f.get("title", ""),
            f.get("content", ""), f.get("tags", ""), bool(f.get("allow_ai")), now, now))
    return redirect(f"/admin/seo-knowledge?key={key}")

@seo_bp.route("/admin/seo-knowledge/item/<int:item_id>/delete", methods=["POST"])
def seo_knowledge_item_delete(item_id):
    ok, key = check_auth()
    if not ok:
        abort(403)
    _q("DELETE FROM seo_knowledge WHERE id=%s", (item_id,))
    return redirect(f"/admin/seo-knowledge?key={key}")

# ── Knowledge Import：貼上原始資料 → AI拆分 → 預覽確認 → 批次寫入 ──

def _run_knowledge_import_job(job_id, raw_text):
    try:
        _q("UPDATE seo_knowledge_import_jobs SET status='running', updated_at=%s WHERE id=%s", (time.time(), job_id))
        prompt = _knowledge_import_prompt(raw_text)
        items, err = _ai_call_json_array(prompt, model="claude-sonnet-4-6", max_tokens=6000)
        if err:
            _q("UPDATE seo_knowledge_import_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s",
               (f"AI分析失敗：{err}", time.time(), job_id))
            return
        _q("UPDATE seo_knowledge_import_jobs SET status='done', result=%s, updated_at=%s WHERE id=%s",
           (json.dumps(items, ensure_ascii=False), time.time(), job_id))
    except Exception as e:
        import sys; print(f"[SEO Knowledge Import Job Error] {e}", file=sys.stderr)
        try:
            _q("UPDATE seo_knowledge_import_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s",
               (str(e), time.time(), job_id))
        except Exception:
            pass

@seo_bp.route("/admin/seo-knowledge/import")
def seo_knowledge_import_page():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    brands = _list_brands()
    shell = _shell_open(key, "seo-knowledge", [("知識庫管理", "/admin/seo-knowledge"), ("批次匯入", None)])
    return render_template_string(IMPORT_HTML, key=key, shell=shell, brands=brands,
        knowledge_types=KNOWLEDGE_TYPES, ai_key_set=bool(ANTHROPIC_API_KEY))

@seo_bp.route("/admin/seo-knowledge/import/analyze", methods=["POST"])
def seo_knowledge_import_analyze():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    raw_text = data.get("raw_text", "")
    if not raw_text.strip():
        return jsonify({"error": "請貼上要分析的內容"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "尚未設定 ANTHROPIC_API_KEY，請在 Render → Environment 加上這個環境變數才能使用AI功能"}), 200
    now = time.time()
    job_id = _q("INSERT INTO seo_knowledge_import_jobs (status,created_at,updated_at) VALUES (%s,%s,%s) RETURNING id",
                ("pending", now, now), fetch="id")
    threading.Thread(target=_run_knowledge_import_job, args=(job_id, raw_text), daemon=True).start()
    return jsonify({"job_id": job_id})

@seo_bp.route("/admin/seo-knowledge/import/status/<int:job_id>")
def seo_knowledge_import_status(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    row = _q("SELECT status,result,error_msg FROM seo_knowledge_import_jobs WHERE id=%s", (job_id,), fetch="one")
    if not row:
        return jsonify({"status": "error", "error": "找不到這個匯入任務"})
    status, result, error_msg = row
    out = {"status": status}
    if status == "error":
        out["error"] = error_msg
    elif status == "done":
        try:
            out["items"] = json.loads(result or "[]")
        except Exception:
            out["items"] = []
    return jsonify(out)

@seo_bp.route("/admin/seo-knowledge/import/confirm", methods=["POST"])
def seo_knowledge_import_confirm():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    brand = data.get("brand", "")
    category = data.get("category", "")
    items = [it for it in (data.get("items") or []) if not it.get("skip")]
    if not items:
        return jsonify({"error": "沒有要匯入的項目"}), 400
    try:
        inserted, updated = _knowledge_upsert(brand, category, items)
    except Exception as e:
        return jsonify({"error": f"寫入失敗：{e}"}), 200
    return jsonify({"inserted": inserted, "updated": updated})

@seo_bp.route("/admin/seo-settings")
def seo_settings_page():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    analyze_prompt = _get_prompt_template("analyze", DEFAULT_ANALYZE_PROMPT)
    generate_prompt = _get_prompt_template("generate", DEFAULT_GENERATE_PROMPT)
    shell = _shell_open(key, "seo-settings", [("Prompt 設定", None)])
    return render_template_string(SETTINGS_HTML, key=key, shell=shell,
        analyze_prompt=analyze_prompt, generate_prompt=generate_prompt, flash=request.args.get("flash", ""))

@seo_bp.route("/admin/seo-settings/save", methods=["POST"])
def seo_settings_save():
    ok, key = check_auth()
    if not ok:
        abort(403)
    prompt_key = request.form.get("prompt_key", "")
    content = request.form.get("content", "")
    if prompt_key not in ("analyze", "generate") or not content.strip():
        return redirect(f"/admin/seo-settings?key={key}")
    _save_prompt_template(prompt_key, content)
    return redirect(f"/admin/seo-settings?key={key}&flash=已儲存")

@seo_bp.route("/admin/seo-settings/reset", methods=["POST"])
def seo_settings_reset():
    ok, key = check_auth()
    if not ok:
        abort(403)
    prompt_key = request.form.get("prompt_key", "")
    if prompt_key not in ("analyze", "generate"):
        return redirect(f"/admin/seo-settings?key={key}")
    default = DEFAULT_ANALYZE_PROMPT if prompt_key == "analyze" else DEFAULT_GENERATE_PROMPT
    _save_prompt_template(prompt_key, default)
    return redirect(f"/admin/seo-settings?key={key}&flash=已還原預設值")

@seo_bp.route("/admin/seo-generator")
def seo_generator_page():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    brands = _list_brands()
    shell = _shell_open(key, "seo-generator", [("AI 生成文章", None)])
    return render_template_string(GENERATOR_HTML, key=key, shell=shell, brands=brands, ai_key_set=bool(ANTHROPIC_API_KEY),
        prefill_brand=request.args.get("brand", ""), prefill_category=request.args.get("category", ""),
        prefill_topic=request.args.get("topic", ""), prefill_opp_id=request.args.get("opp_id", ""))

@seo_bp.route("/admin/seo-generator/analyze", methods=["POST"])
def seo_generator_analyze():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    brand_key = data.get("brand", "")
    category = data.get("category", "")
    topic = data.get("topic", "")
    if not topic.strip():
        return jsonify({"error": "請輸入主題"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "尚未設定 ANTHROPIC_API_KEY，請在 Render → Environment 加上這個環境變數才能使用AI功能"}), 200
    brand = _get_brand(brand_key)
    prompt = _analyze_intent_prompt(brand, category, topic)
    text, err = _ai_call(prompt, model="claude-haiku-4-5-20251001", max_tokens=1500)
    if err:
        return jsonify({"error": f"AI分析失敗：{err}"}), 200
    return jsonify({"analysis": text})

def _run_generate_job(job_id, brand_key, category, topic, analysis, opp_id=None):
    try:
        _q("UPDATE seo_generate_jobs SET status='running', updated_at=%s WHERE id=%s", (time.time(), job_id))
        brand = _get_brand(brand_key)
        knowledge_items = _get_knowledge_for_prompt(brand_key, category, limit=10)
        prompt = _generate_article_prompt(brand, category, topic, analysis, knowledge_items)
        result, err = _ai_call_json(prompt, model="claude-sonnet-4-6", max_tokens=8000)
        if err:
            _q("UPDATE seo_generate_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s",
               (f"AI生成失敗：{err}", time.time(), job_id))
            return
        now = time.time()
        new_id = _q("""INSERT INTO seo_articles
               (title,slug,meta_title,meta_description,content,ai_summary,status,
                brand_key,category,created_at,updated_at,published_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
           (result.get("title",""), result.get("slug",""), result.get("meta_title",""),
            result.get("meta_description",""), result.get("content",""), result.get("ai_summary",""),
            "draft", brand_key, category, now, now, 0), fetch="id")
        _q("""UPDATE seo_generate_jobs SET status='done', article_id=%s, updated_at=%s WHERE id=%s""",
           (new_id, time.time(), job_id))
        if opp_id:
            try:
                _q("UPDATE seo_opportunities SET status='generated', updated_at=%s WHERE id=%s", (now, int(opp_id)))
            except Exception as e:
                import sys; print(f"[SEO Opportunity] 更新狀態失敗：{e}", file=sys.stderr)
    except Exception as e:
        import sys; print(f"[SEO Generate Job Error] {e}", file=sys.stderr)
        try:
            _q("UPDATE seo_generate_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s",
               (str(e), time.time(), job_id))
        except Exception:
            pass

@seo_bp.route("/admin/seo-generator/generate", methods=["POST"])
def seo_generator_generate():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    brand_key = data.get("brand", "")
    category = data.get("category", "")
    topic = data.get("topic", "")
    analysis = data.get("analysis", "")
    opp_id = data.get("opp_id") or None
    if not topic.strip():
        return jsonify({"error": "請輸入主題"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "尚未設定 ANTHROPIC_API_KEY，請在 Render → Environment 加上這個環境變數才能使用AI功能"}), 200
    now = time.time()
    job_id = _q("INSERT INTO seo_generate_jobs (status,created_at,updated_at) VALUES (%s,%s,%s) RETURNING id",
                ("pending", now, now), fetch="id")
    threading.Thread(target=_run_generate_job, args=(job_id, brand_key, category, topic, analysis, opp_id), daemon=True).start()
    return jsonify({"job_id": job_id})

@seo_bp.route("/admin/seo-generator/generate/status/<int:job_id>")
def seo_generator_generate_status(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    row = _q("SELECT status,article_id,error_msg FROM seo_generate_jobs WHERE id=%s", (job_id,), fetch="one")
    if not row:
        return jsonify({"status": "error", "error": "找不到這個生成任務"})
    status, article_id, error_msg = row
    out = {"status": status}
    if status == "error":
        out["error"] = error_msg
    elif status == "done":
        title_row = _q("SELECT title FROM seo_articles WHERE id=%s", (article_id,), fetch="one")
        out["article_id"] = article_id
        out["title"] = title_row[0] if title_row else ""
    return jsonify(out)

@seo_bp.route("/api/seo/articles", methods=["GET"])
def api_seo_articles():
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    rows = _q("""SELECT id,title,slug,meta_title,meta_description,status,ai_summary,updated_at
                 FROM seo_articles ORDER BY id DESC""", fetch="all") or []
    return jsonify([{
        "id": r[0], "title": r[1], "slug": r[2], "meta_title": r[3],
        "meta_description": r[4], "status": r[5], "ai_summary": r[6], "updated_at": r[7],
    } for r in rows])

# ── Dashboard ──────────────────────────────────────────────────

def _articles_with_latest_tracking(brand_key="", category=""):
    """每篇文章 + 該文章最新一筆 seo_tracking 記錄（用 article_id 分組取最大 id），可選擇依品牌/品類篩選"""
    where = []
    params = []
    if brand_key:
        where.append("a.brand_key=%s"); params.append(brand_key)
    if category:
        where.append("a.category=%s"); params.append(category)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = _q(f"""
        SELECT a.id, a.title, a.brand_key, a.category, a.published_at,
               t.record_date, t.ranking, t.clicks, t.impressions,
               t.line_inquiries, t.orders, t.revenue
        FROM seo_articles a
        LEFT JOIN (
            SELECT t1.* FROM seo_tracking t1
            INNER JOIN (SELECT article_id, MAX(id) AS max_id FROM seo_tracking GROUP BY article_id) t2
              ON t1.article_id = t2.article_id AND t1.id = t2.max_id
        ) t ON t.article_id = a.id
        {where_sql}
        ORDER BY a.id DESC
    """, tuple(params), fetch="all") or []
    out = []
    for r in rows:
        clicks = r[7] or 0
        impressions = r[8] or 0
        ctr = round(clicks / impressions * 100, 2) if impressions else 0
        out.append({
            "id": r[0], "title": r[1], "brand_key": r[2] or "", "category": r[3] or "",
            "published_at": time.strftime("%Y-%m-%d", time.localtime(r[4])) if r[4] else "",
            "record_date": r[5] or "", "ranking": r[6] or "",
            "clicks": clicks, "impressions": impressions, "ctr": ctr,
            "line_inquiries": r[9] or 0, "orders": r[10] or 0, "revenue": float(r[11] or 0),
        })
    return out

def _top_n(items, key, n=5):
    return sorted(items, key=lambda x: x.get(key, 0), reverse=True)[:n]

def _month_bounds(year, month):
    start = f"{year:04d}-{month:02d}-01"
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return start, f"{ny:04d}-{nm:02d}-01"

def _prev_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)

def _sum_tracking(brand_key, category, date_from, date_to_exclusive):
    where = ["t.record_date >= %s", "t.record_date < %s"]
    params = [date_from, date_to_exclusive]
    if brand_key:
        where.append("a.brand_key=%s"); params.append(brand_key)
    if category:
        where.append("a.category=%s"); params.append(category)
    row = _q(f"""
        SELECT COALESCE(SUM(t.clicks),0), COALESCE(SUM(t.impressions),0),
               COALESCE(SUM(t.line_inquiries),0), COALESCE(SUM(t.orders),0), COALESCE(SUM(t.revenue),0)
        FROM seo_tracking t JOIN seo_articles a ON a.id = t.article_id
        WHERE {" AND ".join(where)}
    """, tuple(params), fetch="one") or (0, 0, 0, 0, 0)
    clicks, impressions, inquiries, orders, revenue = row
    return {"clicks": clicks, "impressions": impressions, "line_inquiries": inquiries,
            "orders": orders, "revenue": float(revenue)}

def _pct_change(curr, prev):
    if not prev:
        return None
    return round((curr - prev) / prev * 100, 1)

def _dashboard_stats(brand_key, category):
    """統計卡片：待產出主題／草稿文章／已發布文章（依品牌品類篩選）＋本月成效（含較上月百分比變化）"""
    pending_titles = (_q("SELECT COUNT(*) FROM seo_titles WHERE status='待寫'", fetch="one") or (0,))[0]
    where = []
    params = []
    if brand_key:
        where.append("brand_key=%s"); params.append(brand_key)
    if category:
        where.append("category=%s"); params.append(category)
    where_sql = (" AND " + " AND ".join(where)) if where else ""
    draft_count = (_q(f"SELECT COUNT(*) FROM seo_articles WHERE status='draft'{where_sql}", tuple(params), fetch="one") or (0,))[0]
    published_count = (_q(f"SELECT COUNT(*) FROM seo_articles WHERE status='published'{where_sql}", tuple(params), fetch="one") or (0,))[0]

    now = time.localtime()
    cur_start, cur_end = _month_bounds(now.tm_year, now.tm_mon)
    py, pm = _prev_month(now.tm_year, now.tm_mon)
    prev_start, prev_end = _month_bounds(py, pm)
    cur = _sum_tracking(brand_key, category, cur_start, cur_end)
    prev = _sum_tracking(brand_key, category, prev_start, prev_end)

    return {
        "pending_titles": pending_titles, "draft_count": draft_count, "published_count": published_count,
        "cur": cur, "prev": prev,
        "pct": {k: _pct_change(cur[k], prev[k]) for k in ("clicks", "impressions", "line_inquiries", "orders", "revenue")},
    }

def _list_categories():
    rows = _q("SELECT DISTINCT category FROM seo_articles WHERE category != '' ORDER BY category", fetch="all") or []
    return [r[0] for r in rows]

def _get_ai_suggestion(force=False):
    """絕不丟例外給呼叫端——任何失敗都回傳一句友善訊息，確保 Dashboard 一定能正常開啟。"""
    try:
        row = _q("SELECT id,content,generated_at FROM seo_ai_suggestions ORDER BY id DESC LIMIT 1", fetch="one")
        if row and not force and (time.time() - row[2] < 86400):
            return row[1], row[2]
    except Exception as e:
        import sys; print(f"[SEO AI Suggestion] 讀取快取失敗：{e}", file=sys.stderr)
        return "AI建議暫時無法取得。", 0

    if not ANTHROPIC_API_KEY:
        return "AI建議暫時無法取得（尚未設定 ANTHROPIC_API_KEY）。", 0

    try:
        items = _articles_with_latest_tracking()
        top = _top_n([i for i in items if i["clicks"] or i["orders"] or i["line_inquiries"]], "clicks", 8)
    except Exception as e:
        import sys; print(f"[SEO AI Suggestion] 讀取文章數據失敗：{e}", file=sys.stderr)
        return "AI建議暫時無法取得。", 0

    if not top:
        return "目前還沒有足夠的成效數據，請先到各篇文章的「成效記錄」輸入數據，AI才能根據表現分析推薦下一批主題。", time.time()

    summary_lines = [
        f"- {i['title']}（品牌:{i['brand_key']}, 類別:{i['category']}）"
        f" 點擊{i['clicks']} 曝光{i['impressions']} CTR{i['ctr']}% 詢價{i['line_inquiries']} 成交{i['orders']} 營收{i['revenue']}"
        for i in top
    ]
    prompt = f"""你是台灣SEO/GEO/AEO內容策略專家。以下是目前表現最好的幾篇SEO文章數據：

{chr(10).join(summary_lines)}

請根據這些表現最佳文章的品牌、類別、共同特徵（例如哪種問題類型、哪種品類最容易帶來詢價與成交），
推薦下一批（5個）最值得寫的SEO文章主題，並簡短說明推薦原因。

輸出格式：純文字條列，繁體中文，台灣用語，不要過度誇大，不要虛構數據。"""
    text, err = _ai_call(prompt, model="claude-haiku-4-5-20251001", max_tokens=1200)
    if err:
        import sys; print(f"[SEO AI Suggestion] AI呼叫失敗：{err}", file=sys.stderr)
        return "AI建議暫時無法取得。", 0
    now = time.time()
    try:
        _q("INSERT INTO seo_ai_suggestions (content,generated_at) VALUES (%s,%s)", (text, now))
    except Exception as e:
        import sys; print(f"[SEO AI Suggestion] 寫入快取失敗：{e}", file=sys.stderr)
    return text, now

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEO 數據儀表板</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
""" + SIDEBAR_CSS + """
.container{max-width:1100px;margin:24px auto;padding:0 16px}
.section{background:#fff;border-radius:14px;padding:20px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.section h3{font-size:15px;margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 6px;border-bottom:1px solid #f0f0f0;white-space:nowrap}
th{color:#888;font-weight:600;font-size:11px;text-transform:uppercase}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
@media(max-width:900px){.grid4{grid-template-columns:repeat(2,1fr)}}
.lb-card{background:#fafafa;border-radius:10px;padding:12px}
.lb-card h4{font-size:12px;color:#888;margin-bottom:8px;text-transform:uppercase}
.lb-item{font-size:12px;padding:5px 0;border-bottom:1px solid #eee;display:flex;justify-content:space-between;gap:6px}
.lb-item:last-child{border-bottom:none}
.lb-item .v{font-weight:700;color:#0d6efd;flex-shrink:0}
.ai-box{white-space:pre-wrap;font-size:13px;line-height:1.8;background:#fafafa;border-radius:10px;padding:14px}
.ai-meta{font-size:11px;color:#aaa;margin-top:8px}
.btn{padding:6px 14px;background:#0d6efd;color:#fff;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer}
.scroll-x{overflow-x:auto}
.filter-bar{display:flex;gap:10px;align-items:center;margin-bottom:18px;flex-wrap:wrap}
.filter-bar select{border:1px solid #ddd;border-radius:8px;padding:7px 10px;font-size:13px;background:#fff}
.stat-grid{display:grid;grid-template-columns:repeat(8,1fr);gap:12px;margin-bottom:20px}
@media(max-width:1100px){.stat-grid{grid-template-columns:repeat(4,1fr)}}
@media(max-width:600px){.stat-grid{grid-template-columns:repeat(2,1fr)}}
.stat-card{background:#fff;border-radius:14px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.stat-card .label{font-size:11px;color:#999;font-weight:600;margin-bottom:6px;white-space:nowrap}
.stat-card .value{font-size:20px;font-weight:800;color:#1a1a1a}
.stat-card .delta{font-size:11px;font-weight:700;margin-top:4px}
.delta-up{color:#2e7d32}.delta-down{color:#c62828}.delta-flat{color:#999}
.rank-tabs{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
.rank-tab{font-size:12px;font-weight:600;padding:6px 14px;border-radius:20px;border:1.5px solid #ddd;background:#fff;color:#555;cursor:pointer}
.rank-tab.active{background:#0d6efd;border-color:#0d6efd;color:#fff}
.rank-panel{display:none}
.rank-panel.active{display:block}
</style></head><body>
{{ shell|safe }}
<div class="container">

  <form class="filter-bar" method="GET" action="/admin/seo-dashboard">
    <input type="hidden" name="key" value="{{ key }}">
    <select name="brand" onchange="this.form.submit()">
      <option value="">全部品牌</option>
      {% for b in brands %}<option value="{{ b.key }}" {{ 'selected' if b.key==brand_key else '' }}>{{ b.name }}</option>{% endfor %}
    </select>
    <select name="category" onchange="this.form.submit()">
      <option value="">全部品類</option>
      {% for c in categories %}<option value="{{ c }}" {{ 'selected' if c==category else '' }}>{{ c }}</option>{% endfor %}
    </select>
  </form>

  <div class="stat-grid">
    <div class="stat-card"><div class="label">待產出主題</div><div class="value">{{ stats.pending_titles }}</div></div>
    <div class="stat-card"><div class="label">草稿文章</div><div class="value">{{ stats.draft_count }}</div></div>
    <div class="stat-card"><div class="label">已發布文章</div><div class="value">{{ stats.published_count }}</div></div>
    <div class="stat-card"><div class="label">本月曝光數</div><div class="value">{{ stats.cur.impressions }}</div>
      {% if stats.pct.impressions is not none %}<div class="delta {{ 'delta-up' if stats.pct.impressions>=0 else 'delta-down' }}">{{ '↑' if stats.pct.impressions>=0 else '↓' }} {{ stats.pct.impressions|abs }}% 較上月</div>{% endif %}
    </div>
    <div class="stat-card"><div class="label">本月點擊數</div><div class="value">{{ stats.cur.clicks }}</div>
      {% if stats.pct.clicks is not none %}<div class="delta {{ 'delta-up' if stats.pct.clicks>=0 else 'delta-down' }}">{{ '↑' if stats.pct.clicks>=0 else '↓' }} {{ stats.pct.clicks|abs }}% 較上月</div>{% endif %}
    </div>
    <div class="stat-card"><div class="label">本月詢價數</div><div class="value">{{ stats.cur.line_inquiries }}</div>
      {% if stats.pct.line_inquiries is not none %}<div class="delta {{ 'delta-up' if stats.pct.line_inquiries>=0 else 'delta-down' }}">{{ '↑' if stats.pct.line_inquiries>=0 else '↓' }} {{ stats.pct.line_inquiries|abs }}% 較上月</div>{% endif %}
    </div>
    <div class="stat-card"><div class="label">本月成交數</div><div class="value">{{ stats.cur.orders }}</div>
      {% if stats.pct.orders is not none %}<div class="delta {{ 'delta-up' if stats.pct.orders>=0 else 'delta-down' }}">{{ '↑' if stats.pct.orders>=0 else '↓' }} {{ stats.pct.orders|abs }}% 較上月</div>{% endif %}
    </div>
    <div class="stat-card"><div class="label">本月成交金額</div><div class="value">${{ stats.cur.revenue }}</div>
      {% if stats.pct.revenue is not none %}<div class="delta {{ 'delta-up' if stats.pct.revenue>=0 else 'delta-down' }}">{{ '↑' if stats.pct.revenue>=0 else '↓' }} {{ stats.pct.revenue|abs }}% 較上月</div>{% endif %}
    </div>
  </div>

  <div class="section">
    <h3>所有SEO文章（{{ items|length }}）</h3>
    <div class="scroll-x">
    <table>
      <tr><th>標題</th><th>品牌</th><th>類別</th><th>發布日期</th><th>曝光</th><th>點擊</th><th>CTR</th><th>排名</th><th>詢價</th><th>成交</th><th>營收</th></tr>
      {% for i in items %}
      <tr>
        <td>{{ i.title }}</td><td>{{ i.brand_key }}</td><td>{{ i.category }}</td><td>{{ i.published_at }}</td>
        <td>{{ i.impressions }}</td><td>{{ i.clicks }}</td><td>{{ i.ctr }}%</td><td>{{ i.ranking }}</td>
        <td>{{ i.line_inquiries }}</td><td>{{ i.orders }}</td><td>{{ i.revenue }}</td>
      </tr>
      {% endfor %}
    </table>
    </div>
  </div>

  <div class="section">
    <h3>排行榜 TOP5</h3>
    <div class="rank-tabs">
      <div class="rank-tab active" data-panel="rk-clicks" onclick="showRank(this)">點擊最高</div>
      <div class="rank-tab" data-panel="rk-ctr" onclick="showRank(this)">CTR最高</div>
      <div class="rank-tab" data-panel="rk-inquiries" onclick="showRank(this)">詢價最高</div>
      <div class="rank-tab" data-panel="rk-orders" onclick="showRank(this)">成交最高</div>
      <div class="rank-tab" data-panel="rk-revenue" onclick="showRank(this)">營收最高</div>
    </div>
    <div class="lb-card rank-panel active" id="rk-clicks">
      {% for i in top_clicks %}<div class="lb-item"><span>{{ i.title }}</span><span class="v">{{ i.clicks }}</span></div>{% endfor %}
    </div>
    <div class="lb-card rank-panel" id="rk-ctr">
      {% for i in top_ctr %}<div class="lb-item"><span>{{ i.title }}</span><span class="v">{{ i.ctr }}%</span></div>{% endfor %}
    </div>
    <div class="lb-card rank-panel" id="rk-inquiries">
      {% for i in top_inquiries %}<div class="lb-item"><span>{{ i.title }}</span><span class="v">{{ i.line_inquiries }}</span></div>{% endfor %}
    </div>
    <div class="lb-card rank-panel" id="rk-orders">
      {% for i in top_orders %}<div class="lb-item"><span>{{ i.title }}</span><span class="v">{{ i.orders }}</span></div>{% endfor %}
    </div>
    <div class="lb-card rank-panel" id="rk-revenue">
      {% for i in top_revenue %}<div class="lb-item"><span>{{ i.title }}</span><span class="v">${{ i.revenue }}</span></div>{% endfor %}
    </div>
    <script>
    function showRank(el){
      document.querySelectorAll('.rank-tab').forEach(t=>t.classList.remove('active'));
      document.querySelectorAll('.rank-panel').forEach(p=>p.classList.remove('active'));
      el.classList.add('active');
      document.getElementById(el.dataset.panel).classList.add('active');
    }
    </script>
  </div>

  <div class="section">
    <h3>AI 下一批主題建議</h3>
    <div class="ai-box">{{ suggestion }}</div>
    <div class="ai-meta">上次生成：{{ suggestion_time }}（每天最多重新生成一次）</div>
    <form method="POST" action="/admin/seo-dashboard/refresh-suggestion?key={{ key }}" style="margin-top:10px">
      <button class="btn" type="submit">重新生成建議</button>
    </form>
  </div>

</div>
""" + SHELL_CLOSE + """
</body></html>"""

@seo_bp.route("/admin/seo-dashboard")
def seo_dashboard_page():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    brand_key = request.args.get("brand", "")
    category = request.args.get("category", "")
    try:
        items = _articles_with_latest_tracking(brand_key, category)
    except Exception as e:
        import sys; print(f"[SEO Dashboard] 讀取文章數據失敗：{e}", file=sys.stderr)
        items = []
    try:
        stats = _dashboard_stats(brand_key, category)
    except Exception as e:
        import sys; print(f"[SEO Dashboard] 統計計算失敗：{e}", file=sys.stderr)
        empty = {"clicks": 0, "impressions": 0, "line_inquiries": 0, "orders": 0, "revenue": 0}
        stats = {"pending_titles": 0, "draft_count": 0, "published_count": 0, "cur": empty, "prev": empty,
                 "pct": {k: None for k in empty}}
    try:
        brands = _list_brands()
        categories = _list_categories()
    except Exception:
        brands, categories = [], []
    suggestion, gen_at = _get_ai_suggestion()
    shell = _shell_open(key, "seo-dashboard", [("SEO 營運中心", None)])
    return render_template_string(DASHBOARD_HTML, key=key, shell=shell, items=items,
        brand_key=brand_key, category=category, brands=brands, categories=categories, stats=stats,
        top_clicks=_top_n(items, "clicks"), top_ctr=_top_n(items, "ctr"),
        top_inquiries=_top_n(items, "line_inquiries"), top_orders=_top_n(items, "orders"),
        top_revenue=_top_n(items, "revenue"),
        suggestion=suggestion,
        suggestion_time=time.strftime("%Y-%m-%d %H:%M", time.localtime(gen_at)) if gen_at else "尚未生成")

@seo_bp.route("/admin/seo-dashboard/refresh-suggestion", methods=["POST"])
def seo_dashboard_refresh_suggestion():
    ok, key = check_auth()
    if not ok:
        abort(403)
    _get_ai_suggestion(force=True)
    return redirect(f"/admin/seo-dashboard?key={key}")

init_seo_db()
