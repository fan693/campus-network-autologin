#!/usr/bin/env python3
"""Fixed privileged operations for Campus Network Assistant.

This helper is intended to be installed as a root-owned file. The desktop GUI
invokes it through pkexec and sends bounded JSON over stdin, so credentials
never appear in argv, environment variables, or logs.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Iterator, Optional

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


def unit_snapshot(name: str, prefix: list[str]) -> Optional[dict[str, Any]]:
    result = subprocess.run(
        [*prefix, "show", name, "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState"],
        check=False, capture_output=True, text=True, timeout=10,
    )
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if state.get("LoadState") == "not-found":
        return None
    if result.returncode or state.get("LoadState") != "loaded":
        raise ControlError("service_operation_failed")
    return {
        "name": name,
        "running": state.get("ActiveState") in ("active", "activating", "reloading"),
        "enabled": state.get("UnitFileState") in ("enabled", "enabled-runtime", "linked", "linked-runtime"),
    }


def preferred_unit(candidates: tuple[str, ...], prefix: list[str]) -> Optional[dict[str, Any]]:
    installed = [state for name in candidates if (state := unit_snapshot(name, prefix)) is not None]
    return max(installed, key=lambda state: (state["running"], state["enabled"]), default=None)


def loaded_service() -> str:
    state = preferred_unit(SERVICE_CANDIDATES, [str(SYSTEMCTL)])
    if state is None:
        raise ControlError("service_missing")
    return state["name"]


def installed_config(service: str) -> Path:
    paths = CONFIG_CANDIDATES
    if len(paths) == len(SERVICE_CANDIDATES):
        paths = (paths[SERVICE_CANDIDATES.index(service)],)
    for path in paths:
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
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, stat.S_IMODE(mode))
            os.fsync(descriptor)
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
        sync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def control_lock() -> Iterator[None]:
    import fcntl
    TRANSACTION_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    sync_directory(TRANSACTION_ROOT.parent)
    descriptor = os.open(TRANSACTION_ROOT.parent / "control.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControlError("control_busy") from exc
        yield
    finally:
        os.close(descriptor)


def service_running(name: str) -> bool:
    result = subprocess.run(
        [str(SYSTEMCTL), "show", name, "-p", "ActiveState", "--value"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode or result.stdout.strip() not in {
        "active", "reloading", "activating", "deactivating", "inactive", "failed"
    }:
        raise ControlError("service_operation_failed")
    return result.stdout.strip() in ("active", "reloading", "activating")


def write_marker(directory: Path, phase: str, config_path: Path, service: str, was_running: bool) -> None:
    marker = directory / "marker.json"
    content = json.dumps({
        "schema_version": 1, "phase": phase, "config_path": str(config_path),
        "service": service, "was_running": was_running,
    }, separators=(",", ":")).encode("utf-8")
    temporary = directory / f"marker.{uuid.uuid4().hex}.tmp"
    write_private(temporary, content, 0o600, 0, 0)
    os.replace(temporary, marker)
    sync_directory(directory)


def restore_transaction(directory: Path, target: Path, service: str, was_running: bool) -> None:
    replace_config(target, (directory / "config.backup").read_bytes(), target.lstat())
    if was_running:
        run_systemctl("restart", service)


REMOTE_SERVICES = ("campus-remote-recovery.service", "todesk-monitor.service")


def user_systemctl(uid: int) -> list[str]:
    import pwd
    account = pwd.getpwuid(uid)
    runner = shutil.which("runuser", path="/usr/sbin:/usr/bin:/sbin")
    if not runner:
        raise ControlError("control_unavailable")
    runtime = f"/run/user/{uid}"
    return [runner, "-u", account.pw_name, "--", "/usr/bin/env", "-i",
            f"HOME={account.pw_dir}", f"XDG_RUNTIME_DIR={runtime}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus", str(SYSTEMCTL), "--user"]


def apply_unit_state(state: dict[str, Any], prefix: list[str], enabled: bool, running: bool) -> None:
    name = state["name"]
    if enabled != state["enabled"]:
        operation = "enable" if enabled else "disable"
        invoke_unit(prefix, operation, name)
    if running != state["running"]:
        operation = "start" if running else "stop"
        invoke_unit(prefix, operation, name)


def invoke_unit(prefix: list[str], operation: str, service: str) -> None:
    result = subprocess.run([*prefix, operation, service], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False, timeout=30)
    if result.returncode:
        raise ControlError("service_operation_failed")


def restore_units(marker: dict[str, Any]) -> None:
    uid = marker["uid"]
    for saved in reversed(marker["units"]):
        name = saved["name"]
        if name in SERVICE_CANDIDATES:
            prefix = [str(SYSTEMCTL)]
        elif name in REMOTE_SERVICES and type(uid) is int and uid > 0:
            prefix = user_systemctl(uid)
        else:
            raise ControlError("partial_failure")
        current = unit_snapshot(name, prefix)
        if current is None:
            raise ControlError("partial_failure")
        apply_unit_state(current, prefix, saved["enabled"], saved["running"])


def set_master_enabled(enabled: bool) -> None:
    prefix = [str(SYSTEMCTL)]
    campus = preferred_unit(SERVICE_CANDIDATES, prefix)
    if campus is None:
        raise ControlError("service_missing")
    units = [(campus, prefix)]
    uid = int(os.environ.get("PKEXEC_UID", "0"))
    if uid > 0:
        user_prefix = user_systemctl(uid)
        remote = preferred_unit(REMOTE_SERVICES, user_prefix)
        if remote:
            units.append((remote, user_prefix))
    transaction = TRANSACTION_ROOT / str(uuid.uuid4())
    transaction.mkdir(mode=0o700)
    sync_directory(TRANSACTION_ROOT)
    marker = {"schema_version": 1, "units": [state for state, _ in units], "uid": uid}
    marker_path = transaction / "units.json"
    temporary = transaction / "units.tmp"
    write_private(temporary, json.dumps(marker).encode("utf-8"), 0o600, 0, 0)
    os.replace(temporary, marker_path)
    sync_directory(transaction)
    try:
        for state, command in units:
            apply_unit_state(state, command, enabled, enabled)
    except (ControlError, OSError, subprocess.SubprocessError):
        try:
            restore_units(marker)
        except (ControlError, OSError, subprocess.SubprocessError) as exc:
            raise ControlError("partial_failure") from exc
        marker_path.unlink()
        sync_directory(transaction)
        shutil.rmtree(transaction)
        raise ControlError("service_operation_failed")
    marker_path.unlink()
    sync_directory(transaction)
    shutil.rmtree(transaction)
    sync_directory(TRANSACTION_ROOT)


def recover_incomplete_transactions() -> None:
    if not TRANSACTION_ROOT.exists():
        return
    allowed = {str(path) for path in CONFIG_CANDIDATES}
    for directory in TRANSACTION_ROOT.iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        units_path = directory / "units.json"
        if units_path.exists():
            restore_units(strict_object(units_path.read_text(encoding="utf-8")))
        marker_path = directory / "marker.json"
        if marker_path.exists():
            marker = strict_object(marker_path.read_text(encoding="utf-8"))
            path = marker.get("config_path")
            phase = marker.get("phase")
            if path not in allowed or phase not in ("prepared", "applied", "committed"):
                raise ControlError("partial_failure")
            if phase != "committed":
                service = marker.get("service") or loaded_service()
                if service not in SERVICE_CANDIDATES:
                    raise ControlError("partial_failure")
                was_running = marker.get("was_running")
                if was_running is None:  # Transactions from previous preview versions
                    was_running = service_running(service)
                restore_transaction(directory, Path(path), service, was_running)
        shutil.rmtree(directory)
        sync_directory(TRANSACTION_ROOT)


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


def portal_origin(portal: dict[str, Any]) -> Optional[tuple[str, str, int]]:
    try:
        url = urlsplit(portal.get("base_url" if portal.get("type") == "srun" else "login_url", ""))
        if url.scheme not in ("http", "https") or not url.hostname:
            return None
        return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)
    except (ValueError, TypeError):
        return None


def config_with_profile(raw: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    if set(profile) != PROFILE_KEYS:
        raise ControlError("invalid_request")
    username = checked_text(profile["username"], "username", 256)
    password = checked_text(profile["password"], "password", 1024)
    legacy = "portal" not in raw and "student_id" in raw
    kind = profile["school_type"]
    if legacy and kind not in ("cqu", "keep"):
        raise ControlError("legacy_service_incompatible")
    candidate = dict(raw)
    candidate["student_id" if legacy else "username"] = username
    candidate["password"] = password
    if kind == "keep" or legacy:
        return candidate
    portal = portal_from_profile(profile)
    previous = raw.get("portal", {})
    origin = portal_origin(portal)
    if (isinstance(previous, dict) and previous.get("type") == portal["type"]
            and kind != "cqu" and origin is not None and portal_origin(previous) == origin):
        editable = {
            "drcom": ("login_url", "account_prefix"),
            "generic": ("login_url", "method", "success_contains"),
            "srun": ("base_url", "ac_id"),
        }[kind]
        merged = dict(previous)
        if kind == "srun" and str(previous.get("base_url", "")).rstrip("/") != portal["base_url"].rstrip("/"):
            # Absolute overrides refer to the previous server.
            merged.pop("login_url", None)
            merged.pop("challenge_url", None)
        merged.update({key: portal[key] for key in editable})
        portal = merged
    candidate["portal"] = portal
    return candidate


def update_profile(profile: dict[str, Any]) -> None:
    service = loaded_service()
    path = installed_config(service)
    metadata = path.lstat()
    original = path.read_bytes()
    if len(original) > MAX_CONFIG_BYTES:
        raise ControlError("config_invalid")
    try:
        raw = strict_object(original.decode("utf-8"))
    except UnicodeDecodeError:
        raise ControlError("config_invalid")
    candidate = (json.dumps(config_with_profile(raw, profile), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    was_running = service_running(service)
    TRANSACTION_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    transaction = TRANSACTION_ROOT / str(uuid.uuid4())
    transaction.mkdir(mode=0o700)
    sync_directory(TRANSACTION_ROOT)
    keep_backup = False
    try:
        write_private(transaction / "config.backup", original, 0o600, 0, 0)
        candidate_path = transaction / "config.candidate"
        write_private(candidate_path, candidate, 0o600, 0, 0)
        try:
            campus_autologin.load_config(candidate_path)
        except ValueError as exc:
            raise ControlError("config_invalid") from exc
        write_marker(transaction, "prepared", path, service, was_running)
        keep_backup = True
        try:
            replace_config(path, candidate, metadata)
            if was_running:
                run_systemctl("restart", service)
            write_marker(transaction, "committed", path, service, was_running)
        except (OSError, ControlError):
            try:
                restore_transaction(transaction, path, service, was_running)
            except (OSError, ControlError) as exc:
                raise ControlError("partial_failure") from exc
            keep_backup = False
            raise
        keep_backup = False
    finally:
        if not keep_backup:
            shutil.rmtree(transaction)
            sync_directory(TRANSACTION_ROOT)


def update_account(username: str, password: str) -> None:
    profile = dict.fromkeys(PROFILE_KEYS, "")
    profile.update(username=username, password=password, school_type="keep")
    update_profile(profile)


def reauthenticate() -> None:
    service = loaded_service()
    if service_running(service):
        run_systemctl("try-restart", service)
        return
    runner = shutil.which("runuser", path="/usr/sbin:/usr/bin:/sbin")
    if runner is None:
        raise ControlError("control_unavailable")
    config = installed_config(service)
    program = Path(__file__).resolve().with_name("campus_autologin.py")
    command = [runner, "-u", "campus-autologin", "--", "/usr/bin/python3",
               str(program), "--config", str(config), "--once"]
    result = subprocess.run(command, check=False, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=100)
    if result.returncode == 2:
        raise ControlError("network_unavailable")
    if result.returncode == 4:
        raise ControlError("authentication_failed")
    if result.returncode:
        raise ControlError("one_shot_failed")


def handle(action: str, request: dict[str, Any]) -> None:
    if action == "set-enabled":
        if set(request) != {"enabled"} or not isinstance(request["enabled"], bool):
            raise ControlError("invalid_request")
        set_master_enabled(request["enabled"])
        return
    if action == "reauthenticate":
        if request:
            raise ControlError("invalid_request")
        reauthenticate()
        return
    if action == "replace-profile":
        update_profile(request)
        return
    raise ControlError("invalid_action")


def main() -> int:
    if os.geteuid() != 0:
        return emit(False, "permission_denied")
    try:
        if len(sys.argv) != 2:
            raise ControlError("invalid_action")
        request = read_request()
        with control_lock():
            recover_incomplete_transactions()
            handle(sys.argv[1], request)
    except ControlError as exc:
        return emit(False, exc.code)
    except (OSError, subprocess.SubprocessError, ValueError):
        return emit(False, "internal_error")
    return emit(True, "ok")


if __name__ == "__main__":
    sys.exit(main())
