"""
Kick Stake Drops Bot v16
- Parallel watcher: all live streams simultaneously
- Smart claim: only check when claim window open (no API spam)
- Category fallback: Slots & Casino if no Stake streamers
- Follow streamers on watch + follow yesterday's drop streamers
- Password-protected dashboard + logs page
- Fixed follow API: POST kick.com/api/v2/channels/{slug}/follow
"""
import urllib.request, json, time, os, threading, random, hashlib
import asyncio
import websockets
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============ CONFIG ============
TG_TOKEN = os.environ.get("TG_TOKEN", "8860462138:AAGkQQF1c-MyTfD3-3WluZNMarcT7HLj4dg")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8182391939"))
INITIAL_COOKIE = os.environ.get("KICK_COOKIE", "365875656%7C3qeqtSAxow2mU2adRmgluNijBSImYcgoLFRIZ2v9")
DASH_USER = os.environ.get("DASH_USER", "admin")
DASH_PASS = os.environ.get("DASH_PASS", "kickbot2026")
POLL_INTERVAL = 5
DROPS_API = "https://web.kick.com/api/v1/drops/campaigns"
PROGRESS_API = "https://web.kick.com/api/v1/drops/progress"
CLAIM_API = "https://web.kick.com/api/v1/drops/claim"
CHANNEL_API = "https://kick.com/api/v2/channels/{username}"
CHATROOM_API = "https://kick.com/api/v2/channels/{username}/chatroom"
CHAT_SEND_API = "https://kick.com/api/v2/messages/send/{chatroom_id}"
FOLLOW_API = "https://kick.com/api/v2/channels/{channel_slug}/follow"
FOLLOWED_API = "https://web.kick.com/api/v1/followed-channels"
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
DASHBOARD_PORT = int(os.environ.get("PORT", "8080"))
KEEPER_INTERVAL = 1800
SLOTS_CATEGORY_ID = None
BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://kick.com",
    "Referer": "https://kick.com/",
}
# ================================

LOG_BUFFER = []
LOG_LOCK = threading.Lock()
MAX_LOG_BUFFER = 500

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
    try:
        with LOG_LOCK:
            logs = list(LOG_BUFFER[-200:])
        with open(LOG_FILE, "w") as f:
            json.dump(logs, f)
    except: pass

def load_logs():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f: return json.load(f)
        except: pass
    return []

# ---- Cookie ----
def get_cookie():
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE) as f:
                c = json.load(f).get("cookie", "")
                if c: return c
        except: pass
    return INITIAL_COOKIE

def save_cookie(cookie):
    with open(COOKIE_FILE, "w") as f:
        json.dump({"cookie": cookie, "time": datetime.now().isoformat()}, f)

# ---- Session Keeper ----
def session_keeper():
    log("Session keeper started")
    while True:
        try:
            from playwright.sync_api import sync_playwright
            cookie = get_cookie()
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(user_agent="Mozilla/5.0")
                ctx.add_cookies([{"name": "session", "value": cookie, "domain": ".kick.com", "path": "/"}])
                page = ctx.new_page()
                for url in ["https://kick.com/", "https://kick.com/drops/all-campaigns"]:
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=20000)
                        time.sleep(2)
                    except: pass
                new_cookie = None
                for c in ctx.cookies():
                    if c["name"] == "session":
                        new_cookie = c["value"]; break
                browser.close()
                if new_cookie:
                    save_cookie(new_cookie)
                    log(f"Cookie refreshed ({len(new_cookie)} chars)")
        except Exception as e:
            log(f"Keeper error: {e}")
        time.sleep(KEEPER_INTERVAL)

# ---- Subscribers ----
def load_subs():
    if os.path.exists(SUBS_FILE):
        try:
            with open(SUBS_FILE) as f: return json.load(f)
        except: pass
    return {}

def save_subs(subs):
    with open(SUBS_FILE, "w") as f: json.dump(subs, f, indent=2)

def add_sub(chat_id):
    subs = load_subs()
    sid = str(chat_id)
    is_new = sid not in subs or not subs[sid].get("active", True)
    subs[sid] = {"added_at": datetime.now().isoformat(), "active": True}
    save_subs(subs)
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
        try: urllib.request.urlopen(req, timeout=15)
        except: pass
        time.sleep(0.05)

def tg_send_admin(text):
    tg_send(text, chat_id=ADMIN_ID)

def tg_get_updates(offset=0):
    try:
        resp = urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={offset}&timeout=30", timeout=35)
        return json.loads(resp.read().decode()).get("result", [])
    except: return []

