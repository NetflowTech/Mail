# Auto-update, run every 5 minutes by the "AMail Admin update" scheduled task (as SYSTEM).
# When GitHub's main branch has new commits:
#   back up the database -> pull -> install packages if they changed -> run the backend's tests
#   -> restart the backend -> check it answers.
# If the tests or the restart fail, it rolls back to the previous version, keeps the backend
# running on it, and skips that commit until a newer one arrives.
# Commits that touch nothing under backend\ (app-only changes) are pulled without a restart.
# Log: C:\ProgramData\AMailAdmin\logs\update.log
# Windows PowerShell 5.1: native programs' stderr must not stop the script, so errors are
# checked through $LASTEXITCODE instead of $ErrorActionPreference = "Stop".
$ErrorActionPreference = "Continue"
$backend = (Resolve-Path "$PSScriptRoot\..\..").Path     # ...\Mail\backend
$repo = (Resolve-Path "$backend\..").Path                 # ...\Mail (the git clone)
Set-Location $repo
$py = "$backend\.venv\Scripts\python.exe"
$data = if ($env:AMAIL_ADMIN_DATA) { $env:AMAIL_ADMIN_DATA } else { "$env:ProgramData\AMailAdmin" }
$env:AMAIL_ADMIN_DATA = $data
$logFile = "$data\logs\update.log"
$skipFile = "$data\update-skip.txt"
New-Item -ItemType Directory -Force -Path "$data\logs" | Out-Null

function Log($msg) { Add-Content -Path $logFile -Value ("[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg) }
function Run($exe, [string[]]$argv) {
    # Run a program, append its output to the log, return $true on exit code 0.
    $out = & $exe @argv 2>&1 | Out-String
    if ($out.Trim()) { Add-Content -Path $logFile -Value $out.TrimEnd() }
    return ($LASTEXITCODE -eq 0)
}
function Healthy {
    # The PC port uses the server's own certificate: don't verify it for this local check.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    [Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
    for ($i = 0; $i -lt 12; $i++) {
        Start-Sleep -Seconds 5
        try { Invoke-RestMethod https://127.0.0.1:8443/api/v1/health -TimeoutSec 5 | Out-Null; return $true } catch { }
    }
    return $false
}
function Restart-Backend {
    & "$PSScriptRoot\stop-backend.ps1" | Out-Null
    Start-ScheduledTask -TaskName "AMail Admin"
    return (Healthy)
}
. "$PSScriptRoot\notify.ps1"

# 1. Anything new? (quiet when not, so the log only shows real events)
& git fetch -q origin main *> $null
if ($LASTEXITCODE -ne 0) {
    # Log a failing fetch at most once an hour, not every 5 minutes.
    if ((Get-Date).Minute -lt 5) { Log "git fetch failed (network down, or the deploy key was removed from GitHub)" }
    exit 1
}
$old = (& git rev-parse HEAD).Trim()
$new = (& git rev-parse origin/main).Trim()
if ($old -eq $new) { exit 0 }
if ((Test-Path $skipFile) -and ((Get-Content $skipFile -Raw).Trim() -eq $new)) { exit 0 }
if (& git status --porcelain --untracked-files=no) {
    if ((Get-Date).Minute -lt 5) { Log "not updating: files in $repo were edited by hand (git status shows changes)" }
    exit 1
}

& git diff --quiet $old $new -- backend
$backendChanged = ($LASTEXITCODE -ne 0)
Log "update $($old.Substring(0,7)) -> $($new.Substring(0,7)): $((& git log -1 --format=%s $new))"

# 2. Nothing under backend\ changed (an app-only commit): just move forward, no restart.
if (-not $backendChanged) {
    if (Run "git" @("merge", "--ff-only", "-q", "origin/main")) { Log "no backend changes; pulled without restarting" }
    else { Log "not updating: main can't be fast-forwarded. Fix by hand: git reset --hard origin/main"; Set-Content $skipFile $new }
    exit 0
}

# 3. Safety copy of the database first
Run $py @("-m", "amail_admin", "backup", "$data\backups\pre-update", "--keep", "20") | Out-Null

# 4. Pull and install
if (-not (Run "git" @("merge", "--ff-only", "-q", "origin/main"))) {
    Log "not updating: main can't be fast-forwarded (history was rewritten?). Fix by hand: git reset --hard origin/main"
    Set-Content $skipFile $new
    exit 1
}
& git diff --quiet $old $new -- backend/requirements.txt backend/requirements-test.txt
$reqChanged = ($LASTEXITCODE -ne 0)
$ok = $true
if ($reqChanged) { $ok = Run $py @("-m", "pip", "install", "--quiet", "--require-hashes", "-r", "$backend\requirements-test.txt") }

# 5. Test the new code before it goes live (the running backend keeps serving meanwhile)
Set-Location $backend
if ($ok) { $ok = Run $py @("-m", "pytest", "-q", "-p", "no:cacheprovider", "tests") }
Set-Location $repo

function Roll-Back($why) {
    Log "ROLLING BACK to $($old.Substring(0,7)): $why"
    Send-WhatsApp "[AMail Admin] Update rolled back on ${env:COMPUTERNAME}: $why"
    & git reset -q --hard $old
    if ($reqChanged) { Run $py @("-m", "pip", "install", "--quiet", "--require-hashes", "-r", "$backend\requirements-test.txt") | Out-Null }
    Set-Content $skipFile $new
}

if (-not $ok) {
    Roll-Back "packages or tests failed; the backend was not restarted"
    exit 1
}

# 6. Go live
if (Restart-Backend) {
    Log "update OK, backend restarted on $($new.Substring(0,7))"
    Send-WhatsApp "[AMail Admin] Updated $($old.Substring(0,7)) -> $($new.Substring(0,7)) on $env:COMPUTERNAME"
    Remove-Item $skipFile -ErrorAction SilentlyContinue
    exit 0
}
Roll-Back "the backend did not answer after restarting (see $data\server.log)"
if (Restart-Backend) { Log "backend back up on $($old.Substring(0,7))" } else { Log "BACKEND IS DOWN: it did not start on the old version either"; Send-WhatsApp "[AMail Admin] BACKEND IS DOWN on $env:COMPUTERNAME" }
exit 1
