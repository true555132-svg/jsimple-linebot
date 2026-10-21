"""
payments.py — 匯款追蹤 Blueprint
從現有 messages 表偵測「客人稱已匯款」與「你回覆已收到」，
彙整成 payments 表（時間、金額、商品、訂金/尾款），供後台確認。

路由：
    GET  /admin/payments               後台頁面
    GET  /api/payments                 清單 JSON（?status=pending|confirmed|rejected|all&days=N）
    POST /api/payments/scan            立即掃描
    POST /api/payments/<id>            更新（確認/駁回/修改金額商品）
    GET  /api/payments/export.csv      匯出 CSV

偵測只讀 messages，不改動任何既有資料表。
狀態：pending（客人稱已匯，待你確認）→ confirmed（你回覆已收到，或後台手動確認）；rejected 為駁回。
人工操作過的紀錄（manual=TRUE）不會被重新掃描覆蓋。
"""
import os, re, io, csv, json, time, calendar, hmac, threading
from datetime import datetime
from flask import Blueprint, render_template, jsonify, request, Response

DATABASE_URL = os.getenv("DATABASE_URL", "")
SCAN_WINDOW_DAYS = 45          # 每次掃描回看天數
CONFIRM_WINDOW_SEC = 72 * 3600  # 客人稱已匯後，多久內的「收到」才視為確認
CLAIM_MERGE_SEC = 600          # 10 分鐘內連續的匯款訊息併為一筆
IMAGE_AFTER_BANK_SEC = 30 * 86400
IMAGE_MERGE_SEC = 3600       # 文字匯款訊息前後一小時內的圖片，視為同一筆的截圖

payments_bp = Blueprint("payments", __name__)

# ── 規則 ────────────────────────────────────────────────────────────────────

BANK_RE = re.compile(r"戶名|格達貿易|銀行\s*[:：]|帳號\s*[:：]?\s*\d{8,}")
CLAIM_RE = re.compile(
    r"已匯|匯好|匯了|已轉|轉好|轉了|已付|已經付|已經匯|匯出|剛匯|匯款完成了|匯入|"
    r"末五碼|後五碼|末5碼|後5碼|款項已|貨款已|已完成匯款|已完成轉帳|轉帳.{0,10}元"
)
# 預告、詢問、請對方通知等「還沒匯」的說法
FUTURE_RE = re.compile(
    r"明天|明日|後天|等等|稍後|晚點|待會|之後|再通知|完成再|匯款後|匯完再|"
    r"會匯|將匯|準備匯|要匯|想匯|如匯款|如果匯|是否|有沒有|請問|可以.{0,3}匯嗎|怎麼匯|如何匯|"
    r"收款帳戶|如有轉帳|如已轉"
)
CONFIRM_RE = re.compile(r"有收到|收到了|收到的|已收到|收到這邊|收到$|收到[\s，,]|已入帳|有入帳|確認.{0,3}入帳|款項.{0,3}確認")
LAST5_RE = re.compile(r"(?:末|後)\s*(?:五|5)\s*碼[^\d]{0,6}(\d{5})")
AMOUNT_CLAIM_RE = re.compile(r"(?:NT\$?|新臺幣\$?|新台幣\$?|\$)\s*([\d,]{3,9})|([\d,]{3,9})\s*元")
QUOTE_TOTAL_RE = re.compile(r"(?:合計|總計|總額|金額)\s*[:：]?\s*(\d[\d,]{2,8})")
DEPOSIT_RE = re.compile(r"訂金\s*[:：]?\s*(\d[\d,]{2,8})")
BALANCE_RE = re.compile(r"尾款\s*[:：]?\s*(\d[\d,]{2,8})")
ORDER_NO_RE = re.compile(r"訂單(?:編號)?\s*[:：#]?\s*#?\s*([0-9A-Za-z]{4,24})")


def _num(s):
    try:
        return int(str(s).replace(",", ""))
    except Exception:
        return None


def parse_ts(time_str):
    """'2026/10/04 02:54:14'（台灣時間）→ epoch 秒，與主機時區無關。"""
    try:
        t = datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S")
        return calendar.timegm(t.timetuple()) - 8 * 3600
    except Exception:
        return 0


