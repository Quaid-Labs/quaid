# Quaid Memory Plugin — Windows Installer
# Usage: irm https://raw.githubusercontent.com/rekall-inc/quaid/main/install.ps1 | iex
#
# Or with a specific version:
#   $env:QUAID_VERSION = "v1.0.0"; irm https://raw.githubusercontent.com/rekall-inc/quaid/main/install.ps1 | iex

$ErrorActionPreference = "Stop"

$Version = if ($env:QUAID_VERSION) { $env:QUAID_VERSION } else { "latest" }
$Repo = "rekall-inc/quaid"
$InstallDir = Join-Path $env:TEMP "quaid-install-$PID"

function Write-Info  { param($msg) Write-Host "[quaid] $msg" -ForegroundColor Blue }
function Write-Ok    { param($msg) Write-Host "[quaid] $msg" -ForegroundColor Green }
function Write-Err   { param($msg) Write-Host "[quaid] $msg" -ForegroundColor Red }

# --- Pre-checks ---
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Err "Node.js is required. Install it first:"
    Write-Err "  winget install OpenJS.NodeJS.LTS"
    Write-Err "  # or visit https://nodejs.org"
    exit 1
}

$hasClawdbot = Get-Command clawdbot -ErrorAction SilentlyContinue
$hasOpenclaw = Get-Command openclaw -ErrorAction SilentlyContinue
if (-not $hasClawdbot -and -not $hasOpenclaw) {
    Write-Err "OpenClaw is required. Install it first:"
    Write-Err "  npm install -g openclaw"
    Write-Err "  openclaw setup"
    exit 1
}

# --- Download ---
Write-Info "Downloading Quaid..."

if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force }
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

if ($Version -eq "latest") {
    $DownloadUrl = "https://github.com/$Repo/releases/latest/download/quaid-release.tar.gz"
} else {
    $DownloadUrl = "https://github.com/$Repo/releases/download/$Version/quaid-release.tar.gz"
}

$TarballPath = Join-Path $InstallDir "quaid.tar.gz"

try {
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $TarballPath -UseBasicParsing
} catch {
    Write-Err "Download failed: $_"
    Write-Err "URL: $DownloadUrl"
    exit 1
}

# --- Extract ---
Write-Info "Extracting..."

# tar is available on Windows 10 1803+ (April 2018 Update)
try {
    tar xzf $TarballPath -C $InstallDir 2>$null
} catch {
    Write-Err "Extraction failed. Ensure tar is available (Windows 10 1803+)."
    Write-Err "Alternatively, install 7-Zip or Git for Windows."
    exit 1
}

# Find the extracted directory
$ReleaseDir = Get-ChildItem -Path $InstallDir -Directory -Filter "quaid*" | Select-Object -First 1

if (-not $ReleaseDir -or -not (Test-Path (Join-Path $ReleaseDir.FullName "setup-quaid.mjs"))) {
    Write-Err "Archive doesn't contain setup-quaid.mjs"
    exit 1
}

# --- Run guided installer ---
Write-Ok "Downloaded. Starting guided installer..."
Write-Host ""

try {
    node (Join-Path $ReleaseDir.FullName "setup-quaid.mjs")
} finally {
    # Cleanup
    if (Test-Path $InstallDir) {
        Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
