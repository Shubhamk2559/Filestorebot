"""
handlers/autoposter.py
Autoposter screens of the admin panel (registered into handlers.admin's ACTIONS / STATES).

  Panel -> 📅 Autoposter -> channel list -> pick a channel:
      ⏰ TIMINGS      times of the day + posts per time
      📚 ALL POSTS    every post (✅ posted / ⏳ pending), open one -> Post now / Delete
      ➕ CREATE POST  range (first + last message) or send / forward files
      🎨 CAPTION      fixed caption, buttons, thumbnail, imgbb keys, spacing ...
      🎭 POST TYPE    🔥 SOLO DROP / 💎 DUO REEL / 👑 VAULT PACK
      🗂 QUEUE        order of the posts          🔀 RESHUFFLE   new random order
      🔁 MODE         loop / one pass             ⏸ / ▶️         turn the channel OFF / ON

Style: same as the Admin Control Panel - CAPS bold header, one short line, CAPS buttons in a 2-column grid.
Details (timings, counts, order) live only on the screen that owns them.
"""
import html
import logging
import re
import time

from telegram import InlineKeyboardMarkup as M

from database import db
from database.db import settings as S
from handlers.admin import PAGE, action, ask, ask_chat, resolve_channel, show, state
from handlers.upload import media_info
from utils import autoposter as AP
from utils import batch as BT
from utils import zipvault as ZV
from utils import keyboards as K
from utils.keyboards import nm, pager, ub as U
from utils.ui import ago, hl, styled
from utils.helpers import drop_channel_msgs
from utils.links import share_link
from utils.mtproto import mtproto

log = logging.getLogger(__name__)
MAX_RANGE = 10000

# the three post types (stored values: classic / pair / batch)
TYPES = {"classic": "🔥 SOLO DROP", "pair": "💎 DUO REEL", "batch": "👑 VAULT PACK"}
NEXT_TYPE = {"classic": "pair", "pair": "batch", "batch": "classic"}


def _type(ch: dict) -> str:
    return TYPES.get(ch.get("ptype") or "classic", TYPES["classic"])


def _gone(update):
    return show(update, "⚠️ CHANNEL NOT FOUND.", K.back("ap"))


async def _job(coro, bot, chat_id):
    try:
        await coro
    except Exception as e:  # never let a background job die silently
        log.exception("Autoposter job failed")
        try:
            await bot.send_message(chat_id, f"⚠️ CREATE POST FAILED: <code>{html.escape(str(e))}</code>")
        except Exception:
            pass


# ===================================================================== channel list
@action("ap")
async def a_ap(update, context, args):
    chans = await db.ap_list_channels()
    rows = [[nm(f"📺 {c['title'][:30]}", f"adm:apc:{c['_id']}")] for c in chans]   # channel names keep their own font
    rows.append([U("➕ Add Channel", "adm:apadd")])
    rows.append([U("🔙 Back", "adm:home")])
    await show(update,
               "📅 <b>AUTOPOSTER</b>\n\n"
               + ("SELECT A CHANNEL OR ADD A NEW ONE." if chans else "NO CHANNEL ADDED YET."),
               M(rows))


@action("apadd")
async def a_apadd(update, context, args):
    await ask_chat(update, context, "ap_add",
                   "➕ <b>ADD CHANNEL</b>\n\nCHOOSE WHERE THE BOT SHOULD POST.",
                   channel=True, group=True)


@state("ap_add")
async def st_ap_add(update, context):
    chat = await resolve_channel(update, context, need_post=True)
    if not chat:
        return
    await db.ap_add_channel(chat.id, chat.title or str(chat.id))
    context.user_data.pop("state", None)
    await update.effective_message.reply_text(
        f"✅ ADDED <b>{html.escape(chat.title or '')}</b>.",
        reply_markup=M([[U("📺 Open", f"adm:apc:{chat.id}"), U("🔙 Back", "adm:ap")]]))


# ===================================================================== one channel
async def show_channel(update, cid: int, note: str = ""):
    ch = await db.ap_get_channel(cid)
    if not ch:
        await _gone(update)
        return
    on = ch.get("enabled", True)
    ads_n, cost = db.chan_opts(ch)
    await show(update,
               f"{note}📺 <b>{html.escape(ch['title'])}</b>\n\n"
               f"📺 ADS BEFORE FILE ▸ <b>{ads_n}</b>{' (NO AD)' if ads_n == 0 else ''}\n"
               f"👤 USER LIMIT USED ▸ <b>{cost}</b>{' (FREE)' if cost == 0 else ''} / {db.limit_value()} PER DAY\n"
               "APPLIES LIVE TO ALL POSTS OF THIS CHANNEL.\n\nSELECT AN OPTION TO MANAGE THIS CHANNEL.",
               M([
                   [U("➖", f"adm:apchopt:{cid}:ads:-1"), U(f"📺 Ads: {ads_n}", f"adm:apchopt:{cid}:ads:0"),
                    U("➕", f"adm:apchopt:{cid}:ads:1")],
                   [U("➖", f"adm:apchopt:{cid}:cost:-1"), U(f"👤 Limit: {cost}", f"adm:apchopt:{cid}:cost:0"),
                    U("➕", f"adm:apchopt:{cid}:cost:1")],
                   [U("⏰ Timings", f"adm:aptm:{cid}"), U("📚 All Posts", f"adm:apall:{cid}:0")],
                   [U("➕ Create Post", f"adm:apnew:{cid}"), U("🎨 Caption", f"adm:apcb:{cid}")],
                   [U("🎭 Post Type", f"adm:aptype:{cid}")],
                   [U("🗂 Queue", f"adm:apq:{cid}"), U("🔀 Reshuffle", f"adm:apresh:{cid}")],
                   [U("🔁 Mode", f"adm:apmode:{cid}"), U("⏸ Turn OFF" if on else "▶️ Turn ON", f"adm:apon:{cid}")],
                   [U("🧹 Wipe", f"adm:apwipe:{cid}"), U("🗑 Remove", f"adm:aprm:{cid}")],
                   [U("🔙 Back", "adm:ap")],
               ]))


