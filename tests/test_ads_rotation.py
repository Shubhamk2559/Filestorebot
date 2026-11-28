"""
tests/test_ads_rotation.py - Monetag + Adsgram rotation, failover and anti-bypass.
Reuses the in-memory Mongo / Telegram stubs of test_channel_opts.py. Run:  python tests/test_ads_rotation.py
"""
import asyncio, copy, datetime as dt, json, os, sys, time
from unittest.mock import AsyncMock, MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "test_channel_opts.py"), encoding="utf-8").read()
exec(compile(src.split("fails = []")[0], "test_channel_opts_preamble", "exec"))   # stubs + fake Mongo + imports

# fake Mongo: add upsert for find_one_and_update and $addToSet
_old_apply = _apply
def _apply2(doc, upd):
    _old_apply(doc, upd)
    for k, v in upd.get("$addToSet", {}).items():
        lst = doc.setdefault(k, [])
        if v not in lst:
            lst.append(v)
_apply = _apply2
globals()["_apply"] = _apply2

async def _foau(self, flt, upd, upsert=False, **kw):
    for doc in self.d.values():
        if _match(doc, flt):
            before = copy.deepcopy(doc)
            _apply2(doc, upd)
            return before
    if upsert and "_id" in flt:
        doc = {"_id": flt["_id"]}
        _apply2(doc, upd)
        self.d[doc["_id"]] = doc
    return None
Col.find_one_and_update = _foau
db.pb_col = Col()
db.ads_col = Col(); db.usage_col = Col(); db.earn_col = Col(); db.stats_col = Col()

from utils import ads  # noqa: E402
import utils.ads as A  # noqa: E402

fails = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)

UID = 555
ads.zone = lambda: "13042"
ads.mini_app_ready = lambda: True
ads._reply = lambda **kw: kw
S._data.update(ads_enabled=True, ads_ag_enabled=True, ads_ag_block="12345", ads_ag_share=50,
               ads_ag_postback=True, ads_postback=True, ads_block_adblock=False, ads_tries=3, ads_nofill_free=False)

def bot():
    b = MagicMock(); b.username = "bot"
    b.send_message = AsyncMock(return_value=MagicMock(message_id=9)); b.delete_message = AsyncMock()
    return b

def use_session(sid, uid=UID):
    async def fake_auth(request):
        return await request.json(), await db.ads_col.find_one({"_id": sid})
    ads._auth = fake_auth

def req(body=None, q=None):
    r = MagicMock(); r.app = {"bot": bot()}; r.query = q or {}
    async def j(): return body or {}
    r.json = j
    return r

async def new_gate(need=1, uid=UID):
    await db.ads_col.update_many({"uid": uid, "status": "open"}, {"$set": {"status": "expired"}})
    link = {"_id": "TOK", "ids": [1]}
    async def fake_need(l): return need
    db.link_ads = fake_need
    ok = await ads.gate(bot(), uid, link)
    return await db.ads_col.find_one({"uid": uid, "status": "open"})

