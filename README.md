<div align="center">
	<h1>mpv-http-range-cache</h1>
	<p>
		<strong>Watch once, scrub freely forever.</strong><br/>
		Persistent HTTP <code>206</code> byte-range disk cache &amp; cross-URL resume for <a href="https://mpv.io">mpv</a>.
	</p>
	<p>
		<a href="#install">Install</a> •
		<a href="#why-mpv-http-range-cache">Why?</a> •
		<a href="#how-it-works">How It Works</a> •
		<a href="#options">Options</a> •
		<a href="#commands">Commands</a> •
		<a href="#technical-architecture">Technical Deep Dive</a> •
		<a href="#faq">FAQ</a>
	</p>
</div>

---

## What It Does (In Plain English vs. Under the Hood)

| For Everyday Watching | For Technical / Power Users |
| --- | --- |
| **Never re-download what you already watched.** Seeking backward, closing `mpv`, or rewatching tomorrow plays cached parts straight from your SSD/HDD at zero bandwidth cost. | **Disjoint HTTP `206` interval cache.** Merges downloaded inclusive `[start, end]` intervals and splits new requests into ordered `cache` hits and `net` gap fetches with leading-error prefetching. |
| **Scrub to the end without wasting disk space.** Jumping 90 minutes into a 40 GB stream stores only the few megabytes you actually played. | **Native NTFS / POSIX sparse files.** Newly created `cache.data` files invoke `FSCTL_SET_SPARSE` (`0x000900C4`) via Win32 `DeviceIoControl`, so uncached holes occupy zero physical clusters. |
| **Expiring links & new mirrors keep your cache.** When a CDN link rotates or you switch hosts for the same file, playback and resume pick up right where you left off. | **Content-addressable alias matching.** Compares exact `total_size` plus a SHA-256 fingerprint of the first 64 KB (`HEAD_HASH_BYTES = 65536`), linking URL hashes cleanly via `alias_keys`. |
| **Clean, human-readable cache folders.** Every movie/video gets its own named folder (`Movie.2025.mkv/`) instead of cryptic hash soup. | **Collision-safe subfolder store & atomic metadata.** Uses sanitized path names (with `__<key8>` tie-breakers), atomic temp-file writes (`os.replace`), and automatic legacy flat-file migration. |
| **Works seamlessly with your existing VPN / HTTP proxy.** `mpv.conf` proxy and TLS settings continue protecting your remote requests automatically. | **Loopback proxy bypass + header tunneling.** Clears `file-local-options/http-proxy` for `127.0.0.1` traffic while forwarding upstream `http-proxy`, `tls-verify`, and `network-timeout` via `X-Mpv-Cache-*` headers. |

---

## Why `mpv-http-range-cache`?

By default, `mpv`'s built-in demuxer cache (`--cache=yes`, `--demuxer-max-bytes=...`) lives **only in RAM** and evaporates the instant you close the player—or as soon as older chunks get evicted during long streams.

With `mpv-http-range-cache`:

-   **Replay & backward seeks are instant** even days later or offline.
-   **Slow or capped connections stay usable**—you download every byte at most once.
-   **Signed / expiring CDN URLs don't waste gigabytes**—matching by `ignore_query_params` or `match_by_content=yes` reuses existing bytes AND keeps your `save-position-on-quit` timestamp intact.
-   **Your native `mpv` setup stays untouched**—`ytdl_hook`, `ffmpeg`, `osc` / `uosc`, subtitles, and `mpv.conf` network options keep working normally.

---

## Features

