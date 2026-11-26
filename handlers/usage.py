"""
handlers/usage.py
VPS Usage screens of the admin panel (owner only).

  Panel -> 🖥 Servers -> VPS Usage
      live dashboard (bars) + Today / 7d / 30d
      📅 Daily   📆 Weekly   🗓 Monthly (+ busy hours)   💡 Plan Advice (cheapest Contabo plan that fits)

Numbers are collected by utils/usage.py (hourly documents in MongoDB, IST days).
"""
import datetime as dt
import logging

from handlers.admin import action, show
from utils import usage as U
from utils.helpers import owner_only
from utils.keyboards import grid, kb, ub
from utils.ui import bar as _bar, table as _table

log = logging.getLogger(__name__)
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ------------------------------------------------------------------ formatting
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
    return kb(grid([("📅 Daily", "adm:vps:d"), ("📆 Weekly", "adm:vps:w"),
                    ("🗓 Monthly", "adm:vps:m"), ("💡 Plan Advice", "adm:vps:p"),
                    ("🔄 Refresh", f"adm:vps:{active}" if active else "adm:vps")]), ub("🔙 Back", "adm:servers"))


def _row(label, a):
    return [label, _pk(a["cpu_avg"], a["cpu_max"]), _pk(a["ram_avg"], a["ram_max"]),
            _bytes(a["net_out"]), _mbps(a["mbps"])]


HEAD = ["", "CPU%", "RAM%", "OUT", "MBPS"]
WIDTHS = [8, 7, 7, 8, 6]
LEGEND = "<i>AVG/PEAK %</i>"