def parse_quote(text):
    """從你傳銀行帳號的那則訊息抓出商品、合計、訂金、尾款。"""
    mt = QUOTE_TOTAL_RE.search(text)
    total = _num(mt.group(1)) if mt else None
    dep = _num(DEPOSIT_RE.search(text).group(1)) if DEPOSIT_RE.search(text) else None
    bal = _num(BALANCE_RE.search(text).group(1)) if BALANCE_RE.search(text) else None
    if total is None and dep is not None and bal is not None:
        total = dep + bal
    if total is None:
        m = re.search(r"金額\s*[:：]?\s*(\d[\d,]{2,8})|帳號\s*[:：]?\s*\d{8,}\s+(\d[\d,]{2,8})\s*\$?\s*$", text)
        if m:
            total = _num(m.group(1) or m.group(2))
    product = ""
    head = re.split(r"銀行\s*[:：]", text)[0]
    lines = [l.strip() for l in head.split("\n")]
    items = []
    for idx, line in enumerate(lines):
        for mm in re.finditer(r"([^$]{2,60}?)\s*\d[\d,]{2,7}\s*\$", line):
            name = re.sub(r"單價\s*$", "", mm.group(1)).strip(" 　,，:：")
            if not name or re.search(r"運費|電梯|合計|總計|報價|金額|訂金|尾款", name) or re.fullmatch(r"[\d,\s$]+", name):
                continue
            # 品名與規格被換行拆開時，把上一行（不含價格與尺寸）接回來
            if idx > 0 and mm.start() == 0:
                prev = lines[idx - 1]
                if prev and "$" not in prev and len(prev) <= 40 and not re.search(r"內寬|總寬|內長|外長|護欄|床底高|報價", prev):
                    name = prev + " " + name
            items.append(name)
    if items:
        product = "、".join(items[:3])[:80]
    return {"total": total, "deposit": dep, "balance": bal, "product": product}


def _classify(amount, quote):
    if not amount or not quote:
        return "未知"
    tol = 1
    for kind, key in (("訂金", "deposit"), ("尾款", "balance"), ("全額", "total")):
        v = quote.get(key)
        if v and abs(v - amount) <= tol:
            return kind
    return "未知"


