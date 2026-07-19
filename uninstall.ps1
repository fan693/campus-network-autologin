[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$TaskName = "CampusNetworkAutoLogin"
$InstallDir = Join-Path $env:LOCALAPPDATA "CampusAutoLogin"
$ConfigDir = Join-Path $env:APPDATA "CampusAutoLogin"

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $ConfigDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "Removed the Windows task, program, account configuration, and log."
Write-Host "Existing Windows Wi-Fi profiles were not removed."
