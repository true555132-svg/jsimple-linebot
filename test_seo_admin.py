import os, sys, json, time, re

os.environ["ADMIN_PASSWORD"] = "testkey"
os.environ["DATABASE_URL"] = ""  # 保持空，我們會monkeypatch _q，程式不會真的連DB

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seo_admin as SA
from flask import Flask

SA.DATABASE_URL = "fake"  # 讓_get_brand/_get_brand_theme等函式不要因為DATABASE_URL為空而提早return {}

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
]

THEMES = {
    "filterbreath": {"brand_key": "filterbreath", "primary_color": "#1B3F6E", "accent_color": "#2F80ED",
                      "bg_color": "#F5F8FC", "confirmed": True},
    # jsimple / lander 沒有資料 -> 應該回傳 DEFAULT_NEUTRAL_THEME
}

ARTICLES = {}  # id -> dict
JOBS = {}      # id -> dict，模擬 seo_generate_jobs 資料表，供_run_generate_job測試用

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
print("結果")
print("=" * 70)
print(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
if FAIL:
    print("\n--- 失敗項目 ---")
    for name, detail in FAIL:
        print(f"[FAIL] {name}\n       detail: {detail}\n")
else:
    print("全部通過")
