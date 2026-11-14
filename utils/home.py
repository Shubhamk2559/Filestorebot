"""
utils/home.py - landing page served at https://<your-domain>/

  * image   : put your picture in  static/  as  home.jpg / home.jpeg / home.png / home.webp
  * link    : the button opens your bot (t.me/<bot username>) automatically.
              Set HOME_LINK in .env to point it somewhere else (e.g. your channel).
  * texts   : HOME_TITLE, HOME_TEXT, HOME_BUTTON in .env (all optional)
  * theme   : neon pink / dark, round image with glowing ring, floating hearts
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


async def image(request: web.Request) -> web.StreamResponse:
    path, mime = _image()
    if not path:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": mime, "Cache-Control": "public, max-age=3600"})


async def page(request: web.Request) -> web.Response:
    title = os.getenv("HOME_TITLE", "Desi Baddie").strip() or "Desi Baddie"
    first, _, rest = title.partition(" ")
    t1, t2 = html.escape(first), html.escape(rest)
    text = html.escape(os.getenv("HOME_TEXT", "Tap the button to open the bot and get your files instantly."))
    button = html.escape(os.getenv("HOME_BUTTON", "Open on Telegram"))
    link = html.escape(_link(request.app.get("bot")), quote=True)
    has_img = _image()[0] is not None
    img_tag = '<div class="ring"><img src="/assets/home-image" alt="" width="420" height="420"></div>' if has_img else ""
    og = (f'<meta property="og:image" content="{html.escape(config.BASE_URL, quote=True)}/assets/home-image">'
          if has_img and config.BASE_URL else "")
    plane = ('<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path fill="#fff" '
             'd="M21.9 3.6 2.8 10.9c-1.3.5-1.3 1.3-.2 1.6l4.9 1.5 1.9 5.8c.2.6.1.8.7.8.4 0 .6-.2.9-.5l2.3-2.3 4.8 3.5'
             'c.9.5 1.5.2 1.7-.8l3.2-15.1c.3-1.2-.5-1.8-1.4-1.4ZM8.5 13.6l9.9-6.2c.5-.3.9-.1.5.2l-8.1 7.3-.3 3.3-2-4.6Z"/></svg>')
    stars = "".join(f'<i style="left:{x}%;animation-delay:{d}s;animation-duration:{t}s;font-size:{z}px">♥</i>'
                    for x, d, t, z in [(8, 0, 11, 16), (22, 3, 14, 12), (38, 6, 12, 18), (55, 1, 15, 14),
                                       (70, 4, 10, 20), (84, 7, 13, 13), (94, 2, 16, 17)])
    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#12020d">
<title>{t1} {t2} - Telegram Bot</title>
<meta name="description" content="{text}">
<meta property="og:title" content="{t1} {t2} - Telegram Bot"><meta property="og:description" content="{text}">{og}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Pacifico&display=swap" rel="stylesheet">
<style>
:root{{--pink:#ff2d95;--pink2:#ff6ec7;--bg:#12020d;--text:#fff;--muted:#e9c3da}}
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
html,body{{margin:0;min-height:100%}}
body{{background:radial-gradient(900px 520px at 50% -5%,rgba(255,45,149,.38),transparent 70%),
radial-gradient(700px 500px at 100% 100%,rgba(140,40,255,.28),transparent 70%),var(--bg);color:var(--text);
font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow-x:hidden;
padding:max(22px,env(safe-area-inset-top)) 20px max(22px,env(safe-area-inset-bottom))}}
.hearts{{position:fixed;inset:0;pointer-events:none;overflow:hidden;z-index:0}}
.hearts i{{position:absolute;bottom:-30px;color:var(--pink);opacity:0;font-style:normal;text-shadow:0 0 10px var(--pink);
animation:rise linear infinite}}
@keyframes rise{{0%{{transform:translateY(0) scale(.8);opacity:0}}12%{{opacity:.7}}100%{{transform:translateY(-105vh) scale(1.2);opacity:0}}}}
main{{position:relative;z-index:1;max-width:440px;margin:0 auto;text-align:center;display:flex;flex-direction:column;align-items:center}}
.crown{{font-size:34px;filter:drop-shadow(0 0 10px var(--pink));margin-bottom:-4px}}
.ring{{width:min(78vw,340px);aspect-ratio:1;border-radius:50%;overflow:hidden;margin:6px 0 18px;
border:3px solid var(--pink2);box-shadow:0 0 0 4px rgba(255,45,149,.25),0 0 28px 6px rgba(255,45,149,.65),0 0 70px 16px rgba(255,45,149,.35);
animation:glow 3.2s ease-in-out infinite}}
.ring img{{width:100%;height:100%;object-fit:cover;display:block}}
@keyframes glow{{50%{{box-shadow:0 0 0 4px rgba(255,45,149,.35),0 0 40px 10px rgba(255,45,149,.85),0 0 90px 24px rgba(255,45,149,.45)}}}}
h1{{margin:0;font-family:Pacifico,"Brush Script MT",cursive;font-weight:400;font-size:clamp(2.6rem,13vw,3.6rem);line-height:1.05;
text-shadow:0 0 18px rgba(255,255,255,.25)}}
h1 .a{{display:block;color:#fff}}
h1 .b{{display:block;color:var(--pink);text-shadow:0 0 22px rgba(255,45,149,.8)}}
.tag{{margin:14px 0 6px;padding:7px 26px;background:linear-gradient(135deg,var(--pink),var(--pink2));color:#1a0212;font-weight:800;
transform:skew(-8deg);border-radius:6px;box-shadow:0 6px 22px rgba(255,45,149,.45);letter-spacing:.4px}}
.tag span{{display:inline-block;transform:skew(8deg)}}
p.t{{margin:12px 8px 4px;color:var(--muted);line-height:1.55}}
ul{{list-style:none;padding:0;margin:14px 0 22px;display:flex;gap:10px;flex-wrap:wrap;justify-content:center}}
li{{padding:8px 14px;border:1.5px solid var(--pink);border-radius:999px;background:rgba(255,45,149,.1);font-weight:600;font-size:.95rem;
box-shadow:0 0 12px rgba(255,45,149,.3)}}
li:before{{content:"✓ ";color:var(--pink2)}}
.btn{{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:16px 20px;border-radius:16px;color:#fff;
text-decoration:none;font-weight:800;font-size:1.1rem;background:linear-gradient(135deg,#2aabee,#229ed9);
box-shadow:0 0 0 2px rgba(255,255,255,.15),0 10px 30px rgba(34,158,217,.5),0 0 34px rgba(255,45,149,.45);animation:pulse 2.4s ease-in-out infinite}}
.btn:active{{transform:scale(.98)}}
@keyframes pulse{{50%{{transform:scale(1.025)}}}}
.on{{margin-top:18px;color:var(--pink2);font-family:Pacifico,cursive;font-size:1.15rem}}
.on b{{display:inline-block;width:9px;height:9px;border-radius:50%;background:#3dff8b;margin-right:8px;box-shadow:0 0 10px #3dff8b;animation:blink 1.6s infinite}}
@keyframes blink{{50%{{opacity:.35}}}}
small{{display:block;margin-top:22px;color:#b98aa6;font-size:.78rem}}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important}}}}
</style></head>
<body><div class="hearts">{stars}</div>
<main>
<div class="crown">👑</div>
{img_tag}
<h1><span class="a">{t1}</span><span class="b">{t2}</span></h1>
<div class="tag"><span>Telegram Bot</span></div>
<p class="t">{text}</p>
<ul><li>Fast</li><li>Secure</li><li>Unlimited</li></ul>
<a class="btn" href="{link}">{plane}<span>{button}</span></a>
<div class="on"><b></b>Always Online ♡</div>
<small>18+ only. By continuing you confirm you are an adult.</small>
</main></body></html>"""
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "public, max-age=300"})
