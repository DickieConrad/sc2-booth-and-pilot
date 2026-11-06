"""/next/ (the Angular page's build) tests (stdlib; run by ci.sh via tools/checks.py tests). A temp build folder and a
throwaway server on an OS-picked port; the running helper (:8765) is never touched."""
import http.client
import http.server
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import timing  # noqa: E402


def make_dist(d):
    root = Path(d) / "dist"
    root.mkdir()
    (root / "index.html").write_text('<base href="/next/"><script src="main-ABC.js"></script>', encoding="utf-8")
    (root / "main-ABC.js").write_text("console.log(1)", encoding="utf-8")
    (root / "styles-X.css").write_text("body{}", encoding="utf-8")
    (root / ".env").write_text("secret", encoding="utf-8")
    (Path(d) / "outside.js").write_text("nope", encoding="utf-8")
    return root


def test_route(root):
    code, body, ctype, cache = timing.web_route("/next/", root)
    assert code == 200 and b'href="/next/"' in body and ctype.startswith("text/html") and cache == "no-store"
    assert timing.web_route("/next/index.html?x=1", root)[0] == 200
    code, body, ctype, cache = timing.web_route("/next/main-ABC.js", root)
    assert (code, body, ctype) == (200, b"console.log(1)", "application/javascript") and "immutable" in cache
    assert timing.web_route("/next/styles-X.css", root)[2] == "text/css"
    for bad in ("/next/../outside.js", "/next/..%2Foutside.js", "/next/a/../../outside.js", "/next/.env",
                "/next//etc/passwd.js", "/next/C:/x.js", "/next/missing.js", "/next/main-ABC.exe", "/next/\\..\\x.js"):
        assert timing.web_route(bad, root)[0] == 404, bad
    code, body, _, _ = timing.web_route("/next/", Path(root) / "nothing-here")
    assert code == 404 and b"npm run build" in body


def test_server(root):
    """The real Handler: /next/ is served, /next redirects, and the old routes are unchanged."""
    timing.WEB_DIST = root
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), timing.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    assert port not in (8765, 8766, 8767)

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        out = (r.status, r.getheader("Content-Type"), r.getheader("Location"), r.read())
        c.close()
        return out

    try:
        status, ctype, _, body = get("/")
        assert status == 200 and b"SC2 Live Calls" in body and b"<app-root" not in body   # the old page stays at /
        status, ctype, _, body = get("/next/")
        assert status == 200 and b'href="/next/"' in body
        assert get("/next/main-ABC.js")[:2] == (200, "application/javascript")
        assert get("/next")[0] == 301 and get("/next")[2] == "/next/"
        assert get("/next/../timing.py")[0] == 404
        status, ctype, _, body = get("/state")
        assert status == 200 and b'"in_game"' in body
        assert get("/nope")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        root = make_dist(d)
        test_route(root)
        test_server(root)
    print("test_web_route: ok")
