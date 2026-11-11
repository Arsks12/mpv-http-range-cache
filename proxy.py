#!/usr/bin/env python3
"""Local HTTP 206 Range-Caching Proxy for mpv."""

import argparse
import hashlib
import http.server
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if os.name == "nt":
    import ctypes
    import msvcrt
else:
    import fcntl

FSCTL_SET_SPARSE = 0x000900C4
HEAD_HASH_BYTES = 65536
DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError)

HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "accept-encoding",
    "range", "if-range", "if-modified-since", "if-none-match", "icy-metadata",
}

NON_CACHEABLE_CONTENT_TYPES = (
    "text/html", "application/xhtml+xml", "application/vnd.apple.mpegurl",
    "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl", "application/dash+xml",
)

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

RANGE_HEADER_RE = re.compile(r"^bytes=(\d*)-(\d*)$", re.IGNORECASE)
CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def _atomic_write(path: str, content: str, make_dir: bool = False) -> bool:
    tmp = f"{path}.{threading.get_ident()}.tmp"
    try:
        if make_dir:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def _atomic_write_json(path: str, payload: dict, make_dir: bool = False) -> bool:
    return _atomic_write(path, json.dumps(payload), make_dir=make_dir)


def set_sparse_file(file_obj) -> None:
    """Mark a newly created NTFS file sparse so skipped seeks don't zero-fill."""
    if os.name != "nt":
        return
    try:
        handle = msvcrt.get_osfhandle(file_obj.fileno())
        bytes_ret = ctypes.c_ulong(0)
        ctypes.windll.kernel32.DeviceIoControl(
            handle, FSCTL_SET_SPARSE, None, 0, None, 0, ctypes.byref(bytes_ret), None
        )
    except Exception:
        pass


def merge_intervals(intervals: List[List[int]]) -> List[List[int]]:
    """Merge overlapping or adjacent inclusive [start, end] byte intervals."""
    pts = sorted((int(a), int(b)) for a, b in intervals if a <= b)
    if not pts:
        return []
    merged: List[List[int]] = [[pts[0][0], pts[0][1]]]
    for s, e in pts[1:]:
        if s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def total_cached_bytes(intervals: List[List[int]]) -> int:
    return sum(e - s + 1 for s, e in intervals)


def plan_segments(intervals: List[List[int]], req_start: int, req_end: int) -> List[Tuple[str, int, int]]:
    """Split [req_start, req_end] into ordered ('cache'|'net', start, end) segments."""
    plan: List[Tuple[str, int, int]] = []
    pos = req_start
    for iv_s, iv_e in intervals:
        if iv_e < pos:
            continue
        if iv_s > req_end:
            break
        if iv_s > pos:
            net_end = min(req_end, iv_s - 1)
            plan.append(("net", pos, net_end))
            pos = net_end + 1
            if pos > req_end:
                break
        cache_end = min(req_end, iv_e)
        if cache_end >= pos:
            plan.append(("cache", pos, cache_end))
            pos = cache_end + 1
            if pos > req_end:
                break
    if pos <= req_end:
        plan.append(("net", pos, req_end))
    return plan


def canonical_url(url: str, ignore_params: set) -> str:
    """Strip configured volatile query parameters from URL before hashing."""
    if not ignore_params:
        return url
    p = urllib.parse.urlsplit(url)
    if not p.query:
        return url
    filtered = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if k not in ignore_params]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(filtered, doseq=True), p.fragment))


def url_cache_key(url: str, ignore_params: set) -> str:
    return hashlib.sha256(canonical_url(url, ignore_params).encode("utf-8")).hexdigest()


def extract_safe_dirname(url: str) -> str:
    """Derive a clean, Windows-safe folder name from the URL filename."""
    path = urllib.parse.urlsplit(url).path
    raw = urllib.parse.unquote(os.path.basename(path.rstrip("/"))) if path else ""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw).strip().rstrip(". ")
    if not cleaned or cleaned in (".", ".."):
        cleaned = "stream"
    stem, ext = os.path.splitext(cleaned)
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
        stem, ext = os.path.splitext(cleaned)
    if len(cleaned) > 110:
        cleaned = (stem[: 110 - len(ext)].rstrip(". ") + ext) if len(ext) <= 16 else cleaned[:110].rstrip(". ")
    return cleaned or "stream"


def pick_entry_dir(cache_dir: str, url: str, key: str, entries: Dict[str, "CacheEntry"]) -> str:
    base = extract_safe_dirname(url)
    candidate = os.path.join(cache_dir, base)
    used_dirs = {os.path.normcase(e.entry_dir) for e in entries.values()}
    if os.path.normcase(candidate) not in used_dirs:
        meta = _read_json(os.path.join(candidate, "cache.meta.json"))
        if not os.path.exists(candidate) or (meta and meta.get("key") == key):
            return candidate
    return os.path.join(cache_dir, f"{base}__{key[:8]}")


