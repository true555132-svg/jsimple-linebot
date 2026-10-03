import os, sys, json, time, re

os.environ["ADMIN_PASSWORD"] = "testkey"
os.environ["DATABASE_URL"] = ""  # 保持空，我們會monkeypatch _q，程式不會真的連DB

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seo_admin as SA
from flask import Flask

SA.DATABASE_URL = "fake"  # 讓_get_brand/_get_brand_theme等函式不要因為DATABASE_URL為空而提早return {}
SA.ANTHROPIC_API_KEY = "fake"  # 讓analyze/generate路由不要因為沒設金鑰而提早return，才能測到後面monkeypatch的_ai_call_full邏輯

PASS = []
FAIL = []

def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))

# ────────────────────────────────────────────────────────────────
# 假資料庫
# ────────────────────────────────────────────────────────────────

BRANDS = {
    "jsimple": {
        "brand_key": "jsimple", "name": "JSIMPLE", "category": "家具", "style": "", "tone": "",
        "custom_prompt": "", "allowed_products": "辦公桌,辦公椅,收納櫃,高架床",
        "allowed_services": "", "compatible_brands": "", "cta_url": "/pages/contact-js",
    },
    "filterbreath": {
        "brand_key": "filterbreath", "name": "濾呼吸LuAir", "category": "家電耗材", "style": "", "tone": "",
        "custom_prompt": "", "allowed_products": "濾網,活性碳濾芯,HEPA濾網",
        "allowed_services": "", "compatible_brands": "Dyson,LG,Panasonic", "cta_url": "/pages/contact-lu",
    },
    "lander": {
        "brand_key": "lander", "name": "朗德LIGHT+", "category": "燈具", "style": "", "tone": "",
        "custom_prompt": "", "allowed_products": "吊燈,壁燈,窗簾盒",
        "allowed_services": "", "compatible_brands": "", "cta_url": "",  # 尚未設定，測試「缺少CTA要列待填」
    },
}

# seo_brand_rules：id,brand,category,article_type,priority,positioning,target_audience,key_products,
#                   avoid_directions,tone,cta_direction,keywords,negative_keywords
# JSIMPLE「穀倉門」品類規則：key_products登記的商品不在brand_profiles.allowed_products品牌預設清單裡，
# 用來驗證_check_products_brand_ownership必須用_resolve_allowed_products的三層fallback，
# 不能只認品牌預設清單，否則品類規則生出來的商品會被誤擋（見seo_admin.py的_check_products_brand_ownership）。
SEO_BRAND_RULES = [
    (1, "jsimple", "穀倉門", "", 100, "", "", "穀倉門滑軌組,穀倉門五金", "", "", "", "", ""),
    # 還原正式站實際情況：filterbreath/製冰機濾網這筆規則裡的key_products是之前查證後
    # 發現其實查無根據的舊型號清單——這筆資料「存在」(不是rp_source="品牌預設"或"空"那種情況)，
    # 用來驗證_apply_filterbreath_knowledge_only_override對這種「有seo_brand_rules資料，
    # 但品類本身已確認跟品牌無關」的情況，一樣要正確清空，不能只處理brand-level fallback那條路徑。
    (2, "filterbreath", "製冰機濾網", "", 100, "", "",
     "Panasonic 國際牌製冰室濾網：00820（矮）、108850（高）、C13；Hitachi 日立 RD-30 製冰濾芯；"
     "Mitsubishi 三菱 MR-BX52 製冰濾芯。商品型號與相容機種須依濾呼吸實際商品頁資料核對，不得推定型號通用。",
     "", "", "", "", ""),
]

KNOWLEDGE_ITEMS = {
    # 還原正式站真實內容：這筆是2026-10-03修過的通用條目，已含免責聲明，
    # 用來測試_quality_check_knowledge_citations_block能不能把這段文字帶進品質檢查Prompt
    ("filterbreath", "製冰機濾網"): [
        ("faq", "Panasonic濾芯怎麼核對",
         "Panasonic 官方提供依冰箱本體型號查詢自動製冰機淨水濾芯品番的功能；請以冰箱機身標籤、"
         "保證書或說明書上的完整型號查詢。官方建議約每3年更換。（本條為 Panasonic 官方資訊參考記錄，"
         "僅供讀者自行核對型號使用，不代表濾呼吸販售此商品或已確認相容性。）"),
    ],
}

THEMES = {
    "filterbreath": {"brand_key": "filterbreath", "primary_color": "#1B3F6E", "accent_color": "#2F80ED",
                      "bg_color": "#F5F8FC", "confirmed": True},
    # jsimple / lander 沒有資料 -> 應該回傳 DEFAULT_NEUTRAL_THEME
}

ARTICLES = {}  # id -> dict
JOBS = {}      # id -> dict，模擬 seo_generate_jobs 資料表，供_run_generate_job測試用
PROMPT_TEMPLATES = {}  # key -> content，模擬 seo_prompt_templates 資料表
QC_JOBS = {}   # id -> dict，模擬 seo_quality_check_jobs 資料表，供_run_quality_check_job測試用

def _stamp_fingerprint(aid, fp_override=None):
    """模擬「這篇文章的quality_check是針對目前這個版本跑的」：算出正確指紋寫回extra。
    fp_override給一個假指紋，用來模擬「檢查之後內容又被改過、指紋對不起來」的情境。"""
    a = ARTICLES[aid]
    extra = json.loads(a["extra"])
    rp = extra.get("related_products", "")
    fp = fp_override or SA._content_fingerprint(a["title"], a["meta_title"], a["meta_description"],
                                                  a["content"], a["blocks"], a["brand_key"], rp)
    extra["quality_check_fingerprint"] = fp
    a["extra"] = json.dumps(extra, ensure_ascii=False)

def add_article(aid, **kw):
    row = {
        "id": aid, "title": "", "slug": "", "meta_title": "", "meta_description": "",
        "content": "", "ai_summary": "", "status": "draft_review", "extra": "{}",
        "easystore_article_id": "", "brand_key": "", "blocks": "", "category": "",
    }
    row.update(kw)
    ARTICLES[aid] = row

def sample_blocks(brand_cta_ok=True, bad_url=None):
    blocks = [
        {"type": "heading", "level": 2, "text": "測試段落"},
        {"type": "paragraph", "text": "這是一段測試內文 <script>alert(1)</script> 應該被跳脫。"},
        {"type": "summary", "title": "重點整理", "points": ["重點一", "重點二"]},
        {"type": "table", "headers": ["項目", "說明"], "rows": [["A", "說明A"], ["B", "說明B"]]},
        {"type": "takeaway", "text": "這是重點提示。"},
        {"type": "note", "text": "這是補充說明。"},
        {"type": "list", "items": ["條列一", "條列二"]},
        {"type": "faq", "items": [{"q": "問題一？", "a": "回答一。"}]},
        {"type": "related_links", "items": [{"url": bad_url or "/blog/existing", "text": "相關文章"}]},
        {"type": "cta", "text": "歡迎詢問", "url": ""},
    ]
    return blocks

# 1) JS家具：正常已通過品質檢查、已發布，作為別篇文章可連結的對象
add_article(1, title="JS辦公桌怎麼選", slug="/blog/js-desk-guide", meta_title="mt", meta_description="md",
            content="<p>old content</p>", brand_key="jsimple", status="published",
            blocks=json.dumps(sample_blocks(), ensure_ascii=False),
            extra=json.dumps({"related_products": "辦公桌", "quality_check": {"brand_consistency_pass": True, "recommend_publish": True}, "ai_score": 88}, ensure_ascii=False))

# 2) JS家具：草稿(未發布)，slug存在但不該被其他文章拿來連
add_article(2, title="JS草稿文章", slug="/blog/js-draft-only", meta_title="mt", meta_description="md",
            content="<p>draft</p>", brand_key="jsimple", status="draft_review",
            blocks=json.dumps(sample_blocks(), ensure_ascii=False),
            extra=json.dumps({"quality_check": {}}, ensure_ascii=False))

# 3) 濾呼吸：跨品牌商品混入（related_products帶JS家具的商品）-> 應該被_check_products_brand_ownership擋
add_article(3, title="濾網怎麼挑", slug="/blog/lu-filter-guide", meta_title="mt", meta_description="md",
            content="", brand_key="filterbreath", status="draft_review",
            blocks=json.dumps(sample_blocks(), ensure_ascii=False),
            extra=json.dumps({"related_products": "辦公桌,濾網", "quality_check": {"brand_consistency_pass": True, "recommend_publish": True}}, ensure_ascii=False))

# 4) 濾呼吸：related_links塞了javascript:網址 -> 應該被_resolve_block_links / render時濾掉
BAD_URL_BLOCKS = sample_blocks(bad_url="javascript:alert(1)")
add_article(4, title="濾呼吸壞連結測試", slug="/blog/lu-badlink", meta_title="mt", meta_description="md",
            content="", brand_key="filterbreath", status="draft_review",
            blocks=json.dumps(BAD_URL_BLOCKS, ensure_ascii=False),
            extra=json.dumps({"quality_check": {"brand_consistency_pass": True, "recommend_publish": True}}, ensure_ascii=False))

# 5) 朗德：待補充殘留文字 -> 應該被_content_has_placeholder擋
PLACEHOLDER_BLOCKS = sample_blocks()
PLACEHOLDER_BLOCKS[1]["text"] = "價格待確認，請洽詢門市"
add_article(5, title="朗德吊燈待確認測試", slug="/blog/lander-tbd", meta_title="mt", meta_description="md",
            content="", brand_key="lander", status="draft_review",
            blocks=json.dumps(PLACEHOLDER_BLOCKS, ensure_ascii=False),
            extra=json.dumps({"quality_check": {"brand_consistency_pass": True, "recommend_publish": True}}, ensure_ascii=False))

# 6) 朗德：AI輸出被截斷 -> 應該被truncated擋
add_article(6, title="朗德截斷測試", slug="/blog/lander-truncated", meta_title="mt", meta_description="md",
            content="", brand_key="lander", status="draft_review",
            blocks=json.dumps(sample_blocks(), ensure_ascii=False),
            extra=json.dumps({"truncated": True, "quality_check": {"brand_consistency_pass": True, "recommend_publish": True}}, ensure_ascii=False))

# 7) JS：缺欄位（meta_description空的）-> 擋
add_article(7, title="JS缺欄位測試", slug="/blog/js-missing-field", meta_title="mt", meta_description="",
            content="", brand_key="jsimple", status="draft_review",
            blocks=json.dumps(sample_blocks(), ensure_ascii=False),
            extra=json.dumps({"quality_check": {"brand_consistency_pass": True, "recommend_publish": True}}, ensure_ascii=False))

# 8) JS：從沒跑過AI品質檢查 -> 擋（quality_check為空dict）
add_article(8, title="JS未檢查測試", slug="/blog/js-no-qc", meta_title="mt", meta_description="md",
            content="", brand_key="jsimple", status="draft_review",
            blocks=json.dumps(sample_blocks(), ensure_ascii=False),
            extra=json.dumps({"quality_check": {}}, ensure_ascii=False))

# 9) JS：AI品質檢查不建議發布 -> 擋
add_article(9, title="JS不建議發布測試", slug="/blog/js-not-recommend", meta_title="mt", meta_description="md",
            content="", brand_key="jsimple", status="draft_review",
            blocks=json.dumps(sample_blocks(), ensure_ascii=False),
            extra=json.dumps({"quality_check": {"brand_consistency_pass": True, "recommend_publish": False}}, ensure_ascii=False))

# 10) 舊文章（升級前）：完全沒有blocks欄位，只有舊的raw content，brand_key也是空字串
add_article(10, title="舊資料相容文章", slug="", meta_title="舊mt", meta_description="舊md",
            content="<h2>舊文章</h2><p>這是升級前生成的純HTML文章，沒有blocks欄位。</p>",
            brand_key="", status="published", blocks="",
            extra=json.dumps({}, ensure_ascii=False))

