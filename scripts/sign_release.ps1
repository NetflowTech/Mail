# sign_release.ps1
# Watches for new AMail GitHub releases that haven't been signed yet,
# signs the installer with minisign, and uploads the .minisig.
#
# Prerequisites on this server:
#   - gh (GitHub CLI) installed and authenticated: gh auth login
#   - minisign installed (scoop install minisign)
#   - minisign.key stored at $KeyPath below (no passphrase)
#
# Schedule: run every 5 minutes via Task Scheduler (see setup instructions).

param(
    [string]$Repo     = "NetflowTech/Mail",
    [string]$KeyPath  = "C:\amail-sign\minisign.key",
    [string]$WorkDir  = "C:\amail-sign\tmp"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Output "[$ts] $msg"
}

# Ensure working directory exists
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

# Get the latest release tag
$tag = gh release list --repo $Repo --limit 1 --json tagName --jq ".[0].tagName" 2>&1
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: Could not fetch releases. Is 'gh' authenticated? ($tag)"
    exit 1
}
$tag = $tag.Trim()
if (-not $tag) {
    Log "No releases found."
    exit 0
}

$version = $tag.TrimStart("v")
$installer = "AMail-Setup-$version.exe"
$sigfile   = "$installer.minisig"

Log "Latest release: $tag"

# Check if .minisig already exists in this release
$assets = gh release view $tag --repo $Repo --json assets --jq "[.assets[].name]" 2>&1
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: Could not fetch release assets. ($assets)"
    exit 1
}

if ($assets -match [regex]::Escape($sigfile)) {
    Log "Already signed ($sigfile exists). Nothing to do."
    exit 0
}

Log "No signature found - downloading installer..."

$installerPath = Join-Path $WorkDir $installer
$sigPath       = Join-Path $WorkDir $sigfile

# Clean up any leftover files from a previous failed run
Remove-Item -Force -ErrorAction SilentlyContinue $installerPath, $sigPath

# Download the installer
gh release download $tag --repo $Repo --pattern $installer --dir $WorkDir --clobber
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: Download failed."
    exit 1
}
Log "Downloaded $installer"

# Sign it
# -l (legacy "Ed" format): the only format the 2.0.5 client can verify
minisign -S -l -m $installerPath -s $KeyPath -x $sigPath
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: minisign failed. Check that the key path is correct and has no passphrase."
    Remove-Item -Force -ErrorAction SilentlyContinue $installerPath
    exit 1
}
Log "Signed - uploading $sigfile..."

# Upload the signature to the release
gh release upload $tag $sigPath --repo $Repo --clobber
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: Upload failed."
    Remove-Item -Force -ErrorAction SilentlyContinue $installerPath, $sigPath
    exit 1
}

Log "Done. $sigfile uploaded to $tag."

# Clean up
Remove-Item -Force -ErrorAction SilentlyContinue $installerPath, $sigPath
