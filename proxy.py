#!/usr/bin/env python3
"""Fully async aiohttp HTTP 206 Range-Caching Proxy for mpv with parallel downloads."""

import argparse
import asyncio
from collections import deque
import hashlib
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

import aiohttp
from aiohttp import web

if os.name == "nt":
    import ctypes
    import msvcrt
else:
    import fcntl

FSCTL_SET_SPARSE = 0x000900C4
HEAD_HASH_BYTES = 65536
DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError, aiohttp.ClientConnectionError)

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
    """Mark a newly created NTFS file sparse so skipped seeks do not zero-fill."""
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

    def plan_state(self) -> Tuple[Optional[int], str, bool]:
        with self.lock:
            can_plan = (
                self.total_size is not None
                and self.total_size > 0
                and (self.accept_ranges or total_cached_bytes(self.ranges) >= self.total_size)
            )
            return self.total_size, self.content_type, can_plan

    def head_info(self) -> Tuple[Optional[int], str, bool]:
        with self.lock:
            return self.total_size, self.content_type, self.accept_ranges

    def set_head_hash(self, head_hash: str) -> None:
        with self.lock:
            self.head_hash = head_hash
            self._dirty_meta = True

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

        # One-time migration for legacy flat cache files
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

    def acquire_entry(self, url: str) -> CacheEntry:
        entry = self.get_entry(url)
        entry.acquire_user()
        return entry

    def check_dedup_ready(self, entry: CacheEntry, total_size: Optional[int]) -> bool:
        if not total_size or total_size <= 0:
            return False
        with entry.lock:
            if entry.ranges:
                return False
        return self.has_candidates_for_size(total_size, exclude_key=entry.key)

    def link_and_acquire_alias(self, alias_key: str, target: CacheEntry, alias_url: str) -> str:
        self.link_alias(alias_key, target.key, alias_url)
        with target.lock:
            target.acquire_user()
            return target.content_type

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


