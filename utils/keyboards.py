"""utils/keyboards.py - reusable inline keyboards. Callback data: "adm:<action>:<arg>..."."""
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M


def cancel():
    return M([[B("❌ Cancel", callback_data="adm:cancel")]])


def back(to: str = "home"):
    return M([[B("🔙 Back", callback_data=f"adm:{to}")]])


def batch_kb():
    return M([[B("✅ Done", callback_data="adm:batchdone")], [B("❌ Cancel", callback_data="adm:cancel")]])


def after_link():
    return M([[B("🔗 My Links", callback_data="adm:links"), B("🔙 Panel", callback_data="adm:home")]])


def _b(text, data):
    return B(text.upper(), callback_data=data)


def _grid(items):
    """items = [(label, callback)] -> rows of 2 buttons."""
    return [[_b(*items[i]) for i in range(j, min(j + 2, len(items)))] for j in range(0, len(items), 2)]


def main_panel(owner: bool):
    items = [
        ("📦 Create Batch", "adm:batch"), ("🔗 My Links", "adm:links"),
        ("📅 Autoposter", "adm:ap"), ("👥 Users", "adm:users"),
        ("💰 Monetization", "adm:monet"),
    ]
    if owner:
        items += [("🖥️ Servers", "adm:servers"), ("💾 Backup", "adm:bk")]
    items += [("⚙️ Settings", "adm:settings"), ("📢 Broadcast", "adm:bc"), ("📊 Statistics", "adm:stats")]
    rows = _grid(items)
    rows.append([_b("✖️ Close", "adm:close")])
    return M(rows)


def settings_panel():
    rows = _grid([
        ("📡 Database Channel", "adm:dbch"), ("🔔 Force-Sub", "adm:fsub"),
        ("🖼 Start Image", "adm:simg"), ("📝 Start Caption", "adm:scap"),
        ("🔘 Join Buttons", "adm:btns"), ("⏱ Auto-Delete", "adm:autodel"),
        ("🎬 Delivery Mode", "adm:mode"), ("🛡 Protect Content", "adm:protect"),
        ("🏷 File Caption", "adm:fcap"), ("♻️ Reset Settings", "adm:reset"),
    ])
    rows.append([_b("🔙 Back", "adm:home")])
    return M(rows)


def users_panel(owner: bool):
    items = [("🚫 Ban / Unban", "adm:ban")]
    if owner:
        items.append(("👮 Admins", "adm:admins"))
    rows = _grid(items)
    rows.append([_b("🔙 Back", "adm:home")])
    return M(rows)


def monetization_panel(owner: bool):
    items = [("📺 Ads (Monetag)", "adm:ads")]
    if owner:
        items.append(("📈 Earnings", "adm:an"))
    rows = _grid(items)
    rows.append([_b("🔙 Back", "adm:home")])
    return M(rows)


def servers_panel():
    rows = _grid([("🖥 VPS Usage", "adm:vps"), ("🔄 Transfer", "adm:tr")])
    rows.append([_b("🔙 Back", "adm:home")])
    return M(rows)
