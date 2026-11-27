"""
utils/lbcore.py - pure helpers of the Local Database Backup / Recover feature (stdlib only, unit-tested).

A backup SET is a number of ZIP parts + one index file:
    dbbackup_YYYYMMDD_part001.zip, part002.zip, ...      dbbackup_index.json

Inside every part:
    manifest.json                  files of THIS part (token, slot, kind, name, caption HTML, code, size, sha256, ...)
                                   + the link records of the links that own those files
    media/<token>/<slot><ext>      the real files, STORED (no recompression)
    thumbs/<token>/<slot>.jpg      thumbnails (where Telegram has one)
    mongo_export.jsonl.gz          ONLY in part 1: the full MongoDB export (same line format as fsb-backup-*.json.gz)

Nothing here touches Telegram or MongoDB.
"""
import hashlib
import html
import json
import os
import re
import time
import zipfile
from collections import deque

FORMAT = "fsb-localbackup"
INDEX_FORMAT = "fsb-localbackup-index"
VERSION = 1
INDEX_NAME = "dbbackup_index.json"
MANIFEST = "manifest.json"
MONGO_ENTRY = "mongo_export.jsonl.gz"

MiB, GiB = 1 << 20, 1 << 30
DEFAULT_PART_MB = 1536                      # 1.5 GB
MIN_PART_MB, MAX_PART_MB = 50, 1900         # Telegram lets a bot upload 2 GB; keep a margin
BASE_OVERHEAD = 512 * 1024                  # manifest + zip directory reserve per part
ENTRY_OVERHEAD = 4 * 1024                   # manifest line + zip headers per file
THUMB_OVERHEAD = 28 * 1024                  # thumbnail allowance for kinds that can have one
THUMB_KINDS = ("video", "audio", "document", "animation", "video_note")
NO_CAPTION = ("video_note", "sticker")      # Telegram cannot attach a caption (and so no backup code) to these
CAPTION_MAX = 1024
UPLOAD_MAX = int(2.2 * GiB)                 # biggest single file accepted by the web upload page

PART_RE = re.compile(r"^dbbackup_(\d{8})_part(\d{3})\.zip$")
_TOKEN_BAD = re.compile(r"[^A-Za-z0-9_-]")
_DEFAULT_EXT = {"photo": ".jpg", "video": ".mp4", "audio": ".mp3", "voice": ".ogg", "video_note": ".mp4",
                "animation": ".mp4", "sticker": ".webp", "document": ""}


# ------------------------------------------------------------------ names
def part_name(date: str, n: int) -> str:
    return f"dbbackup_{date}_part{n:03d}.zip"


def parse_part_name(name: str):
    """(date, part number) or None."""
    m = PART_RE.match(name or "")
    return (m.group(1), int(m.group(2))) if m else None


def is_set_file(name: str) -> bool:
    return bool(PART_RE.match(name or "")) or name == INDEX_NAME


def safe_token(token) -> str:
    return _TOKEN_BAD.sub("_", str(token))[:64] or "x"


def safe_ext(name, kind: str = "document") -> str:
    ext = os.path.splitext(name or "")[1]
    if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext or ""):
        return ext.lower()
    return _DEFAULT_EXT.get(kind, "")


def media_path(token, idx, name, kind) -> str:
    return f"media/{safe_token(token)}/{int(idx)}{safe_ext(name, kind)}"


def thumb_path(token, idx) -> str:
    return f"thumbs/{safe_token(token)}/{int(idx)}.jpg"


# ------------------------------------------------------------------ formatting
def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def eta_text(sec) -> str:
    if sec is None or sec != sec or sec < 0 or sec > 10 * 86400:
        return "…"
    sec = int(sec)
    if sec < 60:
        return f"{sec} S"
    if sec < 3600:
        return f"{sec // 60} MIN {sec % 60:02d} S"
    return f"{sec // 3600} H {(sec % 3600) // 60:02d} MIN"