class AsyncRangeProxy:
    """Async aiohttp web server and upstream multi-connection range downloader."""

    def __init__(self, manager: CacheManager, chunk_size: int = 65536, connections: int = 4, part_size_kb: int = 2048):
        self.manager = manager
        self.chunk_size = max(8192, chunk_size)
        self.connections = max(1, min(16, connections))
        self.part_size = max(262144, part_size_kb * 1024)
        self.active_requests = 0
        self.last_activity = time.monotonic()
        self.port = 0
        self.session: Optional[aiohttp.ClientSession] = None

    async def start_session(self) -> None:
        if self.session is not None and not self.session.closed:
            return
        connector = aiohttp.TCPConnector(
            limit=max(64, self.connections * 8),
            limit_per_host=max(32, self.connections * 4),
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
            keepalive_timeout=60.0,
        )
        self.session = aiohttp.ClientSession(
            connector=connector,
            auto_decompress=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            read_bufsize=262144,
        )

    async def close_session(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None

    @web.middleware
    async def _activity_middleware(self, request: web.Request, handler):
        self.active_requests += 1
        self.last_activity = time.monotonic()
        try:
            return await handler(request)
        finally:
            self.active_requests = max(0, self.active_requests - 1)
            self.last_activity = time.monotonic()

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._activity_middleware])
        app.router.add_get("/__health", self.handle_health)
        app.router.add_get("/__status", self.handle_status)
        app.router.add_get("/__watch_later", self.handle_get_watch_later)
        app.router.add_post("/__watch_later", self.handle_post_watch_later)
        app.router.add_post("/__clear", self.handle_clear)
        app.router.add_post("/__clear_all", self.handle_clear_all)
        app.router.add_get("/stream", self.handle_stream_get, allow_head=False)
        app.router.add_head("/stream", self.handle_stream_head)
        return app

    @staticmethod
    def _extract_upstream_config(request: web.Request) -> Tuple[Optional[str], dict, Optional[str], bool, float]:
        target_url = request.query.get("url")
        upstream_headers: dict = {}
        proxy_url: Optional[str] = None
        tls_verify = True
        timeout_sec = 60.0

        for k, v in request.headers.items():
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
        return target_url, upstream_headers, proxy_url, tls_verify, timeout_sec

    async def _fetch_upstream(
        self,
        method: str,
        url: str,
        headers: dict,
        proxy_url: Optional[str],
        tls_verify: bool,
        timeout_sec: float,
        range_header: Optional[str] = None,
    ) -> aiohttp.ClientResponse:
        await self.start_session()
        req_headers = dict(headers)
        if range_header is not None:
            req_headers["Range"] = range_header
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=min(20.0, timeout_sec),
            sock_connect=min(20.0, timeout_sec),
            sock_read=timeout_sec,
        )
        return await self.session.request(
            method=method,
            url=url,
            headers=req_headers,
            proxy=proxy_url,
            ssl=None if tls_verify else False,
            allow_redirects=True,
            timeout=timeout,
        )

    @staticmethod
    def _record_probe_meta(entry: CacheEntry, resp: aiohttp.ClientResponse, head_hash: Optional[str] = None) -> None:
        ct = resp.headers.get("Content-Type", "application/octet-stream")
        if is_non_cacheable_ct(ct):
            return
        accept_ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
        total_size = None
        if resp.status == 206:
            accept_ranges = True
            cr = parse_content_range(resp.headers.get("Content-Range", ""))
            if cr:
                total_size = cr[2]
        elif resp.status == 200:
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
        if head_hash:
            entry.set_head_hash(head_hash)
        entry.save_meta(force=True)

    @staticmethod
    def _media_headers(
        content_type: str,
        content_length: Optional[int],
        accept_ranges: bool,
        content_range: Optional[str] = None,
    ) -> dict:
        hdrs = {"Content-Type": content_type}
        if content_range:
            hdrs["Content-Range"] = content_range
        if accept_ranges:
            hdrs["Accept-Ranges"] = "bytes"
        if content_length is not None:
            hdrs["Content-Length"] = str(content_length)
        return hdrs

    async def handle_health(self, request: web.Request) -> web.Response:
        if request.method == "HEAD":
            return web.Response(status=200, headers={"Content-Type": "application/json", "Content-Length": "0"})
        return web.json_response({"status": "ok", "pid": os.getpid(), "port": self.port})

    async def handle_status(self, request: web.Request) -> web.Response:
        stats = await asyncio.to_thread(self.manager.stats, request.query.get("url"))
        return web.json_response(stats)

    async def handle_get_watch_later(self, request: web.Request) -> web.Response:
        target_url = request.query.get("url")
        if not target_url:
            return web.json_response({"error": "Missing url"}, status=400)
        res = await asyncio.to_thread(self.manager.get_watch_later, target_url, request.query.get("wl_dir"))
        return web.json_response(res)

    async def handle_post_watch_later(self, request: web.Request) -> web.Response:
        target_url = request.query.get("url")
        if not target_url:
            return web.json_response({"error": "Missing url"}, status=400)
        pos_val = None
        pos_str = request.query.get("pos")
        if pos_str is not None:
            try:
                pos_val = float(pos_str)
            except ValueError:
                pass
        res = await asyncio.to_thread(
            self.manager.sync_watch_later,
            target_url, request.query.get("wl_dir"), pos_val, request.query.get("reason", ""),
        )
        return web.json_response(res)

    async def handle_clear(self, request: web.Request) -> web.Response:
        target_url = request.query.get("url")
        if not target_url:
            return web.json_response({"error": "Missing url"}, status=400)
        cleared = await asyncio.to_thread(self.manager.clear_url, target_url)
        return web.json_response({"cleared": cleared})

    async def handle_clear_all(self, request: web.Request) -> web.Response:
        count = await asyncio.to_thread(self.manager.clear_all)
        return web.json_response({"cleared_entries": count})

    async def handle_stream_head(self, request: web.Request) -> web.Response:
        target_url, upstream_headers, proxy_url, tls_verify, timeout_sec = self._extract_upstream_config(request)
        if not target_url:
            return web.Response(status=400, text="Missing url")
        entry = await asyncio.to_thread(self.manager.get_entry, target_url)
        known_total, known_ct, known_ar = await asyncio.to_thread(entry.head_info)
        if known_total is not None:
            return web.Response(
                status=200,
                headers=self._media_headers(known_ct, known_total, known_ar),
            )
        try:
            resp = await self._fetch_upstream("HEAD", target_url, upstream_headers, proxy_url, tls_verify, timeout_sec)
            try:
                if resp.status == 405:
                    resp.close()
                    resp = await self._fetch_upstream(
                        "GET", target_url, upstream_headers, proxy_url, tls_verify, timeout_sec, range_header="bytes=0-0"
                    )
                await asyncio.to_thread(self._record_probe_meta, entry, resp)
                known_total, _, known_ar = await asyncio.to_thread(entry.head_info)
                status = 200 if resp.status in (200, 206) else resp.status
                return web.Response(
                    status=status,
                    headers=self._media_headers(
                        resp.headers.get("Content-Type", "application/octet-stream"),
                        known_total,
                        known_ar,
                    ),
                )
            finally:
                resp.close()
        except Exception as e:
            return web.Response(status=502, text=str(e))

    async def handle_stream_get(self, request: web.Request) -> web.StreamResponse:
        target_url, upstream_headers, proxy_url, tls_verify, timeout_sec = self._extract_upstream_config(request)
        if not target_url:
            return web.Response(status=400, text="Missing url parameter")
        entry = await asyncio.to_thread(self.manager.acquire_entry, target_url)
        try:
            return await self._handle_stream_for_entry(
                request, entry, target_url, upstream_headers, proxy_url, tls_verify, timeout_sec
            )
        finally:
            await asyncio.to_thread(entry.release_user)

    async def _handle_stream_for_entry(
        self,
        request: web.Request,
        entry: CacheEntry,
        target_url: str,
        upstream_headers: dict,
        proxy_url: Optional[str],
        tls_verify: bool,
        timeout_sec: float,
    ) -> web.StreamResponse:
        client_range_hdr = request.headers.get("Range")
        known_total, known_ct, can_plan = await asyncio.to_thread(entry.plan_state)

        if can_plan:
            try:
                has_range, req_start, req_end = parse_range_header(client_range_hdr, known_total)
            except ValueError:
                return web.Response(status=416, headers={"Content-Range": f"bytes */{known_total}"})
            if req_start is not None and req_end is not None:
                resp = await self._serve_known_range(
                    request, entry, has_range, req_start, req_end, known_total, known_ct,
                    upstream_headers, proxy_url, tls_verify, timeout_sec,
                )
                await asyncio.to_thread(self.manager.maybe_evict, protect_key=entry.key)
                return resp

        try:
            has_range, req_start, req_end = parse_range_header(client_range_hdr, None)
        except ValueError:
            return web.Response(status=416)

        # On cold open-ended starts with parallel mode enabled, probe only the first part
        # so multi-connection workers can accelerate everything after part 0 immediately.
        probe_capped = (
            self.connections > 1
            and (req_start in (None, 0))
            and req_end is None
        )
        probe_range = f"bytes=0-{self.part_size - 1}" if probe_capped else (client_range_hdr or "bytes=0-")

        try:
            up_resp = await self._fetch_upstream(
                "GET", target_url, upstream_headers, proxy_url, tls_verify, timeout_sec, range_header=probe_range
            )
        except Exception as e:
            return web.Response(status=502, text=f"Upstream error: {e}")

        close_up_in_finally = True
        try:
            ct = up_resp.headers.get("Content-Type", "application/octet-stream")
            if up_resp.status not in (200, 206) or is_non_cacheable_ct(ct):
                return await self._passthrough_response(
                    request, up_resp, send_as_200=(not has_range and up_resp.status == 206)
                )

            if up_resp.status == 206:
                cr = parse_content_range(up_resp.headers.get("Content-Range", ""))
                if not cr:
                    await asyncio.to_thread(self._record_probe_meta, entry, up_resp)
                    return await self._passthrough_response(request, up_resp, send_as_200=(not has_range))
                u_start, u_end, u_total = cr
                stream_max_bytes: Optional[int] = u_end - u_start + 1
            else:
                cl = up_resp.headers.get("Content-Length")
                u_start = 0
                u_total = int(cl) if (cl and cl.isdigit()) else None
                u_end = (u_total - 1) if u_total is not None else None
                stream_max_bytes = u_total

            # Content deduplication: match exact total_size + SHA-256 of first 64 KB across URLs
            head_buf, head_hash = b"", None
            if u_start == 0 and await asyncio.to_thread(self.manager.check_dedup_ready, entry, u_total):
                need_len = min(HEAD_HASH_BYTES, u_total)
                chunks, got = [], 0
                while got < need_len:
                    part = await up_resp.content.read(need_len - got)
                    if not part:
                        break
                    chunks.append(part)
                    got += len(part)
                head_buf = b"".join(chunks)
                if len(head_buf) == need_len:
                    head_hash = hashlib.sha256(head_buf).hexdigest()
                    matched = await asyncio.to_thread(
                        self.manager.find_matching_entry, u_total, head_hash, exclude_key=entry.key
                    )
                    if matched is not None:
                        up_resp.close()
                        close_up_in_finally = False
                        m_ct = (await asyncio.to_thread(
                            self.manager.link_and_acquire_alias, entry.key, matched, target_url
                        )) or ct
                        try:
                            m_has_range, m_start, m_end = parse_range_header(client_range_hdr, u_total)
                            if m_start is None or m_end is None:
                                m_start, m_end = 0, u_total - 1
                            resp = await self._serve_known_range(
                                request, matched, m_has_range, m_start, m_end, u_total, m_ct,
                                upstream_headers, proxy_url, tls_verify, timeout_sec,
                            )
                        finally:
                            await asyncio.to_thread(matched.release_user)
                        await asyncio.to_thread(self.manager.maybe_evict, protect_key=matched.key)
                        return resp

            await asyncio.to_thread(self._record_probe_meta, entry, up_resp, head_hash=head_hash)

            # Hand off cold capped probe into full parallel _serve_known_range seamlessly
            if probe_capped and up_resp.status == 206 and u_start == 0 and u_total and u_end < u_total - 1:
                if head_buf:
                    await asyncio.to_thread(entry.write_chunk, 0, head_buf)
                    u_start = len(head_buf)
                    stream_max_bytes = max(0, (u_end - u_start + 1))
                close_up_in_finally = False
                resp = await self._serve_known_range(
                    request, entry, has_range, 0, u_total - 1, u_total, ct,
                    upstream_headers, proxy_url, tls_verify, timeout_sec,
                    cold_part0=(up_resp, head_buf, u_start, u_end, stream_max_bytes),
                )
                await asyncio.to_thread(self.manager.maybe_evict, protect_key=entry.key)
                return resp

            _, _, accept_ranges_flag = await asyncio.to_thread(entry.head_info)
            if up_resp.status == 206:
                cr_hdr = f"bytes {u_start}-{u_end}/{u_total if u_total is not None else '*'}" if has_range else None
                hdrs = self._media_headers(ct, u_end - u_start + 1, True, cr_hdr)
                status = 206 if has_range else 200
            else:
                hdrs = self._media_headers(ct, u_total, accept_ranges_flag)
                status = 200

            client_resp = web.StreamResponse(status=status, headers=hdrs)
            if hdrs.get("Content-Length") is None:
                client_resp.force_close()
            await client_resp.prepare(request)

            try:
                if head_buf:
                    await asyncio.to_thread(entry.write_chunk, u_start, head_buf)
                    await client_resp.write(head_buf)
                    if stream_max_bytes is not None:
                        stream_max_bytes = max(0, stream_max_bytes - len(head_buf))
                    u_start += len(head_buf)
                await self._stream_and_cache_upstream(
                    client_resp, up_resp, entry, start_offset=u_start, max_bytes=stream_max_bytes
                )
                await client_resp.write_eof()
            except DISCONNECT_ERRORS + (asyncio.CancelledError,):
                pass
            return client_resp
        finally:
            if close_up_in_finally:
                up_resp.close()
            await asyncio.to_thread(entry.save_meta, force=True)
            await asyncio.to_thread(self.manager.maybe_evict, protect_key=entry.key)

    async def _passthrough_response(
        self, request: web.Request, up_resp: aiohttp.ClientResponse, send_as_200: bool = False
    ) -> web.StreamResponse:
        status = 200 if send_as_200 else up_resp.status
        hdrs = {}
        for k, v in up_resp.headers.items():
            kl = k.lower()
            if kl in ("transfer-encoding", "connection", "keep-alive", "content-encoding"):
                continue
            if send_as_200 and kl == "content-range":
                continue
            hdrs[k] = v

        client_resp = web.StreamResponse(status=status, headers=hdrs)
        if "content-length" not in {k.lower() for k in hdrs}:
            client_resp.force_close()
        await client_resp.prepare(request)
        try:
            async for chunk in up_resp.content.iter_any():
                await client_resp.write(chunk)
            await client_resp.write_eof()
        except DISCONNECT_ERRORS + (asyncio.CancelledError,):
            pass
        return client_resp

    async def _stream_and_cache_upstream(
        self,
        client_resp: web.StreamResponse,
        up_resp: aiohttp.ClientResponse,
        entry: CacheEntry,
        start_offset: int,
        max_bytes: Optional[int],
        skip_leading: int = 0,
    ) -> int:
        offset, forwarded, remaining, to_skip = start_offset, 0, max_bytes, skip_leading
        try:
            async for chunk in up_resp.content.iter_any():
                if remaining is not None:
                    if remaining <= 0:
                        break
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]
                    remaining -= len(chunk)
                await asyncio.to_thread(entry.write_chunk, offset, chunk)
                offset += len(chunk)
                if to_skip >= len(chunk):
                    to_skip -= len(chunk)
                    continue
                out = chunk[to_skip:] if to_skip > 0 else chunk
                to_skip = 0
                await client_resp.write(out)
                forwarded += len(out)
                if remaining is not None and remaining <= 0:
                    break
        except DISCONNECT_ERRORS + (asyncio.CancelledError,):
            pass
        return forwarded

    async def _serve_known_range(
        self,
        request: web.Request,
        entry: CacheEntry,
        has_range: bool,
        req_start: int,
        req_end: int,
        total_size: int,
        content_type: str,
        upstream_headers: dict,
        proxy_url: Optional[str],
        tls_verify: bool,
        timeout_sec: float,
        cold_part0: Optional[Tuple[aiohttp.ClientResponse, bytes, int, int, int]] = None,
    ) -> web.StreamResponse:
        if cold_part0 is not None:
            up0, head_buf, u0_start, u0_end, _ = cold_part0
            segments = [("net", 0, req_end)]
        else:
            up0, head_buf, u0_start, u0_end = None, b"", 0, 0
            segments = await asyncio.to_thread(entry.snapshot_plan, req_start, req_end)

        # Prefetch first network segment before committing headers so upstream 4xx/5xx propagate
        prefetched_resp: Optional[aiohttp.ClientResponse] = up0
        if prefetched_resp is None and segments and segments[0][0] == "net":
            _, seg_s, seg_e = segments[0]
            _, _, accept_ranges_flag = await asyncio.to_thread(entry.head_info)
            first_end = min(seg_e, seg_s + self.part_size - 1) if (
                self.connections > 1 and accept_ranges_flag and (seg_e - seg_s + 1) > self.part_size
            ) else seg_e
            rng = f"bytes={seg_s}-" if first_end == total_size - 1 else f"bytes={seg_s}-{first_end}"
            try:
                prefetched_resp = await self._fetch_upstream(
                    "GET", entry.url, upstream_headers, proxy_url, tls_verify, timeout_sec, range_header=rng
                )
            except Exception as e:
                return web.Response(status=502, text=f"Upstream error: {e}")
            if prefetched_resp.status not in (200, 206):
                try:
                    return await self._passthrough_response(request, prefetched_resp)
                finally:
                    prefetched_resp.close()

        cr_hdr = f"bytes {req_start}-{req_end}/{total_size}" if has_range else None
        hdrs = self._media_headers(content_type, req_end - req_start + 1, True, cr_hdr)
        client_resp = web.StreamResponse(status=206 if has_range else 200, headers=hdrs)
        await client_resp.prepare(request)
        await asyncio.to_thread(entry.touch)

        try:
            for idx, (kind, seg_s, seg_e) in enumerate(segments):
                if kind == "cache":
                    pos = seg_s
                    while pos <= seg_e:
                        data = await asyncio.to_thread(
                            entry.read_chunk, pos, min(self.chunk_size, seg_e - pos + 1)
                        )
                        if not data:
                            await self._fetch_net_segment(
                                client_resp, entry, pos, seg_e, total_size,
                                upstream_headers, proxy_url, tls_verify, timeout_sec,
                            )
                            break
                        await client_resp.write(data)
                        pos += len(data)
                else:
                    resp0 = prefetched_resp if idx == 0 else None
                    c_info = (head_buf, u0_start, u0_end) if (idx == 0 and cold_part0 is not None) else None
                    if idx == 0:
                        prefetched_resp = None
                    ok = await self._fetch_net_segment(
                        client_resp, entry, seg_s, seg_e, total_size,
                        upstream_headers, proxy_url, tls_verify, timeout_sec,
                        existing_resp=resp0, cold_part0_info=c_info,
                    )
                    if not ok:
                        break
            await client_resp.write_eof()
        except DISCONNECT_ERRORS + (asyncio.CancelledError,):
            pass
        finally:
            if prefetched_resp is not None:
                prefetched_resp.close()
            await asyncio.to_thread(entry.save_meta, force=True)
        return client_resp

    async def _fetch_net_segment(
        self,
        client_resp: web.StreamResponse,
        entry: CacheEntry,
        seg_s: int,
        seg_e: int,
        total_size: int,
        upstream_headers: dict,
        proxy_url: Optional[str],
        tls_verify: bool,
        timeout_sec: float,
        existing_resp: Optional[aiohttp.ClientResponse] = None,
        cold_part0_info: Optional[Tuple[bytes, int, int]] = None,
    ) -> bool:
        total_needed = seg_e - seg_s + 1
        _, _, accept_ranges_flag = await asyncio.to_thread(entry.head_info)
        use_parallel = (
            self.connections > 1
            and accept_ranges_flag
            and (cold_part0_info is not None or total_needed > self.part_size)
            and (existing_resp is None or existing_resp.status == 206)
        )
        if use_parallel:
            return await self._fetch_parallel_net_segment(
                client_resp, entry, seg_s, seg_e, total_size,
                upstream_headers, proxy_url, tls_verify, timeout_sec,
                existing_resp=existing_resp, cold_part0_info=cold_part0_info,
            )

        resp = existing_resp
        if resp is None:
            rng = f"bytes={seg_s}-" if seg_e == total_size - 1 else f"bytes={seg_s}-{seg_e}"
            try:
                resp = await self._fetch_upstream(
                    "GET", entry.url, upstream_headers, proxy_url, tls_verify, timeout_sec, range_header=rng
                )
            except Exception:
                return False

        try:
            if resp.status == 206:
                cr = parse_content_range(resp.headers.get("Content-Range", ""))
                if cr:
                    u_start, u_end, u_total = cr
                    if u_total is not None and u_total != total_size:
                        await asyncio.to_thread(entry.invalidate)
                        return False
                    if u_start > seg_s:
                        return False
                    skip = seg_s - u_start
                    got = await self._stream_and_cache_upstream(
                        client_resp, resp, entry, start_offset=u_start,
                        max_bytes=min(u_end - u_start + 1, skip + total_needed), skip_leading=skip,
                    )
                else:
                    got = await self._stream_and_cache_upstream(
                        client_resp, resp, entry, start_offset=seg_s, max_bytes=total_needed
                    )
                return got == total_needed

            if resp.status == 200:
                got = await self._stream_and_cache_upstream(
                    client_resp, resp, entry, start_offset=0, max_bytes=seg_e + 1, skip_leading=seg_s
                )
                return got == total_needed
            return False
        finally:
            resp.close()

    async def _fetch_parallel_net_segment(
        self,
        client_resp: web.StreamResponse,
        entry: CacheEntry,
        seg_s: int,
        seg_e: int,
        total_size: int,
        upstream_headers: dict,
        proxy_url: Optional[str],
        tls_verify: bool,
        timeout_sec: float,
        existing_resp: Optional[aiohttp.ClientResponse] = None,
        cold_part0_info: Optional[Tuple[bytes, int, int]] = None,
    ) -> bool:
        """Download large uncached spans across N concurrent aiohttp Range workers in sliding order."""
        part_size = self.part_size
        total_needed = seg_e - seg_s + 1

        # Keep part 0 aligned to existing cold probe end if already opened
        part0_end = cold_part0_info[2] if cold_part0_info is not None else min(seg_e, seg_s + part_size - 1)
        part_bounds: List[Tuple[int, int]] = [(seg_s, part0_end)]
        cur = part0_end + 1
        while cur <= seg_e:
            nxt = min(seg_e, cur + part_size - 1)
            part_bounds.append((cur, nxt))
            cur = nxt + 1

        num_parts = len(part_bounds)
        max_ahead = max(self.connections * 2, self.connections + 2)
        cond = asyncio.Condition()
        part_chunks: Dict[int, deque] = {}
        part_done: Dict[int, bool] = {}
        cur_idx = 0
        next_idx = 0
        stopped = False

        if cold_part0_info is not None and cold_part0_info[0]:
            part_chunks[0] = deque([cold_part0_info[0]])

        async def download_part(idx: int, p_s: int, p_e: int, pre_resp: Optional[aiohttp.ClientResponse] = None) -> bool:
            resp = pre_resp
            read_start = cold_part0_info[1] if (idx == 0 and cold_part0_info is not None) else p_s
            need_from_net = p_e - read_start + 1
            if need_from_net <= 0:
                if resp is not None:
                    resp.close()
                return True

            for attempt in range(2):
                if stopped:
                    if resp is not None:
                        resp.close()
                    return False
                if resp is None:
                    rng = f"bytes={read_start}-{p_e}"
                    try:
                        resp = await self._fetch_upstream(
                            "GET", entry.url, upstream_headers, proxy_url, tls_verify, timeout_sec, range_header=rng
                        )
                    except Exception:
                        if attempt == 0:
                            await asyncio.sleep(0.15)
                            continue
                        return False
                try:
                    if resp.status not in (200, 206):
                        return False
                    u_start = read_start
                    to_skip = 0
                    max_recv = need_from_net
                    if resp.status == 206:
                        cr = parse_content_range(resp.headers.get("Content-Range", ""))
                        if cr:
                            if cr[2] is not None and cr[2] != total_size:
                                await asyncio.to_thread(entry.invalidate)
                                return False
                            if cr[0] > read_start:
                                return False
                            u_start = cr[0]
                            to_skip = read_start - u_start
                            max_recv = min(cr[1] - u_start + 1, to_skip + need_from_net)
                    elif resp.status == 200:
                        u_start = 0
                        to_skip = read_start
                        max_recv = p_e + 1

                    off, remaining, forwarded = u_start, max_recv, 0
                    async for chunk in resp.content.iter_any():
                        if stopped:
                            return False
                        if len(chunk) > remaining:
                            chunk = chunk[:remaining]
                        remaining -= len(chunk)
                        await asyncio.to_thread(entry.write_chunk, off, chunk)
                        off += len(chunk)
                        if to_skip >= len(chunk):
                            to_skip -= len(chunk)
                        else:
                            out = chunk[to_skip:] if to_skip > 0 else chunk
                            to_skip = 0
                            forwarded += len(out)
                            async with cond:
                                part_chunks.setdefault(idx, deque()).append(out)
                                cond.notify_all()
                        if remaining <= 0:
                            break
                    if forwarded == need_from_net:
                        return True
                    read_start += forwarded
                    need_from_net -= forwarded
                except DISCONNECT_ERRORS + (asyncio.CancelledError,):
                    return False
                finally:
                    resp.close()
                    resp = None
            return False

        async def worker(first_resp: Optional[aiohttp.ClientResponse] = None) -> None:
            nonlocal next_idx
            my_pre = first_resp
            while not stopped:
                async with cond:
                    while not stopped and next_idx < num_parts and next_idx >= cur_idx + max_ahead:
                        await cond.wait()
                    if stopped or next_idx >= num_parts:
                        if my_pre is not None:
                            my_pre.close()
                        return
                    my_idx = next_idx
                    next_idx += 1
                p_s, p_e = part_bounds[my_idx]
                ok = await download_part(my_idx, p_s, p_e, my_pre if my_idx == 0 else None)
                my_pre = None
                async with cond:
                    part_done[my_idx] = ok
                    cond.notify_all()

        n_workers = min(self.connections, num_parts)
        workers = [
            asyncio.create_task(worker(existing_resp if i == 0 else None))
            for i in range(n_workers)
        ]

        try:
            for idx in range(num_parts):
                while True:
                    async with cond:
                        q = part_chunks.get(idx)
                        while not stopped and (not q) and (idx not in part_done):
                            await cond.wait()
                            q = part_chunks.get(idx)
                        batch = list(q) if q else []
                        if q:
                            q.clear()
                        done_flag = part_done.get(idx)
                    for chunk in batch:
                        await client_resp.write(chunk)
                    if done_flag is True:
                        async with cond:
                            part_chunks.pop(idx, None)
                            part_done.pop(idx, None)
                            cur_idx = idx + 1
                            cond.notify_all()
                        break
                    if done_flag is False or stopped:
                        return False
            return True
        except DISCONNECT_ERRORS + (asyncio.CancelledError,):
            return False
        finally:
            stopped = True
            async with cond:
                cond.notify_all()
            for t in workers:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if existing_resp is not None and not existing_resp.closed:
                existing_resp.close()


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


