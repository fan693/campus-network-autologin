#!/usr/bin/env python3
"""Fixed privileged operations for Campus Network Assistant.

This helper is intended to be installed as a root-owned file. The desktop GUI
invokes it through pkexec and sends bounded JSON over stdin, so credentials
never appear in argv, environment variables, or logs.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

import campus_autologin


CONFIG_CANDIDATES = (
    Path("/etc/campus-autologin/config.json"),
    Path("/etc/cqu-autologin/config.json"),
)
SERVICE_CANDIDATES = (
    "campus-autologin.service",
    "cqu-autologin.service",
)
TRANSACTION_ROOT = Path("/var/lib/campus-autologin/transactions")
SYSTEMCTL = Path("/usr/bin/systemctl")
MAX_REQUEST_BYTES = 65_536
MAX_CONFIG_BYTES = 1_048_576


class ControlError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def emit(ok: bool, code: str) -> int:
    print(json.dumps({"schema_version": 1, "ok": ok, "code": code}, separators=(",", ":")))
    return 0 if ok else 1


def strict_object(raw: str) -> dict[str, Any]:
    def reject_constant(_value: str) -> None:
        raise ControlError("invalid_request")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ControlError("invalid_request")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ControlError("invalid_request")
    if not isinstance(value, dict):
        raise ControlError("invalid_request")
    return value


def read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ControlError("invalid_request")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ControlError("invalid_request")
    return strict_object(text)


def loaded_service() -> str:
    for service in SERVICE_CANDIDATES:
        result = subprocess.run(
            [str(SYSTEMCTL), "show", service, "-p", "LoadState", "--value"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip() == "loaded":
            return service
    raise ControlError("service_missing")


def installed_config() -> Path:
    for path in CONFIG_CANDIDATES:
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode) and not path.is_symlink():
            return path
    raise ControlError("config_missing")


def run_systemctl(*arguments: str) -> None:
    try:
        result = subprocess.run(
            [str(SYSTEMCTL), *arguments],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ControlError("service_operation_failed")
    if result.returncode != 0:
        raise ControlError("service_operation_failed")


def checked_text(value: Any, key: str, maximum: int, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value.strip())
        or len(value) > maximum
    ):
        raise ControlError(f"invalid_{key}")
    if any(ord(character) < 32 for character in value):
        raise ControlError(f"invalid_{key}")
    return value.strip() if key == "username" else value


def write_private(path: Path, content: bytes, mode: int, uid: int, gid: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.fchmod(descriptor, stat.S_IMODE(mode))
        os.fchown(descriptor, uid, gid)
    finally:
        os.close(descriptor)


def replace_config(path: Path, content: bytes, metadata: os.stat_result) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        write_private(
            temporary,
            content,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
        )
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_marker(directory: Path, phase: str, config_path: Path) -> None:
    marker = directory / "marker.json"
    content = json.dumps(
        {"schema_version": 1, "phase": phase, "config_path": str(config_path)},
        separators=(",", ":"),
    ).encode("utf-8")
    temporary = directory / f"marker.{uuid.uuid4().hex}.tmp"
    write_private(temporary, content, 0o600, 0, 0)
    os.replace(temporary, marker)


def recover_incomplete_transactions() -> None:
    try:
        transactions = list(TRANSACTION_ROOT.iterdir())
    except OSError:
        return
    allowed = {str(path) for path in CONFIG_CANDIDATES}
    for directory in transactions:
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            marker = strict_object((directory / "marker.json").read_text(encoding="utf-8"))
            config_path = marker.get("config_path")
            phase = marker.get("phase")
            backup = directory / "config.backup"
            if phase == "applied" and config_path in allowed and backup.is_file():
                target = Path(config_path)
                metadata = target.lstat()
                replace_config(target, backup.read_bytes(), metadata)
            shutil.rmtree(directory)
        except (ControlError, OSError, ValueError):
            continue


PROFILE_KEYS = {
    "username",
    "password",
    "school_type",
    "login_url",
    "account_prefix",
    "base_url",
    "ac_id",
    "method",
    "success_contains",
}


def portal_from_profile(profile: dict[str, Any]) -> dict[str, Any]:
    school_type = profile.get("school_type")
    if school_type == "cqu":
        return {
            "type": "drcom",
            "login_url": "https://login.cqu.edu.cn:802/eportal/portal/login",
            "account_prefix": ",0,",
        }
    if school_type == "drcom":
        return {
            "type": "drcom",
            "login_url": checked_text(profile.get("login_url"), "portal", 2048),
            "account_prefix": checked_text(
                profile.get("account_prefix"), "portal", 256, allow_empty=True
            ),
        }
    if school_type == "srun":
        return {
            "type": "srun",
            "base_url": checked_text(profile.get("base_url"), "portal", 2048),
            "ac_id": checked_text(profile.get("ac_id"), "portal", 64),
            "n": "200",
            "type_value": "1",
            "enc_ver": "srun_bx1",
            "base64_alphabet": campus_autologin.SRUN_ALPHABET,
        }
    if school_type == "generic":
        method = profile.get("method")
        if method not in ("GET", "POST"):
            raise ControlError("invalid_portal")
        marker = checked_text(profile.get("success_contains"), "portal", 512)
        return {
            "type": "generic",
            "login_url": checked_text(profile.get("login_url"), "portal", 2048),
            "method": method,
            "parameters": {
                "username": "{username}",
                "password": "{password}",
                "user_ip": "{ipv4}",
            },
            "headers": {},
            "success_contains": [marker],
            "already_online_contains": [],
        }
    raise ControlError("invalid_school_type")


def update_profile(profile: dict[str, Any]) -> None:
    if set(profile) != PROFILE_KEYS:
        raise ControlError("invalid_request")
    username = checked_text(profile["username"], "username", 256)
    password = checked_text(profile["password"], "password", 1024)
    portal = portal_from_profile(profile)
    path = installed_config()
    metadata = path.lstat()
    original = path.read_bytes()
    if len(original) > MAX_CONFIG_BYTES:
        raise ControlError("config_invalid")
    try:
        raw = strict_object(original.decode("utf-8"))
    except UnicodeDecodeError:
        raise ControlError("config_invalid")
    legacy = "portal" not in raw and "student_id" in raw
    if legacy:
        if profile["school_type"] != "cqu":
            raise ControlError("legacy_service_incompatible")
        candidate_config = dict(raw)
        candidate_config["student_id"] = username
        candidate_config["password"] = password
        validation_config = campus_autologin.old_config_to_current(candidate_config)
    else:
        candidate_config = dict(raw)
        candidate_config["username"] = username
        candidate_config["password"] = password
        candidate_config["portal"] = portal
        validation_config = candidate_config
    candidate = (
        json.dumps(candidate_config, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    TRANSACTION_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(TRANSACTION_ROOT, 0o700)
    transaction = TRANSACTION_ROOT / str(uuid.uuid4())
    transaction.mkdir(mode=0o700)
    backup = transaction / "config.backup"
    candidate_path = transaction / "config.candidate"
    try:
        write_private(backup, original, 0o600, 0, 0)
        write_private(candidate_path, candidate, 0o600, 0, 0)
        validation_path = transaction / "config.validation"
        write_private(
            validation_path,
            (json.dumps(validation_config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            0o600,
            0,
            0,
        )
        try:
            campus_autologin.load_config(validation_path)
        except ValueError:
            raise ControlError("config_invalid")
        write_marker(transaction, "prepared", path)
        replace_config(path, candidate, metadata)
        write_marker(transaction, "applied", path)
        try:
            run_systemctl("restart", loaded_service())
        except ControlError:
            replace_config(path, original, metadata)
            try:
                run_systemctl("restart", loaded_service())
            except ControlError:
                pass
            raise
        write_marker(transaction, "committed", path)
    finally:
        shutil.rmtree(transaction, ignore_errors=True)


def update_account(username: str, password: str) -> None:
    """Compatibility wrapper for the original CQU-only account action."""
    update_profile(
        {
            "username": username,
            "password": password,
            "school_type": "cqu",
            "login_url": "",
            "account_prefix": "",
            "base_url": "",
            "ac_id": "",
            "method": "POST",
            "success_contains": "",
        }
    )


def handle(action: str, request: dict[str, Any]) -> None:
    if action == "set-enabled":
        if set(request) != {"enabled"} or not isinstance(request["enabled"], bool):
            raise ControlError("invalid_request")
        service = loaded_service()
        operation = "enable" if request["enabled"] else "disable"
        run_systemctl(operation, "--now", service)
        return
    if action == "reauthenticate":
        if request:
            raise ControlError("invalid_request")
        run_systemctl("restart", loaded_service())
        return
    if action == "replace-profile":
        update_profile(request)
        return
    raise ControlError("invalid_action")


def main() -> int:
    if os.geteuid() != 0:
        return emit(False, "permission_denied")
    try:
        recover_incomplete_transactions()
        if len(sys.argv) != 2:
            raise ControlError("invalid_action")
        handle(sys.argv[1], read_request())
    except ControlError as exc:
        return emit(False, exc.code)
    except (OSError, subprocess.SubprocessError, ValueError):
        return emit(False, "internal_error")
    return emit(True, "ok")


if __name__ == "__main__":
    sys.exit(main())
