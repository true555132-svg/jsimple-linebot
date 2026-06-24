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
SUPABASE_KEY    = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imxyc2xsZWV0cXlhZXJzdHJsYmFwIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDQ5ODI5NywiZXhwIjoyMDk2MDc0Mjk3fQ.tL3sa7ue4NBXrh-x7Ga7jfOPCjNYFAwM-vfkMzR2dD0"       # ← 填入 Render 環境變數 SUPABASE_SERVICE_KEY 的值
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

def get_pending_scan_jobs():
    return api_get("/api/store-scan/pending").get("jobs", [])

def post_scan_result(scan_job_id, data):
    return api_post(f"/api/store-scan/{scan_job_id}/result", data)


# ── 圖片工具 ─────────────────────────────────────────────────

_SKIP = re.compile(r'icon|logo|_\d{2,3}x\d{2,3}[_.]|_30x|_50x|_60x|_80x|\.ico$|!!0-rate\.|tbvideo\.', re.I)
_PRIO = re.compile(r'_800x|_790x|_750x|_600x|mainimg', re.I)
_ALICDN = re.compile(
    r'(?:https?:)?//[^"\'<>\s]*?\.alicdn\.com/[^"\'<>\s]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\'<>\s]*)?',
    re.I
)
_TPS_SIZE = re.compile(r'-tps-(\d+)-(\d+)', re.I)

def extract_imgs(html):
    return _ALICDN.findall(html)

def _tps_ok(url, min_px=300, max_ratio=3.5):
    """imgextra URL 含 tps-WIDTH-HEIGHT 格式，小圖或極端比例直接過濾。"""
    m = _TPS_SIZE.search(url)
    if not m:
        return True
    w, h = int(m.group(1)), int(m.group(2))
    if w < min_px or h < min_px:
        return False
    if max(w, h) / min(w, h) > max_ratio:
        return False
    return True

def clean_images(urls, max_count=10):
    seen, result = set(), []
    for u in urls:
        u = u.strip()
        if u.startswith("//"): u = "https:" + u
        if not u.startswith("http") or _SKIP.search(u): continue
        if not _tps_ok(u): continue
        if u not in seen:
            seen.add(u); result.append(u)
    p = [u for u in result if _PRIO.search(u)]
    o = [u for u in result if not _PRIO.search(u)]
    return (p + o)[:max_count]

_BADGE = re.compile(
    r'wangpu|credit|level|certification|badge|guarantee|seal|'
    r'icon|logo|avatar|brand|qrcode|qr_|star|tag|label|'
    r'score|rank|medal|trophy|shield|trust|verify|auth',
    re.I
)
def clean_images_strict(urls, max_count=10):
    seen, result = set(), []
    for u in urls:
        u = u.strip()
        if u.startswith("//"): u = "https:" + u
        if not u.startswith("http"): continue
        if _SKIP.search(u) or _BADGE.search(u): continue
        if u not in seen:
            seen.add(u); result.append(u)
    p = [u for u in result if _PRIO.search(u)]
    o = [u for u in result if not _PRIO.search(u)]
    return (p + o)[:max_count]



# ── Phase 2 DOM 圖片擷取 ─────────────────────────────────────

def _parse_url_size(url):
    """從 URL 解析尺寸：tps-W-H 或 _WxH 格式。"""
    m = re.search(r'-tps-(\d+)-(\d+)', url)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r'[_-](\d{3,4})x(\d{3,4})[_.]', url)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0

def _norm_img(src, w, h):
    """標準化圖片物件；naturalWidth=0 時 fallback URL 解析尺寸。"""
    if not src:
        return None
    if src.startswith('//'):
        src = 'https:' + src
    if not src.startswith('http'):
        return None
    if w == 0 or h == 0:
        w, h = _parse_url_size(src)
    return {"src": src, "w": w, "h": h}

def _scroll_for_lazy(page, steps=12, wait_ms=500):
    """分段捲動頁面，觸發 lazy load / AJAX 詳情圖，等網路靜止再回頭。"""
    for i in range(steps + 1):
        page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {i} / {steps})")
        page.wait_for_timeout(wait_ms)
    # 等所有 AJAX 請求完成
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(1000)

# JavaScript helpers（selector 以 argument 傳入，避免 f-string 轉義問題）
_JS_PICK_IMGS = """(selector) => {
    const results = [];
    const seen = new Set();
    try {
        document.querySelectorAll(selector).forEach(img => {
            const src = img.src
                || img.dataset.src
                || img.dataset.lazyload
                || img.getAttribute('data-lazyload') || '';
            if (!src || seen.has(src)) return;
            seen.add(src);
            results.push({ src, w: img.naturalWidth || 0, h: img.naturalHeight || 0 });
        });
    } catch(e) {}
    return results;
}"""

