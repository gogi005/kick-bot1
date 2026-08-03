"""
Kick Stake Drops Bot v25 - TIME WINDOW + TEST MODE
- ACTIVE HOURS: 4 AM to 10 AM IST (auto watch)
- MANUAL: /watchtest works 24/7
- TEST: /startwatching watches all known live for 1 hour
- TELEGRAM USERNAMES: Shows @username when users join
- MINUTE LOGS: Shows watching status every minute
- INSTANT CLAIM: Watch time already accumulated
- Notifications to ALL users
- Auto-add new drop streamers to watchlist
- Password-protected dashboard + logs page
"""
import urllib.request, json, time, os, threading, random, hashlib
import asyncio
import websockets
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# Supabase database module (persistent storage)
try:
    import supabase_db as db
    USE_SUPABASE = True
    print("[INIT] Using Supabase database for storage")
except Exception as e:
    USE_SUPABASE = False
    print(f"[INIT] Supabase unavailable, using JSON files: {e}")

# ============ CONFIG ============
TG_TOKEN = os.environ.get("TG_TOKEN", "8860462138:AAGkQQF1c-MyTfD3-3WluZNMarcT7HLj4dg")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8182391939"))
INITIAL_COOKIE = os.environ.get("KICK_COOKIE", "")
COOKIE_VALIDATED = False  # Set to True after successful validation
DASH_USER = os.environ.get("DASH_USER", "admin")
DASH_PASS = os.environ.get("DASH_PASS", "kickbot2026")
POLL_INTERVAL = 2
AUTO_WATCH_LIMIT = 1800  # 30 minutes per channel
USER_PREF_LIMIT = None  # No limit for user preferences
MAX_PARALLEL_STREAMS = 10  # Max parallel streams in auto mode
# TIME WINDOW: Only watch during drop hours (IST)
WATCH_START_HOUR = 4   # 4:00 AM IST
WATCH_END_HOUR = 10    # 10:00 AM IST
IST_OFFSET = timedelta(hours=5, minutes=30)
DROPS_API = "https://web.kick.com/api/v1/drops/campaigns"
PROGRESS_API = "https://web.kick.com/api/v1/drops/progress"
CLAIM_API = "https://web.kick.com/api/v1/drops/claim"
CHANNEL_API = "https://kick.com/api/v2/channels/{username}"
CHATROOM_API = "https://kick.com/api/v2/channels/{username}/chatroom"
CHAT_SEND_API = "https://kick.com/api/v2/messages/send/{chatroom_id}"
FOLLOW_API = "https://kick.com/api/v2/channels/{channel_slug}/follow"
FOLLOWED_API = "https://kick.com/api/v2/channels/followed"
FOLLOW_COOLDOWN = {}  # username -> timestamp when retry allowed
CATEGORY_LIVESTREAMS = "https://web.kick.com/api/v1/livestreams?category_id={cat_id}&limit=50&sort=viewer_count_desc"
CATEGORIES_SEARCH = "https://kick.com/api/v2/categories"
WS_TOKEN_API = "https://websockets.kick.com/viewer/v1/token"
WS_URL_TEMPLATE = "wss://websockets.kick.com/viewer/v1/connect?token={token}"
KICK_CLIENT_TOKEN = os.environ.get("KICK_CLIENT_TOKEN", "e1393935a959b4020a4491574f6490129f678acdaa92760471263db43487f823")
STATE_FILE = "tg_bot_state.json"
SUBS_FILE = "tg_subscribers.json"
COOKIE_FILE = "kick_cookie_live.json"
RR_STATE_FILE = "tg_roundrobin_state.json"
HISTORY_FILE = "tg_drop_history.json"
LOG_FILE = "tg_bot_logs.json"
FOLLOWED_CACHE_FILE = "tg_followed_cache.json"
WATCHLIST_FILE = "tg_watchlist.json"  # Manual channel watchlist (persistent)
DASHBOARD_PORT = int(os.environ.get("PORT", "8080"))
KEEPER_INTERVAL = 1800
SLOTS_CATEGORY_ID = None
BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://kick.com",
    "Referer": "https://kick.com/",
    "x-app-platform": "web",
}
# ================================

LOG_BUFFER = []
LOG_LOCK = threading.Lock()
MAX_LOG_BUFFER = 500
_LOG_SAVE_TIMER = 0  # Debounce: only save logs every 30 seconds

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with LOG_LOCK:
        LOG_BUFFER.append({"time": datetime.now().isoformat(), "msg": msg})
        if len(LOG_BUFFER) > MAX_LOG_BUFFER:
            LOG_BUFFER.pop(0)
    _save_logs()

def _save_logs():
    global _LOG_SAVE_TIMER
    try:
        now = time.time()
        if USE_SUPABASE and now - _LOG_SAVE_TIMER < 30:
            return  # Debounce: skip if saved recently
        _LOG_SAVE_TIMER = now
        with LOG_LOCK:
            logs_data = list(LOG_BUFFER[-200:])
        if USE_SUPABASE:
            try:
                db.save_logs(logs_data)
                return
            except Exception as e:
                print(f"[DB] _save_logs fallback: {e}")
        with open(LOG_FILE, "w") as f:
            json.dump(logs_data, f)
    except: pass

def load_logs():
    if USE_SUPABASE:
        try:
            return db.load_logs()
        except Exception as e:
            print(f"[DB] load_logs fallback: {e}")
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f: return json.load(f)
        except: pass
    return []

# ---- Cookie ----
import urllib.parse as _urlparse

# Priority: User-specific > Admin env var > Supabase global > local file
def get_cookie(user_id=None):
    """Get cookie DECODED: user cookie > admin env cookie > Supabase > file"""
    raw = ""
    # 1. User-specific cookie (if user called /setcookie)
    if user_id and USE_SUPABASE:
        try:
            user_data = db.get_user_cookie(user_id)
            if user_data and user_data.get("cookie"):
                raw = user_data["cookie"]
        except Exception as e:
            print(f"[DB] get_user_cookie error: {e}")
    
    # 2. Admin env var cookie (KICK_COOKIE) — ALWAYS use this as default
    if not raw and INITIAL_COOKIE:
        raw = INITIAL_COOKIE
    
    # 3. Supabase global cookie (old fallback)
    if not raw and USE_SUPABASE:
        try:
            c = db.load_cookie_db()
            if c: raw = c
        except Exception as e:
            print(f"[DB] get_cookie fallback: {e}")
    
    # 4. Local file
    if not raw and os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE) as f:
                raw = json.load(f).get("cookie", "")
        except: pass
    
    # ALWAYS decode: %7C → | (Kick needs decoded cookie)
    if raw:
        return _urlparse.unquote(raw)
    return ""

def save_cookie(cookie):
    if USE_SUPABASE:
        try:
            db.save_cookie_db(cookie)
            return
        except Exception as e:
            print(f"[DB] save_cookie fallback: {e}")
    with open(COOKIE_FILE, "w") as f:
        json.dump({"cookie": cookie, "time": datetime.now().isoformat()}, f)

# ---- Session Keeper (HTTP-based, no browser needed) ----
def session_keeper():
    """Keep session alive via HTTP check."""
    global COOKIE_VALIDATED
    log("Session keeper started")
    while True:
        try:
            cookie = get_cookie()
            if not cookie:
                log("[KEEPER] No cookie to refresh - USE /setcookie to add one!")
                tg_send_admin("<b>⚠️ NO COOKIE SET!</b>\nUse /setcookie to update.")
                time.sleep(KEEPER_INTERVAL)
                continue
            
            headers = dict(BASE_HEADERS)
            headers["Cookie"] = "session=" + cookie
            headers["Authorization"] = f"Bearer {cookie}"
            headers["X-Client-Token"] = KICK_CLIENT_TOKEN
            req = urllib.request.Request("https://kick.com/api/v2/users/me", headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read().decode())
            
            if data.get("username"):
                log(f"[KEEPER] Session alive - @{data['username']}")
                COOKIE_VALIDATED = True
            else:
                log("[KEEPER] Cookie valid but no user data")
                COOKIE_VALIDATED = True
            
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log("[KEEPER] Cookie EXPIRED! Use /setcookie to update.")
                COOKIE_VALIDATED = False
                tg_send_admin("<b>🔴 Cookie EXPIRED!</b>\nBot cannot claim drops.\nSend /setcookie with new cookie.")
            elif e.code == 404:
                log("[KEEPER] API 404 - cookie might be OK")
                COOKIE_VALIDATED = True
            else:
                log(f"[KEEPER] HTTP {e.code}")
        except Exception as e:
            log(f"[KEEPER] Error: {str(e)[:80]}")
        time.sleep(KEEPER_INTERVAL)

# ---- Subscribers ----
def load_subs():
    if USE_SUPABASE:
        try:
            return db.load_subs()
        except Exception as e:
            print(f"[DB] load_subs fallback: {e}")
    if os.path.exists(SUBS_FILE):
        try:
            with open(SUBS_FILE) as f: return json.load(f)
        except: pass
    return {}

def save_subs(subs):
    if USE_SUPABASE:
        try:
            db.save_subs(subs)
            return
        except Exception as e:
            print(f"[DB] save_subs fallback: {e}")
    with open(SUBS_FILE, "w") as f: json.dump(subs, f, indent=2)
    _git_commit("Subscribers updated")

def add_sub(chat_id, username=None, first_name=None):
    subs = load_subs()
    sid = str(chat_id)
    is_new = sid not in subs or not subs[sid].get("active", True)
    subs[sid] = {
        "added_at": datetime.now().isoformat(),
        "active": True,
        "username": username or subs.get(sid, {}).get("username", ""),
        "first_name": first_name or subs.get(sid, {}).get("first_name", ""),
    }
    saved = save_subs(subs)
    if is_new:
        log(f"[SUB] New user: {chat_id} @{username or '?'} ({first_name or '?'})")
    return is_new

def remove_sub(chat_id):
    subs = load_subs()
    sid = str(chat_id)
    if sid in subs:
        subs[sid]["active"] = False
        save_subs(subs)

def get_active_subs():
    return [int(cid) for cid, d in load_subs().items() if d.get("active", True)]