def _need_data():
    return "🖥 <b>VPS USAGE</b>\n\n📭 NO DATA YET. CHECK AGAIN IN A FEW MINUTES."


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
        await show(update, "🖥 <b>VPS USAGE</b>\n\n⚠️ NEEDS A LINUX SERVER.", _kb())
        return
    t = U.today()
    docs = await U.load_hours(t - dt.timedelta(days=29))
    if not docs:
        await show(update, _need_data(), _kb())
        return
    d_today = [d for d in docs if d["_id"][:10] == t.isoformat()]
    d7 = [d for d in docs if d["_id"][:10] >= (t - dt.timedelta(days=6)).isoformat()]
    rows = [HEAD, _row("TODAY", U.total(d_today)), _row("7 DAYS", U.total(d7)), _row("30 DAYS", U.total(docs))]

    live = ""
    now = U.last
    if now:
        disk_pct = now["disk_used_gb"] / now["disk_total_gb"] * 100 if now["disk_total_gb"] else 0
        live = ("<pre>"
                f"CPU  {_bar(now['cpu'])} {now['cpu']:>3.0f}%\n"
                f"RAM  {_bar(now['ram'])} {now['ram']:>3.0f}%\n"
                f"DISK {_bar(disk_pct)} {disk_pct:>3.0f}%"
                "</pre>"
                f"⚡ <b>{now['ram_used_mb'] / 1024:.1f}</b>/{now['ram_total_mb'] / 1024:.1f} GB RAM · "
                f"<b>{now['disk_used_gb']:.1f}</b>/{now['disk_total_gb']:.0f} GB DISK\n"
                f"🌐 <b>{now['mbps']:.1f}</b> MBPS · 👥 <b>{now['streams']}</b> STREAMING · "
                f"🤖 BOT <b>{now['bot_rss_mb']:.0f}</b> MB\n\n")
    tot = U.total(docs)
    days = len({d["_id"][:10] for d in docs})
    await show(
        update,
        "🖥 <b>VPS USAGE</b>\n\n" + live + _table(rows, WIDTHS) + LEGEND +
        f"\n\n📊 AVG LOAD ({days} D) ▸ CPU <b>{tot['cpu_avg']:.0f}%</b> · RAM <b>{tot['ram_avg']:.0f}%</b>",
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
    rows = [["DATE"] + HEAD[1:]]
    for d in days:
        a = agg.get(d.isoformat())
        rows.append(_row(d.strftime("%d %b"), a) if a else [d.strftime("%d %b"), "-", "-", "-", "-"])
    peak_streams = max((a["streams"] for a in agg.values()), default=0)
    await show(update, "📅 <b>LAST 7 DAYS</b>\n\n" + _table(rows, WIDTHS) + LEGEND +
               f"\n👥 PEAK STREAMING ▸ <b>{peak_streams}</b>", _kb("d"))


async def _weekly(update):
    t = U.today()
    monday = t - dt.timedelta(days=t.weekday())
    weeks = [monday - dt.timedelta(days=7 * i) for i in range(8)]
    docs = await U.load_hours(weeks[-1])
    if not docs:
        await show(update, _need_data(), _kb("w"))
        return
    agg = U.rollup(docs, U.week_key)
    rows = [["WEEK"] + HEAD[1:]]
    for w in weeks:
        a = agg.get(w.isoformat())
        rows.append(_row(w.strftime("%d %b"), a) if a else [w.strftime("%d %b"), "-", "-", "-", "-"])
    await show(update, "📆 <b>LAST 8 WEEKS</b>\n\n" + _table(rows, WIDTHS) + LEGEND, _kb("w"))


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
    rows = [["MONTH"] + HEAD[1:]]
    for mo in months:
        a = agg.get(mo)
        label = dt.date.fromisoformat(mo + "-01").strftime("%b %y")
        rows.append(_row(label, a) if a else [label, "-", "-", "-", "-"])

    recent = [d for d in docs if d["_id"][:10] >= (t - dt.timedelta(days=29)).isoformat()]
    pat = U.patterns(recent)
    ho = pat["hour_out"]
    metric, name = (ho, "DATA") if max(ho) > 0 else (pat["hour_cpu"], "CPU")
    best = sorted(range(24), key=lambda h: metric[h], reverse=True)[:3]
    best_txt = ", ".join(f"{h:02d}:00-{(h + 1) % 24:02d}:00" for h in sorted(best)) if max(metric) > 0 else "-"
    wd = pat["weekday_out"]
    wd_best = DAYS[max(range(7), key=lambda i: wd[i])] if max(wd) > 0 else "-"

    text = ("🗓 <b>LAST 6 MONTHS</b>\n\n" + _table(rows, WIDTHS) + LEGEND +
            f"\n\n⏰ <b>BUSY HOURS</b> <i>(IST · 30 D · {name})</i>\n"
            f"<pre>00-11 {U.spark(metric[:12])}\n12-23 {U.spark(metric[12:])}</pre>"
            f"🔥 PEAK ▸ <b>{best_txt}</b>\n\n"
            f"📆 <b>WEEKDAYS</b>\n<pre>{'  '.join(d[0] for d in DAYS)}\n{'  '.join(U.spark(wd))}</pre>"
            f"🏆 BUSIEST DAY ▸ <b>{wd_best.upper()}</b>")
    await show(update, text, _kb("m"))


async def _advice(update):
    t = U.today()
    docs = await U.load_hours(t - dt.timedelta(days=29))
    r = U.recommend(docs)
    if not r["ok"]:
        await show(update, _need_data(), _kb("p"))
        return
    pick, cur, u = r["pick"], r["current"], r["util"]
    cs, term = U.currency(), U.best_term()
    vat_txt = f"INCL. {U.vat_pct():g}% VAT" if U.vat_pct() else "EXCL. VAT"
    conf = ("🟢 GOOD DATA" if r["days"] >= 14 else
            f"🟡 {r['days']} D OF DATA" if r["days"] >= 3 else f"🔴 {r['days']} D OF DATA")

    lines = [
        f"💡 <b>PLAN ADVICE</b> ▸ {conf}\n",
        "<b>YOU NEED</b>",
        f"⚙️ CPU ▸ <b>{r['need_cores']}</b> vCPU · RAM ▸ <b>{r['need_ram_gb']:.1f}</b> GB",
        f"💽 DISK ▸ <b>{r['need_disk_gb']:.0f}</b> GB · NET ▸ <b>{r['need_port']:.0f}</b> Mbit/s",
        f"📦 TRAFFIC ▸ <b>{r['out_tb_month']:.2f}</b> TB / MONTH\n",
    ]
    if r["undersized"]:
        lines.append(f"⚠️ EVEN <b>{pick['name']}</b> IS TIGHT. SPLIT THE LOAD (E.G. A 2ND SERVER FOR STREAMING).")
    else:
        lines.append(f"✅ <b>BEST FIT ▸ {pick['name']}</b>")
        lines.append(f"{pick['cores']} vCPU · {pick['ram_gb']} GB · {pick['disk_gb']} GB · {pick['port']} Mbit/s")
        lines.append(f"💰 <b>{cs}{U.price(pick):.2f}</b> / MONTH ({term}-MONTH TERM, {vat_txt})")
        lines.append(f"📈 PEAK LOAD ▸ CPU {u['cpu']:.0f}% · RAM {u['ram']:.0f}% · DISK {u['disk']:.0f}%")
    if cur:
        if cur["eur"] > pick["eur"] and not r["undersized"]:
            diff = U.price(cur) - U.price(pick)
            lines.append(f"\n💸 YOU HAVE <b>{cur['name']}</b> ▸ SWITCH TO SAVE <b>{cs}{diff:.2f}</b> / MONTH ({cs}{diff * 12:.0f} / YEAR)")
        elif cur["name"] == pick["name"]:
            lines.append(f"\n👍 YOU HAVE <b>{cur['name']}</b> ▸ PERFECT SIZE")
        else:
            lines.append(f"\n⚠️ YOU HAVE <b>{cur['name']}</b> ▸ SMALLER THAN NEEDED, UPGRADE SOON")
    if r["alts"] and not r["undersized"]:
        base = U.price(pick) / (1 + U.vat_pct() / 100)          # Contabo, ex-VAT, so the comparison is fair
        lines.append(f"\n🏷 <b>ALTERNATIVES</b> <i>(EXCL. VAT · CONTABO {cs}{base:.2f})</i>")
        for a in r["alts"][:3]:
            tag = " ⚠️" if a.get("risky") else ""
            lines.append(f"• {a['provider']} {a['name']} · {a['cores']} vCPU · {a['ram_gb']:g} GB · <b>{cs}{a['usd']:.2f}</b>{tag}")
        if not [a for a in r["alts"] if not a.get("risky") and a["usd"] < base]:
            lines.append("✅ NO RELIABLE PROVIDER IS CHEAPER.")
    if r["out_tb_month"] > U.FAIR_USE_TB * 0.8:
        lines.append(f"\n⚠️ TRAFFIC IS NEAR THE {U.FAIR_USE_TB} TB FAIR-USE LIMIT.")
    await show(update, "\n".join(lines), _kb("p"))
