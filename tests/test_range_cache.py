import argparse
import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

import aiohttp
from aiohttp import web

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_LUA = os.path.join(REPO_ROOT, "main.lua")
sys.path.insert(0, REPO_ROOT)
import proxy  # noqa: E402


def make_upstream_app(state: dict) -> web.Application:
    async def handle(request: web.Request) -> web.Response:
        path = request.path
        state["requests"].append((request.method, path, dict(request.headers)))
        if path == "/fail404" or (state.get("block_paths") and path in state["block_paths"]):
            return web.Response(status=404 if path == "/fail404" else 503)
        data = state["files"].get(path)
        if data is None:
            return web.Response(status=404)

        total = len(data)
        rng = request.headers.get("Range")
        send_body = request.method != "HEAD"
        if rng and state.get("support_range", True):
            m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                s = int(m.group(1)) if m.group(1) else max(0, total - int(m.group(2)))
                e = min(int(m.group(2)) if m.group(2) else total - 1, total - 1)
                if s >= total or e < s:
                    return web.Response(status=416, headers={"Content-Range": f"bytes */{total}"})
                chunk = data[s : e + 1]
                state["bytes_sent"] += len(chunk)
                hdrs = {
                    "Content-Type": "video/x-matroska",
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {s}-{e}/{total}",
                    "Content-Length": str(len(chunk)),
                }
                return web.Response(status=206, headers=hdrs, body=chunk if send_body else b"")

        state["bytes_sent"] += total
        hdrs = {"Content-Type": "video/x-matroska", "Content-Length": str(total)}
        if state.get("support_range", True):
            hdrs["Accept-Ranges"] = "bytes"
        return web.Response(status=200, headers=hdrs, body=data if send_body else b"")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    return app


def test_unit_helpers(tmp: str):
    assert proxy.merge_intervals([[5, 10], [0, 4], [20, 30], [15, 19]]) == [[0, 10], [15, 30]]
    assert proxy.total_cached_bytes([[0, 9], [20, 29]]) == 20
    assert proxy.plan_segments([[10, 19], [30, 39]], 0, 49) == [
        ("net", 0, 9), ("cache", 10, 19), ("net", 20, 29), ("cache", 30, 39), ("net", 40, 49)
    ]
    assert proxy.canonical_url("http://x/v.mkv?token=abc&id=1", {"token"}) == "http://x/v.mkv?id=1"
    assert proxy.extract_safe_dirname("http://x/a/b/CON.mkv") == "_CON.mkv"
    d = proxy.extract_safe_dirname(f"http://x/{'a' * 130}.mkv")
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
    orig_url, alias_url = "http://old/video.mkv", "http://mirror/video.mkv"
    orig_key = proxy.url_cache_key(orig_url, set())
    alias_key = proxy.url_cache_key(alias_url, set())
    with open(os.path.join(mig_dir, f"{orig_key}.data"), "wb") as f:
        f.write(b"Z" * 1024)
    with open(os.path.join(mig_dir, f"{orig_key}.meta.json"), "w", encoding="utf-8") as f:
        import json
        json.dump({"key": orig_key, "url": orig_url, "total_size": 1024, "ranges": [[0, 1023]]}, f)
    with open(os.path.join(mig_dir, f"{orig_key}.watch_later"), "w", encoding="utf-8") as f:
        f.write("start=12.5\n")
    with open(os.path.join(mig_dir, f"{alias_key}.alias.json"), "w", encoding="utf-8") as f:
        import json
        json.dump({"target_key": orig_key, "url": alias_url}, f)

    mig_mgr = proxy.CacheManager(mig_dir, max_cache_mb=0, flush_kb=64, ignore_params=set())
    assert orig_key in mig_mgr.entries and mig_mgr.aliases.get(alias_key) == orig_key
    me = mig_mgr.get_entry(alias_url)
    assert me.key == orig_key and me.head_hash == hashlib.sha256(b"Z" * 1024).hexdigest()
    assert not os.path.exists(os.path.join(mig_dir, f"{alias_key}.alias.json"))
    mig_mgr.flush_all()
    print("[PASS] unit helpers & migration")


