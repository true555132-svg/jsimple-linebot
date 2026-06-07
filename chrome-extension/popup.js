const SERVER = 'https://jsimple-linebot.onrender.com';
const API_KEY = 'jsimple2024';

let scanResult = null;

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;

  document.getElementById('pageUrl').textContent = tab.url || '';

  const is1688 = /1688\.com/.test(tab.url);
  const isTaobao = /taobao\.com/.test(tab.url);

  if (!is1688 && !isTaobao) {
    document.getElementById('warnBox').style.display = 'block';
    document.getElementById('btnSend').textContent = '不支援此頁面';
    return;
  }

  document.getElementById('platform').textContent = is1688 ? '1688' : '淘寶';

  // Inject content script if needed, then scan
  try {
    const result = await chrome.tabs.sendMessage(tab.id, { action: 'scan' });
    handleScanResult(result);
  } catch (e) {
    // content script not loaded yet, inject manually
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['content.js'] });
    const result = await chrome.tabs.sendMessage(tab.id, { action: 'scan' });
    handleScanResult(result);
  }
}

function handleScanResult(result) {
  if (!result) return;
  scanResult = result;
  const pi = result.product_images || {};
  document.getElementById('cMain').textContent   = (pi.main_images   || []).length;
  document.getElementById('cDetail').textContent = (pi.detail_images || []).length;
  document.getElementById('cVideo').textContent  = (pi.video_urls    || []).length;

  const btn = document.getElementById('btnSend');
  const total = (pi.main_images||[]).length + (pi.detail_images||[]).length;
  btn.disabled = false;
  btn.textContent = total > 0 ? `送出到後台（${total} 張圖）` : '送出（無圖片）';
}

document.getElementById('btnSend').addEventListener('click', async () => {
  if (!scanResult) return;
  const btn = document.getElementById('btnSend');
  const msg = document.getElementById('statusMsg');
  btn.disabled = true;
  btn.textContent = '送出中...';
  msg.className = 'status';
  msg.textContent = '';

  try {
    const brand = document.getElementById('brandSel').value;
    const body = {
      url:            scanResult.url,
      platform:       scanResult.platform,
      brand:          brand,
      title:          scanResult.title,
      product_images: scanResult.product_images,
    };
    const r = await fetch(`${SERVER}/api/products/from-extension?key=${API_KEY}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.ok) {
      msg.className = 'status ok';
      msg.textContent = `成功！任務 #${d.id}，AI 改寫中`;
      btn.textContent = '已送出';
    } else {
      throw new Error(d.error || '未知錯誤');
    }
  } catch (e) {
    msg.className = 'status err';
    msg.textContent = '失敗：' + e.message;
    btn.disabled = false;
    btn.textContent = '重試';
  }
});

init().catch(console.error);
