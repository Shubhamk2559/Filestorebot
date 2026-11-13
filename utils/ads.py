"""
utils/ads.py - Monetag rewarded-ad gate, shown as a Telegram Mini App.

Flow
  1. user opens a file link and passes force-subscribe
  2. gate()  -> bot sends a "Watch Ad & Get Files" button (a Mini App) bound to ONE one-time session
  3. Mini App (utils/ads_page.py) checks for ad blockers / ad-blocking DNS, loads the Monetag SDK, shows the ad
  4. Monetag calls  GET /monetag/postback  (server to server) -> the ad is confirmed on OUR server
  5. Mini App calls /api/ad/claim -> the session is consumed once -> files are delivered in the chat

Anti-bypass
  * every API call carries Telegram initData that is HMAC-verified with the bot token (user can't be spoofed)
  * a session belongs to one user, one link, works once, expires in 30 minutes
  * with server verification ON, only a Monetag postback (secret protected) can confirm an ad
  * ad-blocker / DNS-blocker detected -> no files, not even through the no-fill fallback
  * no-fill fallback: only after N failed tries (default 3), only with a clean environment,
    each attempt/outcome is timed and counted on the server, and capped per user per day
"""
import asyncio
import datetime as dt
import hmac
import hashlib
import json
import logging
import re
import secrets
import time
from urllib.parse import parse_qsl, urlparse

from aiohttp import web
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import TelegramError

import config
from database import db
from database.db import settings as S
from utils.ads_page import PAGE
from utils.helpers import call, is_admin, notify_owner

log = logging.getLogger(__name__)

SESSION_TTL = 30 * 60        # seconds a gate stays valid
INIT_DATA_MAX_AGE = 2 * 3600  # Telegram initData freshness
MAX_ATTEMPTS = 8             # ad attempts per session (user closing ads early also counts)
MIN_AD_SECONDS = 4           # an ad that "finished" faster than this did not really play
ATTEMPT_GAP = 1              # minimum seconds between two attempts
FALLBACK_DAILY = 3           # max no-fill fallbacks per user per day
NOFILL = {"empty feed", "timeout", "network error", "ad is not ready", "failed to open pop",
          "failed to verify the ad show", "empty zone id"}

# Domains that every ad blocker / ad-blocking DNS (AdGuard, NextDNS, Blokada, Pi-hole...) blocks.
PROBES = [
    "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js",
    "https://googleads.g.doubleclick.net/pagead/id",
    "https://static.doubleclick.net/instream/ad_status.js",
    "https://securepubads.g.doubleclick.net/tag/js/gpt.js",
]

_tasks: set = set()
_rate: dict = {}
_seen: dict = {}   # session id -> asyncio.Event, set the moment the Monetag postback confirms the ad


# ------------------------------------------------------------------ settings helpers
def zone() -> str:
    z = str(S.get("ads_zone") or "").strip()
    return z if re.fullmatch(r"\d{3,14}", z) else ""


def sdk_url() -> str:
    u = str(S.get("ads_sdk_url") or "").strip()
    return u if re.fullmatch(r"https://[^\s'\"<>]+", u) else "https://libtl.com/sdk.js"


def mini_app_ready() -> bool:
    return config.BASE_URL.startswith("https://")


async def ensure_secret() -> str:
    sec = S.get("ads_secret")
    if not sec:
        sec = secrets.token_urlsafe(18)
        await S.set("ads_secret", sec)
    return sec


async def postback_url() -> str:
    sec = await ensure_secret()
    return (f"{config.BASE_URL}/monetag/postback?secret={sec}&ymid={{ymid}}&zone_id={{zone_id}}"
            "&sub_zone_id={sub_zone_id}&event_type={event_type}&reward_event_type={reward_event_type}"
            "&telegram_id={telegram_id}&estimated_price={estimated_price}")


async def bump(field: str):
    await db.settings_col.update_one({"_id": "ad_stats"}, {"$inc": {field: 1}}, upsert=True)


async def get_stats() -> dict:
    return await db.settings_col.find_one({"_id": "ad_stats"}) or {}


# ------------------------------------------------------------------ Telegram initData
def validate_init_data(init_data: str):
    """Return the Telegram user dict if initData is genuine and fresh, else None."""
    try:
        parsed = dict(parse_qsl(init_data or "", keep_blank_values=True, strict_parsing=True))
        got = parsed.pop("hash", "")
        if not got:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(parsed.get("auth_date", "0")) > INIT_DATA_MAX_AGE:
            return None
        user = json.loads(parsed.get("user", "{}"))
        return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None
    except Exception:
        return None


