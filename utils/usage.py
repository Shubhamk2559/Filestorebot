"""
utils/usage.py
VPS usage tracker + Contabo plan advisor.

What it does
  * every SAMPLE_EVERY seconds it reads CPU / RAM / network from /proc (Linux, no extra packages)
  * numbers are summed into one MongoDB document per IST hour (collection vps_usage, kept 13 months)
  * web.py reports every streamed byte + concurrent viewers through stream_open/stream_close/stream_bytes_add
  * handlers/usage.py turns the hourly documents into Daily / Weekly / Monthly screens
  * recommend() picks the cheapest Contabo plan that fits the real usage (+ safety headroom)

Notes
  * CPU and RAM are VPS-wide (a Docker container sees the host's /proc/stat and /proc/meminfo).
  * Network counters are the bot container's own interface = the traffic the bot really sends
    (stream/download links, Telegram uploads, MongoDB). Caddy's TLS overhead is negligible.
  * Files delivered inside Telegram chats are sent by Telegram's servers, NOT by your VPS.
"""
import asyncio
import datetime as dt
import json
import logging
import math
import os
import shutil
import time

from database import db as dbm

log = logging.getLogger(__name__)

SAMPLE_EVERY = 15          # seconds between samples (short enough to catch bandwidth peaks)
FLUSH_EVERY = 300          # seconds between MongoDB writes (1 small write / 5 min)
KEEP_DAYS = 400

# Headroom used when sizing a plan: never plan to run hotter than this.
CPU_TARGET = 0.70          # keep CPU peaks under 70 %
RAM_HEADROOM = 1.40        # +40 % above the highest RAM use
DISK_HEADROOM = 1.30
PORT_HEADROOM = 1.30
FAIR_USE_TB = 32           # Contabo "unlimited" traffic is fair-use ~32 TB/month outgoing (EU plans)

# --------------------------------------------------------------------------- Contabo plan catalog
# Approximate monthly prices in EUR. Contabo changes names/prices from time to time:
# check https://contabo.com/en/pricing/ and either edit this list or set CONTABO_PLANS_JSON in .env, e.g.
#   CONTABO_PLANS_JSON=[{"name":"Cloud VPS 4","cores":4,"ram_gb":8,"disk_gb":100,"port":200,"eur":4.5}, ...]
DEFAULT_PLANS = [
    {"name": "Cloud VPS 4",  "cores": 4,  "ram_gb": 8,  "disk_gb": 100, "port": 200,  "eur": 4.50},
    {"name": "Cloud VPS 6",  "cores": 6,  "ram_gb": 12, "disk_gb": 200, "port": 300,  "eur": 7.50},
    {"name": "Cloud VPS 8",  "cores": 8,  "ram_gb": 24, "disk_gb": 300, "port": 600,  "eur": 14.00},
    {"name": "Cloud VPS 12", "cores": 12, "ram_gb": 48, "disk_gb": 400, "port": 800,  "eur": 25.00},
    {"name": "Cloud VPS 16", "cores": 16, "ram_gb": 64, "disk_gb": 500, "port": 1000, "eur": 37.00},
    {"name": "Cloud VPS 18", "cores": 18, "ram_gb": 96, "disk_gb": 600, "port": 1000, "eur": 49.00},
]


def plans() -> list:
    raw = os.getenv("CONTABO_PLANS_JSON", "").strip()
    if raw:
        try:
            lst = json.loads(raw)
            lst = [p for p in lst if all(k in p for k in ("name", "cores", "ram_gb", "disk_gb", "port", "eur"))]
            if lst:
                return sorted(lst, key=lambda p: p["eur"])
        except Exception:
            log.warning("CONTABO_PLANS_JSON is not valid JSON - using the built-in plan list")
    return sorted(DEFAULT_PLANS, key=lambda p: p["eur"])


# --------------------------------------------------------------------------- live counters (written by web.py)
class _Live:
    streams = 0            # viewers streaming/downloading right now
    streams_max = 0        # peak concurrent viewers since the last flush
    stream_bytes = 0       # bytes sent through /dl since the last flush


live = _Live()
last = {}                  # newest sample, shown as "Right now" in the panel


def stream_open():
    live.streams += 1
    live.streams_max = max(live.streams_max, live.streams)


def stream_close():
    live.streams = max(0, live.streams - 1)


def stream_bytes_add(n: int):
    live.stream_bytes += n


# --------------------------------------------------------------------------- /proc readers
def supported() -> bool:
    return os.path.exists("/proc/stat") and os.path.exists("/proc/meminfo")


