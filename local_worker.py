"""
J SIMPLE 本機商品爬取 Worker
==============================
架構：本機 Playwright + Chrome  →  Render Flask API

需求：
    pip install playwright requests rembg pillow
    playwright install  （使用本機 Chrome，不需下載 Chromium）

啟動流程：
    1. 執行 start_chrome_debug.bat  →  Chrome 以 debug 模式啟動
    2. 在 Chrome 登入 1688 / 淘寶
    3. 執行 start_worker.bat  →  本機 Worker 開始輪詢
"""

import re, json, time, sys, io
import requests
from playwright.sync_api import sync_playwright

# ── 設定（根據需要修改）──────────────────────────────────────
SERVER_URL    = "https://jsimple-linebot.onrender.com"
API_KEY       = "jsimple2024"
CHROME_CDP    = "http://localhost:9222"   # Chrome debug port
POLL_SEC      = 5                          # 輪詢間隔（秒）
PAGE_WAIT     = 3000                       # 頁面載入等待（ms）
SUPABASE_URL  = "https://lrslleetqyaerstrlbap.supabase.co"
SUPABASE_KEY  = ""   # ← 填入 Render 環境變數 SUPABASE_SERVICE_KEY 的值
SUPABASE_BUCKET = "chat-images"
ENABLE_REMBG  = True   # 設為 False 可跳過去背，只做白底
# ────────────────────────────────────────────────────────────


# ── Server API 工具 ──────────────────────────────────────────

def api_get(path):
    r = requests.get(f"{SERVER_URL}{path}?key={API_KEY}", timeout=10)
    r.raise_for_status()
    return r.json()

def api_post(path, data):
    r = requests.post(
        f"{SERVER_URL}{path}?key={API_KEY}",
        json=data, timeout=20
    )
    r.raise_for_status()
    return r.json()

def get_pending_jobs():
    return api_get("/api/products/pending").get("jobs", [])

def post_result(job_id, data):
    return api_post(f"/api/products/{job_id}/scrape-result", data)


# ── 圖片工具 ─────────────────────────────────────────────────

_SKIP_IMG = re.compile(
    r'icon|logo|_\d{2}x\d{2}[_.]|_30x|_50x|_60x|_80x|\.ico$', re.I
)
_PRIORITY_IMG = re.compile(
    r'_800x|_790x|_750x|_600x|imgextra|mainimg', re.I
)
_ALICDN = re.compile(
    r'(?:https?:)?//[^"\'<>\s]*?\.alicdn\.com/[^"\'<>\s]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\'<>\s]*)?',
    re.I
)

def extract_alicdn_imgs(html):
    return _ALICDN.findall(html)

def clean_images(urls, max_count=10):
    seen, result = set(), []
    for u in urls:
        u = u.strip()
        if not u:
            continue
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith("http"):
            continue
        if _SKIP_IMG.search(u):
            continue
        if u not in seen:
            seen.add(u)
            result.append(u)
    priority = [u for u in result if _PRIORITY_IMG.search(u)]
    others   = [u for u in result if not _PRIORITY_IMG.search(u)]
    return (priority + others)[:max_count]


# ── 圖片處理：去背 + 白底 + 上傳 Supabase ────────────────────

def _remove_bg(img_bytes):
    """用 rembg 去除背景，回傳 RGBA bytes 或 None。"""
    if not ENABLE_REMBG:
        return None
    try:
        from rembg import remove
        return remove(img_bytes)
    except ImportError:
        print("  [提示] rembg 未安裝，跳過去背。執行：pip install rembg")
        return None
    except Exception as e:
        print(f"  [去背失敗] {e}")
        return None

def _to_white_bg(img_bytes, nobg_bytes=None, size=800):
    """
    去背後加白底，正方形 800×800，JPEG 壓縮。
    nobg_bytes: rembg 處理後的 RGBA bytes（可選）
    img_bytes:  原始圖片 bytes（fallback）
    """
    try:
        from PIL import Image
        src_bytes = nobg_bytes if nobg_bytes else img_bytes
        img = Image.open(io.BytesIO(src_bytes)).convert("RGBA")
        canvas = Image.new("RGB", (size, size), (255, 255, 255))
        img.thumbnail((size, size), Image.LANCZOS)
        offset = ((size - img.width) // 2, (size - img.height) // 2)
        canvas.paste(img, offset, mask=img.split()[3])
        out = io.BytesIO()
        canvas.save(out, format="JPEG", quality=88, optimize=True)
        return out.getvalue()
    except Exception as e:
        print(f"  [白底失敗] {e}")
        return None

def _upload_to_supabase(filename, data, content_type="image/jpeg"):
    """上傳到 Supabase Storage，回傳公開 URL 或 None。"""
    if not SUPABASE_KEY:
        return None
    try:
        url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
        r = requests.put(url, data=data, headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": content_type,
            "x-upsert": "true",
        }, timeout=20)
        if r.status_code in (200, 201):
            return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
        return None
    except Exception:
        return None

