"""
handlers/autoposter.py
Autoposter screens of the admin panel (registered into handlers.admin's ACTIONS / STATES).

  Panel -> 📅 Autoposter -> channel list -> pick a channel:
      ⏰ Timings        several times a day, posts per time (each time has its own count)
      📚 All Posts      every post of this channel (✅ posted / ⏳ pending), open one -> Post now / Delete
      ➕ Create Post    (a) range: first + last message of a source channel  (b) send / forward files, then Done
      🔄 Post Type      Classic (caption file = thumbnail + files below) / Image + Video (image 1, video 1, image 2 ...)
      🗂 Queue          shuffled order of the posts (cycle, cursor, up next)
      🔀 Reshuffle      new random order, all posts pending again
      🔁 Mode           loop (reshuffle every cycle) / one pass (stop after every post is posted once)
      ▶️ ON / OFF, 🗑 remove channel
"""
import html
import logging
import re
import time

from telegram import InlineKeyboardMarkup as M

from database import db
from database.db import settings as S
from handlers.admin import B, PAGE, action, ask, ask_chat, resolve_channel, show, state
from handlers.upload import media_info
from utils import autoposter as AP
from utils import batch as BT
from utils import keyboards as K
from utils.links import share_link
from utils.streamer import streamer

log = logging.getLogger(__name__)
MAX_RANGE = 10000


async def _job(coro, bot, chat_id):
    try:
        await coro
    except Exception as e:  # never let a background job die silently
        log.exception("Autoposter job failed")
        try:
            await bot.send_message(chat_id, f"⚠️ Create Post failed: <code>{html.escape(str(e))}</code>")
        except Exception:
            pass


# ===================================================================== channel list
@action("ap")
async def a_ap(update, context, args):
    chans = await db.ap_list_channels()
    rows = [[B(f"📺 {c['title'][:30]}", f"adm:apc:{c['_id']}")] for c in chans]
    rows.append([B("➕ Add Channel / Group", "adm:apadd")])
    rows.append([B("🔙 Back", "adm:home")])
    await show(update,
               "📅 <b>Autoposter</b>\n\nAdd a channel or group, set the timings and the bot posts "
               "your saved posts there automatically.\n\n"
               + (f"{len(chans)} channel(s) added." if chans else "No channel added yet."),
               M(rows))


@action("apadd")
async def a_apadd(update, context, args):
    await ask_chat(update, context, "ap_add",
                   "➕ <b>Add Channel / Group</b>\n\nChoose where the bot should post.\n"
                   "If the bot is not an admin there, Telegram will offer to add it.",
                   channel=True, group=True)


@state("ap_add")
async def st_ap_add(update, context):
    chat = await resolve_channel(update, context, need_post=True)
    if not chat:
        return
    await db.ap_add_channel(chat.id, chat.title or str(chat.id))
    context.user_data.pop("state", None)
    await update.effective_message.reply_text(
        f"✅ Added <b>{html.escape(chat.title or '')}</b>.",
        reply_markup=M([[B("📺 Open", f"adm:apc:{chat.id}")], [B("🔙 Back", "adm:ap")]]))


# ===================================================================== one channel
def _type_name(ch: dict) -> str:
    return {"pair": "🖼+🎬 Image + Video", "batch": "📂 Folder Batch"}.get(ch.get("ptype"), "🎞 Classic (thumbnail + files)")


def _type_short(ch: dict) -> str:
    return {"pair": "🖼+🎬 Image + Video", "batch": "📂 Folder Batch"}.get(ch.get("ptype"), "🎞 Classic")


async def show_channel(update, cid: int, note: str = ""):
    ch = await db.ap_get_channel(cid)
    if not ch:
        await show(update, "⚠️ Channel not found.", K.back("ap"))
        return
    total, posted = await db.ap_counts(cid)
    on = ch.get("enabled", True)
    loop = ch.get("loop", True)
    batch_line = ""
    if ch.get("ptype") == "batch":
        todo, done = await BT.counts(cid)
        batch_line = (f"\n📂 Folders: <b>{done}</b> ready" + (f" · <b>{todo}</b> preparing" if todo else "")
                      + f"\n🔢 Last post no.: <b>{ch.get('post_no', 0)}</b>")
    text = (f"{note}📺 <b>{html.escape(ch['title'])}</b>\n\n"
            f"▫️ Status: <b>{'ON ✅' if on else 'OFF ⏸'}</b>\n"
            f"▫️ Timings (IST): <b>{AP.fmt_slots(ch.get('slots', []))}</b>\n"
            f"▫️ Type: <b>{_type_name(ch)}</b>\n"
            f"▫️ Mode: <b>{'🔁 loop' if loop else '➡️ one pass'}</b> · cycle <b>{(ch.get('queue_state') or {}).get('cycle', 0)}</b>\n"
            f"▫️ Posts: <b>{total}</b> total · <b>{posted}</b> posted · <b>{total - posted}</b> pending{batch_line}")
    await show(update, text, M([
        [B("⏰ Timings", f"adm:aptm:{cid}"), B(f"📚 All Posts ({total})", f"adm:apall:{cid}:0")],
        [B("➕ Create Post", f"adm:apnew:{cid}"), B("🎨 Caption & Button", f"adm:apcb:{cid}")],
        [B(f"🔄 Post Type: {_type_short(ch)}", f"adm:aptype:{cid}")],
        [B("🗂 Queue", f"adm:apq:{cid}"), B("🔀 Reshuffle", f"adm:apresh:{cid}")],
        [B(f"🔁 Mode: {'loop' if loop else 'one pass'}", f"adm:apmode:{cid}"),
         B("⏸ Turn OFF" if on else "▶️ Turn ON", f"adm:apon:{cid}")],
        [B("🧹 Wipe posts", f"adm:apwipe:{cid}")],
        [B("🗑 Remove channel", f"adm:aprm:{cid}")],
        [B("🔙 Back", "adm:ap")],
    ]))