async def run_server(args, manager: CacheManager, server_info_path: str) -> None:
    proxy_srv = AsyncRangeProxy(manager, args.chunk_size, args.connections, args.part_size_kb)
    await proxy_srv.start_session()
    runner = web.AppRunner(proxy_srv.build_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port, backlog=128)
    await site.start()

    actual_port = site._server.sockets[0].getsockname()[1]
    proxy_srv.port = actual_port
    await asyncio.to_thread(
        _atomic_write_json,
        server_info_path,
        {"pid": os.getpid(), "port": actual_port, "started_at": time.time()},
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(*_args):
        try:
            loop.call_soon_threadsafe(stop_event.set)
        except Exception:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except Exception:
            pass

    watchdog_task = None
    if args.idle_timeout > 0:
        async def idle_watchdog():
            while not stop_event.is_set():
                await asyncio.sleep(5.0)
                idle_for = time.monotonic() - proxy_srv.last_activity
                if proxy_srv.active_requests == 0 and idle_for >= args.idle_timeout:
                    stop_event.set()
                    break
        watchdog_task = asyncio.create_task(idle_watchdog())

    try:
        await stop_event.wait()
    finally:
        if watchdog_task is not None and not watchdog_task.done():
            watchdog_task.cancel()
        await asyncio.to_thread(manager.flush_all)
        await proxy_srv.close_session()
        await runner.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="mpv Async HTTP 206 Range Cache Proxy")
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--idle-timeout", type=int, default=300)
    parser.add_argument("--max-cache-mb", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=65536)
    parser.add_argument("--connections", type=int, default=4)
    parser.add_argument("--part-size-kb", type=int, default=2048)
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
    server_info_path = os.path.join(args.cache_dir, "server.json")

    try:
        asyncio.run(run_server(args, manager, server_info_path))
    except KeyboardInterrupt:
        pass
    finally:
        manager.flush_all()
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
