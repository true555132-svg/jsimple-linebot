// JSIMPLE 商品搬運 - content script
// 在 1688 / 淘寶頁面執行，掃描商品圖片

function scanPage() {
  const platform = location.hostname.includes('1688') ? '1688' : 'taobao';

  // ── 主圖 ──────────────────────────────────────────────────
  const mainSelectors = platform === '1688'
    ? ['img.preview-img', 'img.ant-image-img.preview-img', '.detail-gallery-turn-box img']
    : ['.J_ThumbList img', '.mainPicWraper img', '[class*="gallery"] img'];

  const mainImgs = [];
  const mainSrcs = new Set();

  for (const sel of mainSelectors) {
    const found = [...document.querySelectorAll(sel)].filter(img => {
      const src = img.src || img.dataset.src || '';
      return src.includes('alicdn') && img.naturalWidth >= 400 && img.naturalHeight >= 400
          && Math.max(img.naturalWidth, img.naturalHeight) / Math.min(img.naturalWidth, img.naturalHeight) <= 1.8;
    });
    if (found.length) {
      found.forEach(img => {
        const src = img.src || img.dataset.src || '';
        if (!mainSrcs.has(src)) {
          mainSrcs.add(src);
          mainImgs.push({ src, w: img.naturalWidth, h: img.naturalHeight });
        }
      });
      break;
    }
  }

  // ── 詳情圖 ────────────────────────────────────────────────
  const detailImgs = [...document.querySelectorAll('img')]
    .filter(img => {
      const src = img.src || '';
      return src.includes('alicdn')
        && img.naturalWidth >= 1000
        && img.naturalHeight >= 250
        && !mainSrcs.has(src);
    })
    .map(img => ({ src: img.src, w: img.naturalWidth, h: img.naturalHeight }))
    .filter((item, idx, arr) => arr.findIndex(i => i.src === item.src) === idx); // 去重

  // ── SKU 規格圖（從 window.__INIT_DATA__ 讀，不依賴 CSS selector）────
  const skuImgs = [];
  try {
    const d = window.__INIT_DATA__ || {};
    const o = d.offerDetail || d.detail || {};
    const skuProps = o.skuModel?.skuProps || [];
    const seen = new Set();
    skuProps.forEach(prop => {
      (prop.values || []).forEach(v => {
        const raw = v.image || v.imageUrl || '';
        if (!raw) return;
        const src = raw.startsWith('//') ? 'https:' + raw : raw;
        if (!seen.has(src)) {
          seen.add(src);
          skuImgs.push({
            src,
            w: 0, h: 0,
            label: (prop.name || '') + ':' + (v.name || '')
          });
        }
      });
    });
  } catch(e) {}
  // fallback：DOM selector（1688 舊版或其他平台）
  if (!skuImgs.length) {
    const skuSrcs = new Set([...mainSrcs]);
    const sels = platform === '1688'
      ? ['[class*="sku"] img[src*="alicdn"]', '[class*="prop"] img[src*="alicdn"]']
      : ['.J_TSaleProp img', '.sku-prop-item img'];
    for (const sel of sels) {
      [...document.querySelectorAll(sel)].forEach(img => {
        const src = img.src || '';
        if (src && !skuSrcs.has(src) && img.naturalWidth >= 30) {
          skuSrcs.add(src);
          skuImgs.push({ src, w: img.naturalWidth, h: img.naturalHeight, label: img.alt || '' });
        }
      });
    }
  }

  // ── 影片 ──────────────────────────────────────────────────
  const videoUrls = [...document.querySelectorAll('video source[src], video[src]')]
    .map(el => el.src || el.getAttribute('src'))
    .filter(Boolean)
    .filter((v, i, a) => a.indexOf(v) === i);

  return {
    platform,
    url: location.href,
    title: document.title,
    product_images: {
      main_images:   mainImgs,
      detail_images: detailImgs,
      sku_images:    skuImgs,
      video_urls:    videoUrls
    }
  };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === 'scan') {
    sendResponse(scanPage());
  }
  return true;
});
