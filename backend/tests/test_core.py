"""ATLAS core logic tests (offline: SQLite, no network/LLM).

Run:  cd backend && python -m pytest -q
"""
import os, tempfile, datetime as dt

os.environ["ATLAS_DB"] = os.path.join(tempfile.gettempdir(), "atlas_pytest.db")
os.environ.pop("DATABASE_URL", None)                 # force SQLite for tests
if os.path.exists(os.environ["ATLAS_DB"]):
    os.remove(os.environ["ATLAS_DB"])

import main  # noqa: E402
main.init_db()


def _clear():
    with main.closing(main.db()) as c:
        for t in ("items", "memory", "recurring", "user_settings",
                  "telegram_links", "link_codes", "notified", "push_subs"):
            c.execute(f"DELETE FROM {t}")
        c.commit()


def test_per_user_isolation():
    _clear()
    main.store_items([{"type": "task", "title": "A task", "priority": "routine"}], "t", "alice")
    main.store_items([{"type": "task", "title": "B task", "priority": "routine"}], "t", "bob")
    assert [i["title"] for i in main.list_items_for("alice")] == ["A task"]
    assert [i["title"] for i in main.list_items_for("bob")] == ["B task"]


def test_priority_scheduling_and_duration():
    _clear()
    main.store_items([{"type": "task", "title": "routine", "priority": "routine"},
                      {"type": "task", "title": "critical", "priority": "critical"}], "t", "u")
    order = [i["title"] for i in sorted(main.list_items_for("u"),
                                        key=lambda x: x["scheduled_start"] or "")]
    assert order[0] == "critical"                    # highest priority first
    it = main.list_items_for("u")[0]
    main.edit_item("u", it["id"], est_minutes=120)
    main.reschedule_all("u")
    row = [i for i in main.list_items_for("u") if i["id"] == it["id"]][0]
    span = (main.parse_dt(row["scheduled_end"]) - main.parse_dt(row["scheduled_start"]))
    assert span == dt.timedelta(minutes=120)         # duration honoured


def test_edit_item():
    _clear()
    main.store_items([{"type": "task", "title": "old", "priority": "routine"}], "t", "u")
    i = main.list_items_for("u")[0]
    main.edit_item("u", i["id"], title="new", priority="critical")
    r = main.list_items_for("u")[0]
    assert r["title"] == "new" and r["priority"] == "critical"


def test_memory_recall_block():
    _clear()
    main.add_memory("u", "Manager is Rahul")
    assert "Rahul" in main.memory_block("u")
    main.forget_memory("u", "Rahul")
    assert "Rahul" not in main.memory_block("u")


def test_recurring_rules_and_idempotency():
    _clear()
    mon = dt.date(2026, 8, 17)                        # a Monday
    assert main._rule_matches("weekly:MON", mon)
    assert main._rule_matches("monthly:17", mon)
    assert main._rule_matches("daily", mon)
    assert not main._rule_matches("weekly:TUE", mon)
    main.add_recurring("u", "Standup", "daily", at_time="09:30")
    n1 = len(main.list_items_for("u"))
    main.spawn_recurring("u")                         # again -> no duplicates
    assert len(main.list_items_for("u")) == n1


def test_telegram_link_code():
    _clear()
    code = main.make_link_code("web-user")
    assert main.user_for_chat("chat9") == "local"
    assert main.resolve_link_code(code, "chat9", "Gov") == "web-user"
    assert main.user_for_chat("chat9") == "web-user"
    assert main.resolve_link_code("BADCODE", "c2", "x") is None


def test_per_user_settings_and_approval():
    _clear()
    assert main.needs_approval("draft_email", "u") is True       # default ask
    main.save_settings("u", {"draft_email": "auto"})
    assert main.needs_approval("draft_email", "u") is False
    assert main.needs_approval("draft_email", "other") is True   # isolated
    assert main.needs_approval("propose_purchase", "u") is True  # always ask


def test_push_subscribe():
    _clear()
    main.save_push_sub("u", {"endpoint": "https://x/1", "keys": {}})
    assert len(main._push_subs("u")) == 1
    assert "u" in main._notifiable_users()


def test_tools_registered():
    names = {t["name"] for t in main.TOOLS}
    for expected in ("capture", "update_item", "reschedule", "remember",
                     "add_recurring", "propose_purchase"):
        assert expected in names
