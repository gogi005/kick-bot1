"""
Kick Stake Drops Bot v11 (Bug Fixes)
- Cookie auto-refresh via Playwright (every 30 min)
- Multi-user with admin alerts
- Web dashboard
- 24/7 polling
- Round-robin mode - watch 5-10 min per streamer then switch
- Auto-claim with 2-min claim window handling
- Progress tracking every 2 minutes (server-side)
- NEW: Auto-subscribe on any command (no /start needed after deploy)
- NEW: /watchtest, /testws commands for debugging
- FIXED: watchround async loop issues
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
WS_TOKEN_API = "https://websockets.kick.com/viewer/v1/token"
WS_URL_TEMPLATE = "wss://websockets.kick.com/viewer/v1/connect?token={token}"
KICK_CLIENT_TOKEN = os.environ.get("KICK_CLIENT_TOKEN", "e1393935a959b4020a4491574f6490129f678acdaa92760471263db43487f823")
STATE_FILE = "tg_bot_state.json"
SUBS_FILE = "tg_subscribers.json"
COOKIE_FILE = "kick_cookie_live.json"
RR_STATE_FILE = "tg_roundrobin_state.json"
HISTORY_FILE = "tg_drop_history.json"
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
    """Extract session token from cookie for Bearer auth"""
    cookie = get_cookie()
    if not cookie:
        return None
    # Cookie might be URL-encoded or plain
    import urllib.parse
    decoded = urllib.parse.unquote(cookie)
    # If it contains |, take the part after (Laravel session format)
    if "|" in decoded:
        return decoded.split("|", 1)[1]
    return decoded

def fetch_progress():
    try:
        session_token = get_session_token()
        if not session_token:
            log("No session token for progress")
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
        log(f"Progress HTTP {e.code}: {e.read().decode()[:200]}")
        return []
    except Exception as e:
        log(f"Progress fetch error: {e}")
        return []

def claim_reward(campaign_id, reward_id):
    try:
        session_token = get_session_token()
        if not session_token:
            log("No session token for claim")
            return None
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
    """Send user watch event via WebSocket - must be sent every 60s to track watch time"""
    event = {"type": "user_event", "data": {"message": {
        "name": "tracking.user.watch.livestream",
        "channel_id": channel_id,
        "livestream_id": int(livestream_id) if livestream_id else int(channel_id),
    }}}
    await ws.send(json.dumps(event))

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

def is_stake_drop(c):
    connect = c.get("connect_url", "").lower()
    name = c.get("name", "").lower()
    channels = c.get("channels", [])
    # Check connect URL
    if "stake.com" in connect or "stake" in connect:
        return True
    # Check campaign name
    if "stake" in name:
        return True
    # Check channel names/usernames
    for ch in channels:
        username = (ch.get("slug", "") + ch.get("user", {}).get("username", "")).lower()
        if "stake" in username:
            return True
    # Check category
    cat = c.get("category", {})
    if isinstance(cat, dict) and "stake" in cat.get("name", "").lower():
        return True
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
    tag = " [STAKE]" if "stake.com" in connect.lower() or "stake.com" in name.lower() else ""
    return f"{s} {name}{tag}\n  Cat: {cat}\n  Channels: {ch_str}\n  Rewards: {rew_str}\n  Connect: {connect[:60]}"

# ---- State ----
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: return json.load(f)
        except: pass
    return {"known": {}, "polls": 0, "last_poll": None}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2, default=str)

def load_rr_state():
    if os.path.exists(RR_STATE_FILE):
        try:
            with open(RR_STATE_FILE) as f: return json.load(f)
        except: pass
    return {"active": False, "current_streamer": None, "streamers_watched": 0,
            "total_watch_time": 0, "rewards_claimed": 0, "started_at": None, "per_streamer": {}}

def save_rr_state(s):
    with open(RR_STATE_FILE, "w") as f: json.dump(s, f, indent=2, default=str)

# ---- Auto-Claim Feature ----
auto_claim_enabled = True  # Default ON

def auto_claim_new_drop(campaign):
    """When a new Stake drop is detected, watch its streamers and claim"""
    try:
        name = campaign.get("name", "?")
        channels = campaign.get("channels", [])
        campaign_id = campaign.get("id")
        
        if not channels:
            log(f"[AUTO-CLAIM] No channels for {name}, skipping")
            return
        
        # Find live streamers from this campaign
        live_streamers = []
        for ch in channels:
            username = ch.get("slug") or ch.get("user", {}).get("username")
            if not username:
                continue
            info = get_channel_info(username)
            if info and info.get("is_live"):
                live_streamers.append(info)
            time.sleep(0.5)
        
        if not live_streamers:
            log(f"[AUTO-CLAIM] No live streamers for {name}")
            return
        
        tg_send(f"<b>AUTO-CLAIM:</b> Watching {name} streamers...\n" + ", ".join([s["username"] for s in live_streamers]))
        
        # Watch each live streamer for enough time to claim
        for streamer in live_streamers:
            username = streamer["username"]
            channel_id = streamer["channel_id"]
            livestream_id = streamer.get("livestream_id")
            
            log(f"[AUTO-CLAIM] Watching {username} for {name}")
            _watch_and_claim(username, channel_id, livestream_id, campaign_id)
            
            # Check and claim after watching
            _check_and_claim_sync()
            
    except Exception as e:
        log(f"[AUTO-CLAIM] Error: {e}")

def _watch_and_claim(username, channel_id, livestream_id, campaign_id, minutes=5):
    """Watch a streamer for X minutes via WebSocket, then check claim"""
    target_seconds = minutes * 60
    
    for attempt in range(3):
        try:
            ws_token = get_ws_token(get_cookie())
            if not ws_token:
                log(f"[AUTO-CLAIM] No WS token for {username}")
                return False
            
            ws_url = WS_URL_TEMPLATE.format(token=ws_token)
            headers = {k: v for k, v in BASE_HEADERS.items()}
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_ws_watch_loop(
                    ws_url, headers, channel_id, livestream_id, 
                    username, target_seconds
                ))
            finally:
                loop.close()
            return True
            
        except Exception as e:
            log(f"[AUTO-CLAIM] Watch error for {username}: {e}")
            if attempt < 2:
                time.sleep(5 * (2 ** attempt))
    return False

async def _ws_watch_loop(ws_url, headers, channel_id, livestream_id, username, target_seconds):
    """Async WebSocket watch loop - sends user_event every 60s, server tracks progress every 2 min"""
    loop = asyncio.get_event_loop()
    async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
        # Handshake required to register for channel events
        handshake = json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}})
        await ws.send(handshake)
        
        # Initial user_event
        await send_user_event(ws, channel_id, livestream_id)
        log(f"[AUTO-CLAIM] Connected to {username} | Channel: {channel_id}")
        
        start_time = time.time()
        last_user_event = time.time()
        last_ping = time.time()
        last_progress_check = time.time()
        event_count = 1
        
        while True:
            now = time.time()
            elapsed = now - start_time
            
            # Time's up - stop watching
            if elapsed >= target_seconds:
                log(f"[AUTO-CLAIM] Done watching {username} ({int(elapsed)}s)")
                return
            
            # Ping every 20s to keep connection alive
            if now - last_ping >= 20:
                try:
                    await ws.send(json.dumps({"type": "ping"}))
                    last_ping = now
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=3)
                    except asyncio.TimeoutError:
                        pass
                except Exception:
                    pass
            
            # user_event every 60s - THIS IS CRITICAL for watch time tracking
            if now - last_user_event >= 60:
                await send_user_event(ws, channel_id, livestream_id)
                event_count += 1
                last_user_event = now
                remaining = int(target_seconds - elapsed)
                log(f"[WS] user_event #{event_count} for {username} | {remaining}s left")
            
            # Check progress every 2 minutes (server-side tracking interval)
            if now - last_progress_check >= 120:
                last_progress_check = now
                try:
                    progress = await loop.run_in_executor(None, fetch_progress)
                    if progress:
                        for item in progress:
                            if item.get("campaign_id"):
                                for r in item.get("rewards", []):
                                    if r.get("claimed"):
                                        continue
                                    required = r.get("required_units", 0)
                                    current = r.get("progress", 0)
                                    if required > 0:
                                        log(f"[WS] Progress: {current}/{required}s for reward {r.get('reward_id', '?')[:16]}")
                except Exception as e:
                    log(f"[WS] Progress check error: {e}")
            
            await asyncio.sleep(1)

def _check_and_claim_sync():
    """Check progress and claim rewards synchronously - claim window is 2 minutes after watch time met"""
    try:
        progress = fetch_progress()
        if not progress:
            log("[CLAIM] No progress data")
            return
        
        for item in progress:
            campaign_id = item.get("campaign_id") or item.get("id")
            campaign_name = item.get("campaign_name", campaign_id[:16] if campaign_id else "?")
            
            for r in item.get("rewards", []):
                reward_id = r.get("reward_id") or r.get("id")
                claimed = r.get("claimed", False)
                required = r.get("required_units", 0)
                current = r.get("progress", 0)
                status = r.get("status", "")
                
                if claimed:
                    continue
                
                if required > 0 and current >= required:
                    # Watch time met - CLAIM IMMEDIATELY (2-min window!)
                    log(f"[CLAIM] Watch time met! {current}/{required}s - Claiming {reward_id}")
                    result = claim_reward(campaign_id, reward_id)
                    if result:
                        tg_send(f"<b>REWARD CLAIMED!</b>\n\nCampaign: {campaign_name}\nReward: {reward_id[:16]}...")
                        log(f"[CLAIM] SUCCESS: {reward_id}")
                    else:
                        log(f"[CLAIM] FAILED: {reward_id}")
                elif required > 0:
                    # Still watching
                    remaining = required - current
                    log(f"[CLAIM] Progress: {current}/{required}s ({remaining}s remaining)")
    except Exception as e:
        log(f"[CLAIM] Error: {e}")

def toggle_auto_claim(enable):
    global auto_claim_enabled
    auto_claim_enabled = enable
    return "ON" if enable else "OFF"

def get_auto_claim_status():
    return "ON" if auto_claim_enabled else "OFF"

# ---- Round-Robin Watcher ----
class RoundRobinWatcher:
    def __init__(self):
        self.active = False
        self.stop_event = threading.Event()
        self.watch_thread = None
        self.state = load_rr_state()
        self._lock = threading.Lock()
        self.min_per_streamer = 7

    def _save(self):
        with self._lock:
            save_rr_state(self.state)

    def start(self, minutes_per_streamer=7):
        if self.active:
            return False, "Already watching! Use /watchroundstop first."
        self.min_per_streamer = minutes_per_streamer
        self.stop_event.clear()
        self.active = True
        self.state = load_rr_state()
        self.state["active"] = True
        self.state["started_at"] = datetime.now().isoformat()
        self.state["current_streamer"] = None
        self.state["streamers_watched"] = 0
        self.state["total_watch_time"] = 0
        self.state["rewards_claimed"] = 0
        self.state["per_streamer"] = {}
        self._save()
        self.watch_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self.watch_thread.start()
        return True, f"<b>ROUND-ROBIN STARTED!</b>\n\nWatching each streamer for {minutes_per_streamer} min then switching.\nAuto-claiming rewards when ready."

    def stop(self):
        if not self.active:
            return False, "Not watching!"
        self.stop_event.set()
        self.active = False
        self.state["active"] = False
        self.state["current_streamer"] = None
        self._save()
        return True, f"<b>ROUND-ROBIN STOPPED!</b>\nStreams: {self.state.get('streamers_watched', 0)}\nClaimed: {self.state.get('rewards_claimed', 0)}"

    def get_status(self):
        if not self.active:
            return "<b>ROUND-ROBIN: IDLE</b>\n\nUse /watchround to start."
        current = self.state.get("current_streamer", "None")
        watched = self.state.get("streamers_watched", 0)
        claimed = self.state.get("rewards_claimed", 0)
        # Calculate REAL time from started_at
        started = self.state.get("started_at")
        if started:
            try:
                start_dt = datetime.fromisoformat(started)
                elapsed = (datetime.now() - start_dt).total_seconds()
                th, tm = int(elapsed) // 3600, (int(elapsed) % 3600) // 60
            except:
                th, tm = 0, 0
        else:
            th, tm = 0, 0
        return (f"<b>ROUND-ROBIN: ACTIVE</b>\n\n"
                f"Current: {current}\n"
                f"Min per stream: {self.min_per_streamer}\n"
                f"Streams watched: {watched}\n"
                f"Total watch time: {th}h {tm}m\n"
                f"Rewards claimed: {claimed}")

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
        log("Round-Robin watcher started")
        loop = asyncio.get_event_loop()
        while not self.stop_event.is_set():
            try:
                campaigns, _ = await loop.run_in_executor(None, fetch_campaigns)
                if not campaigns:
                    await asyncio.sleep(30); continue

                streamers = []
                for c in campaigns:
                    if is_stake_drop(c) and c.get("status") == "active":
                        for ch in c.get("channels", []):
                            username = ch.get("slug") or ch.get("user", {}).get("username")
                            if username: streamers.append(username)

                if not streamers:
                    log("No Stake campaigns with channels, waiting..."); await asyncio.sleep(30); continue

                live_streamers = []
                for username in streamers:
                    if self.stop_event.is_set(): break
                    info = await loop.run_in_executor(None, get_channel_info, username)
                    if info and info.get("is_live"):
                        live_streamers.append(info)
                    await asyncio.sleep(0.5)

                if not live_streamers:
                    log("No live streamers, waiting..."); await asyncio.sleep(30); continue

                log(f"Found {len(live_streamers)} live streamers")

                for streamer in live_streamers:
                    if self.stop_event.is_set(): break
                    username = streamer["username"]
                    channel_id = streamer["channel_id"]
                    livestream_id = streamer.get("livestream_id")
                    self.state["current_streamer"] = username
                    self._save()
                    log(f"Watching {username} for {self.min_per_streamer} min...")
                    tg_send(f"<b>SWITCHING:</b> Now watching {username} ({self.min_per_streamer} min)")
                    success = await self._watch_stream(username, channel_id, livestream_id)
                    if success:
                        with self._lock:
                            self.state["streamers_watched"] = self.state.get("streamers_watched", 0) + 1
                            self.state["per_streamer"][username] = self.state.get("per_streamer", {}).get(username, 0) + self.min_per_streamer * 60
                        self._save()
                    await self._check_and_claim()

            except Exception as e:
                log(f"RR main loop error: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(15)
        log("Round-Robin watcher stopped")

    async def _watch_stream(self, username, channel_id, livestream_id):
        target_seconds = self.min_per_streamer * 60
        loop = asyncio.get_event_loop()
        for attempt in range(3):
            if self.stop_event.is_set(): return False
            try:
                ws_token = await loop.run_in_executor(None, get_ws_token, get_cookie())
                if not ws_token: 
                    log(f"No WS token for {username}")
                    return False
                ws_url = WS_URL_TEMPLATE.format(token=ws_token)
                headers = dict(BASE_HEADERS)
                log(f"[WS] Connecting to {username} | channel_id={channel_id} | livestream_id={livestream_id}")
                async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
                    handshake = json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}})
                    await ws.send(handshake)
                    log(f"[WS] Handshake sent for {username}")
                    # Wait for handshake response
                    try:
                        resp = await asyncio.wait_for(ws.recv(), timeout=5)
                        log(f"[WS] Handshake response: {resp[:200]}")
                    except asyncio.TimeoutError:
                        log(f"[WS] No handshake response (timeout)")
                    await send_user_event(ws, channel_id, livestream_id)
                    log(f"[WS] Connected to {username} | watching for {self.min_per_streamer} min")
                    start_time = time.time()
                    last_user_event = time.time()
                    last_ping = time.time()
                    event_count = 1
                    while not self.stop_event.is_set():
                        now = time.time()
                        elapsed = now - start_time
                        if elapsed >= target_seconds:
                            log(f"[WS] Time up for {username} ({int(elapsed)}s), switching..."); return True
                        if now - last_ping >= 20:
                            try:
                                await ws.send(json.dumps({"type": "ping"}))
                                last_ping = now
                                try: await asyncio.wait_for(ws.recv(), timeout=3)
                                except asyncio.TimeoutError: pass
                            except Exception as e:
                                log(f"[WS] Ping error: {e}")
                        if now - last_user_event >= 60:
                            await send_user_event(ws, channel_id, livestream_id)
                            event_count += 1
                            last_user_event = now
                            with self._lock:
                                self.state["total_watch_time"] = self.state.get("total_watch_time", 0) + 60
                            self._save()
                            remaining = int(target_seconds - elapsed)
                            log(f"[WS] user_event #{event_count} for {username} ({remaining}s left)")
                        if event_count % 5 == 0:
                            info = await loop.run_in_executor(None, get_channel_info, username)
                            if not info or not info.get("is_live"):
                                log(f"[WS] {username} went offline"); return True
                        await asyncio.sleep(1)
            except Exception as e:
                log(f"[WS] Watch error for {username}: {e}")
                import traceback
                traceback.print_exc()
                if attempt < 2: await asyncio.sleep(5 * (2 ** attempt))
                else: return False
        return False

    async def _check_and_claim(self):
        """Check progress and claim - claim window is 2 minutes after watch time met"""
        try:
            loop = asyncio.get_event_loop()
            progress = await loop.run_in_executor(None, fetch_progress)
            if not progress:
                return
            
            for item in progress:
                campaign_id = item.get("campaign_id") or item.get("id")
                campaign_name = item.get("campaign_name", campaign_id[:16] if campaign_id else "?")
                
                for r in item.get("rewards", []):
                    reward_id = r.get("reward_id") or r.get("id")
                    claimed = r.get("claimed", False)
                    required = r.get("required_units", 0)
                    current = r.get("progress", 0)
                    
                    if claimed:
                        continue
                    
                    if required > 0 and current >= required:
                        # CLAIM IMMEDIATELY - 2-minute window!
                        log(f"[RR-CLAIM] Watch time met! {current}/{required}s - Claiming {reward_id}")
                        result = await loop.run_in_executor(None, claim_reward, campaign_id, reward_id)
                        if result:
                            with self._lock:
                                self.state["rewards_claimed"] = self.state.get("rewards_claimed", 0) + 1
                            self._save()
                            tg_send(f"<b>REWARD CLAIMED!</b>\n\nCampaign: {campaign_name}\nReward: {reward_id[:16]}...")
                            log(f"[RR-CLAIM] SUCCESS: {reward_id}")
                        else:
                            log(f"[RR-CLAIM] FAILED: {reward_id}")
                    elif required > 0:
                        remaining = required - current
                        log(f"[RR-CLAIM] Progress: {current}/{required}s ({remaining}s remaining)")
        except Exception as e:
            log(f"[RR-CLAIM] Error: {e}")

rr_watcher = RoundRobinWatcher()

# ---- Dashboard ----
class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = load_state()
        rr = load_rr_state()
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
        rr_streamer = rr.get("current_streamer", "None")
        rr_watched = rr.get("streamers_watched", 0)
        rr_claimed = rr.get("rewards_claimed", 0)
        total = rr.get("total_watch_time", 0)
        th, tm = total // 3600, (total % 3600) // 60
        html = f"""<!DOCTYPE html><html><head><title>Kick Drops v11</title>
