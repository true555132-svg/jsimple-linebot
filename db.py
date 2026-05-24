import sqlite3, threading, time, os

DB_FILE = "crm.db"
_lock = threading.Lock()

def _conn():
    c = sqlite3.connect(DB_FILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with _lock:
        c = _conn()
        c.execute("""CREATE TABLE IF NOT EXISTS customers (
            key TEXT PRIMARY KEY,
            platform TEXT DEFAULT '',
            user_id TEXT DEFAULT '',
            room_size TEXT DEFAULT '',
            bed_type TEXT DEFAULT '',
            is_custom INTEGER DEFAULT 0,
            budget TEXT DEFAULT '',
            next_followup TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS conv_status (
            key TEXT PRIMARY KEY,
            status TEXT DEFAULT 'bot',
            updated_at TEXT DEFAULT ''
        )""")
        c.commit()
        c.close()

def get_customer(key):
    with _lock:
        c = _conn()
        row = c.execute("SELECT * FROM customers WHERE key=?", (key,)).fetchone()
        c.close()
        return dict(row) if row else {}

def save_customer(key, platform, user_id, data):
    now = time.strftime("%Y/%m/%d %H:%M:%S")
    with _lock:
        c = _conn()
        c.execute("""INSERT OR REPLACE INTO customers
            (key,platform,user_id,room_size,bed_type,is_custom,budget,next_followup,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (key, platform, user_id,
             data.get("room_size",""), data.get("bed_type",""),
             1 if data.get("is_custom") else 0,
             data.get("budget",""), data.get("next_followup",""), now))
        c.commit()
        c.close()

def get_status(key):
    with _lock:
        c = _conn()
        row = c.execute("SELECT status FROM conv_status WHERE key=?", (key,)).fetchone()
        c.close()
        return row["status"] if row else "bot"

def set_status(key, status):
    now = time.strftime("%Y/%m/%d %H:%M:%S")
    with _lock:
        c = _conn()
        c.execute("INSERT OR REPLACE INTO conv_status (key,status,updated_at) VALUES(?,?,?)",
                  (key, status, now))
        c.commit()
        c.close()

def get_all_statuses():
    with _lock:
        c = _conn()
        rows = c.execute("SELECT key,status FROM conv_status").fetchall()
        c.close()
        return {r["key"]: r["status"] for r in rows}

init_db()