@action("apc")
async def a_apc(update, context, args):
    await show_channel(update, int(args[0]))


@action("apon")
async def a_apon(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid)
    if ch:
        await db.ap_set(cid, enabled=not ch.get("enabled", True))
    await show_channel(update, cid, "✅ Updated.\n\n")


@action("aptype")
async def a_aptype(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid)
    if ch:
        nxt = {"classic": "pair", "pair": "batch"}.get(ch.get("ptype") or "classic", "classic")
        await db.ap_set(cid, ptype=nxt)
    ch = await db.ap_get_channel(cid) or {}
    t = ch.get("ptype")
    await show_channel(update, cid,
                       ("🖼+🎬 <b>Image + Video</b>\n"
                        "Files in order: image, video, image, video…\n"
                        "Each pair = 1 post · image = free preview · video = after ad.\n\n"
                        if t == "pair" else
                        "📂 <b>Folder Batch</b>\n"
                        "Text with “Uploaded” closes a folder · files above it = 1 folder.\n"
                        "Photos → Telegraph link · other files → file link.\n\n"
                        if t == "batch" else
                        "🎞 <b>Classic</b>\n"
                        "Captioned file = thumbnail · captionless files below = post files.\n\n"))


async def show_queue(update, cid: int, note: str = ""):
    ch = await db.ap_get_channel(cid)
    if not ch:
        await show(update, "⚠️ Channel not found.", K.back("ap"))
        return
    total, posted = await db.ap_counts(cid)
    if ch.get("ptype") == "batch":
        now = time.time()
        pend = await db.ap_posts_col.find({"chan": cid, "posted": False, "ptype": "batch"}).sort("not_before", 1).to_list(None)
        ready = [p for p in pend if float(p.get("not_before") or 0) <= now]
        wait = [p for p in pend if float(p.get("not_before") or 0) > now]
        todo, _ = await BT.counts(cid)
        gap, min_h = BT.settings_of(ch)
        lines = [f"• #{p['seq']} {html.escape(p['title'][:26])} — in {max(1, int((p['not_before'] - now) // 86400))} d"
                 for p in wait[:8]]
        await show(update,
                   f"{note}🗂 <b>Queue</b> — {html.escape(ch['title'])}\n\n"
                   f"▫️ Posted: <b>{posted}</b> · Ready: <b>{len(ready)}</b> · Waiting: <b>{len(wait)}</b>\n"
                   f"▫️ Preparing: <b>{todo}</b> folders\n"
                   f"▫️ Last post no.: <b>{ch.get('post_no', 0)}</b>\n"
                   f"▫️ Gap: <b>{gap}</b> d (+0-{BT.GAP_JITTER_DAYS}) · <b>{min_h:g}</b> h between posts\n"
                   f"▫️ Last posted: <b>{_ago((ch.get('queue_state') or {}).get('last_posted_at'))}</b>\n\n"
                   f"<b>Waiting</b>\n" + ("\n".join(lines) if lines else "<i>none</i>"),
                   M([[B("🔄 Refresh", f"adm:apq:{cid}")], [B("🔙 Back", f"adm:apc:{cid}")]]))
        return
    qs = ch.get("queue_state") or {}
    queue, cursor = qs.get("queue") or [], int(qs.get("cursor", 0))
    nxt = AP.next_slot(ch.get("slots", []))
    batch = int(nxt[1]["n"]) if nxt else 1
    up = await db.ap_upcoming(cid, 10)
    lines = [f"{'▶️' if i < batch else '•'} #{p['seq']} {html.escape(p['title'][:30])}" for i, p in enumerate(up)]
    when = (f"{nxt[0].strftime('%H:%M')} IST ×{nxt[1]['n']}" if nxt else "no timing set")
    await show(update,
               f"{note}🗂 <b>Queue</b> — {html.escape(ch['title'])}\n\n"
               f"▫️ Remaining: <b>{total - posted}</b> · Posted: <b>{posted}</b>\n"
               f"▫️ Cycle: <b>{qs.get('cycle', 0)}</b> · Cursor: <b>{cursor}/{len(queue)}</b>\n"
               f"▫️ Mode: <b>{'🔁 loop' if ch.get('loop', True) else '➡️ one pass'}</b>\n"
               f"▫️ Last posted: <b>{_ago(qs.get('last_posted_at'))}</b>\n"
               f"▫️ Next: <b>{when}</b>\n\n"
               f"<b>Up next</b> (▶️ = next slot)\n"
               + ("\n".join(lines) if lines else "<i>empty</i>"),
               M([[B("🔀 Reshuffle", f"adm:apresh:{cid}"), B("🔄 Refresh", f"adm:apq:{cid}")],
                  [B("🔙 Back", f"adm:apc:{cid}")]]))


