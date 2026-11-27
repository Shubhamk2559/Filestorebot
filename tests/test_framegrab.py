"""Offline test of the video-only folder frame capture (needs ffmpeg + OpenCV): python3 tests/test_framegrab.py
Checks: 10 lossless PNG frames at the ORIGINAL resolution, random moments, face frames preferred, never the same
second twice, short videos still give frames, no face detector = still works, cancel, pick() rules."""
import os
import random
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from utils import framegrab as FG  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def make_video(path, secs, size="1280x720"):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25",
                    "-t", str(secs), "-pix_fmt", "yuv420p", path], check=True)


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    return head[:8] == b"\x89PNG\r\n\x1a\n", int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


# ---- pick(): face frames first, random among them, spacing, fill with the sharpest
rnd = random.Random(1)
durs = [100.0]
cands = [{"v": 0, "t": float(t), "face": 0.3 if t % 10 == 0 else 0.0, "sharp": float(t), "ok": True} for t in range(1, 100)]
got = FG.pick(cands, durs, 10, rnd)
check(len(got) == 10, f"pick gives 10 ({len(got)})")
check(sum(1 for c in got if c["face"] > 0) == 9, "all 9 face moments taken first")           # t = 10..90
check(all(abs(a["t"] - b["t"]) >= 2 for i, a in enumerate(got) for b in got[i + 1:]), "no two moments closer than the gap")
got2 = FG.pick(cands, durs, 5, random.Random(2))
got3 = FG.pick(cands, durs, 5, random.Random(3))
check({c["t"] for c in got2} != {c["t"] for c in got3}, "different seeds -> different random faces")
check(len(FG.pick([], durs, 10, rnd)) == 0, "no candidates -> nothing")

if not FG.available():
    print("SKIPPED the ffmpeg part (ffmpeg / ffprobe not installed)")
else:
    with tempfile.TemporaryDirectory() as td:
        v1, v2, v3 = (os.path.join(td, n) for n in ("a.mp4", "b.mp4", "short.mp4"))
        make_video(v1, 30)
        make_video(v2, 20)
        make_video(v3, 3, "640x360")

        # ---- no face detector available (cascade missing) -> still 10 sharp random frames
        FG._CASC = (None, None)
        out = FG.grab([v1, v2], os.path.join(td, "o1"), 10)
        check(len(out) == 10, f"10 frames without detector ({len(out)})")
        ok = [png_size(p) for p in out]
        check(all(o[0] and o[1] == 1280 and o[2] == 720 for o in ok), f"lossless PNG at the original size {ok[:2]}")
        check(out == sorted(out), "files numbered in order")

        # ---- fake detector: only frames of the first video 'have a face'
        calls = {"faces": 0}
        real_faces = FG.faces

        def fake_faces(img):
            calls["faces"] += 1
            return 0.3 if calls["faces"] % 3 == 0 else 0.0               # every 3rd analysed frame is a "face"

        FG.faces = fake_faces
        a = FG.grab([v1, v2], os.path.join(td, "o2"), 10, seed=11)
        b = FG.grab([v1, v2], os.path.join(td, "o3"), 10, seed=12)
        check(len(a) == 10 and len(b) == 10, "10 frames with detector")
        check(open(a[0], "rb").read() != open(b[0], "rb").read() or open(a[5], "rb").read() != open(b[5], "rb").read(),
              "two runs pick different (random) moments")
        FG.faces = real_faces

        # ---- real faces() must never crash, even without a cascade file
        import cv2
        import numpy as np
        check(FG.faces(np.zeros((360, 640, 3), np.uint8)) == 0.0, "faces() on a black picture = 0")

        # ---- short video: still gives frames, never more than asked
        s = FG.grab([v3], os.path.join(td, "o4"), 10, seed=5)
        check(5 <= len(s) <= 10, f"short video gives frames ({len(s)})")

        # ---- missing / broken input
        check(FG.grab([os.path.join(td, "nope.mp4")], os.path.join(td, "o5")) == [], "missing video -> []")
        open(os.path.join(td, "bad.mp4"), "wb").write(b"not a video")
        check(FG.grab([os.path.join(td, "bad.mp4")], os.path.join(td, "o6")) == [], "broken video -> []")

        # ---- cancel
        try:
            FG.grab([v1], os.path.join(td, "o7"), 10, cancel=lambda: True)
            check(False, "cancel must raise")
        except FG.Cancelled:
            pass

print("FAILED:\n" + "\n".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
