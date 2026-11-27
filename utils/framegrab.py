"""
utils/framegrab.py
Vault Pack ZIP upload - folders that hold ONLY videos (no image at all).

  grab(videos, out_dir)  ->  up to 10 frames taken at RANDOM moments of the videos, saved WITHOUT any compression
                             (lossless PNG, original video resolution). Frames with a clear human face are preferred;
                             if a folder has fewer good face frames, the sharpest other frames fill the 10.

How the frames are chosen
  1. random moments are sampled in rounds (spread over all videos of the folder, proportional to their length),
     each one is decoded small (fast) and analysed:  face (OpenCV Haar cascade + an eye check)  /  sharpness  /  not black
  2. frames that really show a face are shuffled and picked (not the same second twice)
  3. missing ones are filled with the sharpest non-face frames
  4. only the chosen moments are decoded again in FULL resolution and written as PNG (never JPEG, never resized).
     A PNG over the imgbb limit (32 MB, only 8K+ video) falls back to a near-lossless JPEG (q=1) for that frame.

Needs ffmpeg + ffprobe (installed in the Dockerfile). OpenCV (opencv-python-headless) is optional: without it the
frames are still random + sharp, only the face preference is off.
"""
import json
import logging
import os
import random
import shutil
import subprocess

log = logging.getLogger(__name__)

COUNT = 10                      # frames wanted per folder
IMGBB_MAX = 32 * 1024 * 1024    # imgbb limit per image
ROUND = 20                      # candidate moments analysed per round
MAX_CANDIDATES = 100           # hard limit of analysed moments per folder
ANALYSE_W = 640                 # width of the small copy that is analysed
MIN_FACE_H = 0.11               # face height / frame height (a small background face does not count)
MIN_SHARP = 40.0                # variance of the Laplacian below this = blurry
_CASC = None


class Cancelled(RuntimeError):
    pass