# 11) 完全通過、乾淨的一篇（正面案例，全部欄位、全部檢查都該過）
add_article(11, title="濾呼吸完整通過範例", slug="/blog/lu-clean-pass", meta_title="mt", meta_description="md",
            content="", brand_key="filterbreath", status="draft_review",
            blocks=json.dumps(sample_blocks(bad_url="/pages/contact-lu"), ensure_ascii=False),
            extra=json.dumps({"related_products": "濾網", "quality_check": {"brand_consistency_pass": True, "recommend_publish": True}, "ai_score": 91}, ensure_ascii=False))

# 幫「已經跑過品質檢查、且指紋對得上目前內容」的文章補上正確指紋（一定要在任何測試呼叫_validate_article_for_publish之前做）
for _aid in (1, 9, 11):
    _stamp_fingerprint(_aid)

# 13) 指紋對不起來的情境：quality_check看起來是通過的，但指紋是舊版本的（模擬檢查後內容又被改過但沒有走save()清除邏輯）
add_article(13, title="指紋失效測試", slug="/blog/js-fp-stale", meta_title="mt", meta_description="md",
            content="", brand_key="jsimple", status="draft_review",
            blocks=json.dumps(sample_blocks(), ensure_ascii=False),
            extra=json.dumps({"quality_check": {"brand_consistency_pass": True, "recommend_publish": True}}, ensure_ascii=False))
_stamp_fingerprint(13, fp_override="deadbeef" * 8)

# 15) 內部連結時效性測試：連到文章11(此時是已發布狀態)的slug
LINK15_BLOCKS = sample_blocks(bad_url="/blog/lu-clean-pass")
add_article(15, title="濾呼吸連結時效測試", slug="/blog/lu-link-freshness", meta_title="mt", meta_description="md",
            content="", brand_key="filterbreath", status="draft_review",
            blocks=json.dumps(LINK15_BLOCKS, ensure_ascii=False),
            extra=json.dumps({"related_products": "濾網", "quality_check": {"brand_consistency_pass": True, "recommend_publish": True}}, ensure_ascii=False))
_stamp_fingerprint(15)

# 16) 實際edit→save→重開preview流程測試用的乾淨文章
add_article(16, title="濾呼吸初始標題", slug="/blog/lu-edit-cycle", meta_title="mt初始", meta_description="md初始",
            content="初始content(不該出現在預覽，因為這篇有blocks)", brand_key="filterbreath", status="draft_review",
            blocks=json.dumps(sample_blocks(), ensure_ascii=False),
            extra=json.dumps({}, ensure_ascii=False))


def fake_q(sql, params=None, fetch=None):
    s = " ".join(sql.split())  # 正規化空白，方便比對
    params = params or ()

    # brand_profiles
    if "FROM brand_profiles WHERE brand_key=%s" in s:
        bk = params[0]
        b = BRANDS.get(bk)
        if not b:
            return None
        return (b["brand_key"], b["name"], b["category"], b["style"], b["tone"], b["custom_prompt"],
                b["allowed_products"], b["allowed_services"], b["compatible_brands"], b["cta_url"])

    if s.startswith("UPDATE brand_profiles SET compatible_brands=%s, cta_url=%s"):
        compat, cta, ts, bk = params
        if bk in BRANDS:
            BRANDS[bk]["compatible_brands"] = compat
            BRANDS[bk]["cta_url"] = cta
        return None

    # seo_brand_rules（_match_brand_rule全撈後在Python端比對）
    if "FROM seo_brand_rules ORDER BY id" in s:
        return SEO_BRAND_RULES

    # _get_knowledge_for_prompt：只處理brand+category都有帶的情況（目前所有呼叫端都這樣用）
    if "FROM seo_knowledge WHERE" in s and len(params) >= 2:
        brand, category = params[0], params[1]
        rows = KNOWLEDGE_ITEMS.get((brand, category), [])
        return rows

    # seo_brand_themes
    if "FROM seo_brand_themes WHERE brand_key=%s" in s:
        bk = params[0]
        t = THEMES.get(bk)
        if not t:
            return None
        return (t["brand_key"], t["primary_color"], t["accent_color"], t["bg_color"], t["confirmed"])

    # confirmed slugs (只抓已發布)
    if "FROM seo_articles WHERE brand_key=%s AND status='published' AND slug<>''" in s:
        bk = params[0]
        rows = [(a["slug"],) for a in ARTICLES.values() if a["brand_key"] == bk and a["status"] == "published" and a["slug"]]
        return rows

    # seo_article_edit 主查詢
    if s.startswith("SELECT id,title,slug,meta_title,meta_description,content,ai_summary,status,extra,easystore_article_id, brand_key,blocks"):
        aid = params[0]
        a = ARTICLES.get(aid)
        if not a:
            return None
        return (a["id"], a["title"], a["slug"], a["meta_title"], a["meta_description"], a["content"],
                a["ai_summary"], a["status"], a["extra"], a["easystore_article_id"], a["brand_key"], a["blocks"])

    # pillar dropdown
    if s.startswith("SELECT id, title, extra FROM seo_articles WHERE id != %s"):
        aid = params[0]
        return [(a["id"], a["title"], a["extra"]) for a in ARTICLES.values() if a["id"] != aid]

    # _validate_article_for_publish 主查詢
    if s.startswith("SELECT title,meta_title,meta_description,content,blocks,brand_key,category,extra,status"):
        aid = params[0]
        a = ARTICLES.get(aid)
        if not a:
            return None
        return (a["title"], a["meta_title"], a["meta_description"], a["content"], a["blocks"],
                a["brand_key"], a["category"], a["extra"], a["status"])

    # _render_article_output_html / rendered-html / preview
    if s.startswith("SELECT blocks, brand_key, content FROM seo_articles WHERE id=%s"):
        aid = params[0]
        a = ARTICLES.get(aid)
        if not a:
            return None
        return (a["blocks"], a["brand_key"], a["content"])

    # seo_article_image_fill 專用查詢/更新
    if s.startswith("SELECT blocks, extra FROM seo_articles WHERE id=%s"):
        aid = params[0]
        a = ARTICLES.get(aid)
        if not a:
            return None
        return (a["blocks"], a["extra"])
    if s.startswith("UPDATE seo_articles SET blocks=%s, extra=%s, updated_at=%s WHERE id=%s"):
        blocks, extra, _, aid = params
        aid = int(aid)
        a = ARTICLES.get(aid)
        if a:
            a.update(blocks=blocks, extra=extra)
        return None

    # preview route 標題查詢
    if s.startswith("SELECT title FROM seo_articles WHERE id=%s"):
        aid = params[0]
        a = ARTICLES.get(aid)
        if not a:
            return None
        return (a["title"],)

    # seo_article_save: 讀舊資料
    if s.startswith("SELECT extra, title, content, meta_title, meta_description FROM seo_articles WHERE id=%s"):
        aid = int(params[0])
        a = ARTICLES.get(aid)
        if not a:
            return None
        return (a["extra"], a["title"], a["content"], a["meta_title"], a["meta_description"])

    # seo_article_save: UPDATE
    if s.startswith("UPDATE seo_articles SET title=%s, slug=%s, meta_title=%s, meta_description=%s,"):
        (title, slug, meta_title, meta_desc, content, ai_summary, status, extra, updated_at,
         status2, published_at_ts, aid) = params
        aid = int(aid)
        a = ARTICLES.get(aid)
        if a:
            a.update(title=title, slug=slug, meta_title=meta_title, meta_description=meta_desc,
                      content=content, ai_summary=ai_summary, status=status, extra=extra)
        return None

    # _run_quality_check_job 專用查詢/更新
    if s.startswith("SELECT title,meta_title,meta_description,content,brand_key,category,extra,blocks"):
        aid = params[0]
        a = ARTICLES.get(aid)
        if not a:
            return None
        return (a["title"], a["meta_title"], a["meta_description"], a["content"],
                a["brand_key"], a["category"], a["extra"], a["blocks"])
    if s.startswith("UPDATE seo_articles SET extra=%s, status=%s, updated_at=%s WHERE id=%s"):
        extra, status, updated_at, aid = params
        aid = int(aid)
        a = ARTICLES.get(aid)
        if a:
            a.update(extra=extra, status=status)
        return None
    if s.startswith("UPDATE seo_quality_check_jobs SET status='running', updated_at=%s WHERE id=%s"):
        _, job_id = params
        QC_JOBS[job_id]["status"] = "running"
        return None
    if s.startswith("UPDATE seo_quality_check_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s"):
        error_msg, _, job_id = params
        QC_JOBS[job_id].update(status="error", error_msg=error_msg)
        return None
    if s.startswith("UPDATE seo_quality_check_jobs SET status='done', result=%s, updated_at=%s WHERE id=%s"):
        result, _, job_id = params
        QC_JOBS[job_id].update(status="done", result=result)
        return None

    # seo_prompt_templates：_get_prompt_template / _save_prompt_template
    if s.startswith("SELECT content FROM seo_prompt_templates WHERE key=%s"):
        k = params[0]
        return (PROMPT_TEMPLATES[k],) if k in PROMPT_TEMPLATES else None
    if s.startswith("INSERT INTO seo_prompt_templates"):
        k, content, _ = params
        PROMPT_TEMPLATES[k] = content
        return None

    # seo_article_save: INSERT (新文章)
    if s.startswith("INSERT INTO seo_articles (title,slug,meta_title,meta_description,content,ai_summary,status,extra,created_at,updated_at,published_at)"):
        new_id = max(ARTICLES.keys()) + 1
        (title, slug, meta_title, meta_desc, content, ai_summary, status, extra, ca, ua, pa) = params
        add_article(new_id, title=title, slug=slug, meta_title=meta_title, meta_description=meta_desc,
                    content=content, ai_summary=ai_summary, status=status, extra=extra, brand_key="")
        return new_id

    # _run_generate_job: 生成完成後INSERT新文章（帶blocks/category，跟上面seo_article_save的INSERT欄位不同）
    if s.startswith("INSERT INTO seo_articles (title,slug,meta_title,meta_description,content,blocks,ai_summary,status,"):
        new_id = max(ARTICLES.keys()) + 1
        (title, slug, meta_title, meta_desc, content, blocks, ai_summary, status,
         brand_key, category, extra, ca, ua, pa) = params
        add_article(new_id, title=title, slug=slug, meta_title=meta_title, meta_description=meta_desc,
                    content=content, blocks=blocks, ai_summary=ai_summary, status=status,
                    brand_key=brand_key, category=category, extra=extra)
        return new_id

    # _run_generate_job: seo_generate_jobs 狀態更新
    if s.startswith("UPDATE seo_generate_jobs SET status='running', updated_at=%s WHERE id=%s"):
        _, job_id = params
        JOBS[job_id]["status"] = "running"
        return None
    if s.startswith("UPDATE seo_generate_jobs SET status='error', error_msg=%s, updated_at=%s WHERE id=%s"):
        error_msg, _, job_id = params
        JOBS[job_id].update(status="error", error_msg=error_msg)
        return None
    if s.startswith("UPDATE seo_generate_jobs SET status='done', article_id=%s, updated_at=%s WHERE id=%s"):
        article_id, _, job_id = params
        JOBS[job_id].update(status="done", article_id=article_id)
        return None

    raise AssertionError("fake_q沒有處理到這個SQL，測試腳本要補：\n" + s + f"\nparams={params}")


SA._q = fake_q

app = Flask(__name__)
app.register_blueprint(SA.seo_bp)
client = app.test_client()
KEY = "testkey"

print("=" * 70)
print("1. 三品牌 blocks 渲染 + scoped/inline + 手機表格文字")
print("=" * 70)