def process_images(job_id, raw_images):
    """下載原始圖片 → 去背 → 白底 → 上傳，回傳已處理 URL 列表。"""
    if not raw_images:
        return []
    processed = []
    print(f"  處理圖片（共 {len(raw_images)} 張）...")
    for i, url in enumerate(raw_images[:8]):
        try:
            r = requests.get(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://www.1688.com/"}, timeout=12)
            if r.status_code != 200:
                continue
            img_bytes = r.content
            nobg = _remove_bg(img_bytes)
            result = _to_white_bg(img_bytes, nobg)
            if not result:
                continue
            filename = f"products/{job_id}_{i+1:02d}.jpg"
            pub_url = _upload_to_supabase(filename, result)
            if pub_url:
                processed.append(pub_url)
                print(f"    ✓ 圖{i+1} 上傳完成")
            else:
                print(f"    - 圖{i+1} 上傳跳過（未設定 SUPABASE_KEY）")
        except Exception as e:
            print(f"    ✗ 圖{i+1} 失敗: {e}")
    return processed

# ── 1688 爬取 ────────────────────────────────────────────────

def scrape_1688(page, url):
    result = {
        "raw_title": "", "raw_price": "", "raw_desc": "",
        "raw_images": [], "raw_extra": {}
    }

    print("    開啟頁面...")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(PAGE_WAIT)

    # 標題
    for sel in [".title-text", ".mod-detail-title h1", "h1"]:
        try:
            t = page.text_content(sel, timeout=2000)
            if t and t.strip():
                result["raw_title"] = t.strip()
                break
        except Exception:
            pass
    if not result["raw_title"]:
        result["raw_title"] = page.title()
    for suffix in ["-1688.com", "- 1688", "阿里巴巴找货", "_阿里巴巴"]:
        result["raw_title"] = result["raw_title"].replace(suffix, "").strip()

    # 價格
    for sel in [".price-value", ".m-price .price", ".price-content"]:
        try:
            p = page.text_content(sel, timeout=1500)
            if p and p.strip():
                result["raw_price"] = p.strip()
                break
        except Exception:
            pass

    # 圖片（alicdn regex 掃 HTML）
    html = page.content()
    result["raw_images"] = clean_images(extract_alicdn_imgs(html))

    # SKU / 規格（從 window.__INIT_DATA__ 抓）
    try:
        extra = page.evaluate("""() => {
            try {
                const d = window.__INIT_DATA__ || {};
                const offer = d.offerDetail || d.detail || {};
                return {
                    sku:   offer.skuModel?.skuProps   || null,
                    specs: offer.attribute?.attributes || null,
                };
            } catch(e) { return {}; }
        }""")
        if extra:
            result["raw_extra"].update({k: v for k, v in extra.items() if v})
    except Exception:
        pass

    # 描述
    for sel in [".mod-detail-description", ".detail-desc-content", ".description-content"]:
        try:
            d = page.text_content(sel, timeout=2000)
            if d and d.strip():
                result["raw_desc"] = d.strip()[:3000]
                break
        except Exception:
            pass

    return result


# ── 淘寶爬取 ─────────────────────────────────────────────────

def scrape_taobao(page, url):
    result = {
        "raw_title": "", "raw_price": "", "raw_desc": "",
        "raw_images": [], "raw_extra": {}
    }

    print("    開啟頁面...")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(PAGE_WAIT)

    # 標題（淘寶 class 名稱常動態產生，多嘗試幾種）
    for sel in ["h1.mainTitle", ".main-title", "h1", ".ItemHeader--title"]:
        try:
            t = page.text_content(sel, timeout=2000)
            if t and t.strip():
                result["raw_title"] = t.strip()
                break
        except Exception:
            pass
    if not result["raw_title"]:
        t = page.title()
        result["raw_title"] = t.replace("- 淘宝网", "").replace("- 淘寶網", "").strip()

    # 價格
    for sel in [".tb-rmb", ".price--5SQHM", ".ItemPrice--price"]:
        try:
            p = page.text_content(sel, timeout=1500)
            if p and p.strip():
                result["raw_price"] = p.strip()
                break
        except Exception:
            pass

    # 圖片
    html = page.content()
    result["raw_images"] = clean_images(extract_alicdn_imgs(html))

    # SKU
    try:
        extra = page.evaluate("""() => {
            try {
                const d = window.__INIT_DATA__ || window.g_page_config || {};
                return { sku: d.skuProps || d.sku || null };
            } catch(e) { return {}; }
        }""")
        if extra:
            result["raw_extra"].update({k: v for k, v in extra.items() if v})
    except Exception:
        pass

    # 描述
    for sel in [".descContainer", ".J_DescContent", ".desc-content"]:
        try:
            d = page.text_content(sel, timeout=2000)
            if d and d.strip():
                result["raw_desc"] = d.strip()[:3000]
                break
        except Exception:
            pass

    return result


# ── 主迴圈 ───────────────────────────────────────────────────

def run():
    print("=" * 52)
    print("  J SIMPLE 本機商品爬取 Worker")
    print(f"  Server : {SERVER_URL}")
    print(f"  Chrome : {CHROME_CDP}")
    print(f"  輪詢間隔 : {POLL_SEC} 秒")
    print("=" * 52)

    # 連線 Chrome
    print("\n[1/2] 連線到 Chrome...")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(CHROME_CDP)
            ctx_count = len(browser.contexts)
            print(f"      已連線，{ctx_count} 個 context")
        except Exception as e:
            print(f"\n[錯誤] 無法連線 Chrome: {e}")
            print("請先執行 start_chrome_debug.bat 再重試")
            sys.exit(1)

        context = browser.contexts[0] if browser.contexts else browser.new_context()

        # 測試 Server 連線
        print("[2/2] 測試 Server 連線...")
        try:
            api_get("/api/products/pending")
            print(f"      Server OK\n")
        except Exception as e:
            print(f"\n[錯誤] 無法連線 Server: {e}")
            sys.exit(1)

        print(f"開始輪詢（Ctrl+C 停止）\n")
        idle_dots = 0

        while True:
            try:
                jobs = get_pending_jobs()

                if jobs:
                    idle_dots = 0
                    for job in jobs:
                        job_id   = job["id"]
                        url      = job["url"]
                        platform = job["platform"]

                        print(f"[任務 #{job_id}] {platform.upper()}")
                        print(f"  URL: {url[:72]}...")

                        page = context.new_page()
                        try:
                            if platform == "1688":
                                data = scrape_1688(page, url)
                            elif platform == "taobao":
                                data = scrape_taobao(page, url)
                            else:
                                print(f"  [略過] 不支援平台: {platform}")
                                continue

                            # 序列化 raw_extra
                            data["raw_extra"] = json.dumps(
                                data.get("raw_extra", {}), ensure_ascii=False
                            )

                            # 圖片處理（去背 + 白底 + 上傳）
                            raw_imgs = data.get("raw_images", [])
                            if raw_imgs and SUPABASE_KEY:
                                processed = process_images(job_id, raw_imgs)
                                data["processed_images"] = processed
                            else:
                                data["processed_images"] = []

                            r = post_result(job_id, data)
                            if r.get("ok"):
                                title_preview = data["raw_title"][:30] or "(無標題)"
                                proc_count = len(data.get("processed_images", []))
                                print(f"  ✓ 完成：{title_preview} → AI 改寫中（已處理圖片 {proc_count} 張）")
                            else:
                                print(f"  ✗ 回傳失敗: {r}")

                        except Exception as e:
                            print(f"  [錯誤] {e}")
                            try:
                                post_result(job_id, {"error": str(e)})
                            except Exception:
                                pass
                        finally:
                            page.close()
                else:
                    idle_dots += 1
                    print("." if idle_dots % 60 != 0 else f"\n等待中（已 {idle_dots * POLL_SEC}s）",
                          end="", flush=True)

                time.sleep(POLL_SEC)

            except KeyboardInterrupt:
                print("\n\n已停止")
                break
            except Exception as e:
                print(f"\n[輪詢錯誤] {e}")
                time.sleep(POLL_SEC)


if __name__ == "__main__":
    run()
