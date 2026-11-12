-- HTTP 206 Range Cache for mpv
local mp = require("mp")
local msg = require("mp.msg")
local utils = require("mp.utils")
local options = require("mp.options")

local opts = {
    enabled = true,
    cache_dir = "~~/cache/http-range-cache",
    idle_timeout = 300,
    max_cache_size_mb = 0,
    chunk_size = 65536,
    connections = 4,
    part_size_kb = 2048,
    flush_interval_kb = 1024,
    ignore_query_params = "",
    match_by_content = true,
    python_path = "auto",
    show_osd = true,
    debug_log = false,
}
options.read_options(opts, "http_range_cache")

local IS_WIN = package.config:sub(1, 1) == "\\"
local script_dir = mp.get_script_directory() or utils.split_path((debug.getinfo(1, "S").source or ""):gsub("^@", ""))
local proxy_script = utils.join_path(script_dir, "proxy.py")
local cache_dir = mp.command_native({"expand-path", opts.cache_dir})
local server_info_path = utils.join_path(cache_dir, "server.json")

local runtime_enabled = opts.enabled
local resolved_python, has_curl, active_port = nil, nil, nil
local last_health_at, starting_proxy, pending_waiters = -1000, false, {}
local active_load_state, current_loaded_url, last_time_pos, last_synced_pos, last_orig_url = nil, nil, nil, nil, nil
local last_proxied_by_path = {}

local function url_encode(str)
    return (str:gsub("([^%w%-_%.~])", function(c) return string.format("%%%02X", c:byte()) end))
end

local function format_bytes(b)
    if not b or b < 0 then return "0 B" end
    local units, i = { "B", "KB", "MB", "GB", "TB" }, 1
    while b >= 1024 and i < #units do b, i = b / 1024, i + 1 end
    return i == 1 and string.format("%d B", b) or string.format("%.2f %s", b, units[i])
end

local function format_time(sec)
    local t = math.max(0, math.floor(sec or 0))
    local h, m, s = math.floor(t / 3600), math.floor((t % 3600) / 60), t % 60
    return h > 0 and string.format("%02d:%02d:%02d", h, m, s) or string.format("%02d:%02d", m, s)
end

local function get_watch_later_dir()
    local d = mp.get_property("current-watch-later-dir")
    if d and d ~= "" then return d end
    local cfg = mp.get_property("watch-later-dir")
    return mp.command_native({"expand-path", (cfg and cfg ~= "") and cfg or "~~/watch_later"})
end

local function is_cacheable_url(url)
    if not url or url == "" then return false end
    local lower = url:lower()
    if not lower:match("^https?://") then return false end
    local host = lower:match("^https?://([^/%?:]+)")
    if host == "127.0.0.1" or host == "localhost" or host == "[::1]" or host == "::1" then return false end
    local path_part = lower:match("^https?://[^/%?#]+([^%?#]*)") or ""
    return not (path_part:match("%.m3u8?$") or path_part:match("%.mpd$") or path_part:match("%.pls$"))
end

local function run_subproc(args, cb)
    local req = { name = "subprocess", playback_only = false, capture_stdout = true, capture_stderr = true, args = args }
    if not cb then return mp.command_native(req) end
    mp.command_native_async(req, function(ok, res)
        cb(ok and res and res.status == 0, (res and res.stdout) or "")
    end)
end

local function detect_python()
    if resolved_python then return resolved_python end
    if opts.python_path ~= "" and opts.python_path ~= "auto" then
        resolved_python = opts.python_path
        return resolved_python
    end
    for _, cmd in ipairs(IS_WIN and { "pythonw", "python", "python3" } or { "python3", "python" }) do
        local res = run_subproc({ cmd, "-c", "import aiohttp; print('ok')" })
        if res and res.status == 0 and (res.stdout or ""):match("ok") then
            resolved_python = cmd
            return cmd
        end
    end
    return nil
end

