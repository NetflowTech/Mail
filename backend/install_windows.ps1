<#
  Install the AMail admin backend on a Windows server.

  Run in an elevated PowerShell (Run as administrator) from this folder:
      powershell -ExecutionPolicy Bypass -File .\install_windows.ps1 -PublicUrl https://amail.example.com:8443

  -PublicUrl  the address AMail PCs will use to reach this server: the office
              IP for PCs in the office, or a public name/IP that forwards port
              8443 here for PCs that work remotely. Changeable later in the panel.
  -Admin      the admin panel username to create (default: admin).
  -AdminNetwork  which computers may open the admin panel: "LocalSubnet"
              (default), or the admins' own addresses / subnet, e.g.
              "192.168.1.50,192.168.1.51" or "192.168.10.0/28". Narrower is
              safer: guest Wi-Fi on the same network should not reach it.
  -TrustedProxies  only when PCs reach port 8443 through a tunnel or reverse
              proxy on this machine (Cloudflare Tunnel, nginx): its address,
              usually 127.0.0.1. The backend then takes each PC's real
              address from the proxy, for its logs and rate limits.
  -ProxyHeader  the header that proxy puts the PC's address in:
              "CF-Connecting-IP" for Cloudflare Tunnel, "X-Forwarded-For"
              (the default) for nginx, Caddy and most others.

  What it does:
    1. creates a Python environment in .venv and installs the requirements,
    2. locks this folder so only Administrators can change it (the service
       runs code from here),
    3. keeps the data (database, keys, certificate) in C:\ProgramData\AMailAdmin,
       readable only by Administrators, SYSTEM and the service account,
    4. creates the keys (protected by Windows DPAPI) and certificate,
    5. asks for the admin password,
    6. runs the backend at startup as the low-privilege LOCAL SERVICE account
       (scheduled task "AMail Admin"), restarting it if it stops,
    7. opens the firewall: port 8443 (PCs) from anywhere, port 8444 (admin
       panel) from -AdminNetwork only.
  Running it again is safe: existing keys and data are kept.
#>
param(
    [Parameter(Mandatory = $true)][string]$PublicUrl,
    [string]$Admin = "admin",
    [string]$AdminNetwork = "LocalSubnet",
    [string]$TrustedProxies = "",
    [string]$ProxyHeader = "X-Forwarded-For"
)
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this in PowerShell opened with 'Run as administrator'."
}
if ($PublicUrl -notmatch '^https://[A-Za-z0-9.-]+(:\d{1,5})?$') {
    throw "PublicUrl must look like https://name-or-ip:8443"
}

$Here = $PSScriptRoot
$Data = Join-Path $env:ProgramData "AMailAdmin"
$Py = Join-Path $Here ".venv\Scripts\python.exe"

Write-Host "==> Checking Python (3.10 or newer)"
& py -3 -c "import sys; assert sys.version_info >= (3, 10), sys.version" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.10+ not found. Install it from python.org (tick 'Add to PATH' and the 'py' launcher), then run this again."
}

Write-Host "==> Creating the Python environment"
if (-not (Test-Path $Py)) { & py -3 -m venv (Join-Path $Here ".venv") }
# Exact versions with checksums: a tampered or substituted package is refused.
& $Py -m pip install --quiet --require-hashes -r (Join-Path $Here "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Installing the requirements failed" }

# The service runs code from this folder: only Administrators and SYSTEM may
# change it, the service account may only read it. (Without this, an ordinary
# user who can write here could have their own code run by the service.)
$LocalService = "*S-1-5-19"
& icacls $Here /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "${LocalService}:(OI)(CI)RX" | Out-Null

Write-Host "==> Preparing $Data (Administrators, SYSTEM and the service account only)"
New-Item -ItemType Directory -Force -Path $Data | Out-Null
& icacls $Data /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "${LocalService}:(OI)(CI)M" | Out-Null

$env:AMAIL_ADMIN_DATA = $Data
Push-Location $Here
try {
    Write-Host "==> Creating keys and certificate"
    & $Py -m amail_admin init --url $PublicUrl
    if ($LASTEXITCODE -ne 0) { throw "init failed" }

    Write-Host "==> Admin account '$Admin'"
    & $Py -m amail_admin create-admin $Admin
    if ($LASTEXITCODE -ne 0) { throw "Admin account not created" }
} finally { Pop-Location }

# The panel's own address check matches the firewall rule.
if ($AdminNetwork -eq "LocalSubnet") {
    $PanelNets = ""      # empty = the backend's default private ranges
} else {
    $PanelNets = $AdminNetwork
}

Write-Host "==> Registering the startup task"
$Runner = Join-Path $Here "run_server.cmd"
$ProxyLines = ""
if ($TrustedProxies) {
    $ProxyLines = "set AMAIL_TRUSTED_PROXIES=$TrustedProxies`r`nset AMAIL_PROXY_HEADER=$ProxyHeader`r`n"
}
@"
@echo off
rem Started by the "AMail Admin" scheduled task. Do not move this folder.
set AMAIL_ADMIN_DATA=$Data
set AMAIL_ADMIN_NETWORKS=$PanelNets
${ProxyLines}cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m amail_admin run >> "$Data\server.log" 2>&1
"@ | Set-Content -Encoding ASCII $Runner

$action = New-ScheduledTaskAction -Execute $Runner
$trigger = New-ScheduledTaskTrigger -AtStartup
# LOCAL SERVICE, not SYSTEM: if the backend were ever compromised, the
# attacker gets an account that can't administer the machine.
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\LOCAL SERVICE" -LogonType ServiceAccount
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "AMail Admin" -Action $action -Trigger $trigger `
    -Principal $taskPrincipal -Settings $settings -Force | Out-Null
Stop-ScheduledTask -TaskName "AMail Admin" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "AMail Admin"

Write-Host "==> Firewall"
foreach ($n in "AMail Admin - PCs", "AMail Admin - Panel") {
    Get-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}
New-NetFirewallRule -DisplayName "AMail Admin - PCs" -Direction Inbound -Protocol TCP `
    -LocalPort 8443 -Action Allow | Out-Null
$panelRemote = if ($AdminNetwork -eq "LocalSubnet") { "LocalSubnet" } else { $AdminNetwork -split "," }
New-NetFirewallRule -DisplayName "AMail Admin - Panel" -Direction Inbound -Protocol TCP `
    -LocalPort 8444 -RemoteAddress $panelRemote -Action Allow | Out-Null

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
        $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
       Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "Done."
Write-Host "  Admin panel:  https://${ip}:8444"
Write-Host "                Install $Data\server.crt as a Trusted Root certificate on each admin PC"
Write-Host "                so the browser never shows a certificate warning (see README)."
Write-Host "                The first sign-in sets up two-step sign-in with an authenticator app."
Write-Host "  PCs connect:  $PublicUrl"
Write-Host "  Data folder:  $Data   (back it up, see README)"
Write-Host "  Log:          $Data\server.log"
