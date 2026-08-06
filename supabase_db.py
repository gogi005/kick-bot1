"""
Supabase Database Module for Kick Drops Bot
Replaces JSON file storage with persistent PostgreSQL database
All data survives Render deploys and restarts!
"""
import os
import json
import time
import threading
from datetime import datetime
from supabase import create_client, Client

# Supabase credentials from environment
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Initialize client
_supabase: Client = None
_lock = threading.Lock()

def get_client():
    """Get or create Supabase client (thread-safe)"""
    global _supabase
    if _supabase is None:
        with _lock:
            if _supabase is None:
                _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase

def _retry(func, max_retries=3, delay=1):
    """Retry function on Windows socket errors"""
    for i in range(max_retries):
        try:
            return func()
        except Exception as e:
            err_str = str(e).lower()
            if 'non-blocking' in err_str or 'winerror' in err_str or '10035' in err_str:
                if i < max_retries - 1:
                    time.sleep(delay)
                    continue
            raise

# ==================== WATCHLIST ====================
def load_watchlist():
    """Load watchlist from Supabase"""
    def _do():
        client = get_client()
        result = client.table("watchlist").select("*").execute()
        channels = [row["channel_name"] for row in result.data]
        added_by = {}
        for row in result.data:
            if row.get("added_by"):
                added_by[row["channel_name"]] = {
                    "by": row["added_by"],
                    "time": row.get("added_at", "")
                }
        return {"channels": channels, "added_by": added_by, "last_updated": None}
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] load_watchlist error: {e}")
        return {"channels": [], "added_by": {}, "last_updated": None}

def save_watchlist(watchlist):
    """Save watchlist to Supabase"""
    try:
        client = get_client()
        channels = watchlist.get("channels", [])
        added_by = watchlist.get("added_by", {})
        
        # Delete all existing
        client.table("watchlist").delete().neq("id", 0).execute()
        
        # Insert all channels
        rows = []
        for ch in channels:
            ab = added_by.get(ch, {})
            rows.append({
                "channel_name": ch,
                "added_by": ab.get("by", "system"),
                "added_at": ab.get("time", datetime.now().isoformat())
            })
        
        if rows:
            client.table("watchlist").insert(rows).execute()
        
        print(f"[DB] Saved {len(rows)} channels to watchlist")
        return True
    except Exception as e:
        print(f"[DB] save_watchlist error: {e}")
        return False

def add_to_watchlist_db(username, added_by="manual"):
    """Add single channel to watchlist"""
    username = username.lower().strip("@").strip()
    try:
        client = get_client()
        # Check if exists
        existing = client.table("watchlist").select("*").eq("channel_name", username).execute()
        if existing.data:
            return False, f"@{username} already in watchlist."
        
        client.table("watchlist").insert({
            "channel_name": username,
            "added_by": added_by,
            "added_at": datetime.now().isoformat()
        }).execute()
        print(f"[DB] Added @{username} to watchlist")
        return True, f"@{username} added to watchlist!"
    except Exception as e:
        print(f"[DB] add_to_watchlist error: {e}")
        return False, f"Error: {e}"

def remove_from_watchlist_db(username):
    """Remove single channel from watchlist"""
    username = username.lower().strip("@").strip()
    try:
        client = get_client()
        result = client.table("watchlist").delete().eq("channel_name", username).execute()
        print(f"[DB] Removed @{username} from watchlist")
        return True, f"@{username} removed from watchlist!"
    except Exception as e:
        print(f"[DB] remove_from_watchlist error: {e}")
        return False, f"Error: {e}"

def get_watchlist_db():
    """Get all watchlist channels"""
    watchlist = load_watchlist()
    return watchlist.get("channels", [])

# ==================== SUBSCRIBERS ====================
def load_subs():
    """Load subscribers from Supabase"""
    def _do():
        client = get_client()
        result = client.table("subscribers").select("*").execute()
        subs = {}
        for row in result.data:
            subs[str(row["user_id"])] = {
                "active": row.get("active", True),
                "added_at": row.get("added_at", "")
            }
        return subs
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] load_subs error: {e}")
        return {}

