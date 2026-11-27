"""Offline test of the ZIP helpers (no Telegram / MongoDB needed): python3 tests/test_zipscan.py"""
import json
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from utils import zipscan as ZS  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def make_zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)


with tempfile.TemporaryDirectory() as td:
    # 1) normal ZIP, with a wrapper folder, natural order, ignored files, hidden junk
    zp = os.path.join(td, "a.zip")
    make_zip(zp, {
        "Pack/Wedding Uploaded ✅/img10.jpg": b"1", "Pack/Wedding Uploaded ✅/img2.jpg": b"1",
        "Pack/Wedding Uploaded ✅/clip.mp4": b"v", "Pack/Wedding Uploaded ✅/notes.txt": b"t",
        "Pack/Wedding Uploaded ✅/sub/deep.PNG": b"1",
        "Pack/Puja/a.webp": b"1", "Pack/Puja/b.mkv": b"v",
        "Pack/Empty/readme.txt": b"x", "__MACOSX/Pack/._x.jpg": b"x", "Pack/.DS_Store": b"x",
    })
    out = os.path.join(td, "out")
    n = ZS.extract(zp, out)
    check(n == 10, f"extract count {n}")
    fs = ZS.scan_dir(out, "pack")
    names = [f["name"] for f in fs]
    check(names == ["Empty", "Puja", "Wedding"], f"folder names {names}")
    w = [f for f in fs if f["name"] == "Wedding"][0]
    check([os.path.basename(p) for p in w["images"]] == ["deep.PNG", "img2.jpg", "img10.jpg"] or
          [os.path.basename(p) for p in w["images"]] == ["img2.jpg", "img10.jpg", "deep.PNG"],
          f"order {w['images']}")
    check(w["images"].index(next(p for p in w["images"] if p.endswith("img2.jpg"))) <
          w["images"].index(next(p for p in w["images"] if p.endswith("img10.jpg"))), "natural sort")
    check(len(w["videos"]) == 1 and w["ignored"] == 1, "videos / ignored")
    check(all(os.path.exists(os.path.join(out, p)) for p in w["images"] + w["videos"]), "paths relative to root")
    check([f for f in fs if f["name"] == "Empty"][0]["images"] == [], "folder without image kept for reporting")

    # 2) single folder ZIP (no wrapper) + loose files in the root
    zp2 = os.path.join(td, "b.zip")
    make_zip(zp2, {"Solo/1.jpg": b"1", "Solo/2.jpg": b"1", "loose.jpg": b"1"})
    out2 = os.path.join(td, "out2")
    ZS.extract(zp2, out2)
    fs2 = ZS.scan_dir(out2, "MyZip")
    check(sorted(f["name"] for f in fs2) == ["MyZip", "Solo"], f"loose + single {[f['name'] for f in fs2]}")

    # 3) path traversal is blocked
    zp3 = os.path.join(td, "c.zip")
    make_zip(zp3, {"../evil.jpg": b"x", "ok/1.jpg": b"1"})
    out3 = os.path.join(td, "out3")
    ZS.extract(zp3, out3)
    check(not os.path.exists(os.path.join(td, "evil.jpg")), "path traversal")
    check(os.path.exists(os.path.join(out3, "ok", "1.jpg")), "normal file still extracted")

    # 4) not a ZIP
    bad = os.path.join(td, "d.zip")
    open(bad, "wb").write(b"Rar!")
    try:
        ZS.extract(bad, os.path.join(td, "o4"))
        check(False, "bad zip must raise")
    except RuntimeError as e:
        check("not a valid ZIP" in str(e), "bad zip message")

# 5) Telegraph chunking: every page stays below the limit, nothing is lost
urls = [f"https://i.ibb.co/abc{i:05d}/some-long-original-file-name-{i}.jpg" for i in range(2000)]
pages = ZS.chunk_nodes(urls)
check(len(pages) > 1, "big folder is split")
check(sum(len(p) for p in pages) == 2000, "no image lost")
check(all(len(json.dumps(p).encode()) < 60000 for p in pages), "page size")
check(len(ZS.chunk_nodes(urls[:50])) == 1, "small folder = one article")

# 6) clean_name
check(ZS.clean_name("Wedding Uploaded ✅") == "Wedding", "clean_name")
check(ZS.clean_name("My  Folder") == "My Folder", "clean_name spaces")

print("FAILED:\n" + "\n".join(fails) if fails else "ALL OK")
sys.exit(1 if fails else 0)
