[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$TaskName = "CampusNetworkAutoLogin"
$InstallDir = Join-Path $env:LOCALAPPDATA "CampusAutoLogin"
$ConfigFile = Join-Path (Join-Path $env:APPDATA "CampusAutoLogin") "config.json"
$py = Get-Command py.exe -ErrorAction SilentlyContinue

if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "configure.py"))) {
    throw "The installed program was not found. Run install.ps1 first."
}
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($py) {
    & $py.Source -3 (Join-Path $InstallDir "configure.py") --existing $ConfigFile --output $ConfigFile
} else {
    $python = (Get-Command python.exe -ErrorAction Stop).Source
    & $python (Join-Path $InstallDir "configure.py") --existing $ConfigFile --output $ConfigFile
}
if ($LASTEXITCODE -ne 0) {
    throw "Configuration failed with exit code $LASTEXITCODE."
}

$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = [System.Security.AccessControl.FileSecurity]::new()
$acl.SetOwner($currentSid)
$acl.SetAccessRuleProtection($true, $false)
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $currentSid,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    [System.Security.AccessControl.AccessControlType]::Allow
)
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $ConfigFile -AclObject $acl
Start-ScheduledTask -TaskName $TaskName
Write-Host "Configuration updated and the background task restarted."
