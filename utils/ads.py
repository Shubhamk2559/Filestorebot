"""
utils/ads.py - Rewarded-ad gate (Monetag + Adsgram, rotated per user), shown as a Telegram Mini App.

Flow
  1. user opens a file link and passes force-subscribe
  2. gate()  -> bot sends a "Watch Ad & Get Files" button (a Mini App) bound to ONE one-time session
  3. Mini App (utils/ads_page.py) checks for ad blockers / ad-blocking DNS, loads the Monetag SDK, shows the ad
  4. Monetag calls  GET /monetag/postback  (server to server) -> the ad is confirmed on OUR server
  5. Mini App calls /api/ad/claim -> the session is consumed once -> files are delivered in the chat

Ads rotation (NEW)
  * two networks: Monetag + Adsgram. Every user has his own counter, so one user sees them alternately
    (share % is set in Panel -> Ads -> Mix; 50 = ABAB..., 70 = 7 Adsgram : 3 Monetag, ...)
  * a gate with N ads is a run of N consecutive picks (2 ads = one of each at 50 %)
  * the SERVER picks the network for every attempt - the user can not choose the cheaper / easier one
  * no-fill / blocked network -> the SAME ad is served by the OTHER network (failover) before anything else happens
  * Adsgram verification: Adsgram "Reward URL" (server to server) -> /adsgram/reward -> one reward = one ad (consumed once)

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
import math
import os
import re
import secrets
import time
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import TelegramError

import config
from database import db
from database.db import settings as S
from utils.ads_page import PAGE
from utils import support as SUP
from utils.helpers import call, is_admin, notify_owner

log = logging.getLogger(__name__)

AG_SDK = "https://sad.adsgram.ai/js/sad.min.js"   # Adsgram SDK (only this host is allowed)
AG_DUP_WINDOW = 5            # seconds: two Adsgram reward callbacks this close are one retry, not two ads
AG_REWARD_TTL = 600          # seconds an unused Adsgram reward stays valid
SESSION_TTL = 30 * 60        # seconds a gate stays valid
INIT_DATA_MAX_AGE = 2 * 3600  # Telegram initData freshness
MAX_ATTEMPTS = 8             # ad attempts per session (user closing ads early also counts)
MIN_AD_SECONDS = 4           # an ad that "finished" faster than this did not really play
ATTEMPT_GAP = 1              # minimum seconds between two attempts
FALLBACK_DAILY = 3           # max no-fill fallbacks per user per day
NOFILL = {"adsgram_error", "empty feed", "timeout", "network error", "ad is not ready", "failed to open pop",
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


def ag_block() -> str:
    """Adsgram blockId (Reward block = digits, e.g. 12345). '' = not set."""
    b = str(S.get("ads_ag_block") or "").strip()
    return b if re.fullmatch(r"(int-)?\d{1,12}", b) else ""


def ag_ready() -> bool:
    return bool(S.get("ads_ag_enabled")) and bool(ag_block())


def providers() -> list:
    """Networks that can serve an ad right now (order = preference when nothing else decides)."""
    out = []
    if zone():
        out.append("monetag")
    if ag_ready():
        out.append("adsgram")
    return out


def ag_share() -> int:
    v = S.get("ads_ag_share")
    try:
        return max(0, min(100, int(50 if v is None else v)))
    except (TypeError, ValueError):
        return 50


def pick_provider(k: int, provs: list) -> str:
    """Pure rotation: ad number k of ONE user -> network. Even spread, e.g. share 50 = M,A,M,A ... share 70 = 7 of 10 Adsgram."""
    if len(provs) == 1:
        return provs[0]
    share = ag_share()
    ag = ((k + 1) * share) // 100 > (k * share) // 100
    return "adsgram" if ag else "monetag"


async def _plan(uid: int, need: int, provs: list) -> list:
    """The networks for the `need` ads of one gate. A per-user counter makes ONE user see the networks in turn."""
    if len(provs) == 1:
        return [provs[0]] * need
    k0 = 0
    try:
        old = await db.usage_col.find_one_and_update(
            {"_id": f"rot:{uid}"},
            {"$inc": {"n": need}, "$set": {"expire_at": _utcnow() + dt.timedelta(days=90)}}, upsert=True)
        k0 = int((old or {}).get("n", 0))
    except Exception:
        k0 = secrets.randbelow(1000)
    k0 += uid % 100                                        # different users start on different networks
    return [pick_provider(k0 + i, provs) for i in range(need)]


def choose_provider(s: dict, provs: list) -> str:
    """Network for the NEXT attempt of this session. Planned one first; a network that already failed this ad
    (no-fill / blocked) is skipped while another one is still untried -> failover instead of a lost ad."""
    order = s.get("order") or []
    i = int(s.get("done_n", 0))
    pref = order[i] if i < len(order) else provs[0]
    if pref not in provs:
        pref = provs[0]                                    # network switched off in the panel meanwhile
    failed = set(s.get("failed_prov") or [])
    if pref in failed:
        alt = [p for p in provs if p not in failed]
        if alt:
            return alt[0]
    return pref


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
    await db.stat_inc(**{field: 1})                       # same counter per day (Analytics screen)


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
    need = await db.link_ads(link)                       # channel-wise (live): how many ads (0 = none)
    if need <= 0:
        return False
    if is_admin(chat_id) and not S.get("ads_admin_test"):
        return False   # admins skip ads (turn on "Test as admin" in Panel -> Ads to see the gate yourself)
    provs = providers()
    if not provs or not mini_app_ready():
        await notify_owner(bot, "ads_cfg",
                           "⚠️ <b>ADS NOT CONFIGURED</b>\n\n"
                           f"{'NO AD NETWORK IS SET (MONETAG ZONE ID / ADSGRAM BLOCK ID)' if not provs else 'BASE_URL IS NOT A PUBLIC HTTPS URL'}.\n"
                           "FILES GO OUT WITHOUT ADS UNTIL YOU FIX IT IN PANEL ▸ MONETIZATION ▸ ADS.", every=300)
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
    order = await _plan(chat_id, need, provs)
    await db.ads_col.insert_one({
        "_id": sid, "uid": chat_id, "token": link["_id"], "status": "open", "created": now,
        "exp": now + SESSION_TTL, "expire_at": _utcnow() + dt.timedelta(seconds=SESSION_TTL + 600),
        "need": need, "done_n": 0,
        "attempts": 0, "fails": 0, "outcome_n": 0, "attempt_started": 0.0,
        "ad_seen": False, "shown": False, "fallback": False, "blocked": None, "probe_at": 0.0,
        "order": order, "prov": None, "failed_prov": [], "blocked_prov": [],
    })
    try:
        msg = await call(
            bot.send_message, chat_id,
            f"🎬 <b>WATCH {'AN AD' if need == 1 else f'{need} ADS'} TO UNLOCK</b>\n\n"
            "TAP THE BUTTON AND WATCH TILL THE END. YOUR FILES ARRIVE HERE.\n"
            "⚠️ TURN OFF ANY <b>AD BLOCKER / PRIVATE DNS</b> FIRST.",
            reply_markup=M([[B("🎬 WATCH AD & GET FILES",
                               web_app=WebAppInfo(url=f"{config.BASE_URL}/ad?s={sid}"))]] + SUP.row(link["_id"])),
            parse_mode=ParseMode.HTML)
        await db.ads_col.update_one({"_id": sid}, {"$set": {"msg_id": msg.message_id}})
        await db.stat_inc(gates=1)
    except TelegramError as e:
        log.warning("Ad gate could not be sent: %s", e)
        await notify_owner(bot, "ads_send",
                           "⚠️ <b>AD GATE NOT SENT</b>\n\nFILES WERE DELIVERED WITHOUT AN AD.\n"
                           f"<code>{e}</code>\nMINI APP URL ▸ <code>{config.BASE_URL}/ad</code> (PUBLIC HTTPS REQUIRED)", every=300)
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
    need_pb = S.get("ads_ag_postback") if s.get("prov") == "adsgram" else S.get("ads_postback")
    return bool(s.get("ad_seen")) if need_pb else True


async def _ag_try(s: dict) -> bool:
    """Adsgram server verification: use ONE unused reward callback of this user that arrived after this attempt started.
    Atomic (find_one_and_update) -> one reward can never confirm two ads. Updates `s` in place."""
    if s.get("prov") != "adsgram" or s.get("ad_seen") or not s.get("shown") or not S.get("ads_ag_postback"):
        return bool(s.get("ad_seen"))
    got = await db.pb_col.find_one_and_update(
        {"kind": "ag", "uid": s["uid"], "used": False, "t": {"$gte": s.get("attempt_started", 0.0) - 1}},
        {"$set": {"used": True, "sid": s["_id"]}})
    if got:
        await db.ads_col.update_one({"_id": s["_id"], "status": "open"},
                                    {"$set": {"ad_seen": True, "ad_seen_at": time.time(), "ad_value": "adsgram"}})
        s["ad_seen"] = True
    return bool(s.get("ad_seen"))


async def api_init(request):
    body, s = await _auth(request)
    state = "closed" if _closed(s) else "open"
    if s["status"] == "done":
        state = "done"
    return _reply(state=state, zone=zone(), sdk=sdk_url(), tries=int(S.get("ads_tries") or 3),
                  strict=bool(S.get("ads_block_adblock")), postback=bool(S.get("ads_postback")),
                  probes=PROBES, provs=providers(), ag_block=ag_block() if ag_ready() else "", ag_sdk=AG_SDK)


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
    provs = providers()
    if not provs:
        return _reply(ok=False, state="closed")
    prov = choose_provider(s, provs)                          # decided here, on the server
    await db.ads_col.update_one({"_id": s["_id"]}, {"$inc": {"attempts": 1},
                                                    "$set": {"attempt_started": now, "shown": False, "prov": prov,
                                                             "ad_seen": False}})
    return _reply(ok=True, ymid=s["_id"], n=s["attempts"] + 1, prov=prov)


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
    await _ag_try(fresh)
    return _reply(state="ready" if _verified(fresh) else "verifying")


async def api_status(request):
    """wait=1 -> hold the request open (up to ~6s) and answer the instant the postback confirms the ad."""
    body, s = await _auth(request)
    if _closed(s):
        return _reply(state="closed")
    await _ag_try(s)
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
            await _ag_try(fresh)
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
    """The SDK could not show an ad. The SAME ad is retried on the OTHER network first. Only when every network has
    failed and the no-fill limit is reached can the user get files without an ad (and only if the panel allows it)."""
    body, s = await _auth(request)
    if _closed(s):
        return _reply(state="closed")
    if s["outcome_n"] >= s["attempts"]:
        return _reply(state="no_attempt")
    reason = str(body.get("reason", "")).strip().lower()[:60]
    prov = s.get("prov") or "monetag"                       # the network the SERVER chose, never the client's word
    strict = bool(S.get("ads_block_adblock"))
    await db.ads_col.update_one({"_id": s["_id"], "outcome_n": s["outcome_n"]},
                                {"$set": {"outcome_n": s["attempts"]}})
    provs = providers()

    if reason == "sdk_blocked" and strict:                  # this network's SDK host is unreachable -> blocked by DNS / blocker
        await db.ads_col.update_one({"_id": s["_id"]}, {"$addToSet": {"failed_prov": prov, "blocked_prov": prov}})
        if set(provs) <= set(s.get("blocked_prov") or []) | {prov}:
            await bump("blocked")
            return _reply(state="blocked")                  # every network is blocked for this user -> no ad, no files
        return _reply(state="retry", fails=s["fails"])      # the other network may still work

    if reason != "sdk_blocked" and reason not in NOFILL:    # user closed / skipped the ad -> not a no-fill
        return _reply(state="retry", fails=s["fails"])

    fails = s["fails"] + 1
    await db.ads_col.update_one({"_id": s["_id"]}, {"$set": {"fails": fails}, "$addToSet": {"failed_prov": prov}})
    tried = set(s.get("failed_prov") or []) | {prov}
    tries = int(S.get("ads_tries") or 3)
    if fails < tries or not set(provs) <= tried:            # not enough tries / a network was not even tried yet
        return _reply(state="retry", fails=fails)

    # ---- fallback decision (all checked on the server)
    if not S.get("ads_nofill_free"):
        return _reply(state="no_fallback")                  # strict mode: no ad = no files, never
    if strict and not _clean_probe(s):
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
    await _ag_try(s)
    if not _verified(s):
        return _reply(ok=False, state="not_verified")
    need, done_n = int(s.get("need", 1)), int(s.get("done_n", 0))
    if not s.get("fallback") and done_n + 1 < need:        # more ads to watch: reset the session for the next one
        nxt = await db.ads_col.find_one_and_update(
            {"_id": s["_id"], "status": "open", "done_n": done_n},
            {"$set": {"attempts": 0, "fails": 0, "outcome_n": 0, "attempt_started": 0.0, "ad_seen": False,
                      "shown": False, "exp": time.time() + SESSION_TTL, "prov": None,
                      "failed_prov": [], "blocked_prov": [],
                      "expire_at": _utcnow() + dt.timedelta(seconds=SESSION_TTL + 600)},
             "$inc": {"done_n": 1}})
        if nxt:
            await bump("verified")
            await bump("ag_ok" if s.get("prov") == "adsgram" else "mt_ok")
        return _reply(ok=True, state="next", done=done_n + 1, need=need)
    taken = await db.ads_col.find_one_and_update({"_id": s["_id"], "status": "open"},
                                                 {"$set": {"status": "done", "done_at": time.time()}})
    if not taken:                                           # lost a race with a second claim: files go out once
        return _reply(ok=True, state="done")
    await bump("fallback" if s.get("fallback") else "verified")
    if not s.get("fallback"):
        await bump("ag_ok" if s.get("prov") == "adsgram" else "mt_ok")
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
        await call(bot.send_message, s["uid"], "❌ <b>INVALID LINK</b>\n\nTHIS LINK IS WRONG OR REVOKED.")
        return
    try:
        await start.deliver(bot, s["uid"], link)
    except Exception:
        log.exception("Delivery after ad failed")


# ------------------------------------------------------------------ Monetag postback (server to server)
def _price(raw) -> float:
    """estimated_price from the postback (USD). Anything odd counts as 0."""
    try:
        v = float(str(raw or "").strip())
    except ValueError:
        return 0.0
    return v if math.isfinite(v) and 0 < v < 1000 else 0.0


DUP_WINDOW = 30   # seconds: an identical postback inside this window is a retry, not a new ad (an ad takes > MIN_AD_SECONDS)


async def _is_duplicate(q) -> bool:
    key = hashlib.sha1("|".join(q.get(k, "") for k in (
        "ymid", "zone_id", "sub_zone_id", "event_type", "reward_event_type", "telegram_id", "estimated_price"
    )).encode()).hexdigest()
    now = time.time()
    try:
        old = await db.pb_col.find_one_and_update(
            {"_id": key}, {"$set": {"t": now, "expire_at": dt.datetime.utcfromtimestamp(now + 300)}},
            upsert=True)                                   # returns the previous doc (None if new)
    except Exception:
        return False
    return bool(old) and now - (old.get("t") or 0) < DUP_WINDOW


async def postback(request):
    q = request.query
    if not hmac.compare_digest(q.get("secret", ""), S.get("ads_secret") or secrets.token_hex(8)):
        raise web.HTTPForbidden(text="bad secret")
    sid = q.get("ymid", "")[:64]
    if time.time() - (S.get("ads_last_postback") or 0) > 60:
        await S.set("ads_last_postback", time.time())
    ev_type = q.get("event_type")
    if ev_type in ("impression", "click"):                 # Analytics: ads shown, clicks and Monetag's estimated revenue
        if await _is_duplicate(q):                         # Monetag retries / double-sends -> count once
            return web.Response(text="ok")
        inc = {"imp" if ev_type == "impression" else "clk": 1}
        price = _price(q.get("estimated_price"))
        if price and ev_type == "impression":              # revenue only on the impression, never again on its click
            inc["rev"] = price
        if ev_type == "impression" and q.get("reward_event_type") == "valued":
            inc["valued"] = 1
        await db.earn_inc(**inc)                           # UTC day, like Monetag
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


async def ag_reward_url() -> str:
    """Paste this as 'Reward URL' of your Adsgram Reward block ([userId] is replaced by Adsgram itself)."""
    sec = await ensure_secret()
    return f"{config.BASE_URL}/adsgram/reward?secret={sec}&userid=[userId]"


async def ag_reward(request):
    """Adsgram -> our server, after a user really finished an Adsgram ad. Stored as ONE unused reward of that user."""
    q = request.query
    if not hmac.compare_digest(q.get("secret", ""), S.get("ads_secret") or secrets.token_hex(8)):
        raise web.HTTPForbidden(text="bad secret")
    uid_raw = q.get("userid", "").strip()
    if not uid_raw.isdigit() or len(uid_raw) > 15:
        return web.Response(text="ok")
    uid, now = int(uid_raw), time.time()
    if now - (S.get("ads_ag_last") or 0) > 60:
        await S.set("ads_ag_last", now)
    try:                                                      # a retry of the same callback is not a second ad
        old = await db.pb_col.find_one_and_update(
            {"_id": f"agl:{uid}"}, {"$set": {"t": now, "expire_at": dt.datetime.utcfromtimestamp(now + 300)}}, upsert=True)
        if old and now - (old.get("t") or 0) < AG_DUP_WINDOW:
            return web.Response(text="ok")
    except Exception:
        pass
    await db.pb_col.insert_one({"_id": f"ag:{uid}:{secrets.token_hex(6)}", "kind": "ag", "uid": uid, "t": now,
                                "used": False, "expire_at": dt.datetime.utcfromtimestamp(now + AG_REWARD_TTL)})
    await db.earn_inc(ag_imp=1)                               # Adsgram ads shown (revenue: see the Adsgram dashboard)
    async for s in db.ads_col.find({"uid": uid, "status": "open", "prov": "adsgram"}, {"_id": 1}):
        ev = _seen.get(s["_id"])
        if ev:
            ev.set()                                          # wake the waiting Mini App at once
    return web.Response(text="ok")


def _boot_json() -> str:
    return json.dumps({"zone": zone(), "sdk": sdk_url(), "tries": int(S.get("ads_tries") or 3),
                       "strict": bool(S.get("ads_block_adblock")), "postback": bool(S.get("ads_postback")),
                       "probes": PROBES, "support": SUP.url() or "",
                       "provs": providers(), "ag_block": ag_block() if ag_ready() else "", "ag_sdk": AG_SDK}).replace("</", "<\\/")


async def page(request):
    return web.Response(text=PAGE.replace("__BOOT__", _boot_json()), content_type="text/html", charset="utf-8", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


GUIDE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "guide")
GUIDE_FILES = {"step1-settings.png", "step2-search.png", "step3-provider.png", "step4-off.png"}


async def guide_image(request):
    """Private-DNS how-to screenshots shown on the 'Ad blocker detected' screen. Served as-is (original, uncompressed)."""
    name = request.match_info["name"]
    path = os.path.join(GUIDE_DIR, name)
    if name not in GUIDE_FILES or not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": "image/png", "Cache-Control": "public, max-age=86400",
                                           "X-Content-Type-Options": "nosniff"})


def setup_routes(app: web.Application):
    app.router.add_get("/ad", page)
    app.router.add_get("/assets/guide/{name}", guide_image)
    app.router.add_post("/api/ad/init", api_init)
    app.router.add_post("/api/ad/probe", api_probe)
    app.router.add_post("/api/ad/attempt", api_attempt)
    app.router.add_post("/api/ad/shown", api_shown)
    app.router.add_post("/api/ad/status", api_status)
    app.router.add_post("/api/ad/fail", api_fail)
    app.router.add_post("/api/ad/claim", api_claim)
    app.router.add_get("/monetag/postback", postback)
    app.router.add_get("/adsgram/reward", ag_reward)
