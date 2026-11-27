"""
utils/lbweb.py - the two protected pages of Local Backup / Recover (served by utils/web.py on your domain).

  Backup  GET  /bkdl/<tok>            download page of ONE part (name, size, sha256, Download, "✅ Downloaded")
          GET  /bkdl/<tok>/file       the zip (HTTP Range supported = resumable downloads)
          POST /bkdl/<tok>/done       you confirm the download -> the bot deletes the part from the VPS
  Recover GET  /bkup/<tok>            upload page (chunked + resumable: a dropped connection continues where it stopped)
          GET  /bkup/<tok>/list       parts already in the restore folder
          GET  /bkup/<tok>/status     how many bytes of <name> the server already has
          PUT  /bkup/<tok>/chunk      one chunk (append-only, offset checked)

Protection: the token is 192 random bits, created only by the owner from the panel, valid for a limited time, and
bound to ONE part (download) or ONE restore folder (upload). Pages are never cached or indexed.
Only names like dbbackup_YYYYMMDD_partNNN.zip / dbbackup_index.json can be uploaded; nothing else can be written.
"""
import asyncio
import datetime as dt
import logging
import os
import secrets
import shutil
import time

from aiohttp import web

from database import db
from utils import lbcore as C
from utils import lbpaths

log = logging.getLogger(__name__)

col = db.db["lb_web"]                       # tokens: {_id, kind: "dl" | "up", exp, expire_at, ...}
DL_TTL = 24 * 3600
UP_TTL = 12 * 3600
SECURITY = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff"}
hooks: dict = {}                            # "dl_done": async fn(part_no, token) · "up_done": async fn(name, size)
_locks: dict = {}


async def init():
    await col.create_index("expire_at", expireAfterSeconds=0)


def _exp(ttl: int):
    return time.time() + ttl, dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl)


async def create(kind: str, ttl: int, **fields) -> str:
    tok = secrets.token_urlsafe(24)
    exp, at = _exp(ttl)
    await col.insert_one({"_id": tok, "kind": kind, "exp": exp, "expire_at": at, "created": time.time(), **fields})
    return tok


async def get(kind: str, tok: str):
    doc = await col.find_one({"_id": tok})
    if not doc or doc.get("kind") != kind or doc.get("exp", 0) < time.time():
        return None
    return doc


async def drop(tok: str):
    await col.delete_one({"_id": tok})


async def drop_kind(kind: str, **flt):
    await col.delete_many({"kind": kind, **flt})


def url(path: str):
    """Public https URL of a page, or None when BASE_URL is not https."""
    import config
    return f"{config.BASE_URL}{path}" if config.BASE_URL.startswith("https://") else None


def _html(body: str, status: int = 200) -> web.Response:
    return web.Response(text=body, status=status, content_type="text/html", headers=SECURITY)


def _json(obj, status: int = 200) -> web.Response:
    return web.json_response(obj, status=status, headers=SECURITY)


_STYLE = """<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content="noindex,nofollow"><title>Backup</title><style>
:root{--bg:#f6f7f9;--fg:#15171a;--mu:#6b7280;--card:#fff;--bd:#e5e7eb;--ac:#2563eb;--ok:#16a34a;--er:#dc2626}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e8eaed;--mu:#9aa0a6;--card:#181b21;--bd:#2a2e36;--ac:#5b9dff;--ok:#4ade80;--er:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;padding:20px 16px}
main{max-width:640px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}p{color:var(--mu);margin:4px 0 16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin:12px 0}
.row{display:flex;justify-content:space-between;gap:12px;padding:4px 0}.row b{font-weight:600}
code{word-break:break-all;font-size:13px;display:block;margin-top:4px;color:var(--mu)}
button,a.btn{display:block;width:100%;text-align:center;padding:12px;border-radius:10px;border:0;font:inherit;font-weight:600;
background:var(--ac);color:#fff;text-decoration:none;cursor:pointer;margin:8px 0}button.ok{background:var(--ok)}
button:disabled{opacity:.5;cursor:default}.bar{height:8px;background:var(--bd);border-radius:6px;overflow:hidden;margin:8px 0 2px}
.bar i{display:block;height:100%;width:0;background:var(--ac)}.mu{color:var(--mu);font-size:14px}.er{color:var(--er)}.okc{color:var(--ok)}
input[type=file]{width:100%;padding:12px;border:1px dashed var(--bd);border-radius:10px;background:var(--card);color:var(--fg)}
</style>"""