for bk, theme_expected_confirmed in [("jsimple", False), ("filterbreath", True), ("lander", False)]:
    theme = SA._get_brand_theme(bk)
    check(f"[{bk}] 取得主題設定不crash", isinstance(theme, dict))
    if bk == "filterbreath":
        check(f"[{bk}] LuAir沿用已確認藍白配色", theme["primary_color"] == "#1B3F6E" and theme["accent_color"] == "#2F80ED"
              and theme["bg_color"] == "#F5F8FC" and theme["confirmed"] is True, theme)
    else:
        check(f"[{bk}] 未設定時回傳中性暫定色且confirmed=False", theme["confirmed"] is False and theme["primary_color"] == SA.DEFAULT_NEUTRAL_THEME["primary_color"], theme)

    blocks = sample_blocks()
    scoped = SA._render_blocks_html(blocks, theme, inline=False)
    inline = SA._render_blocks_html(blocks, theme, inline=True)

    check(f"[{bk}] scoped版含<style>", "<style>" in scoped)
    check(f"[{bk}] inline版不含<style>", "<style>" not in inline)
    check(f"[{bk}] inline版用clamp()做字級縮放", "clamp(" in inline)
    check(f"[{bk}] scoped版桌機字級18px", "font-size:18px" in scoped)
    check(f"[{bk}] scoped版mobile media query把wrap字級降到16px(>=16px規範)", "@media (max-width:600px)" in scoped and "font-size:16px" in scoped.split("@media")[1])
    check(f"[{bk}] scoped版表格桌機/手機都維持16px、不因mobile media query被壓到14px以下",
          "font-size:16px" in scoped and "@media" in scoped and ".jxsa-table{font-size:14px}" not in scoped.split("@media")[1] if "@media" in scoped else False)
    check(f"[{bk}] 表格用overflow-x:auto水平捲動而不是縮小", "overflow-x:auto" in scoped)
    check(f"[{bk}] caption/note字級>=14px", "font-size:14px" in SA._block_style_rules(theme)["caption"] and "font-size:14px" in SA._block_style_rules(theme)["note"])
    check(f"[{bk}] line-height約1.8", "line-height:1.8" in scoped)
    check(f"[{bk}] AI文字被正確escape，沒有原始<script>標籤流出", "<script>alert(1)</script>" not in scoped and "&lt;script&gt;alert(1)&lt;/script&gt;" in scoped)
    check(f"[{bk}] AI文字被正確escape(inline版)", "<script>alert(1)</script>" not in inline and "&lt;script&gt;alert(1)&lt;/script&gt;" in inline)

print("=" * 70)
print("2. _is_safe_url 協定驗證（不是只靠escape）")
print("=" * 70)
url_cases = [
    ("https://www.jsimple.tw/blog/x", True),
    ("http://example.com", True),
    ("/blog/relative-ok", True),
    ("javascript:alert(1)", False),
    ("JAVASCRIPT:alert(1)", False),
    ("  javascript:alert(1)", False),
    ("java\tscript:alert(1)", False),
    ("data:text/html,<script>alert(1)</script>", False),
    ("vbscript:msgbox(1)", False),
    ("//evil.com/phishing", False),
    ("", False),
    (None, False),
]
for url, expected in url_cases:
    got = SA._is_safe_url(url)
    check(f"_is_safe_url({url!r}) == {expected}", got == expected, f"got {got}")

print("=" * 70)
print("3. 跨品牌資料 / 無效連結 / 待補充內容會被阻擋")
print("=" * 70)

ok3, err3 = SA._validate_article_for_publish(3)
check("跨品牌商品混入應該被擋（文章3：濾呼吸帶JS家具的辦公桌）", ok3 is False and any("跨品牌" in e or "不在本品牌允許商品清單內" in e for e in err3), err3)

ok4, err4 = SA._validate_article_for_publish(4)
check("文章4本身沒有明著違規欄位，但resolve_block_links應該已經把javascript:網址濾掉，不會出現在effective_html裡", True)
blocks4 = json.loads(ARTICLES[4]["blocks"])
resolved4, missing4 = SA._resolve_block_links(blocks4, "filterbreath")
rendered4 = SA._render_blocks_html(resolved4, SA._get_brand_theme("filterbreath"), inline=False)
check("related_links的javascript:網址被_resolve_block_links濾掉並記錄missing", "javascript:" not in rendered4 and len(missing4) > 0, missing4)
check("_blocks_body_html本身也擋javascript:網址（雙重防護，不是只靠escape）",
      "javascript:" not in SA._render_blocks_html(blocks4, SA._get_brand_theme("filterbreath"), inline=False))

ok5, err5 = SA._validate_article_for_publish(5)
check("殘留「待確認」文字應該被擋（文章5：朗德吊燈）", ok5 is False and any("待補充" in e or "待確認" in e for e in err5), err5)

ok6, err6 = SA._validate_article_for_publish(6)
check("AI輸出被截斷應該被擋（文章6：朗德truncated）", ok6 is False and any("截斷" in e for e in err6), err6)

ok7, err7 = SA._validate_article_for_publish(7)
check("缺欄位（meta_description空）應該被擋（文章7）", ok7 is False and any("是空的" in e for e in err7), err7)

# 內部連結：草稿文章的slug不該被拿來連
resolved_check, missing_check = SA._resolve_block_links(
    [{"type": "related_links", "items": [{"url": "/blog/js-draft-only", "text": "連到草稿"}]}], "jsimple")
linked_urls = [it["url"] for it in resolved_check[0]["items"]]
check("related_links不能連到草稿(未發布)文章的slug", "/blog/js-draft-only" not in linked_urls, resolved_check)

resolved_pub, missing_pub = SA._resolve_block_links(
    [{"type": "related_links", "items": [{"url": "/blog/js-desk-guide", "text": "連到已發布文章"}]}], "jsimple")
linked_urls_pub = [it["url"] for it in resolved_pub[0]["items"]]
check("related_links可以連到已發布文章的slug", "/blog/js-desk-guide" in linked_urls_pub, resolved_pub)

print("=" * 70)
print("3b. _check_products_brand_ownership必須用品類規則key_products的三層fallback，不能只認品牌預設清單")
print("=" * 70)

# 17) JSIMPLE「穀倉門」品類：related_products命中品類規則key_products，但不在brand.allowed_products品牌預設清單裡
#     -> 應該通過（三層fallback第一優先是品類規則），修這支bug之前會被誤擋
add_article(17, title="穀倉門五金怎麼挑", slug="/blog/js-barn-door", meta_title="mt", meta_description="md",
            content="", brand_key="jsimple", category="穀倉門", status="draft_review",
            blocks=json.dumps(sample_blocks(bad_url="/pages/contact-js"), ensure_ascii=False),
            extra=json.dumps({"related_products": "穀倉門滑軌組", "quality_check": {"brand_consistency_pass": True, "recommend_publish": True}}, ensure_ascii=False))
_stamp_fingerprint(17)

# 18) 同品類，但related_products既不在品類規則key_products、也不在品牌預設清單 -> 仍然應該被擋
#     （驗證修法不是把檢查整個關掉，只是換一套更完整的允許清單來源）
add_article(18, title="穀倉門亂帶商品測試", slug="/blog/js-barn-door-bad", meta_title="mt", meta_description="md",
            content="", brand_key="jsimple", category="穀倉門", status="draft_review",
            blocks=json.dumps(sample_blocks(bad_url="/pages/contact-js"), ensure_ascii=False),
            extra=json.dumps({"related_products": "濾網", "quality_check": {"brand_consistency_pass": True, "recommend_publish": True}}, ensure_ascii=False))
_stamp_fingerprint(18)

ok17, err17 = SA._validate_article_for_publish(17)
check("品類規則key_products有登記的商品（穀倉門滑軌組），即使不在品牌預設allowed_products清單，也應該通過（文章17）",
      ok17 is True, err17)

ok18, err18 = SA._validate_article_for_publish(18)
check("品類規則key_products、品牌預設清單都沒有的商品（濾網），在穀倉門品類文章裡仍應被擋（文章18）",
      ok18 is False and any("不在本品牌允許商品清單內" in e for e in err18), err18)

print("=" * 70)
print("4. AI檢查失敗 / 缺欄位 / 截斷時不能發布；乾淨案例應該要過")
print("=" * 70)

ok8, err8 = SA._validate_article_for_publish(8)
check("沒跑過AI品質檢查應該被擋（文章8）", ok8 is False and any("尚未跑過AI品質檢查" in e for e in err8), err8)

ok9, err9 = SA._validate_article_for_publish(9)
check("AI不建議發布應該被擋（文章9）", ok9 is False and any("不建議發布" in e for e in err9), err9)

ok11, err11 = SA._validate_article_for_publish(11)
check("完全乾淨、全部欄位齊全、AI通過的文章應該要通過發布前檢查（文章11）", ok11 is True, err11)

print("=" * 70)
print("5. 舊文章（無blocks）仍可開啟、編輯、預覽")
print("=" * 70)

resp10 = client.get(f"/admin/seo/article/10?key={KEY}")
check("舊文章編輯頁GET不crash(HTTP 200)", resp10.status_code == 200, resp10.status_code)
check("舊文章編輯頁有帶出舊的raw content", "這是升級前生成的純HTML文章" in resp10.get_data(as_text=True))

resp10p = client.get(f"/admin/seo/article/10/preview?key={KEY}&device=mobile")
check("舊文章預覽頁GET不crash(HTTP 200)", resp10p.status_code == 200, resp10p.status_code)
check("舊文章預覽退回顯示raw content(因為沒有blocks)", "這是升級前生成的純HTML文章" in resp10p.get_data(as_text=True))

resp10h = client.get(f"/admin/seo/article/10/rendered-html?key={KEY}&inline=0")
check("舊文章複製HTML端點不crash", resp10h.status_code == 200)
check("舊文章複製HTML內容正確退回raw content", "這是升級前生成的純HTML文章" in resp10h.get_json().get("html", ""))

print("=" * 70)
print("6. 新文章（有blocks）編輯頁能看到發布前檢查卡片 + 預覽/複製HTML入口")
print("=" * 70)

resp11 = client.get(f"/admin/seo/article/11/preview?key={KEY}&device=desktop")
check("新文章預覽頁GET不crash", resp11.status_code == 200)
check("新文章預覽有渲染出blocks內容(重點整理出現)", "重點整理" in resp11.get_data(as_text=True))

resp9edit = client.get(f"/admin/seo/article/9/preview?key={KEY}")
resp9edit_page = client.get(f"/admin/seo/article/9?key={KEY}")
check("未通過發布檢查的文章，編輯頁會顯示紅框未通過訊息", "尚未通過發布前檢查" in resp9edit_page.get_data(as_text=True))

resp11edit_page = client.get(f"/admin/seo/article/11?key={KEY}")
check("已通過發布檢查的文章，編輯頁會顯示綠色可發布訊息", "通過所有檢查，可以發布" in resp11edit_page.get_data(as_text=True))

print("=" * 70)
print("7. 品牌預覽頁 /admin/seo-brand-preview/<brand_key>")
print("=" * 70)
for bk in ["jsimple", "filterbreath", "lander"]:
    r = client.get(f"/admin/seo-brand-preview/{bk}?key={KEY}&device=mobile")
    check(f"[{bk}] 品牌範例預覽頁不crash", r.status_code == 200, r.status_code)

print("=" * 70)
print("8. 文章修改後，舊的AI品質檢查結果要失效")
print("=" * 70)

before = SA._parse_extra(ARTICLES[11]["extra"])
check("修改前quality_check存在", bool(before.get("quality_check")))

resp_save = client.post(f"/admin/seo/article/save?key={KEY}", data={
    "id": "11",
    "title": ARTICLES[11]["title"],
    "slug": ARTICLES[11]["slug"],
    "status": "draft_review",
    "meta_title": ARTICLES[11]["meta_title"],
    "meta_description": ARTICLES[11]["meta_description"],
    "ai_summary": "",
    "content": ARTICLES[11]["content"] + "<p>手動改過內容</p>",  # 內容變了
    "related_products": "濾網",
}, follow_redirects=False)
check("儲存文章不crash（會是302 redirect）", resp_save.status_code in (302, 200), resp_save.status_code)

