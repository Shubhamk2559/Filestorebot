"""
handlers/usage.py
VPS Usage screens of the admin panel (owner only).

  Panel -> 🖥 VPS Usage
      summary    right now + Today / 7d / 30d
      📅 Daily   last 7 days       📆 Weekly   last 8 weeks      🗓 Monthly   last 6 months + usage patterns
      💡 Plan advice   cheapest Contabo plan that fits your real usage

Numbers are collected by utils/usage.py (hourly documents in MongoDB, IST days).
"""
import datetime as dt
import html
import logging

from telegram import InlineKeyboardMarkup as M

from handlers.admin import B, action, show
from utils import usage as U
from utils.helpers import owner_only

log = logging.getLogger(__name__)
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ------------------------------------------------------------------ formatting
def _table(rows, widths) -> str:
    out = []
    for r in rows:
        out.append(str(r[0]).ljust(widths[0]) + "".join(str(c).rjust(w) for c, w in zip(r[1:], widths[1:])))
    return "<pre>" + html.escape("\n".join(out)) + "</pre>"


def _bytes(n) -> str:
    n = float(n or 0)
    if n >= 1e12:
        return f"{n / 1e12:.2f}TB"
    if n >= 1e9:
        return f"{n / 1e9:.1f}GB"
    if n >= 1e6:
        return f"{n / 1e6:.0f}MB"
    return f"{n / 1e3:.0f}KB" if n else "0"


def _pk(avg, peak) -> str:
    return f"{avg:.0f}/{peak:.0f}"


def _mbps(x) -> str:
    x = float(x or 0)
    return f"{x:.0f}" if x >= 10 else f"{x:.1f}"


def _kb(active: str = ""):
    return M([
        [B("📅 Daily", "adm:vps:d"), B("📆 Weekly", "adm:vps:w"), B("🗓 Monthly", "adm:vps:m")],
        [B("💡 Plan advice", "adm:vps:p")],
        [B("🔄 Refresh", f"adm:vps:{active}" if active else "adm:vps"), B("🔙 Back", "adm:home")],
    ])


def _row(label, a):
    return [label, _pk(a["cpu_avg"], a["cpu_max"]), _pk(a["ram_avg"], a["ram_max"]),
            _bytes(a["net_out"]), _mbps(a["mbps"])]


HEAD = ["", "CPU%", "RAM%", "Out", "Mbps"]
WIDTHS = [7, 7, 7, 8, 6]
LEGEND = ("<i>CPU/RAM = average/peak %. Out = data sent by the bot. Mbps = highest speed seen.</i>")


def _need_data(update_text: str = ""):
    return ("🖥 <b>VPS Usage</b>\n\nNo data yet. The bot records a sample every 15 seconds and saves it every "
            "5 minutes, so check again in a few minutes." + update_text)


# ------------------------------------------------------------------ router
@action("vps")
@owner_only
async def a_vps(update, context, args):
    view = args[0] if args else ""
    if view == "d":
        await _daily(update)
    elif view == "w":
        await _weekly(update)
    elif view == "m":
        await _monthly(update)
    elif view == "p":
        await _advice(update)
    else:
        await _summary(update)


# ------------------------------------------------------------------ screens
async def _summary(update):
    if not U.supported():
        await show(update, "🖥 <b>VPS Usage</b>\n\n⚠️ Needs a Linux server (/proc not found).", _kb())
        return
    t = U.today()
    docs = await U.load_hours(t - dt.timedelta(days=29))
    if not docs:
        await show(update, _need_data(), _kb())
        return
    d_today = [d for d in docs if d["_id"][:10] == t.isoformat()]
    d7 = [d for d in docs if d["_id"][:10] >= (t - dt.timedelta(days=6)).isoformat()]
    rows = [HEAD, _row("Today", U.total(d_today)), _row("7 days", U.total(d7)), _row("30 days", U.total(docs))]

    now = U.last
    live = ""
    if now:
        live = (f"\n⚡ <b>Right now</b>: CPU {now['cpu']:.0f}% · RAM {now['ram']:.0f}% "
                f"({now['ram_used_mb'] / 1024:.1f}/{now['ram_total_mb'] / 1024:.1f} GB) · "
                f"{now['mbps']:.1f} Mbps · {now['streams']} streaming\n"
                f"🤖 Bot process: {now['bot_rss_mb']:.0f} MB RAM · 💽 Disk {now['disk_used_gb']:.1f}/{now['disk_total_gb']:.0f} GB\n")
    tot = U.total(docs)
    days = len({d["_id"][:10] for d in docs})
    await show(
        update,
        "🖥 <b>VPS Usage</b>\n<i>Days change at 12:00 AM IST</i>\n"
        + live + "\n" + _table(rows, WIDTHS) + LEGEND +
        f"\n\n📊 Average load over {days} day(s): <b>CPU {tot['cpu_avg']:.0f}%</b> · <b>RAM {tot['ram_avg']:.0f}%</b>\n"
        "👉 Tap <b>Plan advice</b> to see which Contabo plan fits this usage.",
        _kb(),
    )


