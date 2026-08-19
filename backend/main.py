"""ATLAS — Personal AI Manager. FastAPI backend (single-file, self-contained).

Manager Loop: natural language -> extract goals/tasks/commitments/events ->
decompose goals -> schedule work into free slots (around real Google Calendar
meetings) -> daily brief with proactive follow-up on overdue commitments.

Plus a live chat agent (/api/chat) that talks back and takes real actions
through tools: capture items, create Google Calendar events, check the brief.

Storage: SQLite (zero setup). Calendar sync activates once token.json exists.
"""
import os, json, sqlite3, datetime as dt
from contextlib import closing
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")   # ATLAS/.env
except ImportError:
    pass

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Google sometimes returns scopes in a different order / adds 'openid'; relax so
# the OAuth library doesn't reject the token exchange over a harmless scope diff.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
from urllib.parse import quote as _q

import calendar_client as gcal
import gmail_client as gmail
import web_client as web
import call_client as calls
import auth
import store
import push

# ---- config ---------------------------------------------------------------
MODEL = os.getenv("ATLAS_MODEL", "gemini-flash-lite-latest")   # fast + available on free tier
WORK_START, WORK_END = 9, 21
BLOCK_MINUTES = 60
FRONTEND = Path(__file__).parent.parent / "frontend"

# ---- permission tiers -----------------------------------------------------
# Outward/side-effecting tools default to "ask" (require user approval).
# Levels: "auto" (do it) | "ask" (propose, wait for Approve in the UI).
DEFAULT_SETTINGS = {"create_calendar_event": "ask", "draft_email": "ask",
                    "send_email": "ask", "place_call": "ask",
                    "tz": os.getenv("ATLAS_TZ", "Asia/Kolkata"), "avatar_url": ""}
# These ALWAYS require approval and can never be set to auto:
#  - propose_purchase: ATLAS never pays.
#  - send_email: sending mail is irreversible + outward-facing, so always confirm.
#  - place_call: dialling a real person is irreversible + outward, so always confirm.
ALWAYS_ASK = {"propose_purchase", "send_email", "place_call"}

def load_settings(uid="local"):
    """Per-user settings (permissions, timezone, avatar) stored in the DB."""
    try:
        with closing(db()) as c:
            r = c.execute("SELECT data FROM user_settings WHERE user_id=?", (uid,)).fetchone()
        stored = json.loads(r["data"]) if r else {}
    except Exception:
        stored = {}
    return {**DEFAULT_SETTINGS, **stored}

def save_settings(uid, patch):
    cur = load_settings(uid); cur.update(patch)
    with closing(db()) as c:
        c.execute("INSERT INTO user_settings(user_id,data) VALUES(?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
                  (uid, json.dumps(cur)))
        c.commit()
    return cur

