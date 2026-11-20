"""
utils/admin_web.py - the web Admin Panel (Telegram Mini App).

  GET  /admin        the panel page (utils/admin_page.html)
  POST /api/admin    JSON {initData, op, ...}  ->  JSON

Security: every call carries Telegram initData that is HMAC-verified with the bot token, and the user must be an
admin (OWNER_ID or a panel admin). Owner-only operations (Backup, Transfer, VPS, Earnings) need OWNER_ID.

Growth section : Broadcast, Stats, Ads, Autoposter
Owner section  : Earnings, Backup, Transfer, VPS usage
Every other tile opens the matching screen in the bot chat (op "open") - those screens need typed input.
"""
import asyncio
import datetime as dt
import html
import logging
import time
from pathlib import Path

from aiohttp import web
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, WebAppInfo
from telegram.error import TelegramError

import config
from database import db
from database.db import settings as S
from utils import ads, usage as U
from utils.helpers import is_admin, is_owner

log = logging.getLogger(__name__)

_BC: dict = {}            # admin id -> live broadcast progress
_tasks: set = set()

# tiles that just open a screen of the bot chat -> callback action
OPEN_ACTIONS = {
    "batch": "📦 Create Batch", "links": "🔗 My Links", "dbch": "📡 Database Channel", "fsub": "🔔 Force-Sub",
    "btns": "🔘 Join Buttons", "ban": "🚫 Ban / Unban", "admins": "👮 Admins", "simg": "🖼 Start Image",
    "scap": "📝 Start Caption", "fcap": "🏷 File Caption", "mode": "🎬 Delivery Mode", "reset": "♻️ Reset Settings",
    "tr": "🔄 Transfer", "bk": "💾 Backup / Restore", "ap": "📅 Autoposter", "bc": "📢 Broadcast",
}
OWNER_OPS = {"earnings", "backup", "backup_now", "backup_auto", "transfer", "vps"}


