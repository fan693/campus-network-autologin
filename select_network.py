#!/usr/bin/env python3
"""Select the physical network connection monitored by the Linux installer."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, TextIO


SUPPORTED_TYPES = {"ethernet", "wifi"}
TYPE_LABELS = {"ethernet": "有线", "wifi": "Wi-Fi"}


@dataclass(frozen=True)
class NetworkCandidate:
    connection: str
    interface: str
    network_type: str


def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def active_candidates(
    run: Callable[[list[str]], str] = command_output,
) -> list[NetworkCandidate]:
    status = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"])
    candidates: list[NetworkCandidate] = []
    for line in status.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        interface, network_type, state = fields
        if state != "connected" or network_type not in SUPPORTED_TYPES:
            continue
        connection = run(
            ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", interface]
        )
        if connection and connection != "--":
            candidates.append(NetworkCandidate(connection, interface, network_type))
    return candidates


def primary_route_interface(run: Callable[[list[str]], str] = command_output) -> str:
    route = run(["ip", "-4", "route", "get", "1.1.1.1"])
    match = re.search(r"(?:^|\s)dev\s+(\S+)", route)
    return match.group(1) if match else ""


def choose_candidate(
    candidates: list[NetworkCandidate],
    primary_interface: str,
    *,
    interactive: bool,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stderr,
) -> NetworkCandidate:
    if not candidates:
        raise ValueError("没有检测到已连接的 Wi-Fi 或有线网络。")
    if len(candidates) == 1:
        selected = candidates[0]
        print(
            f"检测到唯一网络：{selected.connection} "
            f"({TYPE_LABELS[selected.network_type]}，{selected.interface})",
            file=output_stream,
        )
        return selected

    recommended = next(
        (
            index
            for index, item in enumerate(candidates)
            if item.interface == primary_interface
        ),
        None,
    )
    print(
        "检测到多个活动网络，请选择需要校园网自动认证的连接：",
        file=output_stream,
    )
    for index, candidate in enumerate(candidates, start=1):
        marker = "，当前默认路由（推荐）" if index - 1 == recommended else ""
        print(
            f"  {index}. {candidate.connection} "
            f"({TYPE_LABELS[candidate.network_type]}，{candidate.interface}{marker})",
            file=output_stream,
        )

    if not interactive:
        if recommended is None:
            raise ValueError(
                "非交互安装无法确定默认网络，请在终端中重新运行安装器。"
            )
        print("非交互安装：自动选择当前默认路由。", file=output_stream)
        return candidates[recommended]

    while True:
        default_hint = f" [{recommended + 1}]" if recommended is not None else ""
        output_stream.write(f"输入编号{default_hint}: ")
        output_stream.flush()
        answer = input_stream.readline()
        if answer == "":
            raise ValueError("未读取到网络选择。")
        answer = answer.strip()
        if not answer and recommended is not None:
            return candidates[recommended]
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        print("请输入列表中的有效编号。", file=output_stream)


def main() -> int:
    parser = argparse.ArgumentParser(description="选择校园网自动认证连接")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        selected = choose_candidate(
            active_candidates(),
            primary_route_interface(),
            interactive=sys.stdin.isatty(),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    args.output.write_text(
        json.dumps(asdict(selected), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
