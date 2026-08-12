[CmdletBinding()]
param([switch]$Purge)

$ErrorActionPreference = "Stop"
$TaskName = "CampusNetworkAutoLogin"
$RemoteTaskName = "CampusRemoteRecovery"
$InstallDir = Join-Path $env:LOCALAPPDATA "CampusAutoLogin"
$ConfigDir = Join-Path $env:APPDATA "CampusAutoLogin"

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName $RemoteTaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $RemoteTaskName -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
if ($Purge) {
    Remove-Item -LiteralPath $ConfigDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Removed the Windows tasks, program, account configuration, and logs."
} else {
    Write-Host "Preserved configuration and logs at $ConfigDir. Use -Purge to remove them."
    Write-Host "Removed the Windows tasks and program."
}
Write-Host "Existing Windows Wi-Fi profiles were not removed."