async def _daily(update):
    t = U.today()
    days = [t - dt.timedelta(days=i) for i in range(7)]
    docs = await U.load_hours(days[-1])
    if not docs:
        await show(update, _need_data(), _kb("d"))
        return
    agg = U.rollup(docs, U.day_key)
    rows = [["Date"] + HEAD[1:]]
    for d in days:
        a = agg.get(d.isoformat())
        rows.append(_row(d.strftime("%d %b"), a) if a else [d.strftime("%d %b"), "-", "-", "-", "-"])
    peak_streams = max((a["streams"] for a in agg.values()), default=0)
    await show(update, "📅 <b>Last 7 days</b>\n\n" + _table(rows, WIDTHS) + LEGEND +
               f"\n👥 Most viewers streaming at once: <b>{peak_streams}</b>", _kb("d"))


async def _weekly(update):
    t = U.today()
    monday = t - dt.timedelta(days=t.weekday())
    weeks = [monday - dt.timedelta(days=7 * i) for i in range(8)]
    docs = await U.load_hours(weeks[-1])
    if not docs:
        await show(update, _need_data(), _kb("w"))
        return
    agg = U.rollup(docs, U.week_key)
    rows = [["Week of"] + HEAD[1:]]
    for w in weeks:
        a = agg.get(w.isoformat())
        rows.append(_row(w.strftime("%d %b"), a) if a else [w.strftime("%d %b"), "-", "-", "-", "-"])
    await show(update, "📆 <b>Last 8 weeks</b> <i>(Mon-Sun)</i>\n\n" + _table(rows, WIDTHS) + LEGEND, _kb("w"))


async def _monthly(update):
    t = U.today()
    months, y, m = [], t.year, t.month
    for _ in range(6):
        months.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    docs = await U.load_hours(dt.date.fromisoformat(months[-1] + "-01"))
    if not docs:
        await show(update, _need_data(), _kb("m"))
        return
    agg = U.rollup(docs, U.month_key)
    rows = [["Month"] + HEAD[1:]]
    for mo in months:
        a = agg.get(mo)
        label = dt.date.fromisoformat(mo + "-01").strftime("%b %y")
        rows.append(_row(label, a) if a else [label, "-", "-", "-", "-"])

    recent = [d for d in docs if d["_id"][:10] >= (t - dt.timedelta(days=29)).isoformat()]
    pat = U.patterns(recent)
    ho = pat["hour_out"]
    metric, name = (ho, "data sent") if max(ho) > 0 else (pat["hour_cpu"], "CPU")
    best = sorted(range(24), key=lambda h: metric[h], reverse=True)[:3]
    best_txt = ", ".join(f"{h:02d}:00-{(h + 1) % 24:02d}:00" for h in sorted(best)) if max(metric) > 0 else "-"
    wd = pat["weekday_out"]
    wd_best = DAYS[max(range(7), key=lambda i: wd[i])] if max(wd) > 0 else "-"

    text = ("🗓 <b>Last 6 months</b>\n\n" + _table(rows, WIDTHS) + LEGEND +
            f"\n\n⏰ <b>Daily pattern (IST, last 30 days, {name})</b>\n"
            f"<pre>00-11 {U.spark(metric[:12])}\n12-23 {U.spark(metric[12:])}</pre>"
            f"🔥 Busiest hours: <b>{best_txt}</b>\n\n"
            f"📆 <b>Weekday pattern</b>\n<pre>{'  '.join(d[0] for d in DAYS)}\n{'  '.join(U.spark(wd))}</pre>"
            f"Busiest day: <b>{wd_best}</b>")
    await show(update, text, _kb("m"))


