"""utils/keyboards.py - reusable inline keyboards. Callback data: "adm:<action>:<arg>..."."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup as M


def B(text, callback_data=None, url=None):
    """PTB's 2nd positional arg is `url`, so always pass callback_data explicitly."""
    return InlineKeyboardButton(text, callback_data=callback_data, url=url)


def cancel():
    return M([[B("❌ Cancel", "adm:cancel")]])


def back(to: str = "home"):
    return M([[B("🔙 Back", f"adm:{to}")]])


def batch_kb():
    return M([[B("✅ Done", "adm:batchdone")], [B("❌ Cancel", "adm:cancel")]])


def after_link():
    return M([[B("🔗 My Links", "adm:links"), B("🔙 Panel", "adm:home")]])


def main_panel(owner: bool):
    rows = [
        [B("📦 Create Batch", "adm:batch"), B("🔗 My Links", "adm:links")],
        [B("📡 Database Channel", "adm:dbch"), B("🔔 Force-Sub", "adm:fsub")],
        [B("🖼 Start Image", "adm:simg"), B("📝 Start Caption", "adm:scap")],
        [B("🔘 Join Buttons", "adm:btns"), B("⏱ Auto-Delete", "adm:autodel")],
        [B("🛡 Protect Content", "adm:protect"), B("🏷 File Caption", "adm:fcap")],
        [B("💸 Shortener", "adm:short"), B("📢 Broadcast", "adm:bc")],
        [B("📊 Stats", "adm:stats"), B("🚫 Ban / Unban", "adm:ban")],
    ]
    last = [B("♻️ Reset Settings", "adm:reset")]
    if owner:
        last.insert(0, B("👮 Admins", "adm:admins"))
    rows.append(last)
    return M(rows)