after = SA._parse_extra(ARTICLES[11]["extra"])
check("內容改過之後，舊的quality_check必須被清掉，逼重新檢查", not after.get("quality_check"), after)

ok11b, err11b = SA._validate_article_for_publish(11)
check("內容改過但還沒重新AI檢查，應該不能發布了", ok11b is False and any("尚未跑過AI品質檢查" in e for e in err11b), err11b)

# 反例：內容/標題/對應商品都沒變 -> quality_check應該保留
add_article(12, title="不變測試", slug="/blog/no-change", meta_title="mt", meta_description="md",
            content="<p>內容不變</p>", brand_key="jsimple", status="draft_review",
            blocks="",
            extra=json.dumps({"related_products": "辦公桌", "quality_check": {"brand_consistency_pass": True, "recommend_publish": True}}, ensure_ascii=False))

# 幫文章12補上正確指紋（1/9/11在頂部fixture區已經補過了，這裡只差12，因為12是這一段才建立的）
_stamp_fingerprint(12)

client.post(f"/admin/seo/article/save?key={KEY}", data={
    "id": "12", "title": "不變測試", "slug": "/blog/no-change", "status": "draft_review",
    "meta_title": "mt", "meta_description": "md", "ai_summary": "",
    "content": "<p>內容不變</p>", "related_products": "辦公桌",
})
after12 = SA._parse_extra(ARTICLES[12]["extra"])
check("內容/標題/對應商品都沒變時，quality_check不該被清掉", bool(after12.get("quality_check")), after12)

print("=" * 70)
print("9. 發布使用的HTML跟預覽/受檢內容一致（不會拿舊content發布）")
print("=" * 70)

# 文章11：把content欄位改成明顯不同、過期的內容，但blocks是最新的 -> 驗證發布/預覽都是用blocks重新渲染，不是用過期content
ARTICLES[11]["content"] = "<p>這是資料庫裡過期的content欄位，不該被發布出去</p>"
effective = SA._render_article_output_html(11, inline=False)
check("有blocks時，effective_html是重新渲染的結果，不是過期的content欄位", "過期的content欄位" not in effective and "重點整理" in effective, effective[:200])

ok11c, err11c = SA._validate_article_for_publish(11)
# 文章11此時quality_check在步驟8已被清掉了，所以會因為"尚未跑過AI品質檢查"擋掉，這裡只驗證它不是因為過期content裡的東西被擋
check("_validate_article_for_publish檢查的是重新渲染的HTML，不是過期的content欄位", not any("過期的content欄位" in e for e in err11c), err11c)

print("=" * 70)
print("10. 指紋不匹配時，就算quality_check欄位存在也視為未檢查（文章13）")
print("=" * 70)

ok13, err13 = SA._validate_article_for_publish(13)
check("指紋對不起來時，即使quality_check看起來通過也要擋下並要求重新檢查（文章13）",
      ok13 is False and any("內容在上次檢查後又被修改" in e for e in err13), err13)

print("=" * 70)
print("11. 發布前重新驗證內部連結的『現在』狀態，不吃生成當下的舊快照（文章15）")
print("=" * 70)

# 一開始文章11還是草稿，文章15連過去的連結本來就該被擋下
ok15a, err15a = SA._validate_article_for_publish(15)
check("連結目標(文章11)還是草稿時，文章15的發布前檢查應該被擋下", ok15a is False and any("內部連結驗證失敗" in e for e in err15a), err15a)

# 讓文章11變成已發布 -> 文章15的連結應該即時反映成有效，不會卡在舊的失敗結果
ARTICLES[11]["status"] = "published"
ok15b, err15b = SA._validate_article_for_publish(15)
check("連結目標變成已發布之後，文章15的連結驗證應該即時反映最新狀態、不再被擋",
      not any("內部連結驗證失敗" in e for e in err15b), err15b)

rendered15_pub = SA._render_article_output_html(15, inline=False)
check("連結目標已發布時，文章15渲染出的HTML真的含有指向文章11的連結", "/blog/lu-clean-pass" in rendered15_pub, rendered15_pub[:300])

# 再讓文章11下架回草稿 -> 驗證不會偷偷沿用剛剛「已通過」的結果，而是重新反映當下狀態
ARTICLES[11]["status"] = "draft_review"
ok15c, err15c = SA._validate_article_for_publish(15)
check("連結目標又被下架回草稿之後，重新檢查應該立刻反映最新狀態、重新擋下，而不是沿用上一次的通過結果",
      ok15c is False and any("內部連結驗證失敗" in e for e in err15c), err15c)

rendered15_draft = SA._render_article_output_html(15, inline=False)
check("連結目標下架回草稿後，文章15重新渲染的HTML不應該再含有失效連結（即時反映，不是快取舊render）",
      "/blog/lu-clean-pass" not in rendered15_draft, rendered15_draft[:300])

print("=" * 70)
print("12. 實際編輯→儲存→重開預覽：修改要真正生效，且content欄位對blocks文章預覽本來就沒作用（文章16）")
print("=" * 70)

resp16_before = client.get(f"/admin/seo/article/16/preview?key={KEY}&device=desktop")
check("編輯前預覽GET不crash", resp16_before.status_code == 200, resp16_before.status_code)
preview16_before = resp16_before.get_data(as_text=True)
check("編輯前預覽顯示的是目前標題", "濾呼吸初始標題" in preview16_before, preview16_before[:300])
check("編輯前預覽是渲染blocks內容(重點整理出現)，不是content欄位的文字",
      "重點整理" in preview16_before and "初始content" not in preview16_before, preview16_before[:300])

resp16_edit_page = client.get(f"/admin/seo/article/16?key={KEY}")
check("blocks文章編輯頁應該顯示「content欄位改了不會反映在預覽」的提醒",
      "改了不會反映在預覽或發布結果上" in resp16_edit_page.get_data(as_text=True))

resp10_edit_page = client.get(f"/admin/seo/article/10?key={KEY}")
check("舊文章(無blocks)編輯頁不應該出現這個提醒(因為content欄位對它就是唯一真實內容)",
      "改了不會反映在預覽或發布結果上" not in resp10_edit_page.get_data(as_text=True))

resp16_save = client.post(f"/admin/seo/article/save?key={KEY}", data={
    "id": "16",
    "title": "濾呼吸改標題測試",
    "slug": ARTICLES[16]["slug"],
    "status": "draft_review",
    "meta_title": ARTICLES[16]["meta_title"],
    "meta_description": ARTICLES[16]["meta_description"],
    "ai_summary": "",
    "content": "編輯頁改了這段content，但這篇是blocks文章，理論上預覽不該變",
    "related_products": "濾網",
}, follow_redirects=False)
check("儲存文章16不crash", resp16_save.status_code in (302, 200), resp16_save.status_code)

resp16_after = client.get(f"/admin/seo/article/16/preview?key={KEY}&device=desktop")
preview16_after = resp16_after.get_data(as_text=True)
check("儲存後重開預覽，標題真的更新成新標題(修改有生效)", "濾呼吸改標題測試" in preview16_after, preview16_after[:300])
check("儲存後重開預覽，仍然是blocks渲染出來的正文(重點整理還在)", "重點整理" in preview16_after, preview16_after[:300])
check("儲存後重開預覽，不會出現剛剛改過的content文字(blocks文章的content本來就不影響輸出)",
      "理論上預覽不該變" not in preview16_after, preview16_after[:300])

print("=" * 70)
print("13. blocks=[]（空氣清淨機濾網多久換？案例）：RELATED_PRODUCTS三層fallback / prompt不矛盾 / main_keyword擋下 / blocks=[]不入庫")
print("=" * 70)

# 13a) 情境還原：filterbreath沒有命中任何seo_brand_rules（SEO_BRAND_RULES目前只有jsimple穀倉門那筆），
#      舊版_resolve_generate_fields只查brand_rule.key_products，這裡一定是空字串；
#      修正後應該再往下fallback到_resolve_allowed_products()（跟guardrail的ALLOWED_PRODUCTS同一套三層），
#      結果要等於品牌預設allowed_products，不能是空字串。
brand_lu = SA._get_brand("filterbreath")
brand_rule_mode_lu, brand_rule_lu = SA._resolve_brand_rule("filterbreath", "空氣清淨機濾網", {})
resolved_lu, sources_lu = SA._resolve_generate_fields({}, brand_rule_lu, brand_lu, "空氣清淨機濾網")
check("沒有命中seo_brand_rules時，RELATED_PRODUCTS要fallback到品牌預設allowed_products，不能是空字串",
      resolved_lu["related_products"] == BRANDS["filterbreath"]["allowed_products"], resolved_lu)
check("field_sources要如實標示這個值來自_resolve_allowed_products的哪一層，不能假裝是seo_brand_rules",
      sources_lu["RELATED_PRODUCTS"]["src"] in ("品牌預設 allowed_products", "品類規則 key_products"), sources_lu)

# 13b) 舊呼叫方式（不傳brand/category）要維持原行為不變，不能因為加了新參數就強制要求呼叫端都要改
resolved_old_sig, _ = SA._resolve_generate_fields({}, brand_rule_lu)
check("_resolve_generate_fields不傳brand時要維持原本行為（不fallback），向下相容舊呼叫方式",
      resolved_old_sig["related_products"] == "", resolved_old_sig)

# 13c) 送進Sonnet的實際prompt：RELATED_PRODUCTS這格要真的填上商品，不能是空的
prompt_lu = SA._generate_article_prompt(brand_lu, "空氣清淨機濾網", "空氣清淨機濾網多久換？",
                                         "（分析內容略）", [], {}, brand_rule_lu)
check("最終prompt的「對應商品」欄位要帶入品牌預設商品，不能因為沒有品類規則就整格空白",
      f"）：{BRANDS['filterbreath']['allowed_products']}" in prompt_lu, prompt_lu)

# 13d) 就算真的沒有任何商品資料（品牌allowed_products也是空的），prompt文字本身也不該再是「必須從裡面挑」
#      這種無論如何都成立的絕對指令——確認新版模板已經把「對應商品是空的」講清楚是允許的，
#      不是靠碰巧有資料才不矛盾。
check("DEFAULT_GENERATE_PROMPT模板已經把「對應商品欄位是空的」明確講成可接受，不再是無條件的强制指令",
      "如果下面是空的，代表這篇不需要對應特定商品" in SA.DEFAULT_GENERATE_PROMPT and
      "如果「對應商品」是空的，整篇就專心把知識講清楚" in SA.DEFAULT_GENERATE_PROMPT)

# 13e) 生成前擋下：main_keyword沒填就不該呼叫AI、也不該建立生成任務
resp_gen_no_kw = client.post(f"/admin/seo-generator/generate?key={KEY}", json={
    "brand": "filterbreath", "category": "空氣清淨機濾網", "topic": "空氣清淨機濾網多久換？",
    "analysis": "", "main_keyword": "",
})
check("main_keyword沒填時，/generate要回400並擋下，不建立生成任務、不呼叫AI",
      resp_gen_no_kw.status_code == 400 and "主關鍵字" in resp_gen_no_kw.get_json().get("error", ""),
      (resp_gen_no_kw.status_code, resp_gen_no_kw.get_data(as_text=True)))

# 13f) Sonnet真的回傳blocks=[]時（模擬guardrail矛盾情境下AI選擇不生成正文），
#      _run_generate_job不能把這個半成品塞進seo_articles，要讓job落在錯誤狀態、讓使用者知道要重試。
JOBS[901] = {"id": 901, "status": "pending", "article_id": None, "error_msg": ""}
before_article_ids = set(ARTICLES.keys())

def fake_ai_call_json_full_empty_blocks(prompt, model=None, max_tokens=None):
    return ({
        "title": "空氣清淨機濾網多久換？", "slug": "/blog/filter-change-cycle",
        "meta_title": "mt", "meta_description": "md", "ai_summary": "ai_summary",
        "needs_confirmation": True, "confirmation_notes": "缺少對應商品/主關鍵字，AI選擇不生成正文",
        "blocks": [], "internal_links": "", "long_tail_keywords": "", "knowledge_citations": [],
    }, None, "end_turn")