_JS_PICK_VIDS = """(selector) => {
    const results = [];
    const seen = new Set();
    try {
        document.querySelectorAll(selector).forEach(el => {
            const src = el.src || el.getAttribute('src') || '';
            if (src && !seen.has(src)) { seen.add(src); results.push(src); }
        });
    } catch(e) {}
    return results;
}"""

_GALLERY_SEL = {
    '1688': [
        'img.preview-img',
        'img.ant-image-img.preview-img',
        '.detail-gallery-turn-box img',
        '.gallery-turn-warp img',
        '.J_Gallery img',
        '[class*="galleryItem"] img',
        '[class*="gallery-wrap"] img',
    ],
    'taobao': [
        '.J_ThumbList img',
        '.mainPicWraper img',
        '#J_ImgBooth img',
        '.tb-gallery img',
        '[class*="mainPic"] img',
        '[class*="mainImage"] img',
        '[class*="PicGallery"] img',
        '[class*="galleryItem"] img',
        '[class*="gallery"] img',
        '[class*="thumbItem"] img',
    ],
}
_DESC_SEL = {
    '1688': '.mod-detail-description img, .detail-desc-content img, .description-content img',
    'taobao': '.descContainer img, .J_DescContent img',
}
_VIDEO_SEL = 'video source[src], video[src]'

