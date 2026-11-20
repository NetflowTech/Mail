# Publishes the PC port (8443) through the server's existing Cloudflare Tunnel (the one the
# other cred-desk.com apps use), the same way their deploy scripts do:
#   1. add the hostname to config.yml above the catch-all line, pointing at
#      https://127.0.0.1:8443 with noTLSVerify (the backend uses its own certificate;
#      PCs see Cloudflare's instead). A dated backup of config.yml is kept next to it.
#   2. check the file with "cloudflared tunnel ingress validate" (restores the backup if it fails),
#   3. create the DNS record:  cloudflared tunnel route dns <tunnel> amail.cred-desk.com
#   4. restart the cloudflared service.
# Only ever the PC port. The admin panel (8444) must stay off the tunnel: through it every
# visitor would look like this machine and pass the panel's office-network check.
# Safe to run again: it does nothing if the hostname is already in config.yml.
# Run as Administrator (install_windows.ps1 calls it with -TunnelHostname).
param(
    [string]$Hostname = "amail.cred-desk.com",
    [int]$Port = 8443
)
$ErrorActionPreference = "Continue"     # cloudflared writes to stderr; check $LASTEXITCODE instead
function Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Warning $m }
function Manual {
    Write-Host ""
    Write-Host "  To finish by hand, add this to the tunnel's config.yml, above the '- service: http_status:404' line:"
    Write-Host "    - hostname: $Hostname"
    Write-Host "      service: https://127.0.0.1:$Port"
    Write-Host "      originRequest:"
    Write-Host "        noTLSVerify: true"
    Write-Host "  then:  cloudflared.exe tunnel route dns <tunnel name> $Hostname"
    Write-Host "  then:  Restart-Service cloudflared"
}

# 1. The cloudflared service, its exe and (if given) its --config file
$svc = Get-CimInstance Win32_Service | Where-Object { $_.PathName -match "cloudflared" } | Select-Object -First 1
if (-not $svc) { Warn "No cloudflared service found on this server."; Manual; exit 1 }
$cmdline = $svc.PathName
$cfExe = if ($cmdline -match '^\s*"([^"]+)"') { $Matches[1] } else { ($cmdline -split '\s+')[0] }
if ($cmdline -match '--token') {
    Warn "This tunnel is managed from the Cloudflare dashboard (token), so config.yml isn't used."
    Write-Host "  Add it there: Zero Trust -> Networks -> Tunnels -> (tunnel) -> Public hostname ->"
    Write-Host "  Add: subdomain/domain = $Hostname, type HTTPS, URL 127.0.0.1:$Port,"
    Write-Host "  Additional settings -> TLS -> No TLS Verify: on."
    exit 1
}
$candidates = @()
if ($cmdline -match '--config[ =]+"([^"]+)"') { $candidates += $Matches[1] }
elseif ($cmdline -match '--config[ =]+(\S+)') { $candidates += $Matches[1] }
$candidates += @(
    "$env:ProgramData\cloudflared\config.yml\config.yml",
    "$env:ProgramData\cloudflared\config.yml",
    "$env:SystemRoot\System32\config\systemprofile\.cloudflared\config.yml",
    "C:\cloudflared\config.yml",
    (Join-Path (Split-Path $cfExe -Parent) "config.yml"),
    "$env:USERPROFILE\.cloudflared\config.yml"
)
$cfg = $candidates | Where-Object { $_ -and (Test-Path $_ -PathType Leaf) } | Select-Object -First 1
if (-not $cfg) { Warn "Couldn't find the tunnel's config.yml (looked in: $($candidates -join '; '))"; Manual; exit 1 }
Ok "cloudflared service '$($svc.Name)', config $cfg"

# 2. Add the ingress rule above the catch-all
$text = [System.IO.File]::ReadAllText($cfg)
$tunnel = if ($text -match '(?m)^tunnel:\s*(\S+)') { $Matches[1] } else { $null }
if ($text -match "(?m)^\s*-\s*hostname:\s*$([regex]::Escape($Hostname))\s*$") {
    Ok "$Hostname is already in config.yml"
} else {
    $catchAll = [regex]::Match($text, '(?m)^([ \t]*)-[ \t]*service:[ \t]*http_status:404')
    if (-not $catchAll.Success) { Warn "config.yml has no '- service: http_status:404' catch-all line."; Manual; exit 1 }
    $ind = $catchAll.Groups[1].Value
    $nl = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    $entry = "$ind- hostname: $Hostname$nl$ind  service: https://127.0.0.1:$Port$nl$ind  originRequest:$nl$ind    noTLSVerify: true$nl"
    $backup = "$cfg.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item $cfg $backup
    [System.IO.File]::WriteAllText($cfg, $text.Insert($catchAll.Index, $entry))
    $check = & $cfExe tunnel --config $cfg ingress validate 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Copy-Item $backup $cfg -Force
        Warn "cloudflared rejected the edited config.yml, so it was put back as it was:`n$check"
        Manual; exit 1
    }
    Ok "added $Hostname -> https://127.0.0.1:$Port (backup: $backup)"
}

# 3. DNS record for the hostname
if ($tunnel) {
    $out = & $cfExe tunnel route dns $tunnel $Hostname 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0 -or $out -match "already") { Ok "DNS: $Hostname -> tunnel $tunnel" }
    else {
        Warn "Couldn't create the DNS record automatically:`n$out"
        Write-Host "  Run it yourself (as the account that set up the tunnel):  `"$cfExe`" tunnel route dns $tunnel $Hostname"
        Write-Host "  or in Cloudflare DNS add a proxied CNAME  $($Hostname.Split('.')[0]) -> <tunnel id>.cfargotunnel.com"
    }
} else {
    Warn "No 'tunnel:' line in config.yml; create the DNS record yourself: cloudflared tunnel route dns <tunnel> $Hostname"
}

# 4. Restart the tunnel and check the public address
Restart-Service -Name $svc.Name
Ok "restarted $($svc.Name)"
Start-Sleep -Seconds 10
try {
    $h = Invoke-RestMethod "https://$Hostname/api/v1/health" -TimeoutSec 15
    Ok "https://$Hostname is live: $($h | ConvertTo-Json -Compress)"
} catch {
    Warn "https://$Hostname didn't answer yet ($($_.Exception.Message)). New DNS records can take a few minutes; try it in a browser shortly."
}
