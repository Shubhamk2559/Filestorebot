"""utils/keyboards.py - the one button style of the panel. Callback data: "adm:<action>:<arg>...".

  ub()   CAPS label (emoji + 1-2 words)          nm()   a name that must keep its own case
  grid() two buttons per row                      BACK / CLOSE / CANCEL are always full width
"""
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M


def ub(text, data, raw=False):
    """Callback button. raw=True keeps the label as is (channel names, captions, titles)."""
    return B(text if raw else text.upper(), callback_data=data)


def nm(text, data):
    """Callback button that shows an owner-defined name exactly as written."""
    return ub(text, data, raw=True)


def link(text, url, raw=False):
    """URL button."""
    return B(text if raw else text.upper(), url=url)


def grid(items):
    """items = [(label, callback) | ready button] -> rows of 2 buttons."""
    btns = [i if isinstance(i, B) else ub(*i) for i in items]
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


def pager(page, pages, prefix):
    """PREV / NEXT row (empty when there is only one page)."""
    row = []
    if page > 0:
        row.append(ub("⬅️ Prev", f"{prefix}:{page - 1}"))
    if page < pages - 1:
        row.append(ub("Next ➡️", f"{prefix}:{page + 1}"))
    return row


def kb(rows, bottom=None):
    """Keyboard from grid rows + an optional full-width bottom button (back / cancel / close)."""
    rows = [r for r in rows if r]
    if bottom is not None:
        rows.append([bottom])
    return M(rows)


def cancel():
    return M([[ub("❌ Cancel", "adm:cancel")]])


def back(to: str = "home"):
    return M([[ub("🔙 Back", f"adm:{to}")]])


def confirm(yes: str, no: str, yes_label="✅ Yes", no_label="❌ No"):
    return M([[ub(yes_label, yes), ub(no_label, no)]])


def batch_kb():
    return M([[ub("✅ Done", "adm:batchdone")], [ub("❌ Cancel", "adm:cancel")]])


def after_link():
    return M([[ub("🔗 My Links", "adm:links"), ub("🔙 Panel", "adm:home")]])


def main_panel(owner: bool):
    items = [
        ("📦 Create Batch", "adm:batch"), ("🔗 My Links", "adm:links"),
        ("📅 Autoposter", "adm:ap"), ("👥 Users", "adm:users"),
        ("💰 Monetization", "adm:monet"),
    ]
    if owner:
        items += [("🖥️ Servers", "adm:servers"), ("💾 Backup", "adm:bk")]
    items += [("⚙️ Settings", "adm:settings"), ("📢 Broadcast", "adm:bc"), ("📊 Statistics", "adm:stats")]
    return kb(grid(items), ub("✖️ Close", "adm:close"))


def settings_panel(owner: bool = False):
    items = [
        ("📡 DB Channel", "adm:dbch"), ("🔔 Force-Sub", "adm:fsub"),
        ("🖼 Start Image", "adm:simg"), ("📝 Start Caption", "adm:scap"),
        ("🔘 Join Buttons", "adm:btns"), ("⏱ Auto-Delete", "adm:autodel"),
        ("🛡 Protect", "adm:protect"),
        ("🏷 File Caption", "adm:fcap"),
    ]
    if owner:
        items.append(("🛟 Support", "adm:sup"))
    items.append(("♻️ Reset", "adm:reset"))
    return kb(grid(items), ub("🔙 Back", "adm:home"))


def users_panel(owner: bool):
    items = [("🚫 Ban / Unban", "adm:ban")]
    if owner:
        items.append(("👮 Admins", "adm:admins"))
    return kb(grid(items), ub("🔙 Back", "adm:home"))


def monetization_panel(owner: bool):
    items = [("📺 Ads (Monetag)", "adm:ads")]
    if owner:
        items.append(("📈 Earnings", "adm:an"))
    return kb(grid(items), ub("🔙 Back", "adm:home"))


def servers_panel():
    return kb(grid([("🖥 VPS Usage", "adm:vps"), ("🔄 Transfer", "adm:tr")]), ub("🔙 Back", "adm:home"))
