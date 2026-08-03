"""Desktop-side bridge for fixed privileged and per-user controls."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


PRIVILEGED_HELPER = Path("/usr/local/lib/campus-autologin/campus_control.py")
PYTHON = Path("/usr/bin/python3")
PKEXEC = Path("/usr/bin/pkexec")
REMOTE_SERVICES = ("campus-remote-recovery.service", "todesk-monitor.service")


@dataclass(frozen=True)
class ControlResult:
    ok: bool
    code: str


def trusted_helper() -> Optional[Path]:
    try:
        metadata = PRIVILEGED_HELPER.lstat()
    except OSError:
        return None
    writable = stat.S_IWGRP | stat.S_IWOTH
    if (
        not stat.S_ISREG(metadata.st_mode)
        or PRIVILEGED_HELPER.is_symlink()
        or metadata.st_uid != 0
        or metadata.st_mode & writable
    ):
        return None
    return PRIVILEGED_HELPER


def invoke_privileged(action: str, payload: dict[str, Any], timeout: int = 120) -> ControlResult:
    helper = trusted_helper()
    if helper is None or not PKEXEC.is_file() or not PYTHON.is_file():
        return ControlResult(False, "control_unavailable")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        result = subprocess.run(
            [str(PKEXEC), str(PYTHON), str(helper), action],
            input=body,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={key: value for key, value in os.environ.items() if key in {
                "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE", "LANG", "LC_ALL",
            }},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ControlResult(False, "operation_timeout")
    if result.returncode != 0 and not result.stdout.strip():
        return ControlResult(False, "permission_denied")
    try:
        response = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return ControlResult(False, "invalid_response")
    if not isinstance(response, dict):
        return ControlResult(False, "invalid_response")
    ok = response.get("ok") is True
    code = response.get("code")
    return ControlResult(ok, code if isinstance(code, str) else "invalid_response")


def user_service() -> Optional[str]:
    for service in REMOTE_SERVICES:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show", service, "-p", "LoadState", "--value"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.strip() == "loaded":
            return service
    return None


def set_user_service(service: str, enabled: bool) -> bool:
    operation = "enable" if enabled else "disable"
    try:
        result = subprocess.run(
            ["systemctl", "--user", operation, "--now", service],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def set_master_enabled(enabled: bool) -> ControlResult:
    result = invoke_privileged("set-enabled", {"enabled": enabled})
    if not result.ok:
        return result
    remote = user_service()
    if remote is None or set_user_service(remote, enabled):
        return result
    rollback = invoke_privileged("set-enabled", {"enabled": not enabled})
    return ControlResult(False, "remote_service_failed" if rollback.ok else "partial_failure")


def reauthenticate() -> ControlResult:
    return invoke_privileged("reauthenticate", {})


def replace_profile(profile: dict[str, str]) -> ControlResult:
    return invoke_privileged(
        "replace-profile",
        profile,
    )
