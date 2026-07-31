"""
Kick Stake Drops Bot v9 (Round-Robin Auto-Watcher)
- Cookie auto-refresh via Playwright (every 30 min)
- Multi-user with admin alerts
- Web dashboard
- 24/7 polling
- NEW: Round-robin mode - watch 5-10 min per streamer then switch
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
    if extra_headers: headers.update(extra_headers)
    req = urllib.request.Request(url)
    for k, v in headers.items(): req.add_header(k, v)
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read().decode())

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

def fetch_progress():
    try:
        cookie = get_cookie()
        data = kick_request(PROGRESS_API, extra_headers={"Authorization": f"Bearer {cookie}"})
        return data.get("data", []) if isinstance(data, dict) else data
    except Exception as e:
        log(f"Progress fetch error: {e}")
        return []

def claim_reward(campaign_id, reward_id):
    try:
        cookie = get_cookie()
        headers = dict(BASE_HEADERS)
        headers["Cookie"] = "session=" + cookie
        headers["Authorization"] = f"Bearer {cookie}"
        headers["Content-Type"] = "application/json"
        body = json.dumps({"campaign_id": campaign_id, "reward_id": reward_id}).encode()
        req = urllib.request.Request(CLAIM_API, data=body, method="POST")
        for k, v in headers.items(): req.add_header(k, v)
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode())
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

def is_stake_drop(c):
    connect = c.get("connect_url", "").lower()
    name = c.get("name", "").lower()
    return "stake.com" in connect or "stake.com" in name

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
    """Async WebSocket watch loop"""
    async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
        # Handshake
        handshake = json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}})
        await ws.send(handshake)
        
        # Initial user_event
        await send_user_event(ws, channel_id, livestream_id)
        log(f"[AUTO-CLAIM] Connected to {username}")
        
        start_time = time.time()
        last_user_event = time.time()
        last_ping = time.time()
        event_count = 1
        
        while True:
            now = time.time()
            elapsed = now - start_time
            
            # Time's up
            if elapsed >= target_seconds:
                log(f"[AUTO-CLAIM] Done watching {username} ({int(elapsed)}s)")
                return
            
            # Ping every 20s
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
            
            # user_event every 60s
            if now - last_user_event >= 60:
                await send_user_event(ws, channel_id, livestream_id)
                event_count += 1
                last_user_event = now
                remaining = int(target_seconds - elapsed)
                log(f"[AUTO-CLAIM] user_event #{event_count} for {username} ({remaining}s left)")
            
            await asyncio.sleep(1)

def _check_and_claim_sync():
    """Check progress and claim rewards synchronously"""
    try:
        progress = fetch_progress()
        if not progress:
            return
        for item in progress:
            campaign_id = item.get("campaign_id") or item.get("id")
            for r in item.get("rewards", []):
                reward_id = r.get("reward_id") or r.get("id")
                claimed = r.get("claimed", False)
                required = r.get("required_units", 0)
                current = r.get("progress", 0)
                if not claimed and required > 0 and current >= required:
                    log(f"[AUTO-CLAIM] Claiming reward: {reward_id}")
                    result = claim_reward(campaign_id, reward_id)
                    if result:
                        tg_send(f"<b>REWARD CLAIMED!</b>\n\nCampaign: {campaign_id[:16]}...\nReward: {reward_id[:16]}...")
                        log(f"[AUTO-CLAIM] Claimed: {reward_id}")
    except Exception as e:
        log(f"[AUTO-CLAIM] Claim check error: {e}")

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
        self.state["active"] = True
        self.state["started_at"] = datetime.now().isoformat()
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
        total = self.state.get("total_watch_time", 0)
        th, tm = total // 3600, (total % 3600) // 60
        claimed = self.state.get("rewards_claimed", 0)
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
        finally:
            loop.close()

    async def _main_loop(self):
        log("Round-Robin watcher started")
        while not self.stop_event.is_set():
            try:
                campaigns, _ = await asyncio.get_event_loop().run_in_executor(None, fetch_campaigns)
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
                    info = await asyncio.get_event_loop().run_in_executor(None, get_channel_info, username)
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
                log(f"RR main loop error: {e}"); await asyncio.sleep(15)
        log("Round-Robin watcher stopped")

    async def _watch_stream(self, username, channel_id, livestream_id):
        target_seconds = self.min_per_streamer * 60
        for attempt in range(3):
            if self.stop_event.is_set(): return False
            try:
                ws_token = await asyncio.get_event_loop().run_in_executor(None, get_ws_token, get_cookie())
                if not ws_token: log(f"No WS token for {username}"); return False
                ws_url = WS_URL_TEMPLATE.format(token=ws_token)
                headers = {k: v for k, v in BASE_HEADERS.items()}
                async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
                    handshake = json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}})
                    await ws.send(handshake)
                    await send_user_event(ws, channel_id, livestream_id)
                    log(f"Connected to {username}")
                    start_time = time.time()
                    last_user_event = time.time()
                    last_ping = time.time()
                    event_count = 1
                    while not self.stop_event.is_set():
                        now = time.time()
                        elapsed = now - start_time
                        if elapsed >= target_seconds:
                            log(f"Time up for {username} ({int(elapsed)}s), switching..."); return True
                        if now - last_ping >= 20:
                            try:
                                await ws.send(json.dumps({"type": "ping"}))
                                last_ping = now
                                try: await asyncio.wait_for(ws.recv(), timeout=3)
                                except asyncio.TimeoutError: pass
                            except Exception: pass
                        if now - last_user_event >= 60:
                            await send_user_event(ws, channel_id, livestream_id)
                            event_count += 1
                            last_user_event = now
                            with self._lock:
                                self.state["total_watch_time"] = self.state.get("total_watch_time", 0) + 60
                            self._save()
                            remaining = int(target_seconds - elapsed)
                            log(f"user_event #{event_count} for {username} ({remaining}s left)")
                        if event_count % 5 == 0:
                            info = await asyncio.get_event_loop().run_in_executor(None, get_channel_info, username)
                            if not info or not info.get("is_live"):
                                log(f"{username} went offline"); return True
                        await asyncio.sleep(1)
            except Exception as e:
                log(f"Watch error for {username}: {e}")
                if attempt < 2: await asyncio.sleep(5 * (2 ** attempt))
                else: return False
        return False

    async def _check_and_claim(self):
        try:
            progress = await asyncio.get_event_loop().run_in_executor(None, fetch_progress)
            if not progress: return
            for item in progress:
                campaign_id = item.get("campaign_id") or item.get("id")
                for r in item.get("rewards", []):
                    reward_id = r.get("reward_id") or r.get("id")
                    claimed = r.get("claimed", False)
                    required = r.get("required_units", 0)
                    current = r.get("progress", 0)
                    if not claimed and required > 0 and current >= required:
                        log(f"Claiming reward: {reward_id}")
                        result = await asyncio.get_event_loop().run_in_executor(None, claim_reward, campaign_id, reward_id)
                        if result:
                            with self._lock:
                                self.state["rewards_claimed"] = self.state.get("rewards_claimed", 0) + 1
                            self._save()
                            tg_send(f"<b>REWARD CLAIMED!</b>\n\nCampaign: {campaign_id[:16]}...\nReward: {reward_id[:16]}...")
                            log(f"Reward claimed: {reward_id}")
        except Exception as e:
            log(f"Claim check error: {e}")

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
        html = f"""<!DOCTYPE html><html><head><title>Kick Drops v9</title>
