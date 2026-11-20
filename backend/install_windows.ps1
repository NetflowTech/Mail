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
  -DeviceHost  which addresses the PC port listens on: 0.0.0.0 (default, all)
              or 127.0.0.1 when every PC comes through a tunnel on this
              machine (the port is then not opened in the firewall).
  -TunnelHostname  publish the PC port through this machine's existing
              Cloudflare Tunnel under this name (deploy\windows\add-tunnel-route.ps1).
  -Python     the python.exe to build the backend's environment from, when
              the one the "py" launcher picks isn't right (for example a
              copy bundled with another program). Must be installed for
              all users, e.g. "C:\Program Files\Python312\python.exe".
  -NoAutoUpdate  don't set up the 5-minute "AMail Admin update" task. By
              default, when this folder is inside a git clone of the repo,
              the task pulls new commits from GitHub, runs the backend's
              tests and restarts it, rolling back if anything fails.

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
    [string]$ProxyHeader = "X-Forwarded-For",
    [string]$DeviceHost = "0.0.0.0",
    [string]$TunnelHostname = "",
    [string]$Python = "",
    [switch]$NoAutoUpdate
)
$ErrorActionPreference = "Stop"
# Another program on the machine may have set PYTHONPATH system-wide (ZKBioTime
# does). It must never reach into the backend's environment: not now, not
# when the service runs.
$env:PYTHONPATH = ""

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
# -Python if given, else the py launcher, else python on PATH. A Python that
# prints warnings on startup (a broken package file, say) must not count as
# missing: Windows PowerShell 5.1 turns native stderr into errors under
# "Stop", so the checks run under "Continue" and look at the exit code.
$ErrorActionPreference = "Continue"
$SysPy = $null
$cands = @()
if ($Python) { $cands += , @($Python) } else { $cands += , @("py", "-3"); $cands += , @("python") }
foreach ($cand in $cands) {
    if (-not (Get-Command $cand[0] -ErrorAction SilentlyContinue)) { continue }
    $out = & $cand[0] @($cand[1..($cand.Length - 1)]) -c "import sys; print(sys.version_info >= (3, 10)); print(sys.base_prefix)" 2>$null
    if ($LASTEXITCODE -eq 0 -and ($out -join "`n") -match "True") { $SysPy = $cand; $base = ($out | Select-Object -Last 1); break }
}
$ErrorActionPreference = "Stop"
if (-not $SysPy) {
    throw "Python 3.10+ not found. Install it from python.org with 'Install for all users' ticked, then run this again (or pass -Python <path to python.exe>)."
}
if ($base -like "C:\Users\*") {
    throw "Python is installed for one user only ($base). The backend runs as LOCAL SERVICE, which can't use it. Reinstall Python from python.org with 'Install for all users' ticked, then run this again."
}
Write-Host "    using $base"

Write-Host "==> Creating the Python environment"
if (-not (Test-Path $Py)) { & $SysPy[0] @($SysPy[1..($SysPy.Length - 1)]) -m venv (Join-Path $Here ".venv") }
# Exact versions with checksums: a tampered or substituted package is refused.
# requirements-test.txt = the backend's packages plus pytest, which the
# auto-updater runs against new code before restarting.
& $Py -m pip install --quiet --require-hashes -r (Join-Path $Here "requirements-test.txt")
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
if ($DeviceHost -ne "0.0.0.0") {
    $ProxyLines += "set AMAIL_DEVICE_HOST=$DeviceHost`r`n"
}
@"
@echo off
rem Started by the "AMail Admin" scheduled task. Do not move this folder.
set PYTHONPATH=
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
if ($DeviceHost -ne "127.0.0.1") {
    New-NetFirewallRule -DisplayName "AMail Admin - PCs" -Direction Inbound -Protocol TCP `
        -LocalPort 8443 -Action Allow | Out-Null
}
$panelRemote = if ($AdminNetwork -eq "LocalSubnet") { "LocalSubnet" } else { $AdminNetwork -split "," }
New-NetFirewallRule -DisplayName "AMail Admin - Panel" -Direction Inbound -Protocol TCP `
    -LocalPort 8444 -RemoteAddress $panelRemote -Action Allow | Out-Null

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
        $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
       Select-Object -First 1).IPAddress