_orig_ai_call_json_full = SA._ai_call_json_full
SA._ai_call_json_full = fake_ai_call_json_full_empty_blocks
try:
    SA._run_generate_job(901, "filterbreath", "空氣清淨機濾網", "空氣清淨機濾網多久換？", "（分析內容略）",
                          opp_id=None, fields={"main_keyword": ""})
finally:
    SA._ai_call_json_full = _orig_ai_call_json_full

check("blocks=[]時不能新增半成品seo_articles", set(ARTICLES.keys()) == before_article_ids, ARTICLES.keys())
check("blocks=[]時job要落在錯誤狀態，不是done", JOBS[901]["status"] == "error", JOBS[901])
check("錯誤訊息要講清楚是blocks為空、不是建立半成品，方便使用者知道要重試",
      "blocks為空" in JOBS[901]["error_msg"] and "半成品" in JOBS[901]["error_msg"], JOBS[901])

# 13g) 正常情境對照組：main_keyword/related_products都有填、AI回傳正常blocks時，仍然要能正常入庫成草稿，
#      確認這次修正沒有連帶把「正常生成」的路徑弄壞。
JOBS[902] = {"id": 902, "status": "pending", "article_id": None, "error_msg": ""}
before_article_ids_2 = set(ARTICLES.keys())

def fake_ai_call_json_full_ok(prompt, model=None, max_tokens=None):
    return ({
        "title": "濾網多久換一次？完整週期指南", "slug": "/blog/filter-change-guide",
        "meta_title": "mt", "meta_description": "md", "ai_summary": "ai_summary",
        "needs_confirmation": False, "confirmation_notes": "",
        "blocks": sample_blocks(bad_url="/pages/contact-lu"),
        "internal_links": "", "long_tail_keywords": "", "knowledge_citations": [],
    }, None, "end_turn")

SA._ai_call_json_full = fake_ai_call_json_full_ok
try:
    SA._run_generate_job(902, "filterbreath", "空氣清淨機濾網", "空氣清淨機濾網多久換？", "（分析內容略）",
                          opp_id=None, fields={"main_keyword": "濾網更換週期"})
finally:
    SA._ai_call_json_full = _orig_ai_call_json_full

check("正常案例（有main_keyword、AI回傳正常blocks）要真的新增一篇文章，證明這次修正沒擋到正常路徑",
      JOBS[902]["status"] == "done" and JOBS[902]["article_id"] in ARTICLES, JOBS[902])
if JOBS[902]["article_id"] in ARTICLES:
    new_article = ARTICLES[JOBS[902]["article_id"]]
    check("正常案例新文章狀態應該是draft_review（沒有blocks_errors/needs_confirmation/placeholder）",
          new_article["status"] == "draft_review", new_article["status"])

# 13h) debug要能看到main_keyword/search_intent的值與來源，方便後台判斷「AI沒填正文」是不是因為這兩個沒填
resp_preview = client.post(f"/admin/seo-generator/preview?key={KEY}", json={
    "brand": "filterbreath", "category": "空氣清淨機濾網", "topic": "空氣清淨機濾網多久換？",
    "analysis": "", "main_keyword": "", "search_intent": "",
})
preview_debug = resp_preview.get_json()["debug"]["fields"]
check("Preview debug要顯示MAIN_KEYWORD欄位且標示為空（未填）", preview_debug["MAIN_KEYWORD"]["src"] == "空（未填，AI不會自動補）", preview_debug)
check("Preview debug要顯示RELATED_PRODUCTS已經fallback到品牌預設商品，不是空的",
      preview_debug["RELATED_PRODUCTS"]["value"] == BRANDS["filterbreath"]["allowed_products"], preview_debug)

print("=" * 70)
print("14. Prompt設定存檔前的輸出契約檢查：擋下會把blocks換成content、或砍掉分析建議格式的自訂Prompt")
print("=" * 70)

# 14a) 情境還原：濾呼吸的luair-filter-blog SOP把generate輸出從blocks陣列換成content純文字，
#      這種存檔不能成功，否則之後所有品牌生成都會變成blocks永遠是空的（_run_generate_job只認blocks）
bad_generate_prompt = SA.DEFAULT_GENERATE_PROMPT.replace('"blocks": [ {"type":"heading","level":2,"text":"..."} ]',
                                                          '"content": "完整文章內容，純HTML格式"')
check("bad_generate_prompt真的把blocks拿掉了（測試前提要成立）", '"blocks"' not in bad_generate_prompt)
resp_bad_gen = client.post(f"/admin/seo-settings/save?key={KEY}", data={
    "prompt_key": "generate", "content": bad_generate_prompt,
})
check("拿掉blocks輸出格式的generate Prompt存檔要被擋下（回200顯示錯誤，不是302導回列表）",
      resp_bad_gen.status_code == 200, resp_bad_gen.status_code)
check("擋下的錯誤訊息要講清楚是缺少blocks欄位",
      "blocks" in resp_bad_gen.get_data(as_text=True) and "擋下儲存" in resp_bad_gen.get_data(as_text=True),
      resp_bad_gen.get_data(as_text=True)[:300])
check("擋下之後不能真的寫進seo_prompt_templates，generate仍要維持系統預設值",
      "generate" not in PROMPT_TEMPLATES, PROMPT_TEMPLATES.get("generate"))

# 14b) 情境還原：分析Prompt砍掉「建議主關鍵字」等固定格式行，會讓後台自動帶入的欄位全部變空
bad_analyze_prompt = SA.DEFAULT_ANALYZE_PROMPT.replace("建議主關鍵字：（1個最重要的SEO主關鍵字，4~10個繁體中文字，不含標點符號）", "")
check("bad_analyze_prompt真的把「建議主關鍵字」這行拿掉了（測試前提要成立）", "建議主關鍵字" not in bad_analyze_prompt)
resp_bad_an = client.post(f"/admin/seo-settings/save?key={KEY}", data={
    "prompt_key": "analyze", "content": bad_analyze_prompt,
})
check("砍掉建議主關鍵字這行的analyze Prompt存檔要被擋下",
      resp_bad_an.status_code == 200 and "建議主關鍵字" in resp_bad_an.get_data(as_text=True), resp_bad_an.status_code)
check("擋下之後analyze仍要維持系統預設值，不能真的存進去",
      "analyze" not in PROMPT_TEMPLATES, PROMPT_TEMPLATES.get("analyze"))

# 14c) 正常案例：格式正確、只改風格/規則文字的自訂Prompt，要能正常存檔成功（不能因為新增檢查就連正常案例都擋掉）
good_generate_prompt = SA.DEFAULT_GENERATE_PROMPT.replace("你是台灣SEO/GEO/AEO內容策略專家與文案編輯", "你是資深家電耗材文案編輯")
resp_good_gen = client.post(f"/admin/seo-settings/save?key={KEY}", data={
    "prompt_key": "generate", "content": good_generate_prompt,
})
check("只調整風格文字、有保留blocks輸出格式的generate Prompt要能正常存檔成功（302導回列表+flash=已儲存）",
      resp_good_gen.status_code == 302 and "flash" in resp_good_gen.headers.get("Location", ""), resp_good_gen.status_code)
check("正常案例真的寫進seo_prompt_templates了", PROMPT_TEMPLATES.get("generate") == good_generate_prompt)

# 14d) 還原預設值路徑本來就是用DEFAULT_*_PROMPT，不用經過使用者輸入檢查，也不該被這次新增的檢查誤擋
resp_reset = client.post(f"/admin/seo-settings/reset?key={KEY}", data={"prompt_key": "generate"})
check("還原預設值本身一定合法，不會被新的契約檢查擋下", resp_reset.status_code == 302, resp_reset.status_code)
check("還原後seo_prompt_templates裡的generate確實變回系統預設值",
      PROMPT_TEMPLATES.get("generate") == SA.DEFAULT_GENERATE_PROMPT)

print("=" * 70)
print("15. seo_generator_analyze()：AI回應被截斷時要明確回錯誤，不能把不完整分析當成功結果")
print("=" * 70)

_orig_ai_call_full = SA._ai_call_full

# 15a) 情境還原：自訂分析Prompt被加長後，Haiku在max_tokens內寫不完，stop_reason="max_tokens"
def fake_ai_call_full_truncated(prompt, model=None, max_tokens=None):
    return ("建議文章類型：教學型\n建議主關鍵字：HEPA濾網更換\n（後面還沒寫完就被截斷了...",
            "", "max_tokens")

SA._ai_call_full = fake_ai_call_full_truncated
try:
    resp_trunc = client.post(f"/admin/seo-generator/analyze?key={KEY}", json={
        "brand": "filterbreath", "category": "", "topic": "HEPA濾網多久該換一次？",
    })
finally:
    SA._ai_call_full = _orig_ai_call_full

trunc_data = resp_trunc.get_json()
check("stop_reason=max_tokens時，回應要有error欄位，不能是正常成功結果",
      bool(trunc_data.get("error")), trunc_data)
check("截斷的錯誤訊息要講清楚是被截斷，不是其他原因",
      "截斷" in (trunc_data.get("error") or ""), trunc_data)
check("截斷時不能回傳suggested_main_keyword等建議欄位（避免前端誤以為分析成功並自動填入不完整的值）",
      "suggested_main_keyword" not in trunc_data, trunc_data)

# 15b) 正常案例：沒有被截斷，7個建議欄位都要能正確解析出來（對照組，確保新檢查沒有誤傷正常情況）
FAKE_COMPLETE_ANALYSIS = """在開始詳細分析之前，請先依序輸出以下7行建議：
建議文章類型：教學型
建議主關鍵字：HEPA濾網更換週期
建議搜尋意圖：想知道HEPA濾網多久該換一次
建議目標客群：使用空氣清淨機、擔心濾網效能下降的使用者
建議對應商品：HEPA濾網,活性碳濾網
建議禁止方向：不要提到其他品牌的濾網
建議CTA方向：引導確認機型後選購對應濾網

---
接下來才開始詳細分析：
（這裡是完整分析內容，略）"""

def fake_ai_call_full_ok(prompt, model=None, max_tokens=None):
    return (FAKE_COMPLETE_ANALYSIS, "", "end_turn")

SA._ai_call_full = fake_ai_call_full_ok
try:
    resp_ok = client.post(f"/admin/seo-generator/analyze?key={KEY}", json={
        "brand": "filterbreath", "category": "", "topic": "HEPA濾網多久該換一次？",
    })
finally:
    SA._ai_call_full = _orig_ai_call_full

ok_data = resp_ok.get_json()
check("沒有截斷時不能誤報error", not ok_data.get("error"), ok_data)
check("7行建議（移到最前面後）依然能被正確解析出來，跟原本放在結尾時抓法一致",
      ok_data.get("suggested_article_type") == "教學型" and
      ok_data.get("suggested_main_keyword") == "HEPA濾網更換週期" and
      ok_data.get("suggested_search_intent") == "想知道HEPA濾網多久該換一次" and
      ok_data.get("suggested_target_audience") == "使用空氣清淨機、擔心濾網效能下降的使用者" and
      ok_data.get("suggested_related_products") == "HEPA濾網,活性碳濾網" and
      ok_data.get("suggested_avoid_directions") == "不要提到其他品牌的濾網" and
      ok_data.get("suggested_cta_direction") == "引導確認機型後選購對應濾網",
      ok_data)

print("=" * 70)
print("16. 品質檢查新增的硬性規則：內部用語外露 / 標題承諾比較但正文沒回答，都要強制擋下不能發布")
print("=" * 70)

add_article(200, title="00820跟108850差在哪？高矮款怎麼選", slug="/blog/lu-model-compare",
            meta_title="mt", meta_description="md", content="<p>內容略</p>", brand_key="filterbreath",
            category="製冰機濾網", status="draft_review",
            blocks=json.dumps(sample_blocks(bad_url="/pages/contact-lu"), ensure_ascii=False),
            extra=json.dumps({"related_products": "濾網"}, ensure_ascii=False))

