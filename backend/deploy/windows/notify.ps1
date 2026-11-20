# Shared WhatsApp notifier - dot-source this in other scripts: . "$PSScriptRoot\notify.ps1"
# Reads WHATSAPP_PHONE and WHATSAPP_APIKEY from Windows system environment variables.
# Set them once on the server and every app's scripts will pick them up automatically:
#   [Environment]::SetEnvironmentVariable("WHATSAPP_PHONE", "971XXXXXXXXX", "Machine")
#   [Environment]::SetEnvironmentVariable("WHATSAPP_APIKEY", "XXXXXXX",     "Machine")
# Does nothing silently if either value is missing or the request fails.
function Send-WhatsApp([string]$text) {
    try {
        $phone = [Environment]::GetEnvironmentVariable("WHATSAPP_PHONE", "Machine")
        $key   = [Environment]::GetEnvironmentVariable("WHATSAPP_APIKEY", "Machine")
        if (-not $phone -or -not $key) { return }
        $encoded = [uri]::EscapeDataString($text)
        Invoke-WebRequest "https://api.callmebot.com/whatsapp.php?phone=$phone&text=$encoded&apikey=$key" -UseBasicParsing -TimeoutSec 10 | Out-Null
    } catch { }
}
