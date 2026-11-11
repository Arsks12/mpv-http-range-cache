import argparse
import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import urllib.parse

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_LUA = os.path.join(REPO_ROOT, "main.lua")
sys.path.insert(0, REPO_ROOT)
import proxy  # noqa: E402


class RangeRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _serve_file(self, send_body: bool):
        st = self.server.state
        path = urllib.parse.urlsplit(self.path).path
        st["requests"].append((self.command, path, dict(self.headers)))

        if path == "/fail404" or (st.get("block_paths") and path in st["block_paths"]):
            self.send_response(404 if path == "/fail404" else 503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        data = st["files"].get(path)
        if data is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        total = len(data)
        rng = self.headers.get("Range")
        if rng and st.get("support_range", True):
            m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                s = int(m.group(1)) if m.group(1) else max(0, total - int(m.group(2)))
                e = min(int(m.group(2)) if m.group(2) else total - 1, total - 1)
                if s >= total or e < s:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                chunk = data[s : e + 1]
                st["bytes_sent"] += len(chunk)
                self.send_response(206)
                self.send_header("Content-Type", "video/x-matroska")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {s}-{e}/{total}")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                if send_body:
                    self.wfile.write(chunk)
                return

        st["bytes_sent"] += total
        self.send_response(200)
        self.send_header("Content-Type", "video/x-matroska")
        if st.get("support_range", True):
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(total))
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def do_HEAD(self):
        self._serve_file(False)

    def do_GET(self):
        self._serve_file(True)


class ThreadedHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def test_unit_helpers(tmp: str):
    assert proxy.merge_intervals([[5, 10], [0, 4], [20, 30], [15, 19]]) == [[0, 10], [15, 30]]
    assert proxy.total_cached_bytes([[0, 9], [20, 29]]) == 20
    assert proxy.plan_segments([[10, 19], [30, 39]], 0, 49) == [
        ("net", 0, 9), ("cache", 10, 19), ("net", 20, 29), ("cache", 30, 39), ("net", 40, 49)
    ]
    assert proxy.canonical_url("http://x/v.mkv?token=abc&id=1", {"token"}) == "http://x/v.mkv?id=1"
    assert proxy.extract_safe_dirname("http://x/a/b/CON.mkv") == "_CON.mkv"
    long_name = "a" * 130 + ".mkv"
    d = proxy.extract_safe_dirname(f"http://x/{long_name}")
    assert len(d) <= 110 and d.endswith(".mkv")

    cdir = os.path.join(tmp, "unit_cache")
    mgr = proxy.CacheManager(cdir, max_cache_mb=0, flush_kb=64, ignore_params=set())
    e1 = mgr.get_entry("http://host1/movie.mkv")
    e1.write_chunk(0, b"abc")
    e1.save_meta(force=True)
    e2 = mgr.get_entry("http://host2/movie.mkv")
    assert e1.entry_dir != e2.entry_dir
    assert os.path.basename(e1.entry_dir) == "movie.mkv"
    assert os.path.basename(e2.entry_dir).startswith("movie.mkv__")

    # Legacy flat-file migration test
    mig_dir = os.path.join(tmp, "mig_cache")
    os.makedirs(mig_dir, exist_ok=True)
    orig_url = "http://old/video.mkv"
    orig_key = proxy.url_cache_key(orig_url, set())
    alias_url = "http://mirror/video.mkv"
    alias_key = proxy.url_cache_key(alias_url, set())
    with open(os.path.join(mig_dir, f"{orig_key}.data"), "wb") as f:
        f.write(b"Z" * 1024)
    with open(os.path.join(mig_dir, f"{orig_key}.meta.json"), "w", encoding="utf-8") as f:
        json.dump({"key": orig_key, "url": orig_url, "total_size": 1024, "ranges": [[0, 1023]]}, f)
    with open(os.path.join(mig_dir, f"{orig_key}.watch_later"), "w", encoding="utf-8") as f:
        f.write("start=12.5\n")
    with open(os.path.join(mig_dir, f"{alias_key}.alias.json"), "w", encoding="utf-8") as f:
        json.dump({"target_key": orig_key, "url": alias_url}, f)

    mig_mgr = proxy.CacheManager(mig_dir, max_cache_mb=0, flush_kb=64, ignore_params=set())
    assert orig_key in mig_mgr.entries
    assert mig_mgr.aliases.get(alias_key) == orig_key
    me = mig_mgr.get_entry(alias_url)
    assert me.key == orig_key and me.head_hash == hashlib.sha256(b"Z" * 1024).hexdigest()
    assert not os.path.exists(os.path.join(mig_dir, f"{alias_key}.alias.json"))
    mig_mgr.flush_all()
    print("[PASS] unit helpers & migration")


