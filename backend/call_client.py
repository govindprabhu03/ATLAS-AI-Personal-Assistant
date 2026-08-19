"""Phone-calling for ATLAS via Twilio's REST API (no SDK — uses `requests`).

ATLAS can place a real phone call when the user orders one, and play either an
uploaded recording or a spoken (TTS) message. Calling is APPROVAL-GATED in the
app: the agent proposes the call, the user clicks Approve, then it dials.

Config (env / ATLAS/.env):
    TWILIO_ACCOUNT_SID   - from console.twilio.com
    TWILIO_AUTH_TOKEN    - from console.twilio.com
    TWILIO_FROM_NUMBER   - a Twilio number you own, E.164 (e.g. +12025550123)
    PUBLIC_BASE_URL      - a PUBLIC https URL where this app is reachable, so
                           Twilio can fetch the call's TwiML + audio
                           (your Render deploy, or an ngrok tunnel over :8077).

Provider is intentionally isolated here so it can be swapped (Plivo, Exotel…)
without touching the rest of ATLAS.
"""
import os
import requests

API = "https://api.twilio.com/2010-04-01"


def _cfg():
    return (os.getenv("TWILIO_ACCOUNT_SID", ""),
            os.getenv("TWILIO_AUTH_TOKEN", ""),
            os.getenv("TWILIO_FROM_NUMBER", ""))


def public_base() -> str:
    return os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


def is_configured() -> bool:
    sid, tok, frm = _cfg()
    return bool(sid and tok and frm and public_base())


def why_not_configured() -> str:
    sid, tok, frm = _cfg()
    missing = [n for n, v in [("TWILIO_ACCOUNT_SID", sid), ("TWILIO_AUTH_TOKEN", tok),
                              ("TWILIO_FROM_NUMBER", frm),
                              ("PUBLIC_BASE_URL", public_base())] if not v]
    return "Calling not set up — missing: " + ", ".join(missing) if missing else ""


def place_call(to: str, twiml_url: str) -> dict:
    """Dial `to`; when answered, Twilio fetches `twiml_url` for what to say/play.
    Returns Twilio's call sid + status. Raises on API error."""
    sid, tok, frm = _cfg()
    if not is_configured():
        raise RuntimeError(why_not_configured())
    resp = requests.post(
        f"{API}/Accounts/{sid}/Calls.json",
        data={"To": to, "From": frm, "Url": twiml_url, "Method": "GET"},
        auth=(sid, tok), timeout=20)
    if resp.status_code >= 300:
        # surface Twilio's own message (e.g. unverified number on trial)
        try:
            msg = resp.json().get("message", resp.text)
        except Exception:
            msg = resp.text
        raise RuntimeError(f"Twilio error {resp.status_code}: {msg}")
    j = resp.json()
    return {"call_sid": j.get("sid"), "status": j.get("status"), "to": to}


def call_status(call_sid: str) -> dict:
    sid, tok, _ = _cfg()
    resp = requests.get(f"{API}/Accounts/{sid}/Calls/{call_sid}.json",
                        auth=(sid, tok), timeout=15)
    j = resp.json()
    return {"status": j.get("status"), "duration": j.get("duration")}


def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def twiml_play(audio_url: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Response><Play>{xml_escape(audio_url)}</Play></Response>')


def twiml_say(text: str, voice: str = "Polly.Aditi") -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Response><Say voice="{voice}">{xml_escape(text)}</Say></Response>')