local function http_req(method, port, path_q, cb)
    local url = "http://127.0.0.1:" .. tostring(port) .. path_q
    if has_curl == nil then
        local r = run_subproc({ "curl", "--version" })
        has_curl = (r ~= nil and r.status == 0)
    end
    local args
    if has_curl then
        args = { "curl", "-sf", "--noproxy", "*", "--connect-timeout", "1", "--max-time", "2", "-X", method, url }
    else
        local py = detect_python()
        if not py then
            if cb then cb(false, "") end
            return false, ""
        end
        local py_cmd = IS_WIN and (py == "pythonw" and "python" or py) or py
        local code = "import urllib.request; print(urllib.request.urlopen(urllib.request.Request('"
            .. url .. "', method='" .. method .. "'), timeout=2).read().decode('utf-8'))"
        args = { py_cmd, "-c", code }
    end
    if cb then
        run_subproc(args, cb)
        return
    end
    local res = run_subproc(args)
    return (res ~= nil and res.status == 0), (res and res.stdout or "")
end

local function read_server_port()
    local f = io.open(server_info_path, "r")
    if not f then return nil end
    local data = utils.parse_json(f:read("*a") or "")
    f:close()
    return (type(data) == "table" and type(data.port) == "number" and data.port > 0) and data.port or nil
end

local function finish_waiters(port)
    starting_proxy = false
    local waiters = pending_waiters
    pending_waiters = {}
    for _, cb in ipairs(waiters) do cb(port) end
end

local function spawn_proxy()
    local py = detect_python()
    if not py then
        msg.error("Python with 'aiohttp' not found; HTTP range caching disabled")
        return false
    end
    local args = {
        py, proxy_script,
        "--cache-dir", cache_dir,
        "--idle-timeout", tostring(opts.idle_timeout),
        "--max-cache-mb", tostring(opts.max_cache_size_mb),
        "--chunk-size", tostring(opts.chunk_size),
        "--connections", tostring(opts.connections),
        "--part-size-kb", tostring(opts.part_size_kb),
        "--flush-kb", tostring(opts.flush_interval_kb),
        "--ignore-params", opts.ignore_query_params or "",
    }
    if not opts.match_by_content then table.insert(args, "--no-match-by-content") end
    if opts.debug_log then
        table.insert(args, "--log-file")
        table.insert(args, utils.join_path(cache_dir, "proxy.log"))
    end
    local res = mp.command_native({ name = "subprocess", playback_only = false, detach = true, args = args })
    if not res or res.status ~= 0 then
        msg.error("Failed to launch proxy.py: " .. tostring(res and res.error_string))
        return false
    end
    return true
end

local function poll_until_ready(attempts)
    local port = read_server_port()
    local function retry()
        if attempts > 0 then
            mp.add_timeout(0.05, function() poll_until_ready(attempts - 1) end)
        else
            active_port = nil
            finish_waiters(nil)
        end
    end
    if not port then return retry() end
    http_req("GET", port, "/__health", function(ok)
        if ok then
            active_port, last_health_at = port, mp.get_time()
            finish_waiters(port)
        else
            retry()
        end
    end)
end

local function ensure_proxy(cb)
    if active_port and (mp.get_time() - last_health_at) < 20 then return cb(active_port) end
    table.insert(pending_waiters, cb)
    if starting_proxy then return end
    starting_proxy = true

    local function launch()
        if spawn_proxy() then mp.add_timeout(0.05, function() poll_until_ready(80) end) else finish_waiters(nil) end
    end
    local port = active_port or read_server_port()
    if not port then return launch() end
    http_req("GET", port, "/__health", function(ok)
        if ok then
            active_port, last_health_at = port, mp.get_time()
            finish_waiters(port)
        else
            active_port = nil
            launch()
        end
    end)
end