def _read_cpu():
    with open("/proc/stat") as f:
        p = f.readline().split()[1:9]
    v = [int(x) for x in p]
    idle = v[3] + v[4]                      # idle + iowait
    return sum(v) - idle, sum(v)            # busy, total


def _read_mem():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            if k in ("MemTotal", "MemAvailable"):
                d[k] = int(rest.split()[0]) / 1024          # MB
            if len(d) == 2:
                break
    return d.get("MemTotal", 0), d.get("MemAvailable", 0)


def _read_net():
    rx = tx = 0
    with open("/proc/net/dev") as f:
        for line in list(f)[2:]:
            name, _, rest = line.partition(":")
            if name.strip() == "lo":
                continue
            c = rest.split()
            rx += int(c[0])
            tx += int(c[8])
    return rx, tx


def _read_rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 0.0


# --------------------------------------------------------------------------- sampler
def _hour_key() -> str:
    return dt.datetime.now(dbm.LIMIT_TZ).strftime("%Y-%m-%d-%H")


def _new_acc(key: str) -> dict:
    return {"key": key, "n": 0, "cpu_sum": 0.0, "ram_sum": 0.0, "net_in": 0, "net_out": 0, "stream_bytes": 0,
            "cpu_max": 0.0, "ram_max": 0.0, "ram_used_mb_max": 0.0, "bot_rss_mb_max": 0.0,
            "mbps_max": 0.0, "streams_max": 0, "load_max": 0.0, "static": {}}


