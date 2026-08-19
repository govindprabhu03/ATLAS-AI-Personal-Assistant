"""Gmail integration for ATLAS. Shares Google auth with calendar_client.

Read-only inbox access + DRAFT creation. ATLAS never sends mail automatically —
it prepares a draft in the user's Gmail for them to review and send.
"""
import base64
from email.mime.text import MIMEText

import calendar_client as ga


def _service():
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=ga.credentials(), cache_discovery=False)


def list_recent(query: str = "is:unread newer_than:7d", max_results: int = 8) -> list[dict]:
    """Recent messages matching a Gmail search query (sender/subject/snippet)."""
    svc = _service()
    res = svc.users().messages().list(
        userId="me", q=query, maxResults=max_results).execute()
    out = []
    for m in res.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"]).execute()
        h = {x["name"]: x["value"] for x in msg["payload"]["headers"]}
        out.append({"id": m["id"], "from": h.get("From", ""),
                    "subject": h.get("Subject", ""), "date": h.get("Date", ""),
                    "snippet": msg.get("snippet", "")})
    return out


def create_draft(to: str, subject: str, body: str) -> dict:
    """Create a Gmail draft (never sends). Returns the draft id."""
    mime = MIMEText(body)
    mime["to"] = to
    mime["subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    d = _service().users().drafts().create(
        userId="me", body={"message": {"raw": raw}}).execute()
    return {"draft_id": d["id"], "to": to, "subject": subject,
            "note": "Draft saved in Gmail — review and send it yourself."}


def send_email(to: str, subject: str, body: str, cc: str = "", bcc: str = "") -> dict:
    """Actually send an email from the user's Gmail. Gated behind approval in the
    app (ATLAS proposes it and only sends after the user clicks Approve)."""
    mime = MIMEText(body)
    mime["to"] = to
    mime["subject"] = subject
    if cc:
        mime["cc"] = cc
    if bcc:
        mime["bcc"] = bcc
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    sent = _service().users().messages().send(
        userId="me", body={"raw": raw}).execute()
    return {"sent": True, "message_id": sent.get("id"), "to": to,
            "subject": subject, "note": "Email sent from your Gmail."}
