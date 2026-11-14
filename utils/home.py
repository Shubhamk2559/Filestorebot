"""
utils/home.py - landing page served at https://<your-domain>/

  * image   : put your picture in  static/  as  home.jpg / home.jpeg / home.png / home.webp
  * link    : the button opens your bot (t.me/<bot username>) automatically.
              Set HOME_LINK in .env to point it somewhere else (e.g. your channel).
  * texts   : HOME_TITLE, HOME_TEXT, HOME_BUTTON in .env (all optional)
  * channels: the 5 buttons below the main button. Link for each channel is taken from (in this order)
              1) HOME_CHANNELS in .env:  Pookie Hub=https://t.me/xxx;Lust Corner=https://t.me/yyy;...
              2) a Force-Sub channel in the panel with the SAME name (its join link is reused)
              3) your bot link (fallback, so a button is never dead)
  * theme   : neon pink / dark, round image with glowing ring, floating hearts + sparkles, rotating neon ring, shimmer title
"""
import html
import os

from aiohttp import web

import config

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
EXTS = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _image():
    for ext, mime in EXTS.items():
        p = os.path.join(STATIC, f"home.{ext}")
        if os.path.isfile(p):
            return p, mime
    return None, None


def _link(bot) -> str:
    custom = os.getenv("HOME_LINK", "").strip()
    if custom:
        return custom
    return f"https://t.me/{bot.username}" if bot and getattr(bot, "username", None) else "#"


CHANNELS = [("Pookie Hub", "🎀"), ("Lust Corner", "🔥"), ("Desi Hub", "🌶"), ("Premium Hub", "💎"),
            ("Trending Reels", "🎬")]


def _channel_links() -> dict:
    """{lowercase name: url} from HOME_CHANNELS, then from Force-Sub channels with the same title."""
    out = {}
    try:
        from database.db import settings as S
        for c in S.get("fsub_channels") or []:
            title, url = (c.get("title") or "").strip().lower(), (c.get("link") or "").strip()
            if title and url:
                out[title] = url
    except Exception:
        pass
    for part in os.getenv("HOME_CHANNELS", "").split(";"):
        name, _, url = part.partition("=")
        if name.strip() and url.strip():
            out[name.strip().lower()] = url.strip()
    return out


async def image(request: web.Request) -> web.StreamResponse:
    path, mime = _image()
    if not path:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": mime, "Cache-Control": "public, max-age=3600"})


async def page(request: web.Request) -> web.Response:
    title = os.getenv("HOME_TITLE", "Desi Baddie").strip() or "Desi Baddie"
    first, _, rest = title.partition(" ")
    t1, t2 = html.escape(first), html.escape(rest)
    text = html.escape(os.getenv("HOME_TEXT", "Come closer... your private collection is just one tap away."))
    button = html.escape(os.getenv("HOME_BUTTON", "Unlock on Telegram"))
    link = html.escape(_link(request.app.get("bot")), quote=True)
    has_img = _image()[0] is not None
    img_tag = ('<div class="ringwrap"><div class="ring"><img src="/assets/home-image" alt="" width="420" height="420">'
               '</div></div>') if has_img else ""
    og = (f'<meta property="og:image" content="{html.escape(config.BASE_URL, quote=True)}/assets/home-image">'
          if has_img and config.BASE_URL else "")
    plane = ('<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path fill="#fff" '
             'd="M21.9 3.6 2.8 10.9c-1.3.5-1.3 1.3-.2 1.6l4.9 1.5 1.9 5.8c.2.6.1.8.7.8.4 0 .6-.2.9-.5l2.3-2.3 4.8 3.5'
             'c.9.5 1.5.2 1.7-.8l3.2-15.1c.3-1.2-.5-1.8-1.4-1.4ZM8.5 13.6l9.9-6.2c.5-.3.9-.1.5.2l-8.1 7.3-.3 3.3-2-4.6Z"/></svg>')
    links = _channel_links()
    chans = "".join(
        f'<a class="ch" href="{html.escape(links.get(n.lower()) or _link(request.app.get("bot")), quote=True)}" '
        f'target="_blank" rel="noopener"><em>{ico}</em><span>{html.escape(n)}</span></a>' for n, ico in CHANNELS)
    glyphs = ["♥", "✦", "💋", "♥", "✦", "♥", "💋", "✦", "♥"]
    stars = "".join(f'<i style="left:{x}%;animation-delay:{d}s;animation-duration:{t}s;font-size:{z}px">{g}</i>'
                    for (x, d, t, z), g in zip([(6, 0, 11, 16), (18, 3, 14, 13), (30, 6, 12, 18), (44, 1, 15, 14),
                                                (57, 4, 10, 20), (68, 7, 13, 13), (79, 2, 16, 17), (88, 5, 12, 12),
                                                (96, 8, 14, 15)], glyphs))
    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0a0107">