class Meter:
    """Moving-window transfer speed (bytes per second)."""

    def __init__(self, window: float = 15.0):
        self.window = window
        self.samples = deque()

    def reset(self):
        self.samples.clear()

    def add(self, total: int):
        now = time.monotonic()
        self.samples.append((now, total))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.popleft()

    def speed(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return max(0.0, (b1 - b0) / (t1 - t0)) if t1 > t0 else 0.0


# ------------------------------------------------------------------ planning
def entry_cost(kind: str, size: int) -> int:
    return int(size or 0) + ENTRY_OVERHEAD + (THUMB_OVERHEAD if kind in THUMB_KINDS else 0)


def plan_parts(costs: list, limit: int, first_reserve: int = 0) -> list:
    """Split files (in order, never splitting one file) into parts of at most `limit` bytes.
    costs = per-file cost (entry_cost). first_reserve = bytes already used in part 1 (the MongoDB export).
    Returns [(first_index, end_index_exclusive), ...]. A file bigger than `limit` gets a part of its own.
    Part 1 may hold zero files when the first file does not fit next to the MongoDB export."""
    parts, i, n = [], 0, len(costs)
    while True:
        first = not parts
        cap = limit - BASE_OVERHEAD - (first_reserve if first else 0)
        used, j = 0, i
        while j < n:
            c = costs[j]
            if used + c > cap:
                if j > i:
                    break
                if first and c <= limit - BASE_OVERHEAD:
                    break                              # does not fit beside the export, fits in its own part
                # a file bigger than a whole part: it travels alone
                used += c
                j += 1
                break
            used += c
            j += 1
        parts.append((i, j))
        i = j
        if i >= n:
            return parts


# ------------------------------------------------------------------ hashing
def sha256_file(path, chunk: int = 4 * MiB, progress=None) -> str:
    h, done = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(done)
    return h.hexdigest()


# ------------------------------------------------------------------ writing a part
class Entry:
    """One file being streamed INTO a zip part (never held in RAM). Computes size + sha256 on the way."""

    def __init__(self, zf, arc: str, size_hint: int = 0, deflate: bool = False):
        zi = zipfile.ZipInfo(arc, date_time=time.localtime()[:6])
        zi.compress_type = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
        zi.file_size = int(size_hint or 0)
        zi.external_attr = 0o600 << 16
        self.arc = arc
        self.size = 0
        self._sha = hashlib.sha256()
        self._w = zf.open(zi, "w", force_zip64=int(size_hint or 0) >= (1 << 30))
        self._closed = False

    def write(self, b: bytes):
        self._sha.update(b)
        self._w.write(b)
        self.size += len(b)

    def close(self):
        if not self._closed:
            self._closed = True
            self._w.close()

    @property
    def sha256(self) -> str:
        return self._sha.hexdigest()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class PartWriter:
    def __init__(self, path):
        self.path = str(path)
        self.zf = zipfile.ZipFile(self.path, "w", zipfile.ZIP_STORED, allowZip64=True)

    def entry(self, arc: str, size_hint: int = 0, deflate: bool = False) -> Entry:
        return Entry(self.zf, arc, size_hint, deflate)

    def add_bytes(self, arc: str, data: bytes, deflate: bool = False) -> str:
        zi = zipfile.ZipInfo(arc, date_time=time.localtime()[:6])
        zi.external_attr = 0o600 << 16
        self.zf.writestr(zi, data, compress_type=zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED)
        return hashlib.sha256(data).hexdigest()

    def current_size(self) -> int:
        try:
            self.zf.fp.flush()
            return os.path.getsize(self.path)
        except Exception:
            return 0

    def finish(self, manifest: dict) -> int:
        self.add_bytes(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1).encode(), deflate=True)
        self.zf.close()
        return os.path.getsize(self.path)

    def abort(self):
        try:
            self.zf.close()
        except Exception:
            pass
        try:
            os.remove(self.path)
        except OSError:
            pass


# ------------------------------------------------------------------ verifying / reading
def read_manifest(path) -> dict:
    """The manifest of a part (reads only the zip directory + manifest.json). Raises ValueError."""
    try:
        with zipfile.ZipFile(path) as zf:
            data = zf.read(MANIFEST)
    except KeyError:
        raise ValueError("manifest.json is missing")
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        raise ValueError(f"not a valid zip ({type(e).__name__})")
    try:
        m = json.loads(data)
    except Exception:
        raise ValueError("manifest.json is damaged")
    if m.get("format") != FORMAT:
        raise ValueError("not a Filestorebot local backup")
    return m


def verify_zip(path) -> tuple:
    """Full integrity check of a finished part: CRC of every entry + manifest agrees with the zip content.
    Returns (ok, message, manifest). Blocking: run it in a thread."""
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                return False, f"corrupt entry {bad}", None
            names = {i.filename: i for i in zf.infolist()}
            m = json.loads(zf.read(MANIFEST))
            for f in m.get("files", []):
                info = names.get(f["path"])
                if info is None:
                    return False, f"missing {f['path']}", m
                if info.file_size != f["size"]:
                    return False, f"size differs for {f['path']}", m
                t = f.get("thumb")
                if t and t["path"] not in names:
                    return False, f"missing {t['path']}", m
            mg = m.get("mongo")
            if mg and mg["path"] not in names:
                return False, "MongoDB export missing", m
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", None
    return True, "ok", m


