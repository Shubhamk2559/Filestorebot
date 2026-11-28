"""
handlers/analytics.py
Earnings screens of the admin panel (owner only).

  Panel -> 💰 Monetization -> 📈 Earnings
      summary         Today / 7 days / 30 days / All time earned + eCPM
      📅 Daily        last 14 days        🗓 Monthly   last 12 months
      🏆 Top Links    most opened links   📊 Traffic   views, ads, clicks, users ...

Where the numbers come from
  * earnings, ads shown, clicks   Monetag postback (estimated_price) - counted per day from the moment
                                  this feature is deployed. Monetag's own dashboard has the final payout.
  * views, ad gates, verified...  counted per day by the bot (collection daily_stats)
  * new users, links, files       computed from the existing data, so their history is complete
"""
import datetime as dt
import html
import logging

from database import db
from handlers.admin import action, show
from utils import ads
from utils.helpers import owner_only
from utils.keyboards import grid, kb, ub
from utils.ui import table as _table

log = logging.getLogger(__name__)
TZ_NAME = "Asia/Kolkata"


# ------------------------------------------------------------------ formatting
def _n(x) -> str:
    x = int(x or 0)
    if x < 1000:
        return str(x)
    if x < 10000:
        return f"{x / 1000:.1f}k"
    if x < 1_000_000:
        return f"{x / 1000:.0f}k"
    return f"{x / 1_000_000:.1f}M"


def _m(x) -> str:
    x = float(x or 0)
    if x < 1000:
        return f"${x:,.2f}"
    return f"${x / 1000:.1f}k"


# ------------------------------------------------------------------ data
def _today() -> dt.date:
    return dt.datetime.now(db.LIMIT_TZ).date()


def _since_ts(day: dt.date) -> float:
    return dt.datetime.combine(day, dt.time.min, tzinfo=db.LIMIT_TZ).timestamp()


async def _per_period(col, field: str, since: float, fmt: str) -> dict:
    """{'2026-11-14': count} grouped in IST from a unix-time field (users.joined, links.created, files.date)."""
    pipe = [
        {"$match": {field: {"$gte": since}}},
        {"$group": {"_id": {"$dateToString": {"format": fmt, "timezone": TZ_NAME,
                                              "date": {"$toDate": {"$multiply": [f"${field}", 1000]}}}},
                    "n": {"$sum": 1}}},
    ]
    try:
        return {d["_id"]: d["n"] async for d in col.aggregate(pipe)}
    except Exception:
        log.exception("Analytics aggregation failed")
        return {}


EARN = ("rev", "imp", "clk", "valued")   # earning numbers: counted per UTC day (Monetag's day); the rest per IST day


def _today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _range_utc(n: int):
    t = _today_utc()
    return [t - dt.timedelta(days=i) for i in range(n)]


async def _daily_docs(first: str = "") -> dict:
    q = {"_id": {"$gte": first}} if first else {}
    docs = {d["_id"]: d async for d in db.stats_col.find(q)}
    async for e in db.earn_col.find(q):                    # days tracked in UTC replace the old IST earning numbers
        x = docs.setdefault(e["_id"], {"_id": e["_id"]})
        for f in EARN:
            x[f] = e.get(f, 0) or 0
    return docs


def _sum(docs: dict, days, field: str):
    if field in EARN:                                      # "today", "7 days"... for earnings are UTC days
        days = _range_utc(len(days))
    return sum(docs.get(str(d), {}).get(field, 0) or 0 for d in days)


def _sum_map(m: dict, days) -> int:
    return sum(m.get(str(d), 0) for d in days)


def _range(n: int):
    t = _today()
    return [t - dt.timedelta(days=i) for i in range(n)]


# ------------------------------------------------------------------ screens
def _kb(active: str = ""):
    return kb(grid([("📅 Daily", "adm:an:d"), ("🗓 Monthly", "adm:an:m"),
                    ("🏆 Top Links", "adm:an:t"), ("📊 Traffic", "adm:an:x"),
                    ("🔄 Refresh", f"adm:an:{active}" if active else "adm:an"), ("📺 Ads", "adm:ads")]),
              ub("🔙 Back", "adm:monet"))


@action("an")
@owner_only
async def a_an(update, context, args):
    view = args[0] if args else ""
    if view == "d":
        await _daily(update)
    elif view == "m":
        await _monthly(update)
    elif view == "t":
        await _top(update)
    elif view == "x":
        await _traffic(update)
    else:
        await _summary(update)


async def _summary(update):
    d30 = _range(30)
    docs = await _daily_docs(str(d30[-1]))
    all_docs = await _daily_docs()
    tot_rev = sum(d.get("rev", 0) or 0 for d in all_docs.values())
    tot_imp = sum(d.get("imp", 0) or 0 for d in all_docs.values())
    rev30, imp30 = _sum(docs, d30, "rev"), _sum(docs, d30, "imp")
    views30 = _sum(docs, d30, "views")
    ecpm = _m(rev30 / imp30 * 1000) if imp30 else "—"
    per_view = _m(rev30 / views30) if views30 else "—"
    rows = [["TODAY", _m(_sum(docs, _range(1), "rev"))],
            ["7 DAYS", _m(_sum(docs, _range(7), "rev"))],
            ["30 DAYS", _m(rev30)],
            ["ALL TIME", _m(tot_rev)]]

    note = ""
    if not ads.providers() or not ads.mini_app_ready():
        note = "\n⚠️ ADS ARE NOT SET UP YET."
    elif tot_imp and not tot_rev:
        note = "\n⚠️ MONETAG SENDS NO PRICE. CHECK THE POSTBACK URL IN 📺 ADS."
    elif not tot_imp:
        note = "\nℹ️ NO AD POSTBACKS YET."

    await show(
        update,
        "📈 <b>EARNINGS</b>\n\n"
        + _table(rows, [10, 10]) +
        f"🎯 eCPM ▸ <b>{ecpm}</b> · PER VIEW ▸ <b>{per_view}</b>\n"
        "<i>MONETAG ESTIMATE · DAYS CHANGE AT 12:00 UTC (5:30 AM IST), LIKE MONETAG.</i>"
        + ("\n<i>ADSGRAM EARNINGS: SEE partner.adsgram.ai (NOT INCLUDED ABOVE).</i>" if ads.ag_ready() else "") + note,
        _kb(),
    )


