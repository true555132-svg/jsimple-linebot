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
ARTICLE_STATUS = ["topic_pending", "ai_generating", "draft_review", "needs_revision",
                  "ready_to_publish", "published", "needs_optimization", "inactive",
                  "draft"]
ARTICLE_STATUS_LABELS = {
    "topic_pending": "主題待確認", "ai_generating": "AI產生中", "draft_review": "草稿待審",
    "needs_revision": "需人工修改", "ready_to_publish": "可發布", "published": "已發布",
    "needs_optimization": "需優化", "inactive": "已失效／暫停",
    "draft": "草稿",  # 舊資料相容（升級前生成的文章）
}
NEXT_ACTION_OPTIONS = ["生成文章", "AI檢查", "人工審稿", "修改內容", "發布", "優化標題", "補FAQ", "補內部連結"]

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
            # AI SEO 生產線升級：先用 extra（JSON存成TEXT）放新欄位，不大動既有資料表結構
            for col_sql in [
                "ALTER TABLE seo_articles ADD COLUMN IF NOT EXISTS extra TEXT DEFAULT '{}'",
                "ALTER TABLE seo_opportunities ADD COLUMN IF NOT EXISTS extra TEXT DEFAULT '{}'",
            ]:
                try: cur.execute(col_sql)
                except Exception: pass
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_brand_rules (
                    id               SERIAL PRIMARY KEY,
                    brand            TEXT DEFAULT '',
                    category         TEXT DEFAULT '',
                    positioning      TEXT DEFAULT '',
                    target_audience  TEXT DEFAULT '',
                    key_products     TEXT DEFAULT '',
                    avoid_directions TEXT DEFAULT '',
                    tone             TEXT DEFAULT '',
                    cta_direction    TEXT DEFAULT '',
                    keywords         TEXT DEFAULT '',
                    negative_keywords TEXT DEFAULT '',
                    created_at       FLOAT DEFAULT 0,
                    updated_at       FLOAT DEFAULT 0
                )
            """)
            # 通用規則比對升級：article_type 讓同一品牌+品類可以針對不同文章類型設不同規則；
            # priority 讓比對分數打平時可以人工決定優先順序，兩者都留空/預設即可保留舊行為
            for col_sql in [
                "ALTER TABLE seo_brand_rules ADD COLUMN IF NOT EXISTS article_type TEXT DEFAULT ''",
                "ALTER TABLE seo_brand_rules ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 100",
            ]:
                try: cur.execute(col_sql)
                except Exception: pass
            try:
                cur.execute("DROP INDEX IF EXISTS idx_seo_brand_rules_unique")
            except Exception: pass
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_seo_brand_rules_unique ON seo_brand_rules(brand, category, article_type)")
            cur.execute("SELECT COUNT(*) FROM seo_brand_rules")
            if cur.fetchone()[0] == 0:
                now0 = time.time()
                cur.execute("""INSERT INTO seo_brand_rules
                    (brand,category,article_type,priority,positioning,target_audience,key_products,avoid_directions,tone,cta_direction,keywords,negative_keywords,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    ("jsimple", "辦公家具", "", 100,
                     "中價位／中高質感辦公家具，不走低價路線。主打現代工業風、木質搭配黑鐵件、實用且有質感的辦公室配置。",
                     "中小企業、工作室、公司採購、設計公司、辦公室搬遷、新設立辦公室的客戶。",
                     "員工桌、主管桌、經理桌、會議桌、洽談桌、辦公椅、培訓椅、資料櫃、展示櫃、辦公室整體配置。",
                     "不要寫成學生宿舍、租屋套房、高架床、小房間家具、低價家具、便宜辦公桌導向。",
                     "專業、清楚、務實、不浮誇，適合公司採購與老闆閱讀。",
                     "請使用者提供辦公室尺寸、人數、預算與需求，可協助搭配辦公桌椅、會議桌、主管桌與收納櫃，提供配置與報價建議。",
                     "辦公家具、辦公桌、主管桌、經理桌、員工桌、多人工作站、會議桌、洽談桌、辦公椅、培訓椅、資料櫃、小型辦公室家具、中小企業辦公家具、辦公室配置、辦公家具採購。",
                     "", now0, now0))
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_quality_check_jobs (
                    id           SERIAL PRIMARY KEY,
                    status       TEXT DEFAULT 'pending',
                    article_id   INTEGER DEFAULT NULL,
                    result       TEXT DEFAULT '',
                    error_msg    TEXT DEFAULT '',
                    created_at   FLOAT DEFAULT 0,
                    updated_at   FLOAT DEFAULT 0
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

OPPORTUNITY_STATUS = ["idea", "confirmed", "draft_generated", "published", "paused"]
OPPORTUNITY_STATUS_LABELS = {
    "idea": "待確認", "confirmed": "已確認", "draft_generated": "已生成草稿",
    "published": "已發布", "paused": "暫停",
    "selected": "已確認", "generated": "已生成草稿",  # 舊資料相容（badge顯示用，下拉選單不會出現這兩個值）
}
ARTICLE_TYPES = ["資訊型", "教學型", "比較型", "商業導購", "FAQ", "案例分享", "價格分析", "尺寸指南", "其他"]
ARTICLE_TYPE_GUIDE_MAP = {
    "資訊型": "以知識性說明為核心，完整解答讀者疑問，給出明確結論與依據。",
    "教學型": "以step-by-step教學流程為核心，列出操作或選購步驟，每個步驟給出具體做法。",
    "比較型": "以對比表為核心，列出比較維度，語氣中立，最後給出明確結論建議。",
    "商業導購": "以商品特色與適用情境為核心，自然導向詢價與購買行動。",
    "FAQ": "以常見問題集為主體，每題直接給答案，避免空泛鋪陳。",
    "案例分享": "以情境描述、解決方案、實際成果為核心，呈現真實使用情境。",
    "價格分析": "以價格區間、影響價格的因素、成本對照為核心，給讀者明確的價格判斷依據。",
    "尺寸指南": "以規格數據、空間或人數對照表為核心，幫助讀者快速對照選擇。",
    "其他": "依主題內容彈性規劃架構重點，但仍須保留下列GEO結構元素。",
}

def _article_type_guide(article_type):
    """不寫死任何品牌或品類，只依「文章類型」這個通用維度決定架構指引；
    沒指定類型（AI自動判斷）時，交給AI自己依搜尋意圖判斷。"""
    article_type = (article_type or "").strip()
    if not article_type:
        return ("文章類型：請先依下面的「搜尋意圖分析」自行判斷最適合的文章類型"
                f"（從以下類型挑一個最貼切的：{ '、'.join(ARTICLE_TYPES) }），"
                "並依該類型決定文章架構重點與語氣，不需要在最終輸出內容中標註你判斷的類型名稱。")
    guide = ARTICLE_TYPE_GUIDE_MAP.get(article_type, ARTICLE_TYPE_GUIDE_MAP["其他"])
    return f"文章類型：{article_type} → {guide}"

def _opportunity_prompt(brand, category, knowledge_items, brand_rule=None):
    return f"""你是台灣SEO/GEO/AEO內容策略專家。請根據以下品牌資訊，產生20個有價值的SEO文章主題。

品牌：{brand.get('name','')}（{brand.get('category','')}）
品牌風格：{brand.get('style','')}
品類：{category}

品牌SEO規則（重要，主題不可偏離）：
{_brand_rule_block(brand_rule)}

品牌知識庫參考（真實資料，主題應該盡量貼近這些內容，不要憑空想像不存在的功能或案例）：
{_knowledge_block(knowledge_items)}

請產生20個SEO文章主題，要求：
1. 是真實使用者會搜尋的問題，不要空泛的標題
2. 涵蓋不同類型：價格型、比較型、商業型、資訊型都要有，不要全部都一樣
3. 盡量能對應到上面知識庫的真實商品/案例/特色
4. 不可偏離品牌SEO規則裡的「禁止偏離方向」

每個主題請評估：
- main_keyword：這個主題的主關鍵字
- seo_score（1~10）：搜尋量與排名機會
- geo_score（1~10）：適合被Google AI Overview / ChatGPT引用的程度
- conversion_score（1~10）：帶來詢價/成交的機會
- difficulty（1~10）：競爭難度，10代表最難
- business_score（1~100）：整體商業價值（綜合考量帶來詢價/成交的潛力與品牌契合度）
- competition_score（1~100）：競爭度，分數越高代表越難排名
- priority：A（優先）/ B（中等）/ C（次要）
- suggested_article_type：建議文章類型，從「導購文/比較文/尺寸指南/案例文/FAQ文/採購指南/問題解決文/品牌介紹文」選一個
- related_products：對應商品，逗號分隔
- reason：50字以內，說明為什麼推薦這個主題

輸出格式（只輸出JSON陣列，不要其他文字，不要markdown code block）：
[
  {{"topic": "主題", "main_keyword": "主關鍵字", "search_intent": "搜尋意圖簡述", "target_customer": "目標客群",
    "seo_score": 8, "geo_score": 7, "conversion_score": 9, "difficulty": 4,
    "business_score": 85, "competition_score": 50, "priority": "A",
    "suggested_article_type": "採購指南", "related_products": "員工桌,辦公椅",
    "reason": "推薦原因"}}
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
        extra = _dump_extra({
            "main_keyword": it.get("main_keyword", ""),
            "business_score": int(it.get("business_score", 0) or 0),
            "competition_score": int(it.get("competition_score", 0) or 0),
            "priority": it.get("priority", "") if it.get("priority") in ("A", "B", "C") else "",
            "suggested_article_type": it.get("suggested_article_type", ""),
            "related_products": it.get("related_products", ""),
        })
        _q("""INSERT INTO seo_opportunities
              (brand,category,topic,search_intent,target_customer,seo_score,geo_score,conversion_score,
               difficulty,reason,status,extra,created_at,updated_at)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
           (brand, category, topic, it.get("search_intent", ""), it.get("target_customer", ""),
            int(it.get("seo_score", 0) or 0), int(it.get("geo_score", 0) or 0),
            int(it.get("conversion_score", 0) or 0), int(it.get("difficulty", 0) or 0),
            it.get("reason", ""), "idea", extra, now, now))
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
                      seo_score,geo_score,conversion_score,difficulty,reason,status,updated_at,extra
                      FROM seo_opportunities{where_sql}
                      ORDER BY (seo_score+geo_score+conversion_score-difficulty) DESC, id DESC""",
                   tuple(params), fetch="all") or []
        out = []
        for r in rows:
            extra = _parse_extra(r[13])
            out.append({
                "id": r[0], "brand": r[1], "category": r[2], "topic": r[3], "search_intent": r[4],
                "target_customer": r[5], "seo_score": r[6], "geo_score": r[7], "conversion_score": r[8],
                "difficulty": r[9], "reason": r[10], "status": r[11],
                "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[12])) if r[12] else "",
                "main_keyword": extra.get("main_keyword", ""),
                "business_score": extra.get("business_score", 0),
                "competition_score": extra.get("competition_score", 0),
                "priority": extra.get("priority", ""),
                "related_products": extra.get("related_products", ""),
                "suggested_article_type": extra.get("suggested_article_type", ""),
            })
        return out
    except Exception as e:
        import sys; print(f"[SEO Opportunities] 讀取清單失敗：{e}", file=sys.stderr)
        return []

def _safe_job_error_msg(e):
    """背景任務發生未預期例外時，回給使用者看的訊息——絕不把原始DB連線字串/系統內部錯誤細節洩漏出去。
    完整原因一律先用 print() 記到伺服器log，這裡只回一句通用訊息。"""
    text = str(e)
    if "psycopg2" in text or "connection to server" in text or "OperationalError" in type(e).__name__:
        return "資料庫暫時連線異常，請稍後再試一次。"
    return "發生未預期的錯誤，請稍後再試一次。"

# ── extra（JSON存成TEXT）共用工具：避免大改既有資料表結構 ──────────

def _parse_extra(raw):
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}

def _dump_extra(d):
    return json.dumps(d or {}, ensure_ascii=False)

def _update_article_extra(article_id, patch):
    """讀出 seo_articles.extra，合併patch欄位後寫回（不覆蓋未提及的舊欄位）"""
    row = _q("SELECT extra FROM seo_articles WHERE id=%s", (article_id,), fetch="one")
    extra = _parse_extra(row[0] if row else None)
    extra.update(patch)
    _q("UPDATE seo_articles SET extra=%s, updated_at=%s WHERE id=%s", (_dump_extra(extra), time.time(), article_id))
    return extra

# ── 品牌 SEO 規則 ───────────────────────────────────────────────

def _list_brand_rules(brand=""):
    """brand 留空＝列出所有品牌的規則（管理頁用）；指定brand＝只列出該品牌可用的規則（手動選擇下拉選單用）"""
    if not DATABASE_URL:
        return []
    try:
        where_sql = " WHERE brand=%s" if brand else ""
        params = (brand,) if brand else ()
        rows = _q(f"""SELECT id,brand,category,article_type,priority,positioning,target_audience,key_products,
                     avoid_directions,tone,cta_direction,keywords,negative_keywords,updated_at
                     FROM seo_brand_rules{where_sql} ORDER BY brand,category,priority DESC""", params, fetch="all") or []
        return [{
            "id": r[0], "brand": r[1], "category": r[2], "article_type": r[3], "priority": r[4],
            "positioning": r[5], "target_audience": r[6],
            "key_products": r[7], "avoid_directions": r[8], "tone": r[9], "cta_direction": r[10],
            "keywords": r[11], "negative_keywords": r[12],
            "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[13])) if r[13] else "",
        } for r in rows]
    except Exception as e:
        import sys; print(f"[SEO Brand Rules] 讀取清單失敗：{e}", file=sys.stderr)
        return []

def _get_brand_rule_by_id(rule_id):
    if not DATABASE_URL or not rule_id:
        return {}
    try:
        row = _q("""SELECT id,brand,category,article_type,priority,positioning,target_audience,key_products,
                    avoid_directions,tone,cta_direction,keywords,negative_keywords
                    FROM seo_brand_rules WHERE id=%s""", (int(rule_id),), fetch="one")
        if not row:
            return {}
        return {"id": row[0], "brand": row[1], "category": row[2], "article_type": row[3], "priority": row[4],
                "positioning": row[5], "target_audience": row[6], "key_products": row[7], "avoid_directions": row[8],
                "tone": row[9], "cta_direction": row[10], "keywords": row[11], "negative_keywords": row[12]}
    except Exception:
        return {}

def _match_brand_rule(brand, category, article_type=""):
    """通用比對：不寫死任何品牌/品類/文章類型名稱，全部從 seo_brand_rules 動態比對。
    比對規則：brand/category/article_type 三個欄位，規則裡留空＝該欄位萬用（適用所有值）；
    填了值就必須完全相符才算候選。候選裡比對分數最高的勝出（brand相符+1000、category相符+100、
    article_type相符+10），分數打平時用 priority（數字越大越優先）做最終決定。
    這個分數機制天然就會做到「精確比對→品牌預設規則→系統預設規則」三層退回，
    不需要另外寫三段if/else，未來新增品牌或規則也不用改這支邏輯。"""
    if not DATABASE_URL or not brand:
        return {}
    try:
        rows = _q("""SELECT id,brand,category,article_type,priority,positioning,target_audience,key_products,
                     avoid_directions,tone,cta_direction,keywords,negative_keywords
                     FROM seo_brand_rules""", fetch="all") or []
    except Exception as e:
        import sys; print(f"[SEO Brand Rules] 比對規則失敗：{e}", file=sys.stderr)
        return {}
    best, best_score = None, None
    for r in rows:
        r_brand, r_category, r_atype = r[1] or "", r[2] or "", r[3] or ""
        r_priority = r[4] if r[4] is not None else 100
        if r_brand and r_brand != brand:
            continue
        if r_category and r_category != category:
            continue
        if r_atype and r_atype != article_type:
            continue
        score = (1000 if r_brand else 0) + (100 if r_category else 0) + (10 if r_atype else 0)
        total = score * 100000 + r_priority
        if best_score is None or total > best_score:
            best_score = total
            best = r
    if not best:
        return {}
    return {"id": best[0], "brand": best[1], "category": best[2], "article_type": best[3], "priority": best[4],
            "positioning": best[5], "target_audience": best[6], "key_products": best[7],
            "avoid_directions": best[8], "tone": best[9], "cta_direction": best[10],
            "keywords": best[11], "negative_keywords": best[12]}

def _save_brand_rule(form):
    rule_id = form.get("id", "")
    now = time.time()
    try:
        priority = int(form.get("priority", "") or 100)
    except (TypeError, ValueError):
        priority = 100
    fields = (form.get("brand", ""), form.get("category", ""), form.get("article_type", ""), priority,
              form.get("positioning", ""), form.get("target_audience", ""), form.get("key_products", ""),
              form.get("avoid_directions", ""), form.get("tone", ""), form.get("cta_direction", ""),
              form.get("keywords", ""), form.get("negative_keywords", ""))
    if rule_id:
        _q("""UPDATE seo_brand_rules SET brand=%s,category=%s,article_type=%s,priority=%s,positioning=%s,
              target_audience=%s,key_products=%s,avoid_directions=%s,tone=%s,cta_direction=%s,keywords=%s,
              negative_keywords=%s,updated_at=%s WHERE id=%s""", fields + (now, rule_id))
    else:
        _q("""INSERT INTO seo_brand_rules
              (brand,category,article_type,priority,positioning,target_audience,key_products,avoid_directions,
               tone,cta_direction,keywords,negative_keywords,created_at,updated_at)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", fields + (now, now))