def scan_dir(folder) -> list:
    """Backup parts found in a folder: [{name, path, size, manifest | None, error}] (zip directory only: fast)."""
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if not PART_RE.match(name):
            continue
        p = os.path.join(folder, name)
        if not os.path.isfile(p):
            continue
        rec = {"name": name, "path": p, "size": os.path.getsize(p), "manifest": None, "error": ""}
        try:
            rec["manifest"] = read_manifest(p)
        except ValueError as e:
            rec["error"] = str(e)
        out.append(rec)
    return out


def read_index(folder):
    p = os.path.join(folder, INDEX_NAME)
    try:
        with open(p, "rb") as f:
            idx = json.loads(f.read(8 * MiB))
        return idx if idx.get("format") == INDEX_FORMAT else None
    except Exception:
        return None


def pick_set(found: list):
    """Group the parts by backup set and return (set_id, parts of the newest set, other set ids)."""
    sets = {}
    for r in found:
        m = r["manifest"]
        if m:
            sets.setdefault(m["set_id"], []).append(r)
    if not sets:
        return None, [], []
    best = max(sets, key=lambda s: (max(r["manifest"].get("created", 0) for r in sets[s]), len(sets[s])))
    return best, sorted(sets[best], key=lambda r: r["manifest"]["part"]), [s for s in sets if s != best]


def missing_parts(found_set: list, index) -> list:
    """Part numbers that belong to the set but are not in the folder (index first, manifests as fallback)."""
    have = {r["manifest"]["part"] for r in found_set if r["manifest"]}
    total = 0
    if index and found_set and index.get("set_id") == found_set[0]["manifest"]["set_id"]:
        total = int(index.get("total_parts") or 0)
    if not total and found_set:
        total = max(int(r["manifest"].get("total_parts") or 0) for r in found_set)
    return [n for n in range(1, total + 1) if n not in have]


def build_index(job: dict, parts: list, complete: bool) -> dict:
    return {
        "format": INDEX_FORMAT, "v": VERSION, "set_id": job["set_id"], "date": job["date"],
        "created": job["created"], "complete": complete, "bot": job.get("bot"),
        "db_channel": job.get("db_channel"), "part_limit": job["limit"],
        "total_parts": job["total_parts"], "total_files": job["total_files"],
        "total_bytes": sum(p["size"] for p in parts), "mongo_part": 1,
        "parts": [{"part": p["n"], "name": p["name"], "size": p["size"], "sha256": p["sha256"],
                   "files": p["files"], "delivered": bool(p.get("delivered"))} for p in parts],
        "not_backed_up": job.get("skipped", []), "not_backed_up_count": job.get("skipped_n", 0),
    }


def extract_entry(zf, arc: str, dst, sha256=None, size=None, chunk: int = 4 * MiB) -> tuple:
    """Stream one zip entry to `dst`, checking size + sha256 on the way. Returns (ok, reason).
    On any problem the partial file is deleted. Blocking: run it in a thread."""
    h, n = hashlib.sha256(), 0
    try:
        with zf.open(arc) as src, open(dst, "wb") as out:
            while True:
                b = src.read(chunk)
                if not b:
                    break
                h.update(b)
                out.write(b)
                n += len(b)
        if size is not None and n != size:
            raise ValueError(f"size {n} instead of {size}")
        if sha256 and h.hexdigest() != sha256:
            raise ValueError("sha256 mismatch")
        return True, ""
    except KeyError:
        err = "file is missing in the zip"
    except (zipfile.BadZipFile, EOFError) as e:
        err = f"zip is damaged ({e})"
    except ValueError as e:
        err = str(e)
    except OSError as e:
        err = f"disk error ({e})"
    try:
        os.remove(dst)
    except OSError:
        pass
    return False, err


# ------------------------------------------------------------------ captions
_TAG = re.compile(r"<[^>]*>")


def plain_len(html_text: str) -> int:
    return len(html.unescape(_TAG.sub("", html_text or "")))


def caption_with_code(cap_html: str, code: str) -> str:
    """Original caption (HTML) + the hidden backup code line - the same rule as handlers/upload.py."""
    tag = f"<code>{code}</code>"
    orig = (cap_html or "").strip()
    n = plain_len(orig)
    if n + len(code) + 2 <= CAPTION_MAX:
        return f"{orig}\n\n{tag}" if orig else tag
    room = max(0, CAPTION_MAX - len(code) - 2)
    plain = html.unescape(_TAG.sub("", orig)).strip()
    return f"{html.escape(plain[:room])}\n\n{tag}"