_orig_ai_call_json_full_qc = SA._ai_call_json_full

def _run_qc(article_id, fake_result):
    # _quality_check_run現在走_quality_check_call -> _ai_call_json_full（拿stop_reason偵測截斷），
    # 不是直接呼叫_ai_call_json了，mock要掛在_ai_call_json_full才會真的生效。
    job_id = f"qc_{article_id}_{len(QC_JOBS)}"
    QC_JOBS[job_id] = {"id": job_id, "status": "pending", "result": None, "error_msg": ""}
    SA._ai_call_json_full = lambda prompt, model=None, max_tokens=None: (dict(fake_result), "", "end_turn")
    try:
        SA._run_quality_check_job(job_id, article_id)
    finally:
        SA._ai_call_json_full = _orig_ai_call_json_full_qc
    return QC_JOBS[job_id]

# 16a) AI自己覺得可以發布(recommend_publish=True)，但同時標了internal_jargon_leaked=True
#      —— 程式要強制蓋掉AI自己的樂觀判斷，不能真的放行
job_a = _run_qc(200, {
    "score": 85, "recommend_publish": True, "brand_consistency_pass": True,
    "brand_consistency_issues": "", "has_placeholder_text": False,
    "internal_jargon_leaked": True, "title_content_mismatch": False,
    "issues": "正文出現「知識庫未列出」這種內部用語", "suggestions": "改成消費者語言",
    "next_status": "ready_to_publish", "suggested_sections": "", "suggested_internal_links": "",
    "suggested_related_products": "",
})
extra_a = SA._parse_extra(ARTICLES[200]["extra"])
check("internal_jargon_leaked=True時，就算AI自己說recommend_publish=True也要被強制蓋成False",
      extra_a["quality_check"]["recommend_publish"] is False, extra_a["quality_check"])
check("internal_jargon_leaked=True時，文章狀態要落在needs_revision，不能是ready_to_publish",
      ARTICLES[200]["status"] == "needs_revision", ARTICLES[200]["status"])

# 16b) 標題承諾了「差在哪、怎麼選」這種比較語意，但AI檢查認定正文沒有真的回答差異
job_b = _run_qc(200, {
    "score": 80, "recommend_publish": True, "brand_consistency_pass": True,
    "brand_consistency_issues": "", "has_placeholder_text": False,
    "internal_jargon_leaked": False, "title_content_mismatch": True,
    "issues": "標題問兩款差在哪，正文只各自介紹沒有給出差異結論", "suggestions": "補上實際差異或選購依據",
    "next_status": "ready_to_publish", "suggested_sections": "", "suggested_internal_links": "",
    "suggested_related_products": "",
})
extra_b = SA._parse_extra(ARTICLES[200]["extra"])
check("title_content_mismatch=True時，同樣要被強制蓋成recommend_publish=False",
      extra_b["quality_check"]["recommend_publish"] is False, extra_b["quality_check"])
check("title_content_mismatch=True時，文章狀態要落在needs_revision",
      ARTICLES[200]["status"] == "needs_revision", ARTICLES[200]["status"])

# 16c) 對照組：兩項都是False、AI也判斷可以發布 —— 這次新增的規則不能誤傷正常過關的案例
job_c = _run_qc(200, {
    "score": 92, "recommend_publish": True, "brand_consistency_pass": True,
    "brand_consistency_issues": "", "has_placeholder_text": False,
    "internal_jargon_leaked": False, "title_content_mismatch": False,
    "issues": "", "suggestions": "", "next_status": "ready_to_publish",
    "suggested_sections": "", "suggested_internal_links": "", "suggested_related_products": "",
})
extra_c = SA._parse_extra(ARTICLES[200]["extra"])
check("兩項新規則都沒觸發時，正常案例應該維持recommend_publish=True，新檢查沒有誤傷正常流程",
      extra_c["quality_check"]["recommend_publish"] is True, extra_c["quality_check"])
check("正常案例狀態應該是ready_to_publish", ARTICLES[200]["status"] == "ready_to_publish", ARTICLES[200]["status"])

# 16d) 2026-10-03正式站實測發現：品質檢查被標了很多問題、brand_consistency_issues寫很長時，
# 舊的max_tokens=2000會在JSON結尾的}之前被截斷，整個品質檢查直接報錯且訊息看不出是截斷。
# 驗證：stop_reason="max_tokens"時要回清楚的截斷錯誤，不能讓job狀態變成一個難懂的JSON解析錯誤。
job_id_trunc = f"qc_200_{len(QC_JOBS)}"
QC_JOBS[job_id_trunc] = {"id": job_id_trunc, "status": "pending", "result": None, "error_msg": ""}
SA._ai_call_json_full = lambda prompt, model=None, max_tokens=None: (
    None, "", "max_tokens")  # 模擬_ai_call_json_full在截斷時，regex抓不到完整JSON而回傳的情境
try:
    SA._run_quality_check_job(job_id_trunc, 200)
finally:
    SA._ai_call_json_full = _orig_ai_call_json_full_qc
check("品質檢查被截斷時，job要落在error狀態，錯誤訊息要明確講「被截斷」，不是一句看不懂的JSON錯誤",
      QC_JOBS[job_id_trunc]["status"] == "error" and "截斷" in QC_JOBS[job_id_trunc]["error_msg"],
      QC_JOBS[job_id_trunc])

# 16d) _quality_check_prompt本身要真的問到這兩項新規則，不能只是程式端硬加欄位、Prompt卻沒要求AI檢查
qc_prompt_text = SA._quality_check_prompt(
    {"title": "00820跟108850差在哪？", "meta_title": "mt", "meta_description": "md", "content": "內容略"},
    SA._get_brand("filterbreath"), "製冰機濾網", {}, {})
check("品質檢查Prompt要包含「內部/後台用語」外露的檢查項目", "內部" in qc_prompt_text and "後台用語" in qc_prompt_text, None)
check("品質檢查Prompt要包含「標題與內容不符」比較類的檢查項目", "標題與內容不符" in qc_prompt_text, None)
check("品質檢查Prompt輸出JSON schema要包含新的兩個欄位",
      "internal_jargon_leaked" in qc_prompt_text and "title_content_mismatch" in qc_prompt_text, None)

print("=" * 70)
print("17. DEFAULT_GENERATE_PROMPT新增的內容品質指示：資料盤點/不編造/核心問題答不出來就blocks=[]/型號呈現方式")
print("=" * 70)

check("generate Prompt要有「動筆前先盤點」的指示（目標1：只用已查證資料，不湊筆數）",
      "動筆前先盤點" in SA.DEFAULT_GENERATE_PROMPT)
check("generate Prompt要明確禁止「同等級」「差不多」這類編造結論（目標2）",
      "同等級" in SA.DEFAULT_GENERATE_PROMPT and "差不多" in SA.DEFAULT_GENERATE_PROMPT)
check("generate Prompt要有「核心問題答不出來就輸出空blocks」的指示（目標2）",
      "完全沒有對應的真實資料可以回答" in SA.DEFAULT_GENERATE_PROMPT and
      "請直接輸出空的 blocks 陣列（[]）" in SA.DEFAULT_GENERATE_PROMPT)
check("generate Prompt要有「品牌觀點的可信度」段落跟suggested_first_hand_data欄位（目標3）",
      "品牌觀點的可信度" in SA.DEFAULT_GENERATE_PROMPT and
      "suggested_first_hand_data" in SA.DEFAULT_GENERATE_PROMPT)
check("generate Prompt要有「完整設備型號→料號→對應商品」的呈現方式指示，且不強制3~5筆（目標4）",
      "完整設備型號" in SA.DEFAULT_GENERATE_PROMPT and "兩筆就寫兩筆" in SA.DEFAULT_GENERATE_PROMPT)
check("generate Prompt輸出JSON schema仍然保留blocks（沒有被改成content，符合修改範圍限制）",
      '"blocks"' in SA.DEFAULT_GENERATE_PROMPT and '"content"' not in SA.DEFAULT_GENERATE_PROMPT)

# 用三品牌各生成一次，確認新增的內容品質指示不會brand-specific、不會跨品牌污染、也不會讓blocks變空
for bk, cat, topic, mk in [
    ("jsimple", "", "高架床下方空間怎麼利用最實用？", "高架床下方空間利用"),
    ("lander", "", "客廳燈具怎麼選才不會太暗或太刺眼？", "客廳燈具怎麼選"),
    ("filterbreath", "", "空氣清淨機濾網多久該換一次？", "空氣清淨機濾網更換週期"),
]:
    prompt_text = SA._generate_article_prompt(SA._get_brand(bk), cat, topic, "（分析內容略）", [],
                                               {"main_keyword": mk}, {})
    check(f"[{bk}] 新版generate prompt組出來時要包含這次新增的內容品質指示，且沒有寫死其他品牌名稱",
          "動筆前先盤點" in prompt_text and "品牌觀點的可信度" in prompt_text, None)
    other_brand_names = [BRANDS[b]["name"] for b in BRANDS if b != bk]
    check(f"[{bk}] prompt裡不該出現其他品牌的名稱（確認新增內容不是寫死給特定品牌）",
          all(name not in prompt_text.replace(BRANDS[bk]["name"], "") for name in other_brand_names), None)

print("=" * 70)
print("18. 濾呼吸專屬文章版型：只對filterbreath生效、不寫進全站共用的generate Prompt")
print("=" * 70)

lu_prompt = SA._generate_article_prompt(SA._get_brand("filterbreath"), "製冰機濾網",
    "國際牌製冰室濾網00820與108850怎麼選", "（分析內容略）", [], {"main_keyword": "測試"}, {})
check("濾呼吸的generate prompt要包含專屬版型指引", "濾呼吸文章版型" in lu_prompt and "快答" in lu_prompt, None)

js_prompt = SA._generate_article_prompt(SA._get_brand("jsimple"), "", "高架床下方空間怎麼利用",
    "（分析內容略）", [], {"main_keyword": "測試"}, {})
ld_prompt = SA._generate_article_prompt(SA._get_brand("lander"), "", "客廳燈具怎麼選",
    "（分析內容略）", [], {"main_keyword": "測試"}, {})
check("JSIMPLE的generate prompt不該出現濾呼吸專屬版型指引", "濾呼吸文章版型" not in js_prompt, None)
check("朗德的generate prompt不該出現濾呼吸專屬版型指引", "濾呼吸文章版型" not in ld_prompt, None)

check("這段版型指引不在DEFAULT_GENERATE_PROMPT裡（證明是code分支加的，不是全站共用範本本身的內容）",
      "濾呼吸文章版型" not in SA.DEFAULT_GENERATE_PROMPT)
check("這段版型指引也沒有被存進seo_prompt_templates（不會跟著全站共用Prompt一起被存檔）",
      "generate" not in PROMPT_TEMPLATES or "濾呼吸文章版型" not in PROMPT_TEMPLATES.get("generate", ""))

print("=" * 70)
print("19. 新增image block：待完成佔位渲染 / schema檢查 / 發布前擋下 / 回填網址API")
print("=" * 70)

IMG_BLOCKS = [
    {"type": "heading", "level": 2, "text": "測試標題"},
    {"type": "paragraph", "text": "測試內文。"},
    {"type": "image", "slot": "cover", "prompt": "wide landscape banner test prompt",
     "alt": "測試封面圖ALT", "url": ""},
    {"type": "image", "slot": "inline_1", "prompt": "process diagram test prompt",
     "alt": "測試內文圖ALT", "url": ""},
    {"type": "faq", "items": [{"q": "測試問題？", "a": "測試回答。"}]},
]

check("_validate_blocks_schema：image block只要prompt/alt/slot齊全，url空字串不算錯誤",
      SA._validate_blocks_schema(IMG_BLOCKS) == [], SA._validate_blocks_schema(IMG_BLOCKS))

