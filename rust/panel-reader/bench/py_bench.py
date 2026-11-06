"""Python side of the Rust experiment (reads live/ only, never writes there).

python py_bench.py dump  OUT.json     {file: panel_ocr.ocr_text} for every labeled crop (parity check input)
python py_bench.py bench [ROUNDS]     panel_ocr in-process: per-read p50/p95/max over all labeled crops
python py_bench.py http  [ROUNDS] [URL]  same reads through `panel-reader serve` (raw RGB body), keep-alive
                                      connection; also a short run with a new connection per read; checks
                                      every HTTP answer equals panel_ocr's.
"""
import http.client
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

LIVE = Path(__file__).resolve().parents[3] / "live"
sys.path.insert(0, str(LIVE))
import panel_ocr  # noqa: E402
from PIL import Image  # noqa: E402

labels = json.loads((LIVE / "out/dev/labels.json").read_text(encoding="utf-8"))


def images():
    return [(f, Image.open(LIVE / f).convert("RGB")) for f in labels]


def read(img):
    return panel_ocr.parse_panel(panel_ocr.ocr_text(img))


def stats(name, t, n_crops, rounds):
    t = sorted(t)
    pick = lambda q: t[round((len(t) - 1) * q)]  # noqa: E731
    print(f"{name}: {len(t)} reads ({n_crops} crops x {rounds})  p50 {pick(.5):.1f} us  p95 {pick(.95):.1f} us  "
          f"max {t[-1]:.1f} us  mean {sum(t) / len(t):.1f} us")


def bench(rounds):
    imgs = images()
    for _, im in imgs:
        read(im)
    t = []
    for _ in range(rounds):
        for _, im in imgs:
            s = time.perf_counter()
            read(im)
            t.append((time.perf_counter() - s) * 1e6)
    stats("python in-process", t, len(imgs), rounds)


def http_bench(rounds, url):
    u = urlparse(url)
    imgs = images()
    # what the helper would send: the panel crop as raw RGB (it already has the pixels; no PNG encode)
    bodies = []
    for f, im in imgs:
        if im.width > 1000:
            im = im.crop(panel_ocr.panel_box(im.width, im.height))
        bodies.append((f, im, im.tobytes(), f"/read?w={im.width}&h={im.height}"))

    def post(conn, body, path):
        conn.request("POST", path, body=body, headers={"Content-Type": "application/octet-stream"})
        r = conn.getresponse()
        return r.status, json.loads(r.read())

    conn = http.client.HTTPConnection(u.hostname, u.port)
    mism = 0
    for f, im, body, path in bodies:                     # warm-up + parity of the HTTP path
        st, got = post(conn, body, path)
        want = read(im)
        if want is not None:
            want = {k: want[k] for k in ("minerals", "gas", "used", "cap", "raw")}
        if st != 200 or got != want:
            mism += 1
            print("HTTP mismatch", f, st, got, want)
    print(f"http parity: {len(bodies) - mism}/{len(bodies)} identical to panel_ocr")
    t = []
    for _ in range(rounds):
        for _, _, body, path in bodies:
            s = time.perf_counter()
            post(conn, body, path)
            t.append((time.perf_counter() - s) * 1e6)
    conn.close()
    stats("rust via HTTP (keep-alive)", t, len(bodies), rounds)
    t = []
    for _, _, body, path in bodies:
        s = time.perf_counter()
        c = http.client.HTTPConnection(u.hostname, u.port)
        post(c, body, path)
        c.close()
        t.append((time.perf_counter() - s) * 1e6)
    stats("rust via HTTP (new connection each)", t, len(bodies), 1)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "dump":
        out = {f: panel_ocr.ocr_text(im) for f, im in images()}
        Path(sys.argv[2]).write_text(json.dumps(out, indent=1), encoding="utf-8")
        print(len(out), "crops")
    elif cmd == "bench":
        bench(int(sys.argv[2]) if len(sys.argv) > 2 else 5)
    elif cmd == "http":
        http_bench(int(sys.argv[2]) if len(sys.argv) > 2 else 20,
                   sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8766")


def stress_dump(out_dir):
    """stress.py's perturbed held-out crops (same seed and drawing), saved as PNG + {name: ocr_text}."""
    import random

    from PIL import ImageDraw
    sys.path.insert(0, str(LIVE / "out/dev"))
    import eval as E
    random.seed(1)
    _, test = E.split()
    out_dir = Path(out_dir)
    texts = {}
    for f in test:
        base = Image.open(LIVE / f).convert("RGB")
        for k in range(10):
            img = base.copy()
            d = ImageDraw.Draw(img)
            for _ in range(random.randint(1, 3)):
                x, y = random.randint(40, 520), random.randint(14, 40)
                kind = random.choice("lsbp")
                col = random.choice([(255, 255, 255), (235, 235, 220), (200, 220, 255), (250, 250, 250)])
                if kind == "l":
                    d.line((x, y, x + random.randint(-15, 15), y + random.randint(-20, 20)), fill=col, width=1)
                elif kind == "s":
                    d.rectangle((x, y, x + 1, y + 1), fill=col)
                elif kind == "b":
                    d.line((x, y, x + random.randint(-6, 6), y + random.randint(8, 16)), fill=col, width=3)
                else:
                    d.rectangle((x, y, x + random.randint(4, 20), y + random.randint(3, 12)), fill=col)
            name = f"{Path(f).stem}_{k}.png"
            img.save(out_dir / name)
            texts[name] = panel_ocr.ocr_text(img)
    (out_dir / "texts.json").write_text(json.dumps(texts), encoding="utf-8")
    print(len(texts), "stress crops")


if __name__ == "__main__" and sys.argv[1] == "stress":
    stress_dump(sys.argv[2])
