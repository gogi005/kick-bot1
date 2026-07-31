"""
Kick Stake Drops Bot v13 (Instant Claim)
- /watchtest <user> - continuous watch until /watchstop
- /watchstop - stop watching
- /watchstatus - streamer, status, watch time, streams, claimed
- RoundRobin: every 30s check, random 3-15 min watch, followed streamers
- INSTANT claim: check every 30s, try API + !claim chat both
"""
import urllib.request, json, time, os, threading, random
import asyncio
import websockets
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============ CONFIG ============
TG_TOKEN = os.environ.get("TG_TOKEN", "8860462138:AAGkQQF1c-MyTfD3-3WluZNMarcT7HLj4dg")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8182391939"))
INITIAL_COOKIE = os.environ.get("KICK_COOKIE", "365875656%7C3qeqtSAxow2mU2adRmgluNijBSImYcgoLFRIZ2v9")
POLL_INTERVAL = 5
DROPS_API = "https://web.kick.com/api/v1/drops/campaigns"
PROGRESS_API = "https://web.kick.com/api/v1/drops/progress"
CLAIM_API = "https://web.kick.com/api/v1/drops/claim"
CHANNEL_API = "https://kick.com/api/v2/channels/{username}"
FOLLOWED_API = "https://web.kick.com/api/v1/followed-channels"
CHATROOM_API = "https://kick.com/api/v2/channels/{username}/chatroom"
CHAT_SEND_API = "https://kick.com/api/v2/messages/send/{chatroom_id}"
WS_TOKEN_API = "https://websockets.kick.com/viewer/v1/token"
WS_URL_TEMPLATE = "wss://websockets.kick.com/viewer/v1/connect?token={token}"
KICK_CLIENT_TOKEN = os.environ.get("KICK_CLIENT_TOKEN", "e1393935a959b4020a4491574f6490129f678acdaa92760471263db43487f823")
STATE_FILE = "tg_bot_state.json"
SUBS_FILE = "tg_subscribers.json"
COOKIE_FILE = "kick_cookie_live.json"
RR_STATE_FILE = "tg_roundrobin_state.json"
HISTORY_FILE = "tg_drop_history.json"
WATCH_STATE_FILE = "tg_watch_state.json"
DASHBOARD_PORT = int(os.environ.get("PORT", "8080"))
KEEPER_INTERVAL = 1800
BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://kick.com",
    "Referer": "https://kick.com/",
}
# ================================

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ---- Cookie Management ----
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
    headers["X-Client-Token"] = KICK_CLIENT_TOKEN
    if extra_headers: headers.update(extra_headers)
    req = urllib.request.Request(url)
    for k, v in headers.items(): req.add_header(k, v)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            log(f"[API] 403 Forbidden: {url}")
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
    except Exception as e:
        log(f"Channel info error for {username}: {e}")
        return None

def get_followed_streamers():
    """Fetch followed channels from Kick using session cookie"""
    try:
        data = kick_request(FOLLOWED_API)
        channels = data.get("data", [])
        usernames = []
        for ch in channels:
            username = ch.get("slug") or ch.get("username")
            if username:
                usernames.append(username)
        log(f"[FOLLOWED] Found {len(usernames)} followed channels")
        return usernames
    except Exception as e:
        log(f"[FOLLOWED] Error fetching followed: {e}")
        return []

def get_chatroom_id(username):
    """Get chatroom_id for a channel"""
    try:
        data = kick_request(CHATROOM_API.format(username=username))
        chatroom_id = data.get("data", {}).get("id")
        return chatroom_id
    except Exception as e:
        log(f"[CHAT] Chatroom ID error for @{username}: {e}")
        return None