def _extract_images_dom(page, platform='1688'):
    """Phase 2 DOM 擷取：main_images / detail_images / video_urls。
    detail 三層策略：CSS selector → page.frames → page.content() regex。
    """
    out = {"main_images": [], "detail_images": [], "video_urls": []}

    # ── 主圖（輪播區，不需 scroll）───────────────────────────
    for sel in _GALLERY_SEL.get(platform, []):
        try:
            items = page.evaluate(_JS_PICK_IMGS, sel)
            cands = []
            for it in items:
                img = _norm_img(it['src'], it['w'], it['h'])
                if not img: continue
                w, h = img['w'], img['h']
                if (w >= 500 and h >= 500 and max(w, h) / min(w, h) <= 1.5) or \
                   (w == 0 and h == 0 and 'alicdn' in img['src']):
                    cands.append(img)
            if cands:
                out['main_images'] = cands
                break
        except Exception as e:
            print(f"  [main] {e}")

    # ── 詳情圖（scroll + 三層策略）──────────────────────────
    print("    scroll lazy load...")
    _scroll_for_lazy(page)

    main_srcs = {i['src'] for i in out['main_images']}

    def _add_detail(img):
        if not img or img['src'] in main_srcs:
            return
        w_, h_ = img['w'], img['h']
        # 過濾追蹤像素：已知尺寸時，寬或高小於 100 的直接跳過
        if (w_ > 0 and w_ < 100) or (h_ > 0 and h_ < 100):
            return
        if w_ >= 1000 and h_ >= 250:
            main_srcs.add(img['src'])
            out['detail_images'].append(img)

    # 策略 1：CSS selector（main frame）
    desc_sel = _DESC_SEL.get(platform, '')
    if desc_sel:
        try:
            for it in page.evaluate(_JS_PICK_IMGS, desc_sel):
                _add_detail(_norm_img(it['src'], it['w'], it['h']))
        except Exception as e:
            print(f"  [detail s1] {e}")

    # 策略 2：掃所有 frames（1688 詳情常在 iframe 內）
    if not out['detail_images']:
        for frame in page.frames[1:]:
            try:
                for it in frame.evaluate(_JS_PICK_IMGS, 'img'):
                    _add_detail(_norm_img(it['src'], it['w'], it['h']))
            except Exception:
                pass

    # 策略 3：page.content() regex
    # tps 有尺寸：w>=1000 h>=250；imgextra 無尺寸：先收下，process_images 再驗
    if not out['detail_images']:
        try:
            for u in _ALICDN.findall(page.content()):
                if u.startswith('//'): u = 'https:' + u
                if not u.startswith('http') or u in main_srcs: continue
                if _SKIP.search(u) or not _tps_ok(u): continue
                w, h = _parse_url_size(u)
                if (w >= 1000 and h >= 250) or (w == 0 and 'imgextra' in u):
                    main_srcs.add(u)
                    out['detail_images'].append({"src": u, "w": w, "h": h})
        except Exception as e:
            print(f"  [detail s3] {e}")

    # ── 影片 ────────────────────────────────────────────────
    try:
        out['video_urls'] = list(dict.fromkeys(page.evaluate(_JS_PICK_VIDS, _VIDEO_SEL)))
    except Exception as e:
        print(f"  [video] {e}")

    # 去重
    for key in ('main_images', 'detail_images'):
        seen, deduped = set(), []
        for img in out[key]:
            if img['src'] not in seen:
                seen.add(img['src']); deduped.append(img)
        out[key] = deduped

    print(f"    [OK] main={len(out['main_images'])} detail={len(out['detail_images'])} video={len(out['video_urls'])}")
    return out


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

    # 攔截 HTML 回應 URL（頁面載入期間，含 iframe / AJAX 載入的描述 HTML）
    intercepted_html_urls = []
    def _on_resp(resp):
        try:
            ct = resp.headers.get('content-type', '')
            u = resp.url
            if resp.status == 200 and any(t in ct for t in ('text/html', 'application/json', 'text/plain', 'text/javascript')) or ct == '':
                if 'login' not in u and 'blank' not in u and '/offer/' not in u:
                    intercepted_html_urls.append(u)
        except Exception:
            pass
    page.on("response", _on_resp)

    print("    載入頁面...")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(PAGE_WAIT)

    # JS 資料：標題 / 價格 / 規格 / 多 SKU 價格
    try:
        js_data = page.evaluate("""() => {
            try {
                const d = window.__INIT_DATA__ || {};
                // 多路徑搜尋 offerDetail
                const o = d.offerDetail || d.detail || d.data?.offerDetail || {};
                const base = o.baseInfo || o.offerInfo || {};

                // SKU 規格圖 + 規格名稱對照表（pid/vid -> name） — 多路徑
                const skuImgs = [];
                const seen = new Set();
                const valueNameMap = {};   // vid -> 顯示名稱（含所屬規格名）
                const addSku = (props) => {
                    (props || []).forEach(prop => {
                        (prop.values || []).forEach(v => {
                            const label = (prop.name||'') + ':' + (v.name||'');
                            if (v.vid != null) valueNameMap[v.vid] = label;
                            const img = v.image || v.imageUrl || v.imageRaw || '';
                            if (img && !seen.has(img)) {
                                seen.add(img);
                                skuImgs.push({ src: img.startsWith('//') ? 'https:' + img : img, label });
                            }
                        });
                    });
                };
                // 路徑 1: offerDetail.skuModel.skuProps
                addSku(o.skuModel?.skuProps);
                // 路徑 2: offerDetail.skuInfos (部分商品)
                addSku(o.skuInfos);
                // 路徑 3: 全域 skuProps 直接掛在 __INIT_DATA__
                addSku(d.skuProps || d.skuModel?.skuProps);
                // 路徑 4: DOM 直接讀色塊圖片 (最可靠的 fallback)
                if (skuImgs.length === 0) {
                    document.querySelectorAll('.sku-item img, .obj-sku img, [class*="sku"] img').forEach(img => {
                        const src = img.src || img.dataset.src || '';
                        if (src && !seen.has(src) && src.includes('alicdn')) {
                            seen.add(src);
                            skuImgs.push({ src, label: img.alt || '' });
                        }
                    });
                }

                // 各 SKU 組合的價格 — 多路徑（skuInfoMap: comboKey "vid1_vid2" -> {price,...}）
                const skuPrices = [];
                const infoMap = o.skuModel?.skuInfoMap || o.skuInfoMap || d.skuInfoMap || null;
                if (infoMap && typeof infoMap === 'object') {
                    Object.keys(infoMap).forEach(comboKey => {
                        const info = infoMap[comboKey] || {};
                        const price = info.price || info.consignPrice || info.promotionPrice || null;
                        if (!price) return;
                        const vids = String(comboKey).split(/[_,;:]/).filter(Boolean);
                        const label = vids.map(vid => valueNameMap[vid] || valueNameMap[Number(vid)] || vid).join(' / ');
                        skuPrices.push({ label, price: String(price) });
                    });
                }

                // 規格屬性（attribute table）— 標準化成 {name, value}
                const rawAttrs = o.attribute?.attributes || o.attributes || d.attribute?.attributes || [];
                const specs = (rawAttrs || []).map(a => ({
                    name: a.name || a.attrName || a.attributeName || '',
                    value: a.value || a.attrValue || a.attributeValue || ''
                })).filter(s => s.name || s.value);

                return {
                    title:   base.subject || base.title || o.subject || null,
                    price:   base.priceInfo?.price || null,
                    specs:   specs.length ? specs : null,
                    skuImgs: skuImgs,
                    skuPrices: skuPrices,
                };
            } catch(e) { return {err: String(e)}; }
        }""")
        if js_data.get("title"):
            result["raw_title"] = js_data["title"].strip()
        if js_data.get("price"):
            result["raw_price"] = str(js_data["price"])
        specs = js_data.get("specs")
        if not specs:
            # DOM fallback：1688 規格表格常見 class
            try:
                specs = page.evaluate("""() => {
                    const rows = [];
                    document.querySelectorAll('.obj-attribute li, .content-property li, table.obj-attribute-list tr, .attributes-list li').forEach(el => {
                        const t = (el.textContent || '').trim().replace(/\\s+/g, ' ');
                        const m = t.match(/^([^：:]{1,20})[：:]\\s*(.+)$/);
                        if (m) rows.push({ name: m[1].trim(), value: m[2].trim() });
                    });
                    return rows;
                }""")
            except Exception:
                specs = []
        if specs:
            result["raw_extra"]["specs"] = specs
        sku_prices = js_data.get("skuPrices") or []
        if sku_prices:
            result["raw_extra"]["sku_prices"] = sku_prices
            print(f"    [JS] 多規格價格 {len(sku_prices)} 組")
        result["_sku_imgs"] = js_data.get("skuImgs", [])
        print(f"    [JS] title={bool(js_data.get('title'))} specs={len(specs or [])} skuImgs={len(result['_sku_imgs'])} err={js_data.get('err','')}")
    except Exception as e:
        print(f"    [JS ERROR] {e}")

    if not result["raw_title"]:
        for sel in [".title-text", ".mod-detail-title h1", ".offer-title", ".detail-title"]:
            try:
                t = page.text_content(sel, timeout=2000)
                if t and t.strip() and len(t.strip()) > 4:
                    result["raw_title"] = t.strip(); break
            except Exception:
                pass

    if not result["raw_title"]:
        pt = page.title()
        for s in ["-1688.com","- 1688","阿里巴巴找货","阿里巴巴","批发","_","1688"]:
            pt = pt.replace(s, "").strip()
        company_keywords = ["有限公司","材料厂","制造厂","加工厂","有限责任","工贸","商贸","实业"]
        if not any(k in pt for k in company_keywords):
            result["raw_title"] = pt

    if not result["raw_price"]:
        for sel in [".price-value", ".m-price .price", ".price-text"]:
            try:
                p = page.text_content(sel, timeout=1500)
                if p and p.strip():
                    result["raw_price"] = p.strip(); break
            except Exception:
                pass

    # Phase 2 DOM 擷取（含 scroll + networkidle）
    product_images = _extract_images_dom(page, '1688')
    # SKU 規格圖（來自 JS data）
    sku_imgs = result.pop("_sku_imgs", [])
    product_images["sku_images"] = sku_imgs
    if sku_imgs:
        print(f"    SKU 規格圖 {len(sku_imgs)} 張")

    # 策略 5：re-fetch 攔截到的 HTML 找詳情圖（描述在獨立 iframe/AJAX 時使用）
    print(f"    [intercept] 攔截到 {len(intercepted_html_urls)} 個 HTML URL")
    for u in intercepted_html_urls[:5]:
        print(f"      {u[:100]}")
    if not product_images["detail_images"] and intercepted_html_urls:
        main_srcs = {i['src'] for i in product_images["main_images"]}
        for iurl in intercepted_html_urls[:15]:
            try:
                r = requests.get(iurl, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://www.1688.com/"
                }, timeout=8)
                if not r.ok:
                    continue
                for u in _ALICDN.findall(r.text):
                    if u.startswith('//'): u = 'https:' + u
                    if not u.startswith('http') or u in main_srcs: continue
                    if _SKIP.search(u) or not _tps_ok(u): continue
                    w, h = _parse_url_size(u)
                    if (w >= 1000 and h >= 250) or \
                       (w == 0 and ('imgextra' in u or 'cbu01' in u)):
                        main_srcs.add(u)
                        product_images["detail_images"].append({"src": u, "w": w, "h": h})
            except Exception:
                pass
        if product_images["detail_images"]:
            print(f"    [intercepted] detail={len(product_images['detail_images'])}")

    try:
        page.remove_listener("response", _on_resp)
    except Exception:
        pass

    result["product_images"] = product_images
    result["raw_images"] = [i["src"] for i in product_images["main_images"]][:8]

    for sel in [".mod-detail-description", ".detail-desc-content", ".description-content"]:
        try:
            d = page.text_content(sel, timeout=2000)
            if d and d.strip():
                result["raw_desc"] = d.strip()[:3000]; break
        except Exception:
            pass

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

    # 規格屬性 + 多 SKU 價格
    try:
        spec_data = page.evaluate("""() => {
            const specs = [];
            document.querySelectorAll('#J_AttrUL li, .attributes-list li, .tb-property-cont li, [class*="Attributes"] li').forEach(el => {
                const t = (el.textContent || '').trim().replace(/\\s+/g, ' ');
                const m = t.match(/^([^：:]{1,20})[：:]\\s*(.+)$/);
                if (m) specs.push({ name: m[1].trim(), value: m[2].trim() });
            });
            const skuPrices = [];
            try {
                const d = window.__GLOBAL_DATA__ || {};
                const sku2info = d.skuCore?.sku2info || d.skuModel?.sku2info || {};
                const valFmt = d.skuCore?.valItemMap || {};
                const nameOf = (vid) => {
                    const it = valFmt[vid];
                    return it ? (it.name || vid) : vid;
                };
                Object.keys(sku2info || {}).forEach(comboKey => {
                    const info = sku2info[comboKey] || {};
                    const price = info.price || info.promotionPrice;
                    if (!price) return;
                    const label = String(comboKey).split(/[_,;:]/).filter(Boolean).map(nameOf).join(' / ');
                    skuPrices.push({ label, price: String(price) });
                });
            } catch(e) {}
            return { specs, skuPrices };
        }""")
        if spec_data.get("specs"):
            result["raw_extra"]["specs"] = spec_data["specs"]
        if spec_data.get("skuPrices"):
            result["raw_extra"]["sku_prices"] = spec_data["skuPrices"]
            print(f"    [淘寶] 多規格價格 {len(spec_data['skuPrices'])} 組")
        print(f"    [淘寶] 規格 {len(spec_data.get('specs') or [])} 項")
    except Exception as e:
        print(f"    [淘寶規格 err] {e}")

    product_images = _extract_images_dom(page, 'taobao')
    # 淘寶主圖 JS fallback（當 DOM selector 抓不到時）
    if not product_images['main_images']:
        try:
            main_js = page.evaluate("""
() => {
    const imgs = []; const seen = new Set();

    // 策略1: 從 <script> 標籤的 JSON 資料撈 alicdn imgextra 大圖
    const RE = /https?:\/\/img\.alicdn\.com\/imgextra\/[^"'\s]+\.(jpg|png|webp)/gi;
    const RE2 = /\/\/img\.alicdn\.com\/imgextra\/[^"'\s]+\.(jpg|png|webp)/gi;
    const skipKw = ['icon','logo','avatar','shop','brand','banner','loading','placeholder','default'];
    for (const script of document.querySelectorAll('script')) {
        const txt = script.textContent || '';
        if (!txt.includes('alicdn')) continue;
        for (const re of [RE, RE2]) {
            let m;
            while ((m = re.exec(txt)) !== null) {
                let url = m[0];
                if (url.startsWith('//')) url = 'https:' + url;
                // 移除尺寸後綴，拿原圖
                url = url.replace(/_\d+x\d+[a-z]*\.(jpg|png|webp)/i, '.$1').split('?')[0];
                if (seen.has(url)) continue;
                if (skipKw.some(k => url.toLowerCase().includes(k))) continue;
                seen.add(url);
                imgs.push({src: url, w: 0, h: 0});
            }
        }
        if (imgs.length >= 10) break;
    }

    // 策略2: DOM 找目前可見的大圖（備用）
    if (imgs.length === 0) {
        document.querySelectorAll('img').forEach(el => {
            const src = el.getAttribute('src') || el.getAttribute('data-src') || el.getAttribute('data-lazysrc') || '';
            if (!src.includes('alicdn') || seen.has(src)) return;
            const rect = el.getBoundingClientRect();
            if (rect.width >= 200 && rect.height >= 200 && rect.left < window.innerWidth * 0.55) {
                seen.add(src);
                imgs.push({src: src.startsWith('//') ? 'https:' + src : src, w: 0, h: 0});
            }
        });
    }

    // 去掉重複、最多 10 張
    return imgs.slice(0, 10);
}
""")
            if main_js:
                product_images['main_images'] = main_js
                result['raw_images'] = [i['src'] for i in main_js[:8]]
                print(f"    [Taobao main JS fallback] {len(main_js)} 張")
        except Exception as e:
            print(f"    [Taobao main JS err] {e}")
    # 淘寶 SKU 規格圖
    try:
        sku_js = page.evaluate("""
() => {
    const imgs = []; const seen = new Set();
    const add = (src, lbl) => {
        if(!src || seen.has(src)) return;
        seen.add(src);
        imgs.push({src: src.startsWith('//')?'https:'+src:src, label:lbl||''});
    };
    // 路徑1: __GLOBAL_DATA__
    try {
        const d = window.__GLOBAL_DATA__ || {};
        const props = (d.item&&d.item.props&&d.item.props.props)
                   || (d.initData&&d.initData.item&&d.initData.item.props&&d.initData.item.props.props)
                   || [];
        props.forEach(p => (p.values||[]).forEach(v => add(v.imageUrl||v.picUrl||'', (p.name||'')+(v.name||''))));
    } catch(e){}
    // 路徑2: skuCore in page JSON
    if(imgs.length===0) {
        try {
            const scripts = document.querySelectorAll('script');
            for(const s of scripts) {
                if(!s.text.includes('skuCore')) continue;
                const m = s.text.match(/skuCore.*?props.*?\[(.{0,5000}?)\]/);
                if(m) {
                    const raw = JSON.parse('['+m[1]+']');
                    raw.forEach(p => (p.values||[]).forEach(v => add(v.imageUrl||'', (p.name||'')+(v.name||''))));
                    break;
                }
            }
        } catch(e){}
    }
    // 路徑3: DOM fallback
    if(imgs.length===0){
        document.querySelectorAll('[class*=sku] img,[class*=Sku] img,.J_TSaleProp img,.sku-prop img,[class*=color] img,[class*=Color] img').forEach(el => {
            const src = el.src || el.dataset.src || el.dataset.lazySrc || '';
            if(src && src.includes('alicdn') && el.width >= 30) add(src, el.alt||'');
        });
    }
    return imgs;
}
""")
        if sku_js:
            product_images["sku_images"] = sku_js
            print(f"    [Taobao SKU] {len(sku_js)} 張")
    except Exception as e:
        print(f"    [Taobao SKU err] {e}")
    # 淘寶評價圖片（買家秀）— 多策略掃描
    try:
        for tab_sel in ["text=用户评价", '[data-name*="评价"]', ".J_TabBar a"]:
            try:
                page.click(tab_sel, timeout=2000)
                page.wait_for_timeout(3000)
                break
            except Exception:
                pass
        for _ in range(6):
            page.evaluate("window.scrollBy(0, 1000)")
            page.wait_for_timeout(800)
        try:
            page.wait_for_selector("[class*=review] img, .pic-box img", timeout=4000)
        except Exception:
            pass
        review_js = (
            "(function(){"
            "var imgs=[];var seen={};"
            "function add(s){"
            "if(!s||seen[s])return;"
            "if(s.indexOf('alicdn')<0&&s.indexOf('taobao')<0)return;"
            "var f=s.replace(/_(\d+)x(\d+)[a-z]*\.(jpg|jpeg|png|webp)/i,'.$3').split('?')[0];"
            "if(seen[f])return;"
            "seen[s]=seen[f]=1;"
            "imgs.push({src:f.indexOf('//')==0?'https:'+f:f,w:0,h:0});}"
            "var sels=['[class*=review] img','[class*=Review] img','[class*=rate] img',"
            "'[class*=Rate] img','.pic-box img','.review-detail img',"
            "'[class*=comment] img','[class*=Comment] img','[class*=buyer] img'];"
            "for(var i=0;i<sels.length;i++){"
            "var els=document.querySelectorAll(sels[i]);"
            "for(var j=0;j<els.length;j++){"
            "add(els[j].getAttribute('src')||els[j].getAttribute('data-src')||'');}}"
            "if(imgs.length<3){"
            "var re=/https?:\\/\\/img\\.alicdn\\.com\\/bao\\/[^\"'\\s]+\\.(jpg|png|webp)/gi;"
            "var ss=document.querySelectorAll('script');"
            "for(var k=0;k<ss.length;k++){var t=ss[k].textContent||'';var m;"
            "while((m=re.exec(t))!==null){add(m[0]);}if(imgs.length>=30)break;}}"
            "return imgs.slice(0,40);})()"
        )
        review_imgs = page.evaluate(review_js)
        if review_imgs:
            product_images["review_images"] = review_imgs
            print(f"    [review] {len(review_imgs)} 張評價圖")
        else:
            print("    [review] 未找到評價圖")
    except Exception as e:
        print(f"    [review err] {e}")
    result["product_images"] = product_images
    result["raw_images"] = [i["src"] for i in product_images["main_images"]][:8]

    for sel in [".descContainer", ".J_DescContent"]:
        try:
            d = page.text_content(sel, timeout=2000)
            if d and d.strip():
                result["raw_desc"] = d.strip()[:3000]; break
        except Exception: pass

    return result


