$Repo = if ($env:MPV_HTTP_RANGE_CACHE_REPO) { $env:MPV_HTTP_RANGE_CACHE_REPO } else { "Arsks12/mpv-http-range-cache" }
$ZipURL = "https://github.com/$Repo/archive/refs/heads/main.zip"
$ConfURL = "https://raw.githubusercontent.com/$Repo/HEAD/http_range_cache.conf"
$TargetRel = "scripts/http-range-cache"

# Determine install directory
if (Test-Path env:MPV_CONFIG_DIR) {
	Write-Output "Installing into (MPV_CONFIG_DIR):"
	$ConfigDir = "$env:MPV_CONFIG_DIR"
}
elseif (Test-Path "$PWD/portable_config") {
	Write-Output "Installing into (portable config):"
	$ConfigDir = "$PWD/portable_config"
}
elseif ((Get-Item -Path $PWD).BaseName -eq "portable_config") {
	Write-Output "Installing into (portable config):"
	$ConfigDir = "$PWD"
}
else {
	Write-Output "Installing into (current user config):"
	$ConfigDir = "$env:APPDATA/mpv"
}

Write-Output "-> $ConfigDir"

$BackupDir = "$ConfigDir/.http-range-cache-backup"
$ExtractDir = "$ConfigDir/.http-range-cache-extract"
$ZipFile = "$ConfigDir/http_range_cache_tmp.zip"

function DeleteIfExists($Path) {
	if (Test-Path -LiteralPath $Path) {
		Remove-Item -LiteralPath $Path -Force -Recurse > $null
	}
}

function Abort($Message) {
	Write-Output "Error: $Message"
	Write-Output "Aborting!"

	DeleteIfExists $ZipFile
	DeleteIfExists $ExtractDir

	Write-Output "Restoring backup..."
	DeleteIfExists "$ConfigDir/$TargetRel"
	if (Test-Path -LiteralPath "$BackupDir/$TargetRel") {
		New-Item -ItemType Directory -Force -Path (Split-Path "$ConfigDir/$TargetRel" -Parent) > $null
		Move-Item -LiteralPath "$BackupDir/$TargetRel" -Destination "$ConfigDir/$TargetRel" -Force > $null
	}
	DeleteIfExists $BackupDir
	Exit 1
}

if (Test-Path -Path $ConfigDir -PathType Leaf) {
	Abort "Config directory is a file."
}
New-Item -ItemType Directory -Force -Path $ConfigDir > $null

Write-Output "Backing up..."
DeleteIfExists $BackupDir
if (Test-Path -LiteralPath "$ConfigDir/$TargetRel") {
	$ToDir = Split-Path "$BackupDir/$TargetRel" -Parent
	New-Item -ItemType Directory -Force -Path $ToDir > $null
	Move-Item -LiteralPath "$ConfigDir/$TargetRel" -Destination "$BackupDir/$TargetRel" -Force > $null
}

Write-Output "Downloading archive..."
try {
	Invoke-WebRequest -OutFile $ZipFile -Uri $ZipURL > $null
}
catch {
	Abort "Couldn't download: $ZipURL"
}

Write-Output "Extracting archive..."
try {
	DeleteIfExists $ExtractDir
	Expand-Archive $ZipFile -DestinationPath $ExtractDir -Force > $null
	$RootFolder = Get-ChildItem -Path $ExtractDir -Directory | Select-Object -First 1
	if (-not $RootFolder) {
		Abort "Archive was empty."
	}
	New-Item -ItemType Directory -Force -Path "$ConfigDir/scripts" > $null
	Move-Item -LiteralPath $RootFolder.FullName -Destination "$ConfigDir/$TargetRel" -Force > $null
}
catch {
	Abort "Couldn't extract: $ZipFile"
}

DeleteIfExists $ZipFile
DeleteIfExists $ExtractDir
DeleteIfExists $BackupDir

$ScriptOptsDir = "$ConfigDir/script-opts"
$ConfFile = "$ScriptOptsDir/http_range_cache.conf"
if (!(Test-Path -LiteralPath $ConfFile)) {
	Write-Output "Config not found, installing default http_range_cache.conf..."
	New-Item -ItemType Directory -Force -Path $ScriptOptsDir > $null
	$BundledConf = "$ConfigDir/$TargetRel/http_range_cache.conf"
	if (Test-Path -LiteralPath $BundledConf) {
		Copy-Item -LiteralPath $BundledConf -Destination $ConfFile -Force > $null
	}
	else {
		try {
			Invoke-WebRequest -OutFile $ConfFile -Uri $ConfURL > $null
		}
		catch {
			Write-Output "Warning: Couldn't download http_range_cache.conf, but script files are installed."
		}
	}
}

$Py = Get-Command python -ErrorAction SilentlyContinue
if (-not $Py) { $Py = Get-Command python3 -ErrorAction SilentlyContinue }
if ($Py) {
	& $Py.Source -c "import requests" 2>$null
	if ($LASTEXITCODE -ne 0) {
		Write-Output "Note: Python 'requests' module missing. Install with: pip install requests"
	}
}
else {
	Write-Output "Note: Python 3 not found in PATH. Install Python 3 + 'pip install requests' to run proxy.py."
}

Write-Output "mpv-http-range-cache has been installed."