# ----- Auto-update from GitHub -----------------------------------------------
# Only when this folder is inside a git clone (C:\...\Mail\backend): the task
# needs the repo to pull into. A plain copy of the folder is updated by hand.
$Repo = Split-Path $Here -Parent
$IsClone = (Test-Path (Join-Path $Repo ".git")) -and (Get-Command git -ErrorAction SilentlyContinue)
Unregister-ScheduledTask -TaskName "AMail Admin update" -Confirm:$false -ErrorAction SilentlyContinue
if ($NoAutoUpdate) {
    Write-Host "==> Auto-update: off (-NoAutoUpdate)"
} elseif (-not $IsClone) {
    Write-Host "==> Auto-update: off. To turn it on, install from a git clone of the repo"
    Write-Host "    (git clone https://github.com/NetflowTech/Mail.git, then run this from Mail\backend)"
    Write-Host "    with Git for Windows installed."
} else {
    Write-Host "==> Auto-update from GitHub"
    # The update task runs as SYSTEM and pulls into the whole clone, so the
    # clone gets the same lock as this folder: Administrators and SYSTEM only.
    & icacls $Repo /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "${LocalService}:(OI)(CI)RX" | Out-Null
    $repoSlash = $Repo -replace "\\", "/"
    $ErrorActionPreference = "Continue"   # git writes to stderr; check $LASTEXITCODE
    if (-not (& git config --system --get-all safe.directory 2>$null | Where-Object { $_ -eq $repoSlash })) {
        & git config --system --add safe.directory $repoSlash
    }
    # A read-only GitHub deploy key, kept in the data folder (never in the repo).
    $keyFile = Join-Path $Data "deploy_key"
    if (-not (Test-Path $keyFile)) {
        & cmd /c "ssh-keygen -q -t ed25519 -N """" -C amail-admin-server -f `"$keyFile`""
    }
    $dataSlash = $Data -replace "\\", "/"
    & git -C $Repo config core.sshCommand "ssh -i '$dataSlash/deploy_key' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile='$dataSlash/known_hosts'"
    $url = (& git -C $Repo remote get-url origin)
    if ($url -match "^https://github\.com/(.+?)(\.git)?$") {
        & git -C $Repo remote set-url origin "git@github.com:$($Matches[1]).git"
    }
    & git -C $Repo ls-remote -q origin *> $null
    $gitOk = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = "Stop"
    $sysPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $short = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    $every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName "AMail Admin update" -Force -Principal $sysPrincipal -Settings $short `
        -Trigger $every5 `
        -Action (New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Repo `
            -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Here\deploy\windows\update-backend.ps1`"") | Out-Null
    if ($gitOk) {
        Write-Host "    GitHub access OK (deploy key). New commits on main are applied within 5 minutes;"
        Write-Host "    log: $Data\logs\update.log"
    } else {
        Write-Host ""
        Write-Warning "Auto-update can't reach GitHub yet. Add this server's deploy key once:"
        Write-Host "  GitHub -> NetflowTech/Mail -> Settings -> Deploy keys -> Add deploy key"
        Write-Host "  Title: amail-admin-server.  Leave 'Allow write access' unticked.  Key:"
        Write-Host ""
        Get-Content "$keyFile.pub"
        Write-Host ""
        Write-Host "  Nothing else to run: the update task starts working as soon as the key is added."
    }
}

# ----- Cloudflare Tunnel ------------------------------------------------------
if ($TunnelHostname) {
    Write-Host "==> Cloudflare Tunnel: $TunnelHostname"
    & "$Here\deploy\windows\add-tunnel-route.ps1" -Hostname $TunnelHostname -Port 8443
}

Write-Host ""
Write-Host "Done."
Write-Host "  Admin panel:  https://${ip}:8444"
Write-Host "                Install $Data\server.crt as a Trusted Root certificate on each admin PC"
Write-Host "                so the browser never shows a certificate warning (see README)."
Write-Host "                The first sign-in sets up two-step sign-in with an authenticator app."
Write-Host "  PCs connect:  $PublicUrl"
Write-Host "  Data folder:  $Data   (back it up, see README)"
Write-Host "  Log:          $Data\server.log"