# ── Store Scan（Phase 1）────────────────────────────────────

_SCAN_1688_JS = """() => {
    const items = [];
    const seen = new Set();
    const selectors = [
        '.offer-list-row-offer', '.J_offerItem',
        '[class*="offerItem"]', '[class*="offer-item"]',
        '.list-item', '[class*="listItem"]',
    ];
    let cards = [];
    for (const sel of selectors) {
        cards = [...document.querySelectorAll(sel)];
        if (cards.length > 0) break;
    }
    for (const card of cards) {
        try {
            const titleEl = card.querySelector('a.title,a[title],.title a,h4 a,[class*="title"] a');
            if (!titleEl) continue;
            const url = titleEl.href || '';
            if (seen.has(url) || !url.includes('1688.com')) continue;
            seen.add(url);
            const title = (titleEl.textContent || titleEl.getAttribute('title') || '').trim();
            const imgEl = card.querySelector('img');
            let image = '';
            if (imgEl) {
                image = imgEl.src || imgEl.dataset.src || imgEl.getAttribute('data-lazyload') || '';
                if (image.startsWith('//')) image = 'https:' + image;
            }
            const priceEl = card.querySelector('[class*="price"] strong,[class*="price-num"],[class*="price-text"],.price strong');
            const price = priceEl ? priceEl.textContent.trim() : '';
            const shopEl = card.querySelector('[class*="company"] a,[class*="shopName"] a,.company-name a');
            const shop_name = shopEl ? shopEl.textContent.trim() : '';
            if (title && url) items.push({ title, url, image, price, shop_name });
        } catch(e) {}
    }
    return items;
}"""

