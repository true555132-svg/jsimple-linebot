import threading, time, json, os

_lock = threading.Lock()
_cache = {}  # key -> {status, tags, platform, user_id, user_name, room_size, bed_type, is_custom, budget, next_followup, note}

CRM_SHEET = "CRM_Data"
CRM_HEADERS = ["key","platform","user_id","user_name","status","tags",
               "room_size","bed_type","is_custom","budget","next_followup","note","updated_at"]
_ws = None
_ws_lock = threading.Lock()

def _get_ws():
    global _ws
    with _ws_lock:
        if _ws is not None:
            return _ws
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
            sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
            sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
            if not sa_json or not sheet_id:
                return None
            creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=SCOPES)
            gc = gspread.authorize(creds)
            spreadsheet = gc.open_by_key(sheet_id)
            try:
                _ws = spreadsheet.worksheet(CRM_SHEET)
            except Exception:
                _ws = spreadsheet.add_worksheet(title=CRM_SHEET, rows=2000, cols=20)
                _ws.append_row(CRM_HEADERS)
            return _ws
        except Exception as e:
            print(f"[CRM] sheets connect error: {e}")
            return None

def load_from_sheets():
    ws = _get_ws()
    if not ws:
        return
    try:
        rows = ws.get_all_records()
        with _lock:
            for row in rows:
                k = str(row.get("key", "")).strip()
                if not k:
                    continue
                tags_raw = row.get("tags", "")
                try:
                    tags = json.loads(tags_raw) if tags_raw else []
                except Exception:
                    tags = []
                _cache[k] = {
                    "platform": row.get("platform", ""),
                    "user_id": str(row.get("user_id", "")),
                    "user_name": row.get("user_name", ""),
                    "status": row.get("status", "bot") or "bot",
                    "tags": tags,
                    "room_size": row.get("room_size", ""),
                    "bed_type": row.get("bed_type", ""),
                    "is_custom": str(row.get("is_custom", "0")) == "1",
                    "budget": row.get("budget", ""),
                    "next_followup": row.get("next_followup", ""),
                    "note": row.get("note", ""),
                }
        print(f"[CRM] loaded {len(rows)} rows from Sheets")
    except Exception as e:
        print(f"[CRM] load error: {e}")

def _write_to_sheets(key):
    try:
        ws = _get_ws()
        if not ws:
            return
        with _lock:
            data = dict(_cache.get(key, {}))
        row_data = [
            key,
            data.get("platform", ""),
            data.get("user_id", ""),
            data.get("user_name", ""),
            data.get("status", "bot"),
            json.dumps(data.get("tags", []), ensure_ascii=False),
            data.get("room_size", ""),
            data.get("bed_type", ""),
            "1" if data.get("is_custom") else "0",
            data.get("budget", ""),
            data.get("next_followup", ""),
            data.get("note", ""),
            str(int(time.time())),
        ]
        try:
            cell = ws.find(key, in_column=1)
            end_col = chr(ord("A") + len(CRM_HEADERS) - 1)
            ws.update(f"A{cell.row}:{end_col}{cell.row}", [row_data])
        except Exception:
            ws.append_row(row_data)
    except Exception as e:
        print(f"[CRM] write error: {e}")

def _async_save(key):
    threading.Thread(target=_write_to_sheets, args=(key,), daemon=True).start()

# ── public API ──────────────────────────────────────────────────────────────

def get_customer(key):
    with _lock:
        d = _cache.get(key, {})
        return {
            "room_size": d.get("room_size", ""),
            "bed_type": d.get("bed_type", ""),
            "is_custom": d.get("is_custom", False),
            "budget": d.get("budget", ""),
            "next_followup": d.get("next_followup", ""),
            "note": d.get("note", ""),
        }

def save_customer(key, platform, user_id, data, user_name=""):
    with _lock:
        if key not in _cache:
            _cache[key] = {"status": "bot", "tags": []}
        _cache[key].update({
            "platform": platform,
            "user_id": user_id,
            "user_name": user_name or _cache[key].get("user_name", ""),
            "room_size": data.get("room_size", ""),
            "bed_type": data.get("bed_type", ""),
            "is_custom": bool(data.get("is_custom", False)),
            "budget": data.get("budget", ""),
            "next_followup": data.get("next_followup", ""),
            "note": data.get("note", ""),
        })
    _async_save(key)

def get_status(key):
    with _lock:
        return _cache.get(key, {}).get("status", "bot")

def set_status(key, status):
    with _lock:
        if key not in _cache:
            _cache[key] = {"tags": []}
        _cache[key]["status"] = status
    _async_save(key)

def get_all_statuses():
    with _lock:
        return {k: v.get("status", "bot") for k, v in _cache.items()}

def get_tags(key):
    with _lock:
        return list(_cache.get(key, {}).get("tags", []))

def save_tags(key, tags, platform="", user_id="", user_name=""):
    with _lock:
        if key not in _cache:
            _cache[key] = {"status": "bot"}
        _cache[key]["tags"] = list(tags)
        if platform:
            _cache[key].setdefault("platform", platform)
        if user_id:
            _cache[key].setdefault("user_id", user_id)
        if user_name and not _cache[key].get("user_name"):
            _cache[key]["user_name"] = user_name
    _async_save(key)

def touch(key, platform="", user_id="", user_name=""):
    with _lock:
        if key not in _cache:
            _cache[key] = {"status": "bot", "tags": []}
        if platform:
            _cache[key].setdefault("platform", platform)
        if user_id:
            _cache[key].setdefault("user_id", user_id)
        if user_name and not _cache[key].get("user_name"):
            _cache[key]["user_name"] = user_name

# Load on import
load_from_sheets()