# ------------------------------------------------------------------ the gate (called from handlers/start.py)
def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


async def gate(bot, chat_id: int, link: dict) -> bool:
    """Send the 'watch ad' Mini App button. True = gate sent (don't deliver yet), False = deliver right away."""
    if not S.get("ads_enabled"):
        return False
    if is_admin(chat_id) and not S.get("ads_admin_test"):
        return False   # admins skip ads (turn on "Test as admin" in Panel -> Ads to see the gate yourself)
    if not zone() or not mini_app_ready():
        await notify_owner(bot, "ads_cfg",
                           "⚠️ Ads are ON but not configured: "
                           f"{'Zone ID is missing' if not zone() else 'BASE_URL is not a public https URL'}. "
                           "Files are being delivered WITHOUT ads until you fix it in Panel → Ads.", every=300)
        return False

    # one open gate per user: close the previous one
    async for old in db.ads_col.find({"uid": chat_id, "status": "open"}, {"msg_id": 1}):
        if old.get("msg_id"):
            try:
                await bot.delete_message(chat_id, old["msg_id"])
            except TelegramError:
                pass
    await db.ads_col.update_many({"uid": chat_id, "status": "open"}, {"$set": {"status": "expired"}})

    sid = secrets.token_hex(12)
    now = time.time()
    await db.ads_col.insert_one({
        "_id": sid, "uid": chat_id, "token": link["_id"], "status": "open", "created": now,
        "exp": now + SESSION_TTL, "expire_at": _utcnow() + dt.timedelta(seconds=SESSION_TTL + 600),
        "attempts": 0, "fails": 0, "outcome_n": 0, "attempt_started": 0.0,
        "ad_seen": False, "shown": False, "fallback": False, "blocked": None, "probe_at": 0.0,
    })
    try:
        msg = await call(
            bot.send_message, chat_id,
            "🎬 <b>Watch a short ad to unlock your files</b>\n\n"
            "Tap the button below, watch the ad till the end, and your files arrive here automatically.\n\n"
            "⚠️ Please turn off any <b>ad blocker / ad-blocking DNS / Private DNS</b> first.",
            reply_markup=M([[B("🎬 Watch Ad & Get Files",
                               web_app=WebAppInfo(url=f"{config.BASE_URL}/ad?s={sid}"))]]),
            parse_mode=ParseMode.HTML)
        await db.ads_col.update_one({"_id": sid}, {"$set": {"msg_id": msg.message_id}})
    except TelegramError as e:
        log.warning("Ad gate could not be sent: %s", e)
        await notify_owner(bot, "ads_send",
                           f"⚠️ Ad gate could not be sent, so files were delivered without an ad.\nReason: {e}\n"
                           f"Mini App URL used: {config.BASE_URL}/ad  (must be a public https URL)", every=300)
        await db.ads_col.update_one({"_id": sid}, {"$set": {"status": "expired"}})
        return False  # can't show the gate (user blocked bot / Telegram error) -> normal delivery path decides
    return True


# ------------------------------------------------------------------ Mini App API
def _limited(uid: int) -> bool:
    now = time.time()
    hits = [t for t in _rate.get(uid, []) if now - t < 60]
    hits.append(now)
    _rate[uid] = hits
    if len(_rate) > 20000:
        _rate.clear()
    return len(hits) > 240


async def _auth(request):
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="bad json")
    user = validate_init_data(str(body.get("initData", "")))
    if not user:
        raise web.HTTPUnauthorized(text="bad init data")
    if _limited(user["id"]):
        raise web.HTTPTooManyRequests(text="slow down")
    s = await db.ads_col.find_one({"_id": str(body.get("sid", ""))[:64]})
    if not s or s["uid"] != user["id"]:
        raise web.HTTPForbidden(text="not your session")
    return body, s


def _reply(**kw):
    return web.json_response(kw, headers={"Cache-Control": "no-store"})


def _clean_probe(s) -> bool:
    """A fresh 'no ad blocker' report from the Mini App is on record."""
    return s.get("blocked") is False and time.time() - s.get("probe_at", 0) < 300


def _closed(s) -> bool:
    return s["status"] != "open" or s["exp"] < time.time()


def _verified(s) -> bool:
    """Has this user really earned the files? (computed on the server only)"""
    if s.get("fallback"):
        return True
    if not s.get("shown"):
        return False
    return bool(s.get("ad_seen")) if S.get("ads_postback") else True