_SCAN_TAOBAO_JS = """() => {
    const items = [];
    const seen = new Set();
    const selectors = ['.item','[class*="Card--"]','[class*="item--"]','[class*="ItemCard"]'];
    let cards = [];
    for (const sel of selectors) {
        cards = [...document.querySelectorAll(sel)];
        if (cards.length > 1) break;
    }
    for (const card of cards) {
        try {
            const titleEl = card.querySelector('[class*="title"] a,a[title],h4 a,.title a');
            if (!titleEl) continue;
            let url = titleEl.href || '';
            if (!url.startsWith('http')) url = 'https:' + url;
            if (seen.has(url)) continue;
            seen.add(url);
            const title = (titleEl.textContent || titleEl.getAttribute('title') || '').trim();
            const imgEl = card.querySelector('img');
            let image = '';
            if (imgEl) {
                image = imgEl.src || imgEl.dataset.src || '';
                if (image.startsWith('//')) image = 'https:' + image;
            }
            const priceEl = card.querySelector('[class*="price"]');
            const price = priceEl ? priceEl.textContent.trim().replace(/[^\\d.~\\-]/g,'') : '';
            const shopEl = card.querySelector('[class*="shop"] a,[class*="store"] a');
            const shop_name = shopEl ? shopEl.textContent.trim() : '';
            if (title && url) items.push({ title, url, image, price, shop_name });
        } catch(e) {}
    }
    return items;
}"""