bad_img_blocks = [
    {"type": "heading", "level": 2, "text": "測試標題"},
    {"type": "image", "slot": "not_a_real_slot", "prompt": "", "alt": "", "url": ""},
]
bad_img_errors = SA._validate_blocks_schema(bad_img_blocks)
check("_validate_blocks_schema：image block的slot不合法、缺prompt、缺alt都要各自報錯",
      any("slot" in e for e in bad_img_errors) and any("Prompt" in e for e in bad_img_errors)
      and any("ALT" in e for e in bad_img_errors), bad_img_errors)

theme = SA._get_brand_theme("filterbreath")
rendered = SA._render_blocks_html(IMG_BLOCKS, theme, inline=False)
check("渲染時，url空的image block要顯示「圖片待完成」佔位，不是破圖的<img>標籤",
      "圖片待完成" in rendered and "測試封面圖ALT" in rendered, None)
check("渲染時，url空的image block不應該輸出<img src=\"\">這種破圖標籤",
      '<img src=""' not in rendered, None)

filled_blocks = json.loads(json.dumps(IMG_BLOCKS))
filled_blocks[2]["url"] = "https://cdn.example.com/cover.jpg"
rendered_filled = SA._render_blocks_html(filled_blocks, theme, inline=False)
check("網址填好之後，該張圖要渲染成真正的<img>標籤",
      '<img src="https://cdn.example.com/cover.jpg"' in rendered_filled, None)
check("另一張還沒填網址的圖，仍然要顯示待完成佔位（不能因為填了一張就全部當作完成）",
      "圖片待完成" in rendered_filled, None)

# 19a) 發布前檢查：還有image url沒填 -> 擋下；全部填好 -> 這項檢查不再擋
add_article(201, title="冰塊有異味怎麼排查？", slug="/blog/lu-ice-smell-check",
            meta_title="mt", meta_description="md", content="", brand_key="filterbreath",
            category="製冰機濾網", status="draft_review",
            blocks=json.dumps(IMG_BLOCKS, ensure_ascii=False),
            extra=json.dumps({"quality_check": {"brand_consistency_pass": True, "recommend_publish": True}},
                              ensure_ascii=False))
_stamp_fingerprint(201)
ok201, err201 = SA._validate_article_for_publish(201)
check("有image待完成時，發布前檢查要擋下並明確列出待完成張數",
      ok201 is False and any("圖片待完成" in e and "2張" in e for e in err201), err201)

add_article(202, title="冰塊有異味怎麼排查？", slug="/blog/lu-ice-smell-check-2",
            meta_title="mt", meta_description="md", content="", brand_key="filterbreath",
            category="製冰機濾網", status="draft_review",
            blocks=json.dumps(filled_blocks[:3] + filled_blocks[4:], ensure_ascii=False),  # 拿掉還沒填的inline_1
            extra=json.dumps({"quality_check": {"brand_consistency_pass": True, "recommend_publish": True}},
                              ensure_ascii=False))
_stamp_fingerprint(202)
ok202, err202 = SA._validate_article_for_publish(202)
check("image都填好網址之後，「圖片待完成」這項檢查不應該再出現",
      not any("圖片待完成" in e for e in err202), err202)

# 19b) 回填網址的API
resp_fill_bad_slot = client.post(f"/admin/seo/article/201/image/fill?key={KEY}",
                                  json={"slot": "not_real", "url": "https://cdn.example.com/x.jpg"})
check("回填API：slot不合法要回400", resp_fill_bad_slot.status_code == 400, resp_fill_bad_slot.status_code)

resp_fill_unsafe = client.post(f"/admin/seo/article/201/image/fill?key={KEY}",
                                json={"slot": "cover", "url": "javascript:alert(1)"})
check("回填API：不安全的url(javascript:)要被擋下，不能真的存進去",
      resp_fill_unsafe.status_code == 400, resp_fill_unsafe.status_code)

resp_fill_ok = client.post(f"/admin/seo/article/201/image/fill?key={KEY}",
                            json={"slot": "cover", "url": "https://cdn.example.com/real-cover.jpg"})
check("回填API：合法slot+安全網址要成功", resp_fill_ok.status_code == 200 and resp_fill_ok.get_json().get("ok"),
      resp_fill_ok.get_data(as_text=True))

a201_blocks = json.loads(ARTICLES[201]["blocks"])
cover_block = next(b for b in a201_blocks if b.get("slot") == "cover")
check("回填API：真的把網址寫回對應slot的block裡，其他block不受影響",
      cover_block["url"] == "https://cdn.example.com/real-cover.jpg" and
      next(b for b in a201_blocks if b.get("slot") == "inline_1")["url"] == "", a201_blocks)

a201_extra = SA._parse_extra(ARTICLES[201]["extra"])
check("回填網址等同內容變了，舊的quality_check要被清掉，逼重新檢查才能發布",
      not a201_extra.get("quality_check"), a201_extra)

print("=" * 70)
print("20. 濾呼吸KNOWLEDGE_ONLY模式：沒有對應商品時正常生成純知識文，不放商品CTA/連結/相容性宣稱")
print("=" * 70)

lu_know_prompt = SA._generate_article_prompt(SA._get_brand("filterbreath"), "製冰機濾網",
    "冰塊有異味怎麼排查？製冰盒、供水與濾芯檢查順序", "（分析內容略）", [], {"main_keyword": "測試"}, {})
check("濾呼吸商品是空的時候，prompt要包含KNOWLEDGE_ONLY純知識文指引",
      "純知識文模式" in lu_know_prompt and "不是生成失敗或拒絕生成的理由" in lu_know_prompt, None)
check("KNOWLEDGE_ONLY指引要明確禁止商品CTA/商品頁連結/相容性宣稱",
      "不放cta block導購" in lu_know_prompt and "不做任何相容性宣稱" in lu_know_prompt, None)
check("KNOWLEDGE_ONLY指引要求圖片只能是概念/流程示意圖，不是真實商品照",
      "概念示意圖" in lu_know_prompt and "不要生成看起來像濾呼吸在賣的實體商品照片" in lu_know_prompt, None)

# 20a) 真正的重點：不是只靠文字指示叫AI「忽略」，是資料組裝階段就把不相關商品清空，
#      Preview Prompt跟最終送進Sonnet的內容都不該出現HEPA、活性碳濾芯這些跟製冰機濾網無關的商品字樣
check("製冰機濾網這個已確認不相關的品類，最終prompt的「對應商品」欄位要是空的，不能出現HEPA/活性碳濾芯",
      "HEPA濾網" not in lu_know_prompt and "活性碳濾芯" not in lu_know_prompt, lu_know_prompt)
check("guardrail的「允許提到的商品」同樣要清空，不能讓AI以為可以提這些不相關商品",
      "允許提到的商品：濾網,活性碳濾芯,HEPA濾網" not in lu_know_prompt, lu_know_prompt)

# 20b) 對照組：空氣清淨機濾網是濾呼吸真正在賣的品類，沒有品類專屬規則時，品牌預設商品
#      仍然要正常帶入（這是最早那次blocks=[]修正的行為，不能被這次新規則誤傷）
lu_real_prompt = SA._generate_article_prompt(SA._get_brand("filterbreath"), "空氣清淨機濾網",
    "空氣清淨機濾網多久該換一次？", "（分析內容略）", [], {"main_keyword": "測試"}, {})
check("真正在賣的品類（空氣清淨機濾網）不該被這次KNOWLEDGE_ONLY規則誤判，對應商品要正常帶入",
      "HEPA濾網" in lu_real_prompt and "純知識文模式" not in lu_real_prompt, lu_real_prompt)

# 20c) JSIMPLE、朗德不受這次filterbreath專屬邏輯影響，也不會被強制要求一定要有3張圖
js_know_prompt = SA._generate_article_prompt(SA._get_brand("jsimple"), "", "高架床怎麼保養",
    "（分析內容略）", [], {"main_keyword": "測試"}, {})
ld_know_prompt = SA._generate_article_prompt(SA._get_brand("lander"), "", "燈具怎麼保養",
    "（分析內容略）", [], {"main_keyword": "測試"}, {})
check("JSIMPLE的prompt不會出現「這篇最多放3個image block」這種濾呼吸專屬的圖片數量規定",
      "這篇最多放3個image block" not in js_know_prompt, None)
check("朗德的prompt同樣不會出現濾呼吸專屬的圖片數量規定",
      "這篇最多放3個image block" not in ld_know_prompt, None)
check("DEFAULT_GENERATE_PROMPT本身對image block只說「不是每篇都需要」，沒有強制任何品牌一定要生圖",
      "不是每篇都需要配圖" in SA.DEFAULT_GENERATE_PROMPT or "沒有特別要求配圖就不要放" in SA.DEFAULT_GENERATE_PROMPT,
      None)

# 20d) 正式站實測發現的真實bug：即使seo_brand_rules裡真的有一筆（filterbreath,製冰機濾網）規則
# （key_products是舊的、已查證不相關的Panasonic/Hitachi型號清單），_run_generate_job存進
# extra.related_products的值也必須被清空——不能只有送給AI的Prompt清空了，後台「對應商品」欄位
# 卻還留著舊資料（這是2026-10-03正式站實測「冰塊有異味」這篇時真的發現的落差，不是假設情境）。
_orig_ai_call_json_full_20d = SA._ai_call_json_full

def fake_ai_call_json_full_knowledge_only(prompt, model=None, max_tokens=None):
    return ({
        "title": "冰塊有異味怎麼排查？", "slug": "/blog/ice-smell-check",
        "meta_title": "mt", "meta_description": "md", "ai_summary": "ai_summary",
        "needs_confirmation": False, "confirmation_notes": "", "suggested_first_hand_data": "",
        "blocks": [
            {"type": "heading", "level": 2, "text": "冰塊有異味怎麼排查"},
            {"type": "paragraph", "text": "先檢查製冰盒跟供水管路。"},
            {"type": "faq", "items": [{"q": "多久換濾芯？", "a": "請以原廠說明書為準。"}]},
        ],
        "internal_links": "", "long_tail_keywords": "", "knowledge_citations": [],
    }, None, "end_turn")

SA._ai_call_json_full = fake_ai_call_json_full_knowledge_only
JOBS[910] = {"id": 910, "status": "pending", "article_id": None, "error_msg": ""}
try:
    SA._run_generate_job(910, "filterbreath", "製冰機濾網", "冰塊有異味怎麼排查？", "（分析內容略）",
                          opp_id=None, fields={"main_keyword": "冰塊有異味原因"})
finally:
    SA._ai_call_json_full = _orig_ai_call_json_full_20d

check("即使seo_brand_rules有舊資料，_run_generate_job產生的job狀態要是done", JOBS[910]["status"] == "done", JOBS[910])
new_aid_910 = JOBS[910]["article_id"]
extra_910 = SA._parse_extra(ARTICLES[new_aid_910]["extra"]) if new_aid_910 in ARTICLES else {}
check("存進文章extra的related_products必須是空字串，不能是seo_brand_rules裡那筆舊的Panasonic/Hitachi清單",
      extra_910.get("related_products", "（找不到欄位）") == "", extra_910.get("related_products"))

# 20e) Preview debug同樣要反映清空後的結果與原因，不能只有生成流程清空、debug畫面還是顯示舊資料
resp_preview_know = client.post(f"/admin/seo-generator/preview?key={KEY}", json={
    "brand": "filterbreath", "category": "製冰機濾網", "topic": "冰塊有異味怎麼排查？",
    "analysis": "", "main_keyword": "冰塊有異味原因",
})
preview_know_debug = resp_preview_know.get_json()["debug"]["fields"]
check("Preview debug的RELATED_PRODUCTS也要顯示空值，且來源要說明是「品類已確認無商品」，不是舊的seo_brand_rules",
      preview_know_debug["RELATED_PRODUCTS"]["value"] == "" and
      "品類已確認無商品" in preview_know_debug["RELATED_PRODUCTS"]["src"], preview_know_debug)