async def api_init(request):
    body, s = await _auth(request)
    state = "closed" if _closed(s) else "open"
    if s["status"] == "done":
        state = "done"
    return _reply(state=state, zone=zone(), sdk=sdk_url(), tries=int(S.get("ads_tries") or 3),
                  strict=bool(S.get("ads_block_adblock")), postback=bool(S.get("ads_postback")),
                  probes=PROBES)


async def api_probe(request):
    body, s = await _auth(request)
    if _closed(s):
        return _reply(state="closed")
    blocked = bool(body.get("blocked"))
    await db.ads_col.update_one({"_id": s["_id"]},
                                {"$set": {"blocked": blocked, "probe_at": time.time(),
                                          "probe_detail": str(body.get("detail", ""))[:80]}})
    if blocked:
        await bump("blocked")
    return _reply(state="ok")


async def api_attempt(request):
    body, s = await _auth(request)
    if _closed(s):
        return _reply(ok=False, state="closed")
    now = time.time()
    if "blocked" in body:                                     # probe result sent together with the attempt (1 round trip less)
        blocked = bool(body.get("blocked"))
        await db.ads_col.update_one({"_id": s["_id"]},
                                    {"$set": {"blocked": blocked, "probe_at": now,
                                              "probe_detail": str(body.get("detail", ""))[:80]}})
        s["blocked"], s["probe_at"] = blocked, now
        if blocked:
            await bump("blocked")
    if S.get("ads_block_adblock") and not _clean_probe(s):
        return _reply(ok=False, state="blocked")          # no clean probe on record -> no ad, no files
    if s["attempts"] >= MAX_ATTEMPTS:
        return _reply(ok=False, state="too_many")
    if now - s.get("attempt_started", 0) < ATTEMPT_GAP:
        return _reply(ok=False, state="slow_down")
    await db.ads_col.update_one({"_id": s["_id"]}, {"$inc": {"attempts": 1},
                                                    "$set": {"attempt_started": now, "shown": False}})
    return _reply(ok=True, ymid=s["_id"], n=s["attempts"] + 1)


async def api_shown(request):
    """The SDK promise resolved (ad closed). Real confirmation = Monetag postback (if enabled)."""
    body, s = await _auth(request)
    if _closed(s):
        return _reply(state="closed")
    if s["outcome_n"] >= s["attempts"]:
        return _reply(state="no_attempt")
    elapsed = time.time() - s["attempt_started"]
    r = await db.ads_col.update_one({"_id": s["_id"], "outcome_n": s["outcome_n"]},
                                    {"$set": {"outcome_n": s["attempts"],
                                              "shown": elapsed >= MIN_AD_SECONDS}})
    if not r.modified_count:
        return _reply(state="no_attempt")
    if elapsed < MIN_AD_SECONDS:
        return _reply(state="too_fast")
    fresh = await db.ads_col.find_one({"_id": s["_id"]})
    return _reply(state="ready" if _verified(fresh) else "verifying")


async def api_status(request):
    """wait=1 -> hold the request open (up to ~6s) and answer the instant the postback confirms the ad."""
    body, s = await _auth(request)
    if _closed(s):
        return _reply(state="closed")
    if _verified(s):
        return _reply(state="ready")
    if not body.get("wait"):
        return _reply(state="verifying")
    ev = _seen.setdefault(s["_id"], asyncio.Event())
    try:
        for _ in range(4):                                    # DB re-check every 1.5s also covers a postback on another instance
            fresh = await db.ads_col.find_one({"_id": s["_id"]})
            if not fresh or _closed(fresh):
                return _reply(state="closed")
            if _verified(fresh):
                return _reply(state="ready")
            try:
                await asyncio.wait_for(ev.wait(), 1.5)
            except asyncio.TimeoutError:
                pass
            ev.clear()
        return _reply(state="verifying")
    finally:
        _seen.pop(s["_id"], None)


async def api_fail(request):
    """The SDK could not show an ad. After N genuine no-fill tries the user gets the files without an ad."""
    body, s = await _auth(request)
    if _closed(s):
        return _reply(state="closed")
    if s["outcome_n"] >= s["attempts"]:
        return _reply(state="no_attempt")
    reason = str(body.get("reason", "")).strip().lower()[:60]
    await db.ads_col.update_one({"_id": s["_id"], "outcome_n": s["outcome_n"]},
                                {"$set": {"outcome_n": s["attempts"]}})
    if reason not in NOFILL:                               # user closed the ad etc. -> not a no-fill
        return _reply(state="retry", fails=s["fails"])

    fails = s["fails"] + 1
    await db.ads_col.update_one({"_id": s["_id"]}, {"$set": {"fails": fails}})
    tries = int(S.get("ads_tries") or 3)
    if fails < tries:
        return _reply(state="retry", fails=fails)

    # ---- fallback decision (all checked on the server)
    if S.get("ads_block_adblock") and not _clean_probe(s):
        return _reply(state="blocked")
    if await _fallback_used(s["uid"]) >= FALLBACK_DAILY:
        return _reply(state="no_fallback")
    await _fallback_inc(s["uid"])
    await db.ads_col.update_one({"_id": s["_id"]}, {"$set": {"fallback": True}})
    return _reply(state="ready", fallback=True)