def test_proxy_http_and_dedup(tmp: str):
    payload = os.urandom(200_000)
    up_state = {"files": {"/a/Test.Movie.mkv": payload, "/b/Mirror.mkv": payload}, "requests": [], "bytes_sent": 0, "support_range": True}
    up_srv = ThreadedHTTP(("127.0.0.1", 0), RangeRequestHandler)
    up_srv.state = up_state
    threading.Thread(target=up_srv.serve_forever, daemon=True).start()
    up_port = up_srv.server_address[1]

    cdir = os.path.join(tmp, "http_cache")
    wl_dir = os.path.join(tmp, "wl_http")
    mgr = proxy.CacheManager(cdir, max_cache_mb=0, flush_kb=64, ignore_params=set(), match_by_content=True)
    psrv = proxy.ProxyHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler, mgr, 32768)
    threading.Thread(target=psrv.serve_forever, daemon=True).start()
    pport = psrv.server_address[1]

    try:
        base = f"http://127.0.0.1:{pport}"
        url_a = f"http://127.0.0.1:{up_port}/a/Test.Movie.mkv"
        url_b = f"http://127.0.0.1:{up_port}/b/Mirror.mkv"

        assert requests.get(f"{base}/__health", timeout=5).json()["status"] == "ok"
        r_head = requests.head(f"{base}/stream", params={"url": url_a}, timeout=5)
        assert r_head.status_code == 200 and int(r_head.headers["Content-Length"]) == len(payload)

        r1 = requests.get(f"{base}/stream", params={"url": url_a}, headers={"Range": "bytes=0-", "X-Mpv-Cache-Timeout": "30"}, timeout=5)
        assert r1.status_code == 206 and r1.content == payload
        _, _, hdrs = up_state["requests"][-1]
        assert not any(k.lower().startswith("x-mpv-cache-") for k in hdrs)

        up_state["block_paths"] = {"/a/Test.Movie.mkv"}
        r_hit = requests.get(f"{base}/stream", params={"url": url_a}, headers={"Range": "bytes=1000-4999"}, timeout=5)
        assert r_hit.status_code == 206 and r_hit.content == payload[1000:5000]

        assert requests.post(f"{base}/__watch_later", params={"url": url_a, "wl_dir": wl_dir, "pos": "42.5", "reason": "quit"}, timeout=5).json()["saved"]
        r_b = requests.get(f"{base}/stream", params={"url": url_b}, headers={"Range": "bytes=0-"}, timeout=5)
        assert r_b.status_code == 206 and r_b.content == payload
        st_b = requests.get(f"{base}/__status", params={"url": url_b}, timeout=5).json()
        assert st_b["entries_count"] == 1 and st_b["current"]["cached_bytes"] == len(payload)

        wl_b = requests.get(f"{base}/__watch_later", params={"url": url_b, "wl_dir": wl_dir}, timeout=5).json()
        assert wl_b["resume"] is True and abs(wl_b["start"] - 42.5) < 0.01

        assert requests.get(f"{base}/stream", params={"url": f"http://127.0.0.1:{up_port}/fail404"}, timeout=5).status_code == 404
        assert requests.get(f"{base}/stream", params={"url": url_a}, headers={"Range": f"bytes={len(payload) + 10}-"}, timeout=5).status_code == 416

        assert requests.post(f"{base}/__clear", params={"url": url_b}, timeout=5).json()["cleared"] is True
        assert requests.post(f"{base}/__clear_all", timeout=5).json()["cleared_entries"] == 1
        assert requests.get(f"{base}/__status", timeout=5).json()["entries_count"] == 0
        print("[PASS] proxy HTTP, range, headers, dedup, watch_later, clear")
    finally:
        psrv.shutdown()
        psrv.server_close()
        mgr.flush_all()
        up_srv.shutdown()
        up_srv.server_close()