mp.add_hook("on_load", 50, function(hook)
    active_load_state = nil
    local orig_url = runtime_enabled and mp.get_property("stream-open-filename") or nil
    if not is_cacheable_url(orig_url) then return end

    hook:defer()
    ensure_proxy(function(port)
        if not port then
            msg.warn("HTTP range cache proxy unavailable; falling back to direct playback")
            return hook:cont()
        end
        local orig_proxy = mp.get_property("http-proxy") or ""
        local orig_hdrs = mp.get_property_native("http-header-fields") or {}
        local new_hdrs = {}
        for _, h in ipairs(orig_hdrs) do
            if type(h) == "string" and not h:lower():match("^x%-mpv%-cache%-") then table.insert(new_hdrs, h) end
        end
        if orig_proxy ~= "" then table.insert(new_hdrs, "X-Mpv-Cache-Proxy: " .. orig_proxy) end
        if not mp.get_property_bool("tls-verify", true) then table.insert(new_hdrs, "X-Mpv-Cache-Tls-Verify: 0") end
        table.insert(new_hdrs, "X-Mpv-Cache-Timeout: " .. tostring(mp.get_property_number("network-timeout") or 60))

        local proxied_url = "http://127.0.0.1:" .. tostring(port) .. "/stream?url=" .. url_encode(orig_url)
        mp.set_property("file-local-options/http-proxy", "")
        mp.set_property_native("file-local-options/http-header-fields", new_hdrs)
        mp.set_property("stream-open-filename", proxied_url)

        local logical_path = mp.get_property("path") or orig_url
        active_load_state = {
            orig_url = orig_url, orig_proxy = orig_proxy, orig_hdrs = orig_hdrs, proxied_url = proxied_url,
        }
        last_proxied_by_path[logical_path] = orig_url
        last_orig_url = orig_url
        hook:cont()
    end)
end)

-- Restore original URL/proxy before ytdl_hook's on_load_fail (priority 10)
mp.add_hook("on_load_fail", 5, function()
    local s = active_load_state
    active_load_state = nil
    if s and mp.get_property("stream-open-filename") == s.proxied_url then
        mp.set_property("stream-open-filename", s.orig_url)
        mp.set_property("file-local-options/http-proxy", s.orig_proxy)
        mp.set_property_native("file-local-options/http-header-fields", s.orig_hdrs)
    end
end)

local function sync_watch_later(url, reason, sync_mode)
    if not url or not active_port or not mp.get_property_bool("save-position-on-quit", false) then return end
    local pos = mp.get_property_number("time-pos") or last_time_pos
    local q = "/__watch_later?url=" .. url_encode(url)
        .. "&wl_dir=" .. url_encode(get_watch_later_dir())
        .. "&reason=" .. url_encode(reason or "")
        .. ((pos and pos > 0) and ("&pos=" .. string.format("%.6f", pos)) or "")
    http_req("POST", active_port, q, sync_mode and nil or function() end)
end

mp.observe_property("time-pos", "number", function(_, val)
    if not val or not current_loaded_url then return end
    last_time_pos = val
    if last_synced_pos == nil or math.abs(val - last_synced_pos) >= 15 then
        last_synced_pos = val
        sync_watch_later(current_loaded_url, "periodic", false)
    end
end)

mp.register_event("file-loaded", function()
    local state = active_load_state
    active_load_state = nil
    current_loaded_url = state and state.orig_url or nil
    last_time_pos, last_synced_pos = nil, nil
    if not state or not active_port then return end

    local wl_msg = nil
    local function show_combined_osd(cache_msg)
        if not opts.show_osd then return end
        local text = (wl_msg and cache_msg) and (wl_msg .. "\n" .. cache_msg) or (wl_msg or cache_msg)
        if text then mp.osd_message(text, wl_msg and 3 or 2.5) end
    end

    if mp.get_property_bool("resume-playback", true) then
        local q = "/__watch_later?url=" .. url_encode(state.orig_url) .. "&wl_dir=" .. url_encode(get_watch_later_dir())
        http_req("GET", active_port, q, function(ok, body)
            local data = ok and body ~= "" and utils.parse_json(body) or nil
            if type(data) ~= "table" or not data.resume or type(data.start) ~= "number" or data.start <= 1 then return end
            if (mp.get_property_number("time-pos", 0) or 0) >= 5 then return end
            if type(data.props) == "table" then
                for _, k in ipairs({ "sid", "aid", "sub-delay", "audio-delay" }) do
                    if data.props[k] ~= nil then mp.set_property(k, tostring(data.props[k])) end
                end
            end
            mp.commandv("seek", tostring(data.start), "absolute")
            wl_msg = "Resumed from " .. format_time(data.start) .. " (cached match)"
            show_combined_osd(nil)
        end)
    end

    if not opts.show_osd then return end
    http_req("GET", active_port, "/__status?url=" .. url_encode(state.orig_url), function(ok, body)
        local data = ok and body ~= "" and utils.parse_json(body) or nil
        local cur = type(data) == "table" and type(data.current) == "table" and data.current or nil
        if not cur or (cur.cached_bytes or 0) <= 0 then return end
        if cur.total_size and cur.cached_bytes >= cur.total_size then
            show_combined_osd(string.format("HTTP Cache: 100%% cached (%s)", format_bytes(cur.total_size)))
        elseif (cur.percent or 0) >= 0.1 then
            show_combined_osd(string.format("HTTP Cache: %.1f%% cached (%s)", cur.percent, format_bytes(cur.cached_bytes)))
        end
    end)
end)

