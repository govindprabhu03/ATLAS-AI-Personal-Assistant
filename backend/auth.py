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
    # Auth is on whenever the project is configured; we can validate tokens via
    # the JWT secret OR by asking Supabase directly (works regardless of the
    # project's signing scheme).
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)

def public_config() -> dict:
    return {"supabase_url": SUPABASE_URL, "supabase_anon_key": SUPABASE_ANON_KEY,
            "auth_required": auth_enabled()}

def _verify_via_supabase(token: str):
    """Validate a token by asking Supabase's auth server. Works for any valid
    Supabase session token, whatever the project's JWT signing method."""
    import requests
    r = requests.get(f"{SUPABASE_URL}/auth/v1/user", timeout=8,
                     headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON_KEY})
    if r.status_code == 200:
        u = r.json()
        return {"id": u.get("id"), "email": u.get("email", "")}
    return None

def current_user(request: Request) -> dict:
    """FastAPI dependency → {'id':..., 'email':...}. In local mode returns the
    'local' user. Otherwise verifies the Supabase token — first with the JWT
    secret (fast), then by asking Supabase (robust fallback)."""
    if not auth_enabled():
        return {"id": "local", "email": "you@local"}
    hdr = request.headers.get("authorization", "")
    if not hdr.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in required")
    token = hdr.split(" ", 1)[1].strip()
    # 1) fast path: HS256 verify with the project's JWT secret (if it's correct)
    if SUPABASE_JWT_SECRET:
        try:
            import jwt
            p = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"],
                           audience="authenticated")
            if p.get("sub"):
                return {"id": p["sub"], "email": p.get("email", "")}
        except Exception:
            pass
    # 2) robust fallback: let Supabase validate the token for us
    try:
        u = _verify_via_supabase(token)
        if u and u.get("id"):
            return u
    except Exception as e:
        print("supabase token check failed:", e)
    raise HTTPException(401, "Invalid or expired session")