def save_subs(subs):
    """Save subscribers to Supabase using upsert"""
    def _do():
        client = get_client()
        if not subs:
            return True
        rows = []
        for uid, data in subs.items():
            rows.append({
                "user_id": int(uid),
                "active": data.get("active", True),
                "added_at": data.get("added_at", datetime.now().isoformat())
            })
        if rows:
            client.table("subscribers").upsert(rows, on_conflict="user_id").execute()
        print(f"[DB] Saved {len(rows)} subscribers")
        return True
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] save_subs error: {e}")
        return False

# ==================== BOT STATE ====================
def load_state():
    """Load bot state from Supabase"""
    try:
        client = get_client()
        result = client.table("bot_state").select("*").execute()
        state = {}
        for row in result.data:
            try:
                state[row["key"]] = json.loads(row["value"]) if row.get("value") else None
            except:
                state[row["key"]] = row.get("value")
        if not state:
            return {"known": {}, "polls": 0, "last_poll": None}
        # Reconstruct full state
        if "known" not in state:
            state["known"] = {}
        if "polls" not in state:
            state["polls"] = 0
        if "last_poll" not in state:
            state["last_poll"] = None
        return state
    except Exception as e:
        print(f"[DB] load_state error: {e}")
        return {"known": {}, "polls": 0, "last_poll": None}

def save_state(state):
    """Save bot state to Supabase"""
    def _do():
        client = get_client()
        for key, value in state.items():
            val_str = json.dumps(value, default=str) if value is not None else None
            existing = client.table("bot_state").select("*").eq("key", key).execute()
            if existing.data:
                client.table("bot_state").update({
                    "value": val_str,
                    "updated_at": datetime.now().isoformat()
                }).eq("key", key).execute()
            else:
                client.table("bot_state").insert({
                    "key": key,
                    "value": val_str,
                    "updated_at": datetime.now().isoformat()
                }).execute()
        return True
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] save_state error: {e}")
        return False

# ==================== COOKIE ====================
def load_cookie_db():
    """Load cookie from Supabase"""
    def _do():
        client = get_client()
        result = client.table("bot_state").select("*").eq("key", "cookie").execute()
        if result.data and result.data[0].get("value"):
            return json.loads(result.data[0]["value"]).get("cookie", "")
        return None
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] load_cookie error: {e}")
    return None

def save_cookie_db(cookie):
    """Save cookie to Supabase"""
    def _do():
        client = get_client()
        data = {
            "key": "cookie",
            "value": json.dumps({"cookie": cookie, "time": datetime.now().isoformat()}),
            "updated_at": datetime.now().isoformat()
        }
        existing = client.table("bot_state").select("*").eq("key", "cookie").execute()
        if existing.data:
            client.table("bot_state").update(data).eq("key", "cookie").execute()
        else:
            client.table("bot_state").insert(data).execute()
        print(f"[DB] Cookie saved ({len(cookie)} chars)")
        return True
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] save_cookie error: {e}")
        return False

# ==================== CLAIM HISTORY ====================
def load_history():
    """Load claim history from Supabase"""
    try:
        client = get_client()
        result = client.table("claim_history").select("*").order("created_at", desc=True).limit(100).execute()
        return result.data if result.data else []
    except Exception as e:
        print(f"[DB] load_history error: {e}")
        return []

def save_history(history):
    """Save claim history to Supabase"""
    try:
        client = get_client()
        if len(history) > 100:
            history = history[-100:]
        
        client.table("claim_history").delete().neq("id", 0).execute()
        if history:
            # Convert channels list to string if needed
            for h in history:
                if isinstance(h.get("channels"), list):
                    h["channels"] = json.dumps(h["channels"])
            client.table("claim_history").insert(history).execute()
        
        print(f"[DB] Saved {len(history)} history entries")
        return True
    except Exception as e:
        print(f"[DB] save_history error: {e}")
        return False

def add_to_history_db(campaign, event_type="seen"):
    """Add entry to claim history"""
    try:
        client = get_client()
        channels = [ch.get("user", {}).get("username", ch.get("slug", "?")) 
                    for ch in campaign.get("channels", [])]
        entry = {
            "campaign_id": str(campaign.get("id", "?")),
            "name": campaign.get("name", "?"),
            "status": campaign.get("status", "?"),
            "channels": json.dumps(channels),
            "event": event_type,
            "created_at": datetime.now().isoformat()
        }
        client.table("claim_history").insert(entry).execute()
        return True
    except Exception as e:
        print(f"[DB] add_to_history error: {e}")
        return False