<meta http-equiv="refresh" content="30">
<style>body{{font-family:Arial;background:#1a1a2e;color:#eee;padding:20px}}h1{{color:#e94560}}.c{{background:#16213e;padding:20px;border-radius:10px;margin:10px 0}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #333}}th{{background:#0f3460}}</style></head><body>
<h1>Kick Stake Drops Bot v9</h1>
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
    if cmd == "/start":
        is_new = add_sub(chat_id)
        if is_new:
            tg_send_admin(f"NEW USER: {chat_id}\nTotal: {len(get_active_subs())}")
        tg_send("<b>Kick Stake Drops Bot v9</b>\n\n<b>Drops:</b> /all /stake /status\n<b>Auto-Claim:</b> /autoclaim on/off\n<b>Round-Robin:</b> /watchround [min] /watchroundstop /watchroundstatus\n<b>Config:</b> /setcookie /stop", chat_id=chat_id)

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

    elif cmd == "/status":
        state = load_state()
        tg_send(f"Polls: {state.get('polls',0)}\nDrops: {len(state.get('known',{}))}\nSubs: {len(get_active_subs())}", chat_id=chat_id)

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
                tg_send(f"<b>NEW STAKE DROP!</b>\n\n<b>{name}</b>\nStatus: {status}\nChannels: {', '.join(ch_names)}\nRewards: {', '.join(rew_names[:5])}\n\n<a href='https://kick.com/drops/all-campaigns'>Open Drops</a>")
                log(f"NEW: {name} ({status})")
                # Auto-claim: watch this drop's streamers immediately
                if auto_claim_enabled:
                    threading.Thread(target=auto_claim_new_drop, args=(c,), daemon=True).start()
            elif known[cid].get("status") != status:
                known[cid]["status"] = status
                if status == "active":
                    tg_send(f"<b>STAKE DROP LIVE!</b>\n\n<b>{name}</b>\n\n<a href='https://kick.com/drops/all-campaigns'>OPEN NOW</a>")
                    log(f"LIVE: {name}")
                    # Auto-claim: stream just went live, watch it
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
    log("KICK STAKE DROPS BOT v9 - ROUND-ROBIN")
    log("=" * 50)
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashboardHandler).serve_forever(), daemon=True).start()
    log(f"Dashboard: port {DASHBOARD_PORT}")
    threading.Thread(target=poller, daemon=True).start()
    try:
        threading.Thread(target=session_keeper, daemon=True).start()
    except Exception as e:
        log(f"Keeper not started (playwright?): {e}")
    tg_send_admin("<b>Bot v9 Started!</b>\n\nRound-Robin auto-watcher enabled.\nCommands: /watchround /watchroundstop /watchroundstatus")
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