@action("apq")
async def a_apq(update, context, args):
    await show_queue(update, int(args[0]))


@action("apresh")
async def a_apresh(update, context, args):
    cid = int(args[0])
    await db.ap_rebuild_cycle(cid)
    await show_queue(update, cid, "🔀 Queue reshuffled.\n\n")


@action("apmode")
async def a_apmode(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid)
    if ch:
        await db.ap_set(cid, loop=not ch.get("loop", True))
    ch = await db.ap_get_channel(cid) or {}
    await show_channel(update, cid,
                       ("🔁 Loop: when every post is posted, the queue is reshuffled and starts again.\n\n"
                        if ch.get("loop", True) else
                        "➡️ One pass: the Autoposter turns OFF after every post is posted once.\n\n"))


@action("aprm")
async def a_aprm(update, context, args):
    cid = int(args[0])
    await show(update,
               "🗑 <b>Remove this channel from Autoposter?</b>\n\nIts post list is deleted from the Autoposter. "
               "Share links and the files in the Database Channel stay.",
               M([[B("✅ Yes, remove", f"adm:aprmyes:{cid}")], [B("❌ No", f"adm:apc:{cid}")]]))


@action("aprmyes")
async def a_aprmyes(update, context, args):
    await db.ap_remove_channel(int(args[0]))
    await show(update, "🗑 Channel removed.", K.back("ap"))


# ===================================================================== caption & button
async def show_cb(update, cid: int, note: str = ""):
    ch = await db.ap_get_channel(cid)
    if not ch:
        await show(update, "⚠️ Channel not found.", K.back("ap"))
        return
    tpl = (ch.get("cap_tpl") or "").replace("{caption}", "").strip()
    btn = ch.get("btn") or AP.DEFAULT_BTN
    await show(update,
               f"{note}🎨 <b>Caption &amp; Button</b> — {html.escape(ch['title'])}\n\n"
               f"📝 Caption: {tpl or '<i>not set</i>'}\n"
               f"🔘 Button: <b>{html.escape(btn)}</b>\n"
               f"🖼 Thumbnail: <b>{'set ✅' if ch.get('fthumb') else 'not set ⚠️'}</b>\n\n"
               f"<b>Image + Video</b>\n"
               f"🔘 Buttons: <b>{html.escape(ch.get('btn_img') or AP.DEFAULT_BTN_IMG)}</b> · "
               f"<b>{html.escape(ch.get('btn_vid') or AP.DEFAULT_BTN_VID)}</b>\n\n"
               f"<b>Folder Batch</b>\n"
               f"🌐 imgbb keys: <b>{len(BT.keys()) or 'none'}</b>\n"
               f"📰 Telegraph title: <code>{html.escape(ch.get('tg_title') or BT.DEFAULT_TG_TITLE)}</code>\n"
               f"🔢 Last post no.: <b>{ch.get('post_no', 0)}</b>\n"
               "🏷 Tags: <code>{Preview Image}</code> <code>{Video Link}</code> <code>{Post Number}</code>",
               M([[B("📝 Edit Caption", f"adm:apcap:{cid}"), B("🔘 Edit Button", f"adm:apbtn:{cid}")],
                  [B("🖼 Fixed Thumbnail", f"adm:apthumb:{cid}"), B("🔘 Pair Buttons", f"adm:appbtn:{cid}")],
                  [B("🌐 imgbb API Keys", f"adm:apimg:{cid}"), B("📰 Telegraph Title", f"adm:aptgt:{cid}")],
                  [B("🔢 Post Number", f"adm:appno:{cid}"), B("📆 Spacing", f"adm:apgap:{cid}")],
                  [B("♻️ Reset to default", f"adm:apcbr:{cid}")],
                  [B("🔙 Back", f"adm:apc:{cid}")]]))


@action("apcb")
async def a_apcb(update, context, args):
    await show_cb(update, int(args[0]))


@action("apcbr")
async def a_apcbr(update, context, args):
    cid = int(args[0])
    await db.ap_set(cid, cap_tpl="", btn="", btn_img="", btn_vid="", fthumb="")
    await show_cb(update, cid, "♻️ Reset.\n\n")


@action("apcap")
async def a_apcap(update, context, args):
    context.user_data["ap_cid"] = int(args[0])
    await ask(update, context, "ap_cap",
              "📝 <b>Fixed post caption</b>\n\nSend the full caption once (bold / italic / links work).\n"
              "Every post of this channel will get exactly this text - nothing else is added.\n"
              "Send <code>-</code> to remove it (then each post uses its own caption).")