async def _flush(acc: dict):
    if not acc["n"]:
        return
    doc = {
        "$inc": {"n": acc["n"], "cpu_sum": round(acc["cpu_sum"], 2), "ram_sum": round(acc["ram_sum"], 2),
                 "net_in": acc["net_in"], "net_out": acc["net_out"], "stream_bytes": acc["stream_bytes"]},
        "$max": {"cpu_max": round(acc["cpu_max"], 1), "ram_max": round(acc["ram_max"], 1),
                 "ram_used_mb_max": round(acc["ram_used_mb_max"], 1),
                 "bot_rss_mb_max": round(acc["bot_rss_mb_max"], 1), "mbps_max": round(acc["mbps_max"], 2),
                 "streams_max": acc["streams_max"], "load_max": round(acc["load_max"], 2)},
        "$set": {**acc["static"], "expire_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=KEEP_DAYS)},
    }
    try:
        await dbm.vps_col.update_one({"_id": acc["key"]}, doc, upsert=True)
    except Exception:
        log.exception("Could not save VPS usage")      # tracking must never break the bot


async def usage_loop():
    """Background task started from main.py."""
    if not supported():
        log.info("VPS usage tracking is off (needs Linux /proc).")
        return
    cores = os.cpu_count() or 1
    prev_cpu, prev_net, prev_t = _read_cpu(), _read_net(), time.time()
    acc = _new_acc(_hour_key())
    last_flush = time.time()
    try:
        while True:
            await asyncio.sleep(SAMPLE_EVERY)
            try:
                now = time.time()
                dt_s = max(now - prev_t, 1e-3)
                cpu, net = _read_cpu(), _read_net()
                d_busy, d_total = cpu[0] - prev_cpu[0], cpu[1] - prev_cpu[1]
                cpu_pct = max(0.0, min(100.0, 100.0 * d_busy / d_total)) if d_total > 0 else 0.0
                d_in, d_out = net[0] - prev_net[0], net[1] - prev_net[1]
                if d_in < 0 or d_out < 0:                      # counters reset (container restarted)
                    d_in = d_out = 0
                mbps = d_out * 8 / dt_s / 1e6
                prev_cpu, prev_net, prev_t = cpu, net, now

                total_mb, avail_mb = _read_mem()
                used_mb = max(total_mb - avail_mb, 0.0)
                ram_pct = 100.0 * used_mb / total_mb if total_mb else 0.0
                rss = _read_rss_mb()
                du = shutil.disk_usage("/")
                load1 = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
                sbytes, live.stream_bytes = live.stream_bytes, 0

                key = _hour_key()
                if key != acc["key"]:                          # a new IST hour started
                    await _flush(acc)
                    acc, last_flush = _new_acc(key), time.time()

                acc["n"] += 1
                acc["cpu_sum"] += cpu_pct
                acc["ram_sum"] += ram_pct
                acc["net_in"] += d_in
                acc["net_out"] += d_out
                acc["stream_bytes"] += sbytes
                acc["cpu_max"] = max(acc["cpu_max"], cpu_pct)
                acc["ram_max"] = max(acc["ram_max"], ram_pct)
                acc["ram_used_mb_max"] = max(acc["ram_used_mb_max"], used_mb)
                acc["bot_rss_mb_max"] = max(acc["bot_rss_mb_max"], rss)
                acc["mbps_max"] = max(acc["mbps_max"], mbps)
                acc["streams_max"] = max(acc["streams_max"], live.streams_max, live.streams)
                acc["load_max"] = max(acc["load_max"], load1)
                live.streams_max = live.streams
                acc["static"] = {"cores": cores, "ram_total_mb": round(total_mb), "disk_total_gb": round(du.total / 1e9, 1),
                                 "disk_used_gb": round(du.used / 1e9, 1)}
                last.update(cpu=cpu_pct, ram=ram_pct, ram_used_mb=used_mb, ram_total_mb=total_mb, mbps=mbps,
                            streams=live.streams, bot_rss_mb=rss, cores=cores, load1=load1,
                            disk_used_gb=du.used / 1e9, disk_total_gb=du.total / 1e9, at=now)

                if now - last_flush >= FLUSH_EVERY:
                    await _flush(acc)
                    acc = _new_acc(acc["key"])
                    last_flush = now
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("VPS usage sample failed")
    except asyncio.CancelledError:
        await _flush(acc)                                      # save what we have on shutdown
        raise


# --------------------------------------------------------------------------- reading + rollups
def today() -> dt.date:
    return dt.datetime.now(dbm.LIMIT_TZ).date()


async def load_hours(since: dt.date) -> list:
    """Hourly documents from `since` (IST date) until now, oldest first."""
    try:
        cur = dbm.vps_col.find({"_id": {"$gte": since.strftime("%Y-%m-%d") + "-00"}}).sort("_id", 1)
        return await cur.to_list(None)
    except Exception:
        log.exception("Could not read VPS usage")
        return []


def day_key(doc_id: str) -> str:
    return doc_id[:10]


def week_key(doc_id: str) -> str:
    d = dt.date.fromisoformat(doc_id[:10])
    return (d - dt.timedelta(days=d.weekday())).isoformat()       # Monday of that week


def month_key(doc_id: str) -> str:
    return doc_id[:7]


def rollup(docs: list, keyfn) -> dict:
    out = {}
    for d in docs:
        a = out.setdefault(keyfn(d["_id"]), {"n": 0, "cpu_sum": 0.0, "ram_sum": 0.0, "cpu_max": 0.0, "ram_max": 0.0,
                                             "ram_mb": 0.0, "net_out": 0, "net_in": 0, "stream": 0, "mbps": 0.0,
                                             "streams": 0, "disk_gb": 0.0, "hours": 0})
        a["n"] += d.get("n", 0)
        a["hours"] += 1
        a["cpu_sum"] += d.get("cpu_sum", 0)
        a["ram_sum"] += d.get("ram_sum", 0)
        a["net_out"] += d.get("net_out", 0)
        a["net_in"] += d.get("net_in", 0)
        a["stream"] += d.get("stream_bytes", 0)
        a["cpu_max"] = max(a["cpu_max"], d.get("cpu_max", 0))
        a["ram_max"] = max(a["ram_max"], d.get("ram_max", 0))
        a["ram_mb"] = max(a["ram_mb"], d.get("ram_used_mb_max", 0))
        a["mbps"] = max(a["mbps"], d.get("mbps_max", 0))
        a["streams"] = max(a["streams"], d.get("streams_max", 0))
        a["disk_gb"] = max(a["disk_gb"], d.get("disk_used_gb", 0))
    for a in out.values():
        a["cpu_avg"] = a["cpu_sum"] / a["n"] if a["n"] else 0.0
        a["ram_avg"] = a["ram_sum"] / a["n"] if a["n"] else 0.0
    return out


_EMPTY = {"n": 0, "cpu_avg": 0.0, "ram_avg": 0.0, "cpu_max": 0.0, "ram_max": 0.0, "ram_mb": 0.0, "net_out": 0,
          "net_in": 0, "stream": 0, "mbps": 0.0, "streams": 0, "disk_gb": 0.0, "hours": 0}


def total(docs: list) -> dict:
    """One combined rollup over all given hourly docs."""
    return rollup(docs, lambda _id: "all").get("all", dict(_EMPTY))


def percentile(vals: list, p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))
    return float(s[k])


SPARK = "▁▂▃▄▅▆▇█"


def spark(values: list) -> str:
    hi = max(values) if values else 0
    if hi <= 0:
        return SPARK[0] * len(values)
    return "".join(SPARK[min(7, int(v / hi * 7.999))] for v in values)