mp.register_event("end-file", function(ev)
    if current_loaded_url then sync_watch_later(current_loaded_url, ev and ev.reason or "", true) end
    current_loaded_url, last_time_pos, last_synced_pos = nil, nil, nil
end)

local function get_current_target_url()
    local path = mp.get_property("path")
    if path and last_proxied_by_path[path] then return last_proxied_by_path[path] end
    return is_cacheable_url(path) and path or last_orig_url
end

local function cmd_cache_status()
    ensure_proxy(function(port)
        if not port then return mp.osd_message("HTTP Cache: proxy not running", 2.5) end
        local target = get_current_target_url()
        local q = "/__status" .. (target and ("?url=" .. url_encode(target)) or "")
        http_req("GET", port, q, function(ok, body)
            local data = ok and utils.parse_json(body) or nil
            if type(data) ~= "table" then return mp.osd_message("HTTP Cache: status unavailable", 2.5) end
            local cur = type(data.current) == "table" and data.current or nil
            local line1 = cur and string.format(
                "Current: %s / %s (%.1f%%, %d range%s)",
                format_bytes(cur.cached_bytes or 0),
                cur.total_size and format_bytes(cur.total_size) or "unknown",
                cur.percent or 0, cur.ranges_count or 0, cur.ranges_count == 1 and "" or "s"
            ) or "Current stream: not cached"
            local n = data.entries_count or 0
            local line2 = string.format("Total Cache: %s across %d file%s", format_bytes(data.total_cached_bytes or 0), n, n == 1 and "" or "s")
            mp.osd_message(line1 .. "\n" .. line2, 4)
        end)
    end)
end

local function cmd_clear(all_entries)
    local target = not all_entries and get_current_target_url() or nil
    if not all_entries and not target then return mp.osd_message("HTTP Cache: no network stream URL", 2) end
    ensure_proxy(function(port)
        if not port then return end
        local q = all_entries and "/__clear_all" or ("/__clear?url=" .. url_encode(target))
        local label = all_entries and "HTTP Cache: cleared all cached streams" or "HTTP Cache: cleared current stream"
        http_req("POST", port, q, function(ok) mp.osd_message(ok and label or "HTTP Cache: clear failed", 2.5) end)
    end)
end

local commands = {
    { "http-range-cache-status", "cache-status", cmd_cache_status },
    { "http-range-cache-clear-current", "clear-current-cache", function() cmd_clear(false) end },
    { "http-range-cache-clear-all", "clear-all-cache", function() cmd_clear(true) end },
    { "http-range-cache-toggle", "toggle-cache", function()
        runtime_enabled = not runtime_enabled
        mp.osd_message("HTTP Range Cache: " .. (runtime_enabled and "ON" or "OFF"), 2)
    end },
}
for _, c in ipairs(commands) do
    mp.register_script_message(c[1], c[3])
    mp.add_key_binding(nil, c[2], c[3])
end

if runtime_enabled then
    mp.add_timeout(0.05, function() ensure_proxy(function() end) end)
end
