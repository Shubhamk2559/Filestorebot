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


def main_panel(owner: bool):
    rows = [
        [B("📦 Create Batch", callback_data="adm:batch"), B("🔗 My Links", callback_data="adm:links")],
        [B("📡 Database Channel", callback_data="adm:dbch"), B("🔔 Force-Sub", callback_data="adm:fsub")],
        [B("🖼 Start Image", callback_data="adm:simg"), B("📝 Start Caption", callback_data="adm:scap")],
        [B("🔘 Join Buttons", callback_data="adm:btns"), B("⏱ Auto-Delete", callback_data="adm:autodel")],
        [B("🎬 Delivery Mode", callback_data="adm:mode"), B("🛡 Protect Content", callback_data="adm:protect")],
        [B("🏷 File Caption", callback_data="adm:fcap")],
        [B("📢 Broadcast", callback_data="adm:bc"), B("📊 Stats", callback_data="adm:stats")],
        [B("📺 Ads (Monetag)", callback_data="adm:ads"), B("🚫 Ban / Unban", callback_data="adm:ban")],
        [B("📅 Autoposter", callback_data="adm:ap")],
    ]
    last = [B("♻️ Reset Settings", callback_data="adm:reset")]
    if owner:
        last.insert(0, B("👮 Admins", callback_data="adm:admins"))
    rows.append(last)
    if owner:
        rows.append([B("📈 Earnings", callback_data="adm:an"), B("💾 Backup", callback_data="adm:bk")])
        rows.append([B("🔄 Transfer", callback_data="adm:tr"), B("🖥 VPS Usage", callback_data="adm:vps")])
    return M(rows)
