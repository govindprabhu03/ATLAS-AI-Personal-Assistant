"""ATLAS on Telegram — message your assistant from your phone.

Uses long-polling (getUpdates), so it needs NO public URL, webhook, or hosting —
just a bot token from @BotFather. It shares ATLAS's database and agent with the
web app, so anything you do here shows up on the dashboard and vice versa.

Setup:
  1. In Telegram, open @BotFather -> /newbot -> follow prompts -> copy the token.
  2. Put it in ATLAS/.env  ->  TELEGRAM_BOT_TOKEN=123456:ABC...
  3. Run:  python telegram_bot.py
  4. Open your bot in Telegram and say hi.

Permission-gated actions (calendar events, email drafts) arrive as Approve /
Cancel buttons — nothing outward happens until you tap Approve.
"""
import os, time, json, requests
import main

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API = f"https://api.telegram.org/bot{TOKEN}"
HIST: dict[int, list] = {}      # chat_id -> conversation history
PENDING: dict[str, tuple] = {}  # token -> (chat_id, tool, input)
_pid = 0


def describe(tool, inp):
    if tool == "create_calendar_event":
        return f"📅 {inp.get('title','')} · {str(inp.get('start','')).replace('T',' ')}"
    if tool == "draft_email":
        return f"✉️ draft to {inp.get('to','?')} · {inp.get('subject','')}"
    if tool == "propose_purchase":
        p = f" · {inp['price']}" if inp.get("price") else ""
        return f"🛒 {inp.get('title','')}{p} (ATLAS won't pay — opens the store for you)"
    return f"{tool}: {json.dumps(inp)[:120]}"


def send(chat_id, text, buttons=None):
    data = {"chat_id": chat_id, "text": text}
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        requests.post(f"{API}/sendMessage", json=data, timeout=20)
    except Exception as e:
        print("send error:", e)


def edit(chat_id, msg_id, text):
    try:
        requests.post(f"{API}/editMessageText",
                      json={"chat_id": chat_id, "message_id": msg_id, "text": text},
                      timeout=20)
    except Exception as e:
        print("edit error:", e)


def handle_message(chat_id, text, name="there"):
    t = text.strip()
    low = t.lower()
    # link this chat to a web account
    if low.startswith("/link"):
        parts = t.split()
        if len(parts) < 2:
            send(chat_id, "To connect your ATLAS account, open the web app → ⚙ → "
                          "Connect Telegram, then send me: /link YOURCODE")
            return
        uid = main.resolve_link_code(parts[1], chat_id, name)
        send(chat_id, "✅ Connected! I'll now use your account and can send you your "
                      "daily brief here." if uid else "That code is invalid or expired — "
                      "get a fresh one from the web app.")
        return
    uid = main.user_for_chat(chat_id)
    if low in ("/start", "/hi", "hi", "hello"):
        linked = uid != "local"
        send(chat_id, f"Hi {name} — I'm ATLAS, your assistant. Tell me what to plan, "
                      "schedule, research, or check. Say /brief for today's rundown."
                      + ("" if linked else "\n\nTip: connect your account via the web "
                         "app (⚙ → Connect Telegram) so your tasks sync."))
        return
    if low == "/brief":
        send(chat_id, main.brief_text(uid, name))
        return
    hist = HIST.setdefault(chat_id, [])
    hist.append({"role": "user", "content": text})
    try:
        res = main.run_agent(hist, uid, name)
    except Exception as e:
        send(chat_id, f"Something went wrong: {e}")
        hist.pop()
        return
    hist.append({"role": "assistant", "content": res.get("reply") or ""})
    HIST[chat_id] = hist[-20:]                    # keep context bounded

    if res.get("reply"):
        send(chat_id, res["reply"])
    global _pid
    for a in res.get("pending", []):
        _pid += 1; tok = str(_pid)
        PENDING[tok] = (chat_id, a["tool"], a["input"])
        send(chat_id, f"Approve this?\n{describe(a['tool'], a['input'])}",
             buttons=[[{"text": "✅ Approve", "callback_data": f"ok:{tok}"},
                       {"text": "✖ Cancel", "callback_data": f"no:{tok}"}]])


def handle_callback(cb_id, chat_id, data, msg_id):
    requests.post(f"{API}/answerCallbackQuery",
                  json={"callback_query_id": cb_id}, timeout=20)
    action, _, tok = data.partition(":")
    p = PENDING.pop(tok, None)
    if not p:
        edit(chat_id, msg_id, "⚠ This request expired — ask me again.")
        return
    _, tool, inp = p
    if action == "ok":
        out = main.run_tool(tool, inp, main.user_for_chat(chat_id), approved=True)
        if isinstance(out, dict) and out.get("error"):
            edit(chat_id, msg_id, f"⚠ {describe(tool, inp)}\n{out['error']}")
        elif isinstance(out, dict) and out.get("open_url"):   # purchase hand-off
            edit(chat_id, msg_id, f"✅ Saved · {describe(tool, inp)}")
            send(chat_id, f"Open to complete payment yourself:\n{out['open_url']}")
        else:
            edit(chat_id, msg_id, f"✅ Done · {describe(tool, inp)}")
    else:
        edit(chat_id, msg_id, f"✖ Cancelled · {describe(tool, inp)}")


def run():
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in ATLAS/.env (get it from @BotFather).")
    me = requests.get(f"{API}/getMe", timeout=20).json()
    print("ATLAS Telegram bot online as @%s" % me.get("result", {}).get("username", "?"))
    offset = None
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"timeout": 30, "offset": offset}, timeout=40).json()
        except Exception as e:
            print("poll error:", e); time.sleep(3); continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            try:
                if "message" in u and "text" in u["message"]:
                    m = u["message"]
                    fname = (m.get("from") or {}).get("first_name", "there")
                    handle_message(m["chat"]["id"], m["text"], fname)
                elif "callback_query" in u:
                    cq = u["callback_query"]
                    handle_callback(cq["id"], cq["message"]["chat"]["id"],
                                    cq["data"], cq["message"]["message_id"])
            except Exception as e:
                print("update error:", e)


if __name__ == "__main__":
    run()