def scan_store_page(page, url, limit=60):
    """Phase 1：掃描店鋪/分類頁，只抓當前頁面商品卡片，不翻頁。"""
    from datetime import datetime
    platform = '1688' if '1688.com' in url else 'taobao' if 'taobao.com' in url else 'unknown'
    if platform == 'unknown':
        return {"platform": platform, "items": [], "error": "不支援的平台"}

    print(f"    [Scan] {platform.upper()} 載入...")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)

    # 輕度捲動觸發 lazy load（不做完整詳情頁那種深度捲動）
    for i in range(1, 4):
        page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {i} / 3)")
        page.wait_for_timeout(700)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        js = _SCAN_1688_JS if platform == '1688' else _SCAN_TAOBAO_JS
        raw = page.evaluate(js)
    except Exception as e:
        print(f"    [Scan] JS 失敗: {e}")
        return {"platform": platform, "items": [], "error": str(e)}

    seen_urls = set()
    items = []
    for it in (raw or [])[:limit]:
        product_url = it.get("url", "")
        if not product_url or product_url in seen_urls:
            continue
        seen_urls.add(product_url)
        img = it.get("image", "")
        if img.startswith("//"): img = "https:" + img
        price_str = it.get("price", "")[:50]
        orig_price = None
        m = re.search(r'[\d.]+', price_str)
        if m:
            try: orig_price = float(m.group())
            except Exception: pass
        items.append({
            "title":          it.get("title", "")[:200],
            "url":            product_url,
            "product_url":    product_url,
            "image":          img,
            "main_image":     img,
            "price":          price_str,
            "original_price": orig_price,
            "shop_name":      it.get("shop_name", "")[:100],
            "platform":       platform,
            "scraped_at":     scraped_at,
        })

    print(f"    [Scan] 抓到 {len(items)} 筆")
    return {"platform": platform, "items": items}


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
            # 先處理店鋪掃描任務（scan jobs）
            scan_jobs = get_pending_scan_jobs()
            if scan_jobs:
                idle = 0
                for sj in scan_jobs:
                    sj_id, sj_url, sj_platform = sj["id"], sj["url"], sj["platform"]
                    print(f"[掃描 #{sj_id}] {sj_platform.upper()} {sj_url[:65]}...")
                    page = context.new_page()
                    try:
                        result = scan_store_page(page, sj_url, limit=60)
                        r = post_scan_result(sj_id, result)
                        if r.get("ok"):
                            print(f"  ✓ 掃描完成（{r.get('count', 0)} 筆）")
                        else:
                            print(f"  ✗ 回傳失敗: {r}")
                    except Exception as e:
                        print(f"  [錯誤] {e}")
                        try: post_scan_result(sj_id, {"error": str(e)})
                        except Exception: pass
                    finally:
                        page.close()

            jobs = get_pending_jobs()
            if jobs:
                idle = 0
                for job in jobs:
                    job_id, url, platform = job["id"], job["url"], job["platform"]
                    mode = job.get("mode", "scrape")

                    if mode == "images_only":
                        raw_images = job.get("raw_images", [])
                        print(f"[白底 #{job_id}] 處理 {len(raw_images)} 張圖片...")
                        processed = process_images(job_id, raw_images) if raw_images else []
                        r = post_result(job_id, {"mode": "images_only", "processed_images": processed})
                        if r.get("ok"):
                            print(f"  ✓ 白底完成（{len(processed)} 張）")
                        else:
                            print(f"  ✗ 回傳失敗: {r}")
                        continue

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

                        data["processed_images"] = []  # 白底改為手動觸發，不自動生成

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