def send_chat_message(chatroom_id, message):
    """Send a chat message to a Kick channel using session cookie"""
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
        result = json.loads(resp.read().decode())
        log(f"[CHAT] Sent '{message}' to chatroom {chatroom_id}")
        return result
    except urllib.error.HTTPError as e:
        log(f"[CHAT] Send error HTTP {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        log(f"[CHAT] Send error: {e}")
        return None

def try_claim_in_chat(username, message="!claim"):
    """Try sending !claim in chat - some streamers have bots that process this"""
    try:
        chatroom_id = get_chatroom_id(username)
        if chatroom_id:
            return send_chat_message(chatroom_id, message)
        else:
            log(f"[CHAT] No chatroom_id for @{username}")
            return None
    except Exception as e:
        log(f"[CHAT] try_claim_in_chat error: {e}")
        return None

def get_ws_token(session_token):
    try:
        data = kick_request(WS_TOKEN_API, extra_headers={
            "Authorization": f"Bearer {session_token}",
            "X-Client-Token": KICK_CLIENT_TOKEN,
            "Sec-Fetch-Site": "same-site",
        })
        return data.get("data", {}).get("token")
    except Exception as e:
        log(f"WS token error: {e}")
        return None

def get_session_token():
    cookie = get_cookie()
    if not cookie: return None
    import urllib.parse
    decoded = urllib.parse.unquote(cookie)
    if "|" in decoded:
        return decoded.split("|", 1)[1]
    return decoded

def fetch_progress():
    try:
        session_token = get_session_token()
        if not session_token:
            return []
        headers = dict(BASE_HEADERS)
        headers["Cookie"] = "session=" + get_cookie()
        headers["Authorization"] = f"Bearer {session_token}"
        headers["X-Client-Token"] = KICK_CLIENT_TOKEN
        req = urllib.request.Request(PROGRESS_API)
        for k, v in headers.items(): req.add_header(k, v)
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode()).get("data", [])
    except urllib.error.HTTPError as e:
        log(f"Progress HTTP {e.code}")
        return []
    except Exception as e:
        log(f"Progress fetch error: {e}")
        return []

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
        log(f"Claim response: {result}")
        return result
    except urllib.error.HTTPError as e:
        log(f"Claim HTTP {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        log(f"Claim error: {e}")
        return None

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
    tag = " [STAKE]" if "stake.com" in connect.lower() or "stake" in name.lower() else ""
    return f"{s} {name}{tag}\n  Cat: {cat}\n  Channels: {ch_str}\n  Rewards: {rew_str}\n  Connect: {connect[:60]}"

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
        "category": campaign.get("category", {}).get("name", "?") if isinstance(campaign.get("category"), dict) else "?",
        "channels": [ch.get("user", {}).get("username", ch.get("slug", "?")) for ch in campaign.get("channels", [])],
        "rewards": [r.get("name", "?") for r in campaign.get("rewards", [])],
        "end_at": campaign.get("end_at", ""),
        "event": event_type,
        "time": datetime.now().isoformat(),
    }
    existing_ids = [h.get("id") for h in history]
    if entry["id"] not in existing_ids:
        history.append(entry)
        save_history(history)

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
#  SINGLE WATCHER - /watchtest <user> continuous until /watchstop
# ============================================================
class SingleWatcher:
    def __init__(self):
        self.active = False
        self.username = None
        self.channel_id = None
        self.livestream_id = None
        self.stop_event = threading.Event()
        self.watch_thread = None
        self.started_at = None
        self.watch_time = 0
        self.events_sent = 0
        self.claimed = 0
        self._lock = threading.Lock()

    def start(self, chat_id, username):
        if self.active:
            return False, f"Already watching @{self.username}! Use /watchstop first."

        info = get_channel_info(username)
        if not info:
            return False, f"Channel @{username} not found."
        if not info.get("is_live"):
            return False, f"@{username} is OFFLINE right now. Try when they go live."

        self.active = True
        self.username = username
        self.channel_id = info["channel_id"]
        self.livestream_id = info.get("livestream_id")
        self.stop_event.clear()
        self.started_at = datetime.now()
        self.watch_time = 0
        self.events_sent = 0
        self.claimed = 0
        self.watch_thread = threading.Thread(target=self._run, args=(chat_id,), daemon=True)
        self.watch_thread.start()
        return True, f"<b>WATCHING @{username}!</b>\n\nContinuous mode. Send /watchstop to stop.\nStreamer goes offline = auto stop."

    def stop(self, reason="manual"):
        if not self.active:
            return False, "Not watching anyone."
        self.stop_event.set()
        self.active = False
        elapsed = (datetime.now() - self.started_at).total_seconds() if self.started_at else 0
        username = self.username
        claimed = self.claimed
        self.username = None
        msg = f"<b>WATCH STOPPED!</b> ({reason})\n\nStreamer: @{username}\nWatch time: {fmt_duration(elapsed)}\nEvents sent: {self.events_sent}\nRewards claimed: {claimed}"
        return True, msg

    def get_status(self):
        if not self.active:
            return "<b>WATCHER: IDLE</b>\n\nUse /watchtest <username> to start."
        elapsed = (datetime.now() - self.started_at).total_seconds() if self.started_at else 0
        return (f"<b>WATCHER: ACTIVE</b>\n\n"
                f"Streamer: @{self.username}\n"
                f"Channel ID: {self.channel_id}\n"
                f"Livestream ID: {self.livestream_id}\n"
                f"Watch time: {fmt_duration(elapsed)}\n"
                f"Events sent: {self.events_sent}\n"
                f"Rewards claimed: {self.claimed}\n\n"
                f"Send /watchstop to stop.")

    def _run(self, chat_id):
        try:
            self._do_watch()
        except Exception as e:
            log(f"[WATCH] Fatal error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if self.active:
                success, msg = self.stop(reason="error")
                if success:
                    tg_send(msg, chat_id=chat_id)
                    tg_send_admin(f"Watch stopped (error) for @{self.username}")

    def _do_watch(self):
        username = self.username
        channel_id = self.channel_id
        livestream_id = self.livestream_id
        log(f"[WATCH] Starting continuous watch for @{username}")

        while not self.stop_event.is_set():
            for attempt in range(3):
                if self.stop_event.is_set(): return
                try:
                    self._ws_connect_and_watch(username, channel_id, livestream_id)
                    break
                except Exception as e:
                    log(f"[WATCH] WS error for @{username} (attempt {attempt+1}): {e}")
                    if attempt < 2:
                        time.sleep(5 * (2 ** attempt))

            if self.stop_event.is_set(): return

            info = get_channel_info(username)
            if not info or not info.get("is_live"):
                success, msg = self.stop(reason="streamer went offline")
                if success:
                    tg_send(msg, chat_id=ADMIN_ID)
                return

            log(f"[WATCH] Reconnecting to @{username}...")
            time.sleep(10)

    def _ws_connect_and_watch(self, username, channel_id, livestream_id):
        ws_token = get_ws_token(get_cookie())
        if not ws_token:
            raise Exception("No WS token")

        ws_url = WS_URL_TEMPLATE.format(token=ws_token)
        headers = dict(BASE_HEADERS)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_loop(ws_url, headers, username, channel_id, livestream_id))
        finally:
            loop.close()

    async def _ws_loop(self, ws_url, headers, username, channel_id, livestream_id):
        async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
            handshake = json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}})
            await ws.send(handshake)
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                pass

            await send_user_event(ws, channel_id, livestream_id)
            with self._lock:
                self.events_sent += 1
            log(f"[WATCH] Connected to @{username} | continuous mode")

            last_user_event = time.time()
            last_ping = time.time()
            last_progress_check = time.time()
            last_alive_check = time.time()

            while not self.stop_event.is_set():
                now = time.time()

                if now - last_ping >= 20:
                    try:
                        await ws.send(json.dumps({"type": "ping"}))
                        last_ping = now
                        try: await asyncio.wait_for(ws.recv(), timeout=3)
                        except asyncio.TimeoutError: pass
                    except: pass

                if now - last_user_event >= 60:
                    await send_user_event(ws, channel_id, livestream_id)
                    with self._lock:
                        self.events_sent += 1
                        self.watch_time += 60
                    last_user_event = now
                    log(f"[WATCH] user_event #{self.events_sent} for @{username} ({fmt_duration(self.watch_time)} total)")

                if now - last_progress_check >= 30:
                    last_progress_check = now
                    try:
                        progress = fetch_progress()
                        if progress:
                            for item in progress:
                                for r in item.get("rewards", []):
                                    if r.get("claimed"): continue
                                    required = r.get("required_units", 0)
                                    current = r.get("progress", 0)
                                    campaign_id = item.get("campaign_id")
                                    reward_id = r.get("reward_id") or r.get("id")
                                    if required > 0 and current >= required:
                                        log(f"[WATCH] CLAIMABLE! {current}/{required}s - Claiming NOW")
                                        # Try API claim
                                        result = claim_reward(campaign_id, reward_id)
                                        if result:
                                            with self._lock:
                                                self.claimed += 1
                                            tg_send(f"<b>REWARD CLAIMED (API)!</b>\n\nStreamer: @{username}\nWatch time: {fmt_duration(self.watch_time)}", chat_id=ADMIN_ID)
                                            log(f"[WATCH] API Claim SUCCESS: {reward_id}")
                                        # Also try !claim in chat
                                        try_claim_in_chat(username, "!claim")
                                    elif required > 0:
                                        remaining = required - current
                                        log(f"[WATCH] Progress: {current}/{required}s ({remaining}s remaining)")
                    except Exception as e:
                        log(f"[WATCH] Progress check error: {e}")

                if now - last_alive_check >= 300:
                    last_alive_check = now
                    info = get_channel_info(username)
                    if not info or not info.get("is_live"):
                        log(f"[WATCH] @{username} went OFFLINE")
                        return

                await asyncio.sleep(1)