class Err(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def _spawn(coro):
    t = asyncio.ensure_future(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# ------------------------------------------------------------------ operations
async def op_home(uid, b, bot):
    s = await db.stats()
    today = db._today()
    d = await db.stats_col.find_one({"_id": today}) or {}
    last = []
    day = dt.datetime.now(db.LIMIT_TZ).date()
    ids = [str(day - dt.timedelta(days=i)) for i in range(14)]
    async for x in db.stats_col.find({"_id": {"$in": ids}}):
        last.append(int(x.get("views", 0) or 0))
    views = int(d.get("views", 0) or 0)
    peak = max(last + [views, 1])
    return {"users": s["users"], "files": s["files"], "links": s["links"], "banned": s["banned"],
            "views_today": views, "views_peak": peak, "owner": is_owner(uid), "bot": bot.username,
            "protect": bool(S.get("protect")), "ads": bool(S.get("ads_enabled")),
            "autodelete": int(S.get("autodelete") or 0), "daily_limit": db.limit_value()}


async def op_setting(uid, b, bot):
    key, val = b.get("key"), b.get("value")
    if key == "protect":
        await S.set("protect", bool(val))
    elif key == "ads":
        if val and not ads.zone():
            raise Err("Set the Monetag Zone ID first (Growth → Ads).")
        await S.set("ads_enabled", bool(val))
    elif key == "daily_limit":
        n = int(val)
        if not 1 <= n <= 1000:
            raise Err("Daily limit must be 1-1000.")
        await S.set("daily_limit", n)
    elif key == "autodelete":
        if int(val) not in (0, 5, 10, 30):
            raise Err("bad value")
        await S.set("autodelete", int(val))
    else:
        raise Err("unknown setting")
    return {}


async def op_stats(uid, b, bot):
    s = await db.stats()
    m = await db.mongo_status()
    out = {"counts": s, "mongo": {"ok": m["ok"], "error": m.get("error", "")}}
    if m["ok"]:
        limit = m["limit_mb"] * 1048576
        out["mongo"].update(
            ping=round(m["ping_ms"] or 0), read=round(m["read_ms"] or 0), used_mb=round(m["used"] / 1048576, 2),
            limit_mb=m["limit_mb"], pct=round(min(100.0, m["used"] / limit * 100), 1) if limit else 0,
            docs=m["objects"], cols=m["collections"],
            top=[{"name": n, "docs": c, "mb": round(sz / 1048576, 2)} for n, c, sz in m.get("cols", [])[:6]])
    return out


# ---- broadcast
async def op_bc_count(uid, b, bot):
    return {"total": await db.count_broadcastable()}


async def op_bc_send(uid, b, bot):
    from handlers import broadcast
    text = str(b.get("text") or "").strip()
    if not text:
        raise Err("Write the message first.")
    if len(text) > 4000:
        raise Err("Message too long (max 4000 characters).")
    cur = _BC.get(uid)
    if cur and not cur.get("finished"):
        raise Err("A broadcast is already running.")
    try:
        sent = await bot.send_message(uid, text)                    # HTML parse mode (bot default)
    except TelegramError as e:
        raise Err(f"Telegram rejected the message: {e}")
    if b.get("test"):
        return {"test": True}
    status = await bot.send_message(uid, "📢 <b>Broadcasting…</b>\n\nStarting…")
    live = _BC[uid] = {"total": 0, "done": 0, "sent": 0, "blocked": 0, "failed": 0, "finished": False}

    async def job():
        try:
            await broadcast.run_broadcast(bot, uid, sent.message_id, uid, status.message_id, live=live)
        except Exception:
            log.exception("Web broadcast failed")
        finally:
            live["finished"] = True
    _spawn(job())
    return {"started": True}


async def op_bc_status(uid, b, bot):
    return _BC.get(uid) or {"idle": True}


# ---- ads
def _ads_view(st):
    return {"on": bool(S.get("ads_enabled")), "zone": ads.zone(), "sdk": ads.sdk_url(),
            "postback": bool(S.get("ads_postback")), "adblock": bool(S.get("ads_block_adblock")),
            "admin_test": bool(S.get("ads_admin_test")), "last_postback": S.get("ads_last_postback") or 0,
            "verified": st.get("verified", 0), "fallback": st.get("fallback", 0), "blocked": st.get("blocked", 0),
            "ready": ads.mini_app_ready(), "base_url": config.BASE_URL}


async def op_ads(uid, b, bot):
    a = b.get("set")
    if a == "on":
        if not S.get("ads_enabled") and not ads.zone():
            raise Err("Set the Zone ID first.")
        await S.set("ads_enabled", not S.get("ads_enabled"))
    elif a == "postback":
        await S.set("ads_postback", not S.get("ads_postback"))
    elif a == "adblock":
        await S.set("ads_block_adblock", not S.get("ads_block_adblock"))
    elif a == "admin_test":
        await S.set("ads_admin_test", not S.get("ads_admin_test"))
    elif a == "zone":
        z = str(b.get("value") or "").strip()
        if not (z.isdigit() and 3 <= len(z) <= 14):
            raise Err("Zone ID must be digits only.")
        await S.set("ads_zone", z)
    elif a == "newsecret":
        await S.set("ads_secret", "")
        await ads.ensure_secret()
    out = _ads_view(await ads.get_stats())
    out["postback_url"] = await ads.postback_url()
    return out


# ---- autoposter
def _ap_view(c, total, posted):
    q = c.get("queue_state") or {}
    return {"id": c["_id"], "title": c.get("title") or str(c["_id"]), "on": c.get("enabled", True),
            "loop": c.get("loop", True), "slots": sorted(c.get("slots", []), key=lambda s: s["t"]),
            "total": total, "posted": posted, "cycle": q.get("cycle", 0)}


async def op_ap(uid, b, bot):
    from utils import autoposter as AP
    cid, a = b.get("cid"), b.get("set")
    if cid is not None:
        cid = int(cid)
        ch = await db.ap_get_channel(cid)
        if not ch:
            raise Err("Channel not found.")
        if a == "on":
            await db.ap_set(cid, enabled=not ch.get("enabled", True))
        elif a == "loop":
            await db.ap_set(cid, loop=not ch.get("loop", True))
        elif a == "reshuffle":
            await db.ap_rebuild_cycle(cid)
        elif a == "slot_add":
            try:
                new = AP.parse_slots(str(b.get("value") or ""))
            except ValueError as e:
                raise Err(str(e))
            merged = {s["t"]: s["n"] for s in ch.get("slots", [])}
            merged.update({s["t"]: s["n"] for s in new})
            await db.ap_set(cid, slots=[{"t": t, "n": n} for t, n in sorted(merged.items())])
        elif a in ("slot_del", "slot_inc", "slot_dec"):
            t = str(b.get("value"))
            slots = list(ch.get("slots", []))
            if a == "slot_del":
                slots = [s for s in slots if s["t"] != t]
            else:
                for s in slots:
                    if s["t"] == t:
                        s["n"] = max(1, min(AP.MAX_PER_SLOT, s["n"] + (1 if a == "slot_inc" else -1)))
            await db.ap_set(cid, slots=slots)
    out = []
    for c in await db.ap_list_channels():
        total, posted = await db.ap_counts(c["_id"])
        out.append(_ap_view(c, total, posted))
    return {"channels": out}


# ---- owner: earnings
async def op_earnings(uid, b, bot):
    from handlers import analytics as A
    d30 = A._range(30)
    docs = await A._daily_docs(str(d30[-1]))
    all_docs = await A._daily_docs()
    users = await A._per_period(db.users_col, "joined", A._since_ts(d30[-1]), "%Y-%m-%d")
    cols = {"today": A._range(1), "d7": A._range(7), "d30": d30}
    keys = ("rev", "views", "imp", "clk", "gates", "verified", "fallback", "blocked")
    table = {k: {n: A._sum(docs, c, k) for n, c in cols.items()} for k in keys}
    for k in keys:
        table[k]["all"] = sum((x.get(k, 0) or 0) for x in all_docs.values())
    table["users"] = {n: A._sum_map(users, c) for n, c in cols.items()}
    table["users"]["all"] = await db.users_col.count_documents({})
    daily = []
    for day in A._range(14):
        x = docs.get(str(day), {})
        daily.append({"d": day.strftime("%d %b"), "rev": round(float(x.get("rev", 0) or 0), 4),
                      "views": int(x.get("views", 0) or 0), "imp": int(x.get("imp", 0) or 0),
                      "users": users.get(str(day), 0)})
    top = await db.links_col.find({"views": {"$gt": 0}}).sort("views", -1).limit(8).to_list(8)
    imp30, rev30 = table["imp"]["d30"], table["rev"]["d30"]
    note = ""
    if not ads.zone() or not ads.mini_app_ready():
        note = "Ads are not configured, so there are no earnings to count."
    elif table["imp"]["all"] and not table["rev"]["all"]:
        note = "Monetag sends no price yet. Use the Postback URL shown in Growth → Ads."
    elif not table["imp"]["all"]:
        note = "No ad postbacks received yet."
    return {"table": table, "daily": daily, "note": note,
            "ecpm": round(rev30 / imp30 * 1000, 2) if imp30 else None,
            "top": [{"label": (l.get("label") or "Link")[:34], "views": int(l.get("views", 0))} for l in top]}


# ---- owner: backup
async def op_backup(uid, b, bot):
    return {"auto": bool(S.get("auto_backup")), "last": S.get("bk_last") or 0}


async def op_backup_auto(uid, b, bot):
    await S.set("auto_backup", not S.get("auto_backup"))
    return await op_backup(uid, b, bot)


async def op_backup_now(uid, b, bot):
    from utils import backup as BK
    try:
        counts = await BK.send_backup(bot, uid)
    except Exception as e:
        log.exception("Web backup failed")
        raise Err(f"Backup failed: {e}", 500)
    return {"counts": counts, "last": S.get("bk_last") or 0, "auto": bool(S.get("auto_backup"))}


# ---- owner: transfer (login and chat selection need typing -> done in the chat; this shows the status)
async def op_transfer(uid, b, bot):
    from utils import transfer as T
    bad = T.available()
    if bad:
        return {"available": False, "reason": bad}
    client = T.session.client
    me = T.session.me
    job = T.CURRENT
    out = {"available": True, "logged_in": bool(client and me),
           "name": (me.first_name if me else ""), "username": (me.username if me and me.username else ""),
           "job": None}
    if job:
        out["job"] = {"src": job.src_title, "dst": job.dst_title, "state": job.state, "total": job.total,
                      "copied": job.copied, "failed": job.failed, "skipped": job.skipped,
                      "running": job.running, "paused": job.paused}
    return out


# ---- owner: VPS
def _vps_row(a):
    return {"cpu": round(a["cpu_avg"]), "cpu_max": round(a["cpu_max"]), "ram": round(a["ram_avg"]),
            "ram_max": round(a["ram_max"]), "out": a["net_out"], "mbps": round(a["mbps"], 1)}


async def op_vps(uid, b, bot):
    if not U.supported():
        return {"supported": False}
    t = U.today()
    docs = await U.load_hours(t - dt.timedelta(days=29))
    out = {"supported": True, "has_data": bool(docs), "now": None}
    now = U.last
    if now:
        out["now"] = {k: (round(now[k], 1) if isinstance(now[k], float) else now[k])
                      for k in ("cpu", "ram", "ram_used_mb", "ram_total_mb", "mbps", "streams", "bot_rss_mb",
                                "disk_used_gb", "disk_total_gb") if k in now}
    if docs:
        today = [d for d in docs if d["_id"][:10] == t.isoformat()]
        d7 = [d for d in docs if d["_id"][:10] >= (t - dt.timedelta(days=6)).isoformat()]
        out["rows"] = {"today": _vps_row(U.total(today)), "d7": _vps_row(U.total(d7)), "d30": _vps_row(U.total(docs))}
        agg = U.rollup(docs, U.day_key)
        out["daily"] = [{"d": (t - dt.timedelta(days=i)).strftime("%d %b"),
                         **(_vps_row(agg[(t - dt.timedelta(days=i)).isoformat()])
                            if (t - dt.timedelta(days=i)).isoformat() in agg else {})}
                        for i in range(7)]
        try:
            r = U.recommend(docs)
            if r.get("ok"):
                pick = r["pick"]
                out["advice"] = {"days": r["days"], "undersized": bool(r["undersized"]), "plan": pick["name"],
                                 "cores": pick["cores"], "ram_gb": pick["ram_gb"], "disk_gb": pick["disk_gb"],
                                 "price": f"{U.currency()}{U.price(pick):.2f}",
                                 "current": (r["current"] or {}).get("name")}
        except Exception:
            log.exception("VPS advice failed")
    return out


# ---- open a bot screen
async def op_open(uid, b, bot):
    act = str(b.get("action") or "")
    if act not in OPEN_ACTIONS:
        raise Err("unknown screen")
    await bot.send_message(uid, f"{OPEN_ACTIONS[act]}\n\nTap the button to open it here in the chat.",
                           reply_markup=M([[B(f"Open {OPEN_ACTIONS[act]}", callback_data=f"adm:{act}")]]))
    return {}


OPS = {k[3:]: v for k, v in list(globals().items()) if k.startswith("op_")}


# ------------------------------------------------------------------ http
async def api(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    user = ads.validate_init_data(str(body.get("initData", "")))
    if not user:
        return web.json_response({"error": "Session expired - close the panel and open it again from the bot."}, status=401)
    if not is_admin(user["id"]):
        return web.json_response({"error": "Not allowed"}, status=403)
    uid, op = user["id"], str(body.get("op", ""))
    fn = OPS.get(op)
    if not fn:
        return web.json_response({"error": "unknown op"}, status=400)
    if op in OWNER_OPS and not is_owner(uid):
        return web.json_response({"error": "Owner only"}, status=403)
    try:
        data = await fn(uid, body, request.app["bot"])
    except Err as e:
        return web.json_response({"error": str(e)}, status=e.status)
    except Exception as e:
        log.exception("Admin API %s failed", op)
        return web.json_response({"error": f"{type(e).__name__}: {e}"[:200]}, status=500)
    return web.json_response({"ok": True, "data": data}, headers={"Cache-Control": "no-store"})


_PAGE = {}


async def page(request: web.Request) -> web.Response:
    if "html" not in _PAGE:
        _PAGE["html"] = (Path(__file__).with_name("admin_page.html")).read_text(encoding="utf-8")
    return web.Response(text=_PAGE["html"], content_type="text/html",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def setup_routes(app: web.Application):
    app.router.add_get("/admin", page)
    app.router.add_post("/api/admin", api)


def open_button():
    """Inline button that opens the panel as a Mini App (needs a public https BASE_URL)."""
    if not config.BASE_URL.startswith("https://"):
        return None
    return B("🌐 Open Web Panel", web_app=WebAppInfo(url=f"{config.BASE_URL}/admin"))
