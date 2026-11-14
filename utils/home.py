"""
utils/home.py - landing page served at https://<your-domain>/

  * image   : put your picture in  static/  as  home.jpg / home.jpeg / home.png / home.webp
  * link    : the button opens your bot (t.me/<bot username>) automatically.
              Set HOME_LINK in .env to point it somewhere else (e.g. your channel).
  * texts   : HOME_TITLE, HOME_TEXT, HOME_BUTTON in .env (all optional)
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
    title = html.escape(os.getenv("HOME_TITLE", "Desibaddie"))
    text = html.escape(os.getenv("HOME_TEXT", "Tap the button to open the Telegram bot and get your files instantly."))
    button = html.escape(os.getenv("HOME_BUTTON", "Open on Telegram"))
    link = html.escape(_link(request.app.get("bot")), quote=True)
    has_img = _image()[0] is not None
    img_tag = '<img class="pic" src="/assets/home-image" alt="">' if has_img else ""
    og = (f'<meta property="og:image" content="{html.escape(config.BASE_URL, quote=True)}/assets/home-image">'
          if has_img and config.BASE_URL else "")
    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<meta name="description" content="{text}">
<meta property="og:title" content="{title}"><meta property="og:description" content="{text}">{og}
<style>
:root{{--bg:#0e0f14;--card:#171923;--text:#f2f3f7;--muted:#a3a7b8;--accent:#e63e6d;--accent2:#ff7a45}}
@media (prefers-color-scheme: light){{:root{{--bg:#f4f5f9;--card:#fff;--text:#161821;--muted:#5d6173}}}}
*{{box-sizing:border-box}}
html,body{{margin:0;min-height:100%}}
body{{background:radial-gradient(1200px 600px at 50% -10%,rgba(230,62,109,.25),transparent),var(--bg);color:var(--text);
font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;display:flex;align-items:center;justify-content:center;
min-height:100vh;padding:24px;padding-top:max(24px,env(safe-area-inset-top));padding-bottom:max(24px,env(safe-area-inset-bottom))}}
.card{{width:100%;max-width:420px;background:var(--card);border-radius:24px;overflow:hidden;text-align:center;
box-shadow:0 20px 60px rgba(0,0,0,.35)}}
.pic{{display:block;width:100%;max-height:60vh;object-fit:cover}}
.in{{padding:26px 24px 30px}}
h1{{margin:0 0 10px;font-size:1.7rem;letter-spacing:.3px}}
p{{margin:0 0 24px;color:var(--muted);line-height:1.55}}
.btn{{display:inline-block;width:100%;padding:15px 20px;border-radius:14px;font-weight:700;font-size:1.05rem;color:#fff;
text-decoration:none;background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 8px 24px rgba(230,62,109,.4)}}
.btn:active{{transform:scale(.98)}}
</style></head>
<body><main class="card">{img_tag}
<div class="in"><h1>{title}</h1><p>{text}</p>
<a class="btn" href="{link}">✈️ {button}</a></div></main></body></html>"""
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "public, max-age=300"})