def needs_approval(tool, uid="local"):
    if tool in ALWAYS_ASK:
        return True
    return load_settings(uid).get(tool, "auto") == "ask"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id {pk},
  user_id TEXT NOT NULL DEFAULT 'local',    -- owner account (per-user isolation)
  type TEXT NOT NULL,                       -- goal | task | commitment | event
  title TEXT NOT NULL,
  parent_id INTEGER,
  priority TEXT DEFAULT 'routine',
  status TEXT DEFAULT 'open',
  deadline TEXT,
  scheduled_start TEXT,
  scheduled_end TEXT,
  google_event_id TEXT,                     -- set when synced to Google Calendar
  link TEXT,                                -- store/checkout URL for purchases
  recurring_id INTEGER,                     -- template this occurrence came from
  est_minutes INTEGER DEFAULT 60,           -- estimated effort, drives scheduling
  source_text TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_settings(
  user_id TEXT PRIMARY KEY,
  data TEXT NOT NULL DEFAULT '{}'           -- per-user JSON (permissions, tz, avatar)
);
CREATE TABLE IF NOT EXISTS memory(
  id {pk},
  user_id TEXT NOT NULL DEFAULT 'local',
  content TEXT NOT NULL,                     -- a durable fact/preference about the user
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recurring(
  id {pk},
  user_id TEXT NOT NULL DEFAULT 'local',
  title TEXT NOT NULL,
  type TEXT DEFAULT 'task',                  -- task | event
  priority TEXT DEFAULT 'routine',
  rule TEXT NOT NULL,                        -- daily | weekly:MON,WED | monthly:15
  at_time TEXT,                              -- HH:MM or NULL
  active INTEGER DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_links(
  chat_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS link_codes(
  code TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notified(
  user_id TEXT NOT NULL, nkey TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(user_id, nkey)
);
CREATE TABLE IF NOT EXISTS push_subs(
  endpoint TEXT PRIMARY KEY, user_id TEXT NOT NULL, sub TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recordings(
  id {pk},
  user_id TEXT NOT NULL DEFAULT 'local',
  name TEXT NOT NULL,                        -- label the user gives the clip/message
  kind TEXT NOT NULL,                        -- 'audio' (uploaded clip) | 'tts' (spoken text)
  mime TEXT,                                 -- audio content-type (for kind='audio')
  media_token TEXT,                          -- unguessable public URL id for the clip
  audio_b64 TEXT,                            -- clip bytes, base64 in DB (durable, deploy-safe)
  text TEXT,                                 -- message to speak (for kind='tts')
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS call_jobs(
  token TEXT PRIMARY KEY,                    -- unguessable id, also the TwiML URL path
  user_id TEXT NOT NULL DEFAULT 'local',
  to_number TEXT NOT NULL,
  recording_id INTEGER,                      -- clip to play, or NULL if speaking text
  say_text TEXT,                             -- text to speak when no recording
  status TEXT DEFAULT 'queued',              -- queued | dialing | failed
  call_sid TEXT,                             -- Twilio call id once placed
  error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_secrets(
  skey TEXT PRIMARY KEY,                     -- e.g. 'google_token' (durable across restarts)
  value TEXT NOT NULL
);
"""

db = store.connect          # returns a connection wrapper (SQLite or Postgres)

def init_db():
    with closing(db()) as c:
        c.executescript(SCHEMA.replace("{pk}", store.PK))
        if store.is_postgres():                      # Postgres supports IF NOT EXISTS
            c.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS est_minutes INTEGER DEFAULT 60")
        else:                                        # migrate older SQLite DBs
            cols = [r[1] for r in c.execute("PRAGMA table_info(items)")]
            for col, typ in [("link", "TEXT"),
                             ("user_id", "TEXT NOT NULL DEFAULT 'local'"),
                             ("recurring_id", "INTEGER"),
                             ("est_minutes", "INTEGER DEFAULT 60")]:
                if col not in cols:
                    c.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
        c.commit()

def row_to_dict(r): return dict(r)

def get_secret(key):
    try:
        with closing(db()) as c:
            r = c.execute("SELECT value FROM app_secrets WHERE skey=?", (key,)).fetchone()
        return r["value"] if r else None
    except Exception:
        return None

def set_secret(key, value):
    with closing(db()) as c:
        if store.is_postgres():
            c.execute("INSERT INTO app_secrets(skey,value) VALUES(?,?) ON CONFLICT(skey) "
                      "DO UPDATE SET value=EXCLUDED.value", (key, value))
        else:
            c.execute("INSERT OR REPLACE INTO app_secrets(skey,value) VALUES(?,?)", (key, value))
        c.commit()

def parse_dt(x):
    try: return dt.datetime.fromisoformat(x) if x else None
    except (TypeError, ValueError): return None

# ---- recordings library + call jobs ---------------------------------------
# Audio clips are stored base64 IN THE DB (not on disk) so they survive restarts
# and work on ephemeral-disk hosts like Render free tier.
import base64, secrets
_REC_COLS = "id,name,kind,mime,media_token,text,created_at"   # never selects audio_b64 (big)

def add_recording(uid, name, kind, text=None, audio_b64=None, mime=None):
    """Save a call recording. kind='audio' stores an uploaded clip (base64 in DB);
    kind='tts' stores text ATLAS will speak."""
    now = dt.datetime.now().isoformat()
    token = clean = None
    if kind == "audio":
        if not audio_b64:
            raise HTTPException(400, "audio data required")
        clean = audio_b64.split(",", 1)[-1]                   # tolerate data: URLs
        try:
            raw = base64.b64decode(clean, validate=False)
        except Exception:
            raise HTTPException(400, "invalid audio data")
        if len(raw) > 8 * 1024 * 1024:
            raise HTTPException(400, "audio too large (max 8 MB)")
        token = secrets.token_urlsafe(12)
    with closing(db()) as c:
        rid = c.insert("INSERT INTO recordings(user_id,name,kind,mime,media_token,"
                       "audio_b64,text,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (uid, name, kind, mime, token, clean, text, now))
        c.commit()
    return {"id": rid, "name": name, "kind": kind, "media_token": token}

def list_recordings(uid):
    with closing(db()) as c:
        rows = c.execute(f"SELECT {_REC_COLS} FROM recordings"
                         " WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()
    return [dict(r) for r in rows]

def get_recording(uid, rid):
    with closing(db()) as c:
        r = c.execute(f"SELECT {_REC_COLS} FROM recordings"
                      " WHERE user_id=? AND id=?", (uid, rid)).fetchone()
    return dict(r) if r else None

def find_recording_by_name(uid, name):
    with closing(db()) as c:
        r = c.execute(f"SELECT {_REC_COLS} FROM recordings"
                      " WHERE user_id=? AND lower(name)=lower(?) ORDER BY id DESC LIMIT 1",
                      (uid, name)).fetchone()
    return dict(r) if r else None

def recording_audio(media_token):
    """Return (bytes, mime) for a stored audio clip, or None. Public lookup by
    unguessable token (Twilio + the in-app <audio> player fetch this)."""
    with closing(db()) as c:
        r = c.execute("SELECT audio_b64,mime FROM recordings WHERE media_token=?",
                      (media_token,)).fetchone()
    if not r or not r["audio_b64"]:
        return None
    try:
        return base64.b64decode(r["audio_b64"]), (r["mime"] or "audio/mpeg")
    except Exception:
        return None

def delete_recording(uid, rid):
    with closing(db()) as c:
        c.execute("DELETE FROM recordings WHERE user_id=? AND id=?", (uid, rid)); c.commit()
    return {"deleted": rid}

def create_call_job(uid, to, recording_id=None, say_text=None):
    token = secrets.token_urlsafe(16)
    now = dt.datetime.now().isoformat()
    with closing(db()) as c:
        c.execute("INSERT INTO call_jobs(token,user_id,to_number,recording_id,say_text,"
                  "status,created_at) VALUES(?,?,?,?,?, 'queued',?)",
                  (token, uid, to, recording_id, say_text, now)); c.commit()
    return token

def get_call_job(token):
    with closing(db()) as c:
        r = c.execute("SELECT * FROM call_jobs WHERE token=?", (token,)).fetchone()
    return dict(r) if r else None

def set_call_job(token, **fields):
    if not fields: return
    cols = ",".join(f"{k}=?" for k in fields)
    with closing(db()) as c:
        c.execute(f"UPDATE call_jobs SET {cols} WHERE token=?",
                  (*fields.values(), token)); c.commit()

def list_call_history(uid, limit=20):
    with closing(db()) as c:
        rows = c.execute("SELECT token,to_number,recording_id,say_text,status,error,"
                         "created_at FROM call_jobs WHERE user_id=? ORDER BY created_at "
                         "DESC LIMIT ?", (uid, limit)).fetchall()
    return [dict(r) for r in rows]

# ---- llm extraction -------------------------------------------------------
EXTRACT_SYS = """You are the intake brain of ATLAS, a personal AI manager. Read \
the user's note and extract structured items. Return ONLY JSON: {"items":[...]}. \
Each item:
  type: "goal" (multi-step project) | "task" (single action) |
        "commitment" (a promise: "I'll call…","I need to…") |
        "event" (a fixed appointment WITH a time)
  title: short imperative phrase (<8 words)
  priority: "critical" | "important" | "routine"
  deadline: ISO 8601 date/datetime or null. For "event" put the start datetime
            here. Resolve relative dates from the provided current datetime.
  subtasks: ONLY for "goal" — 3-7 concrete step titles. Else [].
Split compound notes into multiple items."""

# ---- LLM engine: Google Gemini (free tier) --------------------------------
_GENAI_CLIENT = None
def _genai():
    """One reused client — creating a fresh genai.Client per call closes the
    shared HTTP transport ('client has been closed' errors)."""
    global _GENAI_CLIENT
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    if _GENAI_CLIENT is None:
        from google import genai
        _GENAI_CLIENT = genai.Client(api_key=key)
    return _GENAI_CLIENT

_TRANSIENT = ("503", "500", "429", "overloaded", "unavailable", "resource_exhausted")
def _generate(**kwargs):
    """generate_content with retry/backoff for Gemini's frequent free-tier 503s.
    Google's flash models get overloaded intermittently; retrying usually wins."""
    import time
    last = None
    for attempt in range(5):
        try:
            return _genai().models.generate_content(**kwargs)
        except Exception as e:
            last = e
            if any(x in str(e).lower() for x in _TRANSIENT):
                time.sleep(0.8 * (attempt + 1)); continue    # 0.8,1.6,2.4,3.2s
            raise
    raise last

_GTYPE = {"string": "STRING", "integer": "INTEGER", "number": "NUMBER",
          "boolean": "BOOLEAN", "object": "OBJECT", "array": "ARRAY"}

def _to_schema(js):
    """Convert a JSON-schema dict (our tool format) to a Gemini types.Schema."""
    from google.genai import types
    kw = {"type": _GTYPE.get(js.get("type", "string"), "STRING")}
    if js.get("description"): kw["description"] = js["description"]
    if kw["type"] == "OBJECT":
        props = js.get("properties") or {}
        kw["properties"] = {k: _to_schema(v) for k, v in props.items()}
        if js.get("required"): kw["required"] = js["required"]
    if kw["type"] == "ARRAY" and js.get("items"):
        kw["items"] = _to_schema(js["items"])
    return types.Schema(**kw)

def _gemini_tools():
    from google.genai import types
    decls = []
    for t in TOOLS:
        props = t["input_schema"].get("properties") or {}
        params = _to_schema(t["input_schema"]) if props else None
        decls.append(types.FunctionDeclaration(
            name=t["name"], description=t["description"], parameters=params))
    return [types.Tool(function_declarations=decls)]

def call_llm(system, user, json_mode=False):
    from google.genai import types
    cfg = types.GenerateContentConfig(system_instruction=system, temperature=0.2)
    if json_mode:
        cfg.response_mime_type = "application/json"
    resp = _generate(model=MODEL, contents=user, config=cfg)
    return resp.text or ""

def extract_items(text):
    now = dt.datetime.now().isoformat(timespec="minutes")
    raw = call_llm(EXTRACT_SYS, f"Current datetime: {now}\n\nNote:\n{text}",
                   json_mode=True).strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].removeprefix("json").strip()
    try:
        return json.loads(raw).get("items", [])
    except json.JSONDecodeError:
        raise HTTPException(502, f"Could not parse extraction: {raw[:300]}")

def _sync_event(c, item_id, title, start, end, priority):
    """Create a real Google Calendar event for a scheduled block/appointment."""
    if not gcal.is_configured():
        return
    try:
        ev = gcal.create_event(title, start, end, f"ATLAS · {priority}")
        c.execute("UPDATE items SET google_event_id=? WHERE id=?",
                  (ev["id"], item_id))
    except Exception as e:            # calendar issues never break capture
        print("calendar sync failed:", e)

def store_items(parsed, source, uid="local"):
    created = []; now = dt.datetime.now().isoformat()
    with closing(db()) as c:
        for it in parsed:
            typ = it.get("type", "task")
            dl = it.get("deadline"); pri = it.get("priority", "routine")
            ss = se = None
            if typ == "event" and parse_dt(dl) and "T" in (dl or ""):
                ss = dl; se = (parse_dt(dl) + dt.timedelta(hours=1)).isoformat()
            gid = c.insert(
                "INSERT INTO items(user_id,type,title,priority,deadline,scheduled_start,"
                "scheduled_end,source_text,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (uid, typ, it.get("title", "Untitled"), pri, dl, ss, se, source, now))
            created.append(gid)
            if ss:
                _sync_event(c, gid, it.get("title", "Event"), ss, se, pri)
            for st in (it.get("subtasks") or []):
                title = st if isinstance(st, str) else st.get("title", "step")
                c.execute(
                    "INSERT INTO items(user_id,type,title,parent_id,priority,deadline,"
                    "source_text,created_at) VALUES(?,'task',?,?,?,?,?,?)",
                    (uid, title, gid, pri, dl, source, now))
        c.commit()
    schedule_unplanned(uid)
    return created

# ---- planner --------------------------------------------------------------
def _busy(c, uid, horizon_days=60):
    """Busy intervals from this user's scheduled items + real Google Calendar."""
    out = []
    for r in c.execute("SELECT scheduled_start,scheduled_end FROM items WHERE "
                       "user_id=? AND scheduled_start IS NOT NULL AND status='open'",
                       (uid,)):
        s, e = parse_dt(r["scheduled_start"]), parse_dt(r["scheduled_end"])
        if s and e: out.append((s, e))
    if gcal.is_configured():
        try:
            now = dt.datetime.now()
            out += gcal.busy_intervals(now, now + dt.timedelta(days=horizon_days))
        except Exception as e:
            print("calendar busy fetch failed:", e)
    return out

def _next_free_block(after, busy, minutes=BLOCK_MINUTES):
    t = after.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    for _ in range(24 * 60):
        if t.hour < WORK_START:
            t = t.replace(hour=WORK_START)
        day_end = t.replace(hour=WORK_END, minute=0, second=0, microsecond=0)
        end = t + dt.timedelta(minutes=minutes)
        if end > day_end:                          # doesn't fit today -> next day
            t = (t + dt.timedelta(days=1)).replace(hour=WORK_START, minute=0); continue
        if not any(t < b_end and end > b_start for b_start, b_end in busy):
            return t, end
        t = end
    return t, t + dt.timedelta(minutes=minutes)

# highest priority first, then earliest deadline
_PRIORITY_SQL = ("CASE priority WHEN 'critical' THEN 0 WHEN 'important' THEN 1 "
                 "ELSE 2 END, (deadline IS NULL), deadline")

def schedule_unplanned(uid="local"):
    """Place every open, unscheduled leaf task into a free block — highest
    priority first, then earliest deadline — using each task's effort estimate.
    Scoped to one user; synced to Google Calendar."""
    with closing(db()) as c:
        tasks = c.execute(
            "SELECT * FROM items WHERE user_id=? AND type='task' AND status='open' "
            "AND scheduled_start IS NULL "
            "AND id NOT IN (SELECT parent_id FROM items WHERE parent_id IS NOT NULL) "
            "ORDER BY " + _PRIORITY_SQL, (uid,)).fetchall()
        busy = _busy(c, uid)
        for t in tasks:
            mins = t["est_minutes"] or BLOCK_MINUTES
            start, end = _next_free_block(dt.datetime.now(), busy, mins)
            busy.append((start, end))
            c.execute("UPDATE items SET scheduled_start=?,scheduled_end=? WHERE id=?",
                      (start.isoformat(), end.isoformat(), t["id"]))
            _sync_event(c, t["id"], t["title"], start.isoformat(),
                        end.isoformat(), t["priority"])
        c.commit()

def reschedule_all(uid="local"):
    """Clear and re-plan all open leaf tasks so the day reshuffles by current
    priorities/deadlines (e.g. after something changed)."""
    with closing(db()) as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM items WHERE user_id=? AND type='task' AND status='open' "
            "AND id NOT IN (SELECT parent_id FROM items WHERE parent_id IS NOT NULL)",
            (uid,))]
    _unsync(uid, ids)
    with closing(db()) as c:
        c.execute("UPDATE items SET scheduled_start=NULL, scheduled_end=NULL, "
                  "google_event_id=NULL WHERE user_id=? AND type='task' AND status='open' "
                  "AND id NOT IN (SELECT parent_id FROM items WHERE parent_id IS NOT NULL)",
                  (uid,))
        c.commit()
    schedule_unplanned(uid)

def edit_item(uid, item_id, title=None, priority=None, deadline=None,
              scheduled_start=None, est_minutes=None):
    fields = {}
    if title is not None: fields["title"] = title
    if priority is not None: fields["priority"] = priority
    if deadline is not None: fields["deadline"] = deadline
    if est_minutes is not None: fields["est_minutes"] = est_minutes
    if scheduled_start is not None:
        fields["scheduled_start"] = scheduled_start
        mins = est_minutes or 60
        d = parse_dt(scheduled_start)
        if d: fields["scheduled_end"] = (d + dt.timedelta(minutes=mins)).isoformat()
    if not fields:
        return {"error": "nothing to update"}
    sets = ", ".join(f"{k}=?" for k in fields)
    with closing(db()) as c:
        c.execute(f"UPDATE items SET {sets} WHERE user_id=? AND id=?",
                  list(fields.values()) + [uid, item_id])
        c.commit()
    return {"updated": item_id, "changed": fields}

def _unsync(uid, item_ids):
    if not gcal.is_configured() or not item_ids: return
    with closing(db()) as c:
        q = ",".join("?" * len(item_ids))
        for r in c.execute(f"SELECT google_event_id FROM items WHERE user_id=? AND "
                           f"id IN ({q}) AND google_event_id IS NOT NULL",
                           [uid, *item_ids]):
            try: gcal.delete_event(r["google_event_id"])
            except Exception as e: print("calendar delete failed:", e)

# ---- per-user item operations (used by both the API and the agent) --------
def list_items_for(uid, status="open"):
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM items WHERE user_id=? AND status=? ORDER BY "
                         "(deadline IS NULL), deadline, id", (uid, status)).fetchall()
    return [row_to_dict(r) for r in rows]

def set_status(uid, item_id, status):
    if status == "done":
        with closing(db()) as c:
            kids = [r["id"] for r in c.execute(
                "SELECT id FROM items WHERE user_id=? AND parent_id=?", (uid, item_id))]
        _unsync(uid, [item_id, *kids])
    with closing(db()) as c:
        c.execute("UPDATE items SET status=? WHERE user_id=? AND (id=? OR parent_id=?)",
                  (status, uid, item_id, item_id))
        c.commit()
    schedule_unplanned(uid)
    return {"ok": True}

def remove_item(uid, item_id):
    with closing(db()) as c:
        kids = [r["id"] for r in c.execute(
            "SELECT id FROM items WHERE user_id=? AND parent_id=?", (uid, item_id))]
    _unsync(uid, [item_id, *kids])
    with closing(db()) as c:
        c.execute("DELETE FROM items WHERE user_id=? AND (id=? OR parent_id=?)",
                  (uid, item_id, item_id))
        c.commit()
    return {"ok": True}

# ---- long-term memory -----------------------------------------------------
def add_memory(uid, content):
    with closing(db()) as c:
        c.execute("INSERT INTO memory(user_id,content,created_at) VALUES(?,?,?)",
                  (uid, content.strip(), dt.datetime.now().isoformat()))
        c.commit()
    return {"remembered": content.strip()}

def list_memory(uid):
    with closing(db()) as c:
        return [row_to_dict(r) for r in c.execute(
            "SELECT * FROM memory WHERE user_id=? ORDER BY id DESC", (uid,))]

def forget_memory(uid, needle):
    with closing(db()) as c:
        if str(needle).isdigit():
            c.execute("DELETE FROM memory WHERE user_id=? AND id=?", (uid, int(needle)))
        else:
            c.execute("DELETE FROM memory WHERE user_id=? AND content LIKE ?",
                      (uid, f"%{needle}%"))
        c.commit()
    return {"forgot": needle}

def memory_block(uid):
    facts = list_memory(uid)
    return "\n".join("- " + f["content"] for f in facts[:60]) or "(nothing saved yet)"

# ---- recurring tasks/events ----------------------------------------------
WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

def build_rule(freq, days=None, day_of_month=None):
    if freq == "weekly" and days:
        return "weekly:" + ",".join(d.upper()[:3] for d in days)
    if freq == "monthly" and day_of_month:
        return f"monthly:{int(day_of_month)}"
    return "daily"

def _rule_matches(rule, d):
    if rule == "daily": return True
    if rule.startswith("weekly:"):
        return WEEKDAYS[d.weekday()] in rule.split(":", 1)[1].split(",")
    if rule.startswith("monthly:"):
        try: return d.day == int(rule.split(":", 1)[1])
        except ValueError: return False
    return False

def add_recurring(uid, title, freq, days=None, day_of_month=None, at_time=None,
                  type="task", priority="routine"):
    rule = build_rule(freq, days, day_of_month)
    with closing(db()) as c:
        c.execute("INSERT INTO recurring(user_id,title,type,priority,rule,at_time,"
                  "created_at) VALUES(?,?,?,?,?,?,?)",
                  (uid, title, "event" if at_time else type, priority, rule,
                   at_time, dt.datetime.now().isoformat()))
        c.commit()
    spawn_recurring(uid)
    return {"added_recurring": title, "rule": rule, "at_time": at_time}

def list_recurring(uid):
    with closing(db()) as c:
        return [row_to_dict(r) for r in c.execute(
            "SELECT * FROM recurring WHERE user_id=? AND active=1 ORDER BY id DESC", (uid,))]

def remove_recurring(uid, rid):
    with closing(db()) as c:
        c.execute("UPDATE recurring SET active=0 WHERE user_id=? AND id=?", (uid, int(rid)))
        c.commit()
    return {"removed_recurring": rid}

def spawn_recurring(uid, horizon=7):
    """Materialize concrete items for each active template across the next
    `horizon` days. Idempotent (dedup on recurring_id + date)."""
    today = dt.date.today(); now = dt.datetime.now().isoformat()
    with closing(db()) as c:
        temps = c.execute("SELECT * FROM recurring WHERE user_id=? AND active=1",
                          (uid,)).fetchall()
        for t in temps:
            for i in range(horizon + 1):
                d = today + dt.timedelta(days=i)
                if not _rule_matches(t["rule"], d): continue
                exists = c.execute("SELECT 1 FROM items WHERE user_id=? AND recurring_id=? "
                                   "AND substr(deadline,1,10)=?",
                                   (uid, t["id"], d.isoformat())).fetchone()
                if exists: continue
                if t["at_time"]:
                    ss = f"{d.isoformat()}T{t['at_time']}"
                    se = (parse_dt(ss) + dt.timedelta(hours=1)).isoformat()
                    c.execute("INSERT INTO items(user_id,type,title,priority,deadline,"
                              "scheduled_start,scheduled_end,recurring_id,created_at) "
                              "VALUES(?,'event',?,?,?,?,?,?,?)",
                              (uid, t["title"], t["priority"], ss, ss, se, t["id"], now))
                else:
                    c.execute("INSERT INTO items(user_id,type,title,priority,deadline,"
                              "recurring_id,created_at) VALUES(?,'task',?,?,?,?,?)",
                              (uid, t["title"], t["priority"], d.isoformat(), t["id"], now))
        c.commit()
    schedule_unplanned(uid)

# ---- Telegram linking + proactive delivery --------------------------------
def make_link_code(uid):
    import secrets
    code = secrets.token_hex(3).upper()
    with closing(db()) as c:
        c.execute("INSERT INTO link_codes(code,user_id,created_at) VALUES(?,?,?)",
                  (code, uid, dt.datetime.now().isoformat())); c.commit()
    return code

def resolve_link_code(code, chat_id, name=""):
    with closing(db()) as c:
        r = c.execute("SELECT user_id FROM link_codes WHERE code=?", (code.upper(),)).fetchone()
        if not r:
            return None
        uid = r["user_id"]
        c.execute("INSERT INTO telegram_links(chat_id,user_id,name,created_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(chat_id) DO UPDATE SET user_id=excluded.user_id",
                  (str(chat_id), uid, name, dt.datetime.now().isoformat()))
        c.execute("DELETE FROM link_codes WHERE code=?", (code.upper(),))
        c.commit()
    return uid

def user_for_chat(chat_id):
    with closing(db()) as c:
        r = c.execute("SELECT user_id FROM telegram_links WHERE chat_id=?",
                      (str(chat_id),)).fetchone()
    return r["user_id"] if r else "local"

def linked_chats():
    with closing(db()) as c:
        return [row_to_dict(r) for r in c.execute("SELECT * FROM telegram_links")]

def _notified(uid, nkey):
    with closing(db()) as c:
        return c.execute("SELECT 1 FROM notified WHERE user_id=? AND nkey=?",
                         (uid, nkey)).fetchone() is not None

def _mark_notified(uid, nkey):
    with closing(db()) as c:
        c.execute("INSERT INTO notified(user_id,nkey,created_at) VALUES(?,?,?) "
                  "ON CONFLICT DO NOTHING", (uid, nkey, dt.datetime.now().isoformat()))
        c.commit()

def brief_text(uid, name="there"):
    b = build_brief(uid, name)
    c = b["counts"]
    lines = [b["greeting"],
             f"{c['critical']} critical · {c['important']} important · {c['routine']} routine"]
    if b["schedule"]:
        lines.append("\nToday:")
        lines += [f"  {s['time']}  {s['title']}" for s in b["schedule"][:8]]
    if b["attention"]:
        lines.append("\nNeeds your attention:")
        lines += [f"  ⚠ {a}" for a in b["attention"][:6]]
    return "\n".join(lines)

def due_nudges(uid):
    return [a for a in build_brief(uid)["attention"] if "overdue" in a]

# ---- push subscriptions + unified notifications ---------------------------
def save_push_sub(uid, sub):
    ep = sub.get("endpoint")
    if not ep:
        return {"error": "no endpoint"}
    with closing(db()) as c:
        c.execute("INSERT INTO push_subs(endpoint,user_id,sub,created_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, "
                  "sub=excluded.sub", (ep, uid, json.dumps(sub), dt.datetime.now().isoformat()))
        c.commit()
    return {"ok": True}

def _push_subs(uid):
    with closing(db()) as c:
        return [json.loads(r["sub"]) for r in
                c.execute("SELECT sub FROM push_subs WHERE user_id=?", (uid,))]

def _chat_for_user(uid):
    with closing(db()) as c:
        r = c.execute("SELECT chat_id FROM telegram_links WHERE user_id=? LIMIT 1",
                      (uid,)).fetchone()
    return r["chat_id"] if r else None

def notify_user(uid, title, body):
    """Deliver a proactive message over every channel the user has connected:
    web-push and/or Telegram."""
    for sub in _push_subs(uid):
        push.send(sub, title, body)
    chat = _chat_for_user(uid)
    if chat:
        try:
            import telegram_bot
            telegram_bot.send(chat, f"{title}\n{body}" if body else title)
        except Exception as e:
            print("telegram notify failed:", e)

def _notifiable_users():
    users = set()
    with closing(db()) as c:
        for r in c.execute("SELECT DISTINCT user_id FROM telegram_links"):
            users.add(r["user_id"])
        for r in c.execute("SELECT DISTINCT user_id FROM push_subs"):
            users.add(r["user_id"])
    return users

def proactive_loop():
    """Background: deliver each user their morning brief (at their tz's brief hour,
    once/day) and new overdue nudges, via web-push and/or Telegram."""
    import time
    from zoneinfo import ZoneInfo
    print("ATLAS proactive loop started.")
    while True:
        try:
            for uid in _notifiable_users():
                s = load_settings(uid)
                try:
                    now = dt.datetime.now(ZoneInfo(s.get("tz", "Asia/Kolkata")))
                except Exception:
                    now = dt.datetime.now()
                daykey = "brief-" + now.date().isoformat()
                if now.hour == int(s.get("brief_hour", 8)) and not _notified(uid, daykey):
                    notify_user(uid, "ATLAS · your day", brief_text(uid))
                    _mark_notified(uid, daykey)
                for n in due_nudges(uid):
                    nk = "nudge-" + now.date().isoformat() + "-" + n[:40]
                    if not _notified(uid, nk):
                        notify_user(uid, "🔔 Reminder", n); _mark_notified(uid, nk)
        except Exception as e:
            print("proactive loop error:", e)
        time.sleep(120)

# ---- daily brief ----------------------------------------------------------
def build_brief(uid="local", name="there"):
    spawn_recurring(uid)                       # keep recurring items materialized
    now = dt.datetime.now(); today = now.date()
    tomorrow = today + dt.timedelta(days=1)
    with closing(db()) as c:
        rows = [row_to_dict(r) for r in
                c.execute("SELECT * FROM items WHERE user_id=? AND status='open'", (uid,))]
    counts = {"critical": 0, "important": 0, "routine": 0}
    todays, attention, pending = [], [], []
    for r in rows:
        if r["type"] in ("task", "goal", "event"):
            counts[r["priority"]] = counts.get(r["priority"], 0) + 1
        ss, dl = parse_dt(r["scheduled_start"]), parse_dt(r["deadline"])
        if ss and ss.date() == today: todays.append((ss, r))
        if r["type"] == "commitment" and dl and dl < now:
            attention.append(f"{r['title']} — commitment {(now - dl).days}d overdue")
        elif dl and dl.date() <= tomorrow:
            when = "today" if dl.date() == today else "tomorrow"
            attention.append(f"{r['title']} — due {when}")
        if r["type"] in ("task", "commitment") and not ss:
            pending.append(r["title"])
    todays.sort(key=lambda x: x[0])
    schedule = [{"time": s.strftime("%H:%M"), "title": r["title"],
                 "priority": r["priority"], "id": r["id"]} for s, r in todays]
    s = load_settings(uid)
    try:
        from zoneinfo import ZoneInfo
        uhour = dt.datetime.now(ZoneInfo(s.get("tz", "Asia/Kolkata"))).hour
    except Exception:
        uhour = now.hour
    greet = ("Good morning" if uhour < 12 else
             "Good afternoon" if uhour < 17 else "Good evening")
    av = s.get("avatar_url")
    if not av and (FRONTEND / "avatar.glb").exists():
        av = "/static/avatar.glb"
    return {"greeting": f"{greet}, {name}.", "date": today.isoformat(),
            "counts": counts, "schedule": schedule,
            "attention": attention[:8], "pending": pending[:8],
            "calendar_connected": gcal.is_configured(), "avatar_url": av or ""}

# ---- chat agent (tools) ---------------------------------------------------
TOOLS = [
 {"name": "capture", "description": "Store tasks/goals/commitments/events the user "
  "wants to remember, plan, or get done. YOU extract the structured items and "
  "decompose goals into steps — pass them directly (do not describe, just call). "
  "They're auto-scheduled into free time & synced to Calendar.",
  "input_schema": {"type": "object", "properties": {"items": {"type": "array",
      "items": {"type": "object", "properties": {
          "type": {"type": "string", "description": "goal | task | commitment | event"},
          "title": {"type": "string", "description": "short imperative phrase"},
          "priority": {"type": "string", "description": "critical | important | routine"},
          "deadline": {"type": "string", "description": "ISO date/datetime, or omit"},
          "subtasks": {"type": "array", "items": {"type": "string"},
                       "description": "ONLY for a goal: 3-7 concrete step titles"}},
          "required": ["type", "title"]}}}, "required": ["items"]}},
 {"name": "list_items", "description": "List the user's current open items.",
  "input_schema": {"type": "object", "properties": {}}},
 {"name": "complete_item", "description": "Mark an item (and its subtasks) done.",
  "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}},
                   "required": ["id"]}},
 {"name": "get_brief", "description": "Today's brief: priority counts, schedule, "
  "and overdue commitments needing follow-up.",
  "input_schema": {"type": "object", "properties": {}}},
 {"name": "create_calendar_event", "description": "Create a real event on the "
  "user's Google Calendar.",
  "input_schema": {"type": "object", "properties": {
      "title": {"type": "string"},
      "start": {"type": "string", "description": "naive local ISO, e.g. 2026-08-14T17:30"},
      "end": {"type": "string", "description": "naive local ISO; default +1h"},
      "description": {"type": "string"}}, "required": ["title", "start"]}},
 {"name": "list_calendar_events", "description": "Upcoming Google Calendar events.",
  "input_schema": {"type": "object", "properties": {"days": {"type": "integer"}}}},
 {"name": "check_email", "description": "Read the user's recent Gmail to find "
  "action items, requests, and deadlines. Returns sender, subject, snippet. "
  "After reading, use `capture` to turn action items into tasks/commitments.",
  "input_schema": {"type": "object", "properties": {
      "query": {"type": "string", "description": "Gmail search, default is:unread newer_than:7d"},
      "max": {"type": "integer"}}}},
 {"name": "draft_email", "description": "Create a DRAFT email/reply in the user's "
  "Gmail for them to review and send. Use when the user wants to review before "
  "sending, or is unsure.",
  "input_schema": {"type": "object", "properties": {
      "to": {"type": "string"}, "subject": {"type": "string"},
      "body": {"type": "string"}}, "required": ["to", "subject", "body"]}},
 {"name": "send_email", "description": "Actually SEND an email from the user's "
  "Gmail. Use when the user clearly asks to send/email something. The app always "
  "shows the user an Approve/Cancel card first, so it only sends after they "
  "confirm — you do not need to ask for confirmation yourself, just call it.",
  "input_schema": {"type": "object", "properties": {
      "to": {"type": "string"}, "subject": {"type": "string"},
      "body": {"type": "string"},
      "cc": {"type": "string"}, "bcc": {"type": "string"}},
      "required": ["to", "subject", "body"]}},
 {"name": "place_call", "description": "Place a real phone call from the user's "
  "ATLAS number to a person, when the user asks you to call someone. When they "
  "answer, ATLAS plays either a saved recording (pass recording_name or "
  "recording_id) or speaks a message (pass say). The app shows an Approve/Cancel "
  "card before dialling, so just call the tool; don't ask 'should I call?' "
  "yourself. Give the number in international E.164 form if the user provides it.",
  "input_schema": {"type": "object", "properties": {
      "to": {"type": "string", "description": "phone number to call, E.164 e.g. +919876543210"},
      "recording_name": {"type": "string", "description": "name of a saved recording to play"},
      "recording_id": {"type": "integer", "description": "id of a saved recording to play"},
      "say": {"type": "string", "description": "message to speak aloud if no recording is chosen"}},
      "required": ["to"]}},
 {"name": "list_recordings", "description": "List the user's saved call recordings "
  "and spoken-message templates (name + kind), e.g. before placing a call.",
  "input_schema": {"type": "object", "properties": {}}},
 {"name": "web_search", "description": "Search the live web for current info — "
  "products, prices, reviews, places, facts. Returns title, url, snippet for the "
  "top results. Use this first for any 'find / compare / recommend' request.",
  "input_schema": {"type": "object", "properties": {
      "query": {"type": "string"}, "count": {"type": "integer"}},
      "required": ["query"]}},
 {"name": "web_read", "description": "Open a URL from web_search and read its main "
  "text to check specs, details, or reviews before recommending.",
  "input_schema": {"type": "object", "properties": {"url": {"type": "string"}},
      "required": ["url"]}},
 {"name": "propose_purchase", "description": "Propose a product for the user to BUY, "
  "after researching it. IMPORTANT: this never pays or checks out — approving only "
  "saves the decision and opens the store link so the USER completes payment. Use "
  "after web_search/web_read. Always requires the user's approval.",
  "input_schema": {"type": "object", "properties": {
      "title": {"type": "string"}, "price": {"type": "string"},
      "url": {"type": "string", "description": "direct product/checkout page"},
      "reason": {"type": "string"}, "alternatives": {"type": "string"}},
      "required": ["title", "url"]}},
 {"name": "remember", "description": "Save a durable fact or preference about the "
  "user to long-term memory (persists across all future conversations). Use for "
  "people (manager, teammates, family), habits, preferences, likes/dislikes, "
  "home/work details, ongoing goals — anything worth recalling later.",
  "input_schema": {"type": "object", "properties": {"fact": {"type": "string"}},
      "required": ["fact"]}},
 {"name": "add_recurring", "description": "Create a recurring task or event that "
  "repeats automatically (e.g. daily standup, gym Mon/Wed/Fri, rent on the 1st). "
  "Occurrences are generated onto the user's schedule.",
  "input_schema": {"type": "object", "properties": {
      "title": {"type": "string"},
      "freq": {"type": "string", "description": "daily | weekly | monthly"},
      "days": {"type": "array", "items": {"type": "string"},
               "description": "for weekly: e.g. [MON,WED,FRI]"},
      "day_of_month": {"type": "integer", "description": "for monthly: 1-31"},
      "time": {"type": "string", "description": "HH:MM 24h; makes it a timed event"},
      "priority": {"type": "string", "description": "critical|important|routine"}},
      "required": ["title", "freq"]}},
 {"name": "list_recurring", "description": "List the user's active recurring items.",
  "input_schema": {"type": "object", "properties": {}}},
 {"name": "update_item", "description": "Change an existing item: its title, "
  "priority, deadline, scheduled time, or effort estimate. Call `list_items` first "
  "to get the item's id. Use for 'move/reschedule/rename/make urgent/push the "
  "deadline'.",
  "input_schema": {"type": "object", "properties": {
      "id": {"type": "integer"},
      "title": {"type": "string"},
      "priority": {"type": "string", "description": "critical|important|routine"},
      "deadline": {"type": "string", "description": "ISO date/datetime"},
      "scheduled_start": {"type": "string", "description": "ISO local datetime to move the work block to"},
      "est_minutes": {"type": "integer", "description": "estimated effort in minutes"}},
      "required": ["id"]}},
 {"name": "reschedule", "description": "Re-plan the whole day: reshuffle all open "
  "tasks into free time by priority and deadline (use after things change or the "
  "user falls behind).",
  "input_schema": {"type": "object", "properties": {}}},
]

class StatusIn(BaseModel): status: str

def _plus_hour(s):
    return (parse_dt(s) + dt.timedelta(hours=1)).isoformat() if parse_dt(s) else s

def run_tool(name, inp, uid="local", approved=False):
    if not approved and needs_approval(name, uid):
        return {"pending": True, "tool": name, "input": inp,
                "message": "Proposed — awaiting the user's approval."}
    try:
        if name == "capture":
            items = inp.get("items") or []
            # tolerate the model passing raw text instead of structured items
            if not items and inp.get("text"):
                items = extract_items(inp["text"])
            return {"created": len(store_items(items, "chat", uid)), "items": items}
        if name == "list_items":
            return list_items_for(uid)
        if name == "complete_item":
            return set_status(uid, inp["id"], "done")
        if name == "get_brief":
            return build_brief(uid)
        if name == "create_calendar_event":
            if not gcal.is_configured():
                return {"error": "Google Calendar not connected. Click ‘Connect Google’ in the app."}
            start = inp["start"]; end = inp.get("end") or _plus_hour(start)
            ev = gcal.create_event(inp["title"], start, end,
                                   inp.get("description", "ATLAS"))
            now = dt.datetime.now().isoformat()
            with closing(db()) as c:
                c.execute("INSERT INTO items(user_id,type,title,priority,deadline,"
                          "scheduled_start,scheduled_end,google_event_id,created_at)"
                          " VALUES(?,'event',?,'important',?,?,?,?,?)",
                          (uid, inp["title"], start, start, end, ev["id"], now))
                c.commit()
            return {"created": ev}
        if name == "list_calendar_events":
            if not gcal.is_configured():
                return {"error": "Google Calendar not connected."}
            return gcal.list_events(inp.get("days", 7))
        if name == "check_email":
            if not gcal.is_configured():
                return {"error": "Google account not connected."}
            return gmail.list_recent(inp.get("query", "is:unread newer_than:7d"),
                                     inp.get("max", 8))
        if name == "draft_email":
            if not gcal.is_configured():
                return {"error": "Google account not connected."}
            return gmail.create_draft(inp["to"], inp["subject"], inp["body"])
        if name == "send_email":
            if not gcal.is_configured():
                return {"error": "Google account not connected."}
            return gmail.send_email(inp["to"], inp["subject"], inp["body"],
                                    inp.get("cc", ""), inp.get("bcc", ""))
        if name == "list_recordings":
            return list_recordings(uid)
        if name == "place_call":
            if not calls.is_configured():
                return {"error": calls.why_not_configured() or "Calling not connected."}
            rec = None
            if inp.get("recording_id"):
                rec = get_recording(uid, inp["recording_id"])
            elif inp.get("recording_name"):
                rec = find_recording_by_name(uid, inp["recording_name"])
            say = inp.get("say")
            if not rec and not say:
                return {"error": "Pick a saved recording or provide a message to say."}
            token = create_call_job(uid, inp["to"],
                                    rec["id"] if rec else None,
                                    None if rec else say)
            twiml_url = f"{calls.public_base()}/api/twiml/{token}"
            try:
                res = calls.place_call(inp["to"], twiml_url)
            except Exception as e:
                set_call_job(token, status="failed", error=str(e)[:300])
                return {"error": str(e)[:300]}
            set_call_job(token, status="dialing", call_sid=res.get("call_sid"))
            return {"calling": inp["to"], "status": res.get("status"),
                    "plays": rec["name"] if rec else f"message: {say[:60]}"}
        if name == "web_search":
            return web.search(inp["query"], inp.get("count", 6))
        if name == "web_read":
            return {"url": inp["url"], "text": web.read(inp["url"])}
        if name == "propose_purchase":
            now = dt.datetime.now().isoformat()
            with closing(db()) as c:
                c.execute("INSERT INTO items(user_id,type,title,priority,status,link,"
                          "source_text,created_at) VALUES(?,'purchase',?,'important',"
                          "'open',?,?,?)",
                          (uid, inp["title"], inp.get("url"), inp.get("reason", ""), now))
                c.commit()
            return {"saved": inp["title"], "open_url": inp.get("url"),
                    "note": "Saved to your purchases. Opening the store so YOU can "
                            "complete payment — ATLAS does not pay or check out."}
        if name == "remember":
            return add_memory(uid, inp["fact"])
        if name == "add_recurring":
            return add_recurring(uid, inp["title"], inp["freq"], inp.get("days"),
                                 inp.get("day_of_month"), inp.get("time"),
                                 priority=inp.get("priority", "routine"))
        if name == "list_recurring":
            return list_recurring(uid)
        if name == "update_item":
            return edit_item(uid, inp["id"], inp.get("title"), inp.get("priority"),
                             inp.get("deadline"), inp.get("scheduled_start"),
                             inp.get("est_minutes"))
        if name == "reschedule":
            reschedule_all(uid); return {"rescheduled": True}
        return {"error": f"unknown tool {name}"}
    except Exception as e:
        return {"error": str(e)}

CHAT_SYS = """You are ATLAS, {user}'s personal AI manager and chief-of-staff. \
Be warm, concise, and proactive — a real assistant, not a chatbot. Current \
datetime: {now} ({tz}).

What you know about {user} (long-term memory — use it naturally, don't recite it):
{memory}

Use your tools to ACTUALLY do things, don't just describe them:
- To plan/remember/get something done -> `capture` with structured items YOU
  extract: type (goal|task|commitment|event), short title, priority, and an ISO
  deadline (resolve "tomorrow"/"Friday" from the datetime above). For a goal,
  include 3-7 subtasks. Split a compound request into multiple items. Just call
  it — don't ask for confirmation.
- When {user} shares a durable fact or preference (their manager/teammates/family,
  habits, likes/dislikes, home or work details, ongoing goals) -> `remember` it.
- Anything that repeats ("every day/Monday/month", "daily", "each morning") ->
  `add_recurring`.
- To move/rename/reschedule a task, change its deadline, or make it urgent ->
  `list_items` for the id, then `update_item`. To re-plan the whole day ->
  `reschedule`.
- Anything to plan/remember/get done -> `capture` (it decomposes & schedules).
- A specific appointment at a specific time -> `create_calendar_event`.
- Questions about today/priorities -> `get_brief`; about tasks -> `list_items`;
  about the calendar -> `list_calendar_events`.
- "Check my email / any action items?" -> `check_email`, then `capture` real
  action items and deadlines you find.
- Asked to SEND an email/reply -> `send_email` (it goes out from their Gmail).
  The app shows an Approve/Cancel card before it actually sends, so just call the
  tool with a well-written subject and body; don't ask "should I send?" yourself.
  If the user only wants to review first, or seems unsure, use `draft_email`.
- "Call <person/number>" -> `place_call`. If they name a saved recording, pass
  `recording_name`; otherwise pass a short, natural `say` message to speak. Use
  `list_recordings` if you need to know what's saved. The app shows an
  Approve/Cancel card before dialling, so just call the tool. You never actually
  speak on the line — ATLAS plays the recording or the spoken message.
- "Find / compare / recommend / what's the best..." → `web_search`, then
  `web_read` on the 2-3 most promising results to check details, then give ONE
  clear recommendation with a short reason and the key alternatives. Offer to
  save it as a task or calendar event if useful.
- "Buy / order / get me..." → research first (web_search/web_read), then
  `propose_purchase` with your best pick. NEVER say you bought or ordered it —
  you cannot pay. Approving only opens the store for the user to check out
  themselves. Make that clear.

You are spoken to by VOICE and your replies are read aloud, so keep them short,
natural, and conversational — no markdown, bullet symbols, or long lists. After
acting, say plainly what you did. Resolve relative dates yourself."""

class ChatIn(BaseModel): messages: list[dict]

# ---- deploy helpers -------------------------------------------------------
def materialize_google_files():
    """Restore Google credentials/token to disk on boot so calendar_client can use
    them. Sources (in order): env vars (GOOGLE_CREDENTIALS_JSON / GOOGLE_TOKEN_JSON),
    then the durable DB copy saved when the user connects via the web button. This
    is what makes 'Connect Google' survive restarts on ephemeral-disk hosts."""
    cj, tj = os.getenv("GOOGLE_CREDENTIALS_JSON"), os.getenv("GOOGLE_TOKEN_JSON")
    try:
        if cj and not gcal.CREDS.exists(): gcal.CREDS.write_text(cj)
        if tj and not gcal.TOKEN.exists(): gcal.TOKEN.write_text(tj)
        if not gcal.TOKEN.exists():                    # fall back to durable DB copy
            saved = get_secret("google_token")
            if saved: gcal.TOKEN.write_text(saved)
    except Exception as e:
        print("google file materialize failed:", e)

# ---- api ------------------------------------------------------------------
app = FastAPI(title="ATLAS")
try:
    init_db()                      # tables first (app_secrets must exist before we read it)
    materialize_google_files()     # then restore Google creds/token (may read app_secrets)
except Exception as _e:
    # Never let a boot-time DB/init hiccup take the whole service down (would 404
    # everything). Log it; DB-backed routes surface the error per-request instead.
    print("STARTUP init error (continuing to serve):", repr(_e))

@app.on_event("startup")
def _startup_workers():
    """Proactive delivery loop always runs (web-push needs no token). The
    Telegram long-poller runs in-process too when a token is configured."""
    import threading
    threading.Thread(target=proactive_loop, daemon=True).start()
    if os.getenv("RUN_TELEGRAM_IN_WEB") == "1" and os.getenv("TELEGRAM_BOT_TOKEN"):
        import telegram_bot
        threading.Thread(target=telegram_bot.run, daemon=True).start()
        print("Telegram bot started.")

class CaptureIn(BaseModel): text: str

@app.post("/api/capture")
def capture(inp: CaptureIn, user=Depends(auth.current_user)):
    if not inp.text.strip(): raise HTTPException(400, "empty note")
    try:
        parsed = extract_items(inp.text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, friendly_ai_error(e))
    return {"created": store_items(parsed, inp.text, user["id"]), "items": parsed}

def friendly_ai_error(e) -> str:
    """Turn a Gemini SDK exception into a clear message for the user."""
    msg = str(e); low = msg.lower()
    if "gemini_api_key not set" in low or "api key not valid" in low or \
            "api_key_invalid" in low or "invalid api key" in low:
        return ("⚠️ The AI key is missing or invalid. Get a free Gemini key at "
                "aistudio.google.com/app/apikey, set GEMINI_API_KEY, and restart.")
    if "resource_exhausted" in low or "quota" in low or "429" in low or \
            "rate limit" in low:
        return ("⚠️ Gemini's free-tier limit was hit for the moment. Wait a bit and "
                "retry (there are per-minute request caps).")
    if "503" in low or "overloaded" in low or "unavailable" in low:
        return "⚠️ Gemini is busy right now. Please retry in a moment."
    return "⚠️ The AI request failed: " + msg[:200]

def run_agent(messages: list[dict], uid="local", name="there") -> dict:
    """Shared agent loop (Gemini function-calling) used by the web chat AND the
    Telegram bot. Returns {reply, actions, pending}; all tool actions scoped to uid."""
    from google.genai import types
    try:
        client = _genai()
    except Exception as e:
        return {"reply": friendly_ai_error(e), "actions": [], "pending": []}
    sys = CHAT_SYS.format(user=name, tz=gcal.TZ, memory=memory_block(uid),
                          now=dt.datetime.now().isoformat(timespec="minutes"))
    cfg = types.GenerateContentConfig(system_instruction=sys, tools=_gemini_tools(),
                                      temperature=0.3)
    contents = [types.Content(
        role=("model" if m["role"] == "assistant" else "user"),
        parts=[types.Part(text=str(m["content"]))]) for m in messages]
    actions = []
    for _ in range(8):   # room for multi-step research (search -> read -> answer)
        try:
            resp = _generate(model=MODEL, contents=contents, config=cfg)
        except Exception as e:
            return {"reply": friendly_ai_error(e), "actions": actions,
                    "pending": [a for a in actions if a["pending"]]}
        cand = resp.candidates[0] if resp.candidates else None
        pin = cand.content.parts if (cand and cand.content and cand.content.parts) else []
        fcs = [p.function_call for p in pin if getattr(p, "function_call", None)]
        if fcs:
            contents.append(cand.content)          # the model's tool-call turn
            replies = []
            for fc in fcs:
                args = dict(fc.args) if fc.args else {}
                out = run_tool(fc.name, args, uid)
                pend = isinstance(out, dict) and out.get("pending")
                actions.append({"tool": fc.name, "input": args, "pending": bool(pend)})
                payload = ({"status": "PROPOSED — awaiting the user's approval; NOT done"}
                           if pend else out)
                replies.append(types.Part.from_function_response(
                    name=fc.name, response={"result": payload}))
            contents.append(types.Content(role="user", parts=replies))
            continue
        text = "".join(p.text for p in pin if getattr(p, "text", None))
        return {"reply": text, "actions": actions,
                "pending": [a for a in actions if a["pending"]]}
    return {"reply": "I took several steps but stopped to avoid looping. "
            "Check your dashboard.", "actions": actions,
            "pending": [a for a in actions if a["pending"]]}

def display_name(user):
    return (user.get("email", "").split("@")[0] or "there") if user else "there"

@app.get("/api/config")
def config():
    cfg = auth.public_config(); cfg["push_key"] = push.public_key()
    cfg["calling_connected"] = calls.is_configured()
    cfg["db"] = "postgres" if store.is_postgres() else "sqlite"
    try:
        with closing(db()) as c:
            c.execute("SELECT 1")
        cfg["db_ok"] = True
    except Exception as e:
        cfg["db_ok"] = False; cfg["db_error"] = str(e)[:160]
    return cfg

class PushSub(BaseModel):
    subscription: dict

@app.post("/api/push-subscribe")
def push_subscribe(inp: PushSub, user=Depends(auth.current_user)):
    return save_push_sub(user["id"], inp.subscription)

@app.post("/api/push-test")
def push_test(user=Depends(auth.current_user)):
    notify_user(user["id"], "ATLAS", "Push notifications are working \U0001f389")
    return {"sent": True}

@app.post("/api/chat")
def chat(inp: ChatIn, user=Depends(auth.current_user)):
    return run_agent(inp.messages, user["id"], display_name(user))

@app.get("/api/items")
def api_items(status: str = "open", user=Depends(auth.current_user)):
    return list_items_for(user["id"], status)

@app.patch("/api/items/{item_id}")
def api_update(item_id: int, s: StatusIn, user=Depends(auth.current_user)):
    return set_status(user["id"], item_id, s.status)

@app.delete("/api/items/{item_id}")
def api_delete(item_id: int, user=Depends(auth.current_user)):
    return remove_item(user["id"], item_id)

@app.post("/api/replan")
def replan(user=Depends(auth.current_user)):
    reschedule_all(user["id"]); return {"ok": True}

class ExecIn(BaseModel):
    tool: str
    input: dict

@app.post("/api/execute")
def execute(inp: ExecIn, user=Depends(auth.current_user)):
    """Run a previously proposed action after the user approves it in the UI."""
    return {"result": run_tool(inp.tool, inp.input, user["id"], approved=True)}

class SettingsIn(BaseModel):
    settings: dict

@app.get("/api/settings")
def get_settings(user=Depends(auth.current_user)): return load_settings(user["id"])

@app.put("/api/settings")
def put_settings(s: SettingsIn, user=Depends(auth.current_user)):
    return save_settings(user["id"], s.settings)

@app.get("/api/brief")
def brief(user=Depends(auth.current_user)):
    return build_brief(user["id"], display_name(user))

@app.get("/api/memory")
def api_memory(user=Depends(auth.current_user)):
    return list_memory(user["id"])

@app.delete("/api/memory/{mid}")
def api_forget(mid: int, user=Depends(auth.current_user)):
    return forget_memory(user["id"], mid)

@app.post("/api/link-code")
def api_link_code(user=Depends(auth.current_user)):
    """Generate a code the user sends to the Telegram bot as `/link CODE`."""
    return {"code": make_link_code(user["id"])}

@app.get("/api/recurring")
def api_recurring(user=Depends(auth.current_user)):
    return list_recurring(user["id"])

@app.delete("/api/recurring/{rid}")
def api_remove_recurring(rid: int, user=Depends(auth.current_user)):
    return remove_recurring(user["id"], rid)

# ---- recordings library + calling -----------------------------------------
class RecordingIn(BaseModel):
    name: str
    kind: str = "tts"          # 'tts' | 'audio'
    text: str | None = None
    audio_b64: str | None = None
    mime: str | None = None

@app.get("/api/recordings")
def api_recordings(user=Depends(auth.current_user)):
    return list_recordings(user["id"])

@app.post("/api/recordings")
def api_add_recording(inp: RecordingIn, user=Depends(auth.current_user)):
    return add_recording(user["id"], inp.name.strip() or "Untitled", inp.kind,
                         text=inp.text, audio_b64=inp.audio_b64, mime=inp.mime)

@app.delete("/api/recordings/{rid}")
def api_delete_recording(rid: int, user=Depends(auth.current_user)):
    return delete_recording(user["id"], rid)

@app.get("/api/calls")
def api_calls(user=Depends(auth.current_user)):
    return list_call_history(user["id"])

@app.get("/media/recordings/{media_token}")
def media_recording(media_token: str):
    """Public (Twilio + the in-app player fetch this) — token is unguessable."""
    from fastapi.responses import Response
    got = recording_audio(media_token)
    if not got:
        raise HTTPException(404, "not found")
    audio, mime = got
    return Response(content=audio, media_type=mime)

@app.api_route("/api/twiml/{token}", methods=["GET", "POST"])
def twiml(token: str):
    """Twilio calls this when the person answers — returns what to play/say."""
    from fastapi.responses import Response
    job = get_call_job(token)
    if not job:
        return Response(calls.twiml_say("This call could not be set up. Goodbye."),
                        media_type="application/xml")
    if job.get("recording_id"):
        rec = get_recording(job["user_id"], job["recording_id"])
        if rec and rec.get("kind") == "audio" and rec.get("media_token"):
            url = f"{calls.public_base()}/media/recordings/{rec['media_token']}"
            return Response(calls.twiml_play(url), media_type="application/xml")
        if rec and rec.get("text"):
            return Response(calls.twiml_say(rec["text"]), media_type="application/xml")
    return Response(calls.twiml_say(job.get("say_text") or "Hello from ATLAS."),
                    media_type="application/xml")

# ---- Google connect (web OAuth, works on Render unlike local authorize.py) ---
def _oauth_base(request: Request) -> str:
    """The public origin to build redirect URIs from. Prefer PUBLIC_BASE_URL (set
    on Render); fall back to the request's own origin for local use."""
    return calls.public_base() or str(request.base_url).rstrip("/")

def _google_flow(base: str):
    from google_auth_oauthlib.flow import Flow
    if not gcal.CREDS.exists():
        raise HTTPException(400, "Google client not set up (credentials.json missing).")
    return Flow.from_client_secrets_file(
        str(gcal.CREDS), scopes=gcal.SCOPES,
        redirect_uri=f"{base}/api/google/callback")

@app.get("/api/google/start")
def google_start(request: Request):
    """Kick off Google consent. Full-page redirect to Google's sign-in."""
    base = _oauth_base(request)
    try:
        flow = _google_flow(base)
        url, _state = flow.authorization_url(
            access_type="offline", prompt="consent", include_granted_scopes="true")
    except HTTPException:
        raise
    except Exception as e:
        return RedirectResponse(f"/?google=error&msg={_q(str(e)[:120])}")
    return RedirectResponse(url)

@app.get("/api/google/callback")
def google_callback(request: Request):
    """Google redirects back here with a code; exchange it and save the token
    (to disk for immediate use + to the DB so it survives restarts)."""
    base = _oauth_base(request)
    try:
        flow = _google_flow(base)
        # Rebuild the authorization response against the public base so the scheme
        # matches the registered redirect_uri even behind Render's proxy.
        authz = f"{base}{request.url.path}"
        if request.url.query:
            authz += f"?{request.url.query}"
        flow.fetch_token(authorization_response=authz)
        tok = flow.credentials.to_json()
        gcal.TOKEN.write_text(tok)
        set_secret("google_token", tok)
    except Exception as e:
        return RedirectResponse(f"/?google=error&msg={_q(str(e)[:160])}")
    return RedirectResponse("/?google=connected")

@app.get("/")
def index(): return FileResponse(FRONTEND / "index.html")

# Served from root so the PWA service worker controls the whole origin ("/").
@app.get("/sw.js")
def service_worker():
    return FileResponse(FRONTEND / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/",
                                 "Cache-Control": "no-cache"})

@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(FRONTEND / "manifest.webmanifest",
                        media_type="application/manifest+json")

if (FRONTEND / "index.html").exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