async def main():
    print("1) pure rotation")
    P = ["monetag", "adsgram"]
    S._data["ads_ag_share"] = 50
    seq = [ads.pick_provider(k, P) for k in range(6)]
    check(seq == ["monetag", "adsgram"] * 3, f"share 50 alternates: {seq}")
    S._data["ads_ag_share"] = 70
    check(sum(ads.pick_provider(k, P) == "adsgram" for k in range(10)) == 7, "share 70 -> 7 of 10 Adsgram")
    S._data["ads_ag_share"] = 0
    check(all(ads.pick_provider(k, P) == "monetag" for k in range(10)), "share 0 -> Monetag only")
    S._data["ads_ag_share"] = 100
    check(all(ads.pick_provider(k, P) == "adsgram" for k in range(10)), "share 100 -> Adsgram only")
    check(ads.pick_provider(3, ["monetag"]) == "monetag", "one network configured -> that one")
    S._data["ads_ag_share"] = 50

    print("2) one user sees both networks in turn")
    seen = []
    for _ in range(6):
        s = await new_gate(1)
        seen.append(s["order"][0])
    check(len(set(seen)) == 2 and all(seen[i] != seen[i + 1] for i in range(5)), f"1-ad gates alternate: {seen}")
    s = await new_gate(2)
    check(sorted(s["order"]) == ["adsgram", "monetag"], f"2-ad gate = one of each: {s['order']}")

    print("3) failover to the other network")
    sess = {"order": ["monetag"], "done_n": 0, "failed_prov": []}
    check(ads.choose_provider(sess, P) == "monetag", "planned network first")
    sess["failed_prov"] = ["monetag"]
    check(ads.choose_provider(sess, P) == "adsgram", "monetag failed -> adsgram")
    sess["failed_prov"] = ["monetag", "adsgram"]
    check(ads.choose_provider(sess, P) == "monetag", "both failed -> planned one again")

    print("4) Adsgram: server verification (reward URL), one reward = one ad")
    s = await new_gate(2)
    sid = s["_id"]; use_session(sid)
    # force first ad on adsgram
    await db.ads_col.update_one({"_id": sid}, {"$set": {"order": ["adsgram", "adsgram"]}})
    a = await ads.api_attempt(req())
    check(a["ok"] and a["prov"] == "adsgram", "server picked adsgram for the attempt")
    await db.ads_col.update_one({"_id": sid}, {"$set": {"attempt_started": time.time() - 6}})
    r = await ads.api_shown(req())
    check(r["state"] == "verifying", f"ad finished on the client but NO reward callback yet -> {r['state']}")
    c = await ads.api_claim(req())
    check(not c["ok"] and c["state"] == "not_verified", "claim without reward is refused")
    bad = None
    try:
        await ads.ag_reward(req(q={"secret": "wrong", "userid": str(UID)}))
    except Exception as e:
        bad = e
    check(bad is not None, "reward callback with a wrong secret is rejected")
    S._data["ads_secret"] = "sec"
    await ads.ag_reward(req(q={"secret": "sec", "userid": str(UID)}))
    await ads.ag_reward(req(q={"secret": "sec", "userid": str(UID)}))          # retry of the same callback
    n_rewards = len([d for d in db.pb_col.d.values() if d.get("kind") == "ag"])
    check(n_rewards == 1, "a retried callback is stored once")
    c = await ads.api_claim(req())
    check(c["ok"] and c["state"] == "next", f"after reward: ad 1 counted, 2nd ad next -> {c['state']}")
    # second ad: the same (already used) reward must not count again
    a = await ads.api_attempt(req())
    await db.ads_col.update_one({"_id": sid}, {"$set": {"attempt_started": time.time() - 6, "shown": True}})
    c = await ads.api_claim(req())
    check(not c["ok"] and c["state"] == "not_verified", "old reward can NOT confirm the next ad")
    other = await ads.api_claim(req())
    check(not other["ok"], "still no files without a second reward")

    print("5) no-fill on every network -> strict: no files")
    s = await new_gate(1)
    sid = s["_id"]; use_session(sid)
    await db.ads_col.update_one({"_id": sid}, {"$set": {"order": ["monetag"]}})
    states = []
    for _ in range(3):
        a = await ads.api_attempt(req())
        await db.ads_col.update_one({"_id": sid}, {"$set": {"attempt_started": 0.0}})
        f = await ads.api_fail(req({"reason": "empty feed"}))
        states.append((a["prov"], f["state"]))
    check(states[0][0] == "monetag" and states[1][0] == "adsgram", f"failover on the 2nd try: {states}")
    check(states[-1][1] == "no_fallback", "all networks failed, strict -> no free files")
    check((await db.ads_col.find_one({"_id": sid}))["fallback"] is False, "session never marked as fallback")
    S._data["ads_nofill_free"] = True
    a = await ads.api_attempt(req()); await db.ads_col.update_one({"_id": sid}, {"$set": {"attempt_started": 0.0}})
    f = await ads.api_fail(req({"reason": "empty feed"}))
    check(f["state"] == "ready", "with 'No-fill free' ON the old behaviour is back")
    S._data["ads_nofill_free"] = False

    print("6) blocked SDK hosts (strict)")
    S._data["ads_block_adblock"] = True
    s = await new_gate(1)
    sid = s["_id"]; use_session(sid)
    await db.ads_col.update_one({"_id": sid}, {"$set": {"order": ["monetag"], "probe_at": time.time(), "blocked": False}})
    a = await ads.api_attempt(req()); await db.ads_col.update_one({"_id": sid}, {"$set": {"attempt_started": 0.0}})
    f = await ads.api_fail(req({"reason": "sdk_blocked"}))
    check(f["state"] == "retry", "one network blocked -> retry on the other")
    a = await ads.api_attempt(req())
    check(a["ok"] and a["prov"] == "adsgram", "next attempt uses adsgram")
    await db.ads_col.update_one({"_id": sid}, {"$set": {"attempt_started": 0.0}})
    f = await ads.api_fail(req({"reason": "sdk_blocked"}))
    check(f["state"] == "blocked", "both networks blocked -> blocked screen, no files")

    print("7) Adsgram off -> Monetag only, plan has no counter")
    S._data["ads_ag_enabled"] = False
    check(ads.providers() == ["monetag"], "providers() = monetag")
    s = await new_gate(2)
    check(s["order"] == ["monetag", "monetag"], "2-ad gate all Monetag")

    print()
    print("FAILED:" if fails else "ALL OK")
    for f in fails: print(" -", f)
    sys.exit(1 if fails else 0)

asyncio.run(main())