-   **Zero-hassle transparent hooking**: Intercepts `http://` and `https://` streams at `on_load` priority `50` (after `ytdl_hook` resolves extractors at priority `10`), and cleanly restores original `stream-open-filename`, `http-proxy`, and headers on `on_load_fail` (priority `5`).
-   **Hybrid cache + network range planner**: A single HTTP `Range` request spanning both cached and uncached gaps streams cached chunks straight from disk and fetches only missing upstream spans—no duplicate requests.
-   **Cross-URL `watch_later` synchronization**: Periodically snapshots playback timestamps (every 15s and synchronously on `end-file`) into `cache.watch_later` inside each video's subfolder and propagates state across mirror URL hashes so native `save-position-on-quit=yes` resumes seamlessly across URL changes.
-   **Smart protocol & media exclusions**: Leaves local files, loopback servers (`127.0.0.1`, `localhost`, `::1`), and adaptive manifests (`.m3u8`, `.mpd`, `.pls`, HLS/DASH/HTML MIME types) untouched, while falling back cleanly on HTTP `200`-only upstreams that ignore `Range`.
-   **Single-instance daemon lifecycle**: Spawns one background `proxy.py` process per `cache_dir` guarded by an exclusive `proxy.lock`, advertises its ephemeral loopback port in `server.json`, flushes & closes data handles when idle (`release_user`) so OS file explorers display accurate sizes, and exits automatically after `idle_timeout`.
-   **Optional LRU size budget (`max_cache_size_mb`)**: Automatically evicts least-recently-accessed cache folders (while protecting the currently streaming file) when a disk cap is configured.
-   **OSD feedback & bindings**: Displays cached percentages and resume notices via OSD, plus unbound commands for status inspection, clearing current/all entries, and toggling caching on the fly.

---

## How It Works

### 1. For Normal Users (3-Second Mental Model)

Think of `mpv-http-range-cache` as a tiny smart DVR sitting between `mpv` and the internet:

1. You open an online video in `mpv`.
2. Instead of `mpv` asking the server directly for every seek, it asks the local DVR running on your computer.
3. Anything the DVR hasn't saved yet is downloaded once (through your proxy/VPN if configured) and saved into a clean folder named after the video.
4. Next time you rewind, restart `mpv`, or even open a different link for the same file, the DVR hands `mpv` the saved parts immediately from disk.

### 2. Data & Control Flow

```text
  mpv player
     │
     ├─ on_load (priority 50, after ytdl_hook)
     │    ├─ local file / 127.0.0.1 / .m3u8 / .mpd ──▶ direct playback
     │    └─ http(s) media stream:
     │         • moves mpv's http-proxy ─▶ X-Mpv-Cache-Proxy header
     │         • sets file-local-options/http-proxy="" (bypass proxy on loopback)
     │         • rewrites stream-open-filename ─▶ http://127.0.0.1:<port>/stream?url=...
     ▼
  proxy.py (127.0.0.1:<ephemeral-port>, singleton via proxy.lock)
     ├─ GET /__watch_later  ──▶ restores newest position across mirror URLs
     ├─ GET /stream         ──▶ plan_segments(cached_intervals, req_start, req_end)
     │      ├─ "cache" span ──▶ pread from <cache_dir>/<file>/cache.data
     │      └─ "net" span   ──▶ upstream GET Range (via X-Mpv-Cache-Proxy)
     │                             └─ tee chunk ─▶ socket + sparse file + merge_intervals
     └─ POST /__watch_later ──▶ saves position on 15s tick & end-file (clears on EOF)
```

---

## Install

### Requirements

-   **mpv 0.33** or newer
-   **Python 3.8+** with [`requests`](https://pypi.org/project/requests/):

    ```sh
    pip install requests
    ```

-   **Optional**: `curl` in `PATH` (used by Lua for sub-10ms control calls; automatically falls back to `python` `urllib.request` if absent).

    ```powershell
    irm https://raw.githubusercontent.com/Arsks12/mpv-http-range-cache/HEAD/installers/windows.ps1 | iex
    ```

    > Installs into `MPV_CONFIG_DIR`, nearby `portable_config`, or `%APPDATA%\mpv`, backing up any previous version automatically before updating.

#### Linux & macOS

_Requires `curl` and `unzip`._

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Arsks12/mpv-http-range-cache/HEAD/installers/unix.sh)"
```

> Auto-detects `MPV_CONFIG_DIR`, Flatpak (`io.mpv.Mpv`), Snap (`mpv` / `mpv-wayland`), `~/.config/mpv` (`$XDG_CONFIG_HOME`), and macOS.

### Option B — Manual / Git Clone

1. Clone or extract this repository as `http-range-cache` inside your `mpv` `scripts/` folder ([mpv config locations](https://mpv.io/manual/master/#files)):

    ```sh
    git clone https://github.com/Arsks12/mpv-http-range-cache.git ~/.config/mpv/scripts/http-range-cache
    ```

    _(On Windows: `%APPDATA%\mpv\scripts\http-range-cache`.)_

2. Install Python dependencies:

    ```sh
    pip install -r ~/.config/mpv/scripts/http-range-cache/requirements.txt
    ```

3. Copy [`http_range_cache.conf`](http_range_cache.conf) to `<mpv-config>/script-opts/http_range_cache.conf` and adjust `cache_dir` if you want a dedicated drive/partition (for example `cache_dir=D:/mpv-cache`).

### Recommended `mpv.conf` Settings

```ini
# Enables cross-session and cross-URL playback resume
save-position-on-quit=yes

