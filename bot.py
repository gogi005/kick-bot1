"""
Kick Stake Drops Bot v8 (Render Ready) - with Auto-Watcher
- No Playwright dependency
- Cookie auto-read from file
- Multi-user + admin alerts
- 24/7 polling
- NEW: Real WebSocket-based stream auto-watcher
"""
import urllib.request, json, time, os, threading, random
import asyncio
import websockets
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============ CONFIG ============
TG_TOKEN = os.environ.get("TG_TOKEN", "8860462138:AAGkQQF1c-MyTfD3-3WluZNMarcT7HLj4dg")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8182391939"))
COOKIE = os.environ.get("KICK_COOKIE", "365875656%7C3qeqtSAxow2mU2adRmgluNijBSImYcgoLFRIZ2v9")
KICK_CLIENT_TOKEN = os.environ.get("KICK_CLIENT_TOKEN", "e1393935a959b4020a4491574f6490129f678acdaa92760471263db43487f823")
POLL_INTERVAL = 5
DROPS_API = "https://web.kick.com/api/v1/drops/campaigns"
PROGRESS_API = "https://web.kick.com/api/v1/drops/progress"
CLAIM_API = "https://web.kick.com/api/v1/drops/claim"
CHANNEL_API = "https://kick.com/api/v2/channels/{username}"
LIVESTREAMS_API = "https://web.kick.com/api/v1/livestreams"
WS_TOKEN_API = "https://websockets.kick.com/viewer/v1/token"
WS_URL_TEMPLATE = "wss://websockets.kick.com/viewer/v1/connect?token={token}"
STATE_FILE = "tg_bot_state.json"
SUBS_FILE = "tg_subscribers.json"
WATCHER_FILE = "tg_watcher_state.json"
DASHBOARD_PORT = int(os.environ.get("PORT", "8080"))
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

# ---- Subscribers ----
def load_subs():
    if os.path.exists(SUBS_FILE):
        try:
            with open(SUBS_FILE) as f: return json.load(f)
        except: pass
    return {}

def save_subs(subs):
    with open(SUBS_FILE, "w") as f: json.dump(subs, f, indent=2)

def add_sub(cid):
    subs = load_subs()
    sid = str(cid)
    is_new = sid not in subs or not subs[sid].get("active", True)
    subs[sid] = {"added_at": datetime.now().isoformat(), "active": True}
    save_subs(subs)
    return is_new

def remove_sub(cid):
    subs = load_subs()
    sid = str(cid)
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
    if COOKIE:
        headers["Cookie"] = "session=" + COOKIE
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url)
    for k, v in headers.items():
        req.add_header(k, v)
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

def fetch_progress():
    try:
        data = kick_request(PROGRESS_API, extra_headers={"Authorization": f"Bearer {COOKIE}"})
        return data.get("data", []) if isinstance(data, dict) else data
    except Exception as e:
        log(f"Progress fetch error: {e}")
        return []

def claim_reward(campaign_id, reward_id):
    try:
        headers = dict(BASE_HEADERS)
        headers["Cookie"] = "session=" + COOKIE
        headers["Authorization"] = f"Bearer {COOKIE}"
        headers["Content-Type"] = "application/json"
        body = json.dumps({"campaign_id": campaign_id, "reward_id": reward_id}).encode()
        req = urllib.request.Request(CLAIM_API, data=body, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode())
    except Exception as e:
        log(f"Claim error: {e}")
        return None

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

def load_watcher_state():
    if os.path.exists(WATCHER_FILE):
        try:
            with open(WATCHER_FILE) as f: return json.load(f)
        except: pass
    return {
        "active": False, "current_streamer": None, "watch_time": 0,
        "total_watch_time": 0, "streams_watched": 0, "rewards_claimed": 0,
        "started_at": None, "last_event": None,
    }

def save_watcher_state(ws):
    with open(WATCHER_FILE, "w") as f: json.dump(ws, f, indent=2, default=str)

