"""Offline UI tests (stdlib only; telegram / motor / aiohttp are replaced by light stubs).

Run:  python3 tests/test_ui.py
Checks: every button callback resolves to a registered action, callback data <= 64 bytes, two buttons per row at
most, BACK / CLOSE / CANCEL are full width, panel labels are CAPS, owner names keep their case, headings are CAPS,
and every menu / settings screen renders.
"""
import asyncio
import importlib
import importlib.abc
import importlib.machinery
import os
import re
import sys
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k, v in dict(BOT_TOKEN="1:x", API_ID="1", API_HASH="x", MONGO_URI="mongodb://x", OWNER_ID="1", BASE_URL="https://x.y").items():
    os.environ.setdefault(k, v)


class Btn:
    def __init__(self, text, callback_data=None, url=None, web_app=None, **kw):
        self.text, self.callback_data, self.url, self.web_app = text, callback_data, url, web_app


class Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = [list(r) for r in inline_keyboard]


class _Filter:
    class MessageFilter:
        pass


class _Err(Exception):
    pass


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    ROOTS = ("telegram", "motor", "pymongo", "aiohttp", "pyrogram", "uvloop", "dns", "bson")

    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in self.ROOTS:
            return importlib.machinery.ModuleSpec(name, self, is_package=True)

    def create_module(self, spec):
        m = MagicMock(name=spec.name)
        m.__path__ = []
        m.__name__ = spec.name
        m.__spec__ = spec
        if spec.name == "telegram":
            m.InlineKeyboardButton, m.InlineKeyboardMarkup = Btn, Markup
        if spec.name == "telegram.error":
            for n in ("BadRequest", "Forbidden", "RetryAfter", "TelegramError", "Conflict", "NetworkError"):
                setattr(m, n, type(n, (_Err,), {}))
        if spec.name == "telegram.ext":
            m.filters = _Filter
            m.ExtBot = type("ExtBot", (), {})
        return m

    def exec_module(self, module):
        pass


sys.meta_path.insert(0, _Finder())


import config  # noqa: E402
from database import db  # noqa: E402
from database.db import settings as S  # noqa: E402

S._data = __import__("copy").deepcopy(db.DEFAULTS)
S._data["fsub_channels"] = [{"id": -1001, "title": "My Mixed-Case Channel", "link": "https://t.me/x"},
                            {"id": -1002, "title": "Second", "link": "https://t.me/y"}]
S._data["admins"] = [42]
S._data["db_channel"] = {"id": -100, "title": "Store Chan"}
S.set = AsyncMock()          # no database in the tests

from handlers import admin, analytics, autoposter, backup, broadcast, contact, start, transfer, upload, usage  # noqa: E402,F401
from utils import keyboards as K  # noqa: E402
from utils import ui  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


class Sent:
    def __init__(self):
        self.items = []

    async def edit_message_text(self, text, **kw):
        self.items.append((text, kw.get("reply_markup")))

    reply_text = edit_message_text


def fake_update(owner=True):
    sent = Sent()
    q = MagicMock()
    q.edit_message_text = sent.edit_message_text
    q.answer = AsyncMock()
    q.message.chat_id = 1
    u = MagicMock()
    u.callback_query = q
    u.effective_user.id = config.OWNER_ID if owner else 42
    u.effective_message.reply_text = sent.edit_message_text
    return u, sent


def ctx():
    c = MagicMock()
    c.user_data = {}
    c.bot.username = "bot"
    return c


def all_buttons(markup):
    return [(i, b) for i, r in enumerate(markup.inline_keyboard) for b in r]


def check_markup(name, markup):
    rows = markup.inline_keyboard
    for r in rows:
        check(len(r) <= 2, f"{name}: row with {len(r)} buttons {[b.text for b in r]}")
        for b in r:
            if b.callback_data:
                check(len(b.callback_data.encode()) <= 64, f"{name}: callback too long {b.callback_data}")
                m = re.match(r"adm:([a-z_0-9]+)", b.callback_data)
                if m:
                    check(m.group(1) in admin.ACTIONS, f"{name}: unknown action {b.callback_data}")
    for r in rows:
        for b in r:
            if re.search(r"BACK|CLOSE|CANCEL", b.text.upper()) and b.callback_data and "adm:" in b.callback_data \
                    and not re.search(r"adm:(batchdone|ads)", b.callback_data) and b.text.upper().strip("🔙✖️❌ ") in ("BACK", "CLOSE", "CANCEL"):
                if len(r) != 1 and not (b.callback_data.startswith("adm:ads") or b.callback_data in ("adm:stats", "adm:ads")):
                    check(False, f"{name}: {b.text} not full width")