def test_mode(pw, test_url):
    """--test URL：只跑圖片擷取，輸出 test_product_images.json。"""
    platform = '1688' if '1688.com' in test_url else 'taobao'
    print(f"\n[測試模式] platform={platform}")
    print(f"  URL: {test_url[:80]}...")

    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=HEADLESS,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()

    # 走完整的 scrape 流程（scrape_* 內部會自己 goto）
    if platform == '1688':
        data = scrape_1688(page, test_url)
    else:
        data = scrape_taobao(page, test_url)

    page.close()
    context.close()

    product_images = data.get("product_images", {"main_images": [], "detail_images": [], "video_urls": []})
    out_path = Path("test_product_images.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(product_images, f, ensure_ascii=False, indent=2)

    print(f"\n[OK] 輸出：{out_path.absolute()}")
    print(f"  主圖    : {len(product_images['main_images'])} 張")
    print(f"  詳情圖  : {len(product_images['detail_images'])} 張")
    print(f"  影片    : {len(product_images['video_urls'])} 個")


if __name__ == "__main__":
    login = "--login" in sys.argv
    test  = "--test"  in sys.argv
    scan  = "--scan"  in sys.argv

    with sync_playwright() as pw:
        if login:
            login_mode(pw)
        elif scan:
            idx = sys.argv.index("--scan")
            if idx + 1 >= len(sys.argv):
                print("用法：python local_worker.py --scan <URL>")
                sys.exit(1)
            scan_url = sys.argv[idx + 1]
            if not Path(PROFILE_DIR).exists():
                print("\n[提示] 尚未登入，請先執行：python local_worker.py --login\n")
                sys.exit(0)
            context = pw.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR, headless=HEADLESS,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            result = scan_store_page(page, scan_url)
            page.close(); context.close()
            import json as _json
            print(_json.dumps(result, ensure_ascii=False, indent=2))
        elif test:
            idx = sys.argv.index("--test")
            if idx + 1 >= len(sys.argv):
                print("用法：python local_worker.py --test <URL>")
                sys.exit(1)
            if not Path(PROFILE_DIR).exists():
                print("\n[提示] 尚未登入，請先執行：python local_worker.py --login\n")
                sys.exit(0)
            test_mode(pw, sys.argv[idx + 1])
        else:
            if not Path(PROFILE_DIR).exists():
                print("\n[提示] 尚未登入，請先執行：python local_worker.py --login\n")
                sys.exit(0)
            run(pw)
