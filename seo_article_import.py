"""
seo_article_import.py — 批次匯入舊文章 Blueprint
把官網既有文章標題寫入 seo_articles，避免 AI 生成時重複主題。
不依賴 seo_admin.py，完全獨立。
"""
import os, time
import psycopg2
from flask import Blueprint, request, render_template, redirect

DATABASE_URL   = os.getenv("DATABASE_URL", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "jsimple2024")

seo_article_import_bp = Blueprint("seo_article_import", __name__)

_SKIP_TITLES = {"標題", "文章", "部落格", "title", "article", "blog"}

SIDEBAR_ITEMS = [
    ("home",               "🏠 後台首頁",       "/admin"),
    ("seo-dashboard",      "📊 SEO 營運中心",   "/admin/seo-dashboard"),
    ("seo",                "📝 文章管理",       "/admin/seo"),
    ("seo-opportunities",  "🎯 主題機會池",     "/admin/seo-opportunities"),
    ("seo-generator",      "✨ AI 生成文章",    "/admin/seo-generator"),
    ("seo-knowledge",      "📚 知識庫管理",     "/admin/seo-knowledge"),
    ("seo-brand-rules",    "🏷️ 品牌SEO規則",   "/admin/seo-brand-rules"),
    ("seo-keyword-map",    "🗺️ 關鍵字地圖",    "/admin/seo/keyword-map"),
    ("seo-settings",       "⚙️ Prompt 設定",   "/admin/seo-settings"),
    ("seo-article-import", "📥 批次匯入舊文章", "/admin/seo-article-import"),
]


def _check_auth():
    key = request.args.get("key", "")
    return key == ADMIN_PASSWORD, key


def _pg_conn():
    return psycopg2.connect(DATABASE_URL)


def _get_brands():
    if not DATABASE_URL:
        return [{"brand_key": "jsimple", "name": "JSIMPLE"}]
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT brand_key, name FROM brand_profiles WHERE enabled = TRUE ORDER BY name")
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"brand_key": r[0], "name": r[1]} for r in rows] or [{"brand_key": "jsimple", "name": "JSIMPLE"}]
    except Exception:
        return [{"brand_key": "jsimple", "name": "JSIMPLE"}]


def _parse_lines(raw: str) -> list[str]:
    """回傳清理後的標題清單（已剝除 URL 部分）。"""
    titles = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # 格式 B：標題 | URL
        if "|" in line:
            line = line.split("|")[0].strip()
        if not line:
            continue
        titles.append(line)
    return titles


def _should_skip(title: str) -> bool:
    if len(title) < 4:
        return True
    if title.strip() in _SKIP_TITLES:
        return True
    return False


def _import_titles(brand: str, category: str, titles: list[str]) -> dict:
    inserted = updated = skipped = failed = 0
    errors = []

    if not DATABASE_URL:
        return {"inserted": 0, "updated": 0, "skipped": len(titles),
                "failed": 0, "errors": ["DATABASE_URL 未設定"]}

    try:
        conn = _pg_conn()
        cur = conn.cursor()
    except Exception as e:
        return {"inserted": 0, "updated": 0, "skipped": 0,
                "failed": len(titles), "errors": [f"DB 連線失敗：{e}"]}

    now = time.time()

    for title in titles:
        if _should_skip(title):
            skipped += 1
            continue
        try:
            cur.execute(
                "SELECT id FROM seo_articles WHERE brand_key = %s AND title = %s",
                (brand, title)
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE seo_articles SET category=%s, status=%s, updated_at=%s WHERE id=%s",
                    (category, "imported", now, row[0])
                )
                updated += 1
            else:
                cur.execute(
                    """INSERT INTO seo_articles
                       (title, brand_key, category, content, status, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (title, brand, category, "", "imported", now, now)
                )
                inserted += 1
        except Exception as e:
            failed += 1
            errors.append(f"{title[:30]}… — {e}")

    try:
        conn.commit()
    except Exception as e:
        errors.append(f"commit 失敗：{e}")
    finally:
        cur.close()
        conn.close()

    return {"inserted": inserted, "updated": updated,
            "skipped": skipped, "failed": failed, "errors": errors}


@seo_article_import_bp.route("/admin/seo-article-import", methods=["GET"])
def import_page():
    ok, key = _check_auth()
    if not ok:
        return redirect(f"/admin/seo?key=")
    return render_template(
        "seo_article_import.html",
        key=key,
        sidebar_items=SIDEBAR_ITEMS,
        active="seo-article-import",
        brands=_get_brands(),
        result=None,
        form_brand="jsimple",
        form_category="",
        form_titles="",
    )


@seo_article_import_bp.route("/admin/seo-article-import", methods=["POST"])
def import_submit():
    ok, key = _check_auth()
    if not ok:
        return redirect(f"/admin/seo?key=")

    brand    = request.form.get("brand", "jsimple").strip()
    category = request.form.get("category", "").strip()
    raw      = request.form.get("titles", "")

    titles = _parse_lines(raw)
    result = _import_titles(brand, category, titles)

    return render_template(
        "seo_article_import.html",
        key=key,
        sidebar_items=SIDEBAR_ITEMS,
        active="seo-article-import",
        brands=_get_brands(),
        result=result,
        form_brand=brand,
        form_category=category,
        form_titles=raw,
    )
