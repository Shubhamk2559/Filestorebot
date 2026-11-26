"""utils/ui.py - the text half of the panel design (buttons live in utils/keyboards.py).

Every screen:   <emoji> <b>BOLD CAPS TITLE</b>
                (blank line)
                MINIMAL CAPS DESCRIPTION.            or   LABEL ▸ <b>VALUE</b> lines

Names that belong to the owner (channels, captions, link and post titles) are never upper-cased.
"""
import html
import time


def esc(s) -> str:
    return html.escape(str(s))


def head(icon: str, title: str, desc: str = "", of: str = "") -> str:
    """Screen heading. `of` is a raw name (channel, link ...) shown after the title in its own case."""
    t = f"{icon} <b>{title.upper()}</b>" + (f" — {esc(of)}" if of else "")
    return f"{t}\n\n{desc}" if desc else t


def note(text: str) -> str:
    """Result line placed above a screen after an action ('✅ SAVED.')."""
    return f"{text}\n\n" if text else ""


def onoff(flag) -> str:
    return "ON" if flag else "OFF"


def bar(pct, n: int = 12) -> str:
    """█████░░░░░░░ - a 0-100 progress bar."""
    k = max(0, min(n, round(float(pct or 0) / 100 * n)))
    return "█" * k + "░" * (n - k)


def table(rows, widths) -> str:
    """Monospace table: first column left-aligned, the rest right-aligned."""
    out = [str(r[0]).ljust(widths[0]) + "".join(str(c).rjust(w) for c, w in zip(r[1:], widths[1:])) for r in rows]
    return "<pre>" + html.escape("\n".join(out)) + "</pre>"


def mb(b) -> str:
    return f"{b / 1048576:.2f} MB"


def ago(ts, never: str = "NEVER") -> str:
    if not ts:
        return never
    d = int(time.time() - ts)
    if d < 90:
        return f"{d}S AGO"
    if d < 5400:
        return f"{d // 60} MIN AGO"
    if d < 172800:
        return f"{d // 3600} H AGO"
    return f"{d // 86400} D AGO"
