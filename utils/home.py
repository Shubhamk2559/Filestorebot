"""
utils/home.py - landing page served at https://<your-domain>/

  * theme   : light sky blue (same family as the ads page): solid + soft gradient, white card, no neon / glow.
  * image   : put your picture in  static/  as  home.jpg / home.jpeg / home.png / home.webp
  * button  : the main button opens your bot (t.me/<bot username>). HOME_LINK in .env points it somewhere else.
  * channel : ONE Telegram channel link below the main button. Taken from (in this order)
              1) HOME_CHANNEL in .env           ->  https://t.me/yourchannel
                 (HOME_CHANNEL_NAME = text shown for it, optional)
              2) the first entry of HOME_CHANNELS in .env   ->  Name=https://t.me/xxx;...   (old setting still works)
              3) the first Force-Sub channel of the panel
              4) no channel found -> the box is simply not shown
  * texts   : HOME_TITLE, HOME_TEXT, HOME_BUTTON in .env (all optional)
"""
import html
import os

from aiohttp import web

import config
from utils import support as SUP

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
EXTS = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}   # PNG wins when several exist


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


def _channel(bot=None):
    """(name, url) of the single channel shown on the page, or None."""
    url, name = os.getenv("HOME_CHANNEL", "").strip(), os.getenv("HOME_CHANNEL_NAME", "").strip()
    if not url:
        for part in os.getenv("HOME_CHANNELS", "").split(";"):
            n, _, u = part.partition("=")
            if u.strip():
                url, name = u.strip(), name or n.strip()
                break
    if not url:
        try:
            from database.db import settings as S
            for c in S.get("fsub_channels") or []:
                if (c.get("link") or "").strip():
                    url, name = c["link"].strip(), name or (c.get("title") or "").strip()
                    break
        except Exception:
            pass
    return (name or "Our Channel", url) if url else None


async def image(request: web.Request) -> web.StreamResponse:
    path, mime = _image()
    if not path:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": mime, "Cache-Control": "public, max-age=3600"})


PLANE = ('<svg viewBox="0 0 24 24" width="{s}" height="{s}" aria-hidden="true"><path fill="#fff" '
         'd="M21.9 3.6 2.8 10.9c-1.3.5-1.3 1.3-.2 1.6l4.9 1.5 1.9 5.8c.2.6.1.8.7.8.4 0 .6-.2.9-.5l2.3-2.3 4.8 3.5'
         'c.9.5 1.5.2 1.7-.8l3.2-15.1c.3-1.2-.5-1.8-1.4-1.4ZM8.5 13.6l9.9-6.2c.5-.3.9-.1.5.2l-8.1 7.3-.3 3.3-2-4.6Z"/></svg>')