async def run_async_proxy_tests(tmp: str):
    payload = os.urandom(1_300_000)  # > 4 x 256 KB parts to verify parallel split downloading
    up_state = {"files": {"/a/Test.Movie.mkv": payload, "/b/Mirror.mkv": payload}, "requests": [], "bytes_sent": 0, "support_range": True}
    up_runner = web.AppRunner(make_upstream_app(up_state))
    await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 0)
    await up_site.start()
    up_port = up_site._server.sockets[0].getsockname()[1]

    cdir = os.path.join(tmp, "http_cache")
    wl_dir = os.path.join(tmp, "wl_http")
    mgr = proxy.CacheManager(cdir, max_cache_mb=0, flush_kb=64, ignore_params=set(), match_by_content=True)
    psrv = proxy.AsyncRangeProxy(mgr, chunk_size=32768, connections=4, part_size_kb=256)
    await psrv.start_session()
    p_runner = web.AppRunner(psrv.build_app())
    await p_runner.setup()
    p_site = web.TCPSite(p_runner, "127.0.0.1", 0)
    await p_site.start()
    pport = p_site._server.sockets[0].getsockname()[1]
    psrv.port = pport

    try:
        base = f"http://127.0.0.1:{pport}"
        url_a = f"http://127.0.0.1:{up_port}/a/Test.Movie.mkv"
        url_b = f"http://127.0.0.1:{up_port}/b/Mirror.mkv"

        async with aiohttp.ClientSession() as cli:
            async with cli.get(f"{base}/__health") as r:
                assert (await r.json())["status"] == "ok"

            async with cli.head(f"{base}/stream", params={"url": url_a}) as r:
                assert r.status == 200 and int(r.headers["Content-Length"]) == len(payload)

            # Clear range_reqs after HEAD so we specifically inspect parallel GET slices
            up_state["requests"].clear()
            async with cli.get(
                f"{base}/stream",
                params={"url": url_a},
                headers={"Range": "bytes=0-", "X-Mpv-Cache-Timeout": "30"},
            ) as r1:
                body1 = await r1.read()
                assert r1.status == 206 and body1 == payload

            get_reqs = [x for x in up_state["requests"] if x[0] == "GET" and x[1] == "/a/Test.Movie.mkv"]
            assert len(get_reqs) >= 4, f"Expected parallel multi-part Range fetches, got {len(get_reqs)}"
            assert all(not any(k.lower().startswith("x-mpv-cache-") for k in hdrs) for _, _, hdrs in get_reqs)

            # Offline cache hit (block upstream completely)
            up_state["block_paths"] = {"/a/Test.Movie.mkv"}
            async with cli.get(f"{base}/stream", params={"url": url_a}, headers={"Range": "bytes=1000-4999"}) as r_hit:
                assert r_hit.status == 206 and (await r_hit.read()) == payload[1000:5000]

            # Watch-later save + cross-URL content dedup + resume
            async with cli.post(f"{base}/__watch_later", params={"url": url_a, "wl_dir": wl_dir, "pos": "42.5", "reason": "quit"}) as rw:
                assert (await rw.json())["saved"] is True

            async with cli.get(f"{base}/stream", params={"url": url_b}, headers={"Range": "bytes=0-"}) as rb:
                assert rb.status == 206 and (await rb.read()) == payload

            async with cli.get(f"{base}/__status", params={"url": url_b}) as rs:
                st_b = await rs.json()
                assert st_b["entries_count"] == 1 and st_b["current"]["cached_bytes"] == len(payload)

            async with cli.get(f"{base}/__watch_later", params={"url": url_b, "wl_dir": wl_dir}) as rwl:
                wl_b = await rwl.json()
                assert wl_b["resume"] is True and abs(wl_b["start"] - 42.5) < 0.01

            # Concurrent health/status + stream requests complete cleanly without blocking event loop
            r_h, r_s, r_st = await asyncio.gather(
                cli.get(f"{base}/__health"),
                cli.get(f"{base}/__status", params={"url": url_a}),
                cli.get(f"{base}/stream", params={"url": url_a}, headers={"Range": "bytes=0-1023"}),
            )
            async with r_h, r_s, r_st:
                assert r_h.status == 200 and r_s.status == 200
                assert r_st.status == 206 and (await r_st.read()) == payload[:1024]

            async with cli.get(f"{base}/stream", params={"url": f"http://127.0.0.1:{up_port}/fail404"}) as r404:
                assert r404.status == 404
            async with cli.get(f"{base}/stream", params={"url": url_a}, headers={"Range": f"bytes={len(payload) + 10}-"}) as r416:
                assert r416.status == 416

            async with cli.post(f"{base}/__clear", params={"url": url_b}) as rc:
                assert (await rc.json())["cleared"] is True
            async with cli.post(f"{base}/__clear_all") as rca:
                assert (await rca.json())["cleared_entries"] == 1
            async with cli.get(f"{base}/__status") as rst:
                assert (await rst.json())["entries_count"] == 0

        print("[PASS] async aiohttp proxy: parallel multi-part Range, header strip, offline hit, dedup, watch_later, clear")
    finally:
        await psrv.close_session()
        await p_runner.cleanup()
        mgr.flush_all()
        await up_runner.cleanup()