# ============================================================
#  ROUND-ROBIN WATCHER - every 30s check, random 3-15 min, followed streamers
# ============================================================
class RoundRobinWatcher:
    def __init__(self):
        self.active = False
        self.stop_event = threading.Event()
        self.watch_thread = None
        self.state = {"active": False, "current_streamer": None, "streamers_watched": 0,
                      "total_watch_time": 0, "rewards_claimed": 0, "started_at": None,
                      "per_streamer": {}, "last_check": None}
        self._lock = threading.Lock()
        self._save()

    def _save(self):
        with self._lock:
            with open(RR_STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2, default=str)

    def start(self):
        if self.active:
            return False, "Already running! Use /watchroundstop first."
        self.active = True
        self.stop_event.clear()
        self.state = {
            "active": True, "current_streamer": None, "streamers_watched": 0,
            "total_watch_time": 0, "rewards_claimed": 0,
            "started_at": datetime.now().isoformat(), "per_streamer": {},
            "last_check": None
        }
        self._save()
        self.watch_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self.watch_thread.start()
        return True, "<b>ROUND-ROBIN STARTED!</b>\n\nChecks every 30s for live Stake streamers.\nWatches random 3-15 min per stream.\nAlso checks your followed channels.\nAuto-claims rewards when ready."

    def stop(self):
        if not self.active:
            return False, "Not running!"
        self.stop_event.set()
        self.active = False
        self.state["active"] = False
        self.state["current_streamer"] = None
        self._save()
        elapsed = 0
        if self.state.get("started_at"):
            try:
                elapsed = (datetime.now() - datetime.fromisoformat(self.state["started_at"])).total_seconds()
            except: pass
        return True, (f"<b>ROUND-ROBIN STOPPED!</b>\n\n"
                      f"Streams watched: {self.state.get('streamers_watched', 0)}\n"
                      f"Total watch time: {fmt_duration(elapsed)}\n"
                      f"Rewards claimed: {self.state.get('rewards_claimed', 0)}")

    def get_status(self):
        if not self.active:
            return "<b>ROUND-ROBIN: IDLE</b>\n\nUse /watchround to start."
        current = self.state.get("current_streamer", "None")
        watched = self.state.get("streamers_watched", 0)
        claimed = self.state.get("rewards_claimed", 0)
        started = self.state.get("started_at")
        elapsed = 0
        if started:
            try: elapsed = (datetime.now() - datetime.fromisoformat(started)).total_seconds()
            except: pass
        return (f"<b>ROUND-ROBIN: ACTIVE</b>\n\n"
                f"Current: {current}\n"
                f"Streams watched: {watched}\n"
                f"Total watch time: {fmt_duration(elapsed)}\n"
                f"Rewards claimed: {claimed}\n\n"
                f"Checks every 30s. Random 3-15 min per stream.")

    def _run_async_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main_loop())
        except Exception as e:
            log(f"RR loop error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            loop.close()

    async def _main_loop(self):
        log("[RR] Round-Robin watcher started")
        loop = asyncio.get_event_loop()

        while not self.stop_event.is_set():
            try:
                all_streamers = set()

                campaigns, _ = await loop.run_in_executor(None, fetch_campaigns)
                if campaigns:
                    for c in campaigns:
                        if is_stake_drop(c) and c.get("status") == "active":
                            for ch in c.get("channels", []):
                                username = ch.get("slug") or ch.get("user", {}).get("username")
                                if username:
                                    all_streamers.add(username)

                followed = await loop.run_in_executor(None, get_followed_streamers)
                for u in followed:
                    all_streamers.add(u)

                if not all_streamers:
                    log("[RR] No streamers found, waiting 30s...")
                    self.state["last_check"] = datetime.now().isoformat()
                    self._save()
                    await asyncio.sleep(30)
                    continue

                live_streamers = []
                for username in all_streamers:
                    if self.stop_event.is_set(): break
                    info = await loop.run_in_executor(None, get_channel_info, username)
                    if info and info.get("is_live"):
                        live_streamers.append(info)
                    await asyncio.sleep(0.3)

                self.state["last_check"] = datetime.now().isoformat()
                self._save()

                if not live_streamers:
                    log(f"[RR] No live streamers ({len(all_streamers)} checked), waiting 30s...")
                    await asyncio.sleep(30)
                    continue

                log(f"[RR] Found {len(live_streamers)} live streamers")

                random.shuffle(live_streamers)

                for streamer in live_streamers:
                    if self.stop_event.is_set(): break
                    username = streamer["username"]
                    channel_id = streamer["channel_id"]
                    livestream_id = streamer.get("livestream_id")
                    watch_minutes = random.randint(3, 15)
                    watch_seconds = watch_minutes * 60

                    self.state["current_streamer"] = username
                    self._save()
                    log(f"[RR] Watching @{username} for {watch_minutes} min (random)")
                    tg_send(f"<b>RR:</b> Watching @{username} for {watch_minutes} min")

                    success = await self._watch_stream(username, channel_id, livestream_id, watch_seconds)

                    if success:
                        with self._lock:
                            self.state["streamers_watched"] = self.state.get("streamers_watched", 0) + 1
                            self.state["per_streamer"][username] = self.state.get("per_streamer", {}).get(username, 0) + watch_seconds
                            self.state["total_watch_time"] = self.state.get("total_watch_time", 0) + watch_seconds
                        self._save()

                    await self._check_and_claim()

                await asyncio.sleep(30)

            except Exception as e:
                log(f"[RR] Main loop error: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(30)

        log("[RR] Round-Robin watcher stopped")

    async def _watch_stream(self, username, channel_id, livestream_id, target_seconds):
        loop = asyncio.get_event_loop()
        for attempt in range(3):
            if self.stop_event.is_set(): return False
            try:
                ws_token = await loop.run_in_executor(None, get_ws_token, get_cookie())
                if not ws_token:
                    log(f"[RR] No WS token for @{username}")
                    return False
                ws_url = WS_URL_TEMPLATE.format(token=ws_token)
                headers = dict(BASE_HEADERS)
                async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
                    handshake = json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}})
                    await ws.send(handshake)
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError: pass
                    await send_user_event(ws, channel_id, livestream_id)
                    log(f"[RR] Connected to @{username}")

                    start_time = time.time()
                    last_user_event = time.time()
                    last_ping = time.time()
                    last_progress_check = time.time()
                    event_count = 1

                    while not self.stop_event.is_set():
                        now = time.time()
                        elapsed = now - start_time
                        if elapsed >= target_seconds:
                            log(f"[RR] Done with @{username} ({int(elapsed)}s)")
                            return True
                        if now - last_ping >= 20:
                            try:
                                await ws.send(json.dumps({"type": "ping"}))
                                last_ping = now
                                try: await asyncio.wait_for(ws.recv(), timeout=3)
                                except asyncio.TimeoutError: pass
                            except: pass
                        if now - last_user_event >= 60:
                            await send_user_event(ws, channel_id, livestream_id)
                            event_count += 1
                            last_user_event = now
                            remaining = int(target_seconds - elapsed)
                            log(f"[RR] user_event #{event_count} for @{username} ({remaining}s left)")
                        # Check progress every 30 seconds for immediate claim
                        if now - last_progress_check >= 30:
                            last_progress_check = now
                            try:
                                progress = await loop.run_in_executor(None, fetch_progress)
                                if progress:
                                    for item in progress:
                                        for r in item.get("rewards", []):
                                            if r.get("claimed"): continue
                                            required = r.get("required_units", 0)
                                            current = r.get("progress", 0)
                                            campaign_id = item.get("campaign_id")
                                            reward_id = r.get("reward_id") or r.get("id")
                                            if required > 0 and current >= required:
                                                log(f"[RR-WATCH] CLAIMABLE! {current}/{required}s")
                                                result = await loop.run_in_executor(None, claim_reward, campaign_id, reward_id)
                                                if result:
                                                    with self._lock:
                                                        self.state["rewards_claimed"] = self.state.get("rewards_claimed", 0) + 1
                                                    self._save()
                                                    tg_send(f"<b>REWARD CLAIMED (API)!</b>\n\nStreamer: @{username}", chat_id=ADMIN_ID)
                                                await loop.run_in_executor(None, try_claim_in_chat, username, "!claim")
                            except Exception as e:
                                log(f"[RR-WATCH] Progress check error: {e}")
                        if event_count % 5 == 0:
                            info = await loop.run_in_executor(None, get_channel_info, username)
                            if not info or not info.get("is_live"):
                                log(f"[RR] @{username} went offline")
                                return True
                        await asyncio.sleep(1)
            except Exception as e:
                log(f"[RR] Watch error for @{username}: {e}")
                if attempt < 2: await asyncio.sleep(5 * (2 ** attempt))
                else: return False
        return False

    async def _check_and_claim(self):
        try:
            loop = asyncio.get_event_loop()
            progress = await loop.run_in_executor(None, fetch_progress)
            if not progress: return
            current_streamer = self.state.get("current_streamer", "")
            for item in progress:
                campaign_id = item.get("campaign_id") or item.get("id")
                campaign_name = item.get("campaign_name", campaign_id[:16] if campaign_id else "?")
                for r in item.get("rewards", []):
                    reward_id = r.get("reward_id") or r.get("id")
                    claimed = r.get("claimed", False)
                    required = r.get("required_units", 0)
                    current = r.get("progress", 0)
                    if claimed: continue
                    if required > 0 and current >= required:
                        log(f"[RR-CLAIM] CLAIMABLE! {current}/{required}s - Claiming NOW")
                        # Try API claim
                        result = await loop.run_in_executor(None, claim_reward, campaign_id, reward_id)
                        if result:
                            with self._lock:
                                self.state["rewards_claimed"] = self.state.get("rewards_claimed", 0) + 1
                            self._save()
                            tg_send(f"<b>REWARD CLAIMED (API)!</b>\n\nCampaign: {campaign_name}\nStreamer: @{current_streamer}", chat_id=ADMIN_ID)
                            log(f"[RR-CLAIM] API Claim SUCCESS: {reward_id}")
                        # Also try !claim in chat
                        if current_streamer:
                            await loop.run_in_executor(None, try_claim_in_chat, current_streamer, "!claim")
        except Exception as e:
            log(f"[RR-CLAIM] Error: {e}")