async def _advice(update):
    t = U.today()
    docs = await U.load_hours(t - dt.timedelta(days=29))
    r = U.recommend(docs)
    if not r["ok"]:
        await show(update, _need_data(), _kb("p"))
        return
    pick, cur, u = r["pick"], r["current"], r["util"]
    if r["days"] >= 14:
        conf = "🟢 Good data"
    elif r["days"] >= 3:
        conf = f"🟡 Only {r['days']} day(s) of data - check again after 2 weeks for a reliable answer"
    else:
        conf = f"🔴 Very little data ({r['days']} day) - treat this as a rough guess"

    lines = [
        "💡 <b>Contabo plan advice</b>",
        f"<i>Based on your last {r['days']} day(s) of real usage</i> · {conf}\n",
        "<b>What your users really need</b> (peak + safety room)",
        f"• CPU: {r['need_cores']} vCPU  <i>(peak {r['cpu_peak']:.0f}%, normal peak {r['cpu_p95']:.0f}% of {r['cores']} cores)</i>",
        f"• RAM: {r['need_ram_gb']:.1f} GB  <i>(highest use {r['ram_peak_mb'] / 1024:.2f} GB)</i>",
        f"• Disk: {r['need_disk_gb']:.0f} GB  <i>(using {r['disk_now']:.1f} GB, +{max(r['growth_30d'], 0):.1f} GB / month)</i>",
        f"• Speed: {r['need_port']:.0f} Mbit/s  <i>(highest seen {r['mbps_peak']:.0f}, {r['streams_peak']} viewers at once)</i>",
        f"• Traffic: ≈ {r['out_tb_month']:.2f} TB / month out  <i>(Contabo fair-use ≈ {U.FAIR_USE_TB} TB)</i>\n",
    ]
    if r["undersized"]:
        lines.append(f"⚠️ Even the biggest plan in the list ({pick['name']}) is tight for this usage. "
                     "Consider splitting the load (e.g. move streaming to a second server).")
    else:
        lines.append(f"✅ <b>Recommended: {pick['name']}</b> - {pick['cores']} vCPU · {pick['ram_gb']} GB RAM · "
                     f"{pick['disk_gb']} GB · {pick['port']} Mbit/s · ≈ €{pick['eur']:.2f}/month")
        lines.append(f"It would run at about CPU {u['cpu']:.0f}% · RAM {u['ram']:.0f}% · Disk {u['disk']:.0f}% · "
                     f"Port {u['port']:.0f}% at your busiest moments.")
    if cur:
        if cur["eur"] > pick["eur"] and not r["undersized"]:
            lines.append(f"\n💸 Your VPS looks like <b>{cur['name']}</b> (≈ €{cur['eur']:.2f}). "
                         f"Switching to {pick['name']} saves ≈ <b>€{cur['eur'] - pick['eur']:.2f}/month</b> "
                         f"(€{(cur['eur'] - pick['eur']) * 12:.0f}/year).")
        elif cur["name"] == pick["name"]:
            lines.append(f"\n👍 Your VPS already looks like <b>{cur['name']}</b> - the right size.")
        else:
            lines.append(f"\n⚠️ Your VPS looks like <b>{cur['name']}</b>, smaller than recommended - upgrade when peaks grow.")
    else:
        lines.append(f"\nℹ️ Detected {r['cores']} vCPU / {r['ram_total_mb'] / 1024:.1f} GB RAM (not a plain Cloud VPS size). "
                     "Plus / VDS plans cost more for the same cores and RAM.")
    if r["out_tb_month"] > U.FAIR_USE_TB * 0.8:
        lines.append(f"\n⚠️ Traffic is close to the {U.FAIR_USE_TB} TB fair-use limit.")
    lines.append("\n<i>Prices are approximate (EUR, 1-month term) - confirm on contabo.com/en/pricing. "
                 "Edit the list in utils/usage.py or set CONTABO_PLANS_JSON in .env.</i>")
    await show(update, "\n".join(lines), _kb("p"))