@state("ap_cap")
async def st_ap_cap(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    if not msg.text or cid is None:
        await msg.reply_text("⚠️ Send the caption as text.", reply_markup=K.cancel())
        return
    tpl = "" if msg.text.strip() == "-" else msg.text_html.strip()
    if AP._plain_len(tpl) > AP.CAPTION_MAX:
        await msg.reply_text("⚠️ Too long (Telegram caption limit is 1024).", reply_markup=K.cancel())
        return
    await db.ap_set(cid, cap_tpl=tpl)
    context.user_data.pop("state", None)
    await msg.reply_text("✅ Caption saved.", reply_markup=K.back(f"apcb:{cid}"))


@action("apbtn")
async def a_apbtn(update, context, args):
    context.user_data["ap_cid"] = int(args[0])
    await ask(update, context, "ap_btn",
              f"🔘 <b>Button text</b>\n\nSend the text for the button under every post "
              f"(max {AP.BTN_MAX} characters, emoji ok), e.g. <code>⬇️ Download Now</code>\n"
              f"Send <code>-</code> for the default ({AP.DEFAULT_BTN}).")


@state("ap_btn")
async def st_ap_btn(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    txt = (msg.text or "").strip()
    if not txt or cid is None or len(txt) > AP.BTN_MAX:
        await msg.reply_text(f"⚠️ Send text up to {AP.BTN_MAX} characters.", reply_markup=K.cancel())
        return
    await db.ap_set(cid, btn="" if txt == "-" else txt)
    context.user_data.pop("state", None)
    await msg.reply_text("✅ Button saved.", reply_markup=K.back(f"apcb:{cid}"))


@action("apthumb")
async def a_apthumb(update, context, args):
    cid = int(args[0])
    if not S.get("db_channel"):
        await show(update, "⚠️ Set the Database Channel first.", M([[B("📡 Set Database Channel", "adm:dbch")]]))
        return
    context.user_data["ap_cid"] = cid
    await ask(update, context, "ap_thumb",
              "🖼 <b>Fixed thumbnail</b> (Image + Video posts)\n\nSend ONE photo. Every Image + Video post of this channel "
              "is posted with this picture (the buttons open the preview / video links).\n"
              "It is saved in the Database Channel with a backup code.\n"
              "Send <code>-</code> to remove it (Image + Video posts cannot be sent without it).")


@state("ap_thumb")
async def st_ap_thumb(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    if cid is None:
        return
    if msg.text and msg.text.strip() == "-":
        await db.ap_set(cid, fthumb="")
        context.user_data.pop("state", None)
        await msg.reply_text("✅ Fixed thumbnail removed.", reply_markup=K.back(f"apcb:{cid}"))
        return
    kind, name = media_info(msg)
    channel = S.get("db_channel")
    if kind != "photo" or not channel:
        await msg.reply_text("⚠️ Send one photo.", reply_markup=K.cancel())
        return
    it = AP.item_from_ptb(msg, kind, name)
    it["cap"], it["cap_html"] = "", ""                 # only the backup code is written under it
    token = db.new_token()
    try:
        await AP._store(context.bot, channel["id"], it, db.make_code(token, 0), update.effective_user.id)
    except Exception as e:
        await msg.reply_text(f"⚠️ Could not save it: <code>{html.escape(str(e))}</code>", reply_markup=K.cancel())
        return
    await db.ap_set(cid, fthumb=token)
    context.user_data.pop("state", None)
    await msg.reply_text("✅ Fixed thumbnail saved.", reply_markup=K.back(f"apcb:{cid}"))


@action("appbtn")
async def a_appbtn(update, context, args):
    context.user_data["ap_cid"] = int(args[0])
    await ask(update, context, "ap_pbtn",
              "🔘 <b>Image + Video buttons</b>\n\nSend two lines:\n"
              f"line 1 = image (preview) button, line 2 = video button (max {AP.BTN_MAX} characters each)\n\n"
              "<code>🖼 Preview</code>\n<code>🎬 Watch Video</code>\n\n"
              "Send <code>-</code> for the defaults.")


@state("ap_pbtn")
async def st_ap_pbtn(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    lines = [x.strip() for x in (msg.text or "").splitlines() if x.strip()]
    if cid is None or not lines:
        await msg.reply_text("⚠️ Send two lines (or <code>-</code>).", reply_markup=K.cancel())
        return
    if lines == ["-"]:
        img, vid = "", ""
    elif len(lines) == 2 and all(len(x) <= AP.BTN_MAX for x in lines):
        img, vid = lines
    else:
        await msg.reply_text(f"⚠️ Two lines, up to {AP.BTN_MAX} characters each.", reply_markup=K.cancel())
        return
    await db.ap_set(cid, btn_img=img, btn_vid=vid)
    context.user_data.pop("state", None)
    await msg.reply_text("✅ Buttons saved.", reply_markup=K.back(f"apcb:{cid}"))


async def show_keys(update, context, cid: int, note: str = ""):
    ks = BT.keys()
    rows = [[B(f"🔑 {BT.mask(k)} · {BT.key_state(k)}", "adm:noop"), B("❌", f"adm:apimgrm:{cid}:{i}")]
            for i, k in enumerate(ks)]
    rows.append([B("➕ Add key", f"adm:apimgadd:{cid}")])
    rows.append([B("🔙 Back", f"adm:apcb:{cid}")])
    await show(update,
               f"{note}🌐 <b>imgbb API keys</b>\n\n"
               "Add one or more keys from <code>api.imgbb.com</code>. The bot uses them in rotation: when a key fails "
               "or hits its limit it rests 30 min and the next key is used. Add new keys here any time.\n"
               "Uploads are slow on purpose (5-15 s pause between two images).\n\n"
               + (f"{len(ks)} key(s)." if ks else "No key yet."),
               M(rows))


@action("apimg")
async def a_apimg(update, context, args):
    await show_keys(update, context, int(args[0]))


@action("apimgadd")
async def a_apimgadd(update, context, args):
    context.user_data["ap_cid"] = int(args[0])
    await ask(update, context, "ap_imgkey",
              "🌐 <b>Add imgbb API key</b>\n\nSend the key. Your message is deleted right after it is saved.")


@action("apimgrm")
async def a_apimgrm(update, context, args):
    await BT.remove_key(int(args[1]))
    await show_keys(update, context, int(args[0]), "🗑 Key removed.\n\n")


@state("ap_imgkey")
async def st_ap_imgkey(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    txt = (msg.text or "").strip()
    if not txt or cid is None or " " in txt or len(txt) > 100:
        await msg.reply_text("⚠️ Send just the API key (one word).", reply_markup=K.cancel())
        return
    await BT.add_key(txt)
    context.user_data.pop("state", None)
    try:
        await msg.delete()                      # do not leave the key in the chat
    except Exception:
        pass
    await context.bot.send_message(msg.chat_id, f"✅ Key saved ({len(BT.keys())} in total).",
                                   reply_markup=K.back(f"apimg:{cid}"))


@action("aptgt")
async def a_aptgt(update, context, args):
    context.user_data["ap_cid"] = int(args[0])
    await ask(update, context, "ap_tgtitle",
              "📰 <b>Telegraph article title</b> (Folder Batch)\n\nSend the title every new Telegraph article gets. "
              "<code>{folder_name}</code> = the name of the folder (the text before “Uploaded”).\n"
              "Example: <code>{folder_name} - Full Gallery</code>\n"
              f"Send <code>-</code> for the default (<code>{BT.DEFAULT_TG_TITLE}</code>). Works for folders prepared from now on.")


@state("ap_tgtitle")
async def st_ap_tgtitle(update, context):
    msg = update.effective_message
    cid, txt = context.user_data.get("ap_cid"), (msg.text or "").strip()
    if not txt or cid is None or len(txt) > 200:
        await msg.reply_text("⚠️ Send the title as text (max 200 characters).", reply_markup=K.cancel())
        return
    await db.ap_set(cid, tg_title="" if txt == "-" else txt)
    context.user_data.pop("state", None)
    await msg.reply_text("✅ Title saved.", reply_markup=K.back(f"apcb:{cid}"))


@action("appno")
async def a_appno(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid) or {}
    context.user_data["ap_cid"] = cid
    await ask(update, context, "ap_postno",
              f"🔢 <b>Post number</b>\n\nLast number used: <b>{ch.get('post_no', 0)}</b>\n"
              "The number is given when a post goes out: last number + 1, in sequence, never repeated.\n\n"
              "Send the number your channel is at now (e.g. <code>50</code> -> the next post becomes 51).\n"
              "⚠️ Do not set it lower than the real last number, or numbers will repeat.")


@state("ap_postno")
async def st_ap_postno(update, context):
    msg = update.effective_message
    cid, txt = context.user_data.get("ap_cid"), (msg.text or "").strip()
    if cid is None or not txt.isdigit() or int(txt) > 10_000_000:
        await msg.reply_text("⚠️ Send a whole number, e.g. <code>50</code>.", reply_markup=K.cancel())
        return
    await db.ap_set(cid, post_no=int(txt))
    context.user_data.pop("state", None)
    await msg.reply_text(f"✅ Done. The next post gets number <b>{int(txt) + 1}</b>.",
                         reply_markup=K.back(f"apcb:{cid}"))


@action("apgap")
async def a_apgap(update, context, args):
    cid = int(args[0])
    ch = await db.ap_get_channel(cid) or {}
    gap, min_h = BT.settings_of(ch)
    context.user_data["ap_cid"] = cid
    await ask(update, context, "ap_gap",
              f"📆 <b>Spacing</b> (Folder Batch)\n\nNow: same folder min <b>{gap}</b> days (+ random 0-{BT.GAP_JITTER_DAYS} days) · "
              f"any two posts min <b>{min_h:g}</b> hours.\n\nSend two lines:\n"
              f"<code>30</code>  (days between posts of the same folder)\n<code>3</code>  (hours between any two posts)")


@state("ap_gap")
async def st_ap_gap(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    try:
        a, b = [float(x) for x in (msg.text or "").split()[:2]]
        if cid is None or not (1 <= a <= 365) or not (0 <= b <= 240):
            raise ValueError
    except ValueError:
        await msg.reply_text("⚠️ Two numbers: days (1-365) and hours (0-240).", reply_markup=K.cancel())
        return
    await db.ap_set(cid, gap_days=int(a), min_hours=b)
    context.user_data.pop("state", None)
    await msg.reply_text(f"✅ Saved: {int(a)} days · {b:g} hours.", reply_markup=K.back(f"apcb:{cid}"))


# ===================================================================== timings
async def show_timings(update, cid: int, note: str = ""):
    ch = await db.ap_get_channel(cid)
    if not ch:
        await show(update, "⚠️ Channel not found.", K.back("ap"))
        return
    slots = sorted(ch.get("slots", []), key=lambda s: s["t"])
    rows = []
    for s in slots:
        k = s["t"].replace(":", "")       # ":" is the callback separator
        rows.append([B(f"🕘 {s['t']}", "adm:noop"), B("➖", f"adm:apsn:{cid}:{k}:m"),
                     B(str(s["n"]), "adm:noop"), B("➕", f"adm:apsn:{cid}:{k}:p"),
                     B("❌", f"adm:apsd:{cid}:{k}")])
    rows.append([B("➕ Add Time", f"adm:apsadd:{cid}")])
    rows.append([B("🔙 Back", f"adm:apc:{cid}")])
    await show(update,
               f"{note}⏰ <b>Timings</b> — {html.escape(ch['title'])}\n\n"
               "Time is IST. Each row = a time and how many posts go out at that time.\n"
               "➖ ➕ change the count, ❌ removes the time.",
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
    await show_timings(update, cid, "✅ Time removed.\n\n")


@action("apsadd")
async def a_apsadd(update, context, args):
    context.user_data["ap_cid"] = int(args[0])
    await ask(update, context, "ap_slot",
              "➕ <b>Add Time</b> (IST, 24-hour)\n\n"
              "Send one or more lines: <code>HH:MM  posts</code>\n"
              "<code>09:00 2</code>\n<code>14:30 1</code>\n<code>21:00 3</code>\n\n"
              f"Posts is optional (default 1, max {AP.MAX_PER_SLOT}). Sending an existing time updates its count.")


@state("ap_slot")
async def st_ap_slot(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    try:
        new = AP.parse_slots(msg.text or "")
    except ValueError as e:
        await msg.reply_text(f"⚠️ {html.escape(str(e))}\nExample: <code>09:00 2</code>", reply_markup=K.cancel())
        return
    ch = await db.ap_get_channel(cid)
    if not ch:
        context.user_data.pop("state", None)
        return
    merged = {s["t"]: s["n"] for s in ch.get("slots", [])}
    merged.update({s["t"]: s["n"] for s in new})
    await db.ap_set(cid, slots=[{"t": t, "n": n} for t, n in sorted(merged.items())])
    context.user_data.pop("state", None)
    await msg.reply_text(f"✅ Saved. Timings: {AP.fmt_slots([{'t': t, 'n': n} for t, n in merged.items()])}",
                         reply_markup=K.back(f"aptm:{cid}"))


# ===================================================================== all posts
@action("apall")
async def a_apall(update, context, args):
    cid, page = int(args[0]), (int(args[1]) if len(args) > 1 else 0)
    total, posted = await db.ap_counts(cid)
    posts = await db.ap_list_posts(cid, page * PAGE, PAGE)
    rows = [[B(f"{'✅' if p['posted'] else '⏳'} #{p['seq']} {p['title'][:24]} · {'🖼+🎬' if p.get('ptype') == 'pair' else '📂' if p.get('ptype') == 'batch' else p['n_files']}",
               f"adm:app:{p['_id']}")]
            for p in posts]
    nav = []
    if page > 0:
        nav.append(B("⬅️ Prev", f"adm:apall:{cid}:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(B("Next ➡️", f"adm:apall:{cid}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B("🔙 Back", f"adm:apc:{cid}")])
    await show(update,
               f"📚 <b>All Posts</b> ({total})\n✅ posted {posted} · ⏳ pending {total - posted}\n\n"
               + ("Tap a post to open it." if total else "No posts yet. Use ➕ Create Post."),
               M(rows))


async def show_post(update, bot, token: str, note: str = ""):
    p = await db.ap_get_post(token)
    if not p:
        await show(update, "⚠️ Post not found.", K.back("ap"))
        return
    if p.get("ptype") == "pair":
        links = (f"🖼 Preview (free): <code>{share_link(bot.username, token)}</code>\n"
                 f"🎬 Video (ad): <code>{share_link(bot.username, p['vtoken'])}</code>")
    elif p.get("ptype") == "batch":
        links = (f"🖼 Preview: {p.get('tg_url') or '—'}\n"
                 + (f"🎬 Videos: <code>{share_link(bot.username, p['vtoken'])}</code>\n" if p.get("vtoken") else "")
                 + f"📂 Folder: {html.escape(p.get('batch_name') or '')}"
                 + (f"\n🔢 Posted as #{p['posted_no']}" if p.get("posted_no") else "")
                 + (f"\n🕓 Next possible: in {max(1, int((p['not_before'] - time.time()) // 86400))} d"
                    if not p["posted"] and float(p.get("not_before") or 0) > time.time() else ""))
    else:
        links = f"🔗 <code>{share_link(bot.username, token)}</code>"
    await show(update,
               f"{note}📝 <b>#{p['seq']}</b> · {'✅ posted' if p['posted'] else '⏳ pending'}"
               f"{' · 🖼+🎬 Image + Video' if p.get('ptype') == 'pair' else ''}\n"
               f"📁 Files: {p['n_files']}\n\n{html.escape(p['title'])}\n\n{links}",
               M([[B("📤 Post now", f"adm:appnow:{token}")],
                  [B("🗑 Delete post", f"adm:apdel:{token}:0"), B("🗑 Delete post + link", f"adm:apdel:{token}:1")],
                  [B("🔙 Back", f"adm:apall:{p['chan']}:0")]]))


@action("app")
async def a_app(update, context, args):
    await show_post(update, context.bot, args[0])


@action("appnow")
async def a_appnow(update, context, args):
    p = await db.ap_get_post(args[0])
    if not p:
        await show(update, "⚠️ Post not found.", K.back("ap"))
        return
    err = await AP.send_post(context.bot, p["chan"], p)
    await show_post(update, context.bot, args[0], "✅ Posted.\n\n" if not err else f"⚠️ <code>{html.escape(err)}</code>\n\n")


@action("apdel")
async def a_apdel(update, context, args):
    p = await db.ap_get_post(args[0])
    with_link = len(args) > 1 and args[1] == "1"
    await db.ap_delete_post(args[0])
    if with_link:
        for t in (db.post_tokens(p) if p else [args[0]]):
            await db.delete_link(t)
    await show(update,
               "🗑 Post and its share link deleted." if with_link
               else "🗑 Post deleted from the Autoposter (its share link still works).",
               K.back(f"apall:{p['chan']}:0" if p else "ap"))


@action("apwipe")
async def a_apwipe(update, context, args):
    cid = int(args[0])
    total, posted = await db.ap_counts(cid)
    await show(update,
               f"🧹 <b>Wipe posts</b>\n\n📚 {total} post(s) · ✅ posted {posted} · ⏳ pending {total - posted}\n\n"
               "<b>Posts only</b>: posts leave the Autoposter, old share links keep working.\n"
               "<b>Posts + links</b>: the share links are deleted too (files stay in the Database Channel).",
               M([[B("🧹 Wipe ALL posts", f"adm:apwipey:{cid}:a0")],
                  [B("🧹 Wipe ALL posts + links", f"adm:apwipey:{cid}:a1")],
                  [B("✅ Wipe only posted ones", f"adm:apwipey:{cid}:p0")],
                  [B("🔙 Back", f"adm:apc:{cid}")]]))


@action("apwipey")
async def a_apwipey(update, context, args):
    cid, mode = int(args[0]), args[1]
    n = await db.ap_wipe(cid, only_posted=mode[0] == "p", links=mode[1] == "1")
    await show_channel(update, cid, f"🧹 {n} post(s) deleted.\n\n")


# ===================================================================== create post
@action("apnew")
async def a_apnew(update, context, args):
    cid = int(args[0])
    if not S.get("db_channel"):
        await show(update, "⚠️ Set the Database Channel first.", M([[B("📡 Set Database Channel", "adm:dbch")]]))
        return
    ch = await db.ap_get_channel(cid) or {}
    if ch.get("ptype") == "batch":
        await show(update,
                   "➕ <b>Create Post</b> — 📂 Folder Batch\n\n"
                   "1. Each folder ends with a text containing “Uploaded”.\n"
                   "2. Send first + last message link of the range.\n"
                   "3. Bot prepares everything in the background."
                   + ("" if BT.enabled() else "\n\n⚠️ Add an imgbb key first (Caption & Button → imgbb API Keys).")
                   + ("" if ch.get("cap_tpl") else "\n\n💡 Fixed caption not set."),
                   M([[B("🔢 Range: first + last message", f"adm:aprange:{cid}")],
                      [B("🔙 Back", f"adm:apc:{cid}")]]))
        return
    if ch.get("ptype") == "pair":
        how = ("🖼+🎬 <b>Image + Video</b>\n\n"
               "Files in order: image, video, image, video…\n"
               "Each pair = 1 post (image free · video after ad).\n"
               + ("" if ch.get("fthumb") else "\n⚠️ Set the Fixed Thumbnail first.\n"))
    else:
        how = ("🎞 <b>Classic</b>\n\n"
               "Captioned file = thumbnail.\n"
               "Captionless files below it = post files.\n")
    await show(update,
               f"➕ <b>Create Post</b> — {how}",
               M([[B("🔢 Range: first + last message", f"adm:aprange:{cid}")],
                  [B("📥 Send / forward files, then Done", f"adm:apcollect:{cid}")],
                  [B("🔙 Back", f"adm:apc:{cid}")]]))


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
    if not streamer.ready:
        await show(update,
                   "⚠️ Range mode needs the MTProto engine (API_ID, API_HASH, BASE_URL).\n"
                   f"Status: <code>{html.escape(streamer.error or 'not running')}</code>\n\n"
                   "You can still use <b>Send / forward files</b>.",
                   K.back(f"apnew:{cid}"))
        return
    context.user_data["ap_cid"] = cid
    await ask(update, context, "ap_first",
              "🔢 <b>Create posts by range</b>\n\nThe bot must be a <b>member / admin</b> of the source channel.\n\n"
              "Step 1/2: forward the <b>FIRST</b> message of the source channel (or send its link).")


@state("ap_first")
async def st_ap_first(update, context):
    msg = update.effective_message
    ref = parse_post_ref(msg)
    if not ref:
        await msg.reply_text("⚠️ Forward a post of the source channel or send a post link "
                             "(<code>t.me/c/123/45</code>).", reply_markup=K.cancel())
        return
    context.user_data["ap_src"] = ref
    context.user_data["state"] = "ap_last"
    await msg.reply_text("Step 2/2: now forward the <b>LAST</b> message of the range (or send its link).",
                         reply_markup=K.cancel())


@state("ap_last")
async def st_ap_last(update, context):
    msg = update.effective_message
    ref, first = parse_post_ref(msg), context.user_data.get("ap_src")
    if not ref or not first:
        await msg.reply_text("⚠️ Forward the last post or send its link.", reply_markup=K.cancel())
        return
    same = type(ref[0]) is not type(first[0]) or str(ref[0]).lower() == str(first[0]).lower()
    if not same or ref[1] < first[1] or ref[1] - first[1] + 1 > MAX_RANGE:
        await msg.reply_text(f"⚠️ The last post must be from the same channel and come after the first "
                             f"(max {MAX_RANGE} messages).", reply_markup=K.cancel())
        return
    cid = context.user_data.get("ap_cid")
    src, a, b = first[0], first[1], ref[1]
    for k in ("state", "ap_src", "ap_cid"):
        context.user_data.pop(k, None)
    status = await msg.reply_text("⏳ Reading the source channel…")
    if ((await db.ap_get_channel(cid)) or {}).get("ptype") == "batch":
        if not BT.enabled():
            await status.edit_text("⚠️ Add an imgbb API key first (🎨 Caption & Button → imgbb API Keys).",
                                   reply_markup=K.back(f"apc:{cid}"))
            return
        AP.spawn(_job(_run_range_batch(context.bot, src, a, b, cid, update.effective_user.id, status),
                      context.bot, msg.chat_id))
        return
    AP.spawn(_job(_run_range(context.bot, src, a, b, cid, update.effective_user.id, status), context.bot, msg.chat_id))


async def _run_range(bot, src, first: int, last: int, cid: int, by: int, status):
    client = streamer.client
    ids = list(range(first, last + 1))
    items = []
    for i in range(0, len(ids), 200):
        for m in await client.get_messages(src, ids[i:i + 200]):
            it = AP.item_from_pyro(m)
            if it:
                items.append(it)
        try:
            await bot.edit_message_text(f"⏳ Reading the source channel… {min(i + 200, len(ids))}/{len(ids)}",
                                        chat_id=status.chat_id, message_id=status.message_id)
        except Exception:
            pass
    if not items:
        await bot.edit_message_text("⚠️ No files found in that range. Is the bot a member of the source channel?",
                                    chat_id=status.chat_id, message_id=status.message_id,
                                    reply_markup=K.back(f"apc:{cid}"))
        return
    await AP.create_posts(bot, items, cid, by, status.chat_id, status.message_id)


async def _run_range_batch(bot, src, first: int, last: int, cid: int, by: int, status):
    """Folder Batch: read the range, find the folders ("... Uploaded ✅" texts) and queue them as resumable jobs."""
    client = streamer.client
    ids = list(range(first, last + 1))
    entries = []
    for i in range(0, len(ids), 200):
        for m in await client.get_messages(src, ids[i:i + 200]):
            e = BT.item_or_marker(m)
            if e:
                entries.append(e)
        try:
            await bot.edit_message_text(f"⏳ Reading the source channel… {min(i + 200, len(ids))}/{len(ids)}",
                                        chat_id=status.chat_id, message_id=status.message_id)
        except Exception:
            pass
    folders, left = BT.scan_folders(entries)
    n_posts = sum(max(1, len(BT.plan_folder(f["items"])[2])) for f in folders if BT.plan_folder(f["items"])[0])
    plain = sum(1 for f in folders if BT.plan_folder(f["items"])[0] and not BT.plan_folder(f["items"])[2])
    fixed_ok = bool((await db.ap_get_channel(cid) or {}).get("fthumb"))
    queued = await BT.queue_folders(cid, folders, by)
    no_photo = len(folders) - queued
    if not queued:
        text = ("⚠️ No folder found. A folder needs at least one photo and a text with “Uploaded” below its files."
                if not folders else "⚠️ The folders had no photos, nothing to prepare.")
    else:
        text = (f"✅ <b>{queued} folder(s)</b> queued → <b>{n_posts} post(s)</b> will be created.\n"
                "The bot prepares them slowly in the background (imgbb cooldown) and the posts appear in 📚 All Posts. "
                "If imgbb keys run out it pauses and continues by itself.")
    if plain:
        text += (f"\n🖼 {plain} folder(s) have no captioned photo → one post each with the fixed thumbnail"
                 + ("." if fixed_ok else " ⚠️ <b>Fixed thumbnail is not set</b> (🎨 Caption & Button → Fixed Thumbnail), "
                    "those posts cannot go out until you set it."))
    if no_photo and queued:
        text += f"\n⏭ Folders without any photo (skipped): {no_photo}"
    if left:
        text += f"\n⏭ Files after the last “Uploaded” text (ignored): {left}"
    await bot.edit_message_text(text, chat_id=status.chat_id, message_id=status.message_id,
                                reply_markup=K.back(f"apc:{cid}"))


# ---- (b) collect mode: the admin sends / forwards the files to the bot
def _collect_text(n: int) -> str:
    return (f"📥 <b>Collecting files</b>\n\n{n} file(s) received.\n"
            "Send / forward them <b>in order</b> (classic: caption file first, its captionless files below · "
            "Image + Video: image 1, video 1, image 2, video 2 …), then tap <b>Done</b>.")


def _collect_kb():
    return M([[B("✅ Done", "adm:apdone")], [B("❌ Cancel", "adm:cancel")]])


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
        await msg.reply_text("📎 Send files only, or tap <b>Done</b>.", reply_markup=_collect_kb())
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
        await show(update, "⚠️ Send at least one file first.\n\n" + _collect_text(0), _collect_kb())
        return
    for k in ("state", "ap_items", "ap_cid", "ap_status", "ap_ts"):
        ud.pop(k, None)
    status = await update.effective_chat.send_message(f"⏳ Starting… {len(items)} file(s)")
    AP.spawn(_job(AP.create_posts(context.bot, items, cid, update.effective_user.id,
                                  status.chat_id, status.message_id),
                  context.bot, status.chat_id))