def analyze_conversation(msgs):
    """
    msgs：依時間排序的 [{"role": "user"|"admin", "content": str, "ts": int,
                         "image": bool, "is_auto": bool}]
    回傳 payment 候選清單（dict）。純函式，不碰資料庫。
    """
    events = []
    last_quote = None
    last_bank_ts = 0
    seg_claimed = seg_closed = False   # 同一次匯款資訊之後：是否已有文字匯款、是否已被你確認
    seg_img = None                      # 同一段內第一張圖片候選（多張圖片併成一筆）
    for m in msgs:
        text = (m.get("content") or "").strip()
        ts = m.get("ts", 0)
        role = m.get("role")
        if role == "admin" and not m.get("is_auto") and BANK_RE.search(text):
            q = parse_quote(text)
            # 純帳號訊息沒有金額時，沿用前一則報價的資料
            if last_quote and not (q["total"] or q["deposit"] or q["balance"]):
                q = dict(last_quote, product=q["product"] or last_quote["product"])
            elif last_quote and not q["product"]:
                q["product"] = last_quote["product"]
            last_quote = q
            last_bank_ts = ts
            seg_claimed = seg_closed = False
            seg_img = None
            continue
        if role == "admin" and not m.get("is_auto"):
            # 沒帶帳號的後續報價（只更新商品與金額，不當作匯款資訊）
            if QUOTE_TOTAL_RE.search(text) or DEPOSIT_RE.search(text):
                q = parse_quote(text)
                if q["total"] or q["deposit"]:
                    last_quote = q
            if len(text) <= 80 and CONFIRM_RE.search(text):
                events.append({"type": "confirm", "ts": ts, "text": text})
                seg_closed = True
            continue
        if role == "user":
            is_claim = bool(text) and CLAIM_RE.search(text) and not FUTURE_RE.search(text)
            # 客人貼出自己的收款帳戶（對方要匯給客人）不算
            if is_claim and re.search(r"收款帳戶|我的帳戶.{0,6}(?:退|匯給)", text):
                is_claim = False
            m5 = LAST5_RE.search(text) if text else None
            if is_claim or m5:
                amt = None
                m_amt = AMOUNT_CLAIM_RE.search(text) if is_claim else None
                if m_amt:
                    amt = _num(m_amt.group(1) or m_amt.group(2))
                # 這則文字之前一小時內的圖片候選，視為同一筆的截圖
                if seg_img is not None and ts - seg_img["ts"] <= IMAGE_MERGE_SEC:
                    events.remove(seg_img)
                seg_img = None
                seg_claimed = True
                events.append({"type": "claim", "ts": ts, "text": text, "amount": amt,
                               "last5": m5.group(1) if m5 else "", "source": "text",
                               "quote": dict(last_quote) if last_quote else None})
            elif (m.get("image") and text == "[圖片]" and last_bank_ts
                  and 0 <= ts - last_bank_ts <= IMAGE_AFTER_BANK_SEC
                  and not seg_claimed and not seg_closed):
                if seg_img is None:
                    seg_img = {"type": "claim", "ts": ts, "n": 1, "amount": None, "last5": "",
                               "source": "image", "text": "",
                               "quote": dict(last_quote) if last_quote else None}
                    events.append(seg_img)
                else:
                    seg_img["n"] += 1
                seg_img["text"] = f"傳了 {seg_img['n']} 張圖片（待確認是否為轉帳截圖）"

    # 併連續的匯款訊息
    claims = []
    for e in events:
        if e["type"] != "claim":
            continue
        prev = claims[-1] if claims else None
        if prev and e["ts"] - prev["ts"] <= CLAIM_MERGE_SEC and e["source"] == "text" and prev["source"] == "text":
            prev["text"] = (prev["text"] + " / " + e["text"])[:300]
            prev["amount"] = prev["amount"] or e["amount"]
            prev["last5"] = prev["last5"] or e["last5"]
            continue
        claims.append(dict(e))

    confirms = [e for e in events if e["type"] == "confirm"]
    results = []
    for c in claims:
        # 圖片候選在時間窗內沒有你的確認時，仍列為待確認
        conf = next((x for x in confirms if 0 <= x["ts"] - c["ts"] <= CONFIRM_WINDOW_SEC), None)
        q = c["quote"] or {}
        amount = c["amount"]
        results.append({
            "claim_ts": c["ts"],
            "source": c["source"],
            "amount": amount,
            "last5": c["last5"],
            "kind": _classify(amount, q) if amount else "未知",
            "product": q.get("product", ""),
            "quote_total": q.get("total"),
            "deposit": q.get("deposit"),
            "balance": q.get("balance"),
            "evidence": c["text"][:300],
            "auto_status": "confirmed" if conf else "pending",
            "confirm_ts": conf["ts"] if conf else 0,
            "confirm_text": conf["text"][:120] if conf else "",
        })
    # 客人沒寫金額、但報價只有一種金額時，無法確定是訂金或尾款，保持未知讓你補
    return results


# ── 資料庫 ──────────────────────────────────────────────────────────────────

_db_lock = threading.Lock()
_table_ready = False


def _conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def init_table():
    global _table_ready
    if _table_ready or not DATABASE_URL:
        return
    with _db_lock:
        if _table_ready:
            return
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id           SERIAL PRIMARY KEY,
                    conv_key     TEXT NOT NULL,
                    platform     TEXT DEFAULT '',
                    user_id      TEXT DEFAULT '',
                    claim_ts     BIGINT DEFAULT 0,
                    source       TEXT DEFAULT 'text',
                    amount       INTEGER,
                    last5        TEXT DEFAULT '',
                    kind         TEXT DEFAULT '未知',
                    product      TEXT DEFAULT '',
                    quote_total  INTEGER,
                    deposit      INTEGER,
                    balance      INTEGER,
                    evidence     TEXT DEFAULT '',
                    status       TEXT DEFAULT 'pending',
                    confirm_ts   BIGINT DEFAULT 0,
                    confirm_text TEXT DEFAULT '',
                    manual       BOOLEAN DEFAULT FALSE,
                    note         TEXT DEFAULT '',
                    created_at   BIGINT DEFAULT 0,
                    updated_at   BIGINT DEFAULT 0,
                    UNIQUE (conv_key, claim_ts, source)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status, claim_ts)")
            conn.commit()
            cur.close()
            _table_ready = True
        finally:
            conn.close()


