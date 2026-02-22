# Quaid Memory Plugin — Windows Installer
# Usage: irm https://raw.githubusercontent.com/steadman-labs/quaid/main/install.ps1 | iex
#
# Or with a specific version:
#   $env:QUAID_VERSION = "v1.0.0"; irm https://raw.githubusercontent.com/steadman-labs/quaid/main/install.ps1 | iex

$ErrorActionPreference = "Stop"

$Version = if ($env:QUAID_VERSION) { $env:QUAID_VERSION } else { "latest" }
$Repo = "steadman-labs/quaid"
$InstallDir = Join-Path $env:TEMP "quaid-install-$PID"

function Write-Info  { param($msg) Write-Host "[quaid] $msg" -ForegroundColor Blue }
function Write-Ok    { param($msg) Write-Host "[quaid] $msg" -ForegroundColor Green }
function Write-Err   { param($msg) Write-Host "[quaid] $msg" -ForegroundColor Red }

# --- Pre-checks ---
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Err "Node.js 18+ is required. Install it first:"
    Write-Err "  winget install OpenJS.NodeJS.LTS"
    Write-Err "  # or visit https://nodejs.org"
    exit 1
}

$NodeMajor = [int](node -e "console.log(process.versions.node.split('.')[0])" 2>$null)
if ($NodeMajor -lt 18) {
    Write-Err "Node.js 18+ is required (found v$(node --version))."
    Write-Err "Update: https://nodejs.org"
    exit 1
}

# --- Detect mode: OpenClaw or Standalone ---
$IsOpenClaw = $false
if ($env:CLAWDBOT_WORKSPACE) { $IsOpenClaw = $true }
elseif (Get-Command clawdbot -ErrorAction SilentlyContinue) { $IsOpenClaw = $true }
elseif (Get-Command openclaw -ErrorAction SilentlyContinue) { $IsOpenClaw = $true }

if ($env:QUAID_HOME) {
    $Workspace = $env:QUAID_HOME
} elseif ($env:CLAWDBOT_WORKSPACE) {
    $Workspace = $env:CLAWDBOT_WORKSPACE
} else {
    $Workspace = Join-Path $HOME "quaid"
}

if ($IsOpenClaw) {
    Write-Info "Detected OpenClaw installation"
} else {
    Write-Info "Standalone mode (no OpenClaw detected)"
    Write-Info "Workspace: $Workspace"
}

if (-not $IsOpenClaw -and -not (Test-Path $Workspace)) {
    New-Item -ItemType Directory -Path $Workspace -Force | Out-Null
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

# Find setup-quaid.mjs (could be flat or in a subdirectory)
$SetupPath = Join-Path $InstallDir "setup-quaid.mjs"
if (Test-Path $SetupPath) {
    $ReleaseDir = Get-Item $InstallDir
} else {
    $SubDir = Get-ChildItem -Path $InstallDir -Directory -Filter "quaid*" | Select-Object -First 1
    if ($SubDir) { $ReleaseDir = $SubDir } else { $ReleaseDir = $null }
}

if (-not $ReleaseDir -or -not (Test-Path (Join-Path $ReleaseDir.FullName "setup-quaid.mjs"))) {
    Write-Err "Archive doesn't contain setup-quaid.mjs"
    exit 1
}

# --- Run guided installer ---
Write-Ok "Downloaded. Starting guided installer..."
Write-Host ""

$env:QUAID_HOME = $Workspace
$env:CLAWDBOT_WORKSPACE = $Workspace

try {
    node (Join-Path $ReleaseDir.FullName "setup-quaid.mjs")
} finally {
    # Cleanup
    if (Test-Path $InstallDir) {
        Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
