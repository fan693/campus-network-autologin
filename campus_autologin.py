#!/usr/bin/env python3
"""Cross-platform captive-portal monitor and campus network auto-login."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import platform
import re
import signal
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None


APP_NAME = "campus-autologin"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_CHECKS = (
    {"url": "https://www.gstatic.com/generate_204", "status": 204},
    {
        "url": "http://www.msftconnecttest.com/connecttest.txt",
        "status": 200,
        "body": "Microsoft Connect Test",
    },
)
SRUN_ALPHABET = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"
SIOCGIFADDR = 0x8915
RUNNING = True
LOG_FILE: Optional[Path] = None


@dataclass(frozen=True)
class ConnectivityCheck:
    url: str
    status: int
    body: Optional[str] = None


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    network_name: str
    interface: str
    portal: dict[str, Any]
    online_interval: int
    offline_interval: int
    timeout: int
    failure_threshold: int
    connectivity_checks: tuple[ConnectivityCheck, ...]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def log(message: str) -> None:
    line = f"{APP_NAME}: {message}"
    try:
        print(line, flush=True)
    except (AttributeError, OSError):
        # pythonw.exe intentionally has no console streams.
        pass
    if LOG_FILE is not None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as output:
                output.write(f"{timestamp} {line}\n")
        except OSError:
            pass


def stop_handler(_signum: int, _frame: object) -> None:
    global RUNNING
    RUNNING = False


def positive_int(data: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def checked_url(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    value = value.strip()
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{key} must be an http:// or https:// URL")
    if parts.username or parts.password:
        raise ValueError(f"{key} must not contain embedded credentials")
    return value


def validate_text(value: Any, key: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{key} is required")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{key} contains control characters")
    return value.strip() if key != "password" else value


def old_config_to_current(data: dict[str, Any]) -> dict[str, Any]:
    """Load v3 CQU configuration without exposing or rewriting its password."""
    if "portal" in data or "student_id" not in data:
        return data
    return {
        "version": 4,
        "username": data.get("student_id", ""),
        "password": data.get("password", ""),
        "network_name": data.get("connection_name", ""),
        "interface": data.get("interface", ""),
        "portal": {
            "type": "drcom",
            "login_url": "https://login.cqu.edu.cn:802/eportal/portal/login",
            "account_prefix": ",0,",
        },
        "online_interval": data.get("online_interval", 30),
        "offline_interval": data.get("offline_interval", 10),
        "timeout": data.get("timeout", 5),
        "failure_threshold": data.get("failure_threshold", 2),
        "connectivity_checks": list(DEFAULT_CHECKS),
    }


def validate_portal(raw_portal: Any) -> dict[str, Any]:
    if not isinstance(raw_portal, dict):
        raise ValueError("portal must be an object")
    portal = dict(raw_portal)
    kind = validate_text(portal.get("type", ""), "portal.type").lower()
    if kind not in ("drcom", "srun", "generic"):
        raise ValueError("portal.type must be drcom, srun, or generic")
    portal["type"] = kind

    if kind == "drcom":
        portal["login_url"] = checked_url(portal.get("login_url"), "portal.login_url")
        prefix = portal.get("account_prefix", ",0,")
        portal["account_prefix"] = validate_text(prefix, "portal.account_prefix", allow_empty=True)
        overrides = portal.get("parameters", {})
        if not isinstance(overrides, dict) or not all(
            isinstance(item_key, str) and isinstance(item_value, str)
            for item_key, item_value in overrides.items()
        ):
            raise ValueError("portal.parameters must be a string-to-string object")
    elif kind == "srun":
        portal["base_url"] = checked_url(portal.get("base_url"), "portal.base_url").rstrip("/")
        portal["ac_id"] = validate_text(str(portal.get("ac_id", "1")), "portal.ac_id")
        for url_key in ("challenge_url", "login_url"):
            if url_key in portal:
                portal[url_key] = checked_url(portal[url_key], f"portal.{url_key}")
        alphabet = portal.get("base64_alphabet", SRUN_ALPHABET)
        alphabet = validate_text(alphabet, "portal.base64_alphabet")
        if len(alphabet) != 64 or len(set(alphabet)) != 64:
            raise ValueError("portal.base64_alphabet must contain 64 unique characters")
        portal["base64_alphabet"] = alphabet
    else:
        portal["login_url"] = checked_url(portal.get("login_url"), "portal.login_url")
        method = validate_text(portal.get("method", "POST"), "portal.method").upper()
        if method not in ("GET", "POST"):
            raise ValueError("portal.method must be GET or POST")
        portal["method"] = method
        for key in ("parameters", "headers"):
            value = portal.get(key, {})
            if not isinstance(value, dict) or not all(
                isinstance(item_key, str) and isinstance(item_value, str)
                for item_key, item_value in value.items()
            ):
                raise ValueError(f"portal.{key} must be a string-to-string object")
            if key == "headers" and any(
                any(ord(char) < 32 for char in item_key + item_value)
                for item_key, item_value in value.items()
            ):
                raise ValueError("portal.headers contains control characters")
        markers = portal.get("success_contains", [])
        already = portal.get("already_online_contains", [])
        if not isinstance(markers, list) or not all(isinstance(item, str) and item for item in markers):
            raise ValueError("portal.success_contains must be a list of non-empty strings")
        if not isinstance(already, list) or not all(isinstance(item, str) and item for item in already):
            raise ValueError("portal.already_online_contains must be a list of strings")
        if not markers and not already:
            raise ValueError("generic portal needs at least one success response marker")
        render_templates(
            portal.get("parameters", {}),
            {
                "username": "user",
                "password": "password",
                "password_base64": "cGFzc3dvcmQ=",
                "ipv4": "192.0.2.1",
                "ipv6": "",
                "network_name": "network",
                "timestamp": "0",
            },
        )
    return portal


def load_config(path: Path) -> Config:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"config file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read config: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("config root must be an object")
    data = old_config_to_current(data)

    username = validate_text(data.get("username", ""), "username")
    password = validate_text(data.get("password", ""), "password")
    network_name = validate_text(data.get("network_name", ""), "network_name")
    interface = validate_text(data.get("interface", ""), "interface", allow_empty=True)
    portal = validate_portal(data.get("portal"))

    raw_checks = data.get("connectivity_checks", list(DEFAULT_CHECKS))
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ValueError("connectivity_checks must be a non-empty list")
    checks: list[ConnectivityCheck] = []
    for index, item in enumerate(raw_checks):
        if not isinstance(item, dict):
            raise ValueError(f"connectivity_checks[{index}] must be an object")
        url = checked_url(item.get("url"), f"connectivity_checks[{index}].url")
        status = item.get("status")
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            raise ValueError(f"connectivity_checks[{index}].status is invalid")
        body = item.get("body")
        if body is not None and not isinstance(body, str):
            raise ValueError(f"connectivity_checks[{index}].body must be a string")
        checks.append(ConnectivityCheck(url=url, status=status, body=body))

    return Config(
        username=username,
        password=password,
        network_name=network_name,
        interface=interface,
        portal=portal,
        online_interval=positive_int(data, "online_interval", 30, 10, 3600),
        offline_interval=positive_int(data, "offline_interval", 10, 5, 600),
        timeout=positive_int(data, "timeout", 5, 2, 60),
        failure_threshold=positive_int(data, "failure_threshold", 2, 1, 10),
        connectivity_checks=tuple(checks),
    )


def build_opener(follow_redirects: bool = True) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    ]
    if not follow_redirects:
        handlers.append(NoRedirect())
    return urllib.request.build_opener(*handlers)


def internet_online(opener: urllib.request.OpenerDirector, config: Config) -> bool:
    for check in config.connectivity_checks:
        request = urllib.request.Request(
            check.url,
            headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
            method="GET",
        )
        try:
            with opener.open(request, timeout=config.timeout) as response:
                body = response.read(512).decode("utf-8", errors="replace")
                if response.status == check.status and (check.body is None or body.strip() == check.body):
                    return True
        except urllib.error.HTTPError:
            continue
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return False


def run_command(command: list[str], timeout: int) -> Optional[str]:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform.system() == "Windows" else 0
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
            creationflags=creation_flags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def linux_active_connection(interface: str, timeout: int) -> Optional[str]:
    if interface:
        return run_command(
            ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", interface],
            timeout,
        )
    output = run_command(
        ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
        timeout,
    )
    if not output:
        return None
    first = output.splitlines()[0]
    return first.rsplit(":", 1)[0].replace("\\:", ":")


def windows_connection_profiles(timeout: int) -> list[dict[str, str]]:
    script = (
        "$ErrorActionPreference='Stop';"
        "@(Get-NetConnectionProfile | Where-Object {$_.IPv4Connectivity -ne 'Disconnected'} | "
        "Select-Object Name,InterfaceAlias) | ConvertTo-Json -Compress"
    )
    output = run_command(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout
    )
    if not output:
        return []
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def network_matches(config: Config) -> bool:
    system = platform.system()
    if system == "Linux":
        active = linux_active_connection(config.interface, config.timeout)
        return bool(active) and (config.network_name == "*" or active == config.network_name)
    if system == "Windows":
        for profile in windows_connection_profiles(config.timeout):
            name = str(profile.get("Name", ""))
            interface = str(profile.get("InterfaceAlias", ""))
            name_matches = config.network_name == "*" or name == config.network_name
            if name_matches and (not config.interface or interface == config.interface):
                return True
        return False
    return False


def linux_ipv4(interface: str) -> Optional[str]:
    if not interface or fcntl is None:
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            packed = struct.pack("256s", interface[:15].encode("utf-8"))
            result = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, packed)
            return socket.inet_ntoa(result[20:24])
    except OSError:
        return None


def routed_ipv4() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            address = sock.getsockname()[0]
            return address if address and not address.startswith("127.") else None
    except OSError:
        return None


def get_ipv4(config: Config) -> Optional[str]:
    if platform.system() == "Linux":
        address = linux_ipv4(config.interface)
        if address:
            return address
    if platform.system() == "Windows" and config.interface:
        escaped = config.interface.replace("'", "''")
        script = (
            f"@(Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias '{escaped}' "
            "-ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -notlike '169.254.*'} | "
            "Select-Object -First 1 -ExpandProperty IPAddress)"
        )
        address = run_command(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            config.timeout,
        )
        if address:
            return address.splitlines()[0].strip()
    return routed_ipv4()


def get_ipv6(interface: str) -> str:
    if platform.system() != "Linux" or not interface:
        return ""
    try:
        lines = Path("/proc/net/if_inet6").read_text(encoding="ascii").splitlines()
    except OSError:
        return ""
    fallback = ""
    for line in lines:
        fields = line.split()
        if len(fields) != 6 or fields[5] != interface:
            continue
        raw, scope = fields[0], fields[3]
        try:
            address = socket.inet_ntop(socket.AF_INET6, bytes.fromhex(raw))
        except (OSError, ValueError):
            continue
        if scope == "00":
            return address
        if not address.lower().startswith("fe80:"):
            fallback = address
    return fallback


def parse_jsonp(text: str) -> Optional[dict[str, Any]]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def safe_response(text: str, config: Config) -> str:
    cleaned = text
    secrets = (
        config.password,
        urllib.parse.quote(config.password, safe=""),
        base64.b64encode(config.password.encode("utf-8")).decode("ascii"),
        config.username,
    )
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "<redacted>")
    return re.sub(r"[\r\n\t]+", " ", cleaned)[:300]


def request_text(
    opener: urllib.request.OpenerDirector,
    url: str,
    method: str,
    parameters: dict[str, str],
    headers: dict[str, str],
    timeout: int,
) -> tuple[int, str]:
    encoded = urllib.parse.urlencode(parameters).encode("utf-8")
    data: Optional[bytes] = None
    if method == "GET":
        delimiter = "&" if urllib.parse.urlsplit(url).query else "?"
        url = f"{url}{delimiter}{encoded.decode('ascii')}"
    else:
        data = encoded
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with opener.open(request, timeout=timeout) as response:
        return response.status, response.read(8192).decode("utf-8", errors="replace")


def template_values(config: Config, ipv4: str, ipv6: str) -> dict[str, str]:
    return {
        "username": config.username,
        "password": config.password,
        "password_base64": base64.b64encode(config.password.encode("utf-8")).decode("ascii"),
        "ipv4": ipv4,
        "ipv6": ipv6,
        "network_name": config.network_name,
        "timestamp": str(int(time.time() * 1000)),
    }


def render_templates(source: dict[str, str], values: dict[str, str]) -> dict[str, str]:
    rendered: dict[str, str] = {}
    for key, value in source.items():
        try:
            rendered[key] = value.format_map(values)
        except KeyError as exc:
            raise ValueError(f"unknown template placeholder: {exc.args[0]}") from exc
    return rendered


def drcom_login(
    opener: urllib.request.OpenerDirector, config: Config, ipv4: str, ipv6: str
) -> bool:
    portal = config.portal
    params = {
        "callback": "dr1004",
        "login_method": "1",
        "user_account": f"{portal.get('account_prefix', ',0,')}{config.username}",
        "user_password": config.password,
        "wlan_user_ip": ipv4,
        "wlan_user_ipv6": ipv6,
        "wlan_user_mac": str(portal.get("mac", "000000000000")),
        "wlan_ac_ip": str(portal.get("ac_ip", "")),
        "wlan_ac_name": str(portal.get("ac_name", "")),
        "term_ua": USER_AGENT,
        "term_type": "1",
        "jsVersion": str(portal.get("js_version", "4.2.2")),
        "terminal_type": "1",
        "lang": "zh-cn,zh",
        "v": str(int(time.time() * 1000) % 10000),
    }
    overrides = portal.get("parameters", {})
    if isinstance(overrides, dict):
        params.update(render_templates(overrides, template_values(config, ipv4, ipv6)))
    try:
        _status, body = request_text(
            opener,
            portal["login_url"],
            "GET",
            params,
            {"User-Agent": USER_AGENT, "Accept": "*/*"},
            config.timeout,
        )
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            log("portal TLS verification failed; check system time and CA certificates")
        else:
            log(f"portal request failed: {type(reason).__name__}")
        return False
    except (TimeoutError, OSError, ValueError) as exc:
        log(f"portal request failed: {type(exc).__name__}")
        return False

    result = parse_jsonp(body)
    if result is not None:
        if result.get("result") in (1, "1"):
            log(f"Dr.COM login succeeded (ip={ipv4})")
            return True
        ret_code = result.get("ret_code")
        message = str(result.get("msg", result.get("message", "")))
        legacy_online = (
            result.get("result") in (0, "0")
            and ret_code in (1, "1")
            and "WelcometoDrcomSystem" in re.sub(r"\s+", "", body)
        )
        if (
            ret_code in (2, "2")
            or legacy_online
            or "already" in message.lower()
            or "已经在线" in message
        ):
            log(f"Dr.COM reports this computer is already online (ip={ipv4})")
            return True
    log(f"Dr.COM login rejected; response={safe_response(body, config)}")
    return False


def srun_xencode(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""

    def to_words(value: bytes, include_length: bool) -> list[int]:
        padded = value + b"\0" * ((4 - len(value) % 4) % 4)
        words = [struct.unpack("<I", padded[index : index + 4])[0] for index in range(0, len(padded), 4)]
        if include_length:
            words.append(len(value))
        return words

    values = to_words(data, True)
    keys = (to_words(key, False) + [0, 0, 0, 0])[:4]
    count = len(values) - 1
    z = values[count]
    delta = 0x9E3779B9
    total = 0
    rounds = 6 + 52 // (count + 1)
    for _ in range(rounds):
        total = (total + delta) & 0xFFFFFFFF
        e = (total >> 2) & 3
        for position in range(count):
            y = values[position + 1]
            mixed = ((z >> 5) ^ (y << 2))
            mixed += ((y >> 3) ^ (z << 4)) ^ (total ^ y)
            mixed += keys[(position & 3) ^ e] ^ z
            values[position] = (values[position] + mixed) & 0xFFFFFFFF
            z = values[position]
        y = values[0]
        mixed = ((z >> 5) ^ (y << 2))
        mixed += ((y >> 3) ^ (z << 4)) ^ (total ^ y)
        mixed += keys[(count & 3) ^ e] ^ z
        values[count] = (values[count] + mixed) & 0xFFFFFFFF
        z = values[count]
    return b"".join(struct.pack("<I", value) for value in values)


def srun_base64(data: bytes, alphabet: str) -> str:
    standard = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    translation = str.maketrans(standard, alphabet)
    return base64.b64encode(data).decode("ascii").translate(translation)


def srun_login(opener: urllib.request.OpenerDirector, config: Config, ipv4: str) -> bool:
    portal = config.portal
    base_url = portal["base_url"]
    ac_id = portal.get("ac_id", "1")
    callback = f"jsonp{int(time.time() * 1000)}"
    challenge_url = str(portal.get("challenge_url", f"{base_url}/cgi-bin/get_challenge"))
    login_url = str(portal.get("login_url", f"{base_url}/cgi-bin/srun_portal"))
    try:
        _status, challenge_body = request_text(
            opener,
            challenge_url,
            "GET",
            {"callback": callback, "username": config.username, "ip": ipv4},
            {"User-Agent": USER_AGENT, "Accept": "*/*"},
            config.timeout,
        )
        challenge = parse_jsonp(challenge_body)
        token = str(challenge.get("challenge", "")) if challenge else ""
        if not re.fullmatch(r"[A-Za-z0-9]+", token):
            log(f"SRUN challenge failed; response={safe_response(challenge_body, config)}")
            return False

        hmd5 = hmac.new(token.encode("utf-8"), config.password.encode("utf-8"), hashlib.md5).hexdigest()
        info_object = {
            "username": config.username,
            "password": config.password,
            "ip": ipv4,
            "acid": str(ac_id),
            "enc_ver": str(portal.get("enc_ver", "srun_bx1")),
        }
        info_json = json.dumps(info_object, ensure_ascii=False, separators=(",", ":"))
        encrypted_info = "{SRBX1}" + srun_base64(
            srun_xencode(info_json.encode("utf-8"), token.encode("utf-8")),
            portal["base64_alphabet"],
        )
        n_value = str(portal.get("n", "200"))
        type_value = str(portal.get("type_value", "1"))
        checksum_source = token.join(
            ("", config.username, hmd5, str(ac_id), ipv4, n_value, type_value, encrypted_info)
        )
        checksum = hashlib.sha1(checksum_source.encode("utf-8")).hexdigest()
        login_params = {
            "callback": f"{callback}1",
            "action": "login",
            "username": config.username,
            "password": "{MD5}" + hmd5,
            "ac_id": str(ac_id),
            "ip": ipv4,
            "info": encrypted_info,
            "chksum": checksum,
            "n": n_value,
            "type": type_value,
        }
        _status, body = request_text(
            opener,
            login_url,
            "GET",
            login_params,
            {"User-Agent": USER_AGENT, "Accept": "*/*"},
            config.timeout,
        )
    except urllib.error.URLError as exc:
        log(f"SRUN portal request failed: {type(getattr(exc, 'reason', exc)).__name__}")
        return False
    except (TimeoutError, OSError, ValueError) as exc:
        log(f"SRUN portal request failed: {type(exc).__name__}")
        return False

    result = parse_jsonp(body)
    if result is not None:
        error = str(result.get("error", "")).lower()
        result_value = str(result.get("res", result.get("result", ""))).lower()
        success_message = str(result.get("suc_msg", "")).lower()
        if error == "ok" or result_value in ("ok", "1", "success") or success_message in (
            "login_ok",
            "login_success",
        ):
            log(f"SRUN login succeeded (ip={ipv4})")
            return True
        message = str(result.get("error_msg", result.get("ploy_msg", "")))
        if (
            "already" in error
            or "already" in result_value
            or "already" in message.lower()
            or "已经在线" in message
        ):
            log(f"SRUN reports this account is already online (ip={ipv4})")
            return True
    log(f"SRUN login rejected; response={safe_response(body, config)}")
    return False


def generic_login(
    opener: urllib.request.OpenerDirector, config: Config, ipv4: str, ipv6: str
) -> bool:
    portal = config.portal
    values = template_values(config, ipv4, ipv6)
    try:
        parameters = render_templates(portal.get("parameters", {}), values)
        headers = render_templates(portal.get("headers", {}), values)
        headers.setdefault("User-Agent", USER_AGENT)
        status, body = request_text(
            opener,
            portal["login_url"],
            portal["method"],
            parameters,
            headers,
            config.timeout,
        )
    except urllib.error.URLError as exc:
        log(f"generic portal request failed: {type(getattr(exc, 'reason', exc)).__name__}")
        return False
    except (TimeoutError, OSError, ValueError) as exc:
        log(f"generic portal request failed: {type(exc).__name__}")
        return False

    if not 200 <= status < 400:
        log(f"generic portal returned HTTP {status}")
        return False
    success_markers = portal.get("success_contains", [])
    already_markers = portal.get("already_online_contains", [])
    if any(marker in body for marker in success_markers):
        log(f"generic portal login succeeded (ip={ipv4})")
        return True
    if any(marker in body for marker in already_markers):
        log(f"generic portal reports this computer is already online (ip={ipv4})")
        return True
    log(f"generic portal login rejected; response={safe_response(body, config)}")
    return False


def portal_login(
    opener: urllib.request.OpenerDirector, config: Config, ipv4: str, ipv6: str
) -> bool:
    kind = config.portal["type"]
    if kind == "drcom":
        return drcom_login(opener, config, ipv4, ipv6)
    if kind == "srun":
        return srun_login(opener, config, ipv4)
    return generic_login(opener, config, ipv4, ipv6)


def interruptible_sleep(seconds: int) -> None:
    end = time.monotonic() + seconds
    while RUNNING:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 1.0))


def run(config: Config, once: bool = False) -> int:
    connectivity_opener = build_opener(follow_redirects=False)
    portal_opener = build_opener(follow_redirects=True)
    consecutive_failures = 0
    login_backoff = config.offline_interval
    last_state = ""
    log(
        f"service started; portal={config.portal['type']}, "
        f"network={config.network_name}, interface={config.interface or 'auto'}"
    )
    if config.portal.get("login_url", config.portal.get("base_url", "")).startswith("http://"):
        log("warning: this campus portal uses HTTP, so the password is not encrypted in transit")

    while RUNNING:
        if not network_matches(config):
            state = f"waiting for network {config.network_name}"
            if state != last_state:
                log(state)
                last_state = state
            consecutive_failures = 0
            if once:
                return 2
            interruptible_sleep(config.offline_interval)
            continue

        ipv4 = get_ipv4(config)
        if not ipv4:
            state = f"waiting for IPv4 address on {config.interface or 'active interface'}"
            if state != last_state:
                log(state)
                last_state = state
            consecutive_failures = 0
            if once:
                return 2
            interruptible_sleep(config.offline_interval)
            continue

        if internet_online(connectivity_opener, config):
            if last_state != "online":
                log("internet is online")
                last_state = "online"
            consecutive_failures = 0
            login_backoff = config.offline_interval
            if once:
                return 0
            interruptible_sleep(config.online_interval)
            continue

        consecutive_failures += 1
        if not once and consecutive_failures < config.failure_threshold:
            if last_state != "checking":
                log("internet check failed once; confirming before login")
                last_state = "checking"
            interruptible_sleep(config.offline_interval)
            continue

        log("internet check failed; trying portal login")
        last_state = "authenticating"
        success = portal_login(portal_opener, config, ipv4, get_ipv6(config.interface))
        consecutive_failures = 0
        if success:
            login_backoff = config.offline_interval
            if not once:
                interruptible_sleep(5)
        else:
            login_backoff = min(max(login_backoff * 2, 30), 300)
            log(f"next authentication attempt in {login_backoff}s")

        if once:
            return 0 if success else 4
        interruptible_sleep(login_backoff)

    log("service stopped")
    return 0


def default_config_path() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "CampusAutoLogin" / "config.json"
    return Path("/etc/campus-autologin/config.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Campus network auto-login monitor")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--once", action="store_true", help="check once and log in immediately if offline")
    parser.add_argument("--log-file", type=Path)
    return parser.parse_args()


def main() -> int:
    global LOG_FILE
    args = parse_args()
    LOG_FILE = args.log_file
    try:
        config = load_config(args.config)
    except ValueError as exc:
        log(f"configuration error: {exc}")
        return 78
    if args.check_config:
        log("configuration is valid")
        return 0
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    return run(config, once=args.once)


if __name__ == "__main__":
    sys.exit(main())