# ---- Kick API ----
def kick_request(url, extra_headers=None, timeout=15):
    headers = dict(BASE_HEADERS)
    cookie = get_cookie()
    if cookie: headers["Cookie"] = "session=" + cookie
    session_token = get_session_token()
    if session_token: headers["Authorization"] = f"Bearer {session_token}"
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
    """Follow a channel on Kick - POST to correct v2 endpoint with slug"""
    try:
        url = FOLLOW_API.format(channel_slug=username)
        session_token = get_session_token()
        headers = dict(BASE_HEADERS)
        headers["Content-Type"] = "application/json"
        if session_token:
            headers["Authorization"] = f"Bearer {session_token}"
        cookie = get_cookie()
        if cookie: headers["Cookie"] = "session=" + cookie
        headers["X-Client-Token"] = KICK_CLIENT_TOKEN
        req = urllib.request.Request(url, data=b"{}", method="POST")
        for k, v in headers.items(): req.add_header(k, v)
        resp = urllib.request.urlopen(req, timeout=10)
        log(f"[FOLLOW] OK @{username}")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200] if e.fp else ""
        if e.code == 409:
            log(f"[FOLLOW] Already following @{username}")
            return True
        log(f"[FOLLOW] HTTP {e.code} @{username}: {body}")
        return False
    except Exception as e:
        log(f"[FOLLOW] Error @{username}: {e}")
        return False

def get_followed_streamers():
    try:
        data = kick_request(FOLLOWED_API)
        channels = data.get("data", [])
        return [ch.get("slug") or ch.get("username") for ch in channels if ch.get("slug") or ch.get("username")]
    except: return []

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

def get_session_token():
    """Return full raw cookie as Bearer token (NOT decoded, NOT split)"""
    cookie = get_cookie()
    if not cookie: return None
    return cookie

def fetch_progress():
    try:
        session_token = get_session_token()
        if not session_token: return []
        headers = dict(BASE_HEADERS)
        headers["Cookie"] = "session=" + get_cookie()
        headers["Authorization"] = f"Bearer {session_token}"
        headers["X-Client-Token"] = KICK_CLIENT_TOKEN
        req = urllib.request.Request(PROGRESS_API)
        for k, v in headers.items(): req.add_header(k, v)
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode()).get("data", [])
    except: return []

def claim_reward(campaign_id, reward_id):
    try:
        session_token = get_session_token()
        if not session_token: return None
        headers = dict(BASE_HEADERS)
        headers["Cookie"] = "session=" + get_cookie()
        headers["Authorization"] = f"Bearer {session_token}"
        headers["X-Client-Token"] = KICK_CLIENT_TOKEN
        headers["Content-Type"] = "application/json"
        body = json.dumps({"campaign_id": campaign_id, "reward_id": reward_id}).encode()
        req = urllib.request.Request(CLAIM_API, data=body, method="POST")
        for k, v in headers.items(): req.add_header(k, v)
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode())
        log(f"Claim OK: {result}")
        return result
    except: return None

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
    """Smart claim check - only when there's progress to claim. No spam."""
    try:
        progress = fetch_progress()
        if not progress: return
        claimed_any = False
        for item in progress:
            campaign_id = item.get("campaign_id") or item.get("id")
            for r in item.get("rewards", []):
                if r.get("claimed"): continue
                required = r.get("required_units", 0)
                current = r.get("progress", 0)
                reward_id = r.get("reward_id") or r.get("id")
                if required > 0 and current >= required:
                    log(f"[CLAIM] CLAIMABLE! {current}/{required}s")
                    result = claim_reward(campaign_id, reward_id)
                    if result:
                        claimed_any = True
                        tg_send(f"<b>REWARD CLAIMED!</b>\nStreamer: @{username or '?'}\nReward: {reward_id[:16]}...", chat_id=ADMIN_ID)
                    if username:
                        try_claim_in_chat(username)
                elif required > 0 and current > 0:
                    remaining = required - current
                    if remaining <= 300:
                        log(f"[CLAIM] Almost ready: {current}/{required}s ({remaining}s left)")
        return claimed_any
    except: return False

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
    connect = c.get("connect_url", "").lower()
    name = c.get("name", "").lower()
    channels = c.get("channels", [])
    if "stake.com" in connect or "stake" in connect: return True
    if "stake" in name: return True
    for ch in channels:
        username = (ch.get("slug", "") + ch.get("user", {}).get("username", "")).lower()
        if "stake" in username: return True
    cat = c.get("category", {})
    if isinstance(cat, dict) and "stake" in cat.get("name", "").lower(): return True
    return False