def test_mpv_integration(tmp: str):
    if not (shutil.which("mpv") and shutil.which("ffmpeg")):
        print("[SKIP] mpv integration (mpv or ffmpeg not installed)")
        return

    mkv_path = os.path.join(tmp, "sample.mkv")
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=15:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", mkv_path,
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    mkv_bytes = open(mkv_path, "rb").read()

    up_state = {"files": {"/cdn1/Demo.Video.2026.mkv": mkv_bytes, "/cdn2/Other.Name.mkv": mkv_bytes}, "requests": [], "bytes_sent": 0, "support_range": True}
    up_host = "127.0.0.2" if os.name == "nt" else "127.0.0.1"
    # Bind 127.0.0.2 on Windows so main.lua does not treat upstream as 127.0.0.1 loopback bypass
    up_srv = ThreadedHTTP((up_host, 0), RangeRequestHandler)
    up_srv.state = up_state
    threading.Thread(target=up_srv.serve_forever, daemon=True).start()
    up_port = up_srv.server_address[1]

    cdir = os.path.join(tmp, "mpv_cache")
    wl_dir = os.path.join(tmp, "mpv_wl")
    os.makedirs(cdir, exist_ok=True)
    os.makedirs(wl_dir, exist_ok=True)
    log1, log2, log3 = (os.path.join(tmp, f"mpv{i}.log") for i in (1, 2, 3))

    common_args = [
        "mpv", "--no-config", "--vo=null", "--ao=null", "--ytdl=no", "--untimed",
        "--force-window=no", "--osc=no", "--terminal=no", "--idle=no",
        "--cache=yes", "--demuxer-max-bytes=50MiB", "--save-position-on-quit=yes",
        f"--watch-later-directory={wl_dir}",
        f"--script={MAIN_LUA}",
        f"--script-opts=http_range_cache-cache_dir={cdir},http_range_cache-idle_timeout=30,http_range_cache-show_osd=no,http_range_cache-match_by_content=yes",
    ]

    try:
        url1 = f"http://{up_host}:{up_port}/cdn1/Demo.Video.2026.mkv"
        r1 = subprocess.run(common_args + ["--start=2", "--end=3.5", f"--log-file={log1}", url1], timeout=20)
        assert r1.returncode == 0
        txt1 = open(log1, "r", encoding="utf-8", errors="replace").read().lower()
        assert "stack traceback" not in txt1 and "lua error" not in txt1

        entry_dir = os.path.join(cdir, "Demo.Video.2026.mkv")
        assert os.path.isdir(entry_dir) and os.path.exists(os.path.join(entry_dir, "cache.data"))
        with open(os.path.join(entry_dir, "cache.meta.json"), "r", encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["total_size"] == len(mkv_bytes) and meta["cached_bytes"] > 0

        up_state["requests"].clear()
        up_state["bytes_sent"] = 0
        up_state["block_paths"] = {"/cdn1/Demo.Video.2026.mkv"}
        r2 = subprocess.run(common_args + ["--start=2", "--end=3.0", f"--log-file={log2}", url1], timeout=20)
        assert r2.returncode == 0 and up_state["bytes_sent"] == 0
        assert not [x for x in up_state["requests"] if x[1] == "/cdn1/Demo.Video.2026.mkv"]

        url2 = f"http://{up_host}:{up_port}/cdn2/Other.Name.mkv"
        r3 = subprocess.run(common_args + ["--end=2.5", f"--log-file={log3}", url2], timeout=20)
        assert r3.returncode == 0
        txt3 = open(log3, "r", encoding="utf-8", errors="replace").read().lower()
        assert "stack traceback" not in txt3 and "lua error" not in txt3
        assert not os.path.exists(os.path.join(cdir, "Other.Name.mkv"))
        print("[PASS] mpv integration: online cache, subfolder layout, offline replay, cross-URL dedup")
    finally:
        sfile = os.path.join(cdir, "server.json")
        if os.path.exists(sfile):
            try:
                pid = json.load(open(sfile, "r", encoding="utf-8"))["pid"]
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    os.kill(pid, 15)
            except Exception:
                pass
        up_srv.shutdown()
        up_srv.server_close()


def main():
    parser = argparse.ArgumentParser(description="Test suite for mpv-http-range-cache")
    parser.add_argument("--skip-mpv", action="store_true", help="Skip headless mpv/ffmpeg integration tests")
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="mpv_cache_test_")
    try:
        test_unit_helpers(tmp)
        test_proxy_http_and_dedup(tmp)
        if not args.skip_mpv:
            test_mpv_integration(tmp)
        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

