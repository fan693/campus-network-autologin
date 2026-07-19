#!/usr/bin/env python3
"""Interactive configuration wizard for campus-autologin."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import campus_autologin


def ask(prompt: str, default: str = "", required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ")
        if value:
            return value
        if default:
            return default
        if not required:
            return ""
        print("此项不能为空。")


def ask_json(prompt: str, default: Any) -> Any:
    shown = json.dumps(default, ensure_ascii=False, separators=(",", ":"))
    while True:
        raw = input(f"{prompt} [{shown}]: ").strip()
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"JSON 格式错误：{exc}")


def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def detect_network() -> tuple[str, str]:
    if platform.system() == "Linux":
        output = command_output(
            ["nmcli", "-t", "-f", "NAME,DEVICE,TYPE", "connection", "show", "--active"]
        )
        for line in output.splitlines():
            fields = line.rsplit(":", 2)
            if len(fields) == 3 and fields[1] and fields[2] in ("802-11-wireless", "wifi", "ethernet"):
                return fields[0].replace("\\:", ":"), fields[1]
    elif platform.system() == "Windows":
        script = (
            "$p=Get-NetConnectionProfile | Where-Object {$_.IPv4Connectivity -ne 'Disconnected'} | "
            "Select-Object -First 1 Name,InterfaceAlias; $p | ConvertTo-Json -Compress"
        )
        output = command_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
        )
        try:
            value = json.loads(output)
            return str(value.get("Name", "")), str(value.get("InterfaceAlias", ""))
        except (json.JSONDecodeError, AttributeError):
            pass
    return "", ""


def load_existing(path: Optional[Path]) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取现有配置：{exc}")
    if not isinstance(data, dict):
        raise SystemExit("现有配置的根节点不是 JSON 对象。")
    return campus_autologin.old_config_to_current(data)


def choose_portal(existing: dict[str, Any]) -> dict[str, Any]:
    current = existing.get("portal", {}) if isinstance(existing.get("portal"), dict) else {}
    current_type = current.get("type", "")
    if "login.cqu.edu.cn" in str(current.get("login_url", "")):
        default_choice = "1"
    else:
        default_choice = {"drcom": "2", "srun": "3", "generic": "4"}.get(current_type, "1")

    print("\n请选择校园网认证类型：")
    print("  1. 重庆大学 CQU（Dr.COM，一键预设）")
    print("  2. 其他学校 Dr.COM / ePortal")
    print("  3. 深澜 SRUN（get_challenge + srun_portal）")
    print("  4. 通用 HTTP GET/POST 表单")
    while True:
        choice = ask("输入编号", default_choice)
        if choice in ("1", "2", "3", "4"):
            break
        print("请输入 1、2、3 或 4。")

    if choice == "1":
        return {
            "type": "drcom",
            "login_url": "https://login.cqu.edu.cn:802/eportal/portal/login",
            "account_prefix": ",0,",
        }
    if choice == "2":
        return {
            "type": "drcom",
            "login_url": ask(
                "登录 API 地址（通常以 /eportal/portal/login 结尾）",
                str(current.get("login_url", "")),
            ),
            "account_prefix": ask(
                "账号字段前缀（多数 Dr.COM 为 ,0,；没有则输入 -）",
                str(current.get("account_prefix", ",0,")),
            ).replace("-", "", 1),
        }
    if choice == "3":
        print("提示：base URL 示例为 http://10.0.0.55，不要包含 /cgi-bin/get_challenge。")
        return {
            "type": "srun",
            "base_url": ask("SRUN 认证服务器 base URL", str(current.get("base_url", ""))),
            "ac_id": ask("AC ID", str(current.get("ac_id", "1"))),
            "n": ask("n 参数", str(current.get("n", "200"))),
            "type_value": ask("type 参数", str(current.get("type_value", "1"))),
            "enc_ver": ask("enc_ver 参数", str(current.get("enc_ver", "srun_bx1"))),
            "base64_alphabet": ask(
                "SRUN Base64 字母表",
                str(current.get("base64_alphabet", campus_autologin.SRUN_ALPHABET)),
            ),
        }

    default_parameters = current.get(
        "parameters", {"username": "{username}", "password": "{password}", "user_ip": "{ipv4}"}
    )
    default_success = current.get("success_contains", ["success"])
    print("可用占位符：{username} {password} {password_base64} {ipv4} {ipv6} {network_name} {timestamp}")
    return {
        "type": "generic",
        "login_url": ask("登录请求 URL", str(current.get("login_url", ""))),
        "method": ask("请求方法 GET/POST", str(current.get("method", "POST"))).upper(),
        "parameters": ask_json("请求参数 JSON 对象", default_parameters),
        "headers": ask_json("额外请求头 JSON 对象", current.get("headers", {})),
        "success_contains": ask_json("成功响应中至少出现一个字符串", default_success),
        "already_online_contains": ask_json(
            "已在线响应中至少出现一个字符串", current.get("already_online_contains", [])
        ),
    }


def build_config(args: argparse.Namespace, existing: dict[str, Any]) -> dict[str, Any]:
    detected_name, detected_interface = detect_network()
    network_name = args.network_name or str(existing.get("network_name", "")) or detected_name
    interface = args.interface or str(existing.get("interface", "")) or detected_interface

    print("校园网自动重连配置向导")
    print("安全提示：按你的要求，密码输入会在屏幕上明文显示；请确认身边无人观看或录屏。")
    network_name = ask("目标 Wi-Fi/有线连接名称", network_name)
    interface = ask("网卡名称", interface, required=False)
    username = ask("校园网账号", str(existing.get("username", "")))

    old_password = existing.get("password", "") if isinstance(existing.get("password"), str) else ""
    password_prompt = "校园网密码（明文显示；直接回车保留原密码）" if old_password else "校园网密码（明文显示）"
    password = input(f"{password_prompt}: ")
    if not password and old_password:
        password = old_password
        print("已保留原密码，不在屏幕上显示已保存的密码。")
    elif password:
        print(f"你刚才输入的密码是：{password}")
    else:
        raise SystemExit("密码不能为空。")

    portal = choose_portal(existing)
    return {
        "version": 4,
        "username": username,
        "password": password,
        "network_name": network_name,
        "interface": interface,
        "portal": portal,
        "online_interval": int(existing.get("online_interval", 30)),
        "offline_interval": int(existing.get("offline_interval", 10)),
        "timeout": int(existing.get("timeout", 5)),
        "failure_threshold": int(existing.get("failure_threshold", 2)),
        "connectivity_checks": existing.get(
            "connectivity_checks", list(campus_autologin.DEFAULT_CHECKS)
        ),
    }


def write_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure campus-autologin")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--existing", type=Path)
    parser.add_argument("--network-name")
    parser.add_argument("--interface")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    existing = load_existing(args.existing)
    config = build_config(args, existing)
    temporary_validation = args.output.with_name(args.output.name + ".validate")
    try:
        write_config(temporary_validation, config)
        campus_autologin.load_config(temporary_validation)
    except ValueError as exc:
        print(f"配置无效：{exc}", file=sys.stderr)
        return 78
    finally:
        try:
            temporary_validation.unlink()
        except FileNotFoundError:
            pass
    write_config(args.output, config)
    print(f"配置已写入：{args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