async def screens():
    # (action, args) pairs that render without a database
    plan = [("home", []), ("settings", []), ("users", []), ("monet", []), ("servers", []), ("fsub", []),
            ("fsubch", ["-1001"]), ("btns", []), ("mode", []), ("autodel", []), ("autodel", ["10"]), ("protect", []),
            ("fcap", []), ("reset", []), ("simg", []), ("scap", []), ("admins", []), ("batch", []), ("ads", []),
            ("bk", []), ("fsubup", ["-1002"]), ("fsubtoggle", [])]
    for name, args in plan:
        for owner in (True, False):
            u, sent = fake_update(owner)
            try:
                await admin.ACTIONS[name](u, ctx(), args)
            except Exception as e:  # owner-only decorators deny quietly
                check(False, f"{name}{args} owner={owner} raised {type(e).__name__}: {e}")
                continue
            for text, mk in sent.items:
                head = text.split("\n")[0]
                plain = re.sub(r"<[^>]+>", "", head)
                letters = re.sub(r"[^A-Za-z]", "", plain.split(" — ")[0].split("▸")[0])
                if name not in ("fsubch", "fsubup") and "<b>" in head:
                    check(letters.upper() == letters, f"{name}: heading not CAPS: {head}")
                if mk:
                    check_markup(f"{name}{args}", mk)
                    for _, b in all_buttons(mk):
                        if b.text.startswith("📢 My Mixed"):
                            pass
    # owner names must keep case
    u, sent = fake_update()
    await admin.ACTIONS["fsub"](u, ctx(), [])
    texts = [b.text for _, b in all_buttons(sent.items[-1][1])]
    check(any("My Mixed-Case Channel" in t for t in texts), "channel name was upper-cased in Force-Sub list")
    u, sent = fake_update()
    await admin.ACTIONS["fsubch"](u, ctx(), ["-1001"])
    check("My Mixed-Case Channel" in sent.items[-1][0], "channel name was upper-cased on its detail screen")


def static_menus():
    for name, mk in [("main-owner", K.main_panel(True)), ("main-admin", K.main_panel(False)), ("settings", K.settings_panel()),
                     ("users", K.users_panel(True)), ("monet", K.monetization_panel(True)), ("servers", K.servers_panel()),
                     ("cancel", K.cancel()), ("back", K.back("users")), ("batch", K.batch_kb()), ("after", K.after_link()),
                     ("confirm", K.confirm("adm:home", "adm:home"))]:
        check_markup(name, mk)
        for _, b in all_buttons(mk):
            check(b.text == b.text.upper(), f"{name}: label not CAPS {b.text}")
    check(K.nm("Mixed Case", "adm:noop").text == "Mixed Case", "nm() changed the case")
    check(K.ub("a b", "x").text == "A B", "ub() must upper-case")
    check(K.grid([("a", "1"), ("b", "2"), ("c", "3")])[1][0].text == "C", "grid leftover")
    check(K.pager(0, 1, "p") == [] and len(K.pager(1, 3, "p")) == 2, "pager")
    check(ui.head("⚙️", "Admin Control Panel", "X") == "⚙️ <b>ADMIN CONTROL PANEL</b>\n\nX", "head()")
    check(ui.ago(None) == "NEVER" and ui.ago(__import__("time").time() - 7200) == "2 H AGO", "ago()")
    check(ui.bar(50, 10) == "█████░░░░░", "bar()")


def callbacks_everywhere():
    """Every adm:<x> literal in the source resolves to a registered action."""
    seen = set()
    for d, _, fs in os.walk(ROOT):
        for f in fs:
            if f.endswith(".py") and "tests" not in d:
                for m in re.finditer(r"adm:([a-z_0-9]+)", open(os.path.join(d, f), encoding="utf-8").read()):
                    seen.add(m.group(1))
    for a in sorted(seen):
        check(a in admin.ACTIONS, f"source references adm:{a} but it is not registered")


async def state_prompts():
    """State handlers reject bad input with a warning that keeps CANCEL."""
    c = ctx()
    u, sent = fake_update()
    u.effective_message.text = "no dash here"
    await admin.STATES["btns"](u, c)
    check(sent.items and "MISSING" in sent.items[-1][0] and sent.items[-1][1].inline_keyboard[0][0].callback_data == "adm:cancel",
          "btns state: bad input did not warn with CANCEL")
    u, sent = fake_update()
    u.effective_message.text = "Join - https://example.com | Two - https://e.org"
    c = ctx()
    await admin.STATES["btns"](u, c)
    check("SAVED <b>2</b>" in sent.items[-1][0] and "state" not in c.user_data, "btns state: good input not accepted")


async def broadcast_live():
    """run_broadcast fills the `live` dict the web panel polls, and ends with a BACK button."""
    async def ids():
        for i in range(3):
            yield 100 + i
    db.count_broadcastable = AsyncMock(return_value=3)
    db.iter_broadcast_ids = ids
    bot = MagicMock()
    bot.copy_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    live = {}
    await broadcast.run_broadcast(bot, 1, 2, 3, 4, live=live)
    check(live.get("total") == 3 and live.get("done") == 3 and live.get("sent") == 3, f"broadcast live dict wrong: {live}")
    text = bot.edit_message_text.call_args.args[0]
    check("BROADCAST DONE" in text and "100%" in text, "broadcast final text")
    check(bot.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "adm:home", "broadcast BACK")


def selftest():
    bad = Markup([[Btn("A", "adm:home"), Btn("B", "adm:home"), Btn("C", "adm:home")], [Btn("X", "adm:nope")]])
    n = len(fails)
    check_markup("selftest", bad)
    caught = len(fails) - n
    del fails[n:]
    check(caught >= 2, "test harness does not detect 3-button rows / unknown actions")


async def run():
    selftest()
    await broadcast_live()
    await screens()
    await state_prompts()


static_menus()
callbacks_everywhere()
asyncio.run(run())
print("FAILED:\n" + "\n".join(sorted(set(fails))) if fails else "ALL UI TESTS PASSED")
sys.exit(1 if fails else 0)
