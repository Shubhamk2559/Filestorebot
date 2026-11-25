"""
handlers/analytics.py
Earnings & Analytics screens of the admin panel (owner only).

  Panel -> 📈 Earnings
      summary table   Today / 7 days / 30 days / All time
      📅 Daily        last 14 days        🗓 Monthly   last 12 months        🏆 Top links

Where the numbers come from
  * earnings, ads shown, clicks   Monetag postback (estimated_price) - counted per day from the moment
                                  this feature is deployed. Monetag's own dashboard has the final payout.
  * views, ad gates, verified...  counted per day by the bot (collection daily_stats)
  * new users, links, files       computed from the existing data, so their history is complete
"""
import datetime as dt
import html
import logging

from telegram import InlineKeyboardMarkup as M

from database import db
from handlers.admin import B, action, show
from utils import ads
from utils.helpers import owner_only

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


def _table(rows, widths) -> str:
    out = []
    for r in rows:
        out.append(r[0].ljust(widths[0]) + "".join(str(c).rjust(w) for c, w in zip(r[1:], widths[1:])))
    return "<pre>" + html.escape("\n".join(out)) + "</pre>"


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


async def _daily_docs(first: str = "") -> dict:
    q = {"_id": {"$gte": first}} if first else {}
    return {d["_id"]: d async for d in db.stats_col.find(q)}


def _sum(docs: dict, days, field: str):
    return sum(docs.get(str(d), {}).get(field, 0) or 0 for d in days)


def _sum_map(m: dict, days) -> int:
    return sum(m.get(str(d), 0) for d in days)


def _range(n: int):
    t = _today()
    return [t - dt.timedelta(days=i) for i in range(n)]


# ------------------------------------------------------------------ screens
def _kb(active: str = ""):
    return M([
        [B("📅 Daily", "adm:an:d"), B("🗓 Monthly", "adm:an:m"), B("🏆 Top links", "adm:an:t")],
        [B("🔄 Refresh", f"adm:an:{active}" if active else "adm:an"), B("📺 Ads", "adm:ads")],
        [B("🔙 Back", "adm:home")],
    ])


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
    else:
        await _summary(update)


async def _summary(update):
    d30 = _range(30)
    first30 = str(d30[-1])
    docs, all_docs = await _daily_docs(first30), None
    since = _since_ts(d30[-1])
    users30 = await _per_period(db.users_col, "joined", since, "%Y-%m-%d")
    links30 = await _per_period(db.links_col, "created", since, "%Y-%m-%d")
    files30 = await _per_period(db.files_col, "date", since, "%Y-%m-%d")

    all_docs = await _daily_docs()
    tot = {k: sum(d.get(k, 0) or 0 for d in all_docs.values())
           for k in ("rev", "imp", "clk", "views", "gates", "verified", "fallback", "blocked")}
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

    def row(label, f_docs, fmt, all_val):
        return [label] + [fmt(f_docs(c)) for c in cols] + [fmt(all_val)]

    rows = [["", "Today", "7d", "30d", "All"],
            row("Earned", lambda c: _sum(docs, c, "rev"), _m, tot["rev"]),
            row("Views", lambda c: _sum(docs, c, "views"), _n, max(views_all, tot["views"])),
            row("Ads shown", lambda c: _sum(docs, c, "imp"), _n, tot["imp"]),
            row("Clicks", lambda c: _sum(docs, c, "clk"), _n, tot["clk"]),
            row("Ad gates", lambda c: _sum(docs, c, "gates"), _n, tot["gates"]),
            row("Verified", lambda c: _sum(docs, c, "verified"), _n, max(st.get("verified", 0), tot["verified"])),
            row("Free files", lambda c: _sum(docs, c, "fallback"), _n, max(st.get("fallback", 0), tot["fallback"])),
            row("Blocked", lambda c: _sum(docs, c, "blocked"), _n, max(st.get("blocked", 0), tot["blocked"])),
            row("New users", lambda c: _sum_map(users30, c), _n, n_users),
            row("Links", lambda c: _sum_map(links30, c), _n, n_links),
            row("Files", lambda c: _sum_map(files30, c), _n, n_files)]

    imp30, rev30 = _sum(docs, d30, "imp"), _sum(docs, d30, "rev")
    ecpm = f"{_m(rev30 / imp30 * 1000)}" if imp30 else "—"
    per_view = f"{_m(rev30 / _sum(docs, d30, 'views'))}" if _sum(docs, d30, "views") else "—"
    since_txt = min(all_docs) if all_docs else "not yet"

    note = ""
    if not ads.zone() or not ads.mini_app_ready():
        note = "\n⚠️ Ads are not configured, so there are no earnings to count."
    elif tot["imp"] and not tot["rev"]:
        note = ("\n⚠️ Monetag sends no price yet. Make sure the Postback URL in Monetag is the one "
                "shown in 📺 Ads (it contains <code>estimated_price</code>).")
    elif not tot["imp"]:
        note = "\nℹ️ No ad postbacks received yet."

    await show(
        update,
        "📈 <b>Earnings &amp; Analytics</b>\n"
        "<i>Days change at 12:00 AM IST</i>\n\n"
        + _table(rows, [10, 7, 7, 7, 7]) +
        f"\n🎯 eCPM (30d): <b>{ecpm}</b> per 1000 ads · per view: <b>{per_view}</b>\n"
        f"🕒 Earnings &amp; ads counted since: <b>{since_txt}</b>\n"
        "💡 Earnings are Monetag's estimate; the final payout is in your Monetag dashboard."
        + note,
        _kb(),
    )


async def _daily(update):
    days = _range(14)
    docs = await _daily_docs(str(days[-1]))
    since = _since_ts(days[-1])
    users = await _per_period(db.users_col, "joined", since, "%Y-%m-%d")
    rows = [["Date", "Earned", "Views", "Ads", "Users"]]
    for d in days:
        x = docs.get(str(d), {})
        rows.append([d.strftime("%d %b"), _m(x.get("rev")), _n(x.get("views")), _n(x.get("imp")),
                     _n(users.get(str(d), 0))])
    await show(update, "📅 <b>Last 14 days</b>\n\n" + _table(rows, [7, 8, 7, 7, 7]), _kb("d"))


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
    rows = [["Month", "Earned", "Views", "Ads", "Users"]]
    for mo in months:
        a = agg.get(mo, {})
        label = dt.date.fromisoformat(mo + "-01").strftime("%b %y")
        rows.append([label, _m(a.get("rev")), _n(a.get("views")), _n(a.get("imp")), _n(users.get(mo, 0))])
    await show(update, "🗓 <b>Last 12 months</b>\n\n" + _table(rows, [7, 8, 7, 7, 7]), _kb("m"))


async def _top(update):
    top = await db.links_col.find({"views": {"$gt": 0}}).sort("views", -1).limit(10).to_list(10)
    if not top:
        text = "🏆 <b>Top links</b>\n\nNo link has been opened yet."
    else:
        lines = [f"{i}. {html.escape((l.get('label') or 'Link')[:30])} - <b>{_n(l.get('views'))}</b> views"
                 for i, l in enumerate(top, 1)]
        text = "🏆 <b>Top links</b>\n\n" + "\n".join(lines)
    await show(update, text, _kb("t"))