# ==================== LOGS (24-HOUR RETENTION) ====================
def save_logs(logs_data):
    """Save logs to Supabase.
    LOG_BUFFER holds ~3000 entries = ~24 hours of data.
    This function replaces all Supabase logs with buffer contents.
    """
    def _do():
        client = get_client()
        if not logs_data:
            return True
        
        # Delete all existing logs and insert fresh from buffer
        client.table("logs").delete().neq("id", 0).execute()
        rows = [{"time": l.get("time", ""), "msg": l.get("msg", "")} for l in logs_data]
        if rows:
            for i in range(0, len(rows), 50):
                batch = rows[i:i+50]
                client.table("logs").insert(batch).execute()
        return True
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] save_logs error: {e}")
        return False

def load_logs():
    """Load logs from Supabase"""
    try:
        client = get_client()
        result = client.table("logs").select("*").order("time", desc=True).limit(200).execute()
        return result.data if result.data else []
    except Exception as e:
        print(f"[DB] load_logs error: {e}")
        return []

def add_log_db(time_str, msg):
    """Add single log entry to Supabase"""
    try:
        client = get_client()
        client.table("logs").insert({"time": time_str, "msg": msg}).execute()
        return True
    except Exception as e:
        print(f"[DB] add_log error: {e}")
        return False

# ==================== USER COOKIES (Per-User) ====================
def save_user_cookie(user_id, cookie, kick_username=None, kick_user_id=None):
    """Save per-user cookie to Supabase"""
    def _do():
        client = get_client()
        data = {
            "user_id": int(user_id),
            "cookie": cookie,
            "kick_username": kick_username,
            "kick_user_id": kick_user_id,
            "last_used": datetime.now().isoformat()
        }
        existing = client.table("user_cookies").select("*").eq("user_id", int(user_id)).execute()
        if existing.data:
            client.table("user_cookies").update(data).eq("user_id", int(user_id)).execute()
        else:
            data["added_at"] = datetime.now().isoformat()
            client.table("user_cookies").insert(data).execute()
        print(f"[DB] Saved cookie for user {user_id} (@{kick_username})")
        return True
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] save_user_cookie error: {e}")
        return False

def get_user_cookie(user_id):
    """Get cookie for specific user"""
    def _do():
        client = get_client()
        result = client.table("user_cookies").select("*").eq("user_id", int(user_id)).execute()
        if result.data:
            row = result.data[0]
            # Update last_used
            client.table("user_cookies").update({
                "last_used": datetime.now().isoformat()
            }).eq("user_id", int(user_id)).execute()
            return {
                "cookie": row.get("cookie"),
                "kick_username": row.get("kick_username"),
                "kick_user_id": row.get("kick_user_id")
            }
        return None
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] get_user_cookie error: {e}")
        return None

def get_all_user_cookies():
    """Get all user cookies for dashboard"""
    def _do():
        client = get_client()
        result = client.table("user_cookies").select("*").execute()
        return result.data if result.data else []
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] get_all_user_cookies error: {e}")
        return []

def remove_user_cookie(user_id):
    """Remove user cookie"""
    def _do():
        client = get_client()
        client.table("user_cookies").delete().eq("user_id", int(user_id)).execute()
        return True
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] remove_user_cookie error: {e}")
        return False

# ==================== USER PREFERENCES (Custom Streamers) ====================
def add_user_preference(user_id, channel_name):
    """Add a custom streamer to user's preference list"""
    def _do():
        client = get_client()
        channel_name = channel_name.lower().strip("@").strip()
        # Check if exists
        existing = client.table("user_preferences").select("*").eq("user_id", int(user_id)).eq("channel_name", channel_name).execute()
        if existing.data:
            return False, f"@{channel_name} already in your list!"
        client.table("user_preferences").insert({
            "user_id": int(user_id),
            "channel_name": channel_name
        }).execute()
        print(f"[DB] Added @{channel_name} to user {user_id} preferences")
        return True, f"@{channel_name} added to your watch list!"
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] add_user_preference error: {e}")
        return False, f"Error: {e}"