# ---- Dashboard ----
class DashHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        subs = load_subs()
        state = load_state()
        watcher = load_watcher_state()
        known = state.get("known", {})
        active = sum(1 for d in subs.values() if d.get("active", True))
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
        ws = "ACTIVE" if watcher.get("active") else "STOPPED"
        wc = "#4CAF50" if watcher.get("active") else "#f44336"
        current = watcher.get("current_streamer", "None")
        wtime = watcher.get("total_watch_time", 0)
        wh, wm = wtime // 3600, (wtime % 3600) // 60
        html = f"""<!DOCTYPE html><html><head><title>Kick Drops</title>
<meta http-equiv="refresh" content="30">
<style>body{{font-family:Arial;background:#1a1a2e;color:#eee;padding:20px}}h1{{color:#e94560}}.c{{background:#16213e;padding:20px;border-radius:10px;margin:10px 0}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #333}}th{{background:#0f3460}}</style></head><body>
<h1>Kick Stake Drops Bot v8</h1>
<div class="c"><h2>Status</h2><p>Polls: {state.get('polls',0)}</p><p>Last: {state.get('last_poll','never')}</p></div>
<div class="c"><h2>Auto-Watcher</h2><p style='color:{wc};font-size:1.2em'><b>{ws}</b></p><p>Streamer: {current}</p><p>Watch Time: {wh}h {wm}m</p><p>Streams: {watcher.get('streams_watched',0)}</p><p>Claimed: {watcher.get('rewards_claimed',0)}</p></div>
<div class="c"><h2>Users ({active}/{len(subs)})</h2><table><tr><th>ID</th><th>Status</th><th>Joined</th></tr>{rows}</table></div>
<div class="c"><h2>Drops ({len(known)})</h2><table><tr><th>Name</th><th>Status</th></tr>{drops}</table></div></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())
    def log_message(self, *a): pass

# ---- REAL WebSocket Stream Watcher ----
class StreamWatcher:
    def __init__(self):
        self.active = False
        self.stop_event = threading.Event()
        self.current_streamer = None
        self.watch_thread = None
        self.state = load_watcher_state()
        self._lock = threading.Lock()

    def _save(self):
        with self._lock:
            save_watcher_state(self.state)

    def start(self, category_id=None, target_minutes=0):
        if self.active:
            return False, "Already watching!"
        self.stop_event.clear()
        self.active = True
        self.state["active"] = True
        self.state["started_at"] = datetime.now().isoformat()
        self._save()
        self.watch_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self.watch_thread.start()
        return True, "Auto-watcher started! (Real WebSocket)"

    def stop(self):
        if not self.active:
            return False, "Not watching anything!"
        self.stop_event.set()
        self.active = False
        self.state["active"] = False
        self.state["current_streamer"] = None
        self._save()
        return True, "Auto-watcher stopped!"

    def _run_async_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main_loop())
        except Exception as e:
            log(f"Async loop error: {e}")
        finally:
            loop.close()

    async def _main_loop(self):
        log("Watcher async loop started")
        while not self.stop_event.is_set():
            try:
                campaigns, ok = await asyncio.get_event_loop().run_in_executor(None, fetch_campaigns)
                if not campaigns:
                    await asyncio.sleep(30)
                    continue

                stake_campaigns = [c for c in campaigns if is_stake_drop(c) and c.get("status") == "active" and c.get("channels")]
                if not stake_campaigns:
                    await asyncio.sleep(30)
                    continue

                for campaign in stake_campaigns:
                    if self.stop_event.is_set(): break
                    for ch in campaign.get("channels", []):
                        if self.stop_event.is_set(): break
                        username = ch.get("slug") or ch.get("user", {}).get("username")
                        if not username: continue
                        info = await asyncio.get_event_loop().run_in_executor(None, get_channel_info, username)
                        if not info or not info.get("is_live"): continue

                        log(f"Watching: {username} ({campaign.get('name', '?')})")
                        self.state["current_streamer"] = username
                        self._save()
                        await self._watch_stream_ws(username, info["channel_id"], info.get("livestream_id"), campaign)
                        if self.stop_event.is_set(): break

                if not self.stop_event.is_set():
                    await asyncio.sleep(10)
            except Exception as e:
                log(f"Main loop error: {e}")
                await asyncio.sleep(15)
        log("Watcher async loop ended")

    async def _watch_stream_ws(self, username, channel_id, livestream_id, campaign):
        """Actually connect to Kick WebSocket and send real user_events with reconnection"""
        max_retries = 5
        retry_delay = 5
        
        for attempt in range(max_retries):
            if self.stop_event.is_set():
                break
                
            try:
                ws_token = await asyncio.get_event_loop().run_in_executor(None, get_ws_token, COOKIE)
                if not ws_token:
                    log(f"Failed to get WS token for {username}")
                    return

                ws_url = WS_URL_TEMPLATE.format(token=ws_token)
                headers = {k: v for k, v in BASE_HEADERS.items()}

                log(f"Connecting WebSocket for {username} (attempt {attempt + 1}/{max_retries})...")

                async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
                    # Step 1: Send channel_handshake
                    handshake = json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}})
                    await ws.send(handshake)
                    log(f"Handshake sent for {username}")

                    # Step 2: Send initial user_event
                    await send_user_event(ws, channel_id, livestream_id)
                    log(f"Initial user_event sent for {username}")

                    start_time = time.time()
                    last_user_event = time.time()
                    last_ping = time.time()
                    event_count = 1
                    watch_success = True

                    # Step 3: Main WS loop - ping + user_event
                    while not self.stop_event.is_set():
                        try:
                            now = time.time()

                            # Send ping every ~20 seconds
                            if now - last_ping >= 20:
                                await ws.send(json.dumps({"type": "ping"}))
                                last_ping = now
                                # Wait for pong response
                                try:
                                    pong = await asyncio.wait_for(ws.recv(), timeout=5)
                                except asyncio.TimeoutError:
                                    pass

                            # Send user_event every 60 seconds
                            if now - last_user_event >= 60:
                                await send_user_event(ws, channel_id, livestream_id)
                                event_count += 1
                                last_user_event = now
                                log(f"user_event #{event_count} sent for {username}")

                            # Update local state
                            elapsed = int(now - start_time)
                            with self._lock:
                                self.state["watch_time"] = elapsed
                                self.state["total_watch_time"] = self.state.get("total_watch_time", 0) + 1
                                self.state["last_event"] = datetime.now().isoformat()
                                self._save()

                            # Check if stream went offline
                            if event_count % 5 == 0:
                                info = await asyncio.get_event_loop().run_in_executor(None, get_channel_info, username)
                                if not info or not info.get("is_live"):
                                    log(f"Stream {username} went offline, switching...")
                                    watch_success = True  # Expected behavior, not failure
                                    break

                            # Check target time
                            target = self.state.get("target_minutes", 0)
                            if target > 0 and elapsed >= target * 60:
                                log(f"Target time reached for {username}")
                                break

                            # Check and claim rewards periodically
                            if event_count % 5 == 0:
                                await self._check_and_claim()

                            # Small delay to prevent tight loop
                            await asyncio.sleep(1)

                        except websockets.ConnectionClosed:
                            log(f"WS connection closed for {username}, reconnecting...")
                            watch_success = False
                            break
                        except asyncio.CancelledError:
                            log(f"Watcher cancelled for {username}")
                            watch_success = False
                            break
                        except Exception as e:
                            log(f"WS loop error: {e}")
                            watch_success = False
                            break

                # Increment streams_watched only on successful watch
                if watch_success and event_count > 1:
                    with self._lock:
                        self.state["streams_watched"] = self.state.get("streams_watched", 0) + 1
                        self._save()
                
                # If we broke out successfully, don't retry
                if watch_success or self.stop_event.is_set():
                    break
                    
            except asyncio.CancelledError:
                log(f"Watcher cancelled for {username}")
                break
            except Exception as e:
                log(f"WebSocket watch error for {username}: {e}")
                
            # Exponential backoff retry
            if attempt < max_retries - 1 and not self.stop_event.is_set():
                wait_time = retry_delay * (2 ** attempt)
                log(f"Retrying {username} in {wait_time}s...")
                await asyncio.sleep(wait_time)

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

    def get_status(self):
        with self._lock:
            self.state = load_watcher_state()
            return dict(self.state)

watcher = StreamWatcher()

# ---- Shared WebSocket Helpers ----
async def send_user_event(ws, channel_id, livestream_id):
    """Send the actual user_event that Kick tracks for watch time"""
    event = {
        "type": "user_event",
        "data": {
            "message": {
                "name": "tracking.user.watch.livestream",
                "channel_id": channel_id,
                "livestream_id": int(livestream_id) if livestream_id else int(channel_id),
            }
        }
    }
    await ws.send(json.dumps(event))

# ---- Manual Stream Tester ----
class ManualStreamTester:
    """Test watching a specific streamer via WebSocket"""
    def __init__(self):
        self.active = False
        self.stop_event = threading.Event()
        self.current_streamer = None
        self.watch_thread = None
        self._lock = threading.Lock()
        self.watch_data = {"events_sent": 0, "start_time": None, "status": "idle"}

    def start(self, username, chat_id):
        """Start watching a specific streamer"""
        if self.active:
            return False, "Already watching a streamer! Use /watchteststop first."
        
        # Check if streamer exists and is live
        info = get_channel_info(username)
        if not info:
            return False, f"Could not find streamer: {username}"
        if not info.get("is_live"):
            return False, f"<b>{username}</b> is OFFLINE right now. Try when they're live!"
        
        self.stop_event.clear()
        self.active = True
        self.current_streamer = username
        self.watch_data = {"events_sent": 0, "start_time": time.time(), "status": "connecting"}
        
        self.watch_thread = threading.Thread(target=self._watch_loop, args=(username, info, chat_id), daemon=True)
        self.watch_thread.start()
        
        return True, f"<b>WATCHING: {username}</b>\n\nChannel ID: {info['channel_id']}\nLivestream ID: {info.get('livestream_id')}\n\nWebSocket connecting... Check /watchteststatus for updates."

    def stop(self):
        """Stop watching"""
        if not self.active:
            return False, "Not watching anything!"
        self.stop_event.set()
        self.active = False
        self.watch_data["status"] = "stopped"
        return True, f"<b>STOPPED watching {self.current_streamer}</b>\nEvents sent: {self.watch_data['events_sent']}"

    def get_status(self):
        """Get current test status"""
        if not self.active:
            return "<b>TEST MODE: IDLE</b>\n\nUse /watchtest <username> to start."
        
        elapsed = int(time.time() - self.watch_data.get("start_time", time.time()))
        mins, secs = divmod(elapsed, 60)
        
        return (f"<b>TEST MODE: ACTIVE</b>\n\n"
                f"Streamer: {self.current_streamer}\n"
                f"Status: {self.watch_data['status']}\n"
                f"Events Sent: {self.watch_data['events_sent']}\n"
                f"Watch Time: {mins}m {secs}s")

    def _watch_loop(self, username, info, chat_id):
        """WebSocket watch loop for testing"""
        channel_id = info["channel_id"]
        livestream_id = info.get("livestream_id")
        
        log(f"[TEST] Starting WebSocket watch for {username}")
        
        # Run async loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_watch(username, channel_id, livestream_id, chat_id))
        except Exception as e:
            log(f"[TEST] Error: {e}")
            tg_send(f"<b>TEST ERROR:</b> {e}", chat_id=chat_id)
        finally:
            loop.close()
            self.active = False
            self.watch_data["status"] = "ended"
            log(f"[TEST] Watch ended for {username}. Events sent: {self.watch_data['events_sent']}")

    async def _async_watch(self, username, channel_id, livestream_id, chat_id):
        """Async WebSocket watching with real-time Telegram updates"""
        max_retries = 3
        
        for attempt in range(max_retries):
            if self.stop_event.is_set():
                break
            
            try:
                # Get WS token
                ws_token = await asyncio.get_event_loop().run_in_executor(None, get_ws_token, COOKIE)
                if not ws_token:
                    tg_send(f"<b>ERROR:</b> Could not get WebSocket token. Cookie might be expired.", chat_id=chat_id)
                    return
                
                ws_url = WS_URL_TEMPLATE.format(token=ws_token)
                headers = {k: v for k, v in BASE_HEADERS.items()}
                
                tg_send(f"<b>CONNECTING...</b>\nAttempt {attempt + 1}/{max_retries}", chat_id=chat_id)
                
                async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10) as ws:
                    self.watch_data["status"] = "connected"
                    log(f"[TEST] WebSocket connected for {username}")
                    
                    # Send channel_handshake
                    handshake = json.dumps({"type": "channel_handshake", "data": {"message": {"channelId": channel_id}}})
                    await ws.send(handshake)
                    tg_send(f"<b>CONNECTED!</b>\n\nHandshake sent. Starting user_events...", chat_id=chat_id)
                    
                    # Send initial user_event
                    await send_user_event(ws, channel_id, livestream_id)
                    self.watch_data["events_sent"] += 1
                    self.watch_data["status"] = "watching"
                    
                    tg_send(f"<b>FIRST user_event SENT!</b>\n\nNow sending every 60 seconds...\nUse /watchteststatus to check.\nUse /watchteststop to stop.", chat_id=chat_id)
                    
                    last_user_event = time.time()
                    last_ping = time.time()
                    start_time = time.time()
                    
                    # Main loop
                    while not self.stop_event.is_set():
                        now = time.time()
                        
                        # Send ping every 20s
                        if now - last_ping >= 20:
                            try:
                                await ws.send(json.dumps({"type": "ping"}))
                                last_ping = now
                                try:
                                    await asyncio.wait_for(ws.recv(), timeout=3)
                                except asyncio.TimeoutError:
                                    pass
                            except Exception as e:
                                log(f"[TEST] Ping error: {e}")
                        
                        # Send user_event every 60s
                        if now - last_user_event >= 60:
                            await send_user_event(ws, channel_id, livestream_id)
                            self.watch_data["events_sent"] += 1
                            last_user_event = now
                            
                            elapsed = int(now - start_time)
                            mins, secs = divmod(elapsed, 60)
                            log(f"[TEST] user_event #{self.watch_data['events_sent']} sent ({mins}m {secs}s elapsed)")
                        
                        # Check if still live every 30s
                        if self.watch_data["events_sent"] % 3 == 0:
                            info = await asyncio.get_event_loop().run_in_executor(None, get_channel_info, username)
                            if not info or not info.get("is_live"):
                                tg_send(f"<b>STREAM ENDED:</b> {username} went offline.", chat_id=chat_id)
                                self.watch_data["status"] = "stream_ended"
                                break
                        
                        await asyncio.sleep(1)
                
                # If we exited normally and not stopped, might need reconnect
                if not self.stop_event.is_set():
                    tg_send(f"<b>CONNECTION LOST</b>\nReconnecting in 5s...", chat_id=chat_id)
                    await asyncio.sleep(5)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[TEST] Error: {e}")
                self.watch_data["status"] = f"error: {str(e)[:50]}"
                if attempt < max_retries - 1:
                    tg_send(f"<b>ERROR:</b> {e}\nRetrying in {5 * (2**attempt)}s...", chat_id=chat_id)
                    await asyncio.sleep(5 * (2 ** attempt))
                else:
                    tg_send(f"<b>FATAL ERROR:</b> {e}\n\nMax retries reached. Test ended.", chat_id=chat_id)
        
        # Final summary
        elapsed = int(time.time() - self.watch_data.get("start_time", time.time()))
        mins, secs = divmod(elapsed, 60)
        tg_send(f"<b>TEST COMPLETE</b>\n\nStreamer: {username}\nEvents Sent: {self.watch_data['events_sent']}\nDuration: {mins}m {secs}s\nStatus: {self.watch_data['status']}", chat_id=chat_id)

# Initialize tester
tester = ManualStreamTester()

# ---- Commands ----
def handle_command(cmd, chat_id, text=""):
    if cmd == "/start":
        is_new = add_sub(chat_id)
        if is_new:
            tg_send_admin(f"NEW USER: {chat_id}\nTotal: {len(get_active_subs())}")
        tg_send("<b>Kick Stake Drops Bot v8</b>\n\n/all /stake /status /setcookie\n/watch /watchstop /watchstatus\n/watchtest /watchteststop /watchteststatus\n/stop", chat_id=chat_id)
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
        global COOKIE
        COOKIE = parts[1].strip()
        tg_send("Cookie updated!", chat_id=chat_id)
        tg_send_admin(f"Cookie updated by {chat_id}")
    elif cmd == "/watch":
        parts = text.split()
        minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        success, msg = watcher.start(target_minutes=minutes)
        if success:
            tg_send(f"<b>AUTO-WATCHER STARTED!</b>\n\nMode: Real WebSocket\nTarget: {'Unlimited' if minutes == 0 else f'{minutes} min per stream'}\n\nWatching Stake streams via WS and claiming drops automatically.", chat_id=chat_id)
            tg_send_admin(f"Watcher started by {chat_id}")
        else:
            tg_send(msg, chat_id=chat_id)
    elif cmd == "/watchstop":
        success, msg = watcher.stop()
        if success:
            state = watcher.get_status()
            tg_send(f"<b>AUTO-WATCHER STOPPED!</b>\n\nTotal watch time: {state.get('total_watch_time', 0)} sec\nStreams: {state.get('streams_watched', 0)}\nClaimed: {state.get('rewards_claimed', 0)}", chat_id=chat_id)
        else:
            tg_send(msg, chat_id=chat_id)
    elif cmd == "/watchstatus":
        state = watcher.get_status()
        status = "ACTIVE" if state.get("active") else "STOPPED"
        current = state.get("current_streamer", "None")
        wtime = state.get("total_watch_time", 0)
        wh, wm = wtime // 3600, (wtime % 3600) // 60
        tg_send(f"<b>WATCHER STATUS</b>\n\nStatus: {status}\nStreamer: {current}\nWatch Time: {wh}h {wm}m\nStreams: {state.get('streams_watched', 0)}\nClaimed: {state.get('rewards_claimed', 0)}", chat_id=chat_id)

    # ---- Manual Test Commands ----
    elif cmd == "/watchtest":
        parts = text.split()
        if len(parts) < 2:
            tg_send("<b>USAGE:</b> /watchtest &lt;username&gt;\n\nExample: /watchtest ramee\n\nThis will test WebSocket watching on a specific streamer.", chat_id=chat_id)
            return
        username = parts[1].strip().lower()
        tg_send(f"<b>CHECKING:</b> Is {username} live?...", chat_id=chat_id)
        success, msg = tester.start(username, chat_id)
        tg_send(msg, chat_id=chat_id)
        if success:
            tg_send_admin(f"Test started: watching {username} by {chat_id}")

    elif cmd == "/watchteststop":
        success, msg = tester.stop()
        tg_send(msg, chat_id=chat_id)

    elif cmd == "/watchteststatus":
        status = tester.get_status()
        tg_send(status, chat_id=chat_id)

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
            elif known[cid].get("status") != status:
                known[cid]["status"] = status
                if status == "active":
                    tg_send(f"<b>STAKE DROP LIVE!</b>\n\n<b>{name}</b>\n\n<a href='https://kick.com/drops/all-campaigns'>OPEN NOW</a>")
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
    log("KICK STAKE DROPS BOT v8 - REAL WEBSOCKET")
    log("=" * 50)
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashHandler).serve_forever(), daemon=True).start()
    log(f"Dashboard: port {DASHBOARD_PORT}")
    threading.Thread(target=poller, daemon=True).start()
    tg_send_admin("<b>Bot v8 Started!</b>\n\nReal WebSocket auto-watcher enabled.\nCommands: /watch /watchstop /watchstatus")
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
