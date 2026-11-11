#!/usr/bin/env bash
set -e

REPO="${MPV_HTTP_RANGE_CACHE_REPO:-Arsks12/mpv-http-range-cache}"
zip_url="https://github.com/$REPO/archive/refs/heads/main.zip"
conf_url="https://raw.githubusercontent.com/$REPO/HEAD/http_range_cache.conf"
target_rel="scripts/http-range-cache"
zip_file="/tmp/http-range-cache.zip"
extract_dir="/tmp/http-range-cache-extract"
dependencies=(curl unzip)

abort() {
	echo "Error: $1"
	echo "Aborting!"
	rm -f "$zip_file" || true
	rm -rf "$extract_dir" || true
	rm -rf "$config_dir/$target_rel" || true
	if [[ -e "$backup_dir/$target_rel" ]]; then
		mkdir -p "$(dirname "$config_dir/$target_rel")" || true
		mv "$backup_dir/$target_rel" "$config_dir/$target_rel" || true
	fi
	rm -rf "$backup_dir" || true
	exit 1
}

missing_dependencies=()
for name in "${dependencies[@]}"; do
	if [ ! -x "$(command -v "$name")" ]; then
		missing_dependencies+=("$name")
	fi
done
if [ "${#missing_dependencies[@]}" -ne 0 ]; then
	echo "Missing dependencies: ${missing_dependencies[*]}"
	exit 1
fi

OS="$(uname)"
if [ -n "${MPV_CONFIG_DIR:-}" ]; then
	echo "Installing into (MPV_CONFIG_DIR):"
	config_dir="${MPV_CONFIG_DIR}"
elif [ "$OS" == "Linux" ]; then
	if [ -d "$HOME/.var/app/io.mpv.Mpv" ]; then
		echo "Installing into (flatpak io.mpv.Mpv package):"
		config_dir="$HOME/.var/app/io.mpv.Mpv/config/mpv"
	elif [ -d "$HOME/snap/mpv" ]; then
		echo "Installing into (snap mpv package):"
		config_dir="$HOME/snap/mpv/current/.config/mpv"
	elif [ -d "$HOME/snap/mpv-wayland" ]; then
		echo "Installing into (snap mpv-wayland package):"
		config_dir="$HOME/snap/mpv-wayland/common/.config/mpv"
	else
		echo "Config location:"
		config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/mpv"
	fi
elif [ "$OS" == "Darwin" ]; then
	config_dir="$HOME/.config/mpv"
else
	abort "This install script works only on Linux and macOS."
fi

backup_dir="$config_dir/.http-range-cache-backup"
echo "-> $config_dir"
mkdir -p "$config_dir" || abort "Couldn't create config directory."

echo "Backing up..."
rm -rf "$backup_dir" || abort "Couldn't cleanup backup directory."
if [[ -e "$config_dir/$target_rel" ]]; then
	mkdir -p "$(dirname "$backup_dir/$target_rel")" || abort "Couldn't create backup folder."
	mv "$config_dir/$target_rel" "$backup_dir/$target_rel" || abort "Couldn't move existing install to backup."
fi

echo "Downloading archive..."
curl -fsSL -o "$zip_file" "$zip_url" || abort "Couldn't download: $zip_url"

echo "Extracting archive..."
rm -rf "$extract_dir"
unzip -qod "$extract_dir" "$zip_file" || abort "Couldn't extract: $zip_file"
root_folder="$(find "$extract_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
[ -n "$root_folder" ] || abort "Archive was empty."
mkdir -p "$config_dir/scripts" || abort "Couldn't create scripts directory."
mv "$root_folder" "$config_dir/$target_rel" || abort "Couldn't move extracted folder."

rm -f "$zip_file" || true
rm -rf "$extract_dir" || true
rm -rf "$backup_dir" || true

scriptopts_dir="$config_dir/script-opts"
conf_file="$scriptopts_dir/http_range_cache.conf"
if [ ! -f "$conf_file" ]; then
	echo "Config not found, installing default http_range_cache.conf..."
	mkdir -p "$scriptopts_dir" || echo "Couldn't create: $scriptopts_dir"
	if [ -f "$config_dir/$target_rel/http_range_cache.conf" ]; then
		cp "$config_dir/$target_rel/http_range_cache.conf" "$conf_file"
	else
		curl -fsSL -o "$conf_file" "$conf_url" || echo "Warning: Couldn't download default config."
	fi
fi

py_cmd="$(command -v python3 || command -v python || true)"
if [ -n "$py_cmd" ]; then
	if ! "$py_cmd" -c "import requests" >/dev/null 2>&1; then
		echo "Note: Python 'requests' module missing. Install with: $py_cmd -m pip install requests"
	fi
else
	echo "Note: Python 3 not found in PATH. Install Python 3 + 'pip install requests' to run proxy.py."
fi

echo "mpv-http-range-cache has been installed."