def _brand_rule_block(rule):
    """把品牌SEO規則轉成丟進Prompt的文字區塊"""
    if not rule:
        return "（沒有套用品牌SEO規則）"
    return f"""品牌定位：{rule.get('positioning','')}
目標客群：{rule.get('target_audience','')}
主打商品：{rule.get('key_products','')}
禁止偏離方向：{rule.get('avoid_directions','')}
語氣風格：{rule.get('tone','')}
CTA方向：{rule.get('cta_direction','')}
常用關鍵字：{rule.get('keywords','')}
禁用關鍵字／不建議方向：{rule.get('negative_keywords','')}"""

# ── AI 文章品質檢查 ─────────────────────────────────────────────

def _quality_check_prompt(article, brand_rule, extra):
    return f"""你是台灣SEO/GEO/AEO內容策略專家，請幫以下文章做發布前品質檢查。

品牌SEO規則（文章必須符合，不可偏離）：
{_brand_rule_block(brand_rule)}

文章主關鍵字：{extra.get('main_keyword','')}
文章目標客群：{extra.get('target_audience','')}
文章對應商品：{extra.get('related_products','')}

標題：{article.get('title','')}
Meta Title：{article.get('meta_title','')}
Meta Description：{article.get('meta_description','')}
文章內容：
{article.get('content','')[:8000]}

請檢查以下14項：
1. 標題是否包含主關鍵字
2. Meta Title是否清楚
3. Meta Description是否有吸引點擊
4. 開頭是否直接回答搜尋意圖
5. 內容是否符合品牌定位
6. 是否偏離目標客群
7. 是否有商品導購段落
8. 是否有對應商品
9. 是否有FAQ
10. 是否有CTA
11. 是否有內部連結建議
12. 是否需要拆成多篇文章
13. 是否有內容太泛、太像AI文的問題
14. 是否有錯誤或不適合品牌的方向（尤其注意是否偏離「禁止偏離方向」）

輸出格式（只輸出JSON，不要其他文字，不要markdown code block）：
{{
  "score": 0到100的整數,
  "recommend_publish": true或false,
  "issues": "主要問題，條列式文字，找到的問題具體寫出來",
  "suggestions": "修改建議，具體可執行",
  "next_status": "從 draft_review/needs_revision/ready_to_publish 選一個",
  "suggested_sections": "建議補強的段落，例如：補FAQ、補商品導購段落",
  "suggested_internal_links": "建議內部連結，逗號分隔",
  "suggested_related_products": "建議對應商品，逗號分隔"
}}"""