def remove_user_preference(user_id, channel_name):
    """Remove a streamer from user's preference list"""
    def _do():
        client = get_client()
        channel_name = channel_name.lower().strip("@").strip()
        client.table("user_preferences").delete().eq("user_id", int(user_id)).eq("channel_name", channel_name).execute()
        print(f"[DB] Removed @{channel_name} from user {user_id} preferences")
        return True, f"@{channel_name} removed from your list!"
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] remove_user_preference error: {e}")
        return False, f"Error: {e}"

def get_user_preferences(user_id):
    """Get all custom streamers for a user"""
    def _do():
        client = get_client()
        result = client.table("user_preferences").select("channel_name").eq("user_id", int(user_id)).execute()
        return [r["channel_name"] for r in result.data] if result.data else []
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] get_user_preferences error: {e}")
        return []

def get_all_user_preferences():
    """Get all user preferences for the watcher"""
    def _do():
        client = get_client()
        result = client.table("user_preferences").select("*").execute()
        return result.data if result.data else []
    try:
        return _retry(_do)
    except Exception as e:
        print(f"[DB] get_all_user_preferences error: {e}")
        return []

# ==================== MIGRATION ====================
def migrate_json_to_supabase():
    """Migrate existing JSON files to Supabase"""
    import os
    
    print("[DB] Starting migration from JSON to Supabase...")
    
    # Migrate watchlist
    watchlist_file = "tg_watchlist.json"
    if os.path.exists(watchlist_file):
        try:
            with open(watchlist_file) as f:
                data = json.load(f)
            save_watchlist(data)
            print(f"[DB] Migrated watchlist: {len(data.get('channels', []))} channels")
        except Exception as e:
            print(f"[DB] Watchlist migration error: {e}")
    
    # Migrate subscribers
    subs_file = "tg_subscribers.json"
    if os.path.exists(subs_file):
        try:
            with open(subs_file) as f:
                data = json.load(f)
            save_subs(data)
            print(f"[DB] Migrated subscribers: {len(data)} users")
        except Exception as e:
            print(f"[DB] Subscribers migration error: {e}")
    
    # Migrate state
    state_file = "tg_bot_state.json"
    if os.path.exists(state_file):
        try:
            with open(state_file) as f:
                data = json.load(f)
            save_state(data)
            print(f"[DB] Migrated state")
        except Exception as e:
            print(f"[DB] State migration error: {e}")
    
    # Migrate cookie
    cookie_file = "kick_cookie_live.json"
    if os.path.exists(cookie_file):
        try:
            with open(cookie_file) as f:
                data = json.load(f)
            cookie = data.get("cookie", "")
            if cookie:
                save_cookie_db(cookie)
                print(f"[DB] Migrated cookie ({len(cookie)} chars)")
        except Exception as e:
            print(f"[DB] Cookie migration error: {e}")
    
    # Migrate logs
    logs_file = "tg_bot_logs.json"
    if os.path.exists(logs_file):
        try:
            with open(logs_file) as f:
                data = json.load(f)
            save_logs(data)
            print(f"[DB] Migrated logs: {len(data)} entries")
        except Exception as e:
            print(f"[DB] Logs migration error: {e}")
    
    # Migrate history
    history_file = "tg_drop_history.json"
    if os.path.exists(history_file):
        try:
            with open(history_file) as f:
                data = json.load(f)
            save_history(data)
            print(f"[DB] Migrated history: {len(data)} entries")
        except Exception as e:
            print(f"[DB] History migration error: {e}")
    
    print("[DB] Migration complete!")

# ==================== TEST ====================
if __name__ == "__main__":
    print("=" * 50)
    print("Supabase Connection Test")
    print("=" * 50)
    
    client = get_client()
    print(f"Connected to: {SUPABASE_URL}")
    
    # Test all tables
    tables = ["watchlist", "subscribers", "bot_state", "claim_history", "logs"]
    for t in tables:
        try:
            result = client.table(t).select("*").limit(1).execute()
            print(f"[OK] {t}: OK ({len(result.data)} rows)")
        except Exception as e:
            print(f"[ERR] {t}: ERROR - {str(e)[:60]}")
    
    # Test migration
    print("\nRunning migration...")
    migrate_json_to_supabase()
    
    print("\nDone!")