# ------------------------------------------------------------------ ffmpeg helpers
def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _probe(path: str) -> float:
    """Duration in seconds (0 when unknown)."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                           capture_output=True, text=True, timeout=90)
        return float((json.loads(r.stdout or "{}").get("format") or {}).get("duration") or 0)
    except Exception:
        return 0.0


def _small(path: str, t: float):
    """One frame at second t, small, as an OpenCV BGR image (None when the frame could not be decoded)."""
    try:
        import cv2
        import numpy as np
        r = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                            "-vf", f"scale={ANALYSE_W}:-2", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                           capture_output=True, timeout=90)
        if not r.stdout:
            return None
        return cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


def _full(path: str, t: float, dest_png: str) -> str:
    """The same moment in FULL resolution, lossless. Returns the written file ('' on failure)."""
    for _ in range(2):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                        "-c:v", "png", "-compression_level", "3", dest_png], capture_output=True, timeout=180)
        if os.path.exists(dest_png) and os.path.getsize(dest_png) > 0:
            break
    else:
        return ""
    if os.path.getsize(dest_png) > IMGBB_MAX:                    # only huge 8K frames: near-lossless JPEG instead
        jpg = dest_png[:-4] + ".jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                        "-q:v", "1", jpg], capture_output=True, timeout=180)
        os.remove(dest_png)
        return jpg if os.path.exists(jpg) and os.path.getsize(jpg) > 0 else ""
    return dest_png


# ------------------------------------------------------------------ analysis
def _cascades():
    """(face cascade, eye cascade) of OpenCV, or (None, None) when OpenCV / the XML files are missing."""
    global _CASC
    if _CASC is not None:
        return _CASC
    _CASC = (None, None)
    try:
        import cv2
        base = getattr(getattr(cv2, "data", None), "haarcascades", None) or os.path.join(os.path.dirname(cv2.__file__), "data")
        face = None
        for n in ("haarcascade_frontalface_alt2.xml", "haarcascade_frontalface_default.xml"):
            p = os.path.join(base, n)
            if os.path.exists(p):
                c = cv2.CascadeClassifier(p)
                if not c.empty():
                    face = c
                    break
        eye = None
        p = os.path.join(base, "haarcascade_eye.xml")
        if os.path.exists(p):
            c = cv2.CascadeClassifier(p)
            eye = c if not c.empty() else None
        _CASC = (face, eye)
        if not face:
            log.warning("OpenCV face cascade not found - frames are chosen without the face preference")
    except Exception as e:
        log.warning("OpenCV not available (%s) - frames are chosen without the face preference", e)
    return _CASC


def faces(img) -> float:
    """Height of the best real face in the picture / picture height (0 = no proper face). A face counts when it is
    big enough, sharp enough and has at least one eye inside it (removes most false hits of the cascade)."""
    face_c, eye_c = _cascades()
    if face_c is None:
        return 0.0
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    h, w = gray.shape[:2]
    found = face_c.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                    minSize=(int(h * MIN_FACE_H), int(h * MIN_FACE_H)))
    best = 0.0
    for (x, y, fw, fh) in found:
        roi = gray[y:y + fh, x:x + fw]
        if cv2.Laplacian(roi, cv2.CV_64F).var() < MIN_SHARP / 4:           # blurry face
            continue
        if eye_c is not None:
            top = roi[: int(fh * 0.62), :]
            if len(eye_c.detectMultiScale(top, scaleFactor=1.1, minNeighbors=5, minSize=(max(8, fw // 10),) * 2)) < 1:
                continue
        best = max(best, fh / h)
    return best


def analyse(img) -> dict:
    """{'face': 0..1, 'sharp': float, 'ok': bool}  - ok = not black / not white / not blurry."""
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mean = float(gray.mean())
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    ok = 22 < mean < 238 and sharp >= MIN_SHARP and float(gray.std()) > 12
    return {"face": faces(img) if ok else 0.0, "sharp": sharp, "ok": ok}


# ------------------------------------------------------------------ choosing the moments
def _moments(durs: list, n: int, rnd: random.Random) -> list:
    """n random (video index, second) pairs spread over the videos in proportion to their length."""
    live = [i for i, d in enumerate(durs) if d > 0]
    if not live:
        return [(i, 0.0) for i in range(len(durs))][:n]
    weights = [durs[i] for i in live]
    out = []
    for _ in range(n):
        i = rnd.choices(live, weights)[0]
        d = durs[i]
        lo, hi = (d * 0.04, d * 0.96) if d > 4 else (0.0, d * 0.9)       # skip fade-in / credits of real videos
        out.append((i, rnd.uniform(lo, max(lo, hi))))
    return out


def _spaced(chosen: list, cand: tuple, durs: list, count: int, strict: bool) -> bool:
    """False when cand is too close (same video) to an already chosen moment."""
    gap = max(2.0, durs[cand[0]] / (count * 2)) if strict else 0.4
    return all(not (c[0] == cand[0] and abs(c[1] - cand[1]) < gap) for c in chosen)


def pick(cands: list, durs: list, count: int, rnd: random.Random) -> list:
    """cands: [{'v', 't', 'face', 'sharp', 'ok'}]  ->  up to `count` of them. Face frames first (random order),
    then the sharpest other frames; the same second of a video is never taken twice."""
    chosen: list = []

    def take(pool, strict):
        for c in pool:
            if len(chosen) >= count:
                return
            m = (c["v"], c["t"])
            if _spaced([(x["v"], x["t"]) for x in chosen], m, durs, count, strict):
                chosen.append(c)

    face = [c for c in cands if c["face"] > 0]
    rnd.shuffle(face)                                                    # RANDOM among the real face frames
    take(face, True)
    rest = sorted([c for c in cands if c["face"] <= 0 and c["ok"]], key=lambda c: -c["sharp"])
    take(rest, True)
    if len(chosen) < count:                                              # short videos: allow closer moments
        take(face, False)
        take(rest, False)
    if len(chosen) < count:                                              # last resort: anything decodable
        take(sorted([c for c in cands if c not in chosen], key=lambda c: -c["sharp"]), False)
    return chosen


def grab(videos: list, out_dir: str, count: int = COUNT, cancel=None, seed=None) -> list:
    """videos: absolute paths. Writes frame_01.png ... into out_dir, in video / time order. Returns the paths."""
    if not available():
        raise RuntimeError("ffmpeg is not installed - cannot capture frames from videos")
    rnd = random.Random(seed)
    videos = [v for v in videos if os.path.exists(v)]
    if not videos:
        return []
    os.makedirs(out_dir, exist_ok=True)
    durs = [_probe(v) for v in videos]
    cands: list = []
    seen = set()
    # without a face detector there is nothing to search for: a few more moments than needed are enough
    limit = MAX_CANDIDATES if _cascades()[0] is not None else count * 3
    while len(cands) < limit:
        if cancel and cancel():
            raise Cancelled("stopped")
        fresh = 0
        for vi, t in _moments(durs, ROUND, rnd):
            key = (vi, round(t, 1))
            if key in seen:
                continue
            seen.add(key)
            fresh += 1
            img = _small(videos[vi], t)
            if img is None:
                continue
            a = analyse(img)
            cands.append({"v": vi, "t": t, **a})
        if fresh == 0:                                                   # short video: no new moment left
            break
        if sum(1 for c in cands if c["face"] > 0) >= count * 2:          # plenty of face frames to choose from
            break
    chosen = pick(cands, durs, count, rnd)
    chosen.sort(key=lambda c: (c["v"], c["t"]))                          # story order in the preview
    out = []
    for n, c in enumerate(chosen, 1):
        if cancel and cancel():
            raise Cancelled("stopped")
        p = _full(videos[c["v"]], c["t"], os.path.join(out_dir, f"frame_{n:02d}.png"))
        if p:
            out.append(p)
    log.info("frames: %s/%s captured (%s with a face, %s moments analysed)", len(out), count,
             sum(1 for c in chosen if c["face"] > 0), len(cands))
    return out