@action("apc")
async def a_apc(update, context, args):
    await show_channel(update, int(args[0]))


@action("apchopt")
async def a_apchopt(update, context, args):
    """apchopt:<cid>:<ads|cost>:<-1|0|1> - channel-wise Ads / Limit (0..10, applies live to every post; 0 = just refresh)."""
    cid, field, step = int(args[0]), args[1], int(args[2])
    ch = await db.ap_get_channel(cid)
    if not ch or field not in ("ads", "cost"):
        await _gone(update)
        return
    if step:
        key = "ads_n" if field == "ads" else "cost"
        cur = db.chan_opts(ch)[0 if field == "ads" else 1]
        await db.ap_set(cid, **{key: db._opt(cur + step)})
    await show_channel(update, cid)


@action("apon")
async def a_apon(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid)
    on = not (ch or {}).get("enabled", True)
    if ch:
        await db.ap_set(cid, enabled=on)
    await show_channel(update, cid, f"{'✅ AUTOPOSTER ON' if on else '⏸ AUTOPOSTER OFF'}.\n\n")


@action("aptype")
async def a_aptype(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid)
    if ch:
        await db.ap_set(cid, ptype=NEXT_TYPE.get(ch.get("ptype") or "classic", "classic"))
    await show_channel(update, cid, f"✅ POST TYPE: <b>{_type(await db.ap_get_channel(cid) or {})}</b>\n\n")


# ---------------------------------------------------------------------- queue
async def show_queue(update, cid: int, note: str = ""):
    ch = await db.ap_get_channel(cid)
    if not ch:
        await _gone(update)
        return
    total, posted = await db.ap_counts(cid)
    qs = ch.get("queue_state") or {}
    head = f"{note}🗂 <b>QUEUE</b> — {html.escape(ch['title'])}\n\n"
    if ch.get("ptype") == "batch":
        now = time.time()
        pend = await db.ap_posts_col.find({"chan": cid, "posted": False, "ptype": "batch"}).sort("not_before", 1).to_list(None)
        ready = [p for p in pend if float(p.get("not_before") or 0) <= now]
        wait = [p for p in pend if float(p.get("not_before") or 0) > now]
        todo, _ = await BT.counts(cid)
        thumb_wait = await ZV.waiting_count(cid)
        todo = max(0, todo - thumb_wait)
        gap, min_h = BT.settings_of(ch)
        lines = [f"• #{p['seq']} {html.escape(p['title'][:26])} — IN {max(1, int((p['not_before'] - now) // 86400))} D"
                 for p in wait[:8]]
        await show(update,
                   head
                   + f"✅ POSTED: <b>{posted}</b> · READY: <b>{len(ready)}</b> · WAITING: <b>{len(wait)}</b>\n"
                   f"⚙️ PREPARING: <b>{todo}</b> FOLDERS\n"
                   + (f"🖼 WAITING FOR THUMBNAIL: <b>{thumb_wait}</b> FOLDERS\n" if thumb_wait else "")
                   + f"📆 GAP: <b>{gap}</b> D · <b>{min_h:g}</b> H\n"
                   f"🕓 LAST POSTED: <b>{ago(qs.get('last_posted_at'))}</b>\n\n"
                   "<b>WAITING</b>\n" + ("\n".join(lines) if lines else "<i>NONE</i>"),
                   M([[U("🔄 Refresh", f"adm:apq:{cid}")], [U("🔙 Back", f"adm:apc:{cid}")]]))
        return
    nxt = AP.next_slot(ch.get("slots", []))
    n = int(nxt[1]["n"]) if nxt else 1
    up = await db.ap_upcoming(cid, 10)
    lines = [f"{'▶️' if i < n else '•'} #{p['seq']} {html.escape(p['title'][:30])}" for i, p in enumerate(up)]
    when = f"{nxt[0].strftime('%H:%M')} IST ×{nxt[1]['n']}" if nxt else "NO TIMING SET"
    await show(update,
               head
               + f"⏳ PENDING: <b>{total - posted}</b> · ✅ POSTED: <b>{posted}</b>\n"
               f"🔁 CYCLE: <b>{qs.get('cycle', 0)}</b> · {'LOOP' if ch.get('loop', True) else 'ONE PASS'}\n"
               f"🕘 NEXT: <b>{when}</b>\n"
               f"🕓 LAST POSTED: <b>{ago(qs.get('last_posted_at'))}</b>\n\n"
               "<b>UP NEXT</b>\n" + ("\n".join(lines) if lines else "<i>EMPTY</i>"),
               M([[U("🔀 Reshuffle", f"adm:apresh:{cid}"), U("🔄 Refresh", f"adm:apq:{cid}")],
                  [U("🔙 Back", f"adm:apc:{cid}")]]))


@action("apq")
async def a_apq(update, context, args):
    await show_queue(update, int(args[0]))


@action("apresh")
async def a_apresh(update, context, args):
    cid = int(args[0])
    await db.ap_rebuild_cycle(cid)
    await show_queue(update, cid, "🔀 QUEUE RESHUFFLED.\n\n")


@action("apmode")
async def a_apmode(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid)
    if ch:
        await db.ap_set(cid, loop=not ch.get("loop", True))
    loop = (await db.ap_get_channel(cid) or {}).get("loop", True)
    await show_channel(update, cid,
                       "🔁 <b>MODE: LOOP</b>\nRESHUFFLES AND STARTS AGAIN AFTER THE LAST POST.\n\n" if loop else
                       "➡️ <b>MODE: ONE PASS</b>\nTURNS OFF AFTER EVERY POST IS POSTED ONCE.\n\n")


