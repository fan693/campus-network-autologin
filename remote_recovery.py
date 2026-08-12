#!/usr/bin/env python3
"""Keep detected remote-control clients available while the internet is online."""

from __future__ import annotations

import argparse
import configparser
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from campus_autologin import (
    DEFAULT_CHECKS,
    ConnectivityCheck,
    build_opener,
    internet_online,
)

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None


APP_NAME = "campus-remote-recovery"
RUNNING = True
LOG_FILE: Optional[Path] = None
TODESK_LOG_DIR = Path(os.environ.get("TODESK_LOG_DIR", "/var/log/todesk"))
TODESK_CONTROL_PORT = 37600
REMOTE_CHECKS = tuple(
    ConnectivityCheck(url=item["url"], status=item["status"], body=item.get("body"))
    for item in DEFAULT_CHECKS
) + (ConnectivityCheck("https://www.baidu.com/", 200),)


class ConnectivityConfig:
    connectivity_checks = REMOTE_CHECKS
    connectivity_policy = "any"
    timeout = 6


@dataclass(frozen=True)
class AppDefinition:
    key: str
    display_name: str
    aliases: tuple[str, ...]
    process_names: tuple[str, ...]
    linux_paths: tuple[str, ...]
    windows_paths: tuple[tuple[str, str], ...]
    executable_names: tuple[str, ...]
    linux_services: tuple[str, ...] = ()
    windows_services: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RemoteApp:
    definition: AppDefinition
    command: tuple[str, ...]

    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def display_name(self) -> str:
        return self.definition.display_name


@dataclass(frozen=True)
class HealthStatus:
    state: str
    reason: str
    failure_threshold: int


APP_DEFINITIONS = (
    AppDefinition(
        key="todesk",
        display_name="ToDesk",
        aliases=("todesk",),
        process_names=("ToDesk", "ToDesk.exe"),
        linux_paths=("/opt/todesk/bin/ToDesk",),
        windows_paths=(
            ("ProgramFiles", "ToDesk/ToDesk.exe"),
            ("ProgramFiles(x86)", "ToDesk/ToDesk.exe"),
            ("LOCALAPPDATA", "ToDesk/ToDesk.exe"),
        ),
        executable_names=("ToDesk.exe",),
        linux_services=("todeskd.service",),
        windows_services=("ToDesk_Service", "ToDeskService"),
        environment={
            "LIBVA_DRIVER_NAME": "iHD",
            "LIBVA_DRIVERS_PATH": "/opt/todesk/bin",
            "GDK_BACKEND": "x11",
        },
    ),
    AppDefinition(
        key="sunlogin",
        display_name="Sunlogin",
        aliases=("sunlogin", "sunloginclient", "oray", "向日葵"),
        process_names=("sunloginclient", "SunloginClient.exe"),
        linux_paths=(
            "/usr/local/sunlogin/bin/sunloginclient",
            "/opt/sunlogin/bin/sunloginclient",
        ),
        windows_paths=(
            ("ProgramFiles", "Oray/SunLogin/SunloginClient/SunloginClient.exe"),
            ("ProgramFiles(x86)", "Oray/SunLogin/SunloginClient/SunloginClient.exe"),
        ),
        executable_names=("SunloginClient.exe",),
        linux_services=("runsunloginclient.service", "sunloginclient.service"),
        windows_services=("SunloginService",),
    ),
    AppDefinition(
        key="anydesk",
        display_name="AnyDesk",
        aliases=("anydesk",),
        process_names=("anydesk", "anydesk.exe"),
        linux_paths=("/usr/bin/anydesk", "/usr/local/bin/anydesk"),
        windows_paths=(
            ("ProgramFiles", "AnyDesk/AnyDesk.exe"),
            ("ProgramFiles(x86)", "AnyDesk/AnyDesk.exe"),
            ("APPDATA", "AnyDesk/AnyDesk.exe"),
        ),
        executable_names=("AnyDesk.exe",),
        linux_services=("anydesk.service",),
        windows_services=("AnyDesk",),
    ),
    AppDefinition(
        key="rustdesk",
        display_name="RustDesk",
        aliases=("rustdesk",),
        process_names=("rustdesk", "rustdesk.exe"),
        linux_paths=("/usr/bin/rustdesk", "/usr/local/bin/rustdesk", "~/.local/bin/rustdesk"),
        windows_paths=(
            ("ProgramFiles", "RustDesk/RustDesk.exe"),
            ("ProgramFiles(x86)", "RustDesk/RustDesk.exe"),
            ("LOCALAPPDATA", "RustDesk/RustDesk.exe"),
        ),
        executable_names=("RustDesk.exe",),
        linux_services=("rustdesk.service",),
        windows_services=("RustDesk",),
    ),
    AppDefinition(
        key="teamviewer",
        display_name="TeamViewer",
        aliases=("teamviewer",),
        process_names=("TeamViewer", "TeamViewer.exe"),
        linux_paths=("/usr/bin/teamviewer", "/opt/teamviewer/tv_bin/script/teamviewer"),
        windows_paths=(
            ("ProgramFiles", "TeamViewer/TeamViewer.exe"),
            ("ProgramFiles(x86)", "TeamViewer/TeamViewer.exe"),
        ),
        executable_names=("TeamViewer.exe",),
        linux_services=("teamviewerd.service",),
        windows_services=("TeamViewer",),
    ),
)


