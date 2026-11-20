# Stops the AMail admin backend (for an update or a database restore). Run as Administrator.
# Start it again with:  Start-ScheduledTask -TaskName "AMail Admin"
$backend = (Resolve-Path "$PSScriptRoot\..\..").Path
if (Get-ScheduledTask -TaskName "AMail Admin" -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName "AMail Admin"
}
# Stopping the task can leave its process running: end this backend's Python (only its own:
# matched on the .venv path and the command line, so another app's Python is left alone).
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq "python.exe" -and $_.ExecutablePath -like "$backend\.venv\*" -and $_.CommandLine -like "*amail_admin run*"
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "AMail admin backend stopped."