CSS = """
:root{--sky:#87cefa;--sky2:#5bb7ef;--sky-d:#2b8fd8;--ink:#12354d;--muted:#4d7088;--line:#b5defa;--bg1:#eaf6fe;--bg2:#c9e8fb}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;min-height:100%}
body{min-height:100vh;color:var(--ink);font-family:"Plus Jakarta Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
background:linear-gradient(180deg,var(--bg1),var(--bg2)) fixed;overflow-x:hidden;position:relative}
.sky{position:fixed;inset:0 0 auto 0;height:340px;pointer-events:none;z-index:0;
background:linear-gradient(180deg,#9bd7fa 0%,rgba(155,215,250,.55) 45%,rgba(234,246,254,0) 100%)}
.sky i{position:absolute;border-radius:50%;background:#fff;opacity:.55}
.sky i:nth-child(1){width:150px;height:46px;left:-40px;top:70px}
.sky i:nth-child(2){width:110px;height:34px;right:-24px;top:120px;opacity:.45}
.sky i:nth-child(3){width:80px;height:26px;left:62%;top:36px;opacity:.35}
main{position:relative;z-index:1;max-width:420px;margin:0 auto;display:flex;flex-direction:column;align-items:center;
padding:max(34px,env(safe-area-inset-top)) 18px max(26px,env(safe-area-inset-bottom))}
.ringwrap{width:min(46vw,170px);aspect-ratio:1;border-radius:50%;padding:5px;background:#fff;
box-shadow:0 0 0 5px var(--sky2),0 16px 34px rgba(35,120,180,.25);position:relative;z-index:2;animation:up .5s ease both}
.ring{width:100%;height:100%;border-radius:50%;overflow:hidden;background:#dff1fd}
.ring img{width:100%;height:100%;object-fit:cover;display:block}
.card{width:100%;margin-top:-62px;padding:78px 20px 20px;border-radius:26px;background:#fff;border:1px solid var(--line);text-align:center;
box-shadow:0 14px 36px rgba(43,143,216,.16);animation:up .55s .08s ease both}
.noimg .card{margin-top:0;padding-top:26px}
h1{margin:0;font-weight:800;font-size:clamp(1.7rem,8.4vw,2.15rem);line-height:1.15;letter-spacing:-.4px;color:var(--ink)}
h1 span{background:linear-gradient(100deg,#4db3f0,#1f7cc6);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{margin:10px auto 0;max-width:320px;color:var(--muted);font-size:.97rem;line-height:1.6}
.btn{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;margin-top:20px;padding:16px 18px;border-radius:15px;
color:#fff;text-decoration:none;font-weight:700;font-size:1.04rem;background:linear-gradient(180deg,#4db3f0,#2b8fd8);
box-shadow:0 8px 18px rgba(43,143,216,.32)}
.btn:active{transform:translateY(1px);box-shadow:0 4px 10px rgba(43,143,216,.3)}
.div{display:flex;align-items:center;gap:10px;margin:20px 0 12px;color:#6c8ca3;font-size:.74rem;font-weight:700;letter-spacing:1.4px;text-transform:uppercase}
.div:before,.div:after{content:"";flex:1;height:1px;background:var(--line)}
.ch{display:flex;align-items:center;gap:13px;padding:12px 14px;border-radius:16px;text-decoration:none;text-align:left;
color:var(--ink);background:#f1f9ff;border:1px solid var(--line)}
.ch:active{background:#e3f3fe}
.ch .ic{flex:none;width:44px;height:44px;border-radius:13px;display:grid;place-items:center;background:var(--sky2)}
.ch .tx{flex:1;min-width:0}.ch b{display:block;font-size:1rem;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ch small{display:block;margin-top:1px;color:var(--muted);font-size:.82rem}
.ch .go{flex:none;color:var(--sky-d);font-size:1.5rem;line-height:1;font-weight:700}
.on{margin-top:18px;color:var(--muted);font-size:.9rem;font-weight:600}
.on b{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--sky-d);margin-right:8px;vertical-align:1px}
.sup{display:inline-block;margin-top:10px;color:var(--muted);font-size:.9rem;text-decoration:underline;text-underline-offset:3px}
.fine{margin-top:16px;color:#6c8ca3;font-size:.76rem;text-align:center}
@keyframes up{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
"""


async def page(request: web.Request) -> web.Response:
    bot = request.app.get("bot")
    title = os.getenv("HOME_TITLE", "Desi Baddie").strip() or "Desi Baddie"
    first, _, rest = title.partition(" ")
    t1, t2 = html.escape(first), html.escape(rest)
    text = html.escape(os.getenv("HOME_TEXT", "Your private collection is just one tap away."))
    button = html.escape(os.getenv("HOME_BUTTON", "Open on Telegram"))
    link = html.escape(_link(bot), quote=True)
    has_img = _image()[0] is not None
    img_tag = ('<div class="ringwrap"><div class="ring"><img src="/assets/home-image" alt="" width="170" height="170">'
               '</div></div>') if has_img else ""
    og = (f'<meta property="og:image" content="{html.escape(config.BASE_URL, quote=True)}/assets/home-image">'
          if has_img and config.BASE_URL else "")
    ch = _channel(bot)
    chan = (f'<div class="div">Official channel</div>'
            f'<a class="ch" href="{html.escape(ch[1], quote=True)}" target="_blank" rel="noopener">'
            f'<span class="ic">{PLANE.format(s=22)}</span>'
            f'<span class="tx"><b>{html.escape(ch[0])}</b><small>Tap to join</small></span><span class="go">&rsaquo;</span></a>'
            ) if ch else ""
    sup_url = SUP.url()
    sup = (f'<a class="sup" href="{html.escape(sup_url, quote=True)}" target="_blank" rel="noopener">'
           f'{html.escape(SUP.button_text().lstrip(chr(0x1F6DF) + " "))}</a>') if sup_url else ""
    title_html = f'{t1} <span>{t2}</span>' if t2 else f'<span>{t1}</span>'
    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#9bd7fa">
<title>{t1} {t2} - Telegram Bot</title>
<meta name="description" content="{text}">
<meta property="og:title" content="{t1} {t2} - Telegram Bot"><meta property="og:description" content="{text}">{og}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@500;600;700;800&display=swap" rel="stylesheet">
<style>{CSS}</style></head>
<body class="{'' if has_img else 'noimg'}"><div class="sky"><i></i><i></i><i></i></div>
<main>
{img_tag}
<section class="card">
<h1>{title_html}</h1>
<p class="sub">{text}</p>
<a class="btn" href="{link}">{PLANE.format(s=21)}<span>{button}</span></a>
{chan}
</section>
<div class="on"><b></b>Always Online</div>
{sup}
<div class="fine">18+ only. By continuing you confirm you are an adult.</div>
</main></body></html>"""
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "public, max-age=300"})