# ---- Telegram ----
def tg_send(text, parse_mode="HTML", chat_id=None):
    if not TG_TOKEN: return
    targets = [chat_id] if chat_id else get_active_subs()
    for tid in targets:
        payload = json.dumps({"chat_id": tid, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as e:
            if chat_id == ADMIN_ID:
                log(f"[TG] Admin notify failed: HTTP {e.code}")
        except Exception as e:
            if chat_id == ADMIN_ID:
                log(f"[TG] Admin notify error: {e}")
        time.sleep(0.05)

def tg_send_admin(text):
    log(f"[TG] Sending to admin {ADMIN_ID}: {text[:50]}...")
    tg_send(text, chat_id=ADMIN_ID)

def tg_get_updates(offset=0):
    try:
        resp = urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={offset}&timeout=30", timeout=35)
        return json.loads(resp.read().decode()).get("result", [])
    except: return []

# ---- Kick API ----
def kick_request(url, extra_headers=None, timeout=15, user_id=None):
    headers = dict(BASE_HEADERS)
    cookie = get_cookie(user_id)
    if cookie:
        headers["Cookie"] = "session=" + cookie
        headers["Authorization"] = f"Bearer {cookie}"
    headers["X-Client-Token"] = KICK_CLIENT_TOKEN
    if extra_headers: headers.update(extra_headers)
    req = urllib.request.Request(url)
    for k, v in headers.items(): req.add_header(k, v)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise

def fetch_campaigns():
    try:
        data = kick_request(DROPS_API)
        return data.get("data", []), True
    except urllib.error.HTTPError as e:
        if e.code in (401, 403): return None, False
        return None, True
    except: return None, True

def get_channel_info(username):
    try:
        data = kick_request(CHANNEL_API.format(username=username))
        livestream = data.get("livestream") or {}
        return {
            "channel_id": data.get("id"),
            "livestream_id": livestream.get("id"),
            "username": data.get("slug", username),
            "is_live": bool(livestream.get("is_live")),
        }
    except: return None

def follow_channel(username):
    """Follow a channel on Kick - with 429 backoff.
    Uses browser-like headers including XSRF token for Kasada bypass."""
    global FOLLOW_COOLDOWN
    if username in FOLLOW_COOLDOWN:
        if time.time() < FOLLOW_COOLDOWN[username]:
            return False
    try:
        url = FOLLOW_API.format(channel_slug=username)
        cookie = get_cookie()
        decoded = get_session_token()
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://kick.com",
            "Referer": f"https://kick.com/{username}",
            "x-app-platform": "web",
            "X-Client-Token": KICK_CLIENT_TOKEN,
        }
        if cookie:
            headers["Cookie"] = "session=" + cookie
        if decoded:
            headers["Authorization"] = f"Bearer {decoded}"
        
        body = json.dumps({}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in headers.items(): req.add_header(k, v)
        resp = urllib.request.urlopen(req, timeout=10)
        log(f"[FOLLOW] OK @{username}")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200] if e.fp else ""
        if e.code == 409:
            log(f"[FOLLOW] Already following @{username}")
            return True
        if e.code == 429:
            FOLLOW_COOLDOWN[username] = time.time() + 600
            log(f"[FOLLOW] Kasada (429) @{username} - follow skipped (not critical)")
            return False
        log(f"[FOLLOW] HTTP {e.code} @{username}: {body}")
        return False
    except Exception as e:
        log(f"[FOLLOW] Error @{username}: {e}")
        return False

_followed_cache = {"data": None, "ts": 0, "failed": False}

def get_followed_streamers():
    global _followed_cache
    now = time.time()
    # If failed due to 401, don't retry for 30 minutes
    if _followed_cache["failed"] and now - _followed_cache["ts"] < 1800:
        return _followed_cache["data"] or []
    # Cache for 5 minutes
    if _followed_cache["data"] is not None and now - _followed_cache["ts"] < 300:
        return _followed_cache["data"]
    try:
        data = kick_request(FOLLOWED_API)
        channels = data.get("channels", [])
        result = [ch.get("channel_slug") or ch.get("user_username") for ch in channels if ch.get("channel_slug")]
        _followed_cache = {"data": result, "ts": now, "failed": False}
        return result
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _followed_cache["failed"] = True
            _followed_cache["ts"] = now
            log(f"[FOLLOWED] Cookie expired (401) - skipping for 30 min")
        else:
            log(f"[FOLLOWED] HTTP {e.code}")
        return _followed_cache["data"] or []
    except Exception as e:
        log(f"[FOLLOWED] Error: {e}")
        return _followed_cache["data"] or []

def get_chatroom_id(username):
    try:
        data = kick_request(CHATROOM_API.format(username=username))
        return data.get("data", {}).get("id")
    except: return None

def send_chat_message(chatroom_id, message):
    try:
        headers = dict(BASE_HEADERS)
        cookie = get_cookie()
        if cookie: headers["Cookie"] = "session=" + cookie
        headers["X-Client-Token"] = KICK_CLIENT_TOKEN
        headers["Content-Type"] = "application/json"
        body = json.dumps({"content": message}).encode()
        url = CHAT_SEND_API.format(chatroom_id=chatroom_id)
        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in headers.items(): req.add_header(k, v)
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    except: return None

def try_claim_in_chat(username):
    try:
        chatroom_id = get_chatroom_id(username)
        if chatroom_id: return send_chat_message(chatroom_id, "!claim")
    except: pass
    return None

def get_ws_token(session_token):
    try:
        data = kick_request(WS_TOKEN_API, extra_headers={
            "Authorization": f"Bearer {session_token}",
            "X-Client-Token": KICK_CLIENT_TOKEN,
            "Sec-Fetch-Site": "same-site",
        })
        return data.get("data", {}).get("token")
    except: return None

def get_session_token(user_id=None):
    """Return decoded cookie as Bearer token"""
    return get_cookie(user_id)  # Already decoded by get_cookie()

def validate_cookie_and_get_user(cookie):
    """Validate cookie by fetching followed channels from Kick.
    Returns (valid, username_or_count, user_id_or_channels)
    Note: users/me returns 404 due to Kasada, so we use followed API instead."""
    try:
        # Ensure cookie is decoded (| not %7C)
        try:
            import urllib.parse
            decoded = urllib.parse.unquote(cookie)
        except:
            decoded = cookie
        
        headers = dict(BASE_HEADERS)
        headers["Cookie"] = "session=" + decoded
        headers["X-Client-Token"] = KICK_CLIENT_TOKEN
        
        # Use followed API (users/me returns 404 due to Kasada)
        req = urllib.request.Request("https://kick.com/api/v2/channels/followed")
        for k, v in headers.items():
            req.add_header(k, v)
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        channels = data.get("channels", [])
        
        if channels is not None:  # Valid response
            channel_names = [ch.get("channel_slug", "?") for ch in channels[:3]]
            log(f"[COOKIE] Valid! {len(channels)} followed channels: {', '.join(channel_names)}")
            return True, f"{len(channels)} channels", len(channels)
        return False, None, None
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200] if e.fp else ""
        log(f"[COOKIE] Validation failed: HTTP {e.code}: {body}")
        return False, None, None
    except Exception as e:
        log(f"[COOKIE] Validation error: {e}")
        return False, None, None

_progress_cache = {"data": None, "ts": 0}

def fetch_progress(user_id=None):
    global _progress_cache
    now = time.time()
    # Cache for 5 seconds to avoid 403 rate limits
    cache_key = f"user_{user_id}" if user_id else "global"
    if _progress_cache["data"] is not None and now - _progress_cache["ts"] < 5:
        return _progress_cache["data"]
    try:
        cookie = get_cookie(user_id)
        headers = dict(BASE_HEADERS)
        headers["Cookie"] = "session=" + cookie
        headers["Authorization"] = f"Bearer {get_session_token()}"
        headers["X-Client-Token"] = KICK_CLIENT_TOKEN
        req = urllib.request.Request(PROGRESS_API)
        for k, v in headers.items(): req.add_header(k, v)
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode()).get("data", [])
        _progress_cache = {"data": data, "ts": now}
        return data
    except urllib.error.HTTPError as e:
        log(f"[PROGRESS] HTTP {e.code}: {e.read().decode()[:200] if e.fp else ''}")
        return _progress_cache["data"] or []
    except Exception as e:
        log(f"[PROGRESS] Error: {e}")
        return _progress_cache["data"] or []

def claim_reward(campaign_id, reward_id, user_id=None):
    try:
        cookie = get_cookie(user_id)
        headers = dict(BASE_HEADERS)
        headers["Cookie"] = "session=" + cookie
        headers["Authorization"] = f"Bearer {cookie}"
        headers["X-Client-Token"] = KICK_CLIENT_TOKEN
        headers["Content-Type"] = "application/json"
        body = json.dumps({"campaign_id": campaign_id, "reward_id": reward_id}).encode()
        req = urllib.request.Request(CLAIM_API, data=body, method="POST")
        for k, v in headers.items(): req.add_header(k, v)
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode())
        log(f"[CLAIM] OK: {result}")
        return result
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300] if e.fp else ""
        log(f"[CLAIM] HTTP {e.code}: {body}")
        return None
    except Exception as e:
        log(f"[CLAIM] Error: {e}")
        return None

def search_slots_category():
    """Find Slots & Casino category ID"""
    global SLOTS_CATEGORY_ID
    if SLOTS_CATEGORY_ID: return SLOTS_CATEGORY_ID
    try:
        data = kick_request(CATEGORIES_SEARCH)
        categories = data if isinstance(data, list) else data.get("data", [])
        for cat in categories:
            name = cat.get("name", "").lower()
            if "slots" in name or "casino" in name:
                SLOTS_CATEGORY_ID = cat.get("id")
                log(f"[CAT] Slots & Casino category ID: {SLOTS_CATEGORY_ID}")
                return SLOTS_CATEGORY_ID
    except: pass
    return None

def get_slots_streamers():
    """Get live streamers from Slots & Casino category as fallback"""
    cat_id = search_slots_category()
    if not cat_id: return []
    try:
        data = kick_request(CATEGORY_LIVESTREAMS.format(cat_id=cat_id))
        streams = data.get("data", []) if isinstance(data, dict) else data
        result = []
        for s in streams:
            user = s.get("broadcaster_user", {}) if "broadcaster_user" in s else s.get("channel", {})
            username = user.get("username", "") or s.get("slug", "")
            channel_id = user.get("id") or s.get("channel_id")
            livestream_id = s.get("id") or s.get("livestream_id")
            if username and channel_id:
                result.append({"username": username, "channel_id": channel_id, "livestream_id": livestream_id})
        log(f"[CAT] Found {len(result)} Slots & Casino streamers")
        return result
    except: return []

def smart_claim_check(username=None):
    """Smart claim check - progress is RATIO (0-1), not seconds"""
    global _progress_cache
    try:
        progress = fetch_progress()
        if not progress: return
        claimed_any = False
        for item in progress:
            campaign_id = item.get("campaign_id") or item.get("id")
            total_progress = item.get("progress_units", 0)
            for r in item.get("rewards", []):
                if r.get("claimed"): continue
                required = r.get("required_units", 0)
                ratio = r.get("progress", 0)
                reward_id = r.get("reward_id") or r.get("id")
                claim_key = f"{campaign_id}_{reward_id}"
                if required > 0 and ratio >= 1.0:
                    log(f"[CLAIM] CLAIMABLE! {total_progress}/{required}s (ratio={ratio})")
                    result = claim_reward(campaign_id, reward_id)
                    if result:
                        claimed_any = True
                        tg_send(f"<b>🎉 REWARD CLAIMED!</b>\nStreamer: @{username or '?'}\nCampaign: ATK Drop\nReward: {r.get('name', reward_id[:16])}")
                    if username:
                        try_claim_in_chat(username)
                elif required > 0 and ratio > 0:
                    remaining_secs = required - total_progress
                    pct = int(ratio * 100)
                    log(f"[CLAIM] Progress: @{username or '?'} {total_progress}/{required}s ({pct}%, {remaining_secs:.0f}s left)")
        return claimed_any
    except Exception as e:
        log(f"[CLAIM] Check error: {e}")
        return False

async def send_user_event(ws, channel_id, livestream_id):
    event = {"type": "user_event", "data": {"message": {
        "name": "tracking.user.watch.livestream",
        "channel_id": channel_id,
        "livestream_id": int(livestream_id) if livestream_id else int(channel_id),
    }}}
    await ws.send(json.dumps(event))

def fmt_duration(seconds):
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    if h > 0: return f"{h}h {m}m"
    if m > 0: return f"{m}m {s}s"
    return f"{s}s"

def fmt_countdown(iso_str):
    if not iso_str: return "N/A"
    try:
        target = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        diff = target - datetime.now()
        if diff.total_seconds() < 0: return "EXPIRED"
        days = diff.days
        hours, rem = divmod(diff.seconds, 3600)
        mins, _ = divmod(rem, 60)
        parts = []
        if days > 0: parts.append(f"{days}d")
        if hours > 0: parts.append(f"{hours}h")
        parts.append(f"{mins}m")
        return " ".join(parts)
    except: return "N/A"

def is_stake_drop(c):
    """Check if this is a Stake $5 drop (short ~2min window, not long ATK-type campaigns)."""
    connect = c.get("connect_url", "").lower()
    name = c.get("name", "").lower()
    channels = c.get("channels", [])
    has_stake = False
    if "stake.com" in connect or "stake" in connect: has_stake = True
    if "stake" in name: has_stake = True
    for ch in channels:
        username = (ch.get("slug", "") + (ch.get("user") or {}).get("username", "")).lower()
        if "stake" in username: has_stake = True
    cat = c.get("category", {})
    if isinstance(cat, dict) and "stake" in cat.get("name", "").lower(): has_stake = True
    if not has_stake:
        return False
    # Real Stake drops have SHORT windows (~2 min = 120s). ATK-type have hours.
    # Check duration: if campaign lasts > 10 min, it's NOT a Stake drop
    start_at = c.get("start_at", "")
    end_at = c.get("end_at", "")
    if start_at and end_at:
        try:
            st = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
            en = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
            duration = (en - st).total_seconds()
            if duration > 600:  # > 10 min = NOT a Stake drop
                return False
        except: pass
    return True

def fmt_campaign(c):
    name = c.get("name", "?")
    status = c.get("status", "?")
    connect = c.get("connect_url", "none")
    cat = c.get("category", {}).get("name", "?") if isinstance(c.get("category"), dict) else "?"
    channels = c.get("channels", [])
    ch_str = ", ".join([(ch.get("user") or {}).get("username", ch.get("slug", "?")) for ch in channels]) if channels else "global"
    rewards = c.get("rewards", [])
    rew_str = ", ".join([r.get("name", "?") for r in rewards[:3]])
    if len(rewards) > 3: rew_str += f" +{len(rewards)-3} more"
    s = {"active": "LIVE", "upcoming": "SOON", "expired": "EXP"}.get(status, "?")
    return f"{s} {name}\n  Cat: {cat}\n  Channels: {ch_str}\n  Rewards: {rew_str}\n  Connect: {connect[:60]}"