@dataclass
class ConnectivityState:
    failure_threshold: int = 2
    recovery_threshold: int = 2
    state: str = "unknown"
    failures: int = 0
    successes: int = 0

    def observe(self, online: bool) -> Optional[str]:
        if online:
            self.failures = 0
            self.successes += 1
            if self.state == "offline" and self.successes >= self.recovery_threshold:
                self.state = "online"
                return "recovered"
            if self.state == "unknown" and self.successes >= self.recovery_threshold:
                self.state = "online"
                return "online"
            return None

        self.successes = 0
        self.failures += 1
        if self.state != "offline" and self.failures >= self.failure_threshold:
            self.state = "offline"
            return "offline"
        return None


def log(message: str) -> None:
    line = f"{APP_NAME}: {message}"
    try:
        print(line, flush=True)
    except (AttributeError, OSError):
        pass
    if LOG_FILE is None:
        return
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size >= 2 * 1024 * 1024:
            for index in (2, 1):
                source = LOG_FILE.with_suffix(LOG_FILE.suffix + f".{index}")
                target = LOG_FILE.with_suffix(LOG_FILE.suffix + f".{index + 1}")
                if source.exists():
                    target.unlink(missing_ok=True)
                    source.replace(target)
            backup = LOG_FILE.with_suffix(LOG_FILE.suffix + ".1")
            backup.unlink(missing_ok=True)
            LOG_FILE.replace(backup)
        with LOG_FILE.open("a", encoding="utf-8") as output:
            output.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def stop_handler(_signum: int, _frame: object) -> None:
    global RUNNING
    RUNNING = False


def match_definition(text: str) -> Optional[AppDefinition]:
    lowered = text.casefold()
    for definition in APP_DEFINITIONS:
        if any(alias.casefold() in lowered for alias in definition.aliases):
            return definition
    return None


def executable_from_desktop(path: Path) -> tuple[str, ...]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path, encoding="utf-8")
        value = parser.get("Desktop Entry", "Exec", fallback="")
        fields = shlex.split(value)
    except (OSError, configparser.Error, ValueError):
        return ()
    fields = [field for field in fields if not re.search(r"%[fFuUdDnNickvm]", field)]
    if fields and fields[0] == "env":
        fields.pop(0)
        while fields and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", fields[0]):
            fields.pop(0)
    if not fields:
        return ()
    executable = fields[0]
    resolved = executable if os.path.isabs(executable) else shutil.which(executable)
    if not resolved or not Path(resolved).is_file():
        return ()
    fields[0] = resolved
    return tuple(fields)


