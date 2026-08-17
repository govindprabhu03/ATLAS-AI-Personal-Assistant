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

from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import calendar_client as gcal
import gmail_client as gmail
import web_client as web
import auth
import store

# ---- config ---------------------------------------------------------------
MODEL = os.getenv("ATLAS_MODEL", "gemini-flash-latest")   # Google Gemini (free tier)
WORK_START, WORK_END = 9, 21
BLOCK_MINUTES = 60
FRONTEND = Path(__file__).parent.parent / "frontend"

# ---- permission tiers -----------------------------------------------------
# Outward/side-effecting tools default to "ask" (require user approval).
# Levels: "auto" (do it) | "ask" (propose, wait for Approve in the UI).
DEFAULT_SETTINGS = {"create_calendar_event": "ask", "draft_email": "ask",
                    "tz": os.getenv("ATLAS_TZ", "Asia/Kolkata"), "avatar_url": ""}
# Purchases ALWAYS require approval and can never be set to auto — ATLAS never pays.
ALWAYS_ASK = {"propose_purchase"}

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

def parse_dt(x):
    try: return dt.datetime.fromisoformat(x) if x else None
    except (TypeError, ValueError): return None

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
    """generate_content with retry/backoff for Gemini's transient free-tier errors."""
    import time
    last = None
    for attempt in range(3):
        try:
            return _genai().models.generate_content(**kwargs)
        except Exception as e:
            last = e
            if any(x in str(e).lower() for x in _TRANSIENT):
                time.sleep(1.2 * (attempt + 1)); continue
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

def proactive_loop():
    """Background: message each linked Telegram chat their morning brief (at their
    tz's brief hour, once/day) and any new overdue nudges."""
    import time, telegram_bot
    from zoneinfo import ZoneInfo
    print("ATLAS proactive loop started.")
    while True:
        try:
            for link in linked_chats():
                uid, chat = link["user_id"], link["chat_id"]
                name = link.get("name") or "there"
                s = load_settings(uid)
                try:
                    now = dt.datetime.now(ZoneInfo(s.get("tz", "Asia/Kolkata")))
                except Exception:
                    now = dt.datetime.now()
                hour = int(s.get("brief_hour", 8))
                daykey = "brief-" + now.date().isoformat()
                if now.hour == hour and not _notified(uid, daykey):
                    telegram_bot.send(chat, brief_text(uid, name)); _mark_notified(uid, daykey)
                for n in due_nudges(uid):
                    nk = "nudge-" + now.date().isoformat() + "-" + n[:40]
                    if not _notified(uid, nk):
                        telegram_bot.send(chat, "🔔 " + n); _mark_notified(uid, nk)
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
 {"name": "capture", "description": "Extract & store goals/tasks/commitments/"
  "events from natural language, decompose goals into subtasks, and schedule "
  "them into free time (auto-syncs to Google Calendar). Use for anything the "
  "user wants to remember, plan, or get done.",
  "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                   "required": ["text"]}},
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
  "Gmail for them to review and send. NEVER sends automatically.",
  "input_schema": {"type": "object", "properties": {
      "to": {"type": "string"}, "subject": {"type": "string"},
      "body": {"type": "string"}}, "required": ["to", "subject", "body"]}},
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
            parsed = extract_items(inp["text"])
            return {"created": len(store_items(parsed, inp["text"], uid)), "items": parsed}
        if name == "list_items":
            return list_items_for(uid)
        if name == "complete_item":
            return set_status(uid, inp["id"], "done")
        if name == "get_brief":
            return build_brief(uid)
        if name == "create_calendar_event":
            if not gcal.is_configured():
                return {"error": "Google Calendar not connected. Run authorize.py."}
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
- Asked to reply/email someone -> `draft_email` (creates a DRAFT only; tell the
  user to review and send it — you never send mail yourself).
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
    """On a server there's no interactive OAuth. Instead, provide the JSON
    contents via env vars (GOOGLE_CREDENTIALS_JSON / GOOGLE_TOKEN_JSON, generated
    locally by authorize.py) and we write them to disk on boot."""
    cj, tj = os.getenv("GOOGLE_CREDENTIALS_JSON"), os.getenv("GOOGLE_TOKEN_JSON")
    try:
        if cj and not gcal.CREDS.exists(): gcal.CREDS.write_text(cj)
        if tj and not gcal.TOKEN.exists(): gcal.TOKEN.write_text(tj)
    except Exception as e:
        print("google file materialize failed:", e)

# ---- api ------------------------------------------------------------------
app = FastAPI(title="ATLAS")
materialize_google_files()
init_db()

@app.on_event("startup")
def _maybe_start_telegram():
    """Optionally run the Telegram long-poller in-process so a single web
    service serves both the app and the bot (set RUN_TELEGRAM_IN_WEB=1)."""
    if os.getenv("RUN_TELEGRAM_IN_WEB") == "1" and os.getenv("TELEGRAM_BOT_TOKEN"):
        import threading, telegram_bot
        threading.Thread(target=telegram_bot.run, daemon=True).start()
        threading.Thread(target=proactive_loop, daemon=True).start()
        print("Telegram bot + proactive loop started.")

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
def config(): return auth.public_config()

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