<title>{t1} {t2} - Telegram Bot</title>
<meta name="description" content="{text}">
<meta property="og:title" content="{t1} {t2} - Telegram Bot"><meta property="og:description" content="{text}">{og}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Pacifico&display=swap" rel="stylesheet">
<style>
@property --a{{syntax:'<angle>';initial-value:0deg;inherits:false}}
:root{{--pink:#ff2d95;--pink2:#ff7ccf;--wine:#7a0b3c;--bg:#0a0107;--muted:#f0c9dd}}
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
html,body{{margin:0;min-height:100%}}
body{{background:radial-gradient(800px 520px at 50% -8%,rgba(255,45,149,.5),transparent 70%),
radial-gradient(600px 460px at 0% 70%,rgba(190,20,90,.35),transparent 70%),
radial-gradient(700px 520px at 100% 100%,rgba(150,30,255,.3),transparent 70%),var(--bg);color:#fff;
font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow-x:hidden;
padding:max(22px,env(safe-area-inset-top)) 20px max(22px,env(safe-area-inset-bottom))}}
body:after{{content:"";position:fixed;inset:0;pointer-events:none;box-shadow:inset 0 0 140px 30px rgba(0,0,0,.75);z-index:0}}
.fx{{position:fixed;inset:0;pointer-events:none;overflow:hidden;z-index:0}}
.fx i{{position:absolute;bottom:-30px;color:var(--pink2);opacity:0;font-style:normal;text-shadow:0 0 12px var(--pink);animation:rise linear infinite}}
@keyframes rise{{0%{{transform:translateY(0) rotate(0) scale(.8);opacity:0}}12%{{opacity:.85}}100%{{transform:translateY(-105vh) rotate(25deg) scale(1.25);opacity:0}}}}
main{{position:relative;z-index:1;max-width:440px;margin:0 auto;text-align:center;display:flex;flex-direction:column;align-items:center}}
.crown{{font-size:34px;filter:drop-shadow(0 0 12px var(--pink));margin-bottom:-6px;animation:flick 5s infinite}}
@keyframes flick{{0%,100%{{opacity:1}}46%{{opacity:1}}48%{{opacity:.55}}50%{{opacity:1}}52%{{opacity:.7}}54%{{opacity:1}}}}
.ringwrap{{width:min(80vw,350px);aspect-ratio:1;border-radius:50%;padding:5px;margin:8px 0 20px;
background:conic-gradient(from var(--a),#ff2d95,#ffd1ee,#b400ff,#ff2d95,#ff7ccf,#ff2d95);animation:spin 5s linear infinite,glow 3s ease-in-out infinite;
box-shadow:0 0 30px 6px rgba(255,45,149,.65),0 0 80px 18px rgba(255,45,149,.35)}}
@keyframes spin{{to{{--a:360deg}}}}
@keyframes glow{{50%{{box-shadow:0 0 46px 12px rgba(255,45,149,.9),0 0 110px 28px rgba(255,45,149,.45)}}}}
.ring{{width:100%;height:100%;border-radius:50%;overflow:hidden;background:#000;border:3px solid #12020b}}
.ring img{{width:100%;height:100%;object-fit:cover;display:block}}
h1{{margin:0;font-family:Pacifico,"Brush Script MT",cursive;font-weight:400;font-size:clamp(2.7rem,13.5vw,3.8rem);line-height:1.08}}
h1 .a{{display:block;color:#fff;text-shadow:0 0 22px rgba(255,255,255,.35)}}
h1 .b{{display:block;color:transparent;background:linear-gradient(100deg,#ff2d95 20%,#fff 45%,#ff2d95 55%,#ff1f6e 80%);background-size:250% 100%;
-webkit-background-clip:text;background-clip:text;animation:shine 3.6s linear infinite;filter:drop-shadow(0 0 14px rgba(255,45,149,.85))}}
@keyframes shine{{from{{background-position:150% 0}}to{{background-position:-100% 0}}}}
.tag{{margin:16px 0 4px;padding:7px 28px;background:linear-gradient(135deg,var(--pink),#ff5fb8);color:#1a0212;font-weight:800;
transform:skew(-8deg);border-radius:6px;box-shadow:0 6px 24px rgba(255,45,149,.55);letter-spacing:.5px}}
.tag span{{display:inline-block;transform:skew(8deg)}}
.glass{{margin:16px 0 8px;padding:16px 18px;border-radius:20px;background:rgba(255,45,149,.09);border:1px solid rgba(255,124,207,.35);
backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);box-shadow:0 0 24px rgba(255,45,149,.2) inset}}
.glass p{{margin:0;color:var(--muted);line-height:1.6;font-size:1.02rem}}
.glass p b{{color:#fff;font-style:italic}}
ul{{list-style:none;padding:0;margin:16px 0 22px;display:flex;gap:10px;flex-wrap:wrap;justify-content:center}}
li{{padding:8px 15px;border:1.5px solid var(--pink);border-radius:999px;background:rgba(255,45,149,.12);font-weight:700;font-size:.95rem;
box-shadow:0 0 14px rgba(255,45,149,.4)}}
li:before{{content:"♥ ";color:var(--pink2)}}
.btn{{position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:17px 20px;border-radius:18px;
color:#fff;text-decoration:none;font-weight:800;font-size:1.12rem;letter-spacing:.3px;background:linear-gradient(135deg,#ff2d95,#d4146e 55%,#ff4f8b);
box-shadow:0 0 0 2px rgba(255,255,255,.18),0 12px 32px rgba(255,45,149,.6),0 0 44px rgba(255,45,149,.5);animation:pulse 2.2s ease-in-out infinite}}
.btn:after{{content:"";position:absolute;top:0;left:-60%;width:40%;height:100%;background:linear-gradient(100deg,transparent,rgba(255,255,255,.55),transparent);
transform:skewX(-20deg);animation:sweep 3s ease-in-out infinite}}
@keyframes sweep{{0%,55%{{left:-60%}}100%{{left:130%}}}}
.btn:active{{transform:scale(.98)}}
.btn svg{{position:relative;z-index:1}}.btn span{{position:relative;z-index:1}}
@keyframes pulse{{50%{{transform:scale(1.03)}}}}
h2{{margin:28px 0 12px;font-family:Pacifico,cursive;font-weight:400;font-size:1.45rem;color:#fff;text-shadow:0 0 16px rgba(255,45,149,.9)}}
.chs{{display:grid;grid-template-columns:1fr 1fr;gap:12px;width:100%}}
.chs .ch:last-child{{grid-column:1/-1}}
.ch{{display:flex;align-items:center;justify-content:center;gap:9px;padding:15px 10px;border-radius:16px;color:#fff;text-decoration:none;font-weight:700;
font-size:.98rem;background:linear-gradient(160deg,rgba(255,45,149,.2),rgba(122,11,60,.35));border:1.5px solid rgba(255,124,207,.6);
box-shadow:0 0 16px rgba(255,45,149,.35),0 0 14px rgba(255,45,149,.2) inset;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}}
.ch em{{font-style:normal;font-size:1.25rem;filter:drop-shadow(0 0 6px rgba(255,45,149,.9))}}
.ch:active{{transform:scale(.97);box-shadow:0 0 24px rgba(255,45,149,.7)}}
.hint{{margin-top:12px;color:var(--pink2);font-size:.9rem;letter-spacing:.4px}}
.on{{margin-top:16px;color:var(--pink2);font-family:Pacifico,cursive;font-size:1.2rem;text-shadow:0 0 14px rgba(255,45,149,.8)}}
.on b{{display:inline-block;width:9px;height:9px;border-radius:50%;background:#3dff8b;margin-right:8px;box-shadow:0 0 10px #3dff8b;animation:blink 1.6s infinite}}
@keyframes blink{{50%{{opacity:.35}}}}
small{{display:block;margin-top:22px;color:#b98aa6;font-size:.78rem}}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important}}}}
</style></head>
<body><div class="fx">{stars}</div>
<main>
<div class="crown">👑</div>
{img_tag}
<h1><span class="a">{t1}</span><span class="b">{t2}</span></h1>
<div class="tag"><span>💋 Telegram Bot</span></div>
<div class="glass"><p>{text}</p></div>
<ul><li>Fast</li><li>Secure</li><li>Unlimited</li></ul>
<a class="btn" href="{link}">{plane}<span>{button}</span></a>
<div class="hint">🔥 Private &middot; Instant &middot; Just for you</div>
<h2>Join Our Channels</h2>
<nav class="chs">{chans}</nav>
<div class="on"><b></b>Always Online ♡</div>
<small>18+ only. By continuing you confirm you are an adult.</small>
</main></body></html>"""
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "public, max-age=300"})
