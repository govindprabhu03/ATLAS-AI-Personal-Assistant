"""Authentication for ATLAS.

Multi-user mode uses Supabase Auth: the frontend signs the user in with
supabase-js and sends the access token (a JWT) as `Authorization: Bearer <jwt>`.
This module verifies that JWT with the project's JWT secret and returns the
user's id + email, so all data can be isolated per account.

If Supabase is not configured (no SUPABASE_JWT_SECRET), ATLAS runs in single-user
"local" mode — handy for local dev — and every request maps to the 'local' user.
"""
import os
from fastapi import Request, HTTPException

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")

def auth_enabled() -> bool:
    return bool(SUPABASE_JWT_SECRET)

def public_config() -> dict:
    return {"supabase_url": SUPABASE_URL, "supabase_anon_key": SUPABASE_ANON_KEY,
            "auth_required": auth_enabled()}

def current_user(request: Request) -> dict:
    """FastAPI dependency → {'id':..., 'email':...}. Raises 401 if auth is on and
    the token is missing/invalid. In local mode returns the 'local' user."""
    if not auth_enabled():
        return {"id": "local", "email": "you@local"}
    hdr = request.headers.get("authorization", "")
    if not hdr.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in required")
    token = hdr.split(" ", 1)[1].strip()
    try:
        import jwt
        payload = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"],
                             audience="authenticated")
    except Exception:
        raise HTTPException(401, "Invalid or expired session")
    uid = payload.get("sub")
    if not uid:
        raise HTTPException(401, "Invalid session")
    return {"id": uid, "email": payload.get("email", "")}
