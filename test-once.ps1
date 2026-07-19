[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$InstallDir = Join-Path $env:LOCALAPPDATA "CampusAutoLogin"
$ConfigFile = Join-Path (Join-Path $env:APPDATA "CampusAutoLogin") "config.json"
$Program = Join-Path $InstallDir "campus_autologin.py"
$py = Get-Command py.exe -ErrorAction SilentlyContinue

Stop-ScheduledTask -TaskName "CampusNetworkAutoLogin" -ErrorAction SilentlyContinue
try {
    if ($py) {
        & $py.Source -3 $Program --config $ConfigFile --once
    } else {
        & (Get-Command python.exe -ErrorAction Stop).Source $Program --config $ConfigFile --once
    }
    $result = $LASTEXITCODE
} finally {
    Start-ScheduledTask -TaskName "CampusNetworkAutoLogin" -ErrorAction SilentlyContinue
}
exit $result
