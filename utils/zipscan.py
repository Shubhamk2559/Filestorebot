"""
utils/zipscan.py
Stdlib-only helpers of the Vault Pack ZIP upload (no Telegram / MongoDB imports, so they can be tested offline).

  extract()        safe ZIP extraction (no path traversal, fixes Hindi / UTF-8 file names, checks free disk space)
  scan_dir()       extracted tree -> folders [{"name", "images": [rel paths], "videos": [rel paths], "ignored": n}]
  chunk_nodes()    Telegraph content nodes -> pages that stay under the Telegraph size limit
  probe()          optional ffprobe / ffmpeg: duration, width, height + a small thumbnail for videos

Images are NEVER re-encoded or resized here - the original bytes go to imgbb untouched.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

# formats imgbb accepts AND every browser (Telegraph) can show
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VID_EXT = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".3gp", ".wmv", ".flv", ".ts", ".mpg", ".mpeg"}
STREAMABLE = {".mp4", ".m4v", ".mov"}          # sent as a real Telegram video; the rest as a document
IMGBB_MAX = 32 * 1024 * 1024                  # imgbb limit per image
TELEGRAPH_CHUNK = 50000                       # bytes of JSON per Telegraph page (hard limit is 64 KB)


def work_dir() -> str:
    """Where ZIPs are extracted. ZIP_WORK_DIR, else /data/zipwork (docker volume), else the temp dir."""
    p = os.getenv("ZIP_WORK_DIR") or "/data/zipwork"
    try:
        os.makedirs(p, exist_ok=True)
        if os.access(p, os.W_OK):
            return p
    except OSError:
        pass
    p = os.path.join(tempfile.gettempdir(), "zipvault")
    os.makedirs(p, exist_ok=True)
    return p


def nat_key(s: str):
    """'img10.jpg' sorts after 'img9.jpg'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _fix_name(info: zipfile.ZipInfo) -> str:
    n = info.filename
    if not info.flag_bits & 0x800:                 # no UTF-8 flag: python guessed cp437, most tools wrote UTF-8
        try:
            n = n.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return n.replace("\\", "/")


def extract(zip_path: str, dest: str, progress=None) -> int:
    """Extract every file of the ZIP into dest. Returns the number of files. Raises a readable error on a bad ZIP."""
    dest = os.path.realpath(dest)
    os.makedirs(dest, exist_ok=True)
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise RuntimeError("not a valid ZIP file (RAR / 7z are not supported)")
    n = 0
    done = 0
    with z:
        need = sum(i.file_size for i in z.infolist())
        free = shutil.disk_usage(dest).free
        if need > free * 0.92:
            raise RuntimeError(f"not enough disk space: ZIP needs {need // 2**20} MB, {free // 2**20} MB free")
        for info in z.infolist():
            if info.is_dir():
                continue
            target = os.path.realpath(os.path.join(dest, _fix_name(info)))
            if not target.startswith(dest + os.sep):          # ../../ path traversal -> skipped
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                with z.open(info) as src, open(target, "wb") as out:
                    while True:
                        chunk = src.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, need, n + 1)
            except RuntimeError as e:
                raise RuntimeError("the ZIP is password protected" if "password" in str(e).lower() else str(e))
            n += 1
    return n


def _hidden(parts) -> bool:
    return any(p.startswith(".") or p == "__MACOSX" for p in parts)


def scan_dir(root: str, fallback: str = "Folder") -> list:
    """Folders of an extracted ZIP. One folder = one first-level directory (files in sub-directories belong to it).
    A single wrapper directory ('MyPack/Wedding', 'MyPack/Puja') is skipped. Loose files in the root go to `fallback`.
    Paths are relative to `root`. Images / videos are in natural order (img2 before img10)."""
    rootp = Path(root)
    base = rootp
    while True:
        kids = [k for k in base.iterdir() if not _hidden([k.name])]
        if len(kids) == 1 and kids[0].is_dir() and any(c.is_dir() for c in kids[0].iterdir()):
            base = kids[0]
            continue
        break
    groups: dict = {}
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(base)
        if _hidden(rel.parts):
            continue
        unit = rel.parts[0] if len(rel.parts) > 1 else fallback
        g = groups.setdefault(unit, {"name": clean_name(unit), "images": [], "videos": [], "ignored": 0})
        ext = p.suffix.lower()
        if ext in IMG_EXT:
            g["images"].append(str(p.relative_to(rootp)))
        elif ext in VID_EXT:
            g["videos"].append(str(p.relative_to(rootp)))
        else:
            g["ignored"] += 1
    out = sorted(groups.values(), key=lambda g: nat_key(g["name"]))
    for g in out:
        g["images"].sort(key=nat_key)
        g["videos"].sort(key=nat_key)
    return out


def clean_name(unit: str) -> str:
    """'Wedding Uploaded ✅' -> 'Wedding' (same rule as the channel range mode)."""
    t = re.split(r"\buploaded\b", unit, 1, flags=re.I)[0]
    t = re.sub(r"[✅✔☑️\s]+$", "", t).strip(" -:|_")
    return re.sub(r"\s+", " ", t).strip() or re.sub(r"\s+", " ", unit).strip() or "Folder"


def chunk_nodes(urls: list) -> list:
    """Image URLs -> list of Telegraph node lists, each safely below the Telegraph content limit."""
    pages, cur, size = [], [], 2
    for u in urls:
        node = {"tag": "img", "attrs": {"src": u}}
        n = len(json.dumps(node).encode()) + 1
        if cur and size + n > TELEGRAPH_CHUNK:
            pages.append(cur)
            cur, size = [], 2
        cur.append(node)
        size += n
    if cur:
        pages.append(cur)
    return pages


def probe(path: str) -> dict:
    """Video metadata for Telegram (duration / width / height / thumb). {} when ffprobe is not installed."""
    if not shutil.which("ffprobe"):
        return {}
    meta: dict = {}
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height:format=duration", "-of", "json", path],
                           capture_output=True, text=True, timeout=90)
        js = json.loads(r.stdout or "{}")
        st = (js.get("streams") or [{}])[0]
        dur = float((js.get("format") or {}).get("duration") or 0)
        meta = {"duration": int(dur), "width": int(st.get("width") or 0), "height": int(st.get("height") or 0)}
        if shutil.which("ffmpeg"):
            thumb = path + ".thumb.jpg"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(min(1.0, dur / 2)), "-i", path,
                            "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", thumb],
                           capture_output=True, timeout=90)
            if os.path.exists(thumb) and os.path.getsize(thumb) > 0:
                meta["thumb"] = thumb
    except Exception:
        pass
    return {k: v for k, v in meta.items() if v}