# ---- History ----
def load_history():
    if USE_SUPABASE:
        try:
            return db.load_history()
        except Exception as e:
            print(f"[DB] load_history fallback: {e}")
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f: return json.load(f)
        except: pass
    return []

def save_history(history):
    if USE_SUPABASE:
        try:
            db.save_history(history)
            return
        except Exception as e:
            print(f"[DB] save_history fallback: {e}")
    with open(HISTORY_FILE, "w") as f: json.dump(history, f, indent=2, default=str)

def add_to_history(campaign, event_type="seen"):
    if USE_SUPABASE:
        try:
            db.add_to_history_db(campaign, event_type)
            return
        except Exception as e:
            print(f"[DB] add_to_history fallback: {e}")
    history = load_history()
    entry = {
        "id": campaign.get("id", "?"),
        "name": campaign.get("name", "?"),
        "status": campaign.get("status", "?"),
        "channels": [(ch.get("user") or {}).get("username", ch.get("slug", "?")) for ch in campaign.get("channels", [])],
        "end_at": campaign.get("end_at", ""),
        "event": event_type,
        "time": datetime.now().isoformat(),
    }
    existing_ids = [h.get("id") for h in history]
    if entry["id"] not in existing_ids:
        history.append(entry)
        save_history(history)

# ---- Followed Cache ----
def load_followed_cache():
    if os.path.exists(FOLLOWED_CACHE_FILE):
        try:
            with open(FOLLOWED_CACHE_FILE) as f: return json.load(f)
        except: pass
    return {"usernames": [], "last_updated": None}

def save_followed_cache(cache):
    with open(FOLLOWED_CACHE_FILE, "w") as f: json.dump(cache, f, indent=2)

# ---- Manual Watchlist (Persistent) ----
def load_watchlist():
    """Load manually added channels."""
    if USE_SUPABASE:
        try:
            return db.load_watchlist()
        except Exception as e:
            print(f"[DB] load_watchlist fallback: {e}")
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f: return json.load(f)
        except: pass
    return {"channels": [], "added_by": {}, "last_updated": None}

def save_watchlist(watchlist):
    """Save watchlist."""
    if USE_SUPABASE:
        try:
            db.save_watchlist(watchlist)
            return
        except Exception as e:
            print(f"[DB] save_watchlist fallback: {e}")
    watchlist["last_updated"] = datetime.now().isoformat()
    with open(WATCHLIST_FILE, "w") as f: json.dump(watchlist, f, indent=2)
    _git_commit("Watchlist updated")

def _git_commit(message="Auto-save"):
    """Commit changes to GitHub for persistent storage.
    Skipped when using Supabase (not needed)."""
    if USE_SUPABASE:
        return  # Not needed - data is in Supabase
    try:
        import subprocess
        subprocess.run(["git", "add", "*.json"], capture_output=True, timeout=10)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            capture_output=True, text=True, timeout=10
        )
        subprocess.run(["git", "push"], capture_output=True, timeout=30)
        log(f"[GIT] Committed: {message}")
    except: pass

def add_to_watchlist(username, added_by="manual"):
    """Add a channel to the persistent watchlist."""
    global _watchlist_local_cache
    username = username.lower().strip("@").strip()
    _watchlist_local_cache.add(username)
    if USE_SUPABASE:
        try:
            return db.add_to_watchlist_db(username, added_by)
        except Exception as e:
            print(f"[DB] add_to_watchlist fallback: {e}")
    watchlist = load_watchlist()
    if username not in watchlist["channels"]:
        watchlist["channels"].append(username)
        watchlist["added_by"][username] = {"by": added_by, "time": datetime.now().isoformat()}
        save_watchlist(watchlist)
        log(f"[WATCHLIST] Added @{username} by {added_by}")
        return True, f"@{username} added to watchlist!"
    return False, f"@{username} already in watchlist."

def remove_from_watchlist(username):
    """Remove a channel from the watchlist."""
    if USE_SUPABASE:
        try:
            return db.remove_from_watchlist_db(username)
        except Exception as e:
            print(f"[DB] remove_from_watchlist fallback: {e}")
    watchlist = load_watchlist()
    username = username.lower().strip("@").strip()
    if username in watchlist["channels"]:
        watchlist["channels"].remove(username)
        watchlist["added_by"].pop(username, None)
        save_watchlist(watchlist)
        log(f"[WATCHLIST] Removed @{username}")
        return True, f"@{username} removed from watchlist!"
    return False, f"@{username} not in watchlist."

_watchlist_local_cache = set()

def get_watchlist():
    """Get all channels in the watchlist."""
    global _watchlist_local_cache
    if _watchlist_local_cache:
        return list(_watchlist_local_cache)
    if USE_SUPABASE:
        try:
            result = db.get_watchlist_db()
            _watchlist_local_cache = set(result) if result else set()
            return result
        except Exception as e:
            print(f"[DB] get_watchlist fallback: {e}")
    watchlist = load_watchlist()
    result = watchlist.get("channels", [])
    _watchlist_local_cache = set(result)
    return result

def follow_drop_streamers(campaigns):
    """Follow all streamers from active Stake drops"""
    cache = load_followed_cache()
    already_followed = set(cache.get("usernames", []))
    followed_count = 0
    for c in campaigns:
        if not is_stake_drop(c): continue
        for ch in c.get("channels", []):
            username = ch.get("slug") or (ch.get("user") or {}).get("username")
            if username and username not in already_followed:
                if follow_channel(username):
                    cache["usernames"].append(username)
                    followed_count += 1
                    time.sleep(0.5)
    cache["last_updated"] = datetime.now().isoformat()
    save_followed_cache(cache)
    if followed_count > 0:
        log(f"[FOLLOW] Followed {followed_count} new drop streamers")
    return followed_count

# ---- State ----
def load_state():
    if USE_SUPABASE:
        try:
            return db.load_state()
        except Exception as e:
            print(f"[DB] load_state fallback: {e}")
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: return json.load(f)
        except: pass
    return {"known": {}, "polls": 0, "last_poll": None}

def save_state(state):
    if USE_SUPABASE:
        try:
            db.save_state(state)
            return
        except Exception as e:
            print(f"[DB] save_state fallback: {e}")
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2, default=str)
    _git_commit("State updated")

# ============================================================
#  SINGLE WATCHER
# ============================================================
class SingleWatcher:
    def __init__(self):
        self.active = False
        self.watchers = {}
        self._lock = threading.Lock()
        self.started_at = None

    def start(self, chat_id, usernames):
        """Start watching multiple streamers simultaneously"""
        if isinstance(usernames, str):
            usernames = [usernames]

        started = []
        failed = []
        for username in usernames:
            username = username.strip("@").strip()
            if not username: continue
            if username in self.watchers:
                failed.append(f"@{username} (already watching)")
                continue
            info = get_channel_info(username)
            if not info:
                failed.append(f"@{username} (not found)")
                continue
            if not info.get("is_live"):
                failed.append(f"@{username} (offline)")
                continue
            watcher = SingleStreamWatcher(username, info["channel_id"], info.get("livestream_id"), self)
            with self._lock:
                self.watchers[username] = watcher
                self.active = True
                if not self.started_at:
                    self.started_at = datetime.now()
            follow_channel(username)
            threading.Thread(target=watcher.run, args=(chat_id,), daemon=True).start()
            started.append(f"@{username}")
            log(f"[WATCH] Started @{username}")
            time.sleep(0.3)

        if not started:
            return False, "No streamers started.\n" + "\n".join(failed)

        msg = f"<b>WATCHING {len(started)} STREAMERS!</b>\n\n"
        msg += "\n".join(started)
        if failed:
            msg += "\n\n<b>Failed:</b>\n" + "\n".join(failed)
        msg += "\n\n/watchstop to stop all"
        return True, msg

    def stop(self, reason="manual"):
        if not self.watchers:
            return False, "Not watching."
        with self._lock:
            usernames = list(self.watchers.keys())
            total_events = sum(w.events_sent for w in self.watchers.values())
            total_time = sum(w.watch_time for w in self.watchers.values())
            total_claimed = sum(w.claimed for w in self.watchers.values())
            for w in self.watchers.values():
                w.stop_event.set()
            self.watchers.clear()
            self.active = False
            self.started_at = None
        elapsed = (datetime.now() - self.started_at).total_seconds() if self.started_at else 0
        msg = (f"<b>ALL WATCHERS STOPPED!</b> ({reason})\n\n"
                f"Streamers: {', '.join(['@'+u for u in usernames])}\n"
                f"Total events: {total_events}\n"
                f"Total time: {fmt_duration(total_time)}\n"
                f"Claimed: {total_claimed}")
        return True, msg

    def get_status(self):
        if not self.watchers:
            return "<b>WATCHER: IDLE</b>\n\n/use /watchtest <user1> <user2>..."
        lines = []
        total_events = 0
        total_time = 0
        total_claimed = 0
        with self._lock:
            for username, w in self.watchers.items():
                elapsed = (datetime.now() - w.started_at).total_seconds() if w.started_at else 0
                lines.append(f"@{username}: {fmt_duration(elapsed)} | {w.events_sent} events | {w.claimed} claimed")
                total_events += w.events_sent
                total_time += w.watch_time
                total_claimed += w.claimed
        return (f"<b>WATCHER: ACTIVE</b> ({len(self.watchers)} streams)\n\n"
                + "\n".join(lines) +
                f"\n\nTotal: {fmt_duration(total_time)} | {total_events} events | {total_claimed} claimed\n"
                f"/watchstop to stop all")

    def on_watcher_done(self, username):
        with self._lock:
            self.watchers.pop(username, None)
            if not self.watchers:
                self.active = False
        log(f"[WATCH] @{username} done")


class SingleStreamWatcher:
    def __init__(self, username, channel_id, livestream_id, parent):
        self.username = username
        self.channel_id = channel_id
        self.livestream_id = livestream_id
        self.parent = parent
        self.stop_event = threading.Event()
        self.started_at = datetime.now()
        self.watch_time = 0
        self.events_sent = 0
        self.claimed = 0
        self._lock = threading.Lock()

    def run(self, chat_id):
        try:
            while not self.stop_event.is_set():
                try: self._ws_connect(); break
                except Exception as e:
                    log(f"[WATCH] WS error @{self.username}: {e}")
                    if not self.stop_event.is_set(): time.sleep(5)
            if self.stop_event.is_set(): return
            info = get_channel_info(self.username)
            if not info or not info.get("is_live"):
                tg_send(f"@{self.username} went offline.", chat_id=chat_id)
        except Exception as e:
            log(f"[WATCH] Fatal @{self.username}: {e}")
        finally:
            self.parent.on_watcher_done(self.username)

    def _ws_connect(self):
        ws_token = get_ws_token(get_cookie())
        if not ws_token: raise Exception("No WS token")
        ws_url = WS_URL_TEMPLATE.format(token=ws_token)
        headers = dict(BASE_HEADERS)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try: loop.run_until_complete(self._ws_loop(ws_url, headers))
        finally: loop.close()

    async def _ws_loop(self, ws_url, headers):
        username = self.username
        channel_id = self.channel_id
        livestream_id = self.livestream_id
        async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10) as ws:
            await ws.send(json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}}))
            try: await asyncio.wait_for(ws.recv(), timeout=5)
            except: pass
            await send_user_event(ws, channel_id, livestream_id)
            with self._lock: self.events_sent += 1
            log(f"[WATCH] Connected @{username} channel_id={channel_id} livestream_id={livestream_id}")
            last_ue = time.time()
            last_ping = time.time()
            last_alive = time.time()
            last_refresh = time.time()
            watch_start = time.time()
            while not self.stop_event.is_set():
                now = time.time()
                elapsed_since_start = now - watch_start
                if now - last_ping >= 20:
                    try:
                        await ws.send(json.dumps({"type": "ping"}))
                        last_ping = now
                        try: await asyncio.wait_for(ws.recv(), timeout=3)
                        except: pass
                    except: pass
                if now - last_ue >= 60:
                    await send_user_event(ws, channel_id, livestream_id)
                    with self._lock:
                        self.events_sent += 1
                        self.watch_time += 60
                    last_ue = now
                    if elapsed_since_start >= 60:
                        smart_claim_check(username)
                    log(f"[WATCH] event #{self.events_sent} @{username} ({fmt_duration(self.watch_time)})")
                if now - last_refresh >= 300:
                    last_refresh = now
                    try:
                        info = get_channel_info(username)
                        if info and info.get("livestream_id"):
                            if info["livestream_id"] != livestream_id:
                                livestream_id = info["livestream_id"]
                                self.livestream_id = livestream_id
                                log(f"[WATCH] Updated livestream_id={livestream_id} for @{username}")
                    except: pass
                if now - last_alive >= 300:
                    last_alive = now
                    info = get_channel_info(username)
                    if not info or not info.get("is_live"):
                        log(f"[WATCH] @{username} offline")
                        return
                await asyncio.sleep(1)