def _run_quality_check_job(job_id, article_id):
    try:
        _q("UPDATE seo_quality_check_jobs SET status='running', updated_at=%s WHERE id=%s", (time.time(), job_id))
        row = _q("""SELECT title,meta_title,meta_description,content,brand_key,category,extra
                    FROM seo_articles WHERE id=%s""", (article_id,), fetch="one")
        if not row:
            _q("UPDATE seo_quality_check_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s",
               ("找不到這篇文章", time.time(), job_id))
            return
        article = {"title": row[0], "meta_title": row[1], "meta_description": row[2], "content": row[3]}
        brand_key, category = row[4], row[5]
        extra = _parse_extra(row[6])
        brand_rule = _match_brand_rule(brand_key, category)
        prompt = _quality_check_prompt(article, brand_rule, extra)
        result, err = _ai_call_json(prompt, model="claude-sonnet-4-6", max_tokens=2000)
        if err:
            _q("UPDATE seo_quality_check_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s",
               (f"AI檢查失敗：{err}", time.time(), job_id))
            return
        next_status = result.get("next_status", "")
        if next_status not in ARTICLE_STATUS:
            next_status = "needs_revision"
        extra["ai_score"] = int(result.get("score", 0) or 0)
        extra["quality_check"] = result
        extra["next_action"] = "人工審稿" if result.get("recommend_publish") else "修改內容"
        now = time.time()
        _q("UPDATE seo_articles SET extra=%s, status=%s, updated_at=%s WHERE id=%s",
           (_dump_extra(extra), next_status, now, article_id))
        _q("UPDATE seo_quality_check_jobs SET status='done', result=%s, updated_at=%s WHERE id=%s",
           (_dump_extra(result), now, job_id))
    except Exception as e:
        import sys; print(f"[SEO Quality Check Job Error] {e}", file=sys.stderr)
        try:
            _q("UPDATE seo_quality_check_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s",
               (_safe_job_error_msg(e), time.time(), job_id))
        except Exception:
            pass

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
主關鍵字：[[MAIN_KEYWORD]]
搜尋意圖：[[SEARCH_INTENT]]
目標客群：[[TARGET_AUDIENCE]]
對應商品（文章必須導向這些商品，自然提及並建議）：[[RELATED_PRODUCTS]]
禁止偏離方向（絕對不要寫到這些主題或方向）：[[AVOID_DIRECTIONS]]
CTA方向：[[CTA_DIRECTION]]

搜尋意圖分析參考：
[[ANALYSIS]]

品牌SEO規則（重要，整篇文章不可偏離這份規則）：
[[BRAND_RULE]]

品牌知識庫（真實資料，請優先引用）：
[[KNOWLEDGE]]

━━━ 知識庫引用規則（重要） ━━━
1. 優先引用上面「品牌知識庫」的內容（規格、FAQ、案例、品牌特色），不要憑空想像
2. 不得虛構案例、數據、認證——如果知識庫沒有相關資料，就用一般專業說明帶過，不要假裝有具體數據或案例
3. 如果知識庫顯示「沒有符合此品牌/品類的資料」，文章仍要寫完，只是不要編造具體數字或案例去填補
4. 文章最後（FAQ與CTA之間或CTA之後）新增一個小節，標題為「本篇引用知識庫」：如果有引用，列出引用了哪幾筆資料的標題；如果完全沒有可引用的資料，就寫「本篇未引用品牌知識庫資料，內容為一般專業說明」

━━━ 品牌規則與目標客群（重要） ━━━
- 嚴格遵守上面的「禁止偏離方向」，絕對不要往那些方向寫
- 目標客群是[[TARGET_AUDIENCE]]，全文視角、用詞、案例都要對著這群人寫，不要寫成其他客群會看的內容
- 文章不能只講知識，必須自然導向「對應商品」，至少安排一段具體的商品導購段落
- CTA要呼應「CTA方向」，自然引導但不要太硬銷

━━━ 第一步：依文章類型規劃架構 ━━━
[[ARTICLE_TYPE_GUIDE]]
文章類型只影響語氣與架構重點，下面的GEO結構元素仍然每篇必要。

━━━ 第二步：規劃架構並寫完整文章 ━━━
從搜尋意圖挑最值得寫、問題導向、適合Google AI Overview與ChatGPT引用的標題。
主關鍵字「[[MAIN_KEYWORD]]」必須出現在：H1標題、文章開頭第一段、至少1個H2小標。

字數目標公式：(H2數量 + H3數量) × 200字 ± 25%
範例：4個H2 + 4個H3 = 目標約1600字；6個H2 + 6個H3 = 目標約2400字
不要為了湊字數填廢話，寧可精簡也不要膨脹。

━━━ 固定輸出結構（每篇必要，依序） ━━━
1. Meta Title
2. Meta Description
3. H1標題
4. 前言（直接回答搜尋意圖）
5. 主要內容段落（依文章類型規劃，至少2個H2）
6. 表格或條列比較（HTML <table>或<ul><li>）
7. 商品導購段落（自然提及「對應商品」，說明適用情境）
8. 品牌定位段落（簡述[[BRAND_NAME]]的定位與優勢，呼應品牌SEO規則）
9. FAQ（至少5題，<h3>寫問題，每題80~120字直接回答）
10. CTA結尾（呼應CTA方向，至少80字）
11. 建議內部連結（列出2~3個可以連結的相關主題，例如：xx怎麼選、xx比較）
12. 對應商品建議（重複列出本篇對應的商品，方便編輯加商品連結）
13. 主關鍵字與長尾關鍵字（列出本篇用到的主關鍵字與3~5個長尾關鍵字）

━━━ GEO結構元素（每篇必要） ━━━
1. 至少1個比較表或數據表（用HTML <table><tr><th><td>標籤，AI可直接引用）
2. 至少2個定義段落，格式：<blockquote><strong>詞彙</strong>：解釋其實際意義與用途</blockquote>
3. 至少2個條列清單（步驟、重點、注意事項，每點一個概念，用<ul><li>或<ol><li>標籤）
4. 倒金字塔結構：每個H2開頭先給結論，再展開說明

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
- 不要寫得太空泛、太像罐頭AI文章——多用具體場景、具體數字、具體商品名稱
- FAQ要對應真實搜尋問題，不要硬湊

