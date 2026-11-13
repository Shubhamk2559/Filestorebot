"""
handlers/autoposter.py
Autoposter screens of the admin panel (registered into handlers.admin's ACTIONS / STATES).

  Panel -> 📅 Autoposter -> channel list -> pick a channel:
      ⏰ Timings        several times a day, posts per time (each time has its own count)
      📚 All Posts      every post of this channel (✅ posted / ⏳ pending), open one -> Post now / Delete
      ➕ Create Post    (a) range: first + last message of a source channel  (b) send / forward files, then Done
      ▶️ ON / OFF, 🔁 mark all pending, 🗑 remove channel
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
async def show_channel(update, cid: int, note: str = ""):
    ch = await db.ap_get_channel(cid)
    if not ch:
        await show(update, "⚠️ Channel not found.", K.back("ap"))
        return
    total, posted = await db.ap_counts(cid)
    on = ch.get("enabled", True)
    text = (f"{note}📺 <b>{html.escape(ch['title'])}</b>\n\n"
            f"Autoposter: {'ON ✅' if on else 'OFF ⏸'}\n"
            f"⏰ Timings (IST): {AP.fmt_slots(ch.get('slots', []))}\n"
            f"📚 Posts: <b>{total}</b> · ✅ posted {posted} · ⏳ pending {total - posted}")
    await show(update, text, M([
        [B("⏰ Timings", f"adm:aptm:{cid}"), B(f"📚 All Posts ({total})", f"adm:apall:{cid}:0")],
        [B("➕ Create Post", f"adm:apnew:{cid}"), B("🎨 Caption & Button", f"adm:apcb:{cid}")],
        [B("⏸ Turn OFF" if on else "▶️ Turn ON", f"adm:apon:{cid}")],
        [B("🔁 Mark all pending", f"adm:apreset:{cid}"), B("🗑 Remove", f"adm:aprm:{cid}")],
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


@action("apreset")
async def a_apreset(update, context, args):
    cid = int(args[0])
    await db.ap_reset(cid)
    await show_channel(update, cid, "🔁 All posts are pending again.\n\n")


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
    tpl = ch.get("cap_tpl") or "{caption}"
    btn = ch.get("btn") or AP.DEFAULT_BTN
    await show(update,
               f"{note}🎨 <b>Caption &amp; Button</b> — {html.escape(ch['title'])}\n\n"
               f"📝 Caption:\n<code>{html.escape(tpl)}</code>\n"
               f"🔘 Button: <b>{html.escape(btn)}</b>\n\n"
               "Applies to every post of this channel (also the ones already created).",
               M([[B("📝 Edit Caption", f"adm:apcap:{cid}"), B("🔘 Edit Button", f"adm:apbtn:{cid}")],
                  [B("♻️ Reset to default", f"adm:apcbr:{cid}")],
                  [B("🔙 Back", f"adm:apc:{cid}")]]))


@action("apcb")
async def a_apcb(update, context, args):
    await show_cb(update, int(args[0]))


@action("apcbr")
async def a_apcbr(update, context, args):
    cid = int(args[0])
    await db.ap_set(cid, cap_tpl="", btn="")
    await show_cb(update, cid, "♻️ Reset.\n\n")


@action("apcap")
async def a_apcap(update, context, args):
    context.user_data["ap_cid"] = int(args[0])
    await ask(update, context, "ap_cap",
              "📝 <b>Post caption</b>\n\nSend the caption text (bold / italic / links work).\n"
              "Write <code>{caption}</code> where the post's own caption should go, e.g.\n"
              "<code>🔥 {caption}\n\n👇 Download below</code>\n\n"
              "Without <code>{caption}</code> every post gets exactly this text.\n"
              "Send <code>-</code> to use only the post's own caption.")


@state("ap_cap")
async def st_ap_cap(update, context):
    msg = update.effective_message
    cid = context.user_data.get("ap_cid")
    if not msg.text or cid is None:
        await msg.reply_text("⚠️ Send the caption as text.", reply_markup=K.cancel())
        return
    tpl = "" if msg.text.strip() == "-" else msg.text_html.strip()
    if AP._plain_len(tpl.replace("{caption}", "")) > AP.CAPTION_MAX - 100:
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
    rows = [[B(f"{'✅' if p['posted'] else '⏳'} #{p['seq']} {p['title'][:24]} · {p['n_files']}", f"adm:app:{p['_id']}")]
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
    await show(update,
               f"{note}📝 <b>#{p['seq']}</b> · {'✅ posted' if p['posted'] else '⏳ pending'}\n"
               f"📁 Files: {p['n_files']}\n\n{html.escape(p['title'])}\n\n"
               f"🔗 <code>{share_link(bot.username, token)}</code>",
               M([[B("📤 Post now", f"adm:appnow:{token}"), B("🗑 Delete", f"adm:apdel:{token}")],
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
    await db.ap_delete_post(args[0])
    await show(update, "🗑 Post deleted from the Autoposter (its share link still works).",
               K.back(f"apall:{p['chan']}:0" if p else "ap"))


# ===================================================================== create post
@action("apnew")
async def a_apnew(update, context, args):
    cid = int(args[0])
    if not S.get("db_channel"):
        await show(update, "⚠️ Set the Database Channel first.", M([[B("📡 Set Database Channel", "adm:dbch")]]))
        return
    await show(update,
               "➕ <b>Create Post</b>\n\n"
               "A file <b>with a caption</b> = the post's thumbnail. All files <b>without a caption</b> right below it "
               "are that post's files (one share link). The next captioned file starts the next post.\n"
               "📷 An <b>album</b> is always ONE post: all its files in one link.\n\nChoose how:",
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


# ---- (b) collect mode: the admin sends / forwards the files to the bot
def _collect_text(n: int) -> str:
    return (f"📥 <b>Collecting files</b>\n\n{n} file(s) received.\n"
            "Send / forward them <b>in order</b> (caption file first, its captionless files below), then tap <b>Done</b>.")


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