single_watcher = SingleWatcher()

# ============================================================
#  PARALLEL WATCHER
# ============================================================
class ParallelWatcher:
    def __init__(self):
        self.active = False
        self.stop_event = threading.Event()
        self.main_thread = None
        self.watchers = {}
        self._lock = threading.Lock()
        self.state = {"active": False, "watching": {}, "total_watched": 0,
                      "total_watch_time": 0, "rewards_claimed": 0, "started_at": None}
        self._save()

    def _save(self):
        with self._lock:
            with open(RR_STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2, default=str)

    def start(self):
        if self.active: return False, "Already running! /watchroundstop first."
        self.active = True
        self.stop_event.clear()
        self.state = {
            "active": True, "watching": {}, "total_watched": 0,
            "total_watch_time": 0, "rewards_claimed": 0,
            "started_at": datetime.now().isoformat()
        }
        self._save()
        self.main_thread = threading.Thread(target=self._run, daemon=True)
        self.main_thread.start()
        return True, ("<b>PARALLEL WATCHER STARTED!</b>\n\n"
                      "All live streams watched simultaneously.\n"
                      "Instant claim. /watchroundstop to stop.")

    def stop(self):
        if not self.active: return False, "Not running!"
        self.stop_event.set()
        self.active = False
        self.state["active"] = False
        with self._lock:
            for u in list(self.watchers.keys()):
                self.watchers[u].stop_event.set()
            self.watchers.clear()
        self._save()
        elapsed = 0
        if self.state.get("started_at"):
            try: elapsed = (datetime.now() - datetime.fromisoformat(self.state["started_at"])).total_seconds()
            except: pass
        return True, (f"<b>PARALLEL WATCHER STOPPED!</b>\n\n"
                      f"Watched: {self.state.get('total_watched', 0)}\n"
                      f"Time: {fmt_duration(elapsed)}\n"
                      f"Claimed: {self.state.get('rewards_claimed', 0)}")

    def get_status(self):
        if not self.active: return "<b>PARALLEL: IDLE</b>"
        watching = self.state.get("watching", {})
        watched = self.state.get("total_watched", 0)
        claimed = self.state.get("rewards_claimed", 0)
        elapsed = 0
        if self.state.get("started_at"):
            try: elapsed = (datetime.now() - datetime.fromisoformat(self.state["started_at"])).total_seconds()
            except: pass
        streamers = "\n".join([f"  @{u} ({d['min']}m)" for u, d in watching.items()]) or "  None"
        return (f"<b>PARALLEL: ACTIVE</b>\n\n"
                f"Now watching ({len(watching)}):\n{streamers}\n\n"
                f"Watched: {watched}\nTime: {fmt_duration(elapsed)}\nClaimed: {claimed}")

    def _run(self):
        log("[PW] Parallel watcher started")
        followed_on_startup = False
        while not self.stop_event.is_set():
            try:
                all_streamers = set()
                campaigns, _ = fetch_campaigns()

                # Follow drop streamers (once on startup only)
                if campaigns and not followed_on_startup:
                    follow_drop_streamers(campaigns)
                    followed_on_startup = True

                # Source 1: Active campaign channels
                if campaigns:
                    for c in campaigns:
                        if is_stake_drop(c) and c.get("status") == "active":
                            for ch in c.get("channels", []):
                                username = ch.get("slug") or (ch.get("user") or {}).get("username")
                                if username: all_streamers.add(username)

                # Source 2: All followed streamers (not just campaigns)
                try:
                    followed = get_followed_streamers()
                    for u in followed: all_streamers.add(u)
                except: pass

                # Source 3: Slots & Casino category (ALWAYS check, not just fallback)
                try:
                    slots = get_slots_streamers()
                    for s in slots:
                        if s.get("username"): all_streamers.add(s["username"])
                except: pass

                if not all_streamers:
                    log("[PW] No streamers found from any source, waiting 30s...")
                    time.sleep(30)
                    continue

                log(f"[PW] Checking {len(all_streamers)} streamers for live status...")

                # Check which are live
                live_streamers = []
                for username in list(all_streamers):
                    if self.stop_event.is_set(): break
                    if username in self.watchers: continue
                    info = get_channel_info(username)
                    if info and info.get("is_live"):
                        info["username"] = username
                        live_streamers.append(info)
                    time.sleep(0.2)

                if not live_streamers:
                    log(f"[PW] No live ({len(all_streamers)} checked), waiting 30s...")
                    time.sleep(30)
                    continue

                log(f"[PW] Found {len(live_streamers)} LIVE streamers | Watching: {len(self.watchers)}")

                # Start watching live streamers (max 7 concurrent)
                MAX_CONCURRENT = 7
                for streamer in live_streamers:
                    if self.stop_event.is_set(): break
                    if len(self.watchers) >= MAX_CONCURRENT: break
                    username = streamer["username"]
                    if username in self.watchers: continue
                    watch_min = random.randint(3, 15)
                    watcher = StreamWatcher(
                        username, streamer["channel_id"],
                        streamer.get("livestream_id"), watch_min * 60, self
                    )
                    with self._lock:
                        self.watchers[username] = watcher
                        self.state["watching"][username] = {"min": watch_min, "started": datetime.now().isoformat()}
                    self._save()
                    threading.Thread(target=watcher.run, daemon=True).start()
                    follow_channel(username)
                    log(f"[PW] Started @{username} for {watch_min} min")
                    tg_send(f"<b>PW:</b> Watching @{username} ({watch_min} min)")

                # Smart claim check after starting watchers
                smart_claim_check()

                time.sleep(30)

            except Exception as e:
                log(f"[PW] Error: {e}")
                time.sleep(30)

        log("[PW] Parallel watcher stopped")

    def on_watcher_done(self, username):
        with self._lock:
            self.watchers.pop(username, None)
            self.state["watching"].pop(username, None)
            self.state["total_watched"] = self.state.get("total_watched", 0) + 1
        self._save()
        log(f"[PW] @{username} done")


class StreamWatcher:
    def __init__(self, username, channel_id, livestream_id, target_seconds, pw):
        self.username = username
        self.channel_id = channel_id
        self.livestream_id = livestream_id
        self.target_seconds = target_seconds
        self.pw = pw
        self.stop_event = threading.Event()

    def run(self):
        try:
            ws_token = get_ws_token(get_cookie())
            if not ws_token: return
            ws_url = WS_URL_TEMPLATE.format(token=ws_token)
            headers = dict(BASE_HEADERS)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try: loop.run_until_complete(self._watch(ws_url, headers))
            finally: loop.close()
        except Exception as e:
            log(f"[SW] Error @{self.username}: {e}")
        finally:
            self.pw.on_watcher_done(self.username)

    async def _watch(self, ws_url, headers):
        username = self.username
        channel_id = self.channel_id
        livestream_id = self.livestream_id
        async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10) as ws:
            await ws.send(json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}}))
            try: await asyncio.wait_for(ws.recv(), timeout=5)
            except: pass
            await send_user_event(ws, channel_id, livestream_id)
            log(f"[SW] Connected @{username} channel_id={channel_id} livestream_id={livestream_id}")
            start = time.time()
            last_ue = time.time()
            last_ping = time.time()
            last_refresh = time.time()
            ev_count = 1
            while not self.stop_event.is_set():
                now = time.time()
                elapsed = now - start
                if elapsed >= self.target_seconds:
                    log(f"[SW] Done @{username} ({int(elapsed)}s)")
                    return
                if now - last_ping >= 20:
                    try:
                        await ws.send(json.dumps({"type": "ping"}))
                        last_ping = now
                        try: await asyncio.wait_for(ws.recv(), timeout=3)
                        except: pass
                    except: pass
                if now - last_ue >= 60:
                    await send_user_event(ws, channel_id, livestream_id)
                    ev_count += 1
                    last_ue = now
                    remaining = int(self.target_seconds - elapsed)
                    log(f"[SW] event #{ev_count} @{username} ({remaining}s left)")
                if now - last_refresh >= 300:
                    last_refresh = now
                    try:
                        info = get_channel_info(username)
                        if info and info.get("livestream_id"):
                            if info["livestream_id"] != livestream_id:
                                livestream_id = info["livestream_id"]
                                self.livestream_id = livestream_id
                                log(f"[SW] Updated livestream_id={livestream_id} for @{username}")
                    except: pass
                if ev_count % 5 == 0:
                    info = get_channel_info(username)
                    if not info or not info.get("is_live"):
                        log(f"[SW] @{username} offline")
                        return
                await asyncio.sleep(1)

pw = ParallelWatcher()