@action("aprm")
async def a_aprm(update, context, args):
    cid = int(args[0])
    await show(update,
               "🗑 <b>REMOVE THIS CHANNEL?</b>\n\nSHARE LINKS AND FILES STAY SAFE.",
               M([[U("✅ Yes, Remove", f"adm:aprmyes:{cid}"), U("❌ No", f"adm:apc:{cid}")]]))


@action("aprmyes")
async def a_aprmyes(update, context, args):
    await db.ap_remove_channel(int(args[0]))
    await show(update, "🗑 CHANNEL REMOVED.", K.back("ap"))


# ===================================================================== caption
async def show_cb(update, cid: int, note: str = ""):
    ch = await db.ap_get_channel(cid)
    if not ch:
        await _gone(update)
        return
    await show(update,
               f"{note}🎨 <b>CAPTION</b> — {html.escape(ch['title'])}\n\nSELECT AN OPTION TO CUSTOMIZE YOUR POSTS.",
               M([[U("📝 Caption", f"adm:apcap:{cid}"), U("🔘 Button", f"adm:apbtn:{cid}")],
                  [U("🖼 Thumbnail", f"adm:apthumb:{cid}"), U("🎬 Duo Buttons", f"adm:appbtn:{cid}")],
                  [U("🌐 Imgbb Keys", f"adm:apimg:{cid}"), U("📰 TG Title", f"adm:aptgt:{cid}")],
                  [U("🔢 Post No", f"adm:appno:{cid}"), U("📆 Spacing", f"adm:apgap:{cid}")],
                  [U("♻️ Reset", f"adm:apcbr:{cid}")],
                  [U("🔙 Back", f"adm:apc:{cid}")]]))


@action("apcb")
async def a_apcb(update, context, args):
    await show_cb(update, int(args[0]))


@action("apcbr")
async def a_apcbr(update, context, args):
    cid = int(args[0])
    await db.ap_set(cid, cap_tpl="", btn="", btn_img="", btn_vid="", fthumb="")
    await show_cb(update, cid, "♻️ RESET DONE.\n\n")


async def _saved(msg, cid: int, text: str):
    await msg.reply_text(styled(text), reply_markup=K.back(f"apcb:{cid}"))


@action("apcap")
async def a_apcap(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid) or {}
    tpl = (ch.get("cap_tpl") or "").replace("{caption}", "").strip()
    context.user_data["ap_cid"] = cid
    await ask(update, context, "ap_cap",
              f"📝 <b>CAPTION</b>\n\n{tpl or '<i>NOT SET</i>'}\n\n"
              "SEND A NEW CAPTION TO REPLACE IT.\n"
              "TAGS: <code>{Preview Image}</code> <code>{Video Link}</code> <code>{Post Number}</code>\n"
              "SEND <code>-</code> TO REMOVE IT.")