def default_application_dirs() -> list[Path]:
    return [
        Path.home() / ".config/autostart",
        Path.home() / ".local/share/applications",
        Path.home() / ".local/share/flatpak/exports/share/applications",
        Path("/etc/xdg/autostart"),
        Path("/usr/local/share/applications"),
        Path("/usr/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
    ]


def detect_linux_apps(application_dirs: Optional[Iterable[Path]] = None) -> list[RemoteApp]:
    found: dict[str, RemoteApp] = {}
    for directory in application_dirs or default_application_dirs():
        if not directory.is_dir():
            continue
        for desktop_file in directory.glob("*.desktop"):
            try:
                text = desktop_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            definition = match_definition(f"{desktop_file.name}\n{text}")
            if definition is None or definition.key in found:
                continue
            command = executable_from_desktop(desktop_file)
            if command:
                found[definition.key] = RemoteApp(definition, command)

    for definition in APP_DEFINITIONS:
        if definition.key in found:
            continue
        for raw_path in definition.linux_paths:
            path = Path(raw_path).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                found[definition.key] = RemoteApp(definition, (str(path),))
                break
    return [found[item.key] for item in APP_DEFINITIONS if item.key in found]


def windows_registry_entries() -> list[tuple[str, str, str]]:
    if platform.system() != "Windows":
        return []
    try:
        import winreg
    except ImportError:
        return []
    entries: list[tuple[str, str, str]] = []
    locations = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, location in locations:
        try:
            root = winreg.OpenKey(hive, location)
        except OSError:
            continue
        with root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    child = winreg.OpenKey(root, winreg.EnumKey(root, index))
                    with child:
                        values = []
                        for name in ("DisplayName", "InstallLocation", "DisplayIcon"):
                            try:
                                values.append(str(winreg.QueryValueEx(child, name)[0]))
                            except OSError:
                                values.append("")
                        entries.append(tuple(values))
                except OSError:
                    continue
    return entries


def executable_from_icon(value: str) -> Optional[Path]:
    quoted = re.match(r'^"([^\"]+\.exe)"', value, flags=re.IGNORECASE)
    plain = re.match(r"^(.+?\.exe)(?:,\s*-?\d+)?$", value.strip(), flags=re.IGNORECASE)
    match = quoted or plain
    return Path(os.path.expandvars(match.group(1))) if match else None


def detect_windows_apps(
    environ: Optional[Mapping[str, str]] = None,
    registry_entries: Optional[Iterable[tuple[str, str, str]]] = None,
) -> list[RemoteApp]:
    variables = os.environ if environ is None else environ
    entries = windows_registry_entries() if registry_entries is None else list(registry_entries)
    found: dict[str, RemoteApp] = {}

    for definition in APP_DEFINITIONS:
        candidates: list[Path] = []
        for variable, relative in definition.windows_paths:
            base = variables.get(variable, "")
            if base:
                candidates.append(Path(base) / Path(relative))
        for display_name, install_location, display_icon in entries:
            if match_definition(display_name) != definition:
                continue
            icon_path = executable_from_icon(display_icon)
            if icon_path is not None:
                candidates.append(icon_path)
            if install_location:
                for executable_name in definition.executable_names:
                    candidates.append(Path(install_location) / executable_name)
        executable = next((path for path in candidates if path.is_file()), None)
        if executable is not None:
            found[definition.key] = RemoteApp(definition, (str(executable),))
    return [found[item.key] for item in APP_DEFINITIONS if item.key in found]


def detect_remote_apps(system: Optional[str] = None) -> list[RemoteApp]:
    current = system or platform.system()
    if current == "Linux":
        return detect_linux_apps()
    if current == "Windows":
        return detect_windows_apps()
    return []


def running_process_names(system: Optional[str] = None) -> set[str]:
    current = system or platform.system()
    names: set[str] = set()
    if current == "Linux":
        uid = os.getuid()
        for process_dir in Path("/proc").glob("[0-9]*"):
            try:
                if process_dir.stat().st_uid != uid:
                    continue
                names.add((process_dir / "comm").read_text(encoding="utf-8").strip().casefold())
            except OSError:
                continue
    elif current == "Windows":
        try:
            result = subprocess.run(
                ["tasklist.exe", "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return names
        for line in result.stdout.splitlines():
            match = re.match(r'^"([^\"]+)"', line)
            if match:
                names.add(match.group(1).casefold())
    return names


def app_running(app: RemoteApp, process_names: Optional[set[str]] = None) -> bool:
    running = running_process_names() if process_names is None else process_names
    return any(name.casefold() in running for name in app.definition.process_names)


def latest_todesk_service_log(log_dir: Path = TODESK_LOG_DIR) -> Optional[Path]:
    try:
        candidates = [path for path in log_dir.glob("service*.log") if path.is_file()]
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
    except OSError:
        return None


def parse_todesk_center_log(path: Path) -> tuple[str, Optional[str]]:
    state = "unknown"
    endpoint: Optional[str] = None
    endpoint_pattern = re.compile(
        r"client create connect to comet.*\bip=([^\s]+)\s+port=(\d+)"
    )
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source:
            lines = deque(source, maxlen=5000)
    except OSError:
        return state, endpoint

    for line in lines:
        match = endpoint_pattern.search(line)
        if match:
            endpoint = f"{match.group(1)}:{match.group(2)}"
        if "CCenterClientr doAuth connect AuthOk" in line:
            state = "online"
        elif "center client disconnect!!" in line:
            state = "offline"
    return state, endpoint


def established_tcp_peers() -> set[str]:
    try:
        result = subprocess.run(
            ["ss", "-Htn", "state", "established"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    return {line.split()[-1] for line in result.stdout.splitlines() if line.split()}


def todesk_center_state(
    log_dir: Path = TODESK_LOG_DIR,
    peers: Optional[set[str]] = None,
) -> str:
    service_log = latest_todesk_service_log(log_dir)
    if service_log is None:
        return "unknown"
    log_state, endpoint = parse_todesk_center_log(service_log)
    if log_state != "online":
        return log_state
    if endpoint is None:
        return "unknown"
    active_peers = established_tcp_peers() if peers is None else peers
    return "online" if endpoint in active_peers else "offline"


def local_port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        result = subprocess.run(
            ["ss", "-Hltn"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            suffix = f":{port}"
            return any(
                any(field.endswith(suffix) for field in line.split())
                for line in result.stdout.splitlines()
            )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


@lru_cache(maxsize=None)
def installed_service_name(
    service_names: tuple[str, ...],
    system: Optional[str] = None,
) -> Optional[str]:
    current = system or platform.system()
    for service_name in service_names:
        try:
            if current == "Linux":
                result = subprocess.run(
                    ["systemctl", "show", service_name, "--property", "LoadState", "--value"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip() == "loaded":
                    return service_name
            elif current == "Windows":
                result = subprocess.run(
                    ["sc.exe", "query", service_name],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    return service_name
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def app_service_state(app: RemoteApp) -> str:
    current = platform.system()
    service_names = (
        app.definition.linux_services
        if current == "Linux"
        else app.definition.windows_services
        if current == "Windows"
        else ()
    )
    service_name = installed_service_name(service_names, current)
    if service_name is None:
        return "unknown"
    try:
        if current == "Linux":
            result = subprocess.run(
                ["systemctl", "is-active", service_name],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return "online" if result.returncode == 0 and result.stdout.strip() == "active" else "offline"
        result = subprocess.run(
            ["sc.exe", "query", service_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return "offline"
        return "online" if "RUNNING" in result.stdout.upper() else "offline"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def command_connection_state(app: RemoteApp) -> str:
    if app.key != "anydesk":
        return "unknown"
    try:
        result = subprocess.run(
            [app.command[0], "--get-status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    output = f"{result.stdout}\n{result.stderr}".casefold()
    if "online" in output:
        return "online"
    if any(marker in output for marker in ("offline", "disconnected", "not connected")):
        return "offline"
    return "unknown"


def has_deep_health_probe(app: RemoteApp) -> bool:
    if platform.system() == "Linux" and app.key == "todesk":
        return latest_todesk_service_log() is not None
    if app.key == "anydesk":
        return command_connection_state(app) != "unknown"
    return False


def app_health(app: RemoteApp, process_names: Optional[set[str]] = None) -> HealthStatus:
    if not app_running(app, process_names):
        return HealthStatus("unhealthy", "process is not running", 3)
    service = app_service_state(app)
    if service == "offline":
        return HealthStatus("unhealthy", "background service is not running", 3)
    if platform.system() == "Linux" and app.key == "todesk":
        if not local_port_open(TODESK_CONTROL_PORT):
            return HealthStatus(
                "unhealthy",
                f"local control port {TODESK_CONTROL_PORT} is unavailable",
                3,
            )
        center = todesk_center_state()
        if center == "online":
            return HealthStatus("healthy", "center authentication and socket are online", 1)
        if center == "offline":
            return HealthStatus("unhealthy", "center authentication or socket is offline", 6)
        return HealthStatus("unknown", "center state is unavailable", 6)
    connection = command_connection_state(app)
    if connection == "offline":
        return HealthStatus("unhealthy", "vendor status reports offline", 6)
    if connection == "online":
        return HealthStatus("healthy", "vendor status reports online", 1)
    return HealthStatus("healthy", "process and background service are running", 1)


def stop_app(app: RemoteApp) -> None:
    if platform.system() == "Windows":
        for process_name in app.definition.process_names:
            try:
                subprocess.run(
                    ["taskkill.exe", "/F", "/T", "/IM", process_name],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
        return
    for process_name in app.definition.process_names:
        try:
            subprocess.run(
                ["pkill", "-TERM", "-x", process_name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
    for _ in range(10):
        if not app_running(app):
            return
        time.sleep(1)
    for process_name in app.definition.process_names:
        try:
            subprocess.run(
                ["pkill", "-KILL", "-x", process_name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
    time.sleep(1)


def graphical_environment(environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    environment = dict(os.environ if environ is None else environ)
    if platform.system() != "Linux":
        return environment

    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    allowed = {
        "DISPLAY",
        "XAUTHORITY",
        "WAYLAND_DISPLAY",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_TYPE",
    }
    if result is not None and result.returncode == 0:
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in allowed and value:
                environment[key] = value

    runtime_dir = environment.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if not environment.get("DISPLAY") and Path("/tmp/.X11-unix/X0").exists():
        environment["DISPLAY"] = ":0"
    authority = environment.get("XAUTHORITY", "")
    if not authority or not Path(authority).is_file():
        for candidate in (Path(runtime_dir) / "gdm/Xauthority", Path.home() / ".Xauthority"):
            if candidate.is_file() and os.access(candidate, os.R_OK):
                environment["XAUTHORITY"] = str(candidate)
                break
    return environment


def start_app(app: RemoteApp) -> bool:
    environment = {**graphical_environment(), **app.definition.environment}
    output: object = subprocess.DEVNULL
    output_handle: Optional[object] = None
    if LOG_FILE is not None:
        startup_log = LOG_FILE.with_name(f"{app.key}-startup.log")
        try:
            startup_log.parent.mkdir(parents=True, exist_ok=True)
            if startup_log.exists() and startup_log.stat().st_size >= 1_048_576:
                backup = startup_log.with_suffix(startup_log.suffix + ".1")
                backup.unlink(missing_ok=True)
                startup_log.replace(backup)
            output_handle = startup_log.open("a", encoding="utf-8")
            output = output_handle
        except OSError:
            output_handle = None
    kwargs: dict[str, object] = {
        "cwd": str(Path(app.command[0]).parent),
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": output,
        "stderr": output,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(list(app.command), **kwargs)
    except OSError as exc:
        log(f"failed to start {app.display_name}: {exc}")
        return False
    finally:
        if output_handle is not None:
            output_handle.close()
    log(f"started {app.display_name}")
    return True


def restart_app(app: RemoteApp, reason: str) -> bool:
    log(f"restarting {app.display_name}: {reason}")
    stop_app(app)
    return start_app(app)


def wait_for_app_ready(app: RemoteApp, timeout: int = 60) -> bool:
    deadline = time.monotonic() + timeout
    while RUNNING and time.monotonic() < deadline:
        status = app_health(app)
        if status.state == "healthy":
            log(f"{app.display_name} recovered: {status.reason}")
            return True
        if status.state == "unknown" and app_running(app):
            log(f"{app.display_name} is running, but deep health is unavailable")
            return True
        interruptible_sleep(2)
    log(f"{app.display_name} did not become healthy within {timeout} seconds")
    return False


def restart_app_service(app: RemoteApp) -> bool:
    if platform.system() != "Linux":
        return False
    service_name = installed_service_name(app.definition.linux_services, "Linux")
    if service_name is None:
        return False
    systemctl = "/usr/bin/systemctl"
    if not Path(systemctl).is_file():
        resolved = shutil.which("systemctl")
        if resolved is None:
            return False
        systemctl = resolved
    command = [systemctl, "restart", service_name]
    if os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    log(f"restarted {app.display_name} background service: {service_name}")
    interruptible_sleep(3)
    return True


def recover_app(app: RemoteApp, reason: str, allow_service_restart: bool) -> tuple[bool, bool]:
    if restart_app(app, reason) and wait_for_app_ready(app):
        return True, False
    if platform.system() != "Linux" or not allow_service_restart:
        return False, False
    service_name = installed_service_name(app.definition.linux_services, "Linux")
    if service_name is None:
        return False, False
    log(f"{app.display_name} desktop recovery was insufficient; escalating to its background service")
    if not restart_app_service(app):
        log(f"unable to restart the {app.display_name} service; check the restricted privilege rule")
        return False, False
    return (
        restart_app(app, f"{service_name} was restarted") and wait_for_app_ready(app),
        True,
    )


def acquire_lock() -> Optional[object]:
    if fcntl is None:
        return None
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/{APP_NAME}-{os.getuid()}"))
    try:
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = (runtime / f"{APP_NAME}.lock").open("w", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        return None


def interruptible_sleep(seconds: int) -> None:
    end = time.monotonic() + seconds
    while RUNNING and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


def monitor(
    apps: Sequence[RemoteApp],
    online_interval: int,
    offline_interval: int,
    failure_threshold: int,
    recovery_threshold: int,
    cooldown: int,
    service_cooldown: int,
) -> int:
    if not apps:
        log("no supported remote-control software detected; recovery is not needed")
        return 3
    lock = acquire_lock()
    if fcntl is not None and lock is None:
        log("another recovery monitor is already running")
        return 0

    names = ", ".join(app.display_name for app in apps)
    log(f"monitor started; detected={names}")
    state = ConnectivityState(failure_threshold, recovery_threshold)
    opener = build_opener(follow_redirects=False)
    last_action = {app.key: 0.0 for app in apps}
    health_failures = {app.key: 0 for app in apps}
    last_service_restart = 0.0

    while RUNNING:
        online = internet_online(opener, ConnectivityConfig())
        event = state.observe(online)
        now = time.monotonic()
        if event == "offline":
            log("internet is offline; waiting for campus network recovery")
        elif event == "online":
            log("internet is online")
        elif event == "recovered":
            log("internet recovered; checking detected remote-control clients")
            for app in apps:
                if (
                    not has_deep_health_probe(app)
                    and now - last_action[app.key] >= cooldown
                ):
                    restart_app(app, "internet connection recovered")
                    last_action[app.key] = now

        if state.state == "online":
            processes = running_process_names()
            for app in apps:
                status = app_health(app, processes)
                if status.state != "unhealthy":
                    health_failures[app.key] = 0
                    continue
                health_failures[app.key] += 1
                if health_failures[app.key] == 1:
                    log(f"{app.display_name} health check failed: {status.reason}")
                if (
                    health_failures[app.key] >= status.failure_threshold
                    and now - last_action[app.key] >= cooldown
                ):
                    allow_service_restart = now - last_service_restart >= service_cooldown
                    _, service_restarted = recover_app(
                        app,
                        status.reason,
                        allow_service_restart,
                    )
                    last_action[app.key] = now
                    if service_restarted:
                        last_service_restart = now
                    health_failures[app.key] = 0

        interval = online_interval if state.state == "online" else offline_interval
        interruptible_sleep(interval)

    log("monitor stopped")
    return 0


def bounded_int(value: str, minimum: int, maximum: int) -> int:
    number = int(value)
    if not minimum <= number <= maximum:
        raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remote-control software availability monitor")
    parser.add_argument("--detect", action="store_true", help="list supported installed clients")
    parser.add_argument("--once", action="store_true", help="print current status and exit")
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--online-interval", type=lambda value: bounded_int(value, 10, 3600), default=15)
    parser.add_argument("--offline-interval", type=lambda value: bounded_int(value, 5, 600), default=10)
    parser.add_argument("--failure-threshold", type=lambda value: bounded_int(value, 1, 10), default=2)
    parser.add_argument("--recovery-threshold", type=lambda value: bounded_int(value, 1, 10), default=2)
    parser.add_argument("--cooldown", type=lambda value: bounded_int(value, 30, 3600), default=180)
    parser.add_argument(
        "--service-cooldown",
        type=lambda value: bounded_int(value, 60, 7200),
        default=900,
    )
    return parser.parse_args()


def main() -> int:
    global LOG_FILE
    args = parse_args()
    LOG_FILE = args.log_file
    apps = detect_remote_apps()
    if args.detect:
        for app in apps:
            print(f"{app.display_name}\t{app.command[0]}")
        return 0 if apps else 3
    if args.once:
        processes = running_process_names()
        healthy = True
        for app in apps:
            status = app_health(app, processes)
            print(f"{app.display_name}: {status.state} - {status.reason} ({app.command[0]})")
            healthy = healthy and status.state != "unhealthy"
        return (0 if healthy else 1) if apps else 3
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    return monitor(
        apps,
        args.online_interval,
        args.offline_interval,
        args.failure_threshold,
        args.recovery_threshold,
        args.cooldown,
        args.service_cooldown,
    )


if __name__ == "__main__":
    sys.exit(main())