single_watcher = SingleWatcher()
rr_watcher = RoundRobinWatcher()

# ---- Dashboard ----
class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = load_state()
        rr = rr_watcher.state
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
        rr_status = "ACTIVE" if rr.get("active") else "STOPPED"
        rr_color = "#4CAF50" if rr.get("active") else "#f44336"
        sw_status = "ACTIVE" if single_watcher.active else "IDLE"
        sw_color = "#4CAF50" if single_watcher.active else "#999"
        sw_user = f"@{single_watcher.username}" if single_watcher.username else "None"
        sw_time = fmt_duration(single_watcher.watch_time) if single_watcher.active else "0s"
        html = f"""<!DOCTYPE html><html><head><title>Kick Drops v13</title>
<meta http-equiv="refresh" content="30">
<style>body{{font-family:Arial;background:#1a1a2e;color:#eee;padding:20px}}h1{{color:#e94560}}.c{{background:#16213e;padding:20px;border-radius:10px;margin:10px 0}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #333}}th{{background:#0f3460}}</style></head><body>
<h1>Kick Stake Drops Bot v13</h1>
<div class="c"><h2>Status</h2><p>Polls: {state.get('polls',0)}</p><p>Last: {state.get('last_poll','never')}</p></div>
<div class="c"><h2>Single Watcher</h2><p style='color:{sw_color};font-size:1.2em'><b>{sw_status}</b></p><p>Streamer: {sw_user}</p><p>Watch time: {sw_time}</p><p>Events: {single_watcher.events_sent}</p><p>Claimed: {single_watcher.claimed}</p></div>
<div class="c"><h2>Round-Robin</h2><p style='color:{rr_color};font-size:1.2em'><b>{rr_status}</b></p><p>Streamer: {rr.get('current_streamer','None')}</p><p>Watched: {rr.get('streamers_watched',0)}</p><p>Watch time: {fmt_duration(rr.get('total_watch_time',0))}</p><p>Claimed: {rr.get('rewards_claimed',0)}</p></div>
<div class="c"><h2>Users ({active}/{len(subs)})</h2><table><tr><th>ID</th><th>Status</th><th>Joined</th></tr>{rows}</table></div>
<div class="c"><h2>Drops ({len(known)})</h2><table><tr><th>Name</th><th>Status</th></tr>{drops}</table></div></body></html>"""
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
            "<b>Kick Stake Drops Bot v13</b>\n\n"
            "<b>DROP COMMANDS:</b>\n"
            "/all - Show ALL campaigns\n"
            "/stake - Stake.com campaigns only\n"
            "/live - Live Stake streamers\n"
            "/history - Past drop history\n"
            "/status - Bot status\n\n"
            "<b>SINGLE WATCHER:</b>\n"
            "/watchtest &lt;user&gt; - Watch streamer continuously\n"
            "/watchstop - Stop watching\n"
            "/watchstatus - Current watch info\n\n"
            "<b>ROUND-ROBIN (AUTO-WATCH + CLAIM):</b>\n"
            "/watchround - Start auto-watching\n"
            "/watchroundstop - Stop auto-watching\n"
            "/watchroundstatus - Current RR status\n\n"
            "<b>AUTO-CLAIM:</b>\n"
            "/autoclaim on/off - Toggle auto-claim\n\n"
            "<b>CONFIG:</b>\n"
            "/setcookie - Update Kick cookie\n"
            "/testchat &lt;user&gt; [msg] - Test chat message\n"
            "/stop - Unsubscribe\n"
            "/help - This message\n\n"
            "<b>How it works:</b>\n"
            "RoundRobin checks every 30s for live\n"
            "Stake streamers + your followed channels.\n"
            "Watches random 3-15 min per stream.\n"
            "Claims IMMEDIATELY (API + chat !claim).\n\n"
            "/watchtest <user> watches one streamer\n"
            "continuously until /watchstop or offline.",
            chat_id=chat_id
        )

    elif cmd == "/stop":
        remove_sub(chat_id)
        tg_send("Unsubscribed.", chat_id=chat_id)

    elif cmd == "/all":
        campaigns, _ = fetch_campaigns()
        if not campaigns:
            tg_send("API unavailable.", chat_id=chat_id); return
        msg = f"<b>All ({len(campaigns)}):</b>\n\n"
        for c in campaigns: msg += fmt_campaign(c) + "\n\n"
        for i in range(0, len(msg), 4000): tg_send(msg[i:i+4000], chat_id=chat_id)

    elif cmd == "/stake":
        campaigns, _ = fetch_campaigns()
        if not campaigns:
            tg_send("API unavailable.", chat_id=chat_id); return
        stake = [c for c in campaigns if is_stake_drop(c)]
        if not stake:
            tg_send(f"No Stake drops. Total: {len(campaigns)}", chat_id=chat_id); return
        msg = f"<b>Stake ({len(stake)}):</b>\n\n"
        for c in stake: msg += fmt_campaign(c) + "\n\n"
        tg_send(msg, chat_id=chat_id)

    elif cmd == "/live":
        tg_send("Checking live streams...", chat_id=chat_id)
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
                tg_send(f"No Stake streams live.\nTotal: {len(streams)}", chat_id=chat_id)
            else:
                msg = f"<b>Live Stake ({len(live_stake)}):</b>\n\n"
                for s in sorted(live_stake, key=lambda x: -x["viewers"]):
                    msg += f"@{s['username']} | {s['viewers']}v | {s['category']}\n  {s['title']}\n\n"
                for i in range(0, len(msg), 4000): tg_send(msg[i:i+4000], chat_id=chat_id)
        except Exception as e:
            tg_send(f"Error: {e}", chat_id=chat_id)

    elif cmd == "/history":
        history = load_history()
        if not history:
            tg_send("No drop history yet.", chat_id=chat_id); return
        msg = f"<b>Drop History ({len(history)}):</b>\n\n"
        for h in history[-20:]:
            s = {"active": "LIVE", "upcoming": "SOON", "expired": "EXP"}.get(h.get("status", ""), "?")
            end = fmt_countdown(h.get("end_at", ""))
            msg += f"{s} {h.get('name', '?')}\n  {h.get('event', '?')} | {h.get('time', '?')[:16]}\n  Ends: {end}\n\n"
        for i in range(0, len(msg), 4000): tg_send(msg[i:i+4000], chat_id=chat_id)

    elif cmd == "/status":
        state = load_state()
        tg_send(f"Polls: {state.get('polls',0)}\nDrops: {len(state.get('known',{}))}\nSubs: {len(get_active_subs())}\nHistory: {len(load_history())}\nWatcher: {'ACTIVE' if single_watcher.active else 'IDLE'}\nRoundRobin: {'ACTIVE' if rr_watcher.active else 'IDLE'}", chat_id=chat_id)

    elif cmd == "/testchat":
        parts = text.split()
        if len(parts) < 2:
            tg_send("Usage: /testchat &lt;username&gt; [message]\nDefault message: !claim", chat_id=chat_id)
            return
        username = parts[1].strip("@")
        message = parts[2] if len(parts) > 2 else "!claim"
        tg_send(f"Testing chat: sending '{message}' to @{username}...", chat_id=chat_id)
        chatroom_id = get_chatroom_id(username)
        if not chatroom_id:
            tg_send(f"Could not get chatroom_id for @{username}", chat_id=chat_id)
            return
        result = send_chat_message(chatroom_id, message)
        if result:
            tg_send(f"<b>CHAT SENT!</b>\nChatroom: {chatroom_id}\nMessage: {message}", chat_id=chat_id)
        else:
            tg_send(f"Chat send failed. Chatroom: {chatroom_id}", chat_id=chat_id)

    elif cmd == "/setcookie":
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send("Usage: /setcookie &lt;cookie&gt;", chat_id=chat_id); return
        save_cookie(parts[1].strip())
        tg_send("Cookie updated!", chat_id=chat_id)
        tg_send_admin(f"Cookie updated by {chat_id}")

    elif cmd == "/autoclaim":
        parts = text.split()
        if len(parts) > 1 and parts[1].lower() in ("on", "1", "yes"):
            tg_send("<b>Auto-Claim: ON</b>", chat_id=chat_id)
        elif len(parts) > 1 and parts[1].lower() in ("off", "0", "no"):
            tg_send("<b>Auto-Claim: OFF</b>", chat_id=chat_id)
        else:
            tg_send("<b>Auto-Claim: ON</b>\n\nRoundRobin auto-claims when watch time is met.", chat_id=chat_id)

    # ---- Single Watcher ----
    elif cmd == "/watchtest":
        parts = text.split()
        if len(parts) < 2:
            tg_send("Usage: /watchtest &lt;username&gt;\nExample: /watchtest stake", chat_id=chat_id)
            return
        username = parts[1].strip("@")
        success, msg = single_watcher.start(chat_id, username)
        tg_send(msg, chat_id=chat_id)
        if success:
            tg_send_admin(f"Watch started: @{username} by {chat_id}")

    elif cmd == "/watchstop":
        success, msg = single_watcher.stop(reason="user stopped")
        tg_send(msg, chat_id=chat_id)

    elif cmd == "/watchstatus":
        tg_send(single_watcher.get_status(), chat_id=chat_id)

    # ---- Round-Robin ----
    elif cmd == "/watchround":
        success, msg = rr_watcher.start()
        tg_send(msg, chat_id=chat_id)
        if success:
            tg_send_admin(f"Round-Robin started by {chat_id}")

    elif cmd == "/watchroundstop":
        success, msg = rr_watcher.stop()
        tg_send(msg, chat_id=chat_id)

    elif cmd == "/watchroundstatus":
        tg_send(rr_watcher.get_status(), chat_id=chat_id)

# ---- Poller ----
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
                    tg_send_admin("Cookie expired! Send /setcookie <new>")
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
                old_status = known[cid]["status"]
                known[cid]["status"] = status
                add_to_history(c, f"{old_status}->{status}")
                if status == "active":
                    countdown = fmt_countdown(c.get("end_at", ""))
                    tg_send(f"<b>STAKE DROP LIVE!</b>\n\n<b>{name}</b>\nExpires: {countdown}\n\n<a href='https://kick.com/drops/all-campaigns'>OPEN NOW</a>")
                    log(f"LIVE: {name}")
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
    log("KICK STAKE DROPS BOT v13 - INSTANT CLAIM")
    log("=" * 50)
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashboardHandler).serve_forever(), daemon=True).start()
    log(f"Dashboard: port {DASHBOARD_PORT}")
    threading.Thread(target=poller, daemon=True).start()
    try:
        threading.Thread(target=session_keeper, daemon=True).start()
    except Exception as e:
        log(f"Keeper not started (playwright?): {e}")
    tg_send_admin("<b>Bot v13 Started!</b>\n\nInstant claim: API + chat !claim\nChecks every 30s. Random 3-15 min.")
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