@state("ap_cap")
async def st_ap_cap(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    if not msg.text or cid is None:
        await msg.reply_text("⚠️ SEND THE CAPTION AS TEXT.", reply_markup=K.cancel())
        return
    tpl = "" if msg.text.strip() == "-" else msg.text_html.strip()
    if AP._plain_len(tpl) > AP.CAPTION_MAX:
        await msg.reply_text("⚠️ TOO LONG (MAX 1024).", reply_markup=K.cancel())
        return
    await db.ap_set(cid, cap_tpl=tpl)
    context.user_data.pop("state", None)
    await _saved(msg, cid, "✅ CAPTION SAVED.")


@action("apbtn")
async def a_apbtn(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid) or {}
    context.user_data["ap_cid"] = cid
    await ask(update, context, "ap_btn",
              f"🔘 <b>BUTTON</b>\n\nNOW: <b>{html.escape(ch.get('btn') or AP.DEFAULT_BTN)}</b>\n\n"
              f"SEND NEW BUTTON TEXT (MAX {AP.BTN_MAX} CHARS).\nSEND <code>-</code> FOR DEFAULT.")


@state("ap_btn")
async def st_ap_btn(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    txt = (msg.text or "").strip()
    if not txt or cid is None or len(txt) > AP.BTN_MAX:
        await msg.reply_text(f"⚠️ SEND TEXT UP TO {AP.BTN_MAX} CHARS.", reply_markup=K.cancel())
        return
    await db.ap_set(cid, btn="" if txt == "-" else txt)
    context.user_data.pop("state", None)
    await _saved(msg, cid, "✅ BUTTON SAVED.")


@action("apthumb")
async def a_apthumb(update, context, args):
    cid = int(args[0])
    if not S.get("db_channel"):
        await show(update, "⚠️ SET THE DATABASE CHANNEL FIRST.", M([[U("📡 DB Channel", "adm:dbch")], [U("🔙 Back", f"adm:apc:{cid}")]]))
        return
    ch = await db.ap_get_channel(cid) or {}
    context.user_data["ap_cid"] = cid
    await ask(update, context, "ap_thumb",
              f"🖼 <b>THUMBNAIL</b>\n\nNOW: <b>{'SET ✅' if ch.get('fthumb') else 'NOT SET ⚠️'}</b>\n\n"
              f"SEND ONE PHOTO FOR {TYPES['pair']} POSTS.\nSEND <code>-</code> TO REMOVE IT.")


@state("ap_thumb")
async def st_ap_thumb(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    if cid is None:
        return
    if msg.text and msg.text.strip() == "-":
        await db.ap_set(cid, fthumb="")
        context.user_data.pop("state", None)
        await _saved(msg, cid, "✅ THUMBNAIL REMOVED.")
        return
    kind, name = media_info(msg)
    channel = S.get("db_channel")
    if kind != "photo" or not channel:
        await msg.reply_text("⚠️ SEND ONE PHOTO.", reply_markup=K.cancel())
        return
    it = AP.item_from_ptb(msg, kind, name)
    it["cap"], it["cap_html"] = "", ""                 # only the backup code is written under it
    token = db.new_token()
    try:
        await AP._store(context.bot, channel["id"], it, db.make_code(token, 0), update.effective_user.id)
    except Exception as e:
        await msg.reply_text(f"⚠️ COULD NOT SAVE: <code>{html.escape(str(e))}</code>", reply_markup=K.cancel())
        return
    await db.ap_set(cid, fthumb=token)
    context.user_data.pop("state", None)
    await _saved(msg, cid, "✅ THUMBNAIL SAVED.")


@action("appbtn")
async def a_appbtn(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid) or {}
    context.user_data["ap_cid"] = cid
    await ask(update, context, "ap_pbtn",
              f"🎬 <b>DUO BUTTONS</b>\n\nNOW: <b>{html.escape(ch.get('btn_img') or AP.DEFAULT_BTN_IMG)}</b> · "
              f"<b>{html.escape(ch.get('btn_vid') or AP.DEFAULT_BTN_VID)}</b>\n\n"
              f"SEND TWO LINES: IMAGE BUTTON, THEN VIDEO BUTTON (MAX {AP.BTN_MAX} CHARS).\nSEND <code>-</code> FOR DEFAULT.")


@state("ap_pbtn")
async def st_ap_pbtn(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    lines = [x.strip() for x in (msg.text or "").splitlines() if x.strip()]
    if cid is None or not lines:
        await msg.reply_text("⚠️ SEND TWO LINES (OR <code>-</code>).", reply_markup=K.cancel())
        return
    if lines == ["-"]:
        img, vid = "", ""
    elif len(lines) == 2 and all(len(x) <= AP.BTN_MAX for x in lines):
        img, vid = lines
    else:
        await msg.reply_text(f"⚠️ TWO LINES, MAX {AP.BTN_MAX} CHARS EACH.", reply_markup=K.cancel())
        return
    await db.ap_set(cid, btn_img=img, btn_vid=vid)
    context.user_data.pop("state", None)
    await _saved(msg, cid, "✅ BUTTONS SAVED.")


# ---------------------------------------------------------------------- imgbb keys
async def show_keys(update, context, cid: int, note: str = ""):
    ks = BT.keys()
    rows = [[nm(f"🔑 {BT.mask(k)} · {BT.key_state(k)}", "adm:noop"), U("❌ Remove", f"adm:apimgrm:{cid}:{i}")]
            for i, k in enumerate(ks)]
    rows.append([U("➕ Add Key", f"adm:apimgadd:{cid}")])
    rows.append([U("🔙 Back", f"adm:apcb:{cid}")])
    await show(update,
               f"{note}🌐 <b>IMGBB KEYS</b>\n\n"
               + (f"🌐 KEYS: <b>{len(ks)}</b> · USED IN ROTATION." if ks else "NO KEY YET. GET ONE AT <code>api.imgbb.com</code>."),
               M(rows))


@action("apimg")
async def a_apimg(update, context, args):
    await show_keys(update, context, int(args[0]))


@action("apimgadd")
async def a_apimgadd(update, context, args):
    context.user_data["ap_cid"] = int(args[0])
    await ask(update, context, "ap_imgkey",
              "🌐 <b>ADD IMGBB KEY</b>\n\nSEND THE KEY. " + hl("YOUR MESSAGE IS DELETED AFTER SAVING."))


@action("apimgrm")
async def a_apimgrm(update, context, args):
    await BT.remove_key(int(args[1]))
    await show_keys(update, context, int(args[0]), "🗑 KEY REMOVED.\n\n")


@state("ap_imgkey")
async def st_ap_imgkey(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    txt = (msg.text or "").strip()
    if not txt or cid is None or " " in txt or len(txt) > 100:
        await msg.reply_text("⚠️ SEND JUST THE API KEY.", reply_markup=K.cancel())
        return
    await BT.add_key(txt)
    context.user_data.pop("state", None)
    try:
        await msg.delete()                      # do not leave the key in the chat
    except Exception:
        pass
    await context.bot.send_message(msg.chat_id, f"✅ KEY SAVED ({len(BT.keys())} IN TOTAL).",
                                   reply_markup=K.back(f"apimg:{cid}"))


# ---------------------------------------------------------------------- telegraph / post no / spacing
@action("aptgt")
async def a_aptgt(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid) or {}
    context.user_data["ap_cid"] = cid
    await ask(update, context, "ap_tgtitle",
              f"📰 <b>TG TITLE</b> — {TYPES['batch']}\n\n"
              f"NOW: <code>{html.escape(ch.get('tg_title') or BT.DEFAULT_TG_TITLE)}</code>\n\n"
              "SEND A NEW TITLE. <code>{folder_name}</code> = FOLDER NAME.\n"
              "SEND <code>-</code> FOR DEFAULT.")


@state("ap_tgtitle")
async def st_ap_tgtitle(update, context):
    msg = update.effective_message
    cid, txt = context.user_data.get("ap_cid"), (msg.text or "").strip()
    if not txt or cid is None or len(txt) > 200:
        await msg.reply_text("⚠️ SEND THE TITLE AS TEXT (MAX 200 CHARS).", reply_markup=K.cancel())
        return
    await db.ap_set(cid, tg_title="" if txt == "-" else txt)
    context.user_data.pop("state", None)
    await _saved(msg, cid, "✅ TITLE SAVED.")


@action("appno")
async def a_appno(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid) or {}
    context.user_data["ap_cid"] = cid
    await ask(update, context, "ap_postno",
              f"🔢 <b>POST NUMBER</b>\n\nLAST NUMBER USED: <b>{ch.get('post_no', 0)}</b>\n"
              "SEND THE NUMBER YOUR CHANNEL IS AT NOW (<code>50</code> → NEXT POST IS 51).\n"
              "⚠️ DON'T SET IT LOWER THAN THE REAL LAST NUMBER.")


@state("ap_postno")
async def st_ap_postno(update, context):
    msg = update.effective_message
    cid, txt = context.user_data.get("ap_cid"), (msg.text or "").strip()
    if cid is None or not txt.isdigit() or int(txt) > 10_000_000:
        await msg.reply_text("⚠️ SEND A WHOLE NUMBER, E.G. <code>50</code>.", reply_markup=K.cancel())
        return
    await db.ap_set(cid, post_no=int(txt))
    context.user_data.pop("state", None)
    await _saved(msg, cid, f"✅ DONE. NEXT POST NUMBER: <b>{int(txt) + 1}</b>.")


@action("apgap")
async def a_apgap(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid) or {}
    gap, min_h = BT.settings_of(ch)
    context.user_data["ap_cid"] = cid
    await ask(update, context, "ap_gap",
              f"📆 <b>SPACING</b> ({TYPES['batch']})\n\n"
              f"NOW: <b>{gap}</b> DAYS (+0-{BT.GAP_JITTER_DAYS}) PER FOLDER · <b>{min_h:g}</b> HOURS BETWEEN POSTS.\n\n"
              "SEND TWO NUMBERS:\n<code>30</code> DAYS\n<code>3</code> HOURS")


@state("ap_gap")
async def st_ap_gap(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    try:
        a, b = [float(x) for x in (msg.text or "").split()[:2]]
        if cid is None or not (1 <= a <= 365) or not (0 <= b <= 240):
            raise ValueError
    except ValueError:
        await msg.reply_text("⚠️ TWO NUMBERS: DAYS (1-365) AND HOURS (0-240).", reply_markup=K.cancel())
        return
    await db.ap_set(cid, gap_days=int(a), min_hours=b)
    context.user_data.pop("state", None)
    await _saved(msg, cid, f"✅ SAVED: {int(a)} DAYS · {b:g} HOURS.")


# ===================================================================== timings
async def show_timings(update, cid: int, note: str = ""):
    ch = await db.ap_get_channel(cid)
    if not ch:
        await _gone(update)
        return
    rows = []
    for s in sorted(ch.get("slots", []), key=lambda s: s["t"]):
        k = s["t"].replace(":", "")       # ":" is the callback separator
        rows.append([U(f"🕘 {s['t']} · {s['n']} POST{'S' if s['n'] != 1 else ''}", "adm:noop"), U("❌ Remove", f"adm:apsd:{cid}:{k}")])
        rows.append([U("➖ Less", f"adm:apsn:{cid}:{k}:m"), U("➕ More", f"adm:apsn:{cid}:{k}:p")])
    rows.append([U("➕ Add Time", f"adm:apsadd:{cid}")])
    rows.append([U("🔙 Back", f"adm:apc:{cid}")])
    await show(update,
               f"{note}⏰ <b>TIMINGS</b> — {html.escape(ch['title'])}\n\nSET POST TIMES (IST).",
               M(rows))


@action("aptm")
async def a_aptm(update, context, args):
    await show_timings(update, int(args[0]))


def _hhmm(k: str) -> str:
    return f"{k[:2]}:{k[2:]}"


@action("apsn")
async def a_apsn(update, context, args):
    cid, t, d = int(args[0]), _hhmm(args[1]), args[2]
    ch = await db.ap_get_channel(cid)
    slots = list((ch or {}).get("slots", []))
    for s in slots:
        if s["t"] == t:
            s["n"] = max(1, min(AP.MAX_PER_SLOT, s["n"] + (1 if d == "p" else -1)))
    await db.ap_set(cid, slots=slots)
    await show_timings(update, cid)


@action("apsd")
async def a_apsd(update, context, args):
    cid, t = int(args[0]), _hhmm(args[1])
    ch = await db.ap_get_channel(cid)
    await db.ap_set(cid, slots=[s for s in (ch or {}).get("slots", []) if s["t"] != t])
    await show_timings(update, cid, "✅ TIME REMOVED.\n\n")


@action("apsadd")
async def a_apsadd(update, context, args):
    context.user_data["ap_cid"] = int(args[0])
    await ask(update, context, "ap_slot",
              "➕ <b>ADD TIME</b>\n\nSEND <code>HH:MM POSTS</code> (IST, 24H), ONE PER LINE:\n"
              "<code>09:00 2</code>\n<code>14:30 1</code>\n\n"
              f"POSTS IS OPTIONAL (DEFAULT 1, MAX {AP.MAX_PER_SLOT}).")


@state("ap_slot")
async def st_ap_slot(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    try:
        new = AP.parse_slots(msg.text or "")
    except ValueError as e:
        await msg.reply_text(f"⚠️ {html.escape(str(e))}\nEXAMPLE: <code>09:00 2</code>", reply_markup=K.cancel())
        return
    ch = await db.ap_get_channel(cid)
    if not ch:
        context.user_data.pop("state", None)
        return
    merged = {s["t"]: s["n"] for s in ch.get("slots", [])}
    merged.update({s["t"]: s["n"] for s in new})
    await db.ap_set(cid, slots=[{"t": t, "n": n} for t, n in sorted(merged.items())])
    context.user_data.pop("state", None)
    await msg.reply_text("✅ TIMINGS SAVED.", reply_markup=K.back(f"aptm:{cid}"))


# ===================================================================== all posts
@action("apall")
async def a_apall(update, context, args):
    cid, page = int(args[0]), (int(args[1]) if len(args) > 1 else 0)
    total, posted = await db.ap_counts(cid)
    posts = await db.ap_list_posts(cid, page * PAGE, PAGE)
    icon = {"pair": "💎", "batch": "👑"}
    rows = [[nm(f"{'✅' if p['posted'] else '⏳'} #{p['seq']} {p['title'][:24]} · {icon.get(p.get('ptype'), '🔥')}",
               f"adm:app:{p['_id']}")]
            for p in posts]
    rows.append(pager(page, -(-total // PAGE), f"adm:apall:{cid}"))
    rows.append([U("🔙 Back", f"adm:apc:{cid}")])
    await show(update,
               f"📚 <b>ALL POSTS ({total})</b>\n✅ {posted} · ⏳ {total - posted}\n\n"
               + ("TAP A POST TO OPEN IT." if total else "NO POSTS YET."),
               M(rows))


async def show_post(update, bot, token: str, note: str = ""):
    p = await db.ap_get_post(token)
    if not p:
        await show(update, "⚠️ POST NOT FOUND.", K.back("ap"))
        return
    if p.get("ptype") == "pair":
        links = (f"🖼 PREVIEW (FREE): <code>{share_link(bot.username, token)}</code>\n"
                 f"🎬 VIDEO (AD): <code>{share_link(bot.username, p['vtoken'])}</code>")
    elif p.get("ptype") == "batch":
        links = (f"🖼 PREVIEW: {p.get('tg_url') or '—'}\n"
                 + (f"🎬 VIDEOS: <code>{share_link(bot.username, p['vtoken'])}</code>\n" if p.get("vtoken") else "")
                 + f"📂 FOLDER: {html.escape(p.get('batch_name') or '')}"
                 + (f"\n🔢 POSTED AS #{p['posted_no']}" if p.get("posted_no") else "")
                 + (f"\n🕓 NEXT POSSIBLE: IN {max(1, int((p['not_before'] - time.time()) // 86400))} D"
                    if not p["posted"] and float(p.get("not_before") or 0) > time.time() else ""))
    else:
        links = f"🔗 <code>{share_link(bot.username, token)}</code>"
    ads_n, cost = db.chan_opts(await db.ap_get_channel(p["chan"]))
    opts = f"\n\n📺 Ads: {ads_n} · 👤 Limit: {cost} (channel setting)"
    await show(update,
               f"{note}📝 <b>#{p['seq']}</b> · {'✅ POSTED' if p['posted'] else '⏳ PENDING'} · "
               f"{TYPES.get(p.get('ptype') or 'classic', TYPES['classic'])}\n"
               f"📁 FILES: {p['n_files']}\n\n{html.escape(p['title'])}\n\n{links}{opts}",
               M([[U("📤 Post Now", f"adm:appnow:{token}")],
                  [U("🗑 Delete", f"adm:apdel:{token}:0"), U("🗑 Delete + Link", f"adm:apdel:{token}:1")],
                  [U("🔙 Back", f"adm:apall:{p['chan']}:0")]]))


@action("app")
async def a_app(update, context, args):
    await show_post(update, context.bot, args[0])


@action("appnow")
async def a_appnow(update, context, args):
    p = await db.ap_get_post(args[0])
    if not p:
        await show(update, "⚠️ POST NOT FOUND.", K.back("ap"))
        return
    err = await AP.send_post(context.bot, p["chan"], p)
    await show_post(update, context.bot, args[0], "✅ POSTED.\n\n" if not err else f"⚠️ <code>{html.escape(err)}</code>\n\n")


@action("apdel")
async def a_apdel(update, context, args):
    p = await db.ap_get_post(args[0])
    with_link = len(args) > 1 and args[1] == "1"
    ids = await db.purge_post(args[0], with_link)          # with_link: the post's links are erased from MongoDB too
    await drop_channel_msgs(context.bot, ids)              # thumbnail / file copies in the Database Channel
    await show(update,
               "🗑 <b>POST AND LINK DELETED</b>\n\n" + hl("REMOVED FROM THE DATABASE FOR GOOD.") if with_link
               else "🗑 POST DELETED. ITS LINK STILL WORKS.",
               K.back(f"apall:{p['chan']}:0" if p else "ap"))


@action("apwipe")
async def a_apwipe(update, context, args):
    cid = int(args[0])
    total, posted = await db.ap_counts(cid)
    await show(update,
               f"🧹 <b>WIPE POSTS</b>\n\n📚 {total} · ✅ {posted} · ⏳ {total - posted}\n\n"
               "POSTS ONLY: OLD LINKS KEEP WORKING.\n" + hl("POSTS + LINKS: LINKS ARE ERASED FROM THE DATABASE TOO."),
               M([[U("🧹 Wipe All", f"adm:apwipey:{cid}:a0"), U("🧹 Wipe All + Links", f"adm:apwipey:{cid}:a1")],
                  [U("✅ Wipe Posted Only", f"adm:apwipey:{cid}:p0")],
                  [U("🔙 Back", f"adm:apc:{cid}")]]))


@action("apwipey")
async def a_apwipey(update, context, args):
    cid, mode = int(args[0]), args[1]
    n, ids = await db.ap_wipe(cid, only_posted=mode[0] == "p", links=mode[1] == "1")
    await drop_channel_msgs(context.bot, ids)
    await show_channel(update, cid, f"🧹 {n} POST(S) DELETED.\n\n")


# ===================================================================== create post
@action("apnew")
async def a_apnew(update, context, args):
    cid = int(args[0])
    if not S.get("db_channel"):
        await show(update, "⚠️ SET THE DATABASE CHANNEL FIRST.", M([[U("📡 DB Channel", "adm:dbch")], [U("🔙 Back", f"adm:apc:{cid}")]]))
        return
    ch = await db.ap_get_channel(cid) or {}
    t = ch.get("ptype") or "classic"
    if t == "batch":
        waiting = await ZV.waiting_count(cid)
        busy = await ZV.preparing_count(cid)
        how = ("📦 ZIP — SEND ZIPS (FOLDERS WITH IMAGES + VIDEOS). IMAGES → imgbb (ORIGINAL QUALITY) → TELEGRAPH, "
               "VIDEOS → ONE LINK PER FOLDER. THEN YOU CHOOSE THE THUMBNAILS.\n"
               "🔢 RANGE — FIRST + LAST MESSAGE LINK OF A SOURCE CHANNEL (FOLDERS END WITH A TEXT WITH “UPLOADED”)."
               + (f"\n\n🖼 WAITING FOR THUMBNAIL: {waiting} FOLDER(S)" if waiting else "")
               + (f"\n⚙️ PREPARING LINKS: {busy} FOLDER(S)" if busy else "")
               + ("" if BT.enabled() else "\n\n⚠️ ADD AN IMGBB KEY FIRST (CAPTION → IMGBB KEYS)."))
        kb = M([[U("📦 ZIP Upload", f"adm:zvnew:{cid}"), U("🔢 Range", f"adm:aprange:{cid}")],
                [U(f"🖼 Thumbnails ({waiting})", f"adm:zvthumb:{cid}")],
                [U("🔙 Back", f"adm:apc:{cid}")]])
    else:
        if t == "pair":
            how = ("SEND IMAGE, VIDEO, IMAGE, VIDEO… IN ORDER.\nEACH PAIR = 1 POST."
                   + ("" if ch.get("fthumb") else "\n\n⚠️ SET THE FIXED THUMBNAIL FIRST."))
        else:
            how = "CAPTIONED FILE = THUMBNAIL.\nFILES BELOW IT = POST FILES."
        kb = M([[U("🔢 Range", f"adm:aprange:{cid}"), U("📥 Send Files", f"adm:apcollect:{cid}")],
                [U("🔙 Back", f"adm:apc:{cid}")]])
    await show(update, f"➕ <b>CREATE POST</b> — {_type(ch)}\n\n{how}", kb)


# ---- (a) range mode: the bot reads the source channel by message id
def parse_post_ref(msg):
    """(chat, message_id) from a forwarded channel post or a t.me link, else None."""
    origin = msg.forward_origin
    if origin is not None:
        chat, mid = getattr(origin, "chat", None), getattr(origin, "message_id", None)
        return (chat.id, mid) if chat is not None and mid else None
    if msg.text:
        m = re.search(r"t\.me/c/(\d+)/(?:\d+/)?(\d+)", msg.text)
        if m:
            return int("-100" + m.group(1)), int(m.group(2))
        m = re.search(r"t\.me/([A-Za-z]\w{3,})/(\d+)", msg.text)
        if m:
            return "@" + m.group(1), int(m.group(2))
    return None


@action("aprange")
async def a_aprange(update, context, args):
    cid = int(args[0])
    if not mtproto.ready:
        await show(update,
                   "⚠️ RANGE NEEDS THE MTPROTO SESSION (API_ID, API_HASH).\n"
                   f"STATUS: <code>{html.escape(mtproto.error or 'not running')}</code>\n\n"
                   "USE <b>SEND FILES</b> INSTEAD.",
                   K.back(f"apnew:{cid}"))
        return
    context.user_data["ap_cid"] = cid
    await ask(update, context, "ap_first",
              "🔢 <b>RANGE</b>\n\nTHE BOT MUST BE A MEMBER / ADMIN OF THE SOURCE CHANNEL.\n\n"
              "STEP 1/2: FORWARD THE <b>FIRST</b> MESSAGE (OR SEND ITS LINK).")


@state("ap_first")
async def st_ap_first(update, context):
    msg = update.effective_message
    ref = parse_post_ref(msg)
    if not ref:
        await msg.reply_text("⚠️ FORWARD A POST OR SEND A LINK (<code>t.me/c/123/45</code>).", reply_markup=K.cancel())
        return
    context.user_data["ap_src"] = ref
    context.user_data["state"] = "ap_last"
    await msg.reply_text("STEP 2/2: FORWARD THE <b>LAST</b> MESSAGE (OR SEND ITS LINK).", reply_markup=K.cancel())


@state("ap_last")
async def st_ap_last(update, context):
    msg = update.effective_message
    ref, first = parse_post_ref(msg), context.user_data.get("ap_src")
    if not ref or not first:
        await msg.reply_text("⚠️ FORWARD THE LAST POST OR SEND ITS LINK.", reply_markup=K.cancel())
        return
    same = type(ref[0]) is not type(first[0]) or str(ref[0]).lower() == str(first[0]).lower()
    if not same or ref[1] < first[1] or ref[1] - first[1] + 1 > MAX_RANGE:
        await msg.reply_text(f"⚠️ SAME CHANNEL, AFTER THE FIRST POST (MAX {MAX_RANGE} MESSAGES).", reply_markup=K.cancel())
        return
    cid = context.user_data.get("ap_cid")
    src, a, b = first[0], first[1], ref[1]
    for k in ("state", "ap_src", "ap_cid"):
        context.user_data.pop(k, None)
    status = await msg.reply_text("⏳ READING THE SOURCE CHANNEL…")
    if ((await db.ap_get_channel(cid)) or {}).get("ptype") == "batch":
        if not BT.enabled():
            await status.edit_text("⚠️ ADD AN IMGBB KEY FIRST (CAPTION → IMGBB KEYS).", reply_markup=K.back(f"apc:{cid}"))
            return
        job = _run_range_batch(context.bot, src, a, b, cid, update.effective_user.id, status)
    else:
        job = _run_range(context.bot, src, a, b, cid, update.effective_user.id, status)
    AP.spawn(_job(job, context.bot, msg.chat_id))


async def _read_range(bot, status, src, first: int, last: int, convert) -> list:
    """Read messages first..last of the source channel (200 at a time) and keep what `convert` accepts."""
    client = mtproto.client
    ids = list(range(first, last + 1))
    out = []
    for i in range(0, len(ids), 200):
        for m in await client.get_messages(src, ids[i:i + 200]):
            it = convert(m)
            if it:
                out.append(it)
        try:
            await bot.edit_message_text(f"⏳ READING THE SOURCE CHANNEL… {min(i + 200, len(ids))}/{len(ids)}",
                                        chat_id=status.chat_id, message_id=status.message_id)
        except Exception:
            pass
    return out


async def _run_range(bot, src, first: int, last: int, cid: int, by: int, status):
    items = await _read_range(bot, status, src, first, last, AP.item_from_pyro)
    if not items:
        await bot.edit_message_text("⚠️ NO FILES FOUND IN THAT RANGE. IS THE BOT A MEMBER OF THE SOURCE CHANNEL?",
                                    chat_id=status.chat_id, message_id=status.message_id,
                                    reply_markup=K.back(f"apc:{cid}"))
        return
    await AP.create_posts(bot, items, cid, by, status.chat_id, status.message_id)


async def _run_range_batch(bot, src, first: int, last: int, cid: int, by: int, status):
    """Vault Pack: read the range, find the folders ("... Uploaded ✅" texts) and queue them as resumable jobs."""
    entries = await _read_range(bot, status, src, first, last, BT.item_or_marker)
    folders, left = BT.scan_folders(entries)
    plans = [BT.plan_folder(f["items"]) for f in folders]
    usable = [p for p in plans if p[0]]
    n_posts = sum(max(1, len(p[2])) for p in usable)
    plain = sum(1 for p in usable if not p[2])
    fixed_ok = bool((await db.ap_get_channel(cid) or {}).get("fthumb"))
    queued = await BT.queue_folders(cid, folders, by)
    no_photo = len(folders) - queued
    if not queued:
        text = ("⚠️ NO FOLDER FOUND. A FOLDER NEEDS A PHOTO AND A TEXT WITH “UPLOADED” BELOW ITS FILES."
                if not folders else "⚠️ THE FOLDERS HAD NO PHOTOS. NOTHING TO PREPARE.")
    else:
        text = (f"✅ <b>{queued}</b> FOLDER(S) QUEUED → <b>{n_posts}</b> POST(S).\n"
                "THEY APPEAR IN 📚 ALL POSTS AS THE BOT PREPARES THEM.")
    if plain:
        text += (f"\n🖼 {plain} FOLDER(S) USE THE FIXED THUMBNAIL"
                 + ("." if fixed_ok else " ⚠️ NOT SET YET (CAPTION → THUMBNAIL)."))
    if no_photo and queued:
        text += f"\n⏭ SKIPPED (NO PHOTO): {no_photo}"
    if left:
        text += f"\n⏭ IGNORED (AFTER THE LAST “UPLOADED”): {left}"
    await bot.edit_message_text(text, chat_id=status.chat_id, message_id=status.message_id,
                                reply_markup=K.back(f"apc:{cid}"))


# ---- (b) collect mode: the admin sends / forwards the files to the bot
def _collect_text(n: int) -> str:
    return styled(f"📥 <b>COLLECTING FILES</b>\n\n{n} FILE(S) RECEIVED.\nSEND THEM IN ORDER, THEN TAP DONE.")


def _collect_kb():
    return M([[U("✅ Done", "adm:apdone"), U("❌ Cancel", "adm:cancel")]])


@action("apcollect")
async def a_apcollect(update, context, args):
    q = update.callback_query
    context.user_data.update(state="ap_collect", ap_cid=int(args[0]), ap_items=[],
                             ap_status=(q.message.chat_id, q.message.message_id), ap_ts=0.0)
    await show(update, _collect_text(0), _collect_kb())


@state("ap_collect")
async def st_ap_collect(update, context):
    msg = update.effective_message
    kind, name = media_info(msg)
    if not kind:
        await msg.reply_text("📎 SEND FILES ONLY, OR TAP <b>DONE</b>.", reply_markup=_collect_kb())
        return
    items = context.user_data.setdefault("ap_items", [])
    items.append(AP.item_from_ptb(msg, kind, name))
    if time.time() - context.user_data.get("ap_ts", 0) > 2.5 and context.user_data.get("ap_status"):
        context.user_data["ap_ts"] = time.time()
        chat_id, mid = context.user_data["ap_status"]
        try:
            await context.bot.edit_message_text(_collect_text(len(items)), chat_id=chat_id, message_id=mid,
                                                reply_markup=_collect_kb())
        except Exception:
            pass


@action("apdone")
async def a_apdone(update, context, args):
    ud = context.user_data
    items, cid = ud.get("ap_items") or [], ud.get("ap_cid")
    if not items or cid is None:
        if cid is not None:
            ud["state"] = "ap_collect"       # the router cleared it; keep collecting
        await show(update, "⚠️ SEND AT LEAST ONE FILE FIRST.\n\n" + _collect_text(0), _collect_kb())
        return
    for k in ("state", "ap_items", "ap_cid", "ap_status", "ap_ts"):
        ud.pop(k, None)
    status = await update.effective_chat.send_message(f"⏳ STARTING… {len(items)} FILE(S)")
    AP.spawn(_job(AP.create_posts(context.bot, items, cid, update.effective_user.id,
                                  status.chat_id, status.message_id),
                  context.bot, status.chat_id))
