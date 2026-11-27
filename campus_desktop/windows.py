"""Per-user Windows adapter for the desktop preview and its installed tasks."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import campus_autologin as core
from campus_control import ControlError, config_with_profile, strict_object

CAMPUS_TASK = "CampusNetworkAutoLogin"
REMOTE_TASK = "CampusRemoteRecovery"
CONTROL_LOCK = threading.Lock()

# Only fixed scripts reach PowerShell. Network credentials are handled by Python.
PREAMBLE = """
$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
"""
TASK_COMMAND = r"""
$names = @('CampusNetworkAutoLogin','CampusRemoteRecovery')
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
function Find-OwnTask([string]$name) {
    if ($name -notin $names) { throw 'invalid task' }
    $t = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue
    if (!$t) { return }
    $owner = [string]$t.Principal.UserId
    if ($owner -notlike 'S-1-*') {
        $owner = ([Security.Principal.NTAccount]$owner).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    if ($owner -ne $sid.Value) { throw 'foreign task' }
    $t
}
function Running($task) { $task.State -in @('Running','Queued') }
function Wait-Running([string]$name, [bool]$wanted) {
    for ($i = 0; $i -lt 40; $i++) {
        $task = Find-OwnTask $name
        if ($task -and ((Running $task) -eq $wanted)) { return }
        Start-Sleep -Milliseconds 250
    }
    throw 'task state did not converge'
}
if ($request.action -eq 'snapshot') {
    $states = @{}
    foreach ($name in $names) {
        $task = Find-OwnTask $name
        if ($task) {
            $states[$name] = @{enabled=[bool]$task.Settings.Enabled; running=(Running $task)}
        }
    }
    ConvertTo-Json -InputObject $states -Depth 3 -Compress
} elseif ($request.action -eq 'apply') {
    $name = [string]$request.name
    $task = Find-OwnTask $name
    if (!$task) { throw 'missing task' }
    if ((Running $task) -and !$request.running) {
        $task | Stop-ScheduledTask
        Wait-Running $name $false
    }
    if ($request.enabled -and !$task.Settings.Enabled) { $task | Enable-ScheduledTask | Out-Null }
    if ($request.running -and !(Running (Find-OwnTask $name))) {
        $task | Start-ScheduledTask
        Wait-Running $name $true
    }
    if (!$request.enabled -and $task.Settings.Enabled) { $task | Disable-ScheduledTask | Out-Null }
} else { throw 'invalid action' }
"""
PRIVATE_DIRECTORY = r"""
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetOwner($me)
$acl.SetAccessRuleProtection($true, $false)
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    $me, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
[IO.Directory]::CreateDirectory([string]$request.path, $acl) | Out-Null
if ($request.profile) {
    $fileAcl = [Security.AccessControl.FileSecurity]::new()
    $fileAcl.SetOwner($me)
    $fileAcl.SetAccessRuleProtection($true, $false)
    $fileAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($me, 'FullControl', 'Allow'))
    foreach ($name in @('original', 'candidate', 'restore')) {
        $file = [IO.File]::Create((Join-Path $request.path $name), 4096, 'None', $fileAcl)
        $file.Dispose()
    }
}
"""
ADAPTER_DETAIL = r"""
$adapter = Get-NetAdapter -InterfaceIndex $request.index
$profile = Get-NetConnectionProfile -InterfaceIndex $request.index | Select-Object -First 1
$kind = switch ([int]$adapter.NdisPhysicalMedium) {
    1 { 'wifi' }; 9 { 'wifi' }; 14 { 'ethernet' }; default { 'unknown' }
}
@{name=$profile.Name; kind=$kind} | ConvertTo-Json -Compress
"""


def powershell(script: str, request: dict[str, Any]) -> dict[str, Any]:
    command = Path(os.environ.get("SystemRoot", r"C:\Windows"),
                   "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    try:
        result = subprocess.run(
            [str(command), "-NoProfile", "-NonInteractive", "-Command", PREAMBLE + script],
            input=json.dumps(request).encode("utf-8"), capture_output=True, timeout=35,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControlError("control_unavailable") from exc
    if result.returncode:
        raise ControlError("service_operation_failed")
    try:
        response = json.loads(result.stdout) if result.stdout.strip() else {}
        if not isinstance(response, dict):
            raise ControlError("invalid_response")
        return response
    except (ValueError, UnicodeError) as exc:
        raise ControlError("invalid_response") from exc


@dataclass(frozen=True)
class TaskState:
    enabled: bool
    running: bool


def snapshots() -> dict[str, TaskState]:
    raw = powershell(TASK_COMMAND, {"action": "snapshot"})
    return {name: TaskState(**raw[name]) for name in (CAMPUS_TASK, REMOTE_TASK) if name in raw}


def apply_task(name: str, state: TaskState) -> None:
    powershell(TASK_COMMAND, {"action": "apply", "name": name, **asdict(state)})


def read_status() -> dict[str, Any]:
    status: dict[str, Any] = {
        "network_state": "unknown", "interface": "--", "internet_state": "unknown",
        "connection_name": "未检测到", "connection_type": "unknown",
    }
    try:
        tasks = snapshots()
    except ControlError:
        tasks = None
    for label, name in (("campus", CAMPUS_TASK), ("remote", REMOTE_TASK)):
        state = tasks.get(name) if tasks is not None else None
        status[label + "_service_name"] = name if state else ""
        status[label + "_enabled"] = state.enabled if state else None
        if state:
            status[label + "_service"] = "active" if state.running else "inactive"
        else:
            status[label + "_service"] = "unknown" if tasks is None else "missing"
    try:
        config = core.load_config(core.default_config_path())
        selected = core.select_network(config)
        status["network_state"] = selected.state
        status["interface"] = selected.interface or "--"
        if selected.state != "connected":
            return status
        status["connection_name"] = config.network_name if config.network_name != "*" else "已连接"
        binding = core.interface_binding(selected.interface, config.timeout)
        if binding:
            opener = core.build_opener(follow_redirects=False, binding=binding)
            status["internet_state"] = "online" if core.internet_online(opener, config) else "offline"
            info = powershell(ADAPTER_DETAIL, {"index": binding.index})
            status["connection_name"] = info.get("name") or status["connection_name"]
            status["connection_type"] = info.get("kind") or "unknown"
    except (ControlError, ValueError, OSError):
        pass
    return status


def write_bytes(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def marker(directory: Path, state: dict[str, Any]) -> None:
    pending = directory / "marker.new"
    write_bytes(pending, json.dumps(state).encode("ascii"))
    os.replace(pending, directory / "marker.json")


def recover_transaction(directory: Path, config_path: Path) -> None:
    if not directory.exists():
        return
    marker_path = directory / "marker.json"
    if marker_path.is_file():
        saved = json.loads(marker_path.read_bytes())
        if not saved.get("committed"):
            if saved.get("profile"):
                backup = directory / "original"
                restore = directory / "restore"
                write_bytes(restore, backup.read_bytes())
                os.replace(restore, config_path)
            for name, state in reversed(list(saved["tasks"].items())):
                if name not in (CAMPUS_TASK, REMOTE_TASK):
                    raise ControlError("partial_failure")
                if saved.get("profile") and state["running"]:
                    apply_task(name, TaskState(state["enabled"], False))
                apply_task(name, TaskState(**state))
    shutil.rmtree(directory)


@contextmanager
def mutation():
    """Allow one desktop operation across windows/processes and recover interrupted ones."""
    if not CONTROL_LOCK.acquire(blocking=False):
        raise ControlError("operation_conflict")
    try:
        path = core.default_config_path()
        if not path.parent.is_dir():
            raise ControlError("config_missing")
        # The GUI also has a per-user instance lock. This file lock covers commands
        # from another desktop process during startup, shutdown or a previous crash.
        import msvcrt
        with (path.parent / ".desktop-control.lock").open("a+b") as lock:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ControlError("operation_conflict") from exc
            directory = path.parent / ".desktop-transaction"
            try:
                recover_transaction(directory, path)
            except (ValueError, OSError, ControlError) as exc:
                raise ControlError("partial_failure") from exc
            yield path, directory
    finally:
        CONTROL_LOCK.release()


def recover_on_startup() -> tuple[bool, str]:
    """Finish an interrupted change before reporting the current service state."""
    if not (core.default_config_path().parent / ".desktop-transaction").is_dir():
        return True, "ok"
    try:
        with mutation():
            pass
        return True, "ok"
    except ControlError as exc:
        return False, exc.code
    except OSError:
        return False, "partial_failure"


def run_operation(action: str, value: Any = None) -> tuple[bool, str]:
    try:
        with mutation() as (path, directory):
            tasks = snapshots()
            if CAMPUS_TASK not in tasks:
                raise ControlError("service_missing")
            if action != "switch":
                tasks = {CAMPUS_TASK: tasks[CAMPUS_TASK]}
            state = tasks[CAMPUS_TASK]
            if action == "profile":
                if not path.is_file():
                    raise ControlError("config_missing")
                original = path.read_bytes()
                if len(original) > 1_048_576:
                    raise ControlError("config_invalid")
                candidate = config_with_profile(strict_object(original.decode("utf-8")), value)
                core.validate_config(candidate)
            elif action == "check":
                config = core.load_config(path)
            powershell(PRIVATE_DIRECTORY, {"path": str(directory), "profile": action == "profile"})
            saved = {"tasks": {name: asdict(s) for name, s in tasks.items()}, "profile": action == "profile"}
            try:
                if action == "profile":
                    write_bytes(directory / "original", original)
                    content = (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                    write_bytes(directory / "candidate", content)
                marker(directory, saved)
                if action == "switch":
                    for name, previous in tasks.items():
                        if previous != TaskState(value, value):
                            apply_task(name, TaskState(value, value))
                else:
                    if state.running:
                        apply_task(CAMPUS_TASK, TaskState(state.enabled, False))
                    if action == "profile":
                        os.replace(directory / "candidate", path)
                    else:
                        result = core.run(config, once=True)
                        if result:
                            raise ControlError("network_unavailable" if result == 2 else "authentication_failed")
                    if state.running:
                        apply_task(CAMPUS_TASK, state)
                marker(directory, {"committed": True})
            finally:
                try:
                    recover_transaction(directory, path)
                except (ValueError, OSError, ControlError) as exc:
                    raise ControlError("partial_failure") from exc
        return True, "ok"
    except ControlError as exc:
        return False, exc.code
    except (ValueError, OSError, TypeError):
        return False, "config_invalid"


def set_master_enabled(enabled: bool) -> tuple[bool, str]:
    if type(enabled) is not bool:
        return False, "invalid_request"
    return run_operation("switch", enabled)


def reauthenticate() -> tuple[bool, str]:
    return run_operation("check")


def replace_profile(profile: dict[str, str]) -> tuple[bool, str]:
    return run_operation("profile", profile)