<meta http-equiv="refresh" content="30">
<style>body{{font-family:Arial;background:#1a1a2e;color:#eee;padding:20px}}h1{{color:#e94560}}.c{{background:#16213e;padding:20px;border-radius:10px;margin:10px 0}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #333}}th{{background:#0f3460}}</style></head><body>
<h1>Kick Stake Drops Bot v11</h1>
<div class="c"><h2>Status</h2><p>Polls: {state.get('polls',0)}</p><p>Last: {state.get('last_poll','never')}</p></div>
<div class="c"><h2>Round-Robin Watcher</h2><p style='color:{rr_color};font-size:1.2em'><b>{rr_status}</b></p><p>Streamer: {rr_streamer}</p><p>Watch Time: {th}h {tm}m</p><p>Streams: {rr_watched}</p><p>Claimed: {rr_claimed}</p></div>
<div class="c"><h2>Users ({active}/{len(subs)})</h2><table><tr><th>ID</th><th>Status</th><th>Joined</th></tr>{rows}</table></div>
<div class="c"><h2>Drops ({len(known)})</h2><table><tr><th>Name</th><th>Status</th></tr>{drops}</table></div></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())
    def log_message(self, *a): pass

# ---- Commands ----
def handle_command(cmd, chat_id, text=""):
    # AUTO-ADD: any user who sends ANY command gets added as subscriber
    # This fixes the bug where users had to /start again after deploy
    is_new = add_sub(chat_id)

    if cmd in ("/start", "/help"):
        if cmd == "/start" and is_new:
            tg_send_admin(f"NEW USER: {chat_id}\nTotal: {len(get_active_subs())}")
        tg_send(
            "<b>Kick Stake Drops Bot v11</b>\n\n"
            "<b>DROP COMMANDS:</b>\n"
            "/all - Show ALL campaigns on Kick\n"
            "/stake - Show only Stake.com campaigns\n"
            "/live - Show live Stake-related streamers\n"
            "/history - Show past drop history\n"
            "/status - Bot status (polls, drops, subs)\n\n"
            "<b>ROUND-ROBIN AUTO-WATCHER:</b>\n"
            "/watchround - Start watching all Stake streams\n"
            "/watchround 10 - Watch 10 min per streamer\n"
            "/watchroundstop - Stop auto-watching\n"
            "/watchroundstatus - See current watch progress\n"
            "/watchtest &lt;user&gt; - Watch a live streamer for 5 min\n\n"
            "<b>AUTO-CLAIM:</b>\n"
            "/autoclaim on - Auto-claim rewards when ready\n"
            "/autoclaim off - Disable auto-claim\n"
            "/autoclaim - Check auto-claim status\n\n"
            "<b>CONFIG:</b>\n"
            "/setcookie - Update Kick session cookie\n"
            "/testprogress - Test progress API\n"
            "/testclaim - Test claim API\n"
            "/testws - Test WebSocket connection\n"
            "/stop - Unsubscribe from notifications\n"
            "/help - This message\n\n"
            "<b>How it works:</b>\n"
            "Bot polls Kick every 5s for Stake drops.\n"
            "When a new drop appears -> instant TG alert.\n"
            "Round-Robin watches each streamer for X min,\n"
            "then auto-switches to next. Claims rewards\n"
            "when watch time requirement is met.\n\n"
            "v11: Bug fixes, auto-subscribe, debug commands\n"
            "Use /watchtest <user> to watch a live stream.",
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
                ch = s.get("channel", {})
                user = s.get("broadcaster_user", {})
                cat = s.get("category", {})
                username = user.get("username", "")
                title = s.get("title", "")
                viewers = s.get("viewer_count", 0)
                cat_name = cat.get("name", "") if isinstance(cat, dict) else ""
                combined = (username + title + cat_name).lower()
                if any(k in combined for k in ["stake", "casino", "slots", "gamble", "stake.com"]):
                    live_stake.append({"username": username, "viewers": viewers, "title": title[:60], "category": cat_name})
            if not live_stake:
                tg_send(f"No Stake streams live.\nTotal streams: {len(streams)}", chat_id=chat_id)
            else:
                msg = f"<b>Live Stake Streams ({len(live_stake)}):</b>\n\n"
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
        tg_send(f"Polls: {state.get('polls',0)}\nDrops: {len(state.get('known',{}))}\nSubs: {len(get_active_subs())}\nHistory: {len(load_history())}", chat_id=chat_id)

    elif cmd == "/testprogress":
        tg_send("Testing progress API...", chat_id=chat_id)
        progress = fetch_progress()
        if progress:
            msg = f"<b>Progress API OK!</b>\n\nFound {len(progress)} items:\n\n"
            for item in progress[:5]:
                cid = item.get("campaign_id", "?")[:16]
                rewards = item.get("rewards", [])
                msg += f"Campaign: {cid}...\n"
                for r in rewards[:3]:
                    rid = r.get("reward_id", "?")[:16]
                    req = r.get("required_units", 0)
                    cur = r.get("progress", 0)
                    claimed = r.get("claimed", False)
                    msg += f"  Reward: {rid}... | {cur}/{req}s | Claimed: {claimed}\n"
            tg_send(msg, chat_id=chat_id)
        else:
            tg_send("Progress API returned empty or error.\nCheck logs.", chat_id=chat_id)

    elif cmd == "/testclaim":
        parts = text.split()
        if len(parts) < 3:
            tg_send("Usage: /testclaim &lt;campaign_id&gt; &lt;reward_id&gt;", chat_id=chat_id)
            return
        campaign_id = parts[1]
        reward_id = parts[2]
        tg_send(f"Testing claim: {campaign_id} / {reward_id}...", chat_id=chat_id)
        result = claim_reward(campaign_id, reward_id)
        if result:
            tg_send(f"<b>Claim response:</b>\n<pre>{json.dumps(result, indent=2)[:1000]}</pre>", chat_id=chat_id)
        else:
            tg_send("Claim failed. Check logs.", chat_id=chat_id)

    elif cmd == "/testws":
        parts = text.split()
        if len(parts) < 2:
            tg_send("Usage: /testws &lt;username&gt;\nExample: /testws stake", chat_id=chat_id)
            return
        username = parts[1].strip("@")
        tg_send(f"<b>Testing WS for @{username}...</b>", chat_id=chat_id)
        try:
            info = get_channel_info(username)
            if not info:
                tg_send(f"Channel @{username} not found.", chat_id=chat_id)
                return
            if not info.get("is_live"):
                tg_send(f"@{username} is OFFLINE.\nchannel_id: {info['channel_id']}\nlivestream_id: {info.get('livestream_id')}", chat_id=chat_id)
                return
            # Test WS token
            ws_token = get_ws_token(get_cookie())
            if not ws_token:
                tg_send("Failed to get WS token. Cookie may be expired.", chat_id=chat_id)
                return
            channel_id = info["channel_id"]
            livestream_id = info.get("livestream_id")
            tg_send(f"Channel ID: {channel_id}\nLivestream ID: {livestream_id}\nWS Token: {ws_token[:20]}...\n\nConnecting...", chat_id=chat_id)
            # Try connecting
            async def test_ws():
                ws_url = WS_URL_TEMPLATE.format(token=ws_token)
                headers = dict(BASE_HEADERS)
                async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10) as ws:
                    handshake = json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}})
                    await ws.send(handshake)
                    try:
                        resp = await asyncio.wait_for(ws.recv(), timeout=5)
                        log(f"[TEST-WS] Handshake resp: {resp[:200]}")
                    except asyncio.TimeoutError:
                        log("[TEST-WS] Handshake timeout")
                    await send_user_event(ws, channel_id, livestream_id)
                    log("[TEST-WS] user_event sent")
                    return True
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(test_ws())
                tg_send(f"<b>WS TEST OK!</b>\nConnected to @{username} and sent user_event.", chat_id=chat_id)
            except Exception as e:
                tg_send(f"<b>WS TEST FAILED!</b>\nError: {e}", chat_id=chat_id)
            finally:
                loop.close()
        except Exception as e:
            tg_send(f"Error: {e}", chat_id=chat_id)

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
            status = toggle_auto_claim(True)
            tg_send(f"<b>Auto-Claim: {status}</b>\n\nNow auto-watches and claims new drops.", chat_id=chat_id)
        elif len(parts) > 1 and parts[1].lower() in ("off", "0", "no"):
            status = toggle_auto_claim(False)
            tg_send(f"<b>Auto-Claim: {status}</b>", chat_id=chat_id)
        else:
            status = get_auto_claim_status()
            tg_send(f"<b>Auto-Claim: {status}</b>\n\nUsage: /autoclaim on | /autoclaim off\n\nWhen ON, bot auto-watches and claims new Stake drops.", chat_id=chat_id)

    elif cmd == "/watchround":
        parts = text.split()
        minutes = 7
        if len(parts) > 1 and parts[1].isdigit():
            minutes = max(3, min(30, int(parts[1])))
        success, msg = rr_watcher.start(minutes_per_streamer=minutes)
        tg_send(msg, chat_id=chat_id)
        if success:
            tg_send_admin(f"Round-Robin started by {chat_id} ({minutes} min/stream)")

    elif cmd == "/watchtest":
        parts = text.split()
        if len(parts) < 2:
            tg_send("Usage: /watchtest &lt;username&gt;\nExample: /watchtest stake", chat_id=chat_id)
            return
        username = parts[1].strip("@")
        tg_send(f"<b>WATCH TEST:</b> Checking @{username}...", chat_id=chat_id)
        try:
            info = get_channel_info(username)
            if not info:
                tg_send(f"Channel @{username} not found.", chat_id=chat_id)
                return
            if not info.get("is_live"):
                tg_send(f"@{username} is OFFLINE.\nchannel_id: {info['channel_id']}\nUse /watchtest when they go live.", chat_id=chat_id)
                return
            channel_id = info["channel_id"]
            livestream_id = info.get("livestream_id")
            tg_send(f"@{username} is LIVE!\nchannel_id: {channel_id}\nlivestream_id: {livestream_id}\n\nConnecting via WebSocket...", chat_id=chat_id)
            # Start watching in a thread
            def _do_watch():
                success = _watch_and_claim(username, channel_id, livestream_id, None, minutes=5)
                if success:
                    tg_send(f"<b>WATCH TEST DONE:</b> Watched @{username} for 5 min.\nChecking rewards...", chat_id=chat_id)
                    _check_and_claim_sync()
                else:
                    tg_send(f"<b>WATCH TEST FAILED</b> for @{username}.\nCheck logs.", chat_id=chat_id)
            threading.Thread(target=_do_watch, daemon=True).start()
        except Exception as e:
            tg_send(f"Error: {e}", chat_id=chat_id)

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
                tg_send(f"<b>NEW STAKE DROP!</b>\n\n<b>{name}</b>\nStatus: {status}\nChannels: {', '.join(ch_names)}\nRewards: {', '.join(rew_names[:5])}\nExpires in: {countdown}\n\n<a href='https://kick.com/drops/all-campaigns'>Open Drops</a>")
                log(f"NEW: {name} ({status})")
                if auto_claim_enabled:
                    threading.Thread(target=auto_claim_new_drop, args=(c,), daemon=True).start()
            elif known[cid].get("status") != status:
                old_status = known[cid]["status"]
                known[cid]["status"] = status
                add_to_history(c, f"{old_status}->{status}")
                if status == "active":
                    countdown = fmt_countdown(c.get("end_at", ""))
                    tg_send(f"<b>STAKE DROP LIVE!</b>\n\n<b>{name}</b>\nExpires in: {countdown}\n\n<a href='https://kick.com/drops/all-campaigns'>OPEN NOW</a>")
                    log(f"LIVE: {name}")
                    if auto_claim_enabled:
                        threading.Thread(target=auto_claim_new_drop, args=(c,), daemon=True).start()
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
    log("KICK STAKE DROPS BOT v11 - BUG FIXES")
    log("=" * 50)
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashboardHandler).serve_forever(), daemon=True).start()
    log(f"Dashboard: port {DASHBOARD_PORT}")
    threading.Thread(target=poller, daemon=True).start()
    try:
        threading.Thread(target=session_keeper, daemon=True).start()
    except Exception as e:
        log(f"Keeper not started (playwright?): {e}")
    tg_send_admin("<b>Bot v11 Started!</b>\n\nBug fixes: watchround, auto-subscribe on any command.\nCommands: /watchround /watchtest /testws")
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
