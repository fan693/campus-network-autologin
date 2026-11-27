"""Desktop-side bridge for fixed privileged and per-user controls."""

from __future__ import annotations

import json
import os
import platform
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


def invoke_privileged(action: str, payload: dict[str, Any], timeout: int = 240) -> ControlResult:
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
    except subprocess.TimeoutExpired:
        return ControlResult(False, "operation_timeout")
    except OSError:
        return ControlResult(False, "control_unavailable")
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


def set_master_enabled(enabled: bool) -> ControlResult:
    if platform.system() == "Windows":
        from . import windows
        return ControlResult(*windows.set_master_enabled(enabled))
    return invoke_privileged("set-enabled", {"enabled": enabled})


def reauthenticate() -> ControlResult:
    if platform.system() == "Windows":
        from . import windows
        return ControlResult(*windows.reauthenticate())
    return invoke_privileged("reauthenticate", {})


def replace_profile(profile: dict[str, str]) -> ControlResult:
    if platform.system() == "Windows":
        from . import windows
        return ControlResult(*windows.replace_profile(profile))
    return invoke_privileged("replace-profile", profile)
