"""
J SIMPLE 本機商品爬取 Worker v2
================================
不需要手動啟動 Chrome debug 模式。Worker 自己管理瀏覽器。

第一次使用（登入）：
    python local_worker.py --login
    → 開啟瀏覽器，手動登入 1688 / 淘寶，完成後回來按 Enter

之後正常使用：
    python local_worker.py
    → 自動使用已儲存的登入狀態，開始輪詢後台任務

安裝需求：
    pip install playwright requests rembg pillow
    playwright install chromium
"""

import sys, re, json, time, io
import requests
from playwright.sync_api import sync_playwright
from pathlib import Path

# ── 設定（根據需要修改）──────────────────────────────────────
SERVER_URL      = "https://jsimple-linebot.onrender.com"
API_KEY         = "jsimple2024"
PROFILE_DIR     = str(Path.home() / "jsimple-worker-profile")  # 登入狀態儲存位置
POLL_SEC        = 5        # 輪詢間隔（秒）
PAGE_WAIT       = 3000     # 頁面載入等待（ms）
HEADLESS        = False    # False = 顯示瀏覽器視窗（方便觀察）
SUPABASE_URL    = "https://lrslleetqyaerstrlbap.supabase.co"
SUPABASE_KEY    = ""       # ← 填入 Render 環境變數 SUPABASE_SERVICE_KEY 的值
SUPABASE_BUCKET = "chat-images"
ENABLE_REMBG    = True     # False = 跳過去背，只做白底
# ────────────────────────────────────────────────────────────


# ── Server API ───────────────────────────────────────────────

def api_get(path):
    r = requests.get(f"{SERVER_URL}{path}?key={API_KEY}", timeout=10)
    r.raise_for_status()
    return r.json()

def api_post(path, data):
    r = requests.post(f"{SERVER_URL}{path}?key={API_KEY}", json=data, timeout=20)
    r.raise_for_status()
    return r.json()

def get_pending_jobs():
    return api_get("/api/products/pending").get("jobs", [])

def post_result(job_id, data):
    return api_post(f"/api/products/{job_id}/scrape-result", data)


# ── 圖片工具 ─────────────────────────────────────────────────

_SKIP = re.compile(r'icon|logo|_\d{2}x\d{2}[_.]|_30x|_50x|_60x|_80x|\.ico$', re.I)
_PRIO = re.compile(r'_800x|_790x|_750x|_600x|imgextra|mainimg', re.I)
_ALICDN = re.compile(
    r'(?:https?:)?//[^"\'<>\s]*?\.alicdn\.com/[^"\'<>\s]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\'<>\s]*)?',
    re.I
)

def extract_imgs(html):
    return _ALICDN.findall(html)

def clean_images(urls, max_count=10):
    seen, result = set(), []
    for u in urls:
        u = u.strip()
        if u.startswith("//"): u = "https:" + u
        if not u.startswith("http") or _SKIP.search(u): continue
        if u not in seen:
            seen.add(u); result.append(u)
    p = [u for u in result if _PRIO.search(u)]
    o = [u for u in result if not _PRIO.search(u)]
    return (p + o)[:max_count]


# ── 圖片處理：去背 + 白底 + 上傳 ────────────────────────────

def _remove_bg(img_bytes):
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
    try:
        from PIL import Image
        src = nobg_bytes if nobg_bytes else img_bytes
        img = Image.open(io.BytesIO(src)).convert("RGBA")
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

def _upload_supabase(filename, data):
    if not SUPABASE_KEY:
        return None
    try:
        url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
        r = requests.put(url, data=data, headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "image/jpeg",
            "x-upsert": "true",
        }, timeout=20)
        if r.status_code in (200, 201):
            return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
    except Exception:
        pass
    return None

def process_images(job_id, raw_images):
    if not raw_images:
        return []
    processed = []
    print(f"  處理圖片（{len(raw_images)} 張）...")
    for i, url in enumerate(raw_images[:8]):
        try:
            r = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.1688.com/"
            }, timeout=12)
            if r.status_code != 200:
                continue
            nobg   = _remove_bg(r.content)
            result = _to_white_bg(r.content, nobg)
            if not result:
                continue
            pub_url = _upload_supabase(f"products/{job_id}_{i+1:02d}.jpg", result)
            if pub_url:
                processed.append(pub_url)
                print(f"    ✓ 圖{i+1} 完成")
            else:
                print(f"    - 圖{i+1} 未上傳（SUPABASE_KEY 未設定）")
        except Exception as e:
            print(f"    ✗ 圖{i+1} 失敗: {e}")
    return processed


# ── 爬取：1688 ───────────────────────────────────────────────

def scrape_1688(page, url):
    result = {"raw_title": "", "raw_price": "", "raw_desc": "", "raw_images": [], "raw_extra": {}}
    print("    載入頁面...")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(PAGE_WAIT)

    for sel in [".title-text", ".mod-detail-title h1", "h1"]:
        try:
            t = page.text_content(sel, timeout=2000)
            if t and t.strip():
                result["raw_title"] = t.strip(); break
        except Exception: pass
    if not result["raw_title"]:
        result["raw_title"] = page.title()
    for s in ["-1688.com", "- 1688", "阿里巴巴找货"]:
        result["raw_title"] = result["raw_title"].replace(s, "").strip()

    for sel in [".price-value", ".m-price .price"]:
        try:
            p = page.text_content(sel, timeout=1500)
            if p and p.strip():
                result["raw_price"] = p.strip(); break
        except Exception: pass

    html = page.content()
    result["raw_images"] = clean_images(extract_imgs(html))

    try:
        extra = page.evaluate("""() => {
            try {
                const d = window.__INIT_DATA__ || {};
                const o = d.offerDetail || d.detail || {};
                return { sku: o.skuModel?.skuProps || null, specs: o.attribute?.attributes || null };
            } catch(e) { return {}; }
        }""")
        if extra:
            result["raw_extra"].update({k: v for k, v in extra.items() if v})
    except Exception: pass

    for sel in [".mod-detail-description", ".detail-desc-content"]:
        try:
            d = page.text_content(sel, timeout=2000)
            if d and d.strip():
                result["raw_desc"] = d.strip()[:3000]; break
        except Exception: pass

    return result


