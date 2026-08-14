"""Google Calendar integration for ATLAS.

Auth model: OAuth2 "installed app". The user runs `authorize.py` ONCE to grant
access in their browser -> token.json is saved. This module then loads/refreshes
that token. If token.json is absent, calendar features degrade gracefully
(is_configured() -> False) and ATLAS stays fully usable in local-only mode.
"""
import os, datetime as dt
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

BASE = Path(__file__).parent
CREDS = BASE / "credentials.json"      # from Google Cloud (OAuth client)
TOKEN = BASE / "token.json"            # created by authorize.py
# Shared Google scopes for all ATLAS integrations (calendar + gmail).
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",   # read inbox
    "https://www.googleapis.com/auth/gmail.compose",    # create DRAFTS (never auto-send)
]
CAL_ID = os.getenv("ATLAS_CALENDAR_ID", "primary")
TZ = os.getenv("ATLAS_TZ", "Asia/Kolkata")
TZINFO = ZoneInfo(TZ) if ZoneInfo else dt.timezone(dt.timedelta(hours=5, minutes=30))


def is_configured() -> bool:
    return TOKEN.exists()


def credentials():
    """Load + refresh stored Google credentials (shared by calendar + gmail)."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json())
        else:
            raise RuntimeError("Google auth invalid — re-run authorize.py")
    return creds


def _service():
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=credentials(), cache_discovery=False)


def _to_naive_local(s: str) -> dt.datetime:
    """Parse a Google date/dateTime string into a naive local datetime."""
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo:
        d = d.astimezone(TZINFO).replace(tzinfo=None)
    return d


def create_event(title: str, start: str, end: str, description: str = "") -> dict:
    """start/end are naive local ISO strings (e.g. 2026-08-14T17:30). Google
    interprets them in TZ."""
    ev = _service().events().insert(calendarId=CAL_ID, body={
        "summary": title,
        "description": description,
        "start": {"dateTime": start, "timeZone": TZ},
        "end": {"dateTime": end, "timeZone": TZ},
    }).execute()
    return {"id": ev["id"], "link": ev.get("htmlLink"), "title": title,
            "start": start, "end": end}


def list_events(days: int = 7) -> list[dict]:
    now = dt.datetime.now(TZINFO)
    res = _service().events().list(
        calendarId=CAL_ID, timeMin=now.isoformat(),
        timeMax=(now + dt.timedelta(days=days)).isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=50).execute()
    out = []
    for e in res.get("items", []):
        s = e["start"].get("dateTime") or e["start"].get("date")
        en = e["end"].get("dateTime") or e["end"].get("date")
        out.append({"id": e["id"], "title": e.get("summary", "(busy)"),
                    "start": s, "end": en})
    return out


def busy_intervals(start: dt.datetime, end: dt.datetime) -> list[tuple]:
    """Naive-local (start,end) tuples of real calendar events in the window.
    Used by the planner so it schedules around actual meetings."""
    res = _service().events().list(
        calendarId=CAL_ID, timeMin=start.replace(tzinfo=TZINFO).isoformat(),
        timeMax=end.replace(tzinfo=TZINFO).isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=100).execute()
    out = []
    for e in res.get("items", []):
        s = e["start"].get("dateTime") or e["start"].get("date")
        en = e["end"].get("dateTime") or e["end"].get("date")
        try:
            out.append((_to_naive_local(s), _to_naive_local(en)))
        except (TypeError, ValueError):
            pass
    return out


def delete_event(event_id: str):
    _service().events().delete(calendarId=CAL_ID, eventId=event_id).execute()
