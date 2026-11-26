"""utils/ui.py - the text half of the panel design (buttons live in utils/keyboards.py).

Every screen:   <emoji> <b>BOLD CAPS TITLE</b>               (normal font, as before)
                (blank line)
                <blockquote>𝗠𝗜𝗡𝗜𝗠𝗔𝗟 𝗖𝗔𝗣𝗦 𝗗𝗘𝗦𝗖𝗥𝗜𝗣𝗧𝗜𝗢𝗡.</blockquote>   (quote + small bold font)

Names that belong to the owner (channels, captions, link and post titles) are never upper-cased.
"""
import html
import re
import string
import time

# ---------------------------------------------------------------- font
# CAPS -> 𝗔𝗕𝗖 (bold sans). Only used for the text UNDER the title, inside a quote.
_FONT = {c: chr(0x1D5D4 + i) for i, c in enumerate(string.ascii_uppercase)}
_TABLE = str.maketrans(_FONT)
_TAG = re.compile(r"(<[^>]*>)")
_TAGNAME = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)")
_KEEP = {"code", "pre", "a", "tg-spoiler"}        # never restyled: copyable / clickable parts
_NOCONV = set("@/_\\&#")                           # @usernames, links, ids stay untouched


def _word(tok: str) -> str:
    """Restyle one ALL-CAPS word; anything else (names, usernames, links, mixed case) is left as is."""
    if (not any(c.isupper() for c in tok) or any(c.islower() for c in tok)
            or any(c in _NOCONV for c in tok) or "://" in tok):
        return tok
    return tok.translate(_TABLE)


def small(text: str) -> str:
    """Restyle the ALL-CAPS words of an HTML text. <code>, <pre>, <a> and bold names with lowercase stay as written."""
    parts = _TAG.split(text)
    out, skip, pushed = [], 0, []
    for i, p in enumerate(parts):
        if i % 2:                                   # a tag
            m = _TAGNAME.match(p)
            if m:
                closing, name = bool(m.group(1)), m.group(2).lower()
                if name in _KEEP:
                    skip = max(0, skip + (-1 if closing else 1))
                elif name == "b":
                    if closing:
                        if pushed and pushed.pop():
                            skip = max(0, skip - 1)
                    else:
                        j = next((k for k in range(i + 1, len(parts)) if parts[k].lower() == "</b>"), len(parts))
                        inner = "".join(parts[k] for k in range(i + 1, j) if k % 2 == 0)
                        lower = any(c.islower() for c in inner)       # a name like "Pookie Hub" -> keep
                        pushed.append(lower)
                        if lower:
                            skip += 1
            out.append(p)
        elif skip:
            out.append(p)
        else:
            out.append("".join(_word(t) if t and not t.isspace() else t for t in re.split(r"(\s+)", p)))
    return "".join(out)


def _balanced(t: str) -> bool:
    return all(len(re.findall(rf"<{x}[ >]", t)) == t.count(f"</{x}>") for x in ("b", "i", "u", "s", "code", "a"))


def quote(body: str) -> str:
    """Body text -> restyled <blockquote> paragraphs. <pre> tables stay outside the quote (monospace)."""
    out = []
    for seg in re.split(r"(<pre>.*?</pre>)", body, flags=re.S):
        if seg.startswith("<pre>"):
            out.append(seg)
            continue
        paras, cur = [], ""
        for para in re.split(r"\n{2,}", seg.strip("\n")):
            cur = f"{cur}\n\n{para}" if cur else para
            if _balanced(cur):
                paras.append(cur)
                cur = ""
        if cur:
            paras.append(cur)
        out += [f"<blockquote>{small(p.strip(chr(10)))}</blockquote>" for p in paras if p.strip()]
    return "\n".join(out)


_TITLE = re.compile(r"^[^\s<]+ <b>.*?</b>")


def styled(text: str) -> str:
    """Whole screen text: keep '<emoji> <b>TITLE</b>' as is, put everything below it into the quote + font.
    Text that came from head() is already quoted - only what follows its last quote gets styled."""
    end = text.rfind("</blockquote>")
    if end != -1:
        end += len("</blockquote>")
        tail = text[end:].strip("\n")
        return text[:end] + ("\n" + quote(tail) if tail else "")
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if _TITLE.match(ln):
            body = "\n".join(lines[i + 1:]).strip("\n")
            return "\n".join(lines[:i + 1]) + ("\n\n" + quote(body) if body else "")
    return text


def esc(s) -> str:
    return html.escape(str(s))


def head(icon: str, title: str, desc: str = "", of: str = "") -> str:
    """Screen heading. `of` is a raw name (channel, link ...) shown after the title in its own case."""
    t = f"{icon} <b>{title.upper()}</b>" + (f" — {esc(of)}" if of else "")
    desc = quote(desc.strip("\n")) if desc and desc.strip() else ""
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