def _load_conversations(days):
    """從 messages 取出近 N 天有互動的對話，整理成 analyze_conversation 的輸入。"""
    cutoff = time.strftime("%Y/%m/%d", time.gmtime(time.time() + 8 * 3600 - days * 86400))
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT m.platform, m.user_id, m.time, m.msg, m.reply, m.replied, m.image_url, COALESCE(m.sent_by,'')
            FROM messages m
            WHERE m.user_id <> '' AND m.user_id <> 'ADMIN' AND m.platform <> 'FB_COMMENT'
              AND m.time >= %s
            ORDER BY m.platform, m.user_id, m.time, m.id
        """, (cutoff,))
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    convs = {}
    for pf, uid, t, msg, reply, replied, img, sent_by in rows:
        ts = parse_ts(t)
        lst = convs.setdefault((pf, uid), [])
        if sent_by == "admin":
            if reply:
                lst.append({"role": "admin", "content": reply, "ts": ts, "image": False, "is_auto": False})
        else:
            if msg:
                lst.append({"role": "user", "content": msg, "ts": ts,
                            "image": bool(img) and msg == "[圖片]", "is_auto": False})
            # bot 自動回覆不參與判斷，略過
    return convs


def scan(days=SCAN_WINDOW_DAYS):
    """掃描並寫入 payments。回傳 (新增, 更新)。"""
    if not DATABASE_URL:
        return 0, 0
    init_table()
    convs = _load_conversations(days)
    now = int(time.time())
    inserted = updated = 0
    conn = _conn()
    try:
        cur = conn.cursor()
        for (pf, uid), msgs in convs.items():
            for r in analyze_conversation(msgs):
                key = f"{pf}:{uid}"
                cur.execute("""
                    INSERT INTO payments
                      (conv_key,platform,user_id,claim_ts,source,amount,last5,kind,product,
                       quote_total,deposit,balance,evidence,status,confirm_ts,confirm_text,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (conv_key, claim_ts, source) DO UPDATE SET
                      amount=EXCLUDED.amount, last5=EXCLUDED.last5, kind=EXCLUDED.kind,
                      product=EXCLUDED.product, quote_total=EXCLUDED.quote_total,
                      deposit=EXCLUDED.deposit, balance=EXCLUDED.balance, evidence=EXCLUDED.evidence,
                      status=EXCLUDED.status, confirm_ts=EXCLUDED.confirm_ts,
                      confirm_text=EXCLUDED.confirm_text, updated_at=EXCLUDED.updated_at
                    WHERE payments.manual = FALSE
                    RETURNING (xmax = 0)
                """, (key, pf, uid, r["claim_ts"], r["source"], r["amount"], r["last5"], r["kind"],
                      r["product"], r["quote_total"], r["deposit"], r["balance"], r["evidence"],
                      r["auto_status"], r["confirm_ts"], r["confirm_text"], now, now))
                row = cur.fetchone()
                if row:
                    if row[0]:
                        inserted += 1
                    else:
                        updated += 1
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return inserted, updated


_last_scan = 0
_scan_lock = threading.Lock()


def scan_if_stale(max_age=120):
    global _last_scan
    if time.time() - _last_scan < max_age:
        return
    if not _scan_lock.acquire(blocking=False):
        return
    try:
        scan()
        _last_scan = time.time()
    except Exception as e:
        import sys
        print(f"[Payments] scan error: {e}", file=sys.stderr)
    finally:
        _scan_lock.release()


def start_background_scan(interval=300):
    def loop():
        time.sleep(30)
        while True:
            scan_if_stale(max_age=interval - 1)
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True).start()


# ── 認證 ────────────────────────────────────────────────────────────────────

def _auth():
    pw = os.getenv("ADMIN_PASSWORD", "")
    if not pw:
        return False
    cands = [request.args.get("key", ""), request.args.get("admin_key", "")]
    body = request.get_json(silent=True, force=True) or {} if request.method in ("POST", "PUT") else {}
    cands += [body.get("key", ""), body.get("admin_key", "")]
    return any(c and hmac.compare_digest(str(c), pw) for c in cands)


def _names(keys):
    """顯示名稱：取主程式的快取，查不到的背景補抓。"""
    try:
        import app as _app
        out = {}
        for k in keys:
            prof = _app.user_profiles.get(k) or {}
            out[k] = prof.get("name", "")
            if not out[k] and ":" in k:
                pf, uid = k.split(":", 1)
                threading.Thread(target=_app.get_user_profile, args=(pf, uid), daemon=True).start()
        return out
    except Exception:
        return {k: "" for k in keys}


def _fmt_time(ts):
    if not ts:
        return ""
    return time.strftime("%Y/%m/%d %H:%M", time.gmtime(ts + 8 * 3600))


COLS = ("id,conv_key,claim_ts,source,amount,last5,kind,product,quote_total,deposit,balance,"
        "evidence,status,confirm_ts,confirm_text,manual,note")


def _rows(status, days):
    init_table()
    where, params = [], []
    if status in ("pending", "confirmed", "rejected"):
        where.append("status=%s"); params.append(status)
    if days:
        where.append("claim_ts >= %s"); params.append(int(time.time()) - days * 86400)
    sql = f"SELECT {COLS} FROM payments" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY claim_ts DESC LIMIT 1000"
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    keys = COLS.split(",")
    items = [dict(zip(keys, r)) for r in rows]
    names = _names({i["conv_key"] for i in items})
    for i in items:
        i["name"] = names.get(i["conv_key"], "") or i["conv_key"].split(":", 1)[-1][-6:]
        i["claim_time"] = _fmt_time(i["claim_ts"])
        i["confirm_time"] = _fmt_time(i["confirm_ts"])
    return items


# ── 路由 ────────────────────────────────────────────────────────────────────

@payments_bp.route("/admin/payments")
def payments_page():
    if not _auth():
        from flask import redirect
        return redirect("/admin")
    return render_template("payments.html", key=request.args.get("key", ""))


@payments_bp.route("/api/payments")
def api_payments():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    if not DATABASE_URL:
        return jsonify({"items": [], "summary": {}, "note": "no database"})
    threading.Thread(target=scan_if_stale, daemon=True).start()
    status = request.args.get("status", "pending")
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        days = 30
    items = _rows(status, days)
    confirmed = [i for i in items if i["status"] == "confirmed" and i["amount"]]
    by_product = {}
    for i in confirmed:
        p = (i["product"] or "未註明").split("、")[0][:30]
        by_product[p] = by_product.get(p, 0) + i["amount"]
    summary = {
        "confirmed_total": sum(i["amount"] for i in confirmed),
        "confirmed_count": len([i for i in items if i["status"] == "confirmed"]),
        "pending_count": len([i for i in items if i["status"] == "pending"]),
        "by_product": sorted(by_product.items(), key=lambda x: -x[1])[:8],
    }
    return jsonify({"items": items, "summary": summary})


@payments_bp.route("/api/payments/scan", methods=["POST"])
def api_payments_scan():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    try:
        ins, upd = scan()
        return jsonify({"ok": True, "inserted": ins, "updated": upd})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@payments_bp.route("/api/payments/<int:pid>", methods=["POST"])
def api_payments_update(pid):
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    sets, params = ["manual=TRUE", "updated_at=%s"], [int(time.time())]
    if "status" in data:
        if data["status"] not in ("pending", "confirmed", "rejected"):
            return jsonify({"error": "bad status"}), 400
        sets.append("status=%s"); params.append(data["status"])
        if data["status"] == "confirmed":
            sets.append("confirm_ts=%s"); params.append(int(time.time()))
            sets.append("confirm_text=%s"); params.append("後台手動確認")
    if "amount" in data:
        try:
            amt = int(str(data["amount"]).replace(",", "")) if str(data["amount"]).strip() else None
        except ValueError:
            return jsonify({"error": "bad amount"}), 400
        sets.append("amount=%s"); params.append(amt)
    for f in ("product", "kind", "note", "last5"):
        if f in data:
            sets.append(f"{f}=%s"); params.append(str(data[f])[:200])
    params.append(pid)
    init_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE payments SET {', '.join(sets)} WHERE id=%s", params)
        n = cur.rowcount
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return jsonify({"ok": n == 1})


@payments_bp.route("/api/payments/export.csv")
def api_payments_export():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 403
    if not DATABASE_URL:
        return jsonify({"error": "no database"}), 503
    status = request.args.get("status", "confirmed")
    try:
        days = int(request.args.get("days", 90))
    except ValueError:
        days = 90
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["匯款時間", "確認時間", "客人", "金額", "類型", "商品", "後五碼", "狀態", "備註"])
    for i in _rows(status, days):
        w.writerow([i["claim_time"], i["confirm_time"], i["name"], i["amount"] or "", i["kind"],
                    i["product"], i["last5"], i["status"], i["note"]])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=payments.csv"})