# ============================================================
#  DASHBOARD (Password Protected)
# ============================================================
class DashboardHandler(BaseHTTPRequestHandler):
    def _check_auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth or not auth.startswith("Basic "):
            return False
        import base64
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, pwd = decoded.split(":", 1)
            return user == DASH_USER and pwd == DASH_PASS
        except: return False

    def _send_auth_required(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Kick Bot"')
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h1>401 Unauthorized</h1><p>Admin access only.</p>")

    def do_GET(self):
        if not self._check_auth():
            self._send_auth_required()
            return

        if self.path == "/logs":
            self._serve_logs()
            return

        state = load_state()
        known = state.get("known", {})
        subs = load_subs()
        active = sum(1 for s in subs.values() if s.get("active", True))
        rows = ""
        for sid, d in subs.items():
            st = "Active" if d.get("active", True) else "Inactive"
            color = "#4CAF50" if d.get("active", True) else "#f44336"
            uname = d.get("username", "")
            fname = d.get("first_name", "")
            display = f"@{uname}" if uname else (fname or sid)
            rows += f"<tr><td>{display}</td><td style='color:{color}'>{st}</td><td>{d.get('added_at','?')[:16]}</td></tr>"
        drops = ""
        for cid, c in known.items():
            st = c.get("status", "?")
            color = {"active": "#4CAF50", "expired": "#999"}.get(st, "#FF9800")
            drops += f"<tr><td>{c.get('name','?')}</td><td style='color:{color}'>{st}</td></tr>"
        pw_s = "ACTIVE" if pw.active else "IDLE"
        pw_c = "#4CAF50" if pw.active else "#999"
        watching = pw.state.get("watching", {})
        w_list = ", ".join([f"@{u}" for u in watching]) or "None"
        sw_s = "ACTIVE" if single_watcher.active else "IDLE"
        sw_users = ", ".join([f"@{u}" for u in single_watcher.watchers.keys()]) or "None"
        dh_s = "ACTIVE" if drop_hunter.active else "STOPPED"
        dh_c = "#4CAF50" if drop_hunter.active else "#999"
        dh_watching = len(drop_hunter.watching_channels)
        dh_known = len(drop_hunter.known_all_channels)
        dh_claimed = len(drop_hunter.claimed_rewards)
        dh_retries = len(drop_hunter.claim_retry_queue)
        # User cookies section
        user_cookies_html = ""
        if USE_SUPABASE:
            try:
                user_cookies = db.get_all_user_cookies()
                if user_cookies:
                    user_cookies_html = "<div class='c'><h2>User Cookies</h2><table><tr><th>TG ID</th><th>Kick Username</th><th>Kick User ID</th><th>Last Used</th></tr>"
                    for uc in user_cookies:
                        last_used = uc.get('last_used', '?')[:16] if uc.get('last_used') else '?'
                        user_cookies_html += f"<tr><td>{uc.get('user_id', '?')}</td><td>@{uc.get('kick_username', '?')}</td><td>{uc.get('kick_user_id', '?')}</td><td>{last_used}</td></tr>"
                    user_cookies_html += "</table></div>"
            except: pass
        
        html = f"""<!DOCTYPE html><html><head><title>Kick Drops v25 - Test Mode + Logs</title>
<meta http-equiv="refresh" content="30">
<style>body{{font-family:Arial;background:#1a1a2e;color:#eee;padding:20px}}h1{{color:#e94560}}.c{{background:#16213e;padding:20px;border-radius:10px;margin:10px 0}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #333}}th{{background:#0f3460}}a{{color:#e94560}}</style></head><body>
<h1>Kick Stake Drops Bot v25 - Test Mode + Logs</h1>
<p><a href="/logs">View Logs</a></p>
<div class="c"><h2>Status</h2><p>Polls: {state.get('polls',0)}</p><p>Last: {state.get('last_poll','never')}</p></div>
<div class="c"><h2>Drop Hunter v3 (Always-On)</h2><p style='color:{dh_c};font-size:1.2em'><b>{dh_s}</b></p><p>Watching: {dh_watching} channels</p><p>Known: {dh_known} channels</p><p>Claimed: {dh_claimed}</p><p>Pending retries: {dh_retries}</p></div>
{user_cookies_html}
<div class="c"><h2>Parallel Watcher</h2><p style='color:{pw_c};font-size:1.2em'><b>{pw_s}</b></p><p>Watching: {w_list}</p><p>Watched: {pw.state.get('total_watched',0)}</p><p>Claimed: {pw.state.get('rewards_claimed',0)}</p></div>
<div class="c"><h2>Single Watcher</h2><p><b>{sw_s}</b></p><p>{sw_users}</p></div>
<div class="c"><h2>Users ({active}/{len(subs)})</h2><table><tr><th>User</th><th>Status</th><th>Joined</th></tr>{rows}</table></div>
<div class="c"><h2>Drops ({len(known)})</h2><table><tr><th>Name</th><th>Status</th></tr>{drops}</table></div></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _serve_logs(self):
        logs = load_logs()
        log_lines = ""
        for l in logs[-100:]:
            msg = l.get("msg", "")
            color = "#4CAF50" if "OK" in msg or "SUCCESS" in msg or "Connected" in msg else "#eee"
            if "ERROR" in msg or "error" in msg.lower(): color = "#f44336"
            if "CLAIM" in msg: color = "#FF9800"
            log_lines += f"<p style='color:{color};margin:2px 0;font-family:monospace;font-size:13px'>{l.get('time','')[:19]} | {msg}</p>"
        html = f"""<!DOCTYPE html><html><head><title>Bot Logs</title>
<meta http-equiv="refresh" content="10">
<style>body{{font-family:Arial;background:#1a1a2e;color:#eee;padding:20px}}h1{{color:#e94560}}a{{color:#e94560}}</style></head><body>
<h1>Bot Logs</h1><p><a href="/">Back to Dashboard</a></p>
<div style='background:#0f3460;padding:15px;border-radius:10px;max-height:80vh;overflow-y:auto'>
{log_lines}
</div></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, *a): pass

# ---- Commands ----
def handle_command(cmd, chat_id, text="", username=None, first_name=None):
    is_new = add_sub(chat_id, username=username, first_name=first_name)

    if cmd in ("/start", "/help"):
        if cmd == "/start" and is_new:
            display = f"@{username}" if username else f"ID: {chat_id}"
            name_display = f" ({first_name})" if first_name else ""
            tg_send_admin(f"<b>NEW USER!</b>\n{display}{name_display}\nTotal: {len(get_active_subs())}")
        tg_send(
            "<b>Kick Drops Bot v20</b>\n\n"
            "<b>DROP HUNTER (auto):</b>\n"
            "/dh - Drop Hunter status\n"
            "/dhretry - Claim retry queue\n\n"
            "<b>WATCHLIST (Stake Drops):</b>\n"
            "/addchannel &lt;user&gt; - Add channel to watch 24/7\n"
            "/removechannel &lt;user&gt; - Remove from watchlist\n"
            "/channels - List all watchlist channels\n\n"
            "<b>MY STREAMERS (Custom):</b>\n"
            "/addstreamer &lt;user&gt; - Add your favorite streamer\n"
            "/removestreamer &lt;user&gt; - Remove from your list\n"
            "/mystreamers - See your watch list\n"
            "/mystreamersclear - Clear your list\n\n"
            "<b>COMMANDS:</b>\n"
            "/all - All campaigns\n"
            "/stake - Stake campaigns\n"
            "/live - Live streams\n"
            "/history - Drop history\n"
            "/status - Bot status\n\n"
            "<b>MANUAL WATCH:</b>\n"
            "/watchtest &lt;user1&gt; [user2] - Watch streams\n"
            "/watchstop - Stop\n"
            "/watchstatus - Watch info\n"
            "/startwatching - Test: watch all known live (1 hour)\n\n"
            "<b>CONFIG:</b>\n"
            "/setcookie - Update cookie\n"
            "/checkcookie - Check if cookie is valid\n"
            "/stop - Unsubscribe\n"
            "/help - This",
            chat_id=chat_id)

    elif cmd == "/stop":
        remove_sub(chat_id)
        tg_send("Unsubscribed.", chat_id=chat_id)

    elif cmd == "/dh":
        watching = drop_hunter.watching_channels
        claimed = drop_hunter.claimed_rewards
        known = drop_hunter.known_all_channels
        retries = drop_hunter.claim_retry_queue
        status = "ACTIVE" if drop_hunter.active else "STOPPED"
        msg = f"<b>DROP HUNTER v3: {status}</b>\n\n"
        msg += f"Watching: {len(watching)} channels\n"
        msg += f"Known channels: {len(known)}\n"
        msg += f"Claimed: {len(claimed)}\n"
        msg += f"Pending retries: {len(retries)}\n"
        if watching:
            msg += f"\n<b>Watching:</b>\n"
            for slug, info in list(watching.items())[:15]:
                elapsed = int(time.time() - info.get("started_at", time.time()))
                events = info.get("events_sent", 0)
                msg += f"  @{slug}: {fmt_duration(elapsed)} | {events} ev\n"
            if len(watching) > 15:
                msg += f"  ... +{len(watching)-15} more\n"
        tg_send(msg, chat_id=chat_id)

    elif cmd == "/dhretry":
        msg = drop_hunter.get_retry_status()
        tg_send(msg, chat_id=chat_id)

    elif cmd == "/all":
        campaigns, _ = fetch_campaigns()
        if not campaigns: tg_send("API unavailable.", chat_id=chat_id); return
        msg = f"<b>All ({len(campaigns)}):</b>\n\n"
        for c in campaigns: msg += fmt_campaign(c) + "\n\n"
        for i in range(0, len(msg), 4000): tg_send(msg[i:i+4000], chat_id=chat_id)

    elif cmd == "/stake":
        campaigns, _ = fetch_campaigns()
        if not campaigns: tg_send("API unavailable.", chat_id=chat_id); return
        stake = [c for c in campaigns if is_stake_drop(c)]
        if not stake: tg_send(f"No Stake drops. Total: {len(campaigns)}", chat_id=chat_id); return
        msg = f"<b>Stake ({len(stake)}):</b>\n\n"
        for c in stake: msg += fmt_campaign(c) + "\n\n"
        tg_send(msg, chat_id=chat_id)

    elif cmd == "/live":
        tg_send("Checking...", chat_id=chat_id)
        try:
            data = kick_request("https://web.kick.com/api/v1/livestreams?limit=100&sort=viewer_count")
            streams = data.get("data", [])
            live_stake = []
            for s in streams:
                user = s.get("broadcaster_user", {})
                cat = s.get("category", {})
                username = user.get("username", "")
                title = s.get("title", "")
                viewers = s.get("viewer_count", 0)
                cat_name = cat.get("name", "") if isinstance(cat, dict) else ""
                combined = (username + title + cat_name).lower()
                if any(k in combined for k in ["stake", "casino", "slots", "gamble"]):
                    live_stake.append({"username": username, "viewers": viewers, "title": title[:60], "category": cat_name})
            if not live_stake:
                tg_send(f"No Stake streams.\nTotal: {len(streams)}", chat_id=chat_id)
            else:
                msg = f"<b>Live Stake ({len(live_stake)}):</b>\n\n"
                for s in sorted(live_stake, key=lambda x: -x["viewers"]):
                    msg += f"@{s['username']} | {s['viewers']}v\n  {s['title']}\n\n"
                for i in range(0, len(msg), 4000): tg_send(msg[i:i+4000], chat_id=chat_id)
        except Exception as e:
            tg_send(f"Error: {e}", chat_id=chat_id)

    elif cmd == "/history":
        history = load_history()
        if not history: tg_send("No history.", chat_id=chat_id); return
        msg = f"<b>History ({len(history)}):</b>\n\n"
        for h in history[-20:]:
            s = {"active": "LIVE", "upcoming": "SOON", "expired": "EXP"}.get(h.get("status", ""), "?")
            msg += f"{s} {h.get('name', '?')}\n  {h.get('event', '?')} | {h.get('time', '?')[:16]}\n\n"
        for i in range(0, len(msg), 4000): tg_send(msg[i:i+4000], chat_id=chat_id)

    elif cmd == "/status":
        tg_send(f"Polls: {load_state().get('polls',0)}\nDrops: {len(load_state().get('known',{}))}\nSubs: {len(get_active_subs())}\nPW: {'ON' if pw.active else 'OFF'}\nSW: {'ON' if single_watcher.active else 'OFF'}", chat_id=chat_id)

    elif cmd == "/setcookie":
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send("Usage: /setcookie &lt;cookie&gt;\n\nKick se cookie paste karo.\nBot validate karega aur tumhara username fetch karega.", chat_id=chat_id)
            return
        
        raw_cookie = parts[1].strip()
        # Always decode cookie (if URL-encoded %7C → |)
        try:
            import urllib.parse
            cookie = urllib.parse.unquote(raw_cookie)
        except:
            cookie = raw_cookie
        tg_send("Validating cookie...", chat_id=chat_id)
        
        # Validate cookie and get user info
        valid, username, user_id = validate_cookie_and_get_user(cookie)
        
        if valid:
            # Save per-user cookie
            if USE_SUPABASE:
                db.save_user_cookie(chat_id, cookie, username or "unknown", user_id or 0)
            
            # Also save as global fallback
            save_cookie(cookie)
            
            msg = (f"<b>Cookie Saved!</b>\n\n"
                   f"<b>Status:</b> VALID\n"
                   f"<b>Followed:</b> {username}\n"
                   f"<b>Your TG ID:</b> {chat_id}\n\n"
                   f"Ab tumhare liye yeh cookie use hogi.\n"
                   f"Bot automatically drops claim karega.")
            tg_send(msg, chat_id=chat_id)
            tg_send_admin(f"<b>NEW COOKIE SET!</b>\nFollowed: {username}\nTG: {chat_id}")
        else:
            # Invalid cookie - save as global fallback
            save_cookie(cookie)
            tg_send("Cookie saved (validation failed - using as global fallback).\nAgar yeh cookie valid hai toh baad mein kaam karegi.", chat_id=chat_id)

    elif cmd == "/checkcookie":
        global COOKIE_VALIDATED
        cookie = get_cookie()
        if not cookie:
            tg_send("<b>🔴 No cookie found!</b>\nUse /setcookie to add one.", chat_id=chat_id)
            return
        valid, username, user_id = validate_cookie_and_get_user(cookie)
        if valid:
            COOKIE_VALIDATED = True
            tg_send(f"<b>✅ Cookie VALID!</b>\n\n<b>Kick User:</b> @{username}\n<b>User ID:</b> {user_id}\n<b>Cookie length:</b> {len(cookie)} chars", chat_id=chat_id)
        else:
            COOKIE_VALIDATED = False
            tg_send(f"<b>🔴 Cookie INVALID/EXPIRED!</b>\n\nUse /setcookie with a fresh cookie.\n\n<i>How to get cookie:</i>\n1. Open kick.com in browser\n2. Login to your account\n3. Press F12 → Application → Cookies\n4. Copy the 'session' cookie value", chat_id=chat_id)

    elif cmd == "/addchannel":
        parts = text.split()
        if len(parts) < 2: tg_send("Usage: /addchannel &lt;username&gt;\nExample: /addchannel stake", chat_id=chat_id); return
        username = parts[1].strip("@").strip()
        success, msg = add_to_watchlist(username, added_by=str(chat_id))
        tg_send(msg, chat_id=chat_id)
        if success:
            tg_send_admin(f"<b>WATCHLIST:</b> @{username} added by {chat_id}")

    elif cmd == "/removechannel":
        parts = text.split()
        if len(parts) < 2: tg_send("Usage: /removechannel &lt;username&gt;", chat_id=chat_id); return
        username = parts[1].strip("@").strip()
        success, msg = remove_from_watchlist(username)
        tg_send(msg, chat_id=chat_id)

    elif cmd == "/channels":
        watchlist = get_watchlist()
        if not watchlist:
            tg_send("Watchlist is empty.\nUse /addchannel to add channels.", chat_id=chat_id)
        else:
            msg = f"<b>WATCHLIST ({len(watchlist)} channels):</b>\n\n"
            for ch in watchlist:
                info = get_channel_info(ch)
                status = "🟢 LIVE" if info and info.get("is_live") else "⚫ offline"
                msg += f"  @{ch} - {status}\n"
            msg += "\nUse /addchannel or /removechannel"
            tg_send(msg, chat_id=chat_id)

    elif cmd == "/testchat":
        parts = text.split()
        if len(parts) < 2: tg_send("Usage: /testchat &lt;user&gt;", chat_id=chat_id); return
        username = parts[1].strip("@")
        chatroom_id = get_chatroom_id(username)
        if not chatroom_id: tg_send(f"No chatroom for @{username}", chat_id=chat_id); return
        result = send_chat_message(chatroom_id, "!claim")
        if result: tg_send(f"<b>SENT!</b> !claim to @{username}", chat_id=chat_id)
        else: tg_send("Send failed.", chat_id=chat_id)

    elif cmd == "/watchround":
        success, msg = pw.start()
        tg_send(msg, chat_id=chat_id)
        if success: tg_send_admin(f"Parallel watcher started by {chat_id}")

    elif cmd == "/watchroundstop":
        success, msg = pw.stop()
        tg_send(msg, chat_id=chat_id)

    elif cmd == "/watchroundstatus":
        tg_send(pw.get_status(), chat_id=chat_id)

    elif cmd == "/watchtest":
        parts = text.split()
        if len(parts) < 2:
            tg_send("Usage: /watchtest &lt;user1&gt; [user2] [user3]...\nExample: /watchtest stake casinoen", chat_id=chat_id)
            return
        usernames = [p.strip("@") for p in parts[1:] if p.strip("@")]
        success, msg = single_watcher.start(chat_id, usernames)
        tg_send(msg, chat_id=chat_id)

    elif cmd == "/watchstop":
        success, msg = single_watcher.stop(reason="user stopped")
        tg_send(msg, chat_id=chat_id)

    elif cmd == "/watchstatus":
        tg_send(single_watcher.get_status(), chat_id=chat_id)

    elif cmd == "/startwatching":
        # Test command: watch all known live streamers for 1 hour
        tg_send("<b>Starting 1-hour watch session...</b>\nChecking known streamers...", chat_id=chat_id)
        watched = 0
        not_live = []
        already = []
        for slug in list(drop_hunter.known_all_channels):
            if slug in drop_hunter.watching_channels:
                already.append(slug)
                continue
            info = get_channel_info(slug)
            if info and info.get("is_live"):
                cid = info.get("channel_id")
                lsid = info.get("livestream_id")
                stop_event = threading.Event()
                # 1 hour limit for test
                t = threading.Thread(
                    target=_test_watch_loop,
                    args=(slug, cid, lsid, stop_event, 3600),
                    daemon=True
                )
                with drop_hunter._lock:
                    drop_hunter.watching_channels[slug] = {
                        "channel_id": cid,
                        "livestream_id": lsid,
                        "stop_event": stop_event,
                        "started_at": time.time(),
                        "events_sent": 0,
                        "is_manual": True,
                        "is_user_pref": False,
                    }
                t.start()
                watched += 1
                time.sleep(0.3)
            else:
                not_live.append(slug)
        msg = f"<b>WATCH SESSION STARTED!</b>\n\n"
        msg += f"Watching: {watched} live streamers\n"
        msg += f"Duration: 1 hour\n"
        if already:
            msg += f"Already watching: {len(already)}\n"
        if not_live:
            msg += f"Offline (skipped): {len(not_live)}\n"
        msg += f"\nWill auto-stop after 1 hour.\nCheck /dh for status."
        tg_send(msg, chat_id=chat_id)
        log(f"[TEST] /startwatching: {watched} streamers, 1 hour limit")

    # ---- User Preferences (Custom Streamers) ----
    elif cmd == "/addstreamer":
        parts = text.split()
        if len(parts) < 2:
            tg_send("Usage: /addstreamer &lt;username&gt;\n\nApne manpasand streamer add karo.\nJab tak woh live hai aur tum band na karo, bot watch karega.", chat_id=chat_id)
            return
        username = parts[1].strip("@").strip()
        if USE_SUPABASE:
            success, msg = db.add_user_preference(chat_id, username)
            tg_send(msg, chat_id=chat_id)
            if success:
                tg_send_admin(f"<b>USER PREFERENCE:</b> @{username} added by {chat_id}")
        else:
            tg_send("User preferences require Supabase database.", chat_id=chat_id)

    elif cmd == "/removestreamer":
        parts = text.split()
        if len(parts) < 2:
            tg_send("Usage: /removestreamer &lt;username&gt;", chat_id=chat_id)
            return
        username = parts[1].strip("@").strip()
        if USE_SUPABASE:
            success, msg = db.remove_user_preference(chat_id, username)
            tg_send(msg, chat_id=chat_id)
        else:
            tg_send("User preferences require Supabase database.", chat_id=chat_id)

    elif cmd == "/mystreamers":
        if not USE_SUPABASE:
            tg_send("User preferences require Supabase database.", chat_id=chat_id)
            return
        prefs = db.get_user_preferences(chat_id)
        if not prefs:
            tg_send("Your watch list is empty.\n\nUse /addstreamer &lt;username&gt; to add streamers.", chat_id=chat_id)
            return
        msg = f"<b>Your Watch List ({len(prefs)}):</b>\n\n"
        for ch in prefs:
            info = get_channel_info(ch)
            status = "LIVE" if info and info.get("is_live") else "offline"
            color = "#4CAF50" if status == "LIVE" else "#999"
            msg += f"  @{ch} - <span style='color:{color}'>{status}</span>\n"
        msg += "\n/removestreamer &lt;username&gt; to remove"
        tg_send(msg, chat_id=chat_id)

    elif cmd == "/mystreamersclear":
        if not USE_SUPABASE:
            tg_send("User preferences require Supabase database.", chat_id=chat_id)
            return
        prefs = db.get_user_preferences(chat_id)
        for ch in prefs:
            db.remove_user_preference(chat_id, ch)
        tg_send(f"Cleared {len(prefs)} streamers from your list.", chat_id=chat_id)

def _test_watch_loop(slug, channel_id, livestream_id, stop_event, duration_secs):
    """Test watch loop - watches for specified duration then stops."""
    start_time = time.time()
    ls_id = livestream_id or channel_id
    events_sent = 0
    
    log(f"[TEST] Started watching @{slug} for {duration_secs//60} min")
    
    while not stop_event.is_set():
        elapsed = time.time() - start_time
        if elapsed >= duration_secs:
            log(f"[TEST] @{slug} - 1 hour limit reached, stopping")
            break
        
        try:
            # Check if still live
            info = get_channel_info(slug)
            if not info or not info.get("is_live"):
                log(f"[TEST] @{slug} offline, stopping")
                break
            
            # Get WS token and connect
            ws_token = get_ws_token(get_cookie())
            if not ws_token:
                time.sleep(10)
                continue
            
            ws_url = WS_URL_TEMPLATE.format(token=ws_token)
            headers = dict(BASE_HEADERS)
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_test_ws_loop(ws_url, headers, slug, channel_id, ls_id, stop_event, duration_secs, start_time))
            finally:
                loop.close()
        except Exception as e:
            log(f"[TEST] WS error @{slug}: {e}")
        
        if not stop_event.is_set():
            time.sleep(5)
    
    # Remove from watching
    with drop_hunter._lock:
        drop_hunter.watching_channels.pop(slug, None)
    log(f"[TEST] Stopped watching @{slug} ({events_sent} events sent)")

async def _test_ws_loop(ws_url, headers, slug, channel_id, livestream_id, stop_event, duration_secs, start_time):
    """WS loop for test watching."""
    ls_id = livestream_id or channel_id
    
    async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10) as ws:
        await ws.send(json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}}))
        try: await asyncio.wait_for(ws.recv(), timeout=5)
        except: pass
        await send_user_event(ws, channel_id, ls_id)
        log(f"[TEST] Connected @{slug} channel_id={channel_id}")
        
        last_ue = time.time()
        last_ping = time.time()
        events_sent = 1
        
        while not stop_event.is_set():
            now = time.time()
            elapsed = now - start_time
            
            if elapsed >= duration_secs:
                log(f"[TEST] @{slug} - time up")
                return
            
            if now - last_ping >= 20:
                try:
                    await ws.send(json.dumps({"type": "ping"}))
                    last_ping = now
                    try: await asyncio.wait_for(ws.recv(), timeout=3)
                    except: pass
                except: pass
            
            if now - last_ue >= 60:
                await send_user_event(ws, channel_id, ls_id)
                events_sent += 1
                last_ue = now
                remaining = int(duration_secs - elapsed)
                log(f"[TEST] @{slug} event #{events_sent} ({remaining//60}m left)")
            
            await asyncio.sleep(1)