輸出格式（只輸出JSON，不要其他文字，不要markdown code block）：
{
  "title": "標題（主關鍵字在前）",
  "slug": "/blog/xxx-xxx-xxx（英文小寫，連字號）",
  "meta_title": "Meta Title（含品牌名，60字以內）",
  "meta_description": "Meta Description（120字以內，含關鍵字與品牌名）",
  "ai_summary": "AI Overview摘要，100~200字，純文字，包含1~2個關鍵數字或結論",
  "internal_links": "建議內部連結，逗號分隔，2~3個",
  "long_tail_keywords": "長尾關鍵字，逗號分隔，3~5個",
  "content": "完整文章內容，純HTML格式（用<h2><h3><p><table><ul><ol><li><blockquote><strong>標籤），絕對不要用Markdown符號（不要##、不要**、不要>開頭的引用），這樣才能直接貼到網站後台的HTML/原始碼模式正常顯示，不需要再轉換。內容裡要包含上面13點固定結構（Meta部分已經是獨立欄位不用再放進content，從H1開始放進content即可）"
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

def _generate_article_prompt(brand, category, topic, intent_analysis, knowledge_items=None, fields=None, brand_rule=None):
    """fields: 結構化表單欄位 dict（main_keyword/search_intent/target_audience/related_products/
    avoid_directions/cta_direction/article_type），缺省時用空字串，不影響舊呼叫方式。"""
    fields = fields or {}
    tmpl = _get_prompt_template("generate", DEFAULT_GENERATE_PROMPT)
    return _fill_tokens(tmpl,
        BRAND_NAME=brand.get('name', ''), BRAND_CATEGORY=brand.get('category', ''),
        BRAND_STYLE=brand.get('style', ''), BRAND_TONE=brand.get('tone', ''),
        CATEGORY=category, TOPIC=topic, ANALYSIS=intent_analysis,
        KNOWLEDGE=_knowledge_block(knowledge_items or []),
        MAIN_KEYWORD=fields.get('main_keyword', ''),
        SEARCH_INTENT=fields.get('search_intent', ''),
        TARGET_AUDIENCE=fields.get('target_audience', ''),
        RELATED_PRODUCTS=fields.get('related_products', ''),
        AVOID_DIRECTIONS=fields.get('avoid_directions', ''),
        CTA_DIRECTION=fields.get('cta_direction', ''),
        ARTICLE_TYPE=fields.get('article_type', ''),
        ARTICLE_TYPE_GUIDE=_article_type_guide(fields.get('article_type', '')),
        BRAND_RULE=_brand_rule_block(brand_rule))

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
    ("seo-brand-rules", "🏷️ 品牌SEO規則",   "/admin/seo-brand-rules"),
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
.b-topic_pending{background:#f3e5f5;color:#7b1fa2}
.b-ai_generating{background:#e3f2fd;color:#1565c0}
.b-draft_review{background:#fff8e1;color:#f57f17}
.b-needs_revision{background:#fdecea;color:#c62828}
.b-ready_to_publish{background:#e0f2f1;color:#00695c}
.b-needs_optimization{background:#fff3e0;color:#e65100}
.b-inactive{background:#eceff1;color:#546e7a}
.scroll-x{overflow-x:auto}
.score-badge{display:inline-block;min-width:30px;text-align:center;padding:2px 8px;border-radius:10px;font-weight:700;font-size:12px}
.score-good{background:#e8f5e9;color:#2e7d32}
.score-warn{background:#fff8e1;color:#f57f17}
.score-bad{background:#fdecea;color:#c62828}
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
    <div class="scroll-x">
    <table>
      <tr><th>標題</th><th>狀態</th><th>主關鍵字</th><th>AI評分</th><th>對應商品</th><th>下一步</th><th>更新時間</th><th>操作</th></tr>
      {% for a in articles %}
      <tr>
        <td style="font-weight:600">{{ a.title }}</td>
        <td><span class="badge b-{{ a.status }}">{{ article_status_labels.get(a.status, a.status) }}</span></td>
        <td>{{ a.main_keyword }}</td>
        <td>
          {% if a.ai_score %}
          <span class="score-badge {{ 'score-good' if a.ai_score>=80 else ('score-warn' if a.ai_score>=60 else 'score-bad') }}">{{ a.ai_score }}</span>
          {% else %}<span style="color:#bbb">—</span>{% endif %}
        </td>
        <td style="max-width:160px;font-size:12px;color:#666">{{ a.related_products }}</td>
        <td>{% if a.next_action %}<span class="badge" style="background:#eef2ff;color:#3730a3">{{ a.next_action }}</span>{% endif %}</td>
        <td style="white-space:nowrap">{{ a.updated_at }}</td>
        <td style="white-space:nowrap">
          <a class="link btn-sm" href="/admin/seo/article/{{ a.id }}?key={{ key }}">編輯</a>
          <a class="link btn-sm" href="/admin/seo/article/{{ a.id }}/tracking?key={{ key }}">成效記錄</a>
          <form class="inline" method="POST" action="/admin/seo/article/{{ a.id }}/delete?key={{ key }}" onsubmit="return confirm('刪除這篇文章？')">
            <button class="btn btn-del btn-sm" type="submit">刪除</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
    </div>
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
.btn:disabled{background:#ccc;cursor:not-allowed}
.btn-outline{background:#fff;color:#0d6efd;border:1.5px solid #0d6efd}
.loading{font-size:13px;color:#888;margin-top:8px}
.err{color:#c62828;font-size:13px;margin-top:8px}
.qc-result{display:none;margin-top:14px;border-radius:10px;padding:14px;background:#fafafa}
.qc-result.active{display:block}
.qc-score{display:inline-block;font-size:22px;font-weight:800;padding:4px 16px;border-radius:10px}
.qc-good{background:#e8f5e9;color:#2e7d32}
.qc-warn{background:#fff8e1;color:#f57f17}
.qc-bad{background:#fdecea;color:#c62828}
.qc-row{margin-top:10px;font-size:13px;line-height:1.6}
.qc-row b{display:block;font-size:11px;color:#999;text-transform:uppercase;margin-bottom:2px}
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
    <select name="status" id="status-select">
      {% for s in article_status %}
      <option value="{{ s }}" {{ 'selected' if a and a[7]==s else '' }}>{{ article_status_labels[s] }}</option>
      {% endfor %}
    </select>
  </div>
  <div class="section">
    <label>主關鍵字</label>
    <input type="text" name="main_keyword" value="{{ extra.main_keyword or '' }}">
    <label>目標客群</label>
    <input type="text" name="target_audience" value="{{ extra.target_audience or '' }}">
    <label>對應商品</label>
    <input type="text" name="related_products" value="{{ extra.related_products or '' }}">
    <label>下一步動作</label>
    <select name="next_action">
      <option value="">（無）</option>
      {% for na in next_action_options %}<option value="{{ na }}" {{ 'selected' if extra.next_action==na else '' }}>{{ na }}</option>{% endfor %}
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

{% if a %}
<div class="section">
  <label>AI 品質檢查</label>
  <button class="btn btn-outline" id="btn-qc" onclick="doQualityCheck({{ a[0] }})" type="button">🔍 AI 品質檢查</button>
  <div class="loading" id="qc-loading" style="display:none">AI檢查中，請稍候...</div>
  <div class="err" id="qc-err"></div>
  <div class="qc-result" id="qc-result">
    <span class="qc-score" id="qc-score"></span>
    <span id="qc-recommend" style="margin-left:10px;font-weight:700"></span>
    <div class="qc-row"><b>主要問題</b><span id="qc-issues"></span></div>
    <div class="qc-row"><b>修改建議</b><span id="qc-suggestions"></span></div>
    <div class="qc-row"><b>建議補強段落</b><span id="qc-sections"></span></div>
    <div class="qc-row"><b>建議內部連結</b><span id="qc-links"></span></div>
    <div class="qc-row"><b>建議對應商品</b><span id="qc-products"></span></div>
    <div class="qc-row"><b>建議下一步狀態</b><span id="qc-next-status"></span>
      <button class="btn btn-outline" style="margin-left:8px;padding:4px 10px;font-size:11px" type="button" onclick="applyNextStatus()">套用到上面狀態欄位</button>
    </div>
  </div>
  {% if extra.quality_check %}
  <div class="hint" style="font-size:11px;color:#999;margin-top:8px">上次檢查分數：{{ extra.ai_score }}</div>
  {% endif %}
</div>
{% endif %}

</div>
<script>
const KEY = {{ key|tojson }};
async function safeJson(res){
  const text = await res.text();
  try { return JSON.parse(text); }
  catch(e) { throw new Error('伺服器回應異常（可能是逾時或部署中），請稍後再試。HTTP ' + res.status); }
}
let _qcNextStatus = '';
async function doQualityCheck(articleId){
  document.getElementById('btn-qc').disabled = true;
  document.getElementById('qc-loading').style.display = 'block';
  document.getElementById('qc-err').textContent = '';
  try {
    const res = await fetch('/admin/seo/article/' + articleId + '/quality-check?key=' + encodeURIComponent(KEY), {method:'POST'});
    const data = await safeJson(res);
    if (data.error) { document.getElementById('qc-err').textContent = data.error; document.getElementById('btn-qc').disabled = false; document.getElementById('qc-loading').style.display = 'none'; return; }
    await pollQc(data.job_id);
  } catch(e) {
    document.getElementById('qc-err').textContent = String(e.message || e);
    document.getElementById('btn-qc').disabled = false;
    document.getElementById('qc-loading').style.display = 'none';
  }
}
async function pollQc(jobId){
  while (true) {
    await new Promise(r => setTimeout(r, 3000));
    const res = await fetch('/admin/seo/article/quality-check/status/' + jobId + '?key=' + encodeURIComponent(KEY));
    const data = await safeJson(res);
    if (data.status === 'pending' || data.status === 'running') continue;
    if (data.status === 'error') { document.getElementById('qc-err').textContent = data.error || '檢查失敗'; break; }
    if (data.status === 'done') { renderQc(data.result); break; }
  }
  document.getElementById('btn-qc').disabled = false;
  document.getElementById('qc-loading').style.display = 'none';
}
function renderQc(r){
  const score = r.score || 0;
  const el = document.getElementById('qc-score');
  el.textContent = 'AI評分：' + score + '分';
  el.className = 'qc-score ' + (score>=80?'qc-good':(score>=60?'qc-warn':'qc-bad'));
  document.getElementById('qc-recommend').textContent = '是否建議發布：' + (r.recommend_publish ? '是' : '否');
  document.getElementById('qc-issues').textContent = r.issues || '（無）';
  document.getElementById('qc-suggestions').textContent = r.suggestions || '（無）';
  document.getElementById('qc-sections').textContent = r.suggested_sections || '（無）';
  document.getElementById('qc-links').textContent = r.suggested_internal_links || '（無）';
  document.getElementById('qc-products').textContent = r.suggested_related_products || '（無）';
  document.getElementById('qc-next-status').textContent = r.next_status || '（無）';
  _qcNextStatus = r.next_status || '';
  document.getElementById('qc-result').classList.add('active');
}
function applyNextStatus(){
  if (!_qcNextStatus) return;
  const sel = document.getElementById('status-select');
  for (const opt of sel.options) { if (opt.value === _qcNextStatus) { sel.value = _qcNextStatus; break; } }
}
</script>
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
.scroll-x{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:7px 5px;border-bottom:1px solid #f0f0f0;vertical-align:top}
th{color:#888;font-weight:600;font-size:10px;text-transform:uppercase}
td.topic-cell{max-width:220px;font-weight:700}
td.reason-cell{max-width:200px;color:#888;font-size:11px}
input.score{width:42px;text-align:center;border:1px solid #ddd;border-radius:6px;padding:3px;font-size:12px}
select.status-sel{font-size:11px;padding:4px 6px;border-radius:6px}
.b-idea{background:#fff8e1;color:#f57f17}
.b-confirmed,.b-selected{background:#e3f2fd;color:#1565c0}
.b-draft_generated,.b-generated{background:#e8f5e9;color:#2e7d32}
.b-published{background:#f3e5f5;color:#7b1fa2}
.b-paused{background:#eceff1;color:#546e7a}
form.inline{display:inline}
.row-actions{display:flex;gap:4px;flex-wrap:wrap}
.priority-tag{font-size:10px;font-weight:800;padding:1px 6px;border-radius:6px;display:inline-block}
.priority-A{background:#fdecea;color:#c62828}
.priority-B{background:#fff3e0;color:#e65100}
.priority-C{background:#eceff1;color:#546e7a}
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

    <div class="scroll-x">
    <table>
      <tr>
        <th>主題</th><th>主關鍵字</th><th>搜尋意圖</th><th>目標客群</th>
        <th>商業價值</th><th>競爭度</th><th>優先級</th><th>對應商品</th><th>建議類型</th>
        <th>SEO</th><th>GEO</th><th>成交</th><th>難度</th>
        <th>推薦原因</th><th>狀態</th><th>操作</th>
      </tr>
      {% for o in items %}
      <tr>
        <td class="topic-cell">{{ o.topic }}<div style="font-size:10px;color:#aaa;font-weight:400">{{ o.brand }} / {{ o.category }}</div></td>
        <td>{{ o.main_keyword }}</td>
        <td>{{ o.search_intent }}</td>
        <td>{{ o.target_customer }}</td>
        <td>{{ o.business_score }}</td>
        <td>{{ o.competition_score }}</td>
        <td>{% if o.priority %}<span class="priority-tag priority-{{ o.priority }}">{{ o.priority }}</span>{% endif %}</td>
        <td style="max-width:140px;font-size:11px">{{ o.related_products }}</td>
        <td>{{ o.suggested_article_type }}</td>
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
               data-main_keyword="{{ o.main_keyword }}" data-search_intent="{{ o.search_intent }}"
               data-target_audience="{{ o.target_customer }}" data-related_products="{{ o.related_products }}"
               onclick="goGenerate(this)">用此主題生成</button>
            <form class="inline" method="POST" action="/admin/seo-opportunities/{{ o.id }}/delete?key={{ key }}" onsubmit="return confirm('刪除這個主題？')">
              <button class="btn btn-sm" style="background:#dc3545" type="submit">刪除</button>
            </form>
          </div>
        </td>
      </tr>
      {% endfor %}
    </table>
    </div>
    {% if not items %}<p style="color:#999;font-size:13px;padding:14px 0">目前沒有符合篩選條件的主題，先用上面「AI產生主題池」產生一批。</p>{% endif %}
  </div>

</div>
<script>
const KEY = {{ key|tojson }};
function goGenerate(btn){
  const params = new URLSearchParams({
    key: KEY, opp_id: btn.dataset.id, brand: btn.dataset.brand,
    category: btn.dataset.category, topic: btn.dataset.topic,
    main_keyword: btn.dataset.main_keyword || '', search_intent: btn.dataset.search_intent || '',
    target_audience: btn.dataset.target_audience || '', related_products: btn.dataset.related_products || '',
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

BRAND_RULES_LIST_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>品牌SEO規則</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
""" + SIDEBAR_CSS + """
.container{max-width:1000px;margin:24px auto;padding:0 16px}
.section{background:#fff;border-radius:14px;padding:20px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #f0f0f0;vertical-align:top}
th{color:#888;font-weight:600;font-size:11px;text-transform:uppercase}
td.excerpt{max-width:260px;color:#666;font-size:12px}
.btn{padding:7px 14px;background:#0d6efd;color:#fff;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;text-decoration:none;display:inline-block}
.link{color:#0d6efd;text-decoration:none;font-weight:600;font-size:12px}
</style></head><body>
{{ shell|safe }}
<div class="container">
  <div class="section">
    <h3 style="font-size:15px;margin-bottom:12px">品牌SEO規則（{{ rules|length }}）</h3>
    <table>
      <tr><th>品牌</th><th>品類</th><th>文章類型</th><th>優先權</th><th>品牌定位</th><th>目標客群</th><th>禁止偏離方向</th><th>更新時間</th><th>操作</th></tr>
      {% for r in rules %}
      <tr>
        <td>{{ r.brand or '（全部品牌）' }}</td><td>{{ r.category or '（全部品類）' }}</td>
        <td>{{ r.article_type or '（全部類型）' }}</td><td>{{ r.priority }}</td>
        <td class="excerpt">{{ r.positioning }}</td>
        <td class="excerpt">{{ r.target_audience }}</td>
        <td class="excerpt">{{ r.avoid_directions }}</td>
        <td>{{ r.updated_at }}</td>
        <td><a class="link" href="/admin/seo-brand-rules/item/{{ r.id }}?key={{ key }}">編輯</a></td>
      </tr>
      {% endfor %}
    </table>
    <a class="btn" style="margin-top:14px" href="/admin/seo-brand-rules/item/new?key={{ key }}">+ 新增品牌規則</a>
  </div>
</div>
""" + SHELL_CLOSE + """
</body></html>"""

BRAND_RULE_ITEM_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>品牌SEO規則</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
""" + SIDEBAR_CSS + """
.container{max-width:700px;margin:24px auto;padding:0 16px 80px}
.section{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
label{font-size:12px;color:#888;font-weight:700;display:block;margin-bottom:5px;margin-top:14px}
label:first-child{margin-top:0}
input[type=text],textarea{width:100%;border:1px solid #ddd;border-radius:8px;padding:9px 11px;font-size:14px;font-family:inherit}
textarea{resize:vertical;line-height:1.6}
.btn{padding:10px 22px;background:#0d6efd;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
</style></head><body>
{{ shell|safe }}
<div class="container">
<form method="POST" action="/admin/seo-brand-rules/item/save?key={{ key }}">
  <input type="hidden" name="id" value="{{ r.id if r else '' }}">
  <div class="section">
    <label>品牌（建議用跟AI生成文章一致的品牌Key，例如 jsimple；留空＝適用所有品牌的系統預設規則）</label>
    <input type="text" name="brand" value="{{ r.brand if r else '' }}">
    <label>品類（留空＝適用該品牌所有品類，當作品牌預設規則）</label>
    <input type="text" name="category" value="{{ r.category if r else '' }}" placeholder="例如：辦公家具">
    <label>文章類型（留空＝適用所有文章類型）</label>
    <select name="article_type">
      <option value="">（全部類型）</option>
      {% for t in article_types %}<option value="{{ t }}" {{ 'selected' if r and r.article_type==t else '' }}>{{ t }}</option>{% endfor %}
    </select>
    <label>優先權（數字越大越優先比對，平分時用來決勝負；一般規則建議100，越具體的規則建議設越高，例如200、300）</label>
    <input type="text" name="priority" value="{{ r.priority if r else 100 }}">
    <label>品牌定位</label>
    <textarea name="positioning" rows="3">{{ r.positioning if r else '' }}</textarea>
    <label>目標客群</label>
    <textarea name="target_audience" rows="2">{{ r.target_audience if r else '' }}</textarea>
    <label>主打商品</label>
    <textarea name="key_products" rows="2">{{ r.key_products if r else '' }}</textarea>
    <label>禁止偏離方向</label>
    <textarea name="avoid_directions" rows="2">{{ r.avoid_directions if r else '' }}</textarea>
    <label>語氣風格</label>
    <textarea name="tone" rows="2">{{ r.tone if r else '' }}</textarea>
    <label>CTA方向</label>
    <textarea name="cta_direction" rows="2">{{ r.cta_direction if r else '' }}</textarea>
    <label>常用關鍵字</label>
    <textarea name="keywords" rows="2">{{ r.keywords if r else '' }}</textarea>
    <label>禁用關鍵字或不建議方向</label>
    <textarea name="negative_keywords" rows="2">{{ r.negative_keywords if r else '' }}</textarea>
  </div>
  <button class="btn" type="submit">儲存</button>
</form>
</div>
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
    <div class="hint">可用變數：<code>[[BRAND_NAME]]</code> <code>[[BRAND_CATEGORY]]</code> <code>[[BRAND_STYLE]]</code> <code>[[BRAND_TONE]]</code> <code>[[CATEGORY]]</code> <code>[[TOPIC]]</code> <code>[[ANALYSIS]]</code>（搜尋意圖分析結果）<code>[[KNOWLEDGE]]</code>（<a href="/admin/seo-knowledge?key={{ key }}">知識庫</a>引用資料）<code>[[BRAND_RULE]]</code>（依「自動／不套用／手動」選出的<a href="/admin/seo-brand-rules?key={{ key }}">品牌SEO規則</a>）<code>[[ARTICLE_TYPE_GUIDE]]</code>（依文章類型給的架構指引，未指定類型時會請AI自行判斷）— 結尾的JSON輸出格式請保留，否則文章會存不進去</div>
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
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
@media(max-width:640px){.grid2{grid-template-columns:1fr}}
.checkbox-row{display:flex;align-items:center;gap:8px;margin-top:14px}
.checkbox-row input{width:auto}
.checkbox-row label{margin:0}
.hint{font-size:11px;color:#999;margin-top:4px}
.radio-group{margin-top:14px;border:1px solid #eee;border-radius:8px;padding:10px 12px}
.radio-row{display:flex;align-items:flex-start;gap:8px;padding:6px 0}
.radio-row input{width:auto;margin-top:2px}
.radio-row label{margin:0;font-size:13px;color:#333;font-weight:600}
.radio-row .desc{font-size:11px;color:#999;font-weight:400;margin-top:2px}
#manual-rule-wrap{margin-top:10px;padding-left:24px}
</style></head><body>
{{ shell|safe }}
<div class="container">

  {% if not ai_key_set %}
  <div class="banner">⚠️ 尚未設定 ANTHROPIC_API_KEY，AI 分析／生成功能目前無法使用。請在 Render → Environment 加上這個環境變數後再試。</div>
  {% endif %}

  <div class="section">
    <input type="hidden" id="opp_id" value="{{ prefill_opp_id }}">
    <div class="grid2">
      <div>
        <label>品牌</label>
        <select id="brand">
          {% for b in brands %}
          <option value="{{ b.key }}" data-category="{{ b.category }}" {{ 'selected' if b.key==prefill_brand else '' }}>{{ b.name }}</option>
          {% endfor %}
        </select>
      </div>
      <div>
        <label>品類</label>
        <input type="text" id="category" value="{{ prefill_category }}" placeholder="例如：辦公家具">
      </div>
    </div>

    <label>主題</label>
    <input type="text" id="topic" value="{{ prefill_topic }}" placeholder="例如：小型辦公室家具怎麼選？">

    <div class="grid2">
      <div>
        <label>主關鍵字</label>
        <input type="text" id="main_keyword" value="{{ prefill_main_keyword }}" placeholder="例如：小型辦公室家具">
      </div>
      <div>
        <label>文章類型</label>
        <select id="article_type">
          <option value="">AI自動判斷（預設）</option>
          {% for t in article_types %}<option value="{{ t }}">{{ t }}</option>{% endfor %}
        </select>
      </div>
    </div>

    <label>搜尋意圖</label>
    <input type="text" id="search_intent" value="{{ prefill_search_intent }}" placeholder="例如：中小企業採購前想了解辦公桌椅配置方式">

    <label>目標客群</label>
    <input type="text" id="target_audience" value="{{ prefill_target_audience }}" placeholder="例如：公司採購、老闆、工作室負責人">

    <label>對應商品</label>
    <input type="text" id="related_products" value="{{ prefill_related_products }}" placeholder="例如：員工桌、主管桌、會議桌、辦公椅">

    <label>禁止偏離方向</label>
    <input type="text" id="avoid_directions" placeholder="例如：不要寫高架床、學生宿舍、租屋套房">

    <label>CTA方向</label>
    <input type="text" id="cta_direction" placeholder="例如：提供空間尺寸、人數與預算，可協助配置與報價">

    <label>品牌SEO規則</label>
    <div class="radio-group">
      <div class="radio-row">
        <input type="radio" name="brand_rule_mode" id="mode-auto" value="auto" checked>
        <div><label for="mode-auto">自動（預設）</label><div class="desc">依品牌＋品類＋文章類型自動選出最符合的SEO規則；找不到符合的就退回品牌預設規則，品牌也沒有就用系統預設規則。</div></div>
      </div>
      <div class="radio-row">
        <input type="radio" name="brand_rule_mode" id="mode-none" value="none">
        <div><label for="mode-none">不套用</label><div class="desc">完全不套用品牌SEO規則，只用搜尋意圖＋知識庫＋Prompt生成文章。</div></div>
      </div>
      <div class="radio-row">
        <input type="radio" name="brand_rule_mode" id="mode-manual" value="manual">
        <div><label for="mode-manual">手動選擇</label><div class="desc">自己指定一筆SEO規則。</div></div>
      </div>
    </div>
    <div id="manual-rule-wrap" style="display:none">
      <label>SEO規則</label>
      <select id="manual_rule_id"></select>
    </div>
    <div class="hint" id="brand-rule-hint"></div>

    <button class="btn" id="btn-analyze" onclick="doAnalyze()" {{ 'disabled' if not ai_key_set else '' }} style="margin-top:18px">AI 分析搜尋意圖</button>
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
const BRAND_RULES = {{ brand_rules_json|safe }};

// 跟後端 _match_brand_rule 同一套比分邏輯：規則欄位留空＝萬用，填了就要完全相符；
// 分數最高的勝出，平手用 priority 決定。不寫死任何品牌/品類/文章類型名稱。
function matchBrandRule(brand, category, articleType){
  let best = null, bestScore = -1;
  for (const r of BRAND_RULES) {
    if (r.brand && r.brand !== brand) continue;
    if (r.category && r.category !== category) continue;
    if (r.article_type && r.article_type !== articleType) continue;
    const score = (r.brand ? 1000 : 0) + (r.category ? 100 : 0) + (r.article_type ? 10 : 0);
    const total = score * 100000 + (r.priority || 100);
    if (total > bestScore) { bestScore = total; best = r; }
  }
  return best;
}

function ruleLabel(r){
  return (r.category || '（全部品類）') + ' / ' + (r.article_type || '全部類型') + '（優先權 ' + (r.priority || 100) + '）';
}

function currentMode(){
  return document.querySelector('input[name="brand_rule_mode"]:checked').value;
}

function fillFromRule(rule){
  if (!document.getElementById('target_audience').value) document.getElementById('target_audience').value = rule.target_audience || '';
  if (!document.getElementById('related_products').value) document.getElementById('related_products').value = rule.key_products || '';
  if (!document.getElementById('avoid_directions').value) document.getElementById('avoid_directions').value = rule.avoid_directions || '';
  if (!document.getElementById('cta_direction').value) document.getElementById('cta_direction').value = rule.cta_direction || '';
}

function refreshManualOptions(){
  const brand = document.getElementById('brand').value;
  const select = document.getElementById('manual_rule_id');
  const brandRules = BRAND_RULES.filter(r => r.brand === brand);
  select.innerHTML = '';
  if (!brandRules.length) {
    const opt = document.createElement('option');
    opt.value = ''; opt.textContent = '此品牌目前尚未建立SEO規則';
    select.appendChild(opt);
    return;
  }
  for (const r of brandRules) {
    const opt = document.createElement('option');
    opt.value = r.id; opt.textContent = ruleLabel(r);
    select.appendChild(opt);
  }
}

function applyBrandRule(){
  const mode = currentMode();
  document.getElementById('manual-rule-wrap').style.display = (mode === 'manual') ? 'block' : 'none';
  const hint = document.getElementById('brand-rule-hint');
  const brand = document.getElementById('brand').value;
  const category = document.getElementById('category').value.trim();
  const articleType = document.getElementById('article_type').value;

  if (mode === 'none') { hint.textContent = '（不套用品牌SEO規則，僅用搜尋意圖＋知識庫＋Prompt生成）'; return; }

  if (mode === 'manual') {
    refreshManualOptions();
    const select = document.getElementById('manual_rule_id');
    const rule = BRAND_RULES.find(r => String(r.id) === String(select.value));
    if (!rule) { hint.textContent = '（此品牌目前尚未建立SEO規則，將不套用品牌規則）'; return; }
    fillFromRule(rule);
    hint.textContent = '✓ 已套用所選SEO規則（' + ruleLabel(rule) + '）';
    return;
  }

  // auto
  const rule = matchBrandRule(brand, category, articleType);
  if (!rule) { hint.textContent = '（找不到任何符合的SEO規則，本篇將不套用品牌規則）'; return; }
  fillFromRule(rule);
  hint.textContent = '✓ 自動套用SEO規則（' + ruleLabel(rule) + '）';
}

document.getElementById('brand').addEventListener('change', function(){
  document.getElementById('category').value = this.selectedOptions[0].dataset.category || '';
  applyBrandRule();
});
document.getElementById('category').addEventListener('blur', applyBrandRule);
document.getElementById('article_type').addEventListener('change', applyBrandRule);
document.getElementById('manual_rule_id').addEventListener('change', applyBrandRule);
document.querySelectorAll('input[name="brand_rule_mode"]').forEach(el => el.addEventListener('change', applyBrandRule));
if (document.getElementById('brand').options.length && !document.getElementById('category').value) {
  document.getElementById('category').value = document.getElementById('brand').selectedOptions[0].dataset.category || '';
}
applyBrandRule();

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
        opp_id: document.getElementById('opp_id').value,
        main_keyword: document.getElementById('main_keyword').value,
        search_intent: document.getElementById('search_intent').value,
        target_audience: document.getElementById('target_audience').value,
        related_products: document.getElementById('related_products').value,
        avoid_directions: document.getElementById('avoid_directions').value,
        cta_direction: document.getElementById('cta_direction').value,
        article_type: document.getElementById('article_type').value,
        brand_rule_mode: currentMode(),
        manual_rule_id: document.getElementById('manual_rule_id').value,
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
    rows = _q("SELECT id,title,status,slug,updated_at,extra FROM seo_articles ORDER BY id DESC", fetch="all") or []
    articles = []
    for r in rows:
        extra = _parse_extra(r[5])
        articles.append({
            "id": r[0], "title": r[1], "status": r[2], "slug": r[3],
            "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[4])) if r[4] else "",
            "main_keyword": extra.get("main_keyword", ""),
            "ai_score": extra.get("ai_score", 0),
            "related_products": extra.get("related_products", ""),
            "next_action": extra.get("next_action", ""),
        })
    shell = _shell_open(key, "seo", [("文章管理", None)])
    return render_template_string(LIST_HTML, key=key, shell=shell, titles=titles, articles=articles,
        title_status=TITLE_STATUS, article_status_labels=ARTICLE_STATUS_LABELS)

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
    return render_template_string(ARTICLE_HTML, key=key, shell=shell, a=None, extra={}, default_title=default_title,
        article_status=ARTICLE_STATUS, article_status_labels=ARTICLE_STATUS_LABELS, next_action_options=NEXT_ACTION_OPTIONS)

@seo_bp.route("/admin/seo/article/<int:aid>")
def seo_article_edit(aid):
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    a = _q("""SELECT id,title,slug,meta_title,meta_description,content,ai_summary,status,extra
              FROM seo_articles WHERE id=%s""", (aid,), fetch="one")
    if not a:
        abort(404)
    extra = _parse_extra(a[8])
    shell = _shell_open(key, "seo", [("文章管理", "/admin/seo"), ("編輯文章", None)])
    return render_template_string(ARTICLE_HTML, key=key, shell=shell, a=a, extra=extra, default_title="",
        article_status=ARTICLE_STATUS, article_status_labels=ARTICLE_STATUS_LABELS, next_action_options=NEXT_ACTION_OPTIONS)

@seo_bp.route("/admin/seo/article/save", methods=["POST"])
def seo_article_save():
    ok, key = check_auth()
    if not ok:
        abort(403)
    f = request.form
    aid = f.get("id", "")
    now = time.time()
    extra_patch = {"main_keyword": f.get("main_keyword", ""), "target_audience": f.get("target_audience", ""),
                   "related_products": f.get("related_products", ""), "next_action": f.get("next_action", "")}
    if aid:
        existing = _q("SELECT extra FROM seo_articles WHERE id=%s", (aid,), fetch="one")
        extra = _parse_extra(existing[0] if existing else None)
        extra.update(extra_patch)
        published_at_sql = ", published_at=CASE WHEN status!='published' AND %s='published' THEN %s ELSE published_at END"
        _q(f"""UPDATE seo_articles SET title=%s, slug=%s, meta_title=%s, meta_description=%s,
               content=%s, ai_summary=%s, status=%s, extra=%s, updated_at=%s {published_at_sql}
               WHERE id=%s""",
           (f.get("title",""), f.get("slug",""), f.get("meta_title",""), f.get("meta_description",""),
            f.get("content",""), f.get("ai_summary",""), f.get("status","draft_review"), _dump_extra(extra), now,
            f.get("status","draft_review"), now, aid))
        new_id = aid
    else:
        new_id = _q("""INSERT INTO seo_articles
               (title,slug,meta_title,meta_description,content,ai_summary,status,extra,created_at,updated_at,published_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
           (f.get("title",""), f.get("slug",""), f.get("meta_title",""), f.get("meta_description",""),
            f.get("content",""), f.get("ai_summary",""), f.get("status","draft_review"), _dump_extra(extra_patch), now, now,
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

@seo_bp.route("/admin/seo/article/<int:aid>/quality-check", methods=["POST"])
def seo_article_quality_check(aid):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "尚未設定 ANTHROPIC_API_KEY，請在 Render → Environment 加上這個環境變數才能使用AI功能"}), 200
    now = time.time()
    job_id = _q("INSERT INTO seo_quality_check_jobs (status,article_id,created_at,updated_at) VALUES (%s,%s,%s,%s) RETURNING id",
                ("pending", aid, now, now), fetch="id")
    threading.Thread(target=_run_quality_check_job, args=(job_id, aid), daemon=True).start()
    return jsonify({"job_id": job_id})

@seo_bp.route("/admin/seo/article/quality-check/status/<int:job_id>")
def seo_article_quality_check_status(job_id):
    ok, _ = auth_required()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    row = _q("SELECT status,result,error_msg FROM seo_quality_check_jobs WHERE id=%s", (job_id,), fetch="one")
    if not row:
        return jsonify({"status": "error", "error": "找不到這個檢查任務"})
    status, result, error_msg = row
    out = {"status": status}
    if status == "error":
        out["error"] = error_msg
    elif status == "done":
        out["result"] = _parse_extra(result)
    return jsonify(out)

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
        brand_rule = _match_brand_rule(brand_key, category)
        prompt = _opportunity_prompt(brand, category, knowledge_items, brand_rule)
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
               (_safe_job_error_msg(e), time.time(), job_id))
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
               (_safe_job_error_msg(e), time.time(), job_id))
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
        import sys; print(f"[SEO Knowledge Import Confirm Error] {e}", file=sys.stderr)
        return jsonify({"error": f"寫入失敗：{_safe_job_error_msg(e)}"}), 200
    return jsonify({"inserted": inserted, "updated": updated})

# ── 品牌SEO規則 ─────────────────────────────────────────────────

@seo_bp.route("/admin/seo-brand-rules")
def seo_brand_rules_page():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    rules = _list_brand_rules()
    shell = _shell_open(key, "seo-brand-rules", [("品牌SEO規則", None)])
    return render_template_string(BRAND_RULES_LIST_HTML, key=key, shell=shell, rules=rules)

@seo_bp.route("/admin/seo-brand-rules/item/new")
def seo_brand_rule_new():
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    shell = _shell_open(key, "seo-brand-rules", [("品牌SEO規則", "/admin/seo-brand-rules"), ("新增規則", None)])
    return render_template_string(BRAND_RULE_ITEM_HTML, key=key, shell=shell, r=None, article_types=ARTICLE_TYPES)

@seo_bp.route("/admin/seo-brand-rules/item/<int:rule_id>")
def seo_brand_rule_edit(rule_id):
    ok, key = check_auth()
    if not ok:
        return render_template_string(LOGIN_HTML, error=None)
    row = _q("""SELECT id,brand,category,article_type,priority,positioning,target_audience,key_products,
                avoid_directions,tone,cta_direction,keywords,negative_keywords FROM seo_brand_rules WHERE id=%s""",
              (rule_id,), fetch="one")
    if not row:
        abort(404)
    r = {"id": row[0], "brand": row[1], "category": row[2], "article_type": row[3], "priority": row[4],
         "positioning": row[5], "target_audience": row[6], "key_products": row[7], "avoid_directions": row[8],
         "tone": row[9], "cta_direction": row[10], "keywords": row[11], "negative_keywords": row[12]}
    shell = _shell_open(key, "seo-brand-rules", [("品牌SEO規則", "/admin/seo-brand-rules"), ("編輯規則", None)])
    return render_template_string(BRAND_RULE_ITEM_HTML, key=key, shell=shell, r=r, article_types=ARTICLE_TYPES)

@seo_bp.route("/admin/seo-brand-rules/item/save", methods=["POST"])
def seo_brand_rule_save():
    ok, key = check_auth()
    if not ok:
        abort(403)
    _save_brand_rule(request.form)
    return redirect(f"/admin/seo-brand-rules?key={key}")

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
    try:
        rules = _list_brand_rules()
    except Exception:
        rules = []
    shell = _shell_open(key, "seo-generator", [("AI 生成文章", None)])
    return render_template_string(GENERATOR_HTML, key=key, shell=shell, brands=brands, ai_key_set=bool(ANTHROPIC_API_KEY),
        article_types=ARTICLE_TYPES, brand_rules_json=json.dumps(rules, ensure_ascii=False),
        prefill_brand=request.args.get("brand", ""), prefill_category=request.args.get("category", ""),
        prefill_topic=request.args.get("topic", ""), prefill_opp_id=request.args.get("opp_id", ""),
        prefill_main_keyword=request.args.get("main_keyword", ""),
        prefill_search_intent=request.args.get("search_intent", ""),
        prefill_target_audience=request.args.get("target_audience", ""),
        prefill_related_products=request.args.get("related_products", ""))

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

def _run_generate_job(job_id, brand_key, category, topic, analysis, opp_id=None, fields=None):
    fields = fields or {}
    try:
        _q("UPDATE seo_generate_jobs SET status='running', updated_at=%s WHERE id=%s", (time.time(), job_id))
        brand = _get_brand(brand_key)
        knowledge_items = _get_knowledge_for_prompt(brand_key, category, limit=10)
        brand_rule_mode = fields.get("brand_rule_mode", "auto")
        if brand_rule_mode == "none":
            brand_rule = {}
        elif brand_rule_mode == "manual":
            brand_rule = _get_brand_rule_by_id(fields.get("manual_rule_id"))
        else:
            brand_rule_mode = "auto"
            brand_rule = _match_brand_rule(brand_key, category, fields.get("article_type", ""))
        prompt = _generate_article_prompt(brand, category, topic, analysis, knowledge_items, fields, brand_rule)
        result, err = _ai_call_json(prompt, model="claude-sonnet-4-6", max_tokens=8000)
        if err:
            _q("UPDATE seo_generate_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s",
               (f"AI生成失敗：{err}", time.time(), job_id))
            return
        now = time.time()
        extra = _dump_extra({
            "main_keyword": fields.get("main_keyword", ""),
            "search_intent": fields.get("search_intent", "") or analysis[:200],
            "target_audience": fields.get("target_audience", ""),
            "related_products": fields.get("related_products", ""),
            "article_type": fields.get("article_type", ""),
            "internal_links": result.get("internal_links", ""),
            "long_tail_keywords": result.get("long_tail_keywords", ""),
            "ai_score": 0,
            "quality_check": {},
            "brand_rule_mode": brand_rule_mode,
            "brand_rule_applied": bool(brand_rule),
            "brand_rule_id": brand_rule.get("id") if brand_rule else None,
            "next_action": "AI檢查",
        })
        new_id = _q("""INSERT INTO seo_articles
               (title,slug,meta_title,meta_description,content,ai_summary,status,
                brand_key,category,extra,created_at,updated_at,published_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
           (result.get("title",""), result.get("slug",""), result.get("meta_title",""),
            result.get("meta_description",""), result.get("content",""), result.get("ai_summary",""),
            "draft_review", brand_key, category, extra, now, now, 0), fetch="id")
        _q("""UPDATE seo_generate_jobs SET status='done', article_id=%s, updated_at=%s WHERE id=%s""",
           (new_id, time.time(), job_id))
        if opp_id:
            try:
                _q("UPDATE seo_opportunities SET status='draft_generated', updated_at=%s WHERE id=%s", (now, int(opp_id)))
            except Exception as e:
                import sys; print(f"[SEO Opportunity] 更新狀態失敗：{e}", file=sys.stderr)
    except Exception as e:
        import sys; print(f"[SEO Generate Job Error] {e}", file=sys.stderr)
        try:
            _q("UPDATE seo_generate_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s",
               (_safe_job_error_msg(e), time.time(), job_id))
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
    fields = {
        "main_keyword": data.get("main_keyword", ""),
        "search_intent": data.get("search_intent", ""),
        "target_audience": data.get("target_audience", ""),
        "related_products": data.get("related_products", ""),
        "avoid_directions": data.get("avoid_directions", ""),
        "cta_direction": data.get("cta_direction", ""),
        "article_type": data.get("article_type", ""),
        "brand_rule_mode": data.get("brand_rule_mode") if data.get("brand_rule_mode") in ("auto", "none", "manual") else "auto",
        "manual_rule_id": data.get("manual_rule_id", ""),
    }
    if not topic.strip():
        return jsonify({"error": "請輸入主題"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "尚未設定 ANTHROPIC_API_KEY，請在 Render → Environment 加上這個環境變數才能使用AI功能"}), 200
    now = time.time()
    job_id = _q("INSERT INTO seo_generate_jobs (status,created_at,updated_at) VALUES (%s,%s,%s) RETURNING id",
                ("pending", now, now), fetch="id")
    threading.Thread(target=_run_generate_job, args=(job_id, brand_key, category, topic, analysis, opp_id, fields), daemon=True).start()
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

def _today_tasks(brand_key="", category=""):
    """產生「今日SEO任務」清單。邏輯先簡化成5條固定規則，純Python判斷，不用AI，避免每次開儀表板都耗費API成本。"""
    tasks = []
    a_where = []
    a_params = []
    if brand_key:
        a_where.append("brand_key=%s"); a_params.append(brand_key)
    if category:
        a_where.append("category=%s"); a_params.append(category)
    a_where_sql = (" AND " + " AND ".join(a_where)) if a_where else ""
    try:
        # 1. 草稿待審 → 審核草稿
        rows = _q(f"""SELECT id,title,brand_key,category FROM seo_articles
                      WHERE status IN ('draft_review','draft'){a_where_sql}
                      ORDER BY updated_at DESC LIMIT 5""", tuple(a_params), fetch="all") or []
        for r in rows:
            tasks.append({"priority": "中", "name": f"審核草稿：{r[1]}", "reason": "文章已生成，等待人工審稿",
                          "action": "審核草稿", "brand": r[2], "category": r[3], "link": f"/admin/seo/article/{r[0]}"})

        # 2. 待產出主題 → 生成文章
        t_where = ["status='待寫'"]
        if category:
            t_where.append("topic=%s")
        rows2 = _q(f"SELECT id,topic,title FROM seo_titles WHERE {' AND '.join(t_where)} ORDER BY id DESC LIMIT 5",
                   (category,) if category else (), fetch="all") or []
        for r in rows2:
            tasks.append({"priority": "低", "name": f"生成文章：{r[2]}", "reason": "標題庫已建立，尚未產生草稿",
                          "action": "生成文章", "brand": brand_key, "category": r[1] or category,
                          "link": f"/admin/seo-generator?topic={r[2]}&category={r[1] or category}&brand={brand_key}"})

        # 3. 已發布但曝光為0 → 檢查收錄或等待數據
        rows3 = _q(f"""SELECT a.id,a.title,a.brand_key,a.category FROM seo_articles a
                      LEFT JOIN (
                        SELECT t1.* FROM seo_tracking t1
                        INNER JOIN (SELECT article_id, MAX(id) AS max_id FROM seo_tracking GROUP BY article_id) t2
                          ON t1.article_id = t2.article_id AND t1.id = t2.max_id
                      ) t ON t.article_id = a.id
                      WHERE a.status='published' AND (t.impressions IS NULL OR t.impressions=0){a_where_sql.replace('brand_key','a.brand_key').replace('category','a.category')}
                      ORDER BY a.updated_at DESC LIMIT 5""", tuple(a_params), fetch="all") or []
        for r in rows3:
            tasks.append({"priority": "中", "name": f"檢查收錄：{r[1]}", "reason": "已發布但目前曝光為0，可能尚未被收錄",
                          "action": "檢查收錄／等待數據", "brand": r[2], "category": r[3], "link": f"/admin/seo/article/{r[0]}"})

        # 4. CTR < 2% → 優化標題與Meta
        rows4 = _q(f"""SELECT a.id,a.title,a.brand_key,a.category,t.clicks,t.impressions FROM seo_articles a
                      INNER JOIN (
                        SELECT t1.* FROM seo_tracking t1
                        INNER JOIN (SELECT article_id, MAX(id) AS max_id FROM seo_tracking GROUP BY article_id) t2
                          ON t1.article_id = t2.article_id AND t1.id = t2.max_id
                      ) t ON t.article_id = a.id
                      WHERE a.status='published' AND t.impressions>0{a_where_sql.replace('brand_key','a.brand_key').replace('category','a.category')}
                      ORDER BY a.updated_at DESC LIMIT 30""", tuple(a_params), fetch="all") or []
        count = 0
        for r in rows4:
            ctr = (r[4] or 0) / r[5] * 100 if r[5] else 0
            if ctr < 2:
                tasks.append({"priority": "高", "name": f"優化標題與Meta：{r[1]}", "reason": f"CTR僅{round(ctr,2)}%，低於2%門檻",
                              "action": "優化標題／Meta", "brand": r[2], "category": r[3], "link": f"/admin/seo/article/{r[0]}"})
                count += 1
                if count >= 5:
                    break

        # 5. AI評分 < 70 → 重新修改文章
        rows5 = _q(f"SELECT id,title,brand_key,category,extra FROM seo_articles WHERE extra IS NOT NULL{a_where_sql} ORDER BY updated_at DESC LIMIT 30",
                   tuple(a_params), fetch="all") or []
        count = 0
        for r in rows5:
            score = _parse_extra(r[4]).get("ai_score", 0)
            if score and score < 70:
                tasks.append({"priority": "高", "name": f"重新修改文章：{r[1]}", "reason": f"AI評分{score}分，低於70分門檻",
                              "action": "修改內容", "brand": r[2], "category": r[3], "link": f"/admin/seo/article/{r[0]}"})
                count += 1
                if count >= 5:
                    break
    except Exception as e:
        import sys; print(f"[SEO Today Tasks] {e}", file=sys.stderr)

    order = {"高": 0, "中": 1, "低": 2}
    tasks.sort(key=lambda t: order.get(t["priority"], 3))
    return tasks

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
.priority-tag{font-size:11px;font-weight:800;padding:2px 8px;border-radius:6px;display:inline-block}
.priority-A{background:#fdecea;color:#c62828}
.priority-B{background:#fff3e0;color:#e65100}
.priority-C{background:#eceff1;color:#546e7a}
.grid2col{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
@media(max-width:800px){.grid2col{grid-template-columns:1fr}}
.mini-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:13px}
.mini-row:last-child{border-bottom:none}
.task-link{color:#0d6efd;text-decoration:none;font-weight:600;font-size:12px;white-space:nowrap}
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
    <h3>📋 今日 SEO 任務（{{ today_tasks|length }}）</h3>
    {% if today_tasks %}
    <table>
      <tr><th>優先級</th><th>任務名稱</th><th>原因</th><th>建議動作</th><th>品牌</th><th>類別</th><th>操作</th></tr>
      {% for t in today_tasks %}
      <tr>
        <td><span class="priority-tag priority-{{ {'高':'A','中':'B','低':'C'}[t.priority] }}">{{ t.priority }}</span></td>
        <td style="font-weight:600">{{ t.name }}</td>
        <td style="color:#888;font-size:12px">{{ t.reason }}</td>
        <td>{{ t.action }}</td>
        <td>{{ t.brand }}</td><td>{{ t.category }}</td>
        <td><a class="task-link" href="{{ t.link }}?key={{ key }}">前往處理 →</a></td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <p style="color:#999;font-size:13px;padding:10px 0">目前沒有待辦任務，文章與數據都還算正常 🎉</p>
    {% endif %}
  </div>

  <div class="grid2col">
    <div class="section">
      <h3>📝 待審草稿（{{ draft_articles|length }}）</h3>
      {% for d in draft_articles %}
      <div class="mini-row">
        <div><b>{{ d.title }}</b><div style="font-size:11px;color:#999">{{ d.brand }} / {{ d.category }} · {{ d.updated_at }}</div></div>
        <a class="task-link" href="/admin/seo/article/{{ d.id }}?key={{ key }}">審核 →</a>
      </div>
      {% else %}
      <p style="color:#999;font-size:13px">目前沒有待審草稿。</p>
      {% endfor %}
    </div>
    <div class="section">
      <h3>🎯 高優先主題機會（{{ top_opps|length }}）</h3>
      {% for o in top_opps %}
      <div class="mini-row">
        <div><b>{{ o.topic }}</b><div style="font-size:11px;color:#999">{{ o.brand }} / {{ o.category }} · 商業價值{{ o.business_score }}</div></div>
        <a class="task-link" href="/admin/seo-opportunities?key={{ key }}">查看 →</a>
      </div>
      {% else %}
      <p style="color:#999;font-size:13px">目前沒有A級優先主題，去主題機會池產生一批。</p>
      {% endfor %}
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
    try:
        today_tasks = _today_tasks(brand_key, category)
    except Exception as e:
        import sys; print(f"[SEO Dashboard] 今日任務計算失敗：{e}", file=sys.stderr)
        today_tasks = []
    try:
        draft_articles = _q(f"""SELECT id,title,brand_key,category,updated_at FROM seo_articles
                                 WHERE status IN ('draft_review','draft')
                                 {"AND brand_key=%s" if brand_key else ""} {"AND category=%s" if category else ""}
                                 ORDER BY updated_at DESC LIMIT 8""",
                             tuple(p for p in [brand_key, category] if p), fetch="all") or []
        draft_articles = [{"id": r[0], "title": r[1], "brand": r[2], "category": r[3],
                            "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[4])) if r[4] else ""} for r in draft_articles]
    except Exception as e:
        import sys; print(f"[SEO Dashboard] 待審草稿讀取失敗：{e}", file=sys.stderr)
        draft_articles = []
    try:
        top_opps = _list_opportunities(brand_key, category)
        top_opps = [o for o in top_opps if o.get("priority") == "A" and o["status"] in ("idea", "confirmed")][:8]
    except Exception as e:
        import sys; print(f"[SEO Dashboard] 高優先主題讀取失敗：{e}", file=sys.stderr)
        top_opps = []
    suggestion, gen_at = _get_ai_suggestion()
    shell = _shell_open(key, "seo-dashboard", [("SEO 營運中心", None)])
    return render_template_string(DASHBOARD_HTML, key=key, shell=shell, items=items,
        brand_key=brand_key, category=category, brands=brands, categories=categories, stats=stats,
        today_tasks=today_tasks, draft_articles=draft_articles, top_opps=top_opps,
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