# Keep a healthy in-memory demuxer cache alongside the persistent disk cache
cache=yes
demuxer-max-bytes=2GiB

# Optional upstream proxy (automatically forwarded for uncached requests)
# http-proxy=http://user:pass@host:port
```

---

## Options

All options live in `<mpv-config>/script-opts/http_range_cache.conf` (documented template: [`http_range_cache.conf`](http_range_cache.conf)).

| Option | Type / Values | Default | What It Controls |
| --- | --- | --- | --- |
| `enabled` | `yes` / `no` | `yes` | Master switch at `mpv` startup (can also be toggled live with `toggle-cache`). |
| `cache_dir` | Path (`~~` supported) | `~~/cache/http-range-cache` | Root folder containing per-file cache directories, `server.json`, `proxy.lock`, and optional `proxy.log`. |
| `idle_timeout` | Seconds (`0` = never) | `300` | Auto-terminates `proxy.py` after N seconds with zero active HTTP requests. |
| `max_cache_size_mb` | Megabytes (`0` = unlimited) | `0` | Soft LRU disk cap; oldest-accessed entries are deleted while protecting the active stream. |
| `chunk_size` | Bytes | `65536` | Read/write block size (`64 KB` default, minimum enforced `8192` bytes). |
| `flush_interval_kb` | Kilobytes | `1024` | Writes `cache.meta.json` every N KB downloaded (also flushed whenever a connection finishes). |
| `ignore_query_params` | Comma list | _(empty)_ | URL query keys stripped before key hashing (e.g. `token,sig,expires`). |
| `match_by_content` | `yes` / `no` | `yes` | Deduplicates distinct URLs hosting identical files via `total_size` + 64 KB SHA-256 head hash. |
| `python_path` | Path / `auto` | `auto` | Explicit interpreter path or `auto` probe order (`pythonw` -> `python` -> `python3` on Windows; `python3` -> `python` elsewhere). |
| `show_osd` | `yes` / `no` | `yes` | Displays load cache percentage, cross-URL resume messages, and command notifications. |
| `debug_log` | `yes` / `no` | `no` | Redirects proxy stdout/stderr to `<cache_dir>/proxy.log` for troubleshooting. |

---

## Commands

All commands are unbound by default. Bind them in `input.conf` using `script-binding http-range-cache/{command}` or `script-message {message}`:

```ini
# Bind keys in input.conf
alt+c  script-binding http-range-cache/cache-status
alt+x  script-binding http-range-cache/clear-current-cache
alt+X  script-binding http-range-cache/clear-all-cache
alt+t  script-binding http-range-cache/toggle-cache
```

| Binding (`script-binding`) | Script Message (`script-message`) | Action |
| --- | --- | --- |
| `http-range-cache/cache-status` | `http-range-cache-status` | OSD readout of current file cached bytes / total / percentage / disjoint range count, plus total disk usage across all cached files. |
| `http-range-cache/clear-current-cache` | `http-range-cache-clear-current` | Deletes the active (or last proxied) stream's cache subfolder and aliases cleanly. |
| `http-range-cache/clear-all-cache` | `http-range-cache-clear-all` | Removes every cached stream folder in `cache_dir` (while protecting proxy control files). |
| `http-range-cache/toggle-cache` | `http-range-cache-toggle` | Enables/disables caching for subsequent file loads without restarting `mpv`. |

### [uosc](https://github.com/tomasklaen/uosc) Context Menu Integration

Add `#!` comments in `input.conf` to expose cache controls directly inside `uosc`'s right-click menu:

```ini
alt+c  script-binding http-range-cache/cache-status         #! Cache > Status
alt+t  script-binding http-range-cache/toggle-cache         #! Cache > Toggle range cache
#      script-binding http-range-cache/clear-current-cache  #! Cache > Clear current stream
#      script-binding http-range-cache/clear-all-cache      #! Cache > Clear all cache
```

---

## Technical Architecture