def fmt_campaign(c):
    name = c.get("name", "?")
    status = c.get("status", "?")
    connect = c.get("connect_url", "none")
    cat = c.get("category", {}).get("name", "?") if isinstance(c.get("category"), dict) else "?"
    channels = c.get("channels", [])
    ch_str = ", ".join([ch.get("user", {}).get("username", ch.get("slug", "?")) for ch in channels]) if channels else "global"
    rewards = c.get("rewards", [])
    rew_str = ", ".join([r.get("name", "?") for r in rewards[:3]])
    if len(rewards) > 3: rew_str += f" +{len(rewards)-3} more"
    s = {"active": "LIVE", "upcoming": "SOON", "expired": "EXP"}.get(status, "?")
    return f"{s} {name}\n  Cat: {cat}\n  Channels: {ch_str}\n  Rewards: {rew_str}\n  Connect: {connect[:60]}"

# ---- History ----
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f: return json.load(f)
        except: pass
    return []

def save_history(history):
    with open(HISTORY_FILE, "w") as f: json.dump(history, f, indent=2, default=str)

def add_to_history(campaign, event_type="seen"):
    history = load_history()
    entry = {
        "id": campaign.get("id", "?"),
        "name": campaign.get("name", "?"),
        "status": campaign.get("status", "?"),
        "channels": [ch.get("user", {}).get("username", ch.get("slug", "?")) for ch in campaign.get("channels", [])],
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

def follow_drop_streamers(campaigns):
    """Follow all streamers from active Stake drops"""
    cache = load_followed_cache()
    already_followed = set(cache.get("usernames", []))
    followed_count = 0
    for c in campaigns:
        if not is_stake_drop(c): continue
        for ch in c.get("channels", []):
            username = ch.get("slug") or ch.get("user", {}).get("username")
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
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: return json.load(f)
        except: pass
    return {"known": {}, "polls": 0, "last_poll": None}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2, default=str)

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
            log(f"[WATCH] Connected @{username}")
            last_ue = time.time()
            last_ping = time.time()
            last_alive = time.time()
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
        while not self.stop_event.is_set():
            try:
                all_streamers = set()
                campaigns, _ = fetch_campaigns()

                # Follow yesterday's drop streamers
                if campaigns:
                    follow_drop_streamers(campaigns)

                if campaigns:
                    for c in campaigns:
                        if is_stake_drop(c) and c.get("status") == "active":
                            for ch in c.get("channels", []):
                                username = ch.get("slug") or ch.get("user", {}).get("username")
                                if username: all_streamers.add(username)

                followed = get_followed_streamers()
                for u in followed: all_streamers.add(u)

                # Fallback: Slots & Casino category
                if not all_streamers:
                    log("[PW] No Stake streamers, checking Slots & Casino...")
                    slots = get_slots_streamers()
                    for s in slots:
                        if s["username"]: all_streamers.add(s["username"])

                if not all_streamers:
                    log("[PW] No streamers found, waiting 30s...")
                    time.sleep(30)
                    continue

                live_streamers = []
                for username in all_streamers:
                    if self.stop_event.is_set(): break
                    info = get_channel_info(username)
                    if info and info.get("is_live"):
                        live_streamers.append(info)
                    time.sleep(0.2)

                if not live_streamers:
                    log(f"[PW] No live ({len(all_streamers)} checked), waiting 30s...")
                    time.sleep(30)
                    continue

                log(f"[PW] Found {len(live_streamers)} live streamers")

                for streamer in live_streamers:
                    if self.stop_event.is_set(): break
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

                # Smart claim check (not every 30s, only when progress exists)
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
            log(f"[SW] Connected @{username}")
            start = time.time()
            last_ue = time.time()
            last_ping = time.time()
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
            rows += f"<tr><td>{sid}</td><td style='color:{color}'>{st}</td><td>{d.get('added_at','?')[:16]}</td></tr>"
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
        html = f"""<!DOCTYPE html><html><head><title>Kick Drops v15</title>
<meta http-equiv="refresh" content="30">
<style>body{{font-family:Arial;background:#1a1a2e;color:#eee;padding:20px}}h1{{color:#e94560}}.c{{background:#16213e;padding:20px;border-radius:10px;margin:10px 0}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #333}}th{{background:#0f3460}}a{{color:#e94560}}</style></head><body>
<h1>Kick Stake Drops Bot v15</h1>
<p><a href="/logs">View Logs</a></p>
<div class="c"><h2>Status</h2><p>Polls: {state.get('polls',0)}</p><p>Last: {state.get('last_poll','never')}</p></div>
<div class="c"><h2>Parallel Watcher</h2><p style='color:{pw_c};font-size:1.2em'><b>{pw_s}</b></p><p>Watching: {w_list}</p><p>Watched: {pw.state.get('total_watched',0)}</p><p>Claimed: {pw.state.get('rewards_claimed',0)}</p></div>
<div class="c"><h2>Single Watcher</h2><p><b>{sw_s}</b></p><p>{sw_users}</p></div>
<div class="c"><h2>Users ({active}/{len(subs)})</h2><table><tr><th>ID</th><th>Status</th><th>Joined</th></tr>{rows}</table></div>
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
def handle_command(cmd, chat_id, text=""):
    is_new = add_sub(chat_id)

    if cmd in ("/start", "/help"):
        if cmd == "/start" and is_new:
            tg_send_admin(f"NEW USER: {chat_id}\nTotal: {len(get_active_subs())}")
        tg_send(
            "<b>Kick Stake Drops Bot v15</b>\n\n"
            "<b>DROP COMMANDS:</b>\n"
            "/all - All campaigns\n"
            "/stake - Stake campaigns\n"
            "/live - Live streams\n"
            "/history - Drop history\n"
            "/status - Bot status\n\n"
            "<b>PARALLEL WATCHER:</b>\n"
            "/watchround - Watch ALL live streams\n"
            "/watchroundstop - Stop\n"
            "/watchroundstatus - Status\n\n"
            "<b>SINGLE WATCHER:</b>\n"
            "/watchtest &lt;user1&gt; [user2] - Watch specific streams\n"
            "/watchstop - Stop all\n"
            "/watchstatus - Watch info\n\n"
            "<b>CONFIG:</b>\n"
            "/setcookie - Update cookie\n"
            "/testchat &lt;user&gt; - Test chat\n"
            "/stop - Unsubscribe\n"
            "/help - This",
            chat_id=chat_id)

    elif cmd == "/stop":
        remove_sub(chat_id)
        tg_send("Unsubscribed.", chat_id=chat_id)

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
        if len(parts) < 2: tg_send("Usage: /setcookie &lt;cookie&gt;", chat_id=chat_id); return
        save_cookie(parts[1].strip())
        tg_send("Cookie updated!", chat_id=chat_id)

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

# ---- Poller (5s) ----
def poller():
    state = load_state()
    known = state.get("known", {})
    poll_count = state.get("polls", 0)
    cookie_fails = 0
    log("Poller started")
    while True:
        poll_count += 1
        campaigns, cookie_ok = fetch_campaigns()
        if campaigns is None:
            if not cookie_ok:
                cookie_fails += 1
                if cookie_fails == 1:
                    tg_send_admin("Cookie expired! /setcookie <new>")
            time.sleep(POLL_INTERVAL + random.uniform(0, 3))
            continue
        cookie_fails = 0
        stake_campaigns = [c for c in campaigns if is_stake_drop(c)]
        for c in stake_campaigns:
            cid = c.get("id")
            name = c.get("name", "?")
            status = c.get("status", "?")
            ch_names = [ch.get("user", {}).get("username", "?") for ch in c.get("channels", [])]
            rew_names = [r.get("name", "?") for r in c.get("rewards", [])]
            if cid not in known:
                known[cid] = {"name": name, "status": status}
                add_to_history(c, "new")
                countdown = fmt_countdown(c.get("end_at", ""))
                tg_send(f"<b>NEW STAKE DROP!</b>\n\n<b>{name}</b>\nStatus: {status}\nChannels: {', '.join(ch_names)}\nRewards: {', '.join(rew_names[:5])}\nExpires: {countdown}\n\n<a href='https://kick.com/drops/all-campaigns'>Open Drops</a>")
                log(f"NEW: {name} ({status})")
            elif known[cid].get("status") != status:
                old = known[cid]["status"]
                known[cid]["status"] = status
                add_to_history(c, f"{old}->{status}")
                if status == "active":
                    countdown = fmt_countdown(c.get("end_at", ""))
                    tg_send(f"<b>STAKE DROP LIVE!</b>\n\n<b>{name}</b>\nExpires: {countdown}\n\n<a href='https://kick.com/drops/all-campaigns'>OPEN NOW</a>")
        if poll_count % 60 == 0:
            log(f"POLL #{poll_count} | {len(campaigns)} total | {len(stake_campaigns)} stake | {len(get_active_subs())} subs")
        state["known"] = known
        state["polls"] = poll_count
        state["last_poll"] = datetime.now().isoformat()
        save_state(state)
        time.sleep(POLL_INTERVAL + random.uniform(0, 1))

# ---- Main ----
def main():
    log("=" * 50)
    log("KICK STAKE DROPS BOT v15 - SMART + FOLLOW + LOGS")
    log("=" * 50)
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashboardHandler).serve_forever(), daemon=True).start()
    log(f"Dashboard: port {DASHBOARD_PORT} (user: {DASH_USER})")
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
    tg_send_admin("<b>Bot v15 Started!</b>\n\nSmart claim, follow streamers, logs dashboard.")
    log("Listening...")
    offset = 0
    while True:
        for u in tg_get_updates(offset):
            offset = u["update_id"] + 1
            msg = u.get("message", {})
            cid = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            if text.startswith("/"):
                log(f"CMD: {text} from {cid}")
                handle_command(text.split()[0].lower(), cid, text)
        time.sleep(1)

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: log("Stopped")