def patterns(docs: list) -> dict:
    """Hour-of-day and weekday patterns (IST) - average outgoing bytes and CPU per slot."""
    hours = [[0, 0.0, 0] for _ in range(24)]       # out bytes, cpu_sum, n samples
    days_seen = {h: set() for h in range(24)}
    wd = [[0, 0] for _ in range(7)]                # out bytes per weekday, days seen
    wd_days = [set() for _ in range(7)]
    for d in docs:
        h = int(d["_id"][11:13])
        date = d["_id"][:10]
        hours[h][0] += d.get("net_out", 0)
        hours[h][1] += d.get("cpu_sum", 0)
        hours[h][2] += d.get("n", 0)
        days_seen[h].add(date)
        w = dt.date.fromisoformat(date).weekday()
        wd[w][0] += d.get("net_out", 0)
        wd_days[w].add(date)
    by_hour = [hours[h][0] / max(len(days_seen[h]), 1) for h in range(24)]
    cpu_hour = [hours[h][1] / hours[h][2] if hours[h][2] else 0.0 for h in range(24)]
    by_wd = [wd[w][0] / max(len(wd_days[w]), 1) for w in range(7)]
    return {"hour_out": by_hour, "hour_cpu": cpu_hour, "weekday_out": by_wd}


# --------------------------------------------------------------------------- the plan advisor
def detect_current(cores: int, ram_total_mb: float):
    """Best guess which Contabo plan this VPS is (by vCPU + RAM)."""
    for p in plans():
        want = p["ram_gb"] * 1024
        if p["cores"] == cores and want and abs(want - ram_total_mb) / want < 0.14:
            return p
    return None


def recommend(docs: list) -> dict:
    """Cheapest plan that fits the observed usage (95th percentile of hourly peaks + headroom)."""
    if not docs:
        return {"ok": False}
    days = len({d["_id"][:10] for d in docs})
    cores = max((d.get("cores", 0) for d in docs), default=0) or (os.cpu_count() or 1)
    ram_total = max((d.get("ram_total_mb", 0) for d in docs), default=0)

    cpu_p95 = percentile([d.get("cpu_max", 0) for d in docs], 95)
    cpu_peak = max(d.get("cpu_max", 0) for d in docs)
    ram_peak_mb = max(d.get("ram_used_mb_max", 0) for d in docs)
    mbps_p95 = percentile([d.get("mbps_max", 0) for d in docs], 95)
    mbps_peak = max(d.get("mbps_max", 0) for d in docs)
    streams_peak = max(d.get("streams_max", 0) for d in docs)
    disk_used = [d.get("disk_used_gb", 0) for d in docs if d.get("disk_used_gb")]
    disk_now = disk_used[-1] if disk_used else 0.0
    growth_30d = ((disk_used[-1] - disk_used[0]) * 30 / max(days, 1)) if len(disk_used) > 1 else 0.0
    out_bytes = sum(d.get("net_out", 0) for d in docs)
    out_tb_month = out_bytes / 1e12 * 30 / max(days, 1)

    need_cores = max(1, math.ceil(cpu_p95 / 100 * cores / CPU_TARGET))
    need_ram_gb = max(2.0, ram_peak_mb * RAM_HEADROOM / 1024)
    need_disk_gb = max(10.0, (disk_now + max(growth_30d, 0) * 3) * DISK_HEADROOM)
    need_port = max(50.0, mbps_p95 * PORT_HEADROOM)

    fit, pick = [], None
    for p in plans():                                   # sorted cheapest first
        if p["cores"] >= need_cores and p["ram_gb"] >= need_ram_gb and p["disk_gb"] >= need_disk_gb \
                and p["port"] >= need_port:
            pick = p
            break
    undersized = pick is None
    if undersized:
        pick = plans()[-1]
    cur = detect_current(cores, ram_total)

    # how full the recommended plan would be
    util = {"cpu": cpu_p95 / 100 * cores / pick["cores"] * 100,
            "ram": ram_peak_mb / 1024 / pick["ram_gb"] * 100,
            "disk": disk_now / pick["disk_gb"] * 100,
            "port": mbps_peak / pick["port"] * 100}
    return {"ok": True, "days": days, "cores": cores, "ram_total_mb": ram_total, "cpu_p95": cpu_p95,
            "cpu_peak": cpu_peak, "ram_peak_mb": ram_peak_mb, "mbps_p95": mbps_p95, "mbps_peak": mbps_peak,
            "streams_peak": streams_peak, "disk_now": disk_now, "growth_30d": growth_30d,
            "out_tb_month": out_tb_month, "need_cores": need_cores, "need_ram_gb": need_ram_gb,
            "need_disk_gb": need_disk_gb, "need_port": need_port, "pick": pick, "undersized": undersized,
            "current": cur, "util": util}