def test_mpv_integration(tmp: str):
    if not (shutil.which("mpv") and shutil.which("ffmpeg")):
        print("[SKIP] mpv integration (mpv or ffmpeg not installed)")
        return

    import json, http.server, socketserver, threading

    class SyncRangeHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, fmt, *args): pass
        def _serve(self, body_flag: bool):
            st = self.server.state
            path = urllib.parse.urlsplit(self.path).path
            st["requests"].append((self.command, path))
            if st.get("block_paths") and path in st["block_paths"]:
                self.send_response(503); self.send_header("Content-Length", "0"); self.end_headers(); return
            data = st["files"].get(path)
            if data is None:
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
            total, rng = len(data), self.headers.get("Range")
            if rng:
                m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
                if m and (m.group(1) or m.group(2)):
                    s = int(m.group(1)) if m.group(1) else max(0, total - int(m.group(2)))
                    e = min(int(m.group(2)) if m.group(2) else total - 1, total - 1)
                    chunk = data[s : e + 1]
                    st["bytes_sent"] += len(chunk)
                    self.send_response(206)
                    self.send_header("Content-Type", "video/x-matroska")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {s}-{e}/{total}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.end_headers()
                    if body_flag:
                        self.wfile.write(chunk)
                    return
            st["bytes_sent"] += total
            self.send_response(200)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(total))
            self.end_headers()
            if body_flag:
                self.wfile.write(data)
        def do_HEAD(self): self._serve(False)
        def do_GET(self): self._serve(True)

    class ThreadedHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

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

    up_state = {"files": {"/cdn1/Demo.Video.2026.mkv": mkv_bytes, "/cdn2/Other.Name.mkv": mkv_bytes}, "requests": [], "bytes_sent": 0}
    up_host = "127.0.0.2" if os.name == "nt" else "127.0.0.1"
    up_srv = ThreadedHTTP((up_host, 0), SyncRangeHandler)
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
        f"--script-opts=http_range_cache-cache_dir={cdir},http_range_cache-idle_timeout=30,http_range_cache-show_osd=no,http_range_cache-match_by_content=yes,http_range_cache-connections=4,http_range_cache-part_size_kb=256",
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

        # Trigger quit-watch-later mid-playback to verify async detached end-file watch_later sync
        quit_lua = os.path.join(tmp, "quit_wl.lua")
        with open(quit_lua, "w", encoding="utf-8") as f:
            f.write("local mp = require('mp')\nmp.register_event('file-loaded', function() mp.add_timeout(0.4, function() mp.command('quit-watch-later') end) end)\n")
        r_wl = subprocess.run(
            common_args + ["--start=2.0", f"--script={quit_lua}", f"--log-file={tmp}/mpv_wl.log", url1],
            timeout=20,
        )
        assert r_wl.returncode == 0
        wl_backup = os.path.join(entry_dir, "cache.watch_later")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not os.path.exists(wl_backup):
            time.sleep(0.05)
        assert os.path.exists(wl_backup), "Expected async end-file watch_later backup on quit"

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
        asyncio.run(run_async_proxy_tests(tmp))
        if not args.skip_mpv:
            test_mpv_integration(tmp)
        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