# ------------------------------------------------------------------ download (backup)
async def dl_page(request: web.Request):
    doc = await get("dl", request.match_info["tok"])
    if not doc or not os.path.isfile(doc.get("path", "")):
        return _html(f"<!doctype html>{_STYLE}<main><h1>Link expired</h1><p>This part is no longer on the server.</p></main>", 404)
    tok = request.match_info["tok"]
    body = f"""<!doctype html>{_STYLE}<main>
<h1>Backup part {doc['n']}</h1><p>1. Download the file. 2. Check the SHA-256. 3. Tap “Downloaded”.</p>
<div class=card><div class=row><span>File</span><b>{doc['name']}</b></div>
<div class=row><span>Size</span><b>{C.human(doc['size'])}</b></div>
<div class=mu>SHA-256<code>{doc['sha']}</code></div></div>
<a class=btn href="/bkdl/{tok}/file">⬇️ Download</a>
<button class=ok id=ok disabled>✅ Downloaded</button>
<p class=mu id=msg>The “Downloaded” button unlocks after the download has started. The bot deletes the part from the server only after you
confirm here (or after Telegram confirmed the upload).</p>
<script>
const ok=document.getElementById('ok'),msg=document.getElementById('msg');
document.querySelector('a.btn').addEventListener('click',()=>setTimeout(()=>ok.disabled=false,3000));
ok.onclick=async()=>{{ok.disabled=true;try{{const r=await fetch('/bkdl/{tok}/done',{{method:'POST'}});
msg.textContent=r.ok?'Thank you. The part is being removed from the server.':'Could not confirm. Try again.';msg.className=r.ok?'okc':'er';
if(!r.ok)ok.disabled=false;}}catch(e){{msg.textContent='Network error. Try again.';msg.className='er';ok.disabled=false;}}}};
</script></main>"""
    return _html(body)


async def dl_file(request: web.Request):
    tok = request.match_info["tok"]
    doc = await get("dl", tok)
    if not doc or not os.path.isfile(doc.get("path", "")):
        raise web.HTTPNotFound(text="This link is no longer valid.")
    await col.update_one({"_id": tok}, {"$inc": {"hits": 1}})
    headers = dict(SECURITY, **{"Content-Disposition": f'attachment; filename="{doc["name"]}"'})
    return web.FileResponse(doc["path"], headers=headers)


async def dl_done(request: web.Request):
    tok = request.match_info["tok"]
    doc = await get("dl", tok)
    if not doc:
        return _json({"ok": False}, 404)
    if not doc.get("hits"):
        return _json({"ok": False, "error": "download first"}, 400)
    fn = hooks.get("dl_done")
    if fn:
        await fn(int(doc["n"]), tok)
    return _json({"ok": True})


# ------------------------------------------------------------------ upload (recover)
def _lock(name: str) -> asyncio.Lock:
    return _locks.setdefault(name, asyncio.Lock())


def _name(request: web.Request) -> str:
    name = request.query.get("name", "")
    if not C.is_set_file(name):
        raise web.HTTPBadRequest(text="Only dbbackup_YYYYMMDD_partNNN.zip and dbbackup_index.json can be uploaded.")
    return name


def _partial(name: str) -> str:
    d = os.path.join(lbpaths.restore_dir(), ".partial")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name + ".part")


async def _up_doc(request):
    doc = await get("up", request.match_info["tok"])
    if not doc:
        raise web.HTTPNotFound(text="This upload link has expired. Create a new one in the bot.")
    return doc


