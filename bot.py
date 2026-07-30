"""
Kick Stake Drops Bot v7 (Render Ready)
- No Playwright dependency
- Cookie auto-read from file
- Multi-user + admin alerts
- 24/7 polling
"""
import urllib.request, json, time, os, threading, random
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============ CONFIG ============
TG_TOKEN = os.environ.get("TG_TOKEN", "8860462138:AAGkQQF1c-MyTfD3-3WluZNMarcT7HLj4dg")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8182391939"))
COOKIE = os.environ.get("KICK_COOKIE", "365875656%7C3qeqtSAxow2mU2adRmgluNijBSImYcgoLFRIZ2v9")
POLL_INTERVAL = 5
DROPS_API = "https://web.kick.com/api/v1/drops/campaigns"
STATE_FILE = "tg_bot_state.json"
SUBS_FILE = "tg_subscribers.json"
DASHBOARD_PORT = int(os.environ.get("PORT", "8080"))
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
def fetch_campaigns():
    req = urllib.request.Request(DROPS_API)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    req.add_header("Origin", "https://kick.com")
    req.add_header("Referer", "https://kick.com/")
    req.add_header("Cookie", "session=" + COOKIE)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode()).get("data", []), True
    except urllib.error.HTTPError as e:
        if e.code in (401, 403): return None, False
        return None, True
    except: return None, True

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

# ---- Dashboard ----
class DashHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        subs = load_subs()
        state = load_state()
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

        html = f"""<!DOCTYPE html><html><head><title>Kick Drops</title>
<meta http-equiv="refresh" content="30">
<style>body{{font-family:Arial;background:#1a1a2e;color:#eee;padding:20px}}h1{{color:#e94560}}.c{{background:#16213e;padding:20px;border-radius:10px;margin:10px 0}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #333}}th{{background:#0f3460}}</style></head><body>
<h1>Kick Stake Drops</h1>
<div class="c"><h2>Status</h2><p>Polls: {state.get('polls',0)}</p><p>Last: {state.get('last_poll','never')}</p></div>
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
        tg_send("<b>Kick Stake Drops Bot</b>\n\n/all /stake /status /setcookie /stop", chat_id=chat_id)

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
            connect = c.get("connect_url", "")
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
    log("KICK STAKE DROPS BOT v7")
    log("=" * 50)

    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashHandler).serve_forever(), daemon=True).start()
    log(f"Dashboard: port {DASHBOARD_PORT}")
    threading.Thread(target=poller, daemon=True).start()
    tg_send_admin("<b>Bot Started on Render!</b>")

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
