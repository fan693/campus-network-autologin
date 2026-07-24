[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$TaskName = "CampusNetworkAutoLogin"
$RemoteTaskName = "CampusRemoteRecovery"
$InstallDir = Join-Path $env:LOCALAPPDATA "CampusAutoLogin"
$ConfigDir = Join-Path $env:APPDATA "CampusAutoLogin"
$ConfigFile = Join-Path $ConfigDir "config.json"
$LogFile = Join-Path $ConfigDir "campus-autologin.log"

function Get-PythonCommand {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $pyw = Get-Command pyw.exe -ErrorAction SilentlyContinue
        $background = if ($pyw) { $pyw.Source } else { $py.Source }
        return @{ Executable = $py.Source; BackgroundExecutable = $background; Prefix = @("-3") }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $pythonw = Join-Path (Split-Path $python.Source) "pythonw.exe"
        $background = if (Test-Path -LiteralPath $pythonw) { $pythonw } else { $python.Source }
        return @{ Executable = $python.Source; BackgroundExecutable = $background; Prefix = @() }
    }
    throw "Python 3 was not found. Install Python 3.8+ from https://www.python.org/downloads/windows/ and enable Add Python to PATH."
}

function Protect-ConfigFile([string]$Path) {
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
    Set-Acl -LiteralPath $Path -AclObject $acl
}

$pythonCommand = Get-PythonCommand
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "campus_autologin.py") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "configure.py") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "remote_recovery.py") -Destination $InstallDir -Force

$configureScript = Join-Path $InstallDir "configure.py"
$configureArgs = @($pythonCommand.Prefix) + @($configureScript, "--output", $ConfigFile)
if (Test-Path -LiteralPath $ConfigFile) {
    $configureArgs += @("--existing", $ConfigFile)
}

Write-Host "Starting campus network setup. The password will be visible in the console." -ForegroundColor Yellow
& $pythonCommand.Executable @configureArgs
if ($LASTEXITCODE -ne 0) {
    throw "The configuration wizard failed with exit code $LASTEXITCODE."
}
Protect-ConfigFile $ConfigFile

$programScript = Join-Path $InstallDir "campus_autologin.py"
$checkArgs = @($pythonCommand.Prefix) + @($programScript, "--config", $ConfigFile, "--check-config")
& $pythonCommand.Executable @checkArgs
if ($LASTEXITCODE -ne 0) {
    throw "Configuration validation failed with exit code $LASTEXITCODE."
}

try {
    $config = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
    & netsh.exe wlan set profileparameter name="$($config.network_name)" connectionmode=auto | Out-Null
} catch {
    Write-Verbose "The current connection is not a modifiable Wi-Fi profile."
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

$quotedProgram = '"' + $programScript + '"'
$quotedConfig = '"' + $ConfigFile + '"'
$quotedLog = '"' + $LogFile + '"'
$actionArgs = (@($pythonCommand.Prefix) + @($quotedProgram, "--config", $quotedConfig, "--log-file", $quotedLog)) -join " "
$action = New-ScheduledTaskAction -Execute $pythonCommand.BackgroundExecutable -Argument $actionArgs -WorkingDirectory $InstallDir
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Monitor the selected campus network and re-authenticate its captive portal." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

$remoteProgram = Join-Path $InstallDir "remote_recovery.py"
$detectArgs = @($pythonCommand.Prefix) + @($remoteProgram, "--detect")
& $pythonCommand.Executable @detectArgs
$remoteDetectionExitCode = $LASTEXITCODE

Stop-ScheduledTask -TaskName $RemoteTaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $RemoteTaskName -Confirm:$false -ErrorAction SilentlyContinue
if ($remoteDetectionExitCode -eq 0) {
    $remoteLog = Join-Path $ConfigDir "remote-recovery.log"
    $quotedRemoteProgram = '"' + $remoteProgram + '"'
    $quotedRemoteLog = '"' + $remoteLog + '"'
    $remoteActionArgs = (@($pythonCommand.Prefix) + @(
        $quotedRemoteProgram,
        "--log-file",
        $quotedRemoteLog
    )) -join " "
    $remoteAction = New-ScheduledTaskAction `
        -Execute $pythonCommand.BackgroundExecutable `
        -Argument $remoteActionArgs `
        -WorkingDirectory $InstallDir
    Register-ScheduledTask `
        -TaskName $RemoteTaskName `
        -Description "Restart installed remote-control clients after campus network recovery." `
        -Action $remoteAction `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $RemoteTaskName
    Write-Host "Remote-control software detected. Automatic recovery is enabled."
} elseif ($remoteDetectionExitCode -eq 3) {
    Write-Host "No supported remote-control software was detected; recovery was skipped."
} else {
    Write-Warning "Remote-control software detection failed with exit code $remoteDetectionExitCode."
}

Write-Host ""
Write-Host "Installation completed for Windows 10 / 11. The task starts at user logon."
Write-Host "Configuration: $ConfigFile"
Write-Host "Log: $LogFile"
Write-Host "Test now: run .\test-once.ps1 in PowerShell."