async def up_page(request: web.Request):
    await _up_doc(request)
    tok = request.match_info["tok"]
    body = f"""<!doctype html>{_STYLE}<main>
<h1>Upload backup parts</h1><p>Choose the <b>dbbackup_…_partNNN.zip</b> files (and <b>dbbackup_index.json</b>). Uploads are chunked and
resume automatically if the connection drops. Keep this tab open.</p>
<input type=file id=f multiple accept=".zip,.json"><div id=list></div>
<p class=mu>When everything is uploaded, return to Telegram → Recover → Refresh.</p>
<script>
const T="{tok}",CH=8*1024*1024,sleep=ms=>new Promise(r=>setTimeout(r,ms)),q=s=>encodeURIComponent(s);
const hum=n=>n>1e9?(n/1073741824).toFixed(2)+' GB':n>1e6?(n/1048576).toFixed(1)+' MB':(n/1024).toFixed(0)+' KB';
async function status(f){{const r=await fetch(`/bkup/${{T}}/status?name=${{q(f.name)}}&size=${{f.size}}`);if(!r.ok)throw new Error(await r.text());return r.json();}}
async function send(f,row){{
  let off=(await status(f)).offset,fails=0;
  while(off<f.size){{
    const end=Math.min(off+CH,f.size);
    try{{
      const r=await fetch(`/bkup/${{T}}/chunk?name=${{q(f.name)}}&offset=${{off}}&size=${{f.size}}`,{{method:'PUT',body:f.slice(off,end)}});
      const j=await r.json().catch(()=>({{}}));
      if(r.status==409){{off=j.offset;continue}}
      if(!r.ok)throw new Error(j.error||('HTTP '+r.status));
      off=j.offset;fails=0;row.bar.style.width=(off/f.size*100)+'%';row.txt.textContent=hum(off)+' / '+hum(f.size);
    }}catch(e){{
      if(++fails>30)throw e;row.txt.textContent='Connection lost, retrying…';await sleep(Math.min(15000,1000*fails));
      try{{off=(await status(f)).offset}}catch(_){{}}
    }}
  }}
  row.txt.textContent='✅ '+hum(f.size)+' uploaded';row.txt.className='okc';
}}
document.getElementById('f').onchange=async e=>{{
  for(const f of e.target.files){{
    const d=document.createElement('div');d.className='card';d.innerHTML=`<b></b><div class=bar><i></i></div><div class=mu></div>`;
    d.firstChild.textContent=f.name;document.getElementById('list').append(d);
    const row={{bar:d.querySelector('i'),txt:d.querySelector('.mu')}};
    try{{await send(f,row)}}catch(err){{row.txt.textContent='❌ '+err.message;row.txt.className='er'}}
  }}
}};
</script></main>"""
    return _html(body)


async def up_list(request: web.Request):
    await _up_doc(request)
    out = []
    try:
        for n in sorted(os.listdir(lbpaths.restore_dir())):
            p = os.path.join(lbpaths.restore_dir(), n)
            if C.is_set_file(n) and os.path.isfile(p):
                out.append({"name": n, "size": os.path.getsize(p)})
    except OSError:
        pass
    return _json({"files": out})


async def up_status(request: web.Request):
    await _up_doc(request)
    name = _name(request)
    try:
        size = int(request.query.get("size", "0"))
    except ValueError:
        size = 0
    final = os.path.join(lbpaths.restore_dir(), name)
    if size and os.path.isfile(final) and os.path.getsize(final) == size:
        return _json({"offset": size})                          # already complete: nothing to send
    p = _partial(name)
    return _json({"offset": os.path.getsize(p) if os.path.isfile(p) else 0})


async def up_chunk(request: web.Request):
    await _up_doc(request)
    name = _name(request)
    try:
        offset, total = int(request.query["offset"]), int(request.query["size"])
    except (KeyError, ValueError):
        return _json({"error": "bad offset/size"}, 400)
    limit = C.UPLOAD_MAX if name != C.INDEX_NAME else 8 * C.MiB
    if total <= 0 or total > limit:
        return _json({"error": "file too large"}, 413)
    async with _lock(name):
        p = _partial(name)
        cur = os.path.getsize(p) if os.path.isfile(p) else 0
        if offset != cur:
            return _json({"offset": cur}, 409)                  # client re-syncs and continues from here
        free = shutil.disk_usage(lbpaths.restore_dir()).free
        if free < 256 * C.MiB:
            return _json({"error": "VPS disk is full"}, 507)
        written = 0
        try:
            with open(p, "ab") as f:
                async for piece in request.content.iter_chunked(C.MiB):
                    if cur + written + len(piece) > total:
                        raise ValueError("more data than announced")
                    await asyncio.to_thread(f.write, piece)
                    written += len(piece)
        except ValueError as e:
            os.remove(p)
            return _json({"error": str(e)}, 400)
        except (ConnectionResetError, asyncio.CancelledError, OSError) as e:
            # the connection dropped mid-chunk: keep what arrived, the client asks /status and resumes
            if isinstance(e, asyncio.CancelledError):
                raise
            return _json({"offset": os.path.getsize(p)}, 409)
        new = cur + written
        done = new == total
        if done:
            final = os.path.join(lbpaths.restore_dir(), name)
            os.replace(p, final)
    if done:
        fn = hooks.get("up_done")
        if fn:
            try:
                await fn(name, total)
            except Exception:
                log.exception("up_done hook failed")
    return _json({"offset": new, "done": done})


def setup_routes(app: web.Application):
    app.router.add_get("/bkdl/{tok}", dl_page)
    app.router.add_get("/bkdl/{tok}/file", dl_file)
    app.router.add_post("/bkdl/{tok}/done", dl_done)
    app.router.add_get("/bkup/{tok}", up_page)
    app.router.add_get("/bkup/{tok}/list", up_list)
    app.router.add_get("/bkup/{tok}/status", up_status)
    app.router.add_put("/bkup/{tok}/chunk", up_chunk)