async def _fallback_used(uid: int) -> int:
    d = await db.usage_col.find_one({"_id": f"fb:{uid}:{db._today()}"}, {"used": 1})
    return (d or {}).get("used", 0)


async def _fallback_inc(uid: int):
    await db.usage_col.update_one(
        {"_id": f"fb:{uid}:{db._today()}"},
        {"$inc": {"used": 1}, "$set": {"expire_at": db._next_midnight() + dt.timedelta(days=1)}},
        upsert=True)


async def api_claim(request):
    body, s = await _auth(request)
    if s["status"] == "done":
        return _reply(ok=True, state="done")
    if _closed(s):
        return _reply(ok=False, state="closed")
    if not _verified(s):
        return _reply(ok=False, state="not_verified")
    taken = await db.ads_col.find_one_and_update({"_id": s["_id"], "status": "open"},
                                                 {"$set": {"status": "done", "done_at": time.time()}})
    if not taken:                                           # lost a race with a second claim: files go out once
        return _reply(ok=True, state="done")
    await bump("fallback" if s.get("fallback") else "verified")
    bot = request.app["bot"]
    task = asyncio.create_task(_deliver_after_ad(bot, s))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return _reply(ok=True, state="done")


async def _deliver_after_ad(bot, s):
    from handlers import start  # lazy: handlers.start imports this module
    if s.get("msg_id"):
        try:
            await bot.delete_message(s["uid"], s["msg_id"])
        except TelegramError:
            pass
    link = await db.get_link(s["token"])
    if not link:
        await call(bot.send_message, s["uid"], "❌ This link is invalid or has been revoked.")
        return
    try:
        await start.deliver(bot, s["uid"], link)
    except Exception:
        log.exception("Delivery after ad failed")


# ------------------------------------------------------------------ Monetag postback (server to server)
async def postback(request):
    q = request.query
    if not hmac.compare_digest(q.get("secret", ""), S.get("ads_secret") or secrets.token_hex(8)):
        raise web.HTTPForbidden(text="bad secret")
    sid = q.get("ymid", "")[:64]
    if time.time() - (S.get("ads_last_postback") or 0) > 60:
        await S.set("ads_last_postback", time.time())
    if q.get("event_type") in ("impression", "click") and re.fullmatch(r"[0-9a-f]{24}", sid):
        s = await db.ads_col.find_one({"_id": sid})
        tid = q.get("telegram_id", "")
        if s and (not tid or tid == str(s["uid"])):
            await db.ads_col.update_one(
                {"_id": sid, "status": "open"},
                {"$set": {"ad_seen": True, "ad_value": q.get("reward_event_type", "")[:20],
                          "ad_seen_at": time.time()}})
            ev = _seen.get(sid)
            if ev:
                ev.set()                                   # wake the Mini App's waiting status call right now
    return web.Response(text="ok")   # always 2xx for a valid secret so Monetag doesn't keep retrying


def _boot_json() -> str:
    return json.dumps({"zone": zone(), "sdk": sdk_url(), "tries": int(S.get("ads_tries") or 3),
                       "strict": bool(S.get("ads_block_adblock")), "postback": bool(S.get("ads_postback")),
                       "probes": PROBES}).replace("</", "<\\/")


async def page(request):
    return web.Response(text=PAGE.replace("__BOOT__", _boot_json()), content_type="text/html", charset="utf-8", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


def setup_routes(app: web.Application):
    app.router.add_get("/ad", page)
    app.router.add_post("/api/ad/init", api_init)
    app.router.add_post("/api/ad/probe", api_probe)
    app.router.add_post("/api/ad/attempt", api_attempt)
    app.router.add_post("/api/ad/shown", api_shown)
    app.router.add_post("/api/ad/status", api_status)
    app.router.add_post("/api/ad/fail", api_fail)
    app.router.add_post("/api/ad/claim", api_claim)
    app.router.add_get("/monetag/postback", postback)
