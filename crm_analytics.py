"""
crm_analytics.py — CRM 對話分析 Blueprint
查詢現有 messages + customer_data，不新增任何資料表或欄位。
路由：
    GET  /admin/crm-analytics          顯示儀表板頁面
    GET  /api/crm-analytics/data       回傳 JSON 資料（支援 ?days=7/30/90）
"""
import os, time
from datetime import date, timedelta
import psycopg2
from flask import Blueprint, render_template, jsonify, request, redirect

DATABASE_URL   = os.getenv("DATABASE_URL", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "jsimple2024")

crm_analytics_bp = Blueprint("crm_analytics", __name__)


def _check_auth():
    key = request.args.get("key", "")
    return key == ADMIN_PASSWORD, key


def _pg_conn():
    return psycopg2.connect(DATABASE_URL)


def _q(sql, params=(), fetch="all"):
    if not DATABASE_URL:
        return [] if fetch == "all" else None
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        result = cur.fetchall() if fetch == "all" else cur.fetchone()
        cur.close()
        conn.close()
        return result
    except Exception as e:
        import sys
        print(f"[CRM Analytics] SQL error: {e}", file=sys.stderr)
        return [] if fetch == "all" else None


def _n(row, idx=0, default=0):
    """安全取值，處理 None / 空 row。"""
    if row is None:
        return default
    v = row[idx] if len(row) > idx else None
    return v if v is not None else default


def _get_analytics(days=30):
    now_ts     = time.time()
    today_str  = time.strftime("%Y/%m/%d", time.localtime(now_ts))
    cutoff_str = time.strftime("%Y/%m/%d", time.localtime(now_ts - days * 86400))

    # ── Card 1：今日新對話 ─────────────────────────────────────────
    today_convs = _n(_q(
        "SELECT COUNT(DISTINCT user_id) FROM messages "
        "WHERE SUBSTRING(time,1,10)=%s AND COALESCE(sent_by,'')!='admin' AND user_id!=''",
        (today_str,), fetch="one"
    ))

    # ── Card 2：Bot 回覆率（期間內） ───────────────────────────────
    r2 = _q(
        """SELECT
             COUNT(DISTINCT user_id) FILTER (WHERE COALESCE(sent_by,'')!='admin' AND user_id!='') AS total,
             COUNT(DISTINCT user_id) FILTER (WHERE replied=TRUE AND COALESCE(sent_by,'')!='admin') AS bot_ok
           FROM messages WHERE SUBSTRING(time,1,10)>=%s""",
        (cutoff_str,), fetch="one"
    )
    total_30d = max(_n(r2, 0), 1)
    bot_ok    = _n(r2, 1)
    bot_rate  = round(bot_ok / total_30d * 100, 1)

    # ── Card 3：人工接手（曾有 admin 回覆的 user 數） ──────────────
    human_count = _n(_q(
        "SELECT COUNT(DISTINCT user_id) FROM messages WHERE sent_by='admin'",
        fetch="one"
    ))

    # ── Card 4：未回覆對話（期間進線，但從未被回覆） ───────────────
    unreplied = _n(_q(
        """SELECT COUNT(DISTINCT user_id) FROM messages
           WHERE SUBSTRING(time,1,10)>=%s
             AND COALESCE(sent_by,'')!='admin' AND user_id!=''
             AND user_id NOT IN (
                 SELECT DISTINCT user_id FROM messages
                 WHERE (replied=TRUE OR sent_by='admin')
                   AND SUBSTRING(time,1,10)>=%s
             )""",
        (cutoff_str, cutoff_str), fetch="one"
    ))

    # ── Card 5：高意願 / 成交 ──────────────────────────────────────
    r5 = _q(
        """SELECT
             COUNT(CASE WHEN status IN ('followup','waiting') THEN 1 END),
             COUNT(CASE WHEN status='sold' THEN 1 END)
           FROM customer_data""",
        fetch="one"
    )
    high_intent = _n(r5, 0)
    sold        = _n(r5, 1)

    # ── Daily chart：每日訊息量 + 唯一用戶 + 詢價 ─────────────────
    raw_daily = _q(
        """SELECT
             SUBSTRING(time,1,10) AS day,
             COUNT(*) FILTER (WHERE msg!='' AND COALESCE(sent_by,'')!='admin') AS cust_msgs,
             COUNT(DISTINCT user_id) FILTER (WHERE COALESCE(sent_by,'')!='admin' AND user_id!='') AS unique_users,
             COUNT(*) FILTER (WHERE intent='詢價') AS inquiry
           FROM messages
           WHERE SUBSTRING(time,1,10)>=%s
           GROUP BY SUBSTRING(time,1,10)
           ORDER BY day ASC""",
        (cutoff_str,), fetch="all"
    ) or []

    # 補齊缺漏日期（讓折線圖不斷點）
    day_map    = {r[0]: r for r in raw_daily}
    start_date = date.fromtimestamp(now_ts - days * 86400)
    daily = []
    for i in range(days):
        ds = (start_date + timedelta(days=i)).strftime("%Y/%m/%d")
        r  = day_map.get(ds)
        daily.append({
            "day":          ds,
            "cust_msgs":    (r[1] or 0) if r else 0,
            "unique_users": (r[2] or 0) if r else 0,
            "inquiry":      (r[3] or 0) if r else 0,
        })

    # ── Funnel ─────────────────────────────────────────────────────
    f_total = _n(_q("SELECT COUNT(DISTINCT user_id) FROM messages WHERE COALESCE(sent_by,'')!='admin' AND user_id!=''", fetch="one"))
    f_bot   = _n(_q("SELECT COUNT(DISTINCT user_id) FROM messages WHERE replied=TRUE AND COALESCE(sent_by,'')!='admin'", fetch="one"))
    f_human = _n(_q("SELECT COUNT(DISTINCT user_id) FROM messages WHERE sent_by='admin'", fetch="one"))
    f_high  = _n(_q("SELECT COUNT(*) FROM customer_data WHERE status IN ('followup','waiting')", fetch="one"))
    f_sold  = _n(_q("SELECT COUNT(*) FROM customer_data WHERE status='sold'", fetch="one"))

    return {
        "cards": {
            "today_convs": today_convs,
            "bot_rate":    bot_rate,
            "human_count": human_count,
            "unreplied":   unreplied,
            "high_intent": high_intent,
            "sold":        sold,
        },
        "daily": daily,
        "funnel": [
            {"label": "訊客傳訊",   "count": f_total,  "color": "#4e79e7"},
            {"label": "Bot 自動回", "count": f_bot,    "color": "#59a14f"},
            {"label": "人工接手",   "count": f_human,  "color": "#f28e2b"},
            {"label": "標記高意願", "count": f_high,   "color": "#9467bd"},
            {"label": "標記成交",   "count": f_sold,   "color": "#e15759"},
        ],
    }


@crm_analytics_bp.route("/admin/crm-analytics")
def crm_analytics_page():
    ok, key = _check_auth()
    if not ok:
        return redirect("/admin")
    return render_template("crm_analytics.html", key=key)


@crm_analytics_bp.route("/api/crm-analytics/data")
def crm_analytics_data():
    ok, key = _check_auth()
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    days = int(request.args.get("days", 30))
    if days not in (7, 30, 90):
        days = 30
    return jsonify(_get_analytics(days))