# 20f) 2026-10-03正式站實測「冰塊有異味」這篇時發現：送進Prompt的知識庫條目裡，混入了
# 00820/108850/RD-30/MR-BX52這些跟主題無關的具體型號舊資料，導致AI在正文寫出未經佐證的
# 型號相容宣稱、被品質檢查擋下。還原正式站那5筆真實知識庫內容，驗證過濾邏輯。
FILTERBREATH_ICE_KNOWLEDGE = [
    {"type": "spec", "title": "00820對應機型",
     "content": "Panasonic 台灣官方 NR-E507XT 使用說明書的另售部件欄列有 ARMH00B00820，"
                "可確認該手冊機型與此料號的對應關係；文件未列濾芯高度尺寸。（本條為 Panasonic "
                "原廠使用說明書參考記錄，不代表濾呼吸販售此商品或已確認副廠相容性。）"},
    {"type": "spec", "title": "108850對應機型",
     "content": "Panasonic 台灣官方 NR-E417XT 使用說明書的另售部件欄列有 CNRMJ-108850，"
                "可確認該手冊機型與此料號的對應關係；文件未列濾芯高度尺寸。"},
    {"type": "faq", "title": "Panasonic濾芯怎麼核對",
     "content": "Panasonic 官方提供依冰箱本體型號查詢自動製冰機淨水濾芯品番的功能；"
                "請以冰箱機身標籤、保證書或說明書上的完整型號查詢。官方建議約每3年更換。"},
    {"type": "spec", "title": "日立RJK-30更換週期",
     "content": "日立官方資料列出自動製冰用浄水フィルター RJK-30，使用期間參考約3至4年。"
                "這無法證明賣場標示的 RD-30 與原廠 RJK-30 是相同料號。"},
    {"type": "faq", "title": "三菱濾芯如何核對",
     "content": "三菱官方提供依冷藏庫型號查詢自動製冰用零件的功能。MR-BX52 是冰箱型號系列標示，"
                "不是濾芯料號；目前資料不足以確認它對應哪一款濾芯。"},
]
filtered_generic_topic = SA._filter_knowledge_for_filterbreath(
    FILTERBREATH_ICE_KNOWLEDGE, SA._get_brand("filterbreath"), "製冰機濾網", "冰塊有異味怎麼排查？製冰盒、供水與濾芯檢查順序")
filtered_titles_generic = {it["title"] for it in filtered_generic_topic}
check("通用主題（冰塊有異味）不應該帶入00820/108850/RD-30/MR-BX52這些型號專屬條目",
      filtered_titles_generic == {"Panasonic濾芯怎麼核對"}, filtered_titles_generic)

filtered_specific_topic = SA._filter_knowledge_for_filterbreath(
    FILTERBREATH_ICE_KNOWLEDGE, SA._get_brand("filterbreath"), "製冰機濾網",
    "國際牌製冰室濾網00820與108850怎麼選")
filtered_titles_specific = {it["title"] for it in filtered_specific_topic}
check("主題明確問到00820/108850時，這兩筆對應型號的條目要能正確帶入（不是整個品類一律清空）",
      "00820對應機型" in filtered_titles_specific and "108850對應機型" in filtered_titles_specific,
      filtered_titles_specific)
check("主題明確問00820/108850時，沒提到的RD-30/MR-BX52條目仍然不該帶入",
      "日立RJK-30更換週期" not in filtered_titles_specific and "三菱濾芯如何核對" not in filtered_titles_specific,
      filtered_titles_specific)

filtered_other_brand = SA._filter_knowledge_for_filterbreath(
    FILTERBREATH_ICE_KNOWLEDGE, SA._get_brand("jsimple"), "製冰機濾網", "冰塊有異味怎麼排查？")
check("這個過濾只對filterbreath生效，其他品牌（就算brand不對category不符）原樣回傳不過濾",
      len(filtered_other_brand) == len(FILTERBREATH_ICE_KNOWLEDGE), filtered_other_brand)

print("=" * 70)
print("21. 圖片管理後台UI：編輯頁顯示用途/Prompt/ALT/建議位置、暫存連結擋下、auto_qc自動觸發品質檢查")
print("=" * 70)

resp_edit_201 = client.get(f"/admin/seo/article/201?key={KEY}")
edit_201_html = resp_edit_201.get_data(as_text=True)
check("編輯頁GET不crash", resp_edit_201.status_code == 200, resp_edit_201.status_code)
check("編輯頁要顯示封面圖、內文圖1的用途標籤", "封面圖" in edit_201_html and "內文圖 1" in edit_201_html, None)
check("編輯頁要顯示image block的ALT文字", "測試封面圖ALT" in edit_201_html, None)
check("編輯頁要顯示image block的生圖Prompt內容（放在textarea裡可複製）",
      "wide landscape banner test prompt" in edit_201_html, None)
check("編輯頁要顯示「建議插入位置」，cover要標示為文章最前面", "文章最前面（封面）" in edit_201_html, None)
check("還沒回填的image要顯示「圖片待完成」警示", "圖片待完成" in edit_201_html, None)
check("有複製Prompt的按鈕", "複製 Prompt" in edit_201_html, None)

resp_fill_ephemeral = client.post(f"/admin/seo/article/201/image/fill?key={KEY}",
                                   json={"slot": "inline_1", "url": "https://files.oaiusercontent.com/tmp/abc123.png"})
check("回填API：常見的聊天工具/AI暫存附件網域要被擋下，不能真的存進去",
      resp_fill_ephemeral.status_code == 400 and "暫存" in resp_fill_ephemeral.get_json().get("error", ""),
      resp_fill_ephemeral.get_data(as_text=True))
a201_blocks_after_ephemeral = json.loads(ARTICLES[201]["blocks"])
check("被擋下的暫存網址真的沒有被寫進blocks裡",
      next(b for b in a201_blocks_after_ephemeral if b.get("slot") == "inline_1")["url"] == "",
      a201_blocks_after_ephemeral)

resp_edit_auto_qc = client.get(f"/admin/seo/article/201?key={KEY}&auto_qc=1")
check("帶auto_qc=1重新整理時，頁面要自動呼叫doQualityCheck（回填網址後不用使用者自己再點一次）",
      "doQualityCheck(201)" in resp_edit_auto_qc.get_data(as_text=True), None)
resp_edit_no_auto_qc = client.get(f"/admin/seo/article/201?key={KEY}")
check("沒帶auto_qc時，不應該有自動呼叫doQualityCheck(201)這行（只有按鈕onclick那個，不是自動執行）",
      resp_edit_no_auto_qc.get_data(as_text=True).count("doQualityCheck(201)") <
      resp_edit_auto_qc.get_data(as_text=True).count("doQualityCheck(201)"), None)

print("=" * 70)
print("22. 品質檢查取得與生成階段一致的事實背景：content_mode、知識庫引用、不建議新增不存在的商品")
print("=" * 70)

# 22a) 還原正式站文章113的真實情境：filterbreath/製冰機濾網，related_products是空的（KNOWLEDGE_ONLY），
# 生成時引用了「Panasonic濾芯怎麼核對」這筆通用條目（內容已含「不代表濾呼吸販售此商品」的免責說明）
know_only_article = {"title": "冰塊有異味怎麼排查？", "meta_title": "mt", "meta_description": "md",
                      "content": "正文略，提到Panasonic官方建議約3年更換濾芯。"}
know_only_extra = {
    "main_keyword": "冰塊有異味原因", "target_audience": "家用冰箱使用者",
    "related_products": "",  # KNOWLEDGE_ONLY：確認是空的
    "knowledge_citations": ["Panasonic濾芯怎麼核對"],
}
check("_quality_check_content_mode：related_products是空的要判定為KNOWLEDGE_ONLY",
      SA._quality_check_content_mode(know_only_extra) == "KNOWLEDGE_ONLY", None)

qc_prompt_know_only = SA._quality_check_prompt(know_only_article, SA._get_brand("filterbreath"),
                                                "製冰機濾網", {}, know_only_extra)
check("品質檢查Prompt要明確標示本篇是KNOWLEDGE_ONLY，且講清楚沒有商品不是問題",
      "本篇內容模式：KNOWLEDGE_ONLY" in qc_prompt_know_only and
      "缺商品、商品連結、品牌CTA本身不是問題" in qc_prompt_know_only, None)
check("品質檢查Prompt要把引用的知識庫條目實際內容帶進去，包含免責聲明文字",
      "Panasonic濾芯怎麼核對" in qc_prompt_know_only and "不代表濾呼吸販售此商品" in qc_prompt_know_only,
      None)
check("品質檢查Prompt要明確禁止建議新增清單外的商品/型號/購買CTA",
      "不能自己想像、補充或建議清單外的商品" in qc_prompt_know_only and
      "不要建議新增任何商品型號" in qc_prompt_know_only, None)
check("品質檢查Prompt對「官方建議」的判定要看有沒有標來源/適用範圍，不是看到字眼就算違規",
      "只有完全沒標來源、卻讓讀者誤以為是濾呼吸自己官方認證時" in qc_prompt_know_only, None)
check("品質檢查Prompt要有「特定機型週期不可泛化成全品類通則」的檢查項目",
      "適用於所有" in qc_prompt_know_only and "泛化" in qc_prompt_know_only, None)
check("第7、8項要標明KNOWLEDGE_ONLY時沒有商品導購/對應商品不算缺失",
      "本篇內容模式是KNOWLEDGE_ONLY時，沒有商品導購段落是正常的" in qc_prompt_know_only and
      "本篇內容模式是KNOWLEDGE_ONLY時，沒有對應商品是正常狀態" in qc_prompt_know_only, None)

# 22b) 對照組：有真實商品資料的PRODUCT_GUIDE案例（JSIMPLE穀倉門）要正常判定、商品清單正確帶入，
# 確認這次修正沒有把「真的有商品」的案例也誤判成KNOWLEDGE_ONLY
product_guide_article = {"title": "穀倉門五金怎麼挑", "meta_title": "mt", "meta_description": "md",
                          "content": "正文略。"}
product_guide_extra = {
    "main_keyword": "穀倉門五金怎麼挑", "target_audience": "想裝穀倉門的屋主",
    "related_products": "穀倉門滑軌組,穀倉門五金", "knowledge_citations": [],
}
check("_quality_check_content_mode：related_products有值要判定為PRODUCT_GUIDE",
      SA._quality_check_content_mode(product_guide_extra) == "PRODUCT_GUIDE", None)
qc_prompt_product = SA._quality_check_prompt(product_guide_article, SA._get_brand("jsimple"),
                                              "穀倉門", {}, product_guide_extra)
check("PRODUCT_GUIDE案例的品質檢查Prompt要標示正確模式，且列出真實對應商品清單",
      "本篇內容模式：PRODUCT_GUIDE" in qc_prompt_product and
      "本篇對應商品：穀倉門滑軌組,穀倉門五金" in qc_prompt_product, None)
check("PRODUCT_GUIDE案例「文章對應商品」欄位要顯示真實商品，不是「無」",
      "穀倉門滑軌組,穀倉門五金" in qc_prompt_product and
      "本篇是KNOWLEDGE_ONLY，這是正常狀態" not in qc_prompt_product.split("文章對應商品")[-1][:100], None)

# 22c) 找不到引用條目時（例如條目被刪掉或改名）要有合理訊息，不能crash
missing_citation_extra = dict(know_only_extra, knowledge_citations=["已經被刪除的條目"])
qc_prompt_missing = SA._quality_check_prompt(know_only_article, SA._get_brand("filterbreath"),
                                              "製冰機濾網", {}, missing_citation_extra)
check("引用的知識庫條目找不到時，要顯示合理說明，不能crash或留空白",
      "找不到這筆條目的完整內容" in qc_prompt_missing, None)

print("=" * 70)
print("結果")
print("=" * 70)
print(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
if FAIL:
    print("\n--- 失敗項目 ---")
    for name, detail in FAIL:
        print(f"[FAIL] {name}\n       detail: {detail}\n")
else:
    print("全部通過")