def watch_later_hash(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest().upper()


def parse_watch_later(text: str) -> Tuple[Optional[float], dict]:
    start_val: Optional[float] = None
    props: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = (part.strip() for part in line.split("=", 1))
        if k == "start":
            try:
                start_val = float(v)
            except ValueError:
                pass
        else:
            props[k] = v
    return start_val, props


def parse_content_range(cr: str) -> Optional[Tuple[int, int, Optional[int]]]:
    m = CONTENT_RANGE_RE.match((cr or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), (int(m.group(3)) if m.group(3) != "*" else None)


def parse_range_header(header_val: Optional[str], total_size: Optional[int]) -> Tuple[bool, Optional[int], Optional[int]]:
    """Parse HTTP Range -> (has_range, start, end). Raises ValueError on 416."""
    full_end = total_size - 1 if total_size is not None else None
    if not header_val:
        return (False, 0, full_end)
    m = RANGE_HEADER_RE.match(header_val.strip())
    if not m:
        return (False, 0, full_end)
    start_s, end_s = m.group(1), m.group(2)
    if not start_s and not end_s:
        raise ValueError("Empty range")
    if not start_s:
        if total_size is None:
            return (True, None, None)
        suffix_len = int(end_s)
        if suffix_len <= 0:
            raise ValueError("Zero suffix range")
        return (True, max(0, total_size - suffix_len), total_size - 1)
    start = int(start_s)
    end = int(end_s) if end_s else None
    if total_size is not None:
        if start >= total_size:
            raise ValueError("Range start >= total_size")
        end = total_size - 1 if (end is None or end >= total_size) else end
    if end is not None and end < start:
        raise ValueError("Range end < start")
    return (True, start, end)


def is_non_cacheable_ct(content_type: str) -> bool:
    return (content_type or "").split(";")[0].strip().lower() in NON_CACHEABLE_CONTENT_TYPES


class CacheEntry:
    """Thread-safe sparse-file cache entry stored in a single URL subfolder."""

    def __init__(self, key: str, url: str, entry_dir: str, flush_bytes: int):
        self.key = key
        self.url = url
        self.entry_dir = entry_dir
        self.flush_bytes = flush_bytes
        self.data_path = os.path.join(entry_dir, "cache.data")
        self.meta_path = os.path.join(entry_dir, "cache.meta.json")
        self.watch_later_path = os.path.join(entry_dir, "cache.watch_later")
        self.lock = threading.RLock()

        self.total_size: Optional[int] = None
        self.accept_ranges: bool = False
        self.content_type: str = "application/octet-stream"
        self.etag: Optional[str] = None
        self.last_modified: Optional[str] = None
        self.head_hash: Optional[str] = None
        self.known_urls: List[str] = [url] if url else []
        self.alias_keys: List[str] = []
        self.ranges: List[List[int]] = []
        self.created_at: float = time.time()
        self.last_access: float = self.created_at

        self._unflushed_bytes = 0
        self._dirty_meta = False
        self._data_file = None
        self._active_users = 0
        self._load_meta()

    def _load_meta(self) -> None:
        if not os.path.exists(self.data_path):
            return
        meta = _read_json(self.meta_path)
        if not meta:
            return
        try:
            self.key = meta.get("key") or self.key
            self.total_size = meta.get("total_size")
            self.accept_ranges = bool(meta.get("accept_ranges", False))
            self.content_type = meta.get("content_type") or self.content_type
            self.etag = meta.get("etag")
            self.last_modified = meta.get("last_modified")
            self.head_hash = meta.get("head_hash")
            ku = meta.get("known_urls")
            if isinstance(ku, list):
                self.known_urls = [u for u in ku if isinstance(u, str) and u]
            if self.url and self.url not in self.known_urls:
                self.known_urls.append(self.url)
            ak = meta.get("alias_keys")
            if isinstance(ak, list):
                self.alias_keys = [k for k in ak if isinstance(k, str) and k]
            self.ranges = merge_intervals(meta.get("ranges", []))
            self.created_at = float(meta.get("created_at", self.created_at))
            self.last_access = float(meta.get("last_access", self.last_access))
        except Exception:
            self.ranges = []
            self.total_size = None

    def add_alias_key(self, alias_key: str) -> None:
        if not alias_key or alias_key == self.key:
            return
        with self.lock:
            if alias_key not in self.alias_keys:
                self.alias_keys.append(alias_key)
                self._dirty_meta = True

    def add_known_url(self, u: str) -> None:
        if not u:
            return
        with self.lock:
            if u not in self.known_urls:
                self.known_urls = (self.known_urls + [u])[-30:]
                self._dirty_meta = True

    def _ensure_data_file(self):
        if self._data_file is not None and not self._data_file.closed:
            return self._data_file
        os.makedirs(self.entry_dir, exist_ok=True)
        exists = os.path.exists(self.data_path)
        f = open(self.data_path, "r+b" if exists else "w+b")
        if not exists:
            set_sparse_file(f)
        self._data_file = f
        return f

    def acquire_user(self) -> None:
        with self.lock:
            self._active_users += 1

    def release_user(self) -> None:
        with self.lock:
            self._active_users = max(0, self._active_users - 1)
            if self._active_users == 0:
                if self.head_hash is None and self.total_size and self.ranges:
                    self.get_or_compute_head_hash()
                self.save_meta(force=True)
                self.close_file()

    def _has_head_bytes(self) -> Optional[int]:
        if not self.total_size or self.total_size <= 0 or not self.ranges:
            return None
        need_len = min(HEAD_HASH_BYTES, self.total_size)
        return need_len if (self.ranges[0][0] == 0 and self.ranges[0][1] >= need_len - 1) else None

    def get_or_compute_head_hash(self) -> Optional[str]:
        with self.lock:
            if self.head_hash is not None:
                return self.head_hash
            need_len = self._has_head_bytes()
            if not need_len or not os.path.exists(self.data_path):
                return None
            was_closed = self._data_file is None or self._data_file.closed
            try:
                f = self._ensure_data_file()
                f.seek(0)
                data = f.read(need_len)
                if len(data) != need_len:
                    return None
                self.head_hash = hashlib.sha256(data).hexdigest()
                self._dirty_meta = True
                self.save_meta(force=True)
                return self.head_hash
            except Exception:
                return None
            finally:
                if was_closed and self._active_users == 0:
                    self.close_file()

    def close_file(self) -> None:
        with self.lock:
            if self._data_file is not None and not self._data_file.closed:
                for fn in (self._data_file.flush, self._data_file.close):
                    try:
                        fn()
                    except Exception:
                        pass
            self._data_file = None

    def save_meta(self, force: bool = False) -> None:
        with self.lock:
            if (not force and not self._dirty_meta) or (not self.ranges and self.total_size is None):
                return
            if self._data_file is not None and not self._data_file.closed:
                try:
                    self._data_file.flush()
                except Exception:
                    pass
            meta = {
                "key": self.key,
                "url": self.url,
                "total_size": self.total_size,
                "accept_ranges": self.accept_ranges,
                "content_type": self.content_type,
                "etag": self.etag,
                "last_modified": self.last_modified,
                "head_hash": self.head_hash,
                "known_urls": self.known_urls,
                "alias_keys": self.alias_keys,
                "ranges": self.ranges,
                "cached_bytes": total_cached_bytes(self.ranges),
                "created_at": self.created_at,
                "last_access": self.last_access,
            }
            if _atomic_write_json(self.meta_path, meta, make_dir=True):
                self._dirty_meta = False
                self._unflushed_bytes = 0

    def touch(self) -> None:
        with self.lock:
            self.last_access = time.time()
            self._dirty_meta = True

    def update_metadata(
        self,
        total_size: Optional[int] = None,
        accept_ranges: Optional[bool] = None,
        content_type: Optional[str] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> bool:
        """Update metadata; return False if total_size changed on a non-empty entry."""
        with self.lock:
            if total_size is not None and self.total_size is not None and total_size != self.total_size and self.ranges:
                return False
            if total_size is not None:
                self.total_size = total_size
            if accept_ranges is not None:
                self.accept_ranges = accept_ranges
            if content_type:
                self.content_type = content_type
            if etag is not None:
                self.etag = etag
            if last_modified is not None:
                self.last_modified = last_modified
            self.touch()
            return True

    def invalidate(self) -> None:
        with self.lock:
            self.close_file()
            self.ranges = []
            self.total_size = None
            self.accept_ranges = False
            self.etag = None
            self.last_modified = None
            self.head_hash = None
            self.alias_keys = []
            self._unflushed_bytes = 0
            self._dirty_meta = False
            shutil.rmtree(self.entry_dir, ignore_errors=True)

    def read_chunk(self, offset: int, length: int) -> bytes:
        with self.lock:
            f = self._ensure_data_file()
            f.seek(offset)
            data = f.read(length)
            self.last_access = time.time()
            return data

    def write_chunk(self, offset: int, data: bytes) -> None:
        if not data:
            return
        with self.lock:
            f = self._ensure_data_file()
            f.seek(offset)
            f.write(data)
            self.ranges = merge_intervals(self.ranges + [[offset, offset + len(data) - 1]])
            self.touch()
            self._unflushed_bytes += len(data)
            if self._unflushed_bytes >= self.flush_bytes:
                self.save_meta(force=True)

    def snapshot_plan(self, req_start: int, req_end: int) -> List[Tuple[str, int, int]]:
        with self.lock:
            return plan_segments(self.ranges, req_start, req_end)

    def info(self) -> dict:
        with self.lock:
            cached = total_cached_bytes(self.ranges)
            pct = (cached * 100.0 / self.total_size) if (self.total_size and self.total_size > 0) else 0.0
            return {
                "key": self.key,
                "url": self.url,
                "total_size": self.total_size,
                "cached_bytes": cached,
                "percent": round(pct, 2),
                "ranges_count": len(self.ranges),
                "accept_ranges": self.accept_ranges,
                "content_type": self.content_type,
                "last_access": self.last_access,
            }


class CacheManager:
    """Manages CacheEntry instances, aliases, content dedup, watch_later sync, and LRU eviction."""

    def __init__(self, cache_dir: str, max_cache_mb: int, flush_kb: int, ignore_params: set, match_by_content: bool = True):
        self.cache_dir = cache_dir
        self.max_cache_bytes = max(0, max_cache_mb) * 1024 * 1024
        self.flush_bytes = max(64, flush_kb) * 1024
        self.ignore_params = ignore_params
        self.match_by_content = match_by_content
        os.makedirs(self.cache_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.entries: Dict[str, CacheEntry] = {}
        self.aliases: Dict[str, str] = {}
        self._load_existing_entries()

    def _register_loaded_entry(self, key: str, url: str, entry_dir: str, mark_dirty: bool = False) -> CacheEntry:
        entry = CacheEntry(key, url, entry_dir, self.flush_bytes)
        if mark_dirty:
            entry._dirty_meta = True
        if entry.head_hash is None and entry.total_size and entry.ranges:
            entry.get_or_compute_head_hash()
        if mark_dirty:
            entry.save_meta(force=True)
        self.entries[key] = entry
        for ak in entry.alias_keys:
            self.aliases[ak] = key
        return entry

    def _load_existing_entries(self) -> None:
        try:
            names = os.listdir(self.cache_dir)
        except Exception:
            return

        for name in names:
            subdir = os.path.join(self.cache_dir, name)
            meta = _read_json(os.path.join(subdir, "cache.meta.json")) if os.path.isdir(subdir) else None
            if not meta:
                continue
            url = meta.get("url", "")
            key = meta.get("key") or (url_cache_key(url, self.ignore_params) if url else "")
            if key and url:
                self._register_loaded_entry(key, url, subdir)

        # One-time migration for any legacy flat cache files
        for name in names:
            if not name.endswith(".meta.json"):
                continue
            key = name[:-10]
            meta = _read_json(os.path.join(self.cache_dir, name))
            url = (meta or {}).get("url", "")
            if not url or key in self.entries:
                continue
            target_dir = pick_entry_dir(self.cache_dir, url, key, self.entries)
            os.makedirs(target_dir, exist_ok=True)
            for src_name, dst_name in ((f"{key}.data", "cache.data"), (f"{key}.watch_later", "cache.watch_later"), (name, "cache.meta.json")):
                src = os.path.join(self.cache_dir, src_name)
                if os.path.exists(src):
                    os.replace(src, os.path.join(target_dir, dst_name))
            self._register_loaded_entry(key, url, target_dir, mark_dirty=True)

        for name in names:
            if not name.endswith(".alias.json"):
                continue
            alias_path = os.path.join(self.cache_dir, name)
            data = _read_json(alias_path) or {}
            target = self.entries.get(data.get("target_key", ""))
            if target is not None:
                alias_key = name[:-11]
                self.aliases[alias_key] = target.key
                target.add_alias_key(alias_key)
                target.add_known_url(data.get("url", ""))
                target.save_meta(force=True)
            try:
                os.remove(alias_path)
            except Exception:
                pass

    def _resolve(self, url: str) -> Tuple[str, str, Optional[CacheEntry]]:
        raw_key = url_cache_key(url, self.ignore_params)
        key = self.aliases.get(raw_key, raw_key)
        return raw_key, key, self.entries.get(key)

    def _remove_aliases_for(self, target_key: str) -> None:
        for ak in [k for k, v in self.aliases.items() if v == target_key]:
            self.aliases.pop(ak, None)

    def get_entry(self, url: str) -> CacheEntry:
        with self.lock:
            raw_key, _, entry = self._resolve(url)
            if entry is None:
                entry = CacheEntry(raw_key, url, pick_entry_dir(self.cache_dir, url, raw_key, self.entries), self.flush_bytes)
                self.entries[raw_key] = entry
            else:
                with entry.lock:
                    if entry.url != url:
                        entry.url = url
                entry.add_known_url(url)
            return entry

    def _size_candidates(self, total_size: int, exclude_key: Optional[str]) -> List[CacheEntry]:
        if not self.match_by_content or total_size <= 0:
            return []
        with self.lock:
            return [
                e for k, e in self.entries.items()
                if k != exclude_key and e.total_size == total_size and e._has_head_bytes()
            ]

    def has_candidates_for_size(self, total_size: int, exclude_key: Optional[str] = None) -> bool:
        return bool(self._size_candidates(total_size, exclude_key))

    def find_matching_entry(
        self, total_size: int, head_hash: str, exclude_key: Optional[str] = None
    ) -> Optional[CacheEntry]:
        if not head_hash:
            return None
        for entry in self._size_candidates(total_size, exclude_key):
            if entry.get_or_compute_head_hash() == head_hash:
                return entry
        return None

    def link_alias(self, alias_key: str, target_key: str, alias_url: str) -> None:
        if alias_key == target_key:
            return
        with self.lock:
            temp = self.entries.get(alias_key)
            if temp is not None and not temp.ranges:
                self.entries.pop(alias_key, None)
            self.aliases[alias_key] = target_key
            target = self.entries.get(target_key)
            if target is not None:
                with target.lock:
                    target.url = alias_url
                    target.touch()
                target.add_alias_key(alias_key)
                target.add_known_url(alias_url)
                target.save_meta(force=True)

    def maybe_evict(self, protect_key: Optional[str] = None) -> None:
        if self.max_cache_bytes <= 0:
            return
        with self.lock:
            infos = []
            total = 0
            for key, entry in list(self.entries.items()):
                info = entry.info()
                total += info["cached_bytes"]
                infos.append((info["last_access"], key, entry, info["cached_bytes"]))
            if total <= self.max_cache_bytes:
                return
            for _, key, entry, size in sorted(infos, key=lambda x: x[0]):
                if total <= self.max_cache_bytes:
                    break
                if key == protect_key:
                    continue
                entry.invalidate()
                self.entries.pop(key, None)
                self._remove_aliases_for(key)
                total -= size

    def clear_url(self, url: str) -> bool:
        with self.lock:
            _, key, entry = self._resolve(url)
            self.entries.pop(key, None)
            self._remove_aliases_for(key)
            if entry is not None:
                entry.invalidate()
                return True
            return False

    def clear_all(self) -> int:
        with self.lock:
            count = len(self.entries)
            for entry in list(self.entries.values()):
                entry.invalidate()
            self.entries.clear()
            self.aliases.clear()
            for name in os.listdir(self.cache_dir):
                p = os.path.join(self.cache_dir, name)
                if os.path.isdir(p) and os.path.exists(os.path.join(p, "cache.meta.json")):
                    shutil.rmtree(p, ignore_errors=True)
                elif name.endswith((".data", ".meta.json", ".alias.json", ".watch_later", ".tmp")):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            return count

    def get_watch_later(self, url: str, wl_dir: Optional[str]) -> dict:
        with self.lock:
            _, _, entry = self._resolve(url)
        if entry is None:
            return {"resume": False}
        with entry.lock:
            known_urls, wl_backup = list(entry.known_urls), entry.watch_later_path

        cur_wl = os.path.join(wl_dir, watch_later_hash(url)) if wl_dir else None
        candidates = []
        seen_paths = set()
        for is_cur, p in [(True, cur_wl), (False, wl_backup)] + [
            (False, os.path.join(wl_dir, watch_later_hash(u))) for u in (known_urls if wl_dir else [])
        ]:
            if not p or p in seen_paths or not os.path.exists(p):
                continue
            seen_paths.add(p)
            txt = _read_text(p)
            if txt is not None:
                try:
                    candidates.append((os.path.getmtime(p), txt, is_cur))
                except Exception:
                    pass

        if not candidates:
            return {"resume": False}

        best_mtime, best_text, is_current = max(candidates, key=lambda c: c[0])
        cur_mtime = next((c[0] for c in candidates if c[2]), None)
        # Prefer current file within 1s of newest so native mpv resume isn't overridden
        if is_current or (cur_mtime is not None and best_mtime - cur_mtime <= 1.0):
            return {"resume": False, "already_had": True}

        start_val, props = parse_watch_later(best_text)
        if start_val is None or start_val <= 0:
            return {"resume": False}
        if cur_wl and wl_dir:
            _atomic_write(cur_wl, best_text, make_dir=True)
        return {"resume": True, "start": start_val, "props": props}

    def sync_watch_later(self, url: str, wl_dir: Optional[str], pos: Optional[float], reason: str) -> dict:
        with self.lock:
            _, _, entry = self._resolve(url)
        if entry is None:
            return {"saved": False}
        with entry.lock:
            known_urls, wl_backup = list(entry.known_urls), entry.watch_later_path

        if reason == "eof":
            try:
                if os.path.exists(wl_backup):
                    os.remove(wl_backup)
            except Exception:
                pass
            return {"saved": False, "cleared": True}

        cur_wl = os.path.join(wl_dir, watch_later_hash(url)) if wl_dir else None
        wl_text = None
        if cur_wl and os.path.exists(cur_wl):
            txt = _read_text(cur_wl)
            if txt:
                start_val, _ = parse_watch_later(txt)
                if start_val is not None and start_val > 0:
                    wl_text = txt
        if wl_text is None and pos is not None and pos > 0:
            wl_text = f"start={pos:.6f}\n"
        if not wl_text:
            return {"saved": False}

        _atomic_write(wl_backup, wl_text, make_dir=True)
        if wl_dir and os.path.isdir(wl_dir):
            targets = {os.path.join(wl_dir, watch_later_hash(u)) for u in known_urls}
            targets = {p for p in targets if os.path.exists(p)}
            if cur_wl and not os.path.exists(cur_wl):
                targets.add(cur_wl)
            for p in targets:
                _atomic_write(p, wl_text)
        return {"saved": True}

    def flush_all(self) -> None:
        with self.lock:
            for entry in list(self.entries.values()):
                entry.save_meta(force=True)
                entry.close_file()

    def stats(self, url: Optional[str] = None) -> dict:
        with self.lock:
            cached_entries = [cb for e in self.entries.values() if (cb := total_cached_bytes(e.ranges)) > 0]
            current = self._resolve(url)[2] if url else None
            return {
                "entries_count": len(cached_entries),
                "total_cached_bytes": sum(cached_entries),
                "cache_dir": self.cache_dir,
                "current": current.info() if current is not None else None,
            }


class ProxyHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, server_address, handler_class, manager: CacheManager, chunk_size: int):
        self.manager = manager
        self.chunk_size = chunk_size
        self.activity_lock = threading.Lock()
        self.active_requests = 0
        self.last_activity = time.monotonic()
        self.thread_local = threading.local()
        super().__init__(server_address, handler_class)

    def get_session(self) -> requests.Session:
        sess = getattr(self.thread_local, "session", None)
        if sess is None:
            sess = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
            sess.mount("http://", adapter)
            sess.mount("https://", adapter)
            self.thread_local.session = sess
        sess.cookies.clear()
        return sess

    def begin_request(self) -> None:
        with self.activity_lock:
            self.active_requests += 1
            self.last_activity = time.monotonic()

    def end_request(self) -> None:
        with self.activity_lock:
            self.active_requests = max(0, self.active_requests - 1)
            self.last_activity = time.monotonic()


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _parse_req(self) -> Tuple[str, dict]:
        parsed = urllib.parse.urlsplit(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_media_headers(
        self,
        status: int,
        content_type: str,
        content_length: Optional[int],
        accept_ranges: bool,
        content_range: Optional[str] = None,
    ) -> None:
        self.send_response(status)
        if content_range:
            self.send_header("Content-Range", content_range)
        self.send_header("Content-Type", content_type)
        if accept_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

    def _extract_upstream_config(self) -> Tuple[Optional[str], dict, Optional[dict], bool, float]:
        _, qs = self._parse_req()
        target_url = qs.get("url", [None])[0]
        upstream_headers = {}
        proxy_url = None
        tls_verify = True
        timeout_sec = 60.0

        for k, v in self.headers.items():
            kl = k.lower()
            if kl == "x-mpv-cache-proxy":
                proxy_url = v.strip() or None
            elif kl == "x-mpv-cache-tls-verify":
                tls_verify = v.strip() not in ("0", "false", "no")
            elif kl == "x-mpv-cache-timeout":
                try:
                    timeout_sec = max(5.0, float(v.strip()))
                except Exception:
                    pass
            elif not kl.startswith("x-mpv-cache-") and kl not in HOP_BY_HOP_HEADERS:
                upstream_headers[k] = v

        upstream_headers["Accept-Encoding"] = "identity"
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        return target_url, upstream_headers, proxies, tls_verify, timeout_sec

    def _fetch_upstream(
        self,
        method: str,
        url: str,
        headers: dict,
        proxies: Optional[dict],
        tls_verify: bool,
        timeout_sec: float,
        range_header: Optional[str] = None,
    ) -> requests.Response:
        req_headers = dict(headers)
        if range_header is not None:
            req_headers["Range"] = range_header
        return self.server.get_session().request(
            method=method, url=url, headers=req_headers, proxies=proxies,
            verify=tls_verify, stream=True, allow_redirects=True,
            timeout=(min(20.0, timeout_sec), timeout_sec),
        )

    def _record_probe_meta(self, entry: CacheEntry, resp: requests.Response) -> None:
        ct = resp.headers.get("Content-Type", "application/octet-stream")
        if is_non_cacheable_ct(ct):
            return
        accept_ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
        total_size = None
        if resp.status_code == 206:
            accept_ranges = True
            cr = parse_content_range(resp.headers.get("Content-Range", ""))
            if cr:
                total_size = cr[2]
        elif resp.status_code == 200:
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                total_size = int(cl)
        meta_kw = dict(
            total_size=total_size, accept_ranges=accept_ranges, content_type=ct,
            etag=resp.headers.get("ETag"), last_modified=resp.headers.get("Last-Modified"),
        )
        if not entry.update_metadata(**meta_kw):
            entry.invalidate()
            entry.update_metadata(**meta_kw)
        entry.save_meta(force=True)

    def do_HEAD(self):
        self.server.begin_request()
        try:
            path, _ = self._parse_req()
            if path == "/__health":
                return self._send_media_headers(200, "application/json", 0, False)
            if path != "/stream":
                return self.send_error(404)

            target_url, upstream_headers, proxies, tls_verify, timeout_sec = self._extract_upstream_config()
            if not target_url:
                return self.send_error(400, "Missing url")

            entry = self.server.manager.get_entry(target_url)
            with entry.lock:
                if entry.total_size is not None:
                    return self._send_media_headers(200, entry.content_type, entry.total_size, entry.accept_ranges)

            resp = self._fetch_upstream("HEAD", target_url, upstream_headers, proxies, tls_verify, timeout_sec)
            try:
                if resp.status_code == 405:
                    resp.close()
                    resp = self._fetch_upstream(
                        "GET", target_url, upstream_headers, proxies, tls_verify, timeout_sec, range_header="bytes=0-0"
                    )
                self._record_probe_meta(entry, resp)
                self._send_media_headers(
                    200 if resp.status_code in (200, 206) else resp.status_code,
                    resp.headers.get("Content-Type", "application/octet-stream"),
                    entry.total_size,
                    entry.accept_ranges,
                )
            finally:
                resp.close()
        except Exception as e:
            try:
                self.close_connection = True
                self.send_error(502, str(e))
            except Exception:
                pass
        finally:
            self.server.end_request()

    def do_POST(self):
        self.server.begin_request()
        try:
            path, qs = self._parse_req()
            content_len = int(self.headers.get("Content-Length", "0") or "0")
            if content_len > 0:
                self.rfile.read(content_len)
            target_url = qs.get("url", [None])[0]
            if path == "/__clear_all":
                return self._send_json(200, {"cleared_entries": self.server.manager.clear_all()})
            if path in ("/__clear", "/__watch_later") and not target_url:
                return self._send_json(400, {"error": "Missing url"})
            if path == "/__clear":
                return self._send_json(200, {"cleared": self.server.manager.clear_url(target_url)})
            if path == "/__watch_later":
                pos_val = None
                try:
                    if qs.get("pos", [None])[0] is not None:
                        pos_val = float(qs["pos"][0])
                except ValueError:
                    pass
                return self._send_json(200, self.server.manager.sync_watch_later(
                    target_url, qs.get("wl_dir", [None])[0], pos_val, qs.get("reason", [""])[0]
                ))
            self._send_json(404, {"error": "Not found"})
        finally:
            self.server.end_request()

    def do_GET(self):
        self.server.begin_request()
        try:
            path, qs = self._parse_req()
            target_url = qs.get("url", [None])[0]
            if path == "/__health":
                self._send_json(200, {"status": "ok", "pid": os.getpid(), "port": self.server.server_address[1]})
            elif path == "/__status":
                self._send_json(200, self.server.manager.stats(target_url))
            elif path == "/__watch_later":
                if not target_url:
                    self._send_json(400, {"error": "Missing url"})
                else:
                    self._send_json(200, self.server.manager.get_watch_later(target_url, qs.get("wl_dir", [None])[0]))
            elif path == "/stream":
                self._handle_stream()
            else:
                self._send_json(404, {"error": "Not found"})
        finally:
            self.server.end_request()

    def _handle_stream(self) -> None:
        target_url, upstream_headers, proxies, tls_verify, timeout_sec = self._extract_upstream_config()
        if not target_url:
            return self.send_error(400, "Missing url parameter")
        entry = self.server.manager.get_entry(target_url)
        entry.acquire_user()
        try:
            self._handle_stream_for_entry(entry, target_url, upstream_headers, proxies, tls_verify, timeout_sec)
        finally:
            entry.release_user()

    def _handle_stream_for_entry(
        self, entry: CacheEntry, target_url: str, upstream_headers: dict,
        proxies: Optional[dict], tls_verify: bool, timeout_sec: float,
    ) -> None:
        client_range_hdr = self.headers.get("Range")
        with entry.lock:
            known_total = entry.total_size
            known_ct = entry.content_type
            can_plan = known_total is not None and known_total > 0 and (
                entry.accept_ranges or total_cached_bytes(entry.ranges) >= known_total
            )

        if can_plan:
            try:
                has_range, req_start, req_end = parse_range_header(client_range_hdr, known_total)
            except ValueError:
                return self._send_media_headers(416, known_ct, 0, False, content_range=f"bytes */{known_total}")
            if req_start is not None and req_end is not None:
                self._serve_known_range(
                    entry, has_range, req_start, req_end, known_total, known_ct,
                    upstream_headers, proxies, tls_verify, timeout_sec,
                )
                self.server.manager.maybe_evict(protect_key=entry.key)
                return

        try:
            has_range, _, _ = parse_range_header(client_range_hdr, None)
        except ValueError:
            return self._send_media_headers(416, "application/octet-stream", 0, False)

        try:
            resp = self._fetch_upstream(
                "GET", target_url, upstream_headers, proxies, tls_verify, timeout_sec,
                range_header=client_range_hdr or "bytes=0-",
            )
        except Exception as e:
            self.close_connection = True
            return self.send_error(502, f"Upstream error: {e}")

        try:
            ct = resp.headers.get("Content-Type", "application/octet-stream")
            if resp.status_code not in (200, 206) or is_non_cacheable_ct(ct):
                return self._passthrough_response(resp, send_as_200=(not has_range and resp.status_code == 206))

            if resp.status_code == 206:
                cr = parse_content_range(resp.headers.get("Content-Range", ""))
                if not cr:
                    self._record_probe_meta(entry, resp)
                    return self._passthrough_response(resp, send_as_200=(not has_range))
                u_start, u_end, u_total = cr
                stream_max_bytes: Optional[int] = u_end - u_start + 1
            else:
                cl = resp.headers.get("Content-Length")
                u_start = 0
                u_total = int(cl) if (cl and cl.isdigit()) else None
                u_end = (u_total - 1) if u_total is not None else None
                stream_max_bytes = u_total

            # Content deduplication: match exact total_size + SHA-256 of first 64 KB across URLs
            head_buf, head_hash = b"", None
            if (
                u_start == 0 and u_total and not entry.ranges
                and self.server.manager.has_candidates_for_size(u_total, exclude_key=entry.key)
            ):
                need_len = min(HEAD_HASH_BYTES, u_total)
                chunks, got = [], 0
                while got < need_len:
                    part = resp.raw.read(need_len - got, decode_content=False)
                    if not part:
                        break
                    chunks.append(part)
                    got += len(part)
                head_buf = b"".join(chunks)
                if len(head_buf) == need_len:
                    head_hash = hashlib.sha256(head_buf).hexdigest()
                    matched = self.server.manager.find_matching_entry(u_total, head_hash, exclude_key=entry.key)
                    if matched is not None:
                        try:
                            resp.close()
                        except Exception:
                            pass
                        self.server.manager.link_alias(entry.key, matched.key, target_url)
                        matched.acquire_user()
                        try:
                            m_has_range, m_start, m_end = parse_range_header(client_range_hdr, u_total)
                            if m_start is None or m_end is None:
                                m_start, m_end = 0, u_total - 1
                            with matched.lock:
                                m_ct = matched.content_type or ct
                            self._serve_known_range(
                                matched, m_has_range, m_start, m_end, u_total, m_ct,
                                upstream_headers, proxies, tls_verify, timeout_sec,
                            )
                        finally:
                            matched.release_user()
                        self.server.manager.maybe_evict(protect_key=matched.key)
                        return

            self._record_probe_meta(entry, resp)
            if head_hash:
                with entry.lock:
                    entry.head_hash = head_hash
                    entry._dirty_meta = True

            if resp.status_code == 206:
                cr_hdr = f"bytes {u_start}-{u_end}/{u_total if u_total is not None else '*'}" if has_range else None
                self._send_media_headers(206 if has_range else 200, ct, u_end - u_start + 1, True, cr_hdr)
            else:
                self._send_media_headers(200, ct, u_total, entry.accept_ranges)

            if head_buf:
                entry.write_chunk(u_start, head_buf)
                try:
                    self.wfile.write(head_buf)
                except DISCONNECT_ERRORS:
                    self.close_connection = True
                    return
                if stream_max_bytes is not None:
                    stream_max_bytes = max(0, stream_max_bytes - len(head_buf))
                u_start += len(head_buf)

            self._stream_and_cache_upstream(resp, entry, start_offset=u_start, max_bytes=stream_max_bytes)
        finally:
            try:
                resp.close()
            except Exception:
                pass
            entry.save_meta(force=True)
            self.server.manager.maybe_evict(protect_key=entry.key)

    def _passthrough_response(self, resp: requests.Response, send_as_200: bool = False) -> None:
        self.send_response(200 if send_as_200 else resp.status_code)
        for k, v in resp.headers.items():
            kl = k.lower()
            if kl in ("transfer-encoding", "connection", "keep-alive", "content-encoding"):
                continue
            if send_as_200 and kl == "content-range":
                continue
            self.send_header(k, v)
        if "content-length" not in {k.lower() for k in resp.headers}:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        try:
            while (chunk := resp.raw.read(self.server.chunk_size, decode_content=False)):
                self.wfile.write(chunk)
        except DISCONNECT_ERRORS:
            self.close_connection = True

    def _stream_and_cache_upstream(
        self, resp: requests.Response, entry: CacheEntry, start_offset: int,
        max_bytes: Optional[int], skip_leading: int = 0,
    ) -> int:
        offset, forwarded, remaining, to_skip = start_offset, 0, max_bytes, skip_leading
        try:
            while remaining is None or remaining > 0:
                to_read = self.server.chunk_size if remaining is None else min(self.server.chunk_size, remaining)
                chunk = resp.raw.read(to_read, decode_content=False)
                if not chunk:
                    break
                entry.write_chunk(offset, chunk)
                offset += len(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
                if to_skip >= len(chunk):
                    to_skip -= len(chunk)
                    continue
                out = chunk[to_skip:] if to_skip > 0 else chunk
                to_skip = 0
                self.wfile.write(out)
                forwarded += len(out)
            self.wfile.flush()
        except DISCONNECT_ERRORS:
            self.close_connection = True
        return forwarded

    def _serve_known_range(
        self, entry: CacheEntry, has_range: bool, req_start: int, req_end: int, total_size: int,
        content_type: str, upstream_headers: dict, proxies: Optional[dict], tls_verify: bool, timeout_sec: float,
    ) -> None:
        segments = entry.snapshot_plan(req_start, req_end)

        # Prefetch first network segment before committing headers so upstream 4xx/5xx propagate
        prefetched_resp: Optional[requests.Response] = None
        if segments and segments[0][0] == "net":
            _, seg_s, seg_e = segments[0]
            rng = f"bytes={seg_s}-" if seg_e == total_size - 1 else f"bytes={seg_s}-{seg_e}"
            try:
                prefetched_resp = self._fetch_upstream(
                    "GET", entry.url, upstream_headers, proxies, tls_verify, timeout_sec, range_header=rng
                )
            except Exception as e:
                self.close_connection = True
                return self.send_error(502, f"Upstream error: {e}")
            if prefetched_resp.status_code not in (200, 206):
                try:
                    return self._passthrough_response(prefetched_resp)
                finally:
                    prefetched_resp.close()

        cr_hdr = f"bytes {req_start}-{req_end}/{total_size}" if has_range else None
        self._send_media_headers(206 if has_range else 200, content_type, req_end - req_start + 1, True, cr_hdr)
        chunk_size = self.server.chunk_size
        entry.touch()

        try:
            for idx, (kind, seg_s, seg_e) in enumerate(segments):
                if kind == "cache":
                    pos = seg_s
                    while pos <= seg_e:
                        data = entry.read_chunk(pos, min(chunk_size, seg_e - pos + 1))
                        if not data:
                            self._fetch_and_stream_net_segment(
                                entry, pos, seg_e, total_size, upstream_headers, proxies, tls_verify, timeout_sec
                            )
                            break
                        self.wfile.write(data)
                        pos += len(data)
                else:
                    resp = prefetched_resp if idx == 0 else None
                    if idx == 0:
                        prefetched_resp = None
                    if not self._fetch_and_stream_net_segment(
                        entry, seg_s, seg_e, total_size, upstream_headers, proxies, tls_verify, timeout_sec, resp
                    ):
                        self.close_connection = True
                        break
            self.wfile.flush()
        except DISCONNECT_ERRORS:
            self.close_connection = True
        finally:
            if prefetched_resp is not None:
                try:
                    prefetched_resp.close()
                except Exception:
                    pass
            entry.save_meta(force=True)

    def _fetch_and_stream_net_segment(
        self, entry: CacheEntry, seg_s: int, seg_e: int, total_size: int,
        upstream_headers: dict, proxies: Optional[dict], tls_verify: bool,
        timeout_sec: float, existing_resp: Optional[requests.Response] = None,
    ) -> bool:
        resp = existing_resp
        if resp is None:
            rng = f"bytes={seg_s}-" if seg_e == total_size - 1 else f"bytes={seg_s}-{seg_e}"
            try:
                resp = self._fetch_upstream(
                    "GET", entry.url, upstream_headers, proxies, tls_verify, timeout_sec, range_header=rng
                )
            except Exception:
                return False

        try:
            needed = seg_e - seg_s + 1
            if resp.status_code == 206:
                cr = parse_content_range(resp.headers.get("Content-Range", ""))
                if cr:
                    u_start, u_end, u_total = cr
                    if u_total is not None and u_total != total_size:
                        entry.invalidate()
                        return False
                    if u_start > seg_s:
                        return False
                    skip = seg_s - u_start
                    got = self._stream_and_cache_upstream(
                        resp, entry, start_offset=u_start,
                        max_bytes=min(u_end - u_start + 1, skip + needed), skip_leading=skip,
                    )
                else:
                    got = self._stream_and_cache_upstream(resp, entry, start_offset=seg_s, max_bytes=needed)
                return got == needed

            if resp.status_code == 200:
                got = self._stream_and_cache_upstream(
                    resp, entry, start_offset=0, max_bytes=seg_e + 1, skip_leading=seg_s
                )
                return got == needed
            return False
        finally:
            try:
                resp.close()
            except Exception:
                pass


def acquire_singleton_lock(lock_path: str):
    """Acquire non-blocking exclusive lock file, returning open file or None."""
    f = open(lock_path, "a+b")
    try:
        f.seek(0)
        if os.name == "nt":
            if os.path.getsize(lock_path) == 0:
                f.write(b"0")
                f.flush()
                f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except Exception:
        try:
            f.close()
        except Exception:
            pass
        return None


def redirect_stdio(log_path: Optional[str]) -> None:
    try:
        in_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(in_fd, 0)
        os.close(in_fd)
    except Exception:
        pass
    try:
        out_fd = os.open(log_path or os.devnull, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
        os.dup2(out_fd, 1)
        os.dup2(out_fd, 2)
        if out_fd > 2:
            os.close(out_fd)
        sys.stdout = open(1, "w", encoding="utf-8", buffering=1, closefd=False)
        sys.stderr = open(2, "w", encoding="utf-8", buffering=1, closefd=False)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="mpv HTTP 206 Range Cache Proxy")
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--idle-timeout", type=int, default=300)
    parser.add_argument("--max-cache-mb", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=65536)
    parser.add_argument("--flush-kb", type=int, default=1024)
    parser.add_argument("--ignore-params", default="")
    parser.add_argument("--no-match-by-content", action="store_true")
    parser.add_argument("--log-file", default="")
    args = parser.parse_args()

    os.makedirs(args.cache_dir, exist_ok=True)
    redirect_stdio(args.log_file or None)

    lock_file = acquire_singleton_lock(os.path.join(args.cache_dir, "proxy.lock"))
    if lock_file is None:
        return 0

    ignore_params = {p.strip() for p in args.ignore_params.split(",") if p.strip()}
    manager = CacheManager(
        args.cache_dir, args.max_cache_mb, args.flush_kb, ignore_params, not args.no_match_by_content
    )
    server = ProxyHTTPServer(("127.0.0.1", args.port), ProxyHandler, manager, max(8192, args.chunk_size))
    server_info_path = os.path.join(args.cache_dir, "server.json")
    _atomic_write_json(server_info_path, {"pid": os.getpid(), "port": server.server_address[1], "started_at": time.time()})

    shutdown_event = threading.Event()

    def stop_server(*_args):
        if not shutdown_event.is_set():
            shutdown_event.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop_server)
    signal.signal(signal.SIGTERM, stop_server)

    if args.idle_timeout > 0:
        def idle_watchdog():
            while not shutdown_event.wait(5.0):
                with server.activity_lock:
                    idle_for = time.monotonic() - server.last_activity
                    active = server.active_requests
                if active == 0 and idle_for >= args.idle_timeout:
                    stop_server()
                    break
        threading.Thread(target=idle_watchdog, daemon=True).start()

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        manager.flush_all()
        try:
            server.server_close()
        except Exception:
            pass
        info = _read_json(server_info_path)
        if info and info.get("pid") == os.getpid():
            try:
                os.remove(server_info_path)
            except Exception:
                pass
        try:
            lock_file.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