# ---- Poller (5s) ----
# ============================================================
#  DROP HUNTER v3 - Always-On Pre-Watch + Aggressive Claim Retry
# ============================================================
class DropHunter:
    """ALWAYS-ON PRE-WATCH + AGGRESSIVE CLAIM approach:
    1. On startup: watch ALL channels from ALL campaigns 24/7
    2. For upcoming drops: watch channels IMMEDIATELY (not 15 min before)
    3. When drop activates: RETRY claim every 3s for 5 minutes
    4. Track ALL known channels and watch them continuously
    5. Poll every 2s for faster detection"""
    
    def __init__(self):
        self.active = False
        self.watching_channels = {}  # slug -> watcher thread info
        self.claimed_rewards = set()  # campaign_id_reward_id
        self.known_campaigns = {}  # cid -> status
        self.known_all_channels = set()  # ALL channels ever seen in campaigns
        self.user_pref_channels = set()  # User preference channels (no time limit)
        self.claim_retry_queue = {}  # claim_key -> {campaign_id, reward_id, slug, retries, next_retry, last_progress}
        self.failed_rewards = set()  # permanently failed - don't re-add to retry queue
        self.known_stake_campaigns = set()  # IDs of confirmed Stake drops only
        self._lock = threading.Lock()
    
    def start(self):
        if self.active: return
        self.active = True
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._claim_retry_loop, daemon=True).start()
        log("[DH] Drop Hunter v3 started - always-on pre-watch + aggressive claim retry")
    
    def stop(self):
        self.active = False
        with self._lock:
            for slug, info in self.watching_channels.items():
                info.get("stop_event", threading.Event()).set()
            self.watching_channels.clear()
        log("[DH] Drop Hunter stopped")
    
    def _claim_retry_loop(self):
        """Smart claim retry: wait 90s, then try every 5s for 2 min total.
        Handles Kasada 403 on progress API gracefully."""
        while self.active:
            try:
                now = time.time()
                with self._lock:
                    to_retry = []
                    to_remove = []
                    for key, info in self.claim_retry_queue.items():
                        if now >= info.get("next_retry", 0):
                            to_retry.append((key, info))
                        # Give up after 2.5 min (150s) total or 20 retries
                        if info.get("retries", 0) >= 20 or now - info.get("started_at", 0) > 150:
                            to_remove.append(key)
                    for key in to_remove:
                        info = self.claim_retry_queue.pop(key, None)
                        self.failed_rewards.add(key)
                        name = info.get("name", "?") if info else "?"
                        log(f"[DH] Claim gave up after retries: {name}")
                
                for key, info in to_retry:
                    campaign_id = info.get("campaign_id")
                    reward_id = info.get("reward_id")
                    slug = info.get("slug", "?")
                    name = info.get("name", "?")
                    reward_name = info.get("reward_name", "?")
                    elapsed = int(now - info.get("started_at", now))
                    retries = info.get("retries", 0)
                    
                    # STEP 1: Try progress check (may fail with Kasada 403)
                    progress_ok = False
                    try:
                        progress = fetch_progress()
                        if progress is not None:  # Not None means we got data
                            for p in progress:
                                pid = str(p.get("id", "")) or str(p.get("campaign_id", ""))
                                if pid == str(campaign_id):
                                    for r in p.get("rewards", []):
                                        rid = str(r.get("id", "")) or str(r.get("reward_id", ""))
                                        if rid == str(reward_id):
                                            progress_ok = True
                                            if r.get("claimed"):
                                                with self._lock: self.claim_retry_queue.pop(key, None)
                                                log(f"[DH] Already claimed: {name}")
                                                tg_send(f"<b>✅ Already claimed!</b>\n{name} - {reward_name}", chat_id=ADMIN_ID)
                                                break
                                            ratio = r.get("progress", 0)
                                            log(f"[DH] Progress check: {name} ratio={ratio:.2f} ({elapsed}s elapsed, {retries} retries)")
                                            if ratio >= 1.0:
                                                # Ready to claim!
                                                result = claim_reward(campaign_id, reward_id)
                                                if result:
                                                    with self._lock:
                                                        self.claimed_rewards.add(key)
                                                        self.claim_retry_queue.pop(key, None)
                                                    log(f"[DH] ✅ CLAIMED! {name} - {reward_name}")
                                                    tg_send(f"<b>🎉 CLAIMED!</b>\n@{slug}\n{name}\n{reward_name}")
                                                    break
                                                else:
                                                    # Claim API returned None (might be Kasada)
                                                    log(f"[DH] Claim API failed for {name} - will retry")
                                            else:
                                                last_progress = info.get("last_progress", 0)
                                                if ratio > last_progress:
                                                    info["last_progress"] = ratio
                                                    log(f"[DH] Progress increasing: {ratio:.2f} - keep watching")
                                            break
                    except Exception as e:
                        log(f"[DH] Progress check error: {str(e)[:50]}")
                    
                    # STEP 2: If progress API blocked (Kasada 403), try claiming directly
                    # after enough watch time (90s+). Stake drops need ~2 min watch.
                    if not progress_ok and elapsed >= 90:
                        log(f"[DH] Attempting claim (no progress data, {elapsed}s elapsed): {name}")
                        result = claim_reward(campaign_id, reward_id)
                        if result:
                            with self._lock:
                                self.claimed_rewards.add(key)
                                self.claim_retry_queue.pop(key, None)
                            log(f"[DH] ✅ CLAIMED! {name} - {reward_name}")
                            tg_send(f"<b>🎉 CLAIMED!</b>\n@{slug}\n{name}\n{reward_name}")
                            continue
                    
                    # Update retry counters
                    if key in self.claim_retry_queue and key not in self.claimed_rewards:
                        with self._lock:
                            if key in self.claim_retry_queue:
                                self.claim_retry_queue[key]["retries"] = retries + 1
                                # First 90s: wait. After: retry every 5s
                                wait = 5 if elapsed >= 90 else max(5, 90 - elapsed)
                                self.claim_retry_queue[key]["next_retry"] = now + wait
                                if retries > 0 and retries % 5 == 0:
                                    log(f"[DH] Still retrying {name}: {retries} attempts, {elapsed}s elapsed")
            except Exception as e:
                log(f"[DH] Retry loop error: {e}")
            time.sleep(2)
    
    def _run(self):
        # Step 1: On startup, discover ALL channels
        self._discover_all_channels()
        
        # Log time window status
        if self._is_active_hours():
            log(f"[DH] Starting in ACTIVE HOURS (4AM-10AM IST) - watching channels!")
            self._start_watching_all_known()
        else:
            from datetime import timezone
            utc_now = datetime.now(timezone.utc)
            ist_now = utc_now + IST_OFFSET
            log(f"[DH] Outside active hours (IST: {ist_now.strftime('%H:%M')}) - will start watching at 4 AM IST")
        
        # Step 3: Continuous polling loop
        was_active = self._is_active_hours()
        while self.active:
            try:
                is_now_active = self._is_active_hours()
                
                if not is_now_active:
                    # OUTSIDE ACTIVE HOURS: Sleep 5 min, no polling
                    if was_active:
                        log(f"[DH] leaving active hours - stopping watches")
                        with self._lock:
                            for slug in list(self.watching_channels.keys()):
                                self.watching_channels[slug]["stop_event"].set()
                                del self.watching_channels[slug]
                        log(f"[DH] All watches stopped for the night")
                        was_active = False
                    time.sleep(300)  # Sleep 5 min outside hours
                    continue
                
                # INSIDE ACTIVE HOURS: Fast polling (2s)
                if not was_active:
                    log(f"[DH] entering ACTIVE HOURS (4AM-10AM IST) - starting watches!")
                    self._start_watching_all_known()
                    was_active = True
                
                self._poll_and_watch()
            except Exception as e:
                log(f"[DH] Error: {e}")
            time.sleep(POLL_INTERVAL)
    
    def _discover_all_channels(self):
        """Discover ALL channels from campaigns + watchlist + user preferences."""
        try:
            # Source 1: From API (current campaigns)
            campaigns, _ = fetch_campaigns()
            if campaigns:
                for c in campaigns:
                    for ch in c.get("channels", []):
                        slug = ch.get("slug") or (ch.get("user") or {}).get("username", "")
                        if slug and slug not in self.known_all_channels:
                            self.known_all_channels.add(slug)
            
            # Source 2: From persistent watchlist (manual)
            watchlist = get_watchlist()
            for slug in watchlist:
                if slug not in self.known_all_channels:
                    self.known_all_channels.add(slug)
            
            # Source 3: From followed channels
            try:
                followed = get_followed_streamers()
                for slug in followed:
                    if slug not in self.known_all_channels:
                        self.known_all_channels.add(slug)
            except: pass
            
            # Source 4: From user preferences (custom streamers)
            self.user_pref_channels = set()
            if USE_SUPABASE:
                try:
                    all_prefs = db.get_all_user_preferences()
                    for pref in all_prefs:
                        ch = pref.get("channel_name", "")
                        if ch:
                            self.user_pref_channels.add(ch)
                            if ch not in self.known_all_channels:
                                self.known_all_channels.add(ch)
                except Exception as e:
                    log(f"[DH] User prefs load error: {e}")
            
            log(f"[DH] Total known: {len(self.known_all_channels)} (watchlist: {len(watchlist)}, user_prefs: {len(self.user_pref_channels)})")
            
        except Exception as e:
            log(f"[DH] Discover error: {e}")
    
    def _start_watching_all_known(self):
        """Start watching ALL known channels that are currently live.
        - Stake list channels: 30 min limit
        - User preference channels: No limit (until stopped/offline)"""
        try:
            watched = 0
            for slug in list(self.known_all_channels):
                if not self.active: break
                if slug in self.watching_channels: continue
                info = get_channel_info(slug)
                if info and info.get("is_live"):
                    cid = info.get("channel_id")
                    is_user_pref = slug in self.user_pref_channels
                    self._start_watching_channel(slug, cid, is_user_pref=is_user_pref)
                    watched += 1
                    time.sleep(0.3)
            log(f"[DH] Started watching {watched} live from {len(self.known_all_channels)} known ({len(self.user_pref_channels)} user prefs)")
        except Exception as e:
            log(f"[DH] Start watching error: {e}")
    
    def _start_watching_channel(self, slug, channel_id=None, is_manual=False, is_user_pref=False):
        """Start watching a channel.
        - is_manual: No time limit
        - is_user_pref: No limit (until stopped/offline)
        - Default: 30 min limit, only during active hours (4AM-10AM IST)"""
        if slug in self.watching_channels: return
        
        # Get channel info
        info = get_channel_info(slug)
        if not info or not info.get("is_live"):
            log(f"[DH] @{slug} offline, will retry when live")
            return
        
        cid = channel_id or info.get("channel_id")
        lsid = info.get("livestream_id")
        
        stop_event = threading.Event()
        
        t = threading.Thread(
            target=self._watch_loop,
            args=(slug, cid, lsid, stop_event, is_manual, is_user_pref),
            daemon=True
        )
        
        with self._lock:
            self.watching_channels[slug] = {
                "channel_id": cid,
                "livestream_id": lsid,
                "stop_event": stop_event,
                "started_at": time.time(),
                "events_sent": 0,
                "is_manual": is_manual,
                "is_user_pref": is_user_pref,
            }
        
        t.start()
        mode = "USER_PREF" if is_user_pref else ("MANUAL" if is_manual else "STAKE_LIST")
        log(f"[DH] Watching @{slug} ({mode})")
    
    def _watch_loop(self, slug, channel_id, livestream_id, stop_event, is_manual=False, is_user_pref=False):
        """Watch a channel.
        - Stake list: 30 min limit, only during active hours (4AM-10AM IST)
        - User preferences: No limit (until stopped/offline)
        - Manual: No limit"""
        start_time = time.time()
        
        while not stop_event.is_set():
            # Check time limit (only for Stake list)
            elapsed = time.time() - start_time
            if not is_manual and not is_user_pref and elapsed >= AUTO_WATCH_LIMIT:
                log(f"[DH] @{slug} 30min limit reached, stopping")
                break
            
            # Check if user preference streamer went offline
            if is_user_pref and not is_manual:
                try:
                    info = get_channel_info(slug)
                    if not info or not info.get("is_live"):
                        log(f"[DH] @{slug} offline - stopping user preference watch")
                        break
                except: pass
            
            try:
                ws_token = get_ws_token(get_cookie())
                if not ws_token:
                    time.sleep(10)
                    continue
                
                ws_url = WS_URL_TEMPLATE.format(token=ws_token)
                headers = dict(BASE_HEADERS)
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._ws_loop(ws_url, headers, slug, channel_id, livestream_id, stop_event))
                finally:
                    loop.close()
            except Exception as e:
                log(f"[DH] WS error @{slug}: {e}")
            
            if not stop_event.is_set():
                time.sleep(5)
    
    async def _ws_loop(self, ws_url, headers, slug, channel_id, livestream_id, stop_event):
        """WS connection: handshake + user_events every 60s"""
        import websockets
        
        ls_id = livestream_id or channel_id
        
        async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10) as ws:
            # Handshake
            await ws.send(json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}}))
            try: await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError: pass
            except Exception as e: log(f"[DH] Handshake recv error @{slug}: {e}")
            
            # Initial user_event
            await send_user_event(ws, channel_id, ls_id)
            
            last_ue = time.time()
            last_ping = time.time()
            last_refresh = time.time()
            
            while not stop_event.is_set():
                now = time.time()
                
                if now - last_ping >= 20:
                    try:
                        await ws.send(json.dumps({"type": "ping"}))
                        last_ping = now
                        try: await asyncio.wait_for(ws.recv(), timeout=3)
                        except asyncio.TimeoutError: pass
                        except Exception as e: log(f"[DH] Ping recv error @{slug}: {e}")
                    except Exception as e: log(f"[DH] Ping send error @{slug}: {e}")
                
                if now - last_ue >= 60:
                    # Refresh livestream_id
                    if now - last_refresh >= 300:
                        last_refresh = now
                        try:
                            fresh = get_channel_info(slug)
                            if fresh and fresh.get("livestream_id"):
                                ls_id = fresh["livestream_id"]
                        except Exception as e: log(f"[DH] Refresh error @{slug}: {e}")
                    
                    await send_user_event(ws, channel_id, ls_id)
                    last_ue = now
                    
                    with self._lock:
                        if slug in self.watching_channels:
                            self.watching_channels[slug]["events_sent"] = self.watching_channels[slug].get("events_sent", 0) + 1
                
                await asyncio.sleep(1)
    
    def _is_active_hours(self):
        """Check if current time is within active hours (4 AM - 10 AM IST)."""
        try:
            from datetime import timezone
            utc_now = datetime.now(timezone.utc)
            ist_now = utc_now + IST_OFFSET
            hour = ist_now.hour
            return WATCH_START_HOUR <= hour < WATCH_END_HOUR
        except:
            return True  # Default to active if time check fails
    
    def _log_watching_status(self):
        """Log which streamers are being watched with their IDs."""
        with self._lock:
            watching = dict(self.watching_channels)
        if not watching:
            log(f"[DH] WATCH STATUS: Not watching any streamers")
            return
        lines = []
        for slug, info in watching.items():
            elapsed = int(time.time() - info.get("started_at", time.time()))
            events = info.get("events_sent", 0)
            cid = info.get("channel_id", "?")
            lines.append(f"@{slug} (ID:{cid}) {elapsed//60}m {events}ev")
        log(f"[DH] WATCHING {len(watching)} streamers: {', '.join(lines)}")
    
    def _poll_and_watch(self):
        """Check campaigns, start watching ALL channels, claim instantly.
        Called only during active hours (4AM-10AM IST)."""
        # Log watching status every minute
        self._log_watching_status()
        
        campaigns, cookie_ok = fetch_campaigns()
        if not campaigns: return
        
        # Safety: ensure campaigns is a list of dicts
        if not isinstance(campaigns, list):
            log(f"[DH] Unexpected campaigns type: {type(campaigns)}")
            return
        
        for c in campaigns:
            if not self.active: break
            if not isinstance(c, dict): continue  # Skip non-dict items
            cid = c.get("id", "")
            status = c.get("status", "")
            channels = c.get("channels", [])
            rewards = c.get("rewards", [])
            name = c.get("name", "?")
            connect = c.get("connect_url", "")
            start_at = c.get("start_at", "")
            end_at = c.get("end_at", "")
            
            if not channels or not rewards: continue
            
            # Track ALL channels from ALL campaigns + AUTO-ADD to watchlist
            for ch in channels:
                if not isinstance(ch, dict): continue  # Safety check
                slug = ch.get("slug") or (ch.get("user") or {}).get("username", "")
                if slug:
                    # Add to known channels
                    self.known_all_channels.add(slug)
                    # AUTO-ADD new drop channels to persistent watchlist
                    if slug not in get_watchlist():
                        add_to_watchlist(slug, added_by="auto-drop")
                        log(f"[DH] Auto-added @{slug} to watchlist (new drop channel)")
            
            # Track campaign status changes
            old_status = self.known_campaigns.get(cid)
            self.known_campaigns[cid] = status
            
            # Handle active drops - SMART CLAIM (instant if pre-watched, else wait)
            if status == "active" and old_status != "active":
                if not is_stake_drop(c):
                    log(f"[DH] Non-Stake active: {name} - watching only")
                    continue
                
                self.known_stake_campaigns.add(cid)
                
                # Check if ANY channel was already pre-watched
                pre_watched = False
                for ch in channels:
                    slug = ch.get("slug") or (ch.get("user") or {}).get("username", "")
                    if slug and slug in self.watching_channels:
                        pre_watched = True
                        break
                
                if pre_watched:
                    log(f"[DH] PRE-WATCHED ACTIVE STAKE DROP: {name} - claiming INSTANTLY!")
                    tg_send(f"<b>🎯 STAKE DROP ACTIVE!</b>\n<b>{name}</b>\nClaim window: {fmt_countdown(end_at)}\nAlready pre-watched - claiming NOW!")
                else:
                    log(f"[DH] NEW ACTIVE STAKE DROP (not pre-watched): {name} - will claim after 30s")
                    tg_send(f"<b>🎯 STAKE DROP ACTIVE!</b>\n<b>{name}</b>\nClaim window: {fmt_countdown(end_at)}\nBot watching + claiming!")
                
                # Start watching ALL channels for this drop
                for ch in channels:
                    slug = ch.get("slug") or (ch.get("user") or {}).get("username", "")
                    if not slug: continue
                    
                    if slug not in self.watching_channels:
                        cid_val = ch.get("id") or (ch.get("user") or {}).get("id")
                        self._start_watching_channel(slug, cid_val)
                
                # Schedule claims - INSTANT if pre-watched, else 30s wait
                claim_delay = 0 if pre_watched else 30
                for r in rewards:
                    reward_id = r.get("id", "")
                    claim_key = f"{cid}_{reward_id}"
                    if claim_key in self.claimed_rewards: continue
                    if claim_key in self.failed_rewards: continue
                    
                    with self._lock:
                        self.claim_retry_queue[claim_key] = {
                            "campaign_id": cid,
                            "reward_id": reward_id,
                            "slug": channels[0].get("slug") if channels else "?",
                            "retries": 0,
                            "next_retry": time.time() + claim_delay,
                            "started_at": time.time(),
                            "last_progress": 0,
                            "name": name,
                            "reward_name": r.get("name", "?"),
                        }
                    if pre_watched:
                        log(f"[DH] Queued INSTANT claim for {r.get('name', '?')} (pre-watched!)")
                    else:
                        log(f"[DH] Queued claim for {r.get('name', '?')} - will try in 30s")
            
            # Handle upcoming Stake drops - notify + PRE-WATCH immediately
            if start_at and status == "upcoming":
                if is_stake_drop(c):
                    self.known_stake_campaigns.add(cid)
                    # Notify ONCE about upcoming Stake drops
                    notify_key = f"upcoming_{cid}"
                    if notify_key not in self.claimed_rewards:
                        self.claimed_rewards.add(notify_key)
                        tg_send(f"<b>🎯 STAKE DROP SOON!</b>\n<b>{name}</b>\nStarts: {fmt_countdown(start_at)}\nBot pre-watching channels now!")
                try:
                    start_time = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
                    now = datetime.now(start_time.tzinfo) if start_time.tzinfo else datetime.now()
                    seconds_until = (start_time - now).total_seconds()
                except: continue
                
                # Watch channels IMMEDIATELY for upcoming drops (start watching NOW)
                if seconds_until > 0:
                    for ch in channels:
                        slug = ch.get("slug") or (ch.get("user") or {}).get("username", "")
                        if slug and slug not in self.watching_channels:
                            cid_val = ch.get("id") or (ch.get("user") or {}).get("id")
                            log(f"[DH] PRE-WATCH: @{slug} for {name} (starts in {fmt_countdown(start_at)})")
                            self._start_watching_channel(slug, cid_val)
        
        # Also check followed streamers + slots category for potential drop channels
        try:
            followed = get_followed_streamers()
            for slug in followed:
                if slug not in self.known_all_channels:
                    self.known_all_channels.add(slug)
        except: pass
        
        try:
            slots = get_slots_streamers()
            for s in slots:
                if s.get("username") and s["username"] not in self.known_all_channels:
                    self.known_all_channels.add(s["username"])
        except: pass
        
        # Ensure ALL known live channels are being watched (round-robin check)
        known_list = list(self.known_all_channels)
        if known_list:
            # Check 3 channels per cycle (round-robin)
            check_start = getattr(self, '_check_idx', 0) % len(known_list)
            for i in range(min(3, len(known_list))):
                idx = (check_start + i) % len(known_list)
                slug = known_list[idx]
                if not self.active: break
                if slug in self.watching_channels: continue
                info = get_channel_info(slug)
                if info and info.get("is_live"):
                    self._start_watching_channel(slug, info.get("channel_id"))
                    time.sleep(0.3)
            self._check_idx = (check_start + 3) % len(known_list)
        
        # Re-check ALL progress for unclaimed rewards (ONLY Stake campaigns)
        # Note: This may fail with Kasada 403 - that's OK, retry loop handles it
        try:
            progress = fetch_progress()
            if progress is None or progress == []:
                # Kasada blocked progress check - this is expected
                pass
            else:
                for p in progress:
                    cid = p.get("id") or p.get("campaign_id", "")
                    if cid not in self.known_stake_campaigns: continue
                    total = p.get("progress_units", 0)
                    for r in p.get("rewards", []):
                        if r.get("claimed"): continue
                        required = r.get("required_units", 0)
                        ratio = r.get("progress", 0)
                        reward_id = r.get("id") or r.get("reward_id", "")
                        claim_key = f"{cid}_{reward_id}"
                        
                        if claim_key in self.claimed_rewards: continue
                        if claim_key in self.failed_rewards: continue
                        
                        if ratio >= 1.0 or total >= required:
                            result = claim_reward(cid, reward_id)
                            if result:
                                self.claimed_rewards.add(claim_key)
                                log(f"[DH] ✅ CLAIMED via progress: {r.get('name', '?')}")
                                tg_send(f"<b>🎉 CLAIMED!</b>\n{p.get('name', '?')}\n{r.get('name', '?')}")
                        elif ratio > 0.3 and claim_key not in self.claim_retry_queue:
                            # Close to done - add to retry queue with 90s initial delay
                            with self._lock:
                                self.claim_retry_queue[claim_key] = {
                                    "campaign_id": cid,
                                    "reward_id": reward_id,
                                    "slug": "?",
                                    "retries": 0,
                                    "next_retry": time.time() + 90,
                                    "started_at": time.time(),
                                    "last_progress": ratio,
                                    "name": p.get("name", "?"),
                                    "reward_name": r.get("name", "?"),
                                }
                            log(f"[DH] Queued ({ratio:.0%}): {r.get('name', '?')}")
        except Exception as e:
            log(f"[DH] Progress check skipped: {str(e)[:50]}")
        
        # Cleanup finished channels, re-add live ones
        self._refresh_channels()
    
    def _refresh_channels(self):
        """Remove offline channels, re-check periodically."""
        with self._lock:
            to_remove = []
            for slug, info in list(self.watching_channels.items()):
                elapsed = time.time() - info.get("started_at", 0)
                # If watching for more than 2 hours without events, might be stuck
                if elapsed > 7200 and info.get("events_sent", 0) == 0:
                    to_remove.append(slug)
            
            for slug in to_remove:
                self.watching_channels[slug]["stop_event"].set()
                del self.watching_channels[slug]
                log(f"[DH] Removed stale @{slug}")
    
    def get_retry_status(self):
        """Get claim retry queue status."""
        with self._lock:
            retries = dict(self.claim_retry_queue)
        if not retries:
            return "No pending retries."
        lines = [f"<b>RETRY QUEUE ({len(retries)}):</b>\n"]
        for key, info in retries.items():
            elapsed = int(time.time() - info.get('started_at', 0))
            retries_count = info.get('retries', 0)
            lines.append(f"  {key[:30]}: {retries_count} retries, {elapsed}s elapsed")
        return "\n".join(lines)