async def _traffic(update):
    d30 = _range(30)
    docs = await _daily_docs(str(d30[-1]))
    since = _since_ts(d30[-1])
    users30 = await _per_period(db.users_col, "joined", since, "%Y-%m-%d")
    links30 = await _per_period(db.links_col, "created", since, "%Y-%m-%d")
    files30 = await _per_period(db.files_col, "date", since, "%Y-%m-%d")
    all_docs = await _daily_docs()
    tot = {k: sum(d.get(k, 0) or 0 for d in all_docs.values())
           for k in ("imp", "clk", "views", "gates", "verified", "fallback", "blocked")}
    st = await ads.get_stats()                              # lifetime ad counters (older than daily tracking)
    views_all = 0
    try:
        async for r in db.links_col.aggregate([{"$group": {"_id": None, "v": {"$sum": "$views"}}}]):
            views_all = r["v"]
    except Exception:
        pass
    n_users, n_links, n_files = (await db.users_col.count_documents({}), await db.links_col.count_documents({}),
                                 await db.files_col.count_documents({}))
    cols = [_range(1), _range(7), d30]

    def row(label, f_docs, all_val):
        return [label] + [_n(f_docs(c)) for c in cols] + [_n(all_val)]

    rows = [["", "TODAY", "7D", "30D", "ALL"],
            row("VIEWS", lambda c: _sum(docs, c, "views"), max(views_all, tot["views"])),
            row("ADS", lambda c: _sum(docs, c, "imp"), tot["imp"]),
            row("CLICKS", lambda c: _sum(docs, c, "clk"), tot["clk"]),
            row("GATES", lambda c: _sum(docs, c, "gates"), tot["gates"]),
            row("VERIFIED", lambda c: _sum(docs, c, "verified"), max(st.get("verified", 0), tot["verified"])),
            row("FREE", lambda c: _sum(docs, c, "fallback"), max(st.get("fallback", 0), tot["fallback"])),
            row("BLOCKED", lambda c: _sum(docs, c, "blocked"), max(st.get("blocked", 0), tot["blocked"])),
            row("USERS", lambda c: _sum_map(users30, c), n_users),
            row("LINKS", lambda c: _sum_map(links30, c), n_links),
            row("FILES", lambda c: _sum_map(files30, c), n_files)]
    await show(update, "📊 <b>TRAFFIC</b>\n\n" + _table(rows, [9, 7, 6, 6, 6]), _kb("x"))


async def _daily(update):
    days = _range(14)
    docs = await _daily_docs(str(days[-1]))
    since = _since_ts(days[-1])
    users = await _per_period(db.users_col, "joined", since, "%Y-%m-%d")
    rows = [["DATE", "EARNED", "VIEWS", "ADS", "USERS"]]
    for d in days:
        x = docs.get(str(d), {})
        rows.append([d.strftime("%d %b"), _m(x.get("rev")), _n(x.get("views")), _n(x.get("imp")),
                     _n(users.get(str(d), 0))])
    await show(update, "📅 <b>LAST 14 DAYS</b>\n\n" + _table(rows, [7, 8, 7, 7, 7]), _kb("d"))


async def _monthly(update):
    t = _today()
    months = []
    y, m = t.year, t.month
    for _ in range(12):
        months.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    first = months[-1] + "-01"
    docs = await _daily_docs(first)
    since = _since_ts(dt.date.fromisoformat(first))
    users = await _per_period(db.users_col, "joined", since, "%Y-%m")
    agg = {}
    for day, x in docs.items():
        a = agg.setdefault(day[:7], {"rev": 0, "views": 0, "imp": 0})
        for k in a:
            a[k] += x.get(k, 0) or 0
    rows = [["MONTH", "EARNED", "VIEWS", "ADS", "USERS"]]
    for mo in months:
        a = agg.get(mo, {})
        label = dt.date.fromisoformat(mo + "-01").strftime("%b %y")
        rows.append([label, _m(a.get("rev")), _n(a.get("views")), _n(a.get("imp")), _n(users.get(mo, 0))])
    await show(update, "🗓 <b>LAST 12 MONTHS</b>\n\n" + _table(rows, [7, 8, 7, 7, 7]), _kb("m"))


async def _top(update):
    top = await db.links_col.find({"views": {"$gt": 0}}).sort("views", -1).limit(10).to_list(10)
    if not top:
        text = "🏆 <b>TOP LINKS</b>\n\nNO LINK HAS BEEN OPENED YET."
    else:
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"{medal.get(i, f'{i}.')} {html.escape((l.get('label') or 'Link')[:30])} ▸ <b>{_n(l.get('views'))}</b>"
                 for i, l in enumerate(top, 1)]
        text = "🏆 <b>TOP LINKS</b>\n\n" + "\n".join(lines)
    await show(update, text, _kb("t"))
