"""Web-push (VAPID) for ATLAS — proactive browser notifications, even when the
PWA is closed. No external credentials: a VAPID keypair is generated on first
run and cached to vapid.json (or provide VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY via
env for a fixed pair across deploys).
"""
import os, json, base64
from pathlib import Path

VAPID_FILE = Path(__file__).parent / "vapid.json"
SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:atlas@example.com")
_KEYS = None


def _gen_keys():
    from py_vapid import Vapid
    from cryptography.hazmat.primitives import serialization
    v = Vapid()
    v.generate_keys()
    priv_pem = v.private_pem().decode() if isinstance(v.private_pem(), bytes) else v.private_pem()
    raw = v.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    pub = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return {"private_pem": priv_pem, "public": pub}


def keys():
    global _KEYS
    if _KEYS:
        return _KEYS
    env_priv, env_pub = os.getenv("VAPID_PRIVATE_KEY"), os.getenv("VAPID_PUBLIC_KEY")
    if env_priv and env_pub:
        _KEYS = {"private_pem": env_priv.replace("\\n", "\n"), "public": env_pub}
        return _KEYS
    if VAPID_FILE.exists():
        _KEYS = json.loads(VAPID_FILE.read_text()); return _KEYS
    try:
        _KEYS = _gen_keys()
        VAPID_FILE.write_text(json.dumps(_KEYS))
    except Exception as e:
        print("VAPID key generation failed (web-push disabled):", e)
        _KEYS = None
    return _KEYS


def public_key():
    k = keys()
    return k["public"] if k else ""


def is_enabled():
    return bool(keys())


def send(subscription: dict, title: str, body: str) -> bool:
    k = keys()
    if not k:
        return False
    try:
        from pywebpush import webpush, WebPushException
        webpush(subscription_info=subscription,
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=k["private_pem"],
                vapid_claims={"sub": SUBJECT})
        return True
    except Exception as e:
        print("web-push send failed:", e)
        return False