### Hook Ordering & Safe Fallback (`main.lua`)

-   `on_load` runs at **priority `50`**, intentionally after `ytdl_hook` (priority `10`), so resolved direct media URLs from `yt-dlp` get cached when they point to HTTP/HTTPS streams—while preserving `mpv`'s `file-local-options/http-header-fields`.
-   Any incoming `X-Mpv-Cache-*` headers are sanitized before setting `X-Mpv-Cache-Proxy`, `X-Mpv-Cache-Tls-Verify`, and `X-Mpv-Cache-Timeout`.
-   `on_load_fail` runs at **priority `5`** (earlier than `ytdl_hook` at `10`), restoring `stream-open-filename`, `file-local-options/http-proxy`, and `http-header-fields` if local proxy startup or upstream connection fails before playback begins.

### Interval Merging, Segment Planning & Prefetching (`proxy.py`)

-   Cached spans are stored as sorted disjoint inclusive intervals `[[start, end], ...]`. Every write merges touching/overlapping spans in $O(k)$ via `merge_intervals`.
-   For an incoming client request `[req_start, req_end]`, `plan_segments` emits a minimal alternating sequence of `("cache", s, e)` and `("net", s, e)` segments.
-   If `segments[0]` requires network (`"net"`), the proxy **opens the upstream connection before committing client response headers**, allowing genuine HTTP errors (`403`, `404`, `502`) to propagate cleanly instead of truncating a fake `200`/`206` body.
-   Incoming client disconnects (`BrokenPipeError`, `ConnectionResetError`, `ConnectionAbortedError`, `OSError`) are trapped gracefully without corrupting already-written upstream chunks.
-   Upstream requests force `Accept-Encoding: identity` and strip hop-by-hop headers (`HOP_BY_HOP_HEADERS`) + internal `X-Mpv-Cache-*` headers so byte offsets on disk always stay 1:1 with the wire stream.

### Content Deduplication & `watch_later` Sync

