"""One-time Google Calendar authorization for ATLAS.

Run once after placing credentials.json (from Google Cloud) in this folder:

    python authorize.py

A browser window opens; sign in and click Allow. This writes token.json, which
ATLAS then uses. Re-run only if you revoke access or delete token.json.
"""
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from calendar_client import CREDS, TOKEN, SCOPES

def main():
    if not Path(CREDS).exists():
        raise SystemExit(
            f"Missing {CREDS}. Download your OAuth client (Desktop app) from "
            "Google Cloud Console and save it there first.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    Path(TOKEN).write_text(creds.to_json())
    print(f"Success. Saved {TOKEN}. ATLAS can now read/write your calendar.")

if __name__ == "__main__":
    main()