drop_hunter = DropHunter()

def poller():
    """Legacy poller - now just starts DropHunter"""
    drop_hunter.start()
    while True:
        time.sleep(60)

# ---- Main ----
def main():
    global COOKIE_VALIDATED
    log("=" * 50)
    log("KICK STAKE DROPS BOT v25 - TIME WINDOW + TEST MODE")
    log("=" * 50)
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashboardHandler).serve_forever(), daemon=True).start()
    log(f"Dashboard: port {DASHBOARD_PORT} (user: {DASH_USER})")
    
    # Validate cookie on startup
    cookie = get_cookie()
    if cookie:
        try:
            headers = dict(BASE_HEADERS)
            headers["Cookie"] = "session=" + cookie
            headers["Authorization"] = f"Bearer {cookie}"
            headers["X-Client-Token"] = KICK_CLIENT_TOKEN
            req = urllib.request.Request("https://kick.com/api/v2/users/me", headers=headers)
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode())
            if data.get("username"):
                COOKIE_VALIDATED = True
                log(f"[STARTUP] Cookie VALID - @{data['username']}")
            else:
                COOKIE_VALIDATED = True
                log("[STARTUP] Cookie check - no username but no error")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                COOKIE_VALIDATED = False
                log("[STARTUP] Cookie EXPIRED! Use /setcookie to update.")
            else:
                COOKIE_VALIDATED = True
                log(f"[STARTUP] Cookie check: HTTP {e.code} (not cookie issue)")
        except Exception as e:
            COOKIE_VALIDATED = True
            log(f"[STARTUP] Cookie check error: {str(e)[:50]} (assuming valid)")
    else:
        COOKIE_VALIDATED = False
        log("[STARTUP] No cookie found! Use /setcookie to add one.")
    
    threading.Thread(target=poller, daemon=True).start()
    try:
        threading.Thread(target=session_keeper, daemon=True).start()
    except: pass
    # Follow yesterday's drop streamers on startup
    try:
        campaigns, _ = fetch_campaigns()
        if campaigns:
            follow_drop_streamers(campaigns)
    except: pass
    
    status = "✅ Cookie OK" if COOKIE_VALIDATED else "🔴 Cookie INVALID"
    tg_send_admin(f"<b>Bot v24 Started!</b>\n\nCookie: {status}\nDrop Hunter: active\nActive Hours: 4 AM - 10 AM IST\nManual: /watchtest works 24/7")
    log("Listening...")
    offset = 0
    while True:
        for u in tg_get_updates(offset):
            offset = u["update_id"] + 1
            msg = u.get("message", {})
            cid = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            # Extract Telegram username and name
            from_user = msg.get("from", {})
            tg_username = from_user.get("username", "")
            tg_first_name = from_user.get("first_name", "")
            if text.startswith("/"):
                log(f"CMD: {text} from @{tg_username or cid}")
                handle_command(text.split()[0].lower(), cid, text, username=tg_username, first_name=tg_first_name)
        time.sleep(1)

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: log("Stopped")