1. On a fresh `bytes=0-` (offset `0`) fetch where another cached entry already has the same `total_size` and cached bytes covering `[0, min(65536, total_size) - 1]`, `proxy.py` buffers the first 64 KB from the upstream stream, hashes it with SHA-256 (`head_hash`), and scans matching size candidates.
2. On a match, `link_alias` closes the redundant upstream stream immediately, binds the new URL hash to the existing entry's `alias_keys`, records the URL in `known_urls` (capped at 30 recent URLs), and begins serving cached segments from disk immediately.
3. For playback resume, `mpv` natively hashes the *rewritten or original path* differently across URLs; [`main.lua`](file:///c:/Users/Arif/AppData/Roaming/mpv/scripts/http-range-cache/main.lua) queries `GET /__watch_later` on `file-loaded` and posts updates to `POST /__watch_later` every 15s and on `end-file` (clearing `cache.watch_later` when `reason == "eof"`). If another mirror URL has a `watch_later` file >1s newer than the current one, the proxy writes the synced MD5 `watch_later` file and returns `start` + `sid`/`aid`/`sub-delay`/`audio-delay` properties to seek seamlessly.

### Internal Proxy HTTP Endpoints

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/__health` | `GET`, `HEAD` | Returns `{"status": "ok", "pid": ..., "port": ...}` for readiness probes. |
| `/__status?url=...` | `GET` | Global stats (`entries_count`, `total_cached_bytes`, `cache_dir`) + `current` entry metadata. |
| `/__watch_later?url=...&wl_dir=...` | `GET` | Resolves newest saved resume state across `cache.watch_later` and `known_urls`. |
| `/__watch_later?url=...&wl_dir=...&pos=...&reason=...` | `POST` | Persists `cache.watch_later` and mirrors to existing `watch_later` files (deletes backup on `eof`). |
| `/__clear?url=...` | `POST` | Invalidates & deletes the entry subfolder and all aliases for `url`. |
| `/__clear_all` | `POST` | Purges all cached subfolders and legacy files in `cache_dir`. |
| `/stream?url=...` | `GET`, `HEAD` | Serves cached/upstream byte ranges with full `Content-Range` / `416` / `200`-fallback support. |

---

## Cache Folder Layout

```text
<cache_dir>/
├── Movie.Name.2025.1080p.mkv/
│   ├── cache.data          # Sparse binary file containing only downloaded intervals
│   ├── cache.meta.json     # Key, URLs, alias_keys, total_size, head_hash, intervals, timestamps
│   └── cache.watch_later   # Latest synced watch_later snapshot for cross-URL resume
├── Another.Movie.mkv__a1b2c3d4/
│   └── ...                 # Auto-suffixed with 8-char SHA-256 prefix on filename collision
├── proxy.lock              # OS file lock enforcing one proxy process per cache_dir
├── server.json             # {"pid": ..., "port": ..., "started_at": ...}
└── proxy.log               # Debug output when debug_log=yes
```

<details>
<summary>Example <code>cache.meta.json</code> schema</summary>

```json
{
  "key": "f09051468bef30c0d630df0bdaf29d3f4045cb2c115dab759f6f2d7f80b7ed68",
  "url": "https://cdn-mirror-2.example.com/videos/Movie.Name.2025.1080p.mkv",
  "total_size": 4831838208,
  "accept_ranges": true,
  "content_type": "video/x-matroska",
  "etag": "\"66a1-4b2\"",
  "last_modified": "Wed, 01 Oct 2025 12:00:00 GMT",
  "head_hash": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "known_urls": [
    "https://cdn-1.example.com/Movie.Name.2025.1080p.mkv",
    "https://cdn-mirror-2.example.com/videos/Movie.Name.2025.1080p.mkv"
  ],
  "alias_keys": ["e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"],
  "ranges": [[0, 67108863], [1073741824, 1140850687]],
  "cached_bytes": 134217728,
  "created_at": 1762880000.12,
  "last_access": 1762881205.45
}
```

</details>

---

## Tests & Contributing

Run the full test suite (unit helpers, legacy flat-file migration, mock HTTP range/dedup/`watch_later` server, plus end-to-end headless `mpv` online/offline/cross-URL tests):

```sh
python tests/test_range_cache.py
```

Skip `mpv` integration tests when running in headless CI without `mpv`/`ffmpeg`:

```sh
python tests/test_range_cache.py --skip-mpv
```

Contributions are welcome:
-   Follow formatting rules in [`.editorconfig`](.editorconfig) (UTF-8, LF, tabs in `.lua`/`.sh`/`.ps1`, 4 spaces in `.py`).
-   Ensure `python -m py_compile proxy.py tests/test_range_cache.py` and `python tests/test_range_cache.py` pass before opening a pull request.

---

## FAQ

#### Why does `cache.data` sometimes show `0 KB` in Windows Explorer while playing?

Two things happen on Windows:
1. Windows Explorer caches directory entry sizes while a write handle remains open. Once `mpv` closes connections (`_active_users == 0`), [`proxy.py`](proxy.py) flushes buffers and closes `cache.data`, and Explorer immediately reflects the true byte span.
2. `cache.data` is marked **sparse** (`FSCTL_SET_SPARSE`), so *Size* reflects the highest written byte offset while *Size on disk* reflects only actual cached chunks.

#### Why does this use Python + `requests` instead of pure `mpv` Lua?

`mpv`'s embedded Lua sandbox has no non-blocking HTTP server sockets, thread-pooled upstream connections, or Win32 `DeviceIoControl` sparse-file calls. Offloading HTTP range splicing and sparse disk I/O to a tiny localhost process (`proxy.py`) lets `mpv` keep using its native `ffmpeg` demuxer cache and fast seeks untouched.

#### What happens if `http-proxy` is set in my `mpv.conf`?

Everything keeps working as expected. [`main.lua`](main.lua) copies your configured `http-proxy` into `X-Mpv-Cache-Proxy`, clears `file-local-options/http-proxy` so `mpv` talks directly to `127.0.0.1` (avoiding proxy loops or corporate proxy blocks on loopback), and [`proxy.py`](proxy.py) sends all uncached upstream requests through your proxy after stripping `X-Mpv-Cache-*`.

#### Do HLS (`.m3u8`), DASH (`.mpd`), or non-range HTTP `200` streams break?

No:
-   Playlist/adaptive manifests (`.m3u8`, `.m3u`, `.mpd`, `.pls`, HLS/DASH/HTML responses) are automatically bypassed so live streams play normally.
-   Servers returning full HTTP `200` without `Range` support still cache bytes sequentially from offset `0` (`skip_leading` handles offset requests while writing received chunks).

## License

[MIT](LICENSE)