# ── 爬取：淘寶 ───────────────────────────────────────────────

def scrape_taobao(page, url):
    result = {"raw_title": "", "raw_price": "", "raw_desc": "", "raw_images": [], "raw_extra": {}}
    print("    載入頁面...")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(PAGE_WAIT)

    for sel in ["h1.mainTitle", ".main-title", "h1"]:
        try:
            t = page.text_content(sel, timeout=2000)
            if t and t.strip():
                result["raw_title"] = t.strip(); break
        except Exception: pass
    if not result["raw_title"]:
        result["raw_title"] = page.title().replace("- 淘宝网", "").replace("- 淘寶網", "").strip()

    for sel in [".tb-rmb", ".price--5SQHM"]:
        try:
            p = page.text_content(sel, timeout=1500)
            if p and p.strip():
                result["raw_price"] = p.strip(); break
        except Exception: pass

    html = page.content()
    result["raw_images"] = clean_images(extract_imgs(html))

    for sel in [".descContainer", ".J_DescContent"]:
        try:
            d = page.text_content(sel, timeout=2000)
            if d and d.strip():
                result["raw_desc"] = d.strip()[:3000]; break
        except Exception: pass

    return result


# ── 登入模式 ─────────────────────────────────────────────────

def login_mode(pw):
    print("\n" + "=" * 52)
    print("  【登入模式】")
    print(f"  Profile 位置：{PROFILE_DIR}")
    print("=" * 52)
    print("\n瀏覽器即將開啟，請：")
    print("  1. 登入 1688（https://login.1688.com）")
    print("  2. 登入 淘寶（https://login.taobao.com）")
    print("  3. 完成後回到這個視窗按 Enter\n")

    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()
    page.goto("https://login.1688.com", wait_until="domcontentloaded")

    input("登入完成後按 Enter...")
    context.close()
    print("\n✓ 登入狀態已儲存")
    print("之後直接執行 python local_worker.py 即可\n")


# ── 主迴圈 ───────────────────────────────────────────────────

def run(pw):
    print("=" * 52)
    print("  J SIMPLE 本機商品爬取 Worker v2")
    print(f"  Server  : {SERVER_URL}")
    print(f"  Profile : {PROFILE_DIR}")
    print(f"  輪詢間隔 : {POLL_SEC} 秒")
    print("=" * 52)

    print("\n[1/2] 啟動瀏覽器...")
    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=HEADLESS,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 800},
    )
    print("      瀏覽器已啟動")

    print("[2/2] 測試 Server 連線...")
    try:
        api_get("/api/products/pending")
        print("      Server OK\n")
    except Exception as e:
        print(f"\n[錯誤] 無法連線 Server: {e}")
        context.close(); return

    print("開始輪詢（Ctrl+C 停止）\n")
    idle = 0

    while True:
        try:
            jobs = get_pending_jobs()
            if jobs:
                idle = 0
                for job in jobs:
                    job_id, url, platform = job["id"], job["url"], job["platform"]
                    print(f"[任務 #{job_id}] {platform.upper()} {url[:65]}...")

                    page = context.new_page()
                    try:
                        if platform == "1688":
                            data = scrape_1688(page, url)
                        elif platform == "taobao":
                            data = scrape_taobao(page, url)
                        else:
                            print(f"  [略過] 不支援: {platform}"); continue

                        data["raw_extra"] = json.dumps(data.get("raw_extra", {}), ensure_ascii=False)

                        if data.get("raw_images") and SUPABASE_KEY:
                            data["processed_images"] = process_images(job_id, data["raw_images"])
                        else:
                            data["processed_images"] = []

                        r = post_result(job_id, data)
                        if r.get("ok"):
                            title = data["raw_title"][:28] or "(無標題)"
                            imgs  = len(data.get("processed_images", []))
                            print(f"  ✓ {title} → AI 改寫中（處理圖片 {imgs} 張）")
                        else:
                            print(f"  ✗ 回傳失敗: {r}")

                    except Exception as e:
                        print(f"  [錯誤] {e}")
                        try: post_result(job_id, {"error": str(e)})
                        except Exception: pass
                    finally:
                        page.close()
            else:
                idle += 1
                print("." if idle % 60 != 0 else f"\n等待中（{idle * POLL_SEC}s）",
                      end="", flush=True)

            time.sleep(POLL_SEC)

        except KeyboardInterrupt:
            print("\n\n已停止")
            break
        except Exception as e:
            print(f"\n[輪詢錯誤] {e}")
            time.sleep(POLL_SEC)

    context.close()


# ── 入口 ─────────────────────────────────────────────────────

if __name__ == "__main__":
    login = "--login" in sys.argv
    with sync_playwright() as pw:
        if login:
            login_mode(pw)
        else:
            if not Path(PROFILE_DIR).exists():
                print("\n[提示] 尚未登入，請先執行：python local_worker.py --login\n")
                sys.exit(0)
            run(pw)
