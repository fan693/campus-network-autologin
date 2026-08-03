"""Runnable desktop preview for Campus Network Assistant."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from PySide6.QtCore import QEasingCurve, QLockFile, QPointF, QPropertyAnimation, Qt, QThread, QTimer, Signal
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFrame,
        QGraphicsDropShadowEffect,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
        QMenu,
    )
    QT_BINDING = "PySide6"
except ImportError:
    from PyQt5.QtCore import QEasingCurve, QLockFile, QPointF, QPropertyAnimation, Qt, QThread, QTimer, pyqtSignal as Signal
    from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
    from PyQt5.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFrame,
        QGraphicsDropShadowEffect,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
        QMenu,
    )
    QT_BINDING = "PyQt5"

from . import control as desktop_control
from .updater import CheckResult, UpdateChecker, Version

try:
    import remote_recovery as remote_health
except ImportError:
    remote_health = None


COLORS = {
    "ink": "#071014",
    "panel": "#0c191e",
    "panel_alt": "#102229",
    "line": "#20363d",
    "text": "#e8f0ec",
    "muted": "#8fa59e",
    "green": "#54e39d",
    "green_dark": "#173f31",
    "amber": "#f4bd68",
    "red": "#ff776d",
    "cyan": "#65cfe2",
}


def application_icon() -> QIcon:
    source = Path(__file__).resolve().parents[1] / "assets" / "campus-network-assistant.svg"
    return QIcon(str(source)) if source.is_file() else QIcon()


def run_command(command: list[str], timeout: int = 4) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def active_service(candidates: tuple[str, ...], *, user: bool = False) -> tuple[str, str]:
    prefix = ["systemctl", "--user"] if user else ["systemctl"]
    first_known = ""
    for name in candidates:
        load_state = run_command(prefix + ["show", name, "-p", "LoadState", "--value"])
        if load_state != "loaded":
            continue
        first_known = first_known or name
        state = run_command(prefix + ["is-active", name])
        if state == "active":
            return name, "active"
    return (first_known, "inactive") if first_known else ("", "missing")


def default_interface() -> str:
    route = run_command(["ip", "-4", "route", "get", "1.1.1.1"])
    match = re.search(r"(?:^|\s)dev\s+(\S+)", route)
    return match.group(1) if match else ""


REMOTE_REASON_LABELS = {
    "center authentication and socket are online": "中心认证与实时连接正常",
    "center authentication or socket is offline": "中心认证或实时连接已经离线",
    "center state is unavailable": "暂时无法确认中心连接状态",
    "process and background service are running": "客户端进程与后台服务正常",
    "vendor status reports online": "客户端报告连接在线",
    "vendor status reports offline": "客户端报告连接离线",
    "process is not running": "客户端进程未运行",
    "background service is not running": "后台服务未运行",
}


def translate_remote_reason(reason: str) -> str:
    """Return a concise public status without exposing raw diagnostic output."""
    if reason in REMOTE_REASON_LABELS:
        return REMOTE_REASON_LABELS[reason]
    if re.fullmatch(r"local control port \d+ is unavailable", reason):
        return "客户端本地控制通道不可用"
    return "远程连接状态需要进一步确认"


def translate_remote_state(state: str) -> str:
    return {
        "healthy": "在线",
        "unhealthy": "异常",
        "unknown": "待确认",
    }.get(state, "待确认")


@dataclass
class DesktopStatus:
    connection_name: str = "未检测到"
    interface: str = "--"
    connection_type: str = "unknown"
    network_state: str = "unknown"
    internet_state: str = "unknown"
    campus_service: str = "missing"
    campus_service_name: str = ""
    remote_service: str = "missing"
    remote_service_name: str = ""
    remote_client: str = "未检测到"
    remote_state: str = "unknown"
    remote_detail: str = "尚未执行深度健康检查"
    remote_apps: tuple[str, ...] = ()
    checked_at: str = "--:--:--"
    log_lines: tuple[str, ...] = ()

    @property
    def overall(self) -> str:
        if self.campus_service == "active" and self.network_state == "connected":
            if self.internet_state in ("online", "unknown"):
                return "online"
        if self.network_state == "connected":
            return "attention"
        return "offline"


class StatusWorker(QThread):
    completed = Signal(object)

    def run(self) -> None:
        status = DesktopStatus(checked_at=time.strftime("%H:%M:%S"))
        interface = default_interface()
        if interface:
            status.interface = interface
            status.connection_name = run_command(
                ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", interface]
            ) or "未命名连接"
            status.connection_type = run_command(
                ["nmcli", "-g", "GENERAL.TYPE", "device", "show", interface]
            ) or "unknown"
            device_state = run_command(
                ["nmcli", "-g", "GENERAL.STATE", "device", "show", interface]
            )
            status.network_state = "connected" if device_state.startswith("100") else "unknown"

        connectivity = run_command(["nmcli", "networking", "connectivity"])
        status.internet_state = {
            "full": "online",
            "limited": "limited",
            "portal": "portal",
            "none": "offline",
        }.get(connectivity, "unknown")

        service_name, service_state = active_service(
            ("campus-autologin.service", "cqu-autologin.service")
        )
        status.campus_service_name = service_name
        status.campus_service = service_state

        remote_name, remote_state = active_service(
            ("campus-remote-recovery.service", "todesk-monitor.service"),
            user=True,
        )
        status.remote_service_name = remote_name
        status.remote_service = remote_state

        if remote_health is not None:
            try:
                apps = remote_health.detect_remote_apps()
                processes = remote_health.running_process_names()
                health_rows = [
                    (app, remote_health.app_health(app, processes)) for app in apps
                ]
            except (OSError, RuntimeError):
                health_rows = []
            if health_rows:
                status.remote_apps = tuple(
                    f"{app.display_name}：{translate_remote_state(health.state)}"
                    for app, health in health_rows
                )
                status.remote_client = "、".join(app.display_name for app, _ in health_rows)
                selected_app, selected_health = next(
                    (
                        row
                        for row in health_rows
                        if row[1].state == "unhealthy"
                    ),
                    next(
                        (row for row in health_rows if row[1].state == "unknown"),
                        health_rows[0],
                    ),
                )
                status.remote_state = selected_health.state
                status.remote_detail = translate_remote_reason(selected_health.reason)
            else:
                status.remote_detail = "未检测到受支持的远程控制软件"
        elif run_command(["pgrep", "-x", "ToDesk"]):
            status.remote_client = "ToDesk"
            status.remote_state = "unknown"
            status.remote_detail = "深度健康模块不可用"

        logs: list[str] = []
        if service_name:
            output = run_command(
                ["journalctl", "-u", service_name, "-n", "12", "--no-pager", "-o", "cat"],
                5,
            )
            for line in reversed(output.splitlines()):
                normalized = line.strip()
                if not normalized:
                    continue
                if any(
                    marker in normalized.casefold()
                    for marker in (
                        "internet is online",
                        "waiting for network",
                        "reconnect request completed",
                        "trying portal login",
                    )
                ):
                    logs.append(normalized[:180])
                if len(logs) >= 3:
                    break
        if remote_name:
            output = run_command(
                [
                    "journalctl",
                    "--user",
                    "-u",
                    remote_name,
                    "-n",
                    "12",
                    "--no-pager",
                    "-o",
                    "cat",
                ],
                5,
            )
            for line in reversed(output.splitlines()):
                normalized = line.strip()
                if any(
                    marker in normalized.casefold()
                    for marker in ("internet", "reconnect", "recovered", "started")
                ):
                    logs.append(normalized[:180])
                if len(logs) >= 5:
                    break
        status.log_lines = tuple(logs)
        self.completed.emit(status)


class UpdateWorker(QThread):
    completed = Signal(object)

    def __init__(self, version: Version) -> None:
        super().__init__()
        self.version = version

    def run(self) -> None:
        self.completed.emit(UpdateChecker(self.version).check(automatic=False))


class ControlWorker(QThread):
    completed = Signal(object)

    def __init__(self, action: str, *arguments: object, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.action = action
        self.arguments = arguments

    def run(self) -> None:
        if self.action == "master":
            result = desktop_control.set_master_enabled(bool(self.arguments[0]))
        elif self.action == "reauthenticate":
            result = desktop_control.reauthenticate()
        elif self.action == "replace-profile":
            profile = self.arguments[0]
            result = (
                desktop_control.replace_profile(profile)
                if isinstance(profile, dict)
                else desktop_control.ControlResult(False, "invalid_request")
            )
        else:
            result = desktop_control.ControlResult(False, "invalid_action")
        self.arguments = ()
        self.completed.emit(result)


CONTROL_ERROR_LABELS = {
    "control_unavailable": "受保护控制组件尚未安装，请先运行新版安装程序。",
    "permission_denied": "操作已取消，或系统管理员授权未通过。",
    "operation_timeout": "系统授权或服务操作超时，请稍后重试。",
    "service_missing": "没有找到已安装的校园网自动认证服务。",
    "service_operation_failed": "后台服务未能完成操作，原有设置已尽量保留。",
    "remote_service_failed": "远程维护服务操作失败，校园认证开关已回滚。",
    "partial_failure": "部分服务状态未能回滚，请检查后台服务状态。",
    "config_missing": "没有找到已保存的校园网配置。",
    "config_invalid": "新账号配置校验失败，原配置没有被替换。",
    "invalid_username": "校园网账号格式无效。",
    "invalid_password": "校园网密码格式无效。",
    "invalid_school_type": "请选择受支持的学校或认证方式。",
    "invalid_portal": "认证服务器参数无效，请检查地址和协议字段。",
    "legacy_service_incompatible": "当前是旧版重庆大学专用后台。切换其他学校前，需要先升级为通用后台服务。原配置没有改变。",
}


def control_error_message(code: str) -> str:
    return CONTROL_ERROR_LABELS.get(code, "操作没有完成，当前配置未被确认修改。")


class StatusOrb(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._phase = 0.0
        self._color = QColor(COLORS["green"])
        self.setFixedSize(138, 138)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.advance)
        self.timer.start(45)

    def set_state(self, state: str) -> None:
        self._color = QColor(
            COLORS["green"] if state == "online" else COLORS["amber"] if state == "attention" else COLORS["red"]
        )
        self.update()

    def advance(self) -> None:
        self._phase = (self._phase + 0.02) % 1.0
        self.update()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2)
        pulse = 42 + 10 * self._phase
        halo = QColor(self._color)
        halo.setAlpha(max(12, int(56 * (1 - self._phase))))
        painter.setPen(Qt.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(center, pulse, pulse)
        ring = QPen(self._color, 2)
        ring.setCosmetic(True)
        painter.setPen(ring)
        painter.setBrush(QColor(COLORS["panel_alt"]))
        painter.drawEllipse(center, 39, 39)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(center, 14, 14)
        painter.setPen(QPen(QColor(COLORS["ink"]), 3))
        painter.drawLine(int(center.x() - 6), int(center.y()), int(center.x() - 1), int(center.y() + 6))
        painter.drawLine(int(center.x() - 1), int(center.y() + 6), int(center.x() + 8), int(center.y() - 7))


class RouteDiagram(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._phase = 0.0
        self.setMinimumHeight(92)
        timer = QTimer(self)
        timer.timeout.connect(self.advance)
        timer.start(35)

    def advance(self) -> None:
        self._phase = (self._phase + 0.012) % 1.0
        self.update()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        y = self.height() / 2
        left, right = 34.0, self.width() - 34.0
        path = QPainterPath(QPointF(left, y))
        path.cubicTo(
            QPointF(self.width() * 0.33, y - 22),
            QPointF(self.width() * 0.67, y + 22),
            QPointF(right, y),
        )
        painter.setPen(QPen(QColor(COLORS["line"]), 3))
        painter.drawPath(path)
        painter.setPen(QPen(QColor(COLORS["green"]), 3))
        segment = QPainterPath()
        start = max(0.0, self._phase - 0.18)
        segment.moveTo(path.pointAtPercent(start))
        steps = 22
        for index in range(1, steps + 1):
            point = start + (self._phase - start) * index / steps
            segment.lineTo(path.pointAtPercent(min(point, 1.0)))
        painter.drawPath(segment)
        for x, color in ((left, COLORS["cyan"]), (self.width() / 2, COLORS["amber"]), (right, COLORS["green"])):
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(COLORS["panel"]))
            painter.drawEllipse(QPointF(x, y), 12, 12)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(x, y), 5, 5)


def shadow(widget: QWidget, blur: int = 28, opacity: int = 95) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, 10)
    color = QColor("#000000")
    color.setAlpha(opacity)
    effect.setColor(color)
    widget.setGraphicsEffect(effect)


class MetricCard(QFrame):
    def __init__(self, eyebrow: str, title: str, detail: str, accent: str) -> None:
        super().__init__()
        self.setObjectName("metricCard")
        self.setProperty("accent", accent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(7)
        top = QHBoxLayout()
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color:{accent}; font-size:13px;")
        label = QLabel(eyebrow.upper())
        label.setObjectName("eyebrow")
        top.addWidget(self.dot)
        top.addWidget(label)
        top.addStretch()
        layout.addLayout(top)
        self.title = QLabel(title)
        self.title.setObjectName("metricTitle")
        self.detail = QLabel(detail)
        self.detail.setObjectName("metricDetail")
        self.detail.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.detail)
        shadow(self, 20, 65)

    def update_content(self, title: str, detail: str, accent: str) -> None:
        self.title.setText(title)
        self.detail.setText(detail)
        self.dot.setStyleSheet(f"color:{accent}; font-size:13px;")


class LogDialog(QDialog):
    def __init__(self, lines: tuple[str, ...], parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("最近状态记录")
        self.resize(720, 430)
        layout = QVBoxLayout(self)
        title = QLabel("最近状态记录")
        title.setObjectName("dialogTitle")
        subtitle = QLabel("只展示校园网和恢复服务的脱敏状态摘要。")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 12, 12, 12)
        if lines:
            for index, line in enumerate(lines, 1):
                label = QLabel(f"{index:02d}  {line}")
                label.setObjectName("logLine")
                label.setWordWrap(True)
                content_layout.addWidget(label)
        else:
            empty = QLabel("当前没有可展示的状态变化记录。")
            empty.setObjectName("muted")
            content_layout.addWidget(empty)
        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)


class SettingsDialog(QDialog):
    def __init__(self, status: DesktopStatus, parent: "MainWindow") -> None:
        super().__init__(parent)
        self.main_window = parent
        self.worker: Optional[ControlWorker] = None
        self.initial_enabled = (
            status.campus_service == "active"
            and status.remote_service in ("active", "missing")
        )
        self.setWindowTitle("设置 · 校园网连接助手")
        self.setMinimumWidth(580)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(18)
        title = QLabel("自动化设置")
        title.setObjectName("dialogTitle")
        subtitle = QLabel("总开关控制校园网自动认证与远程软件在线维护。")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        panel = QFrame()
        panel.setObjectName("dialogPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(20, 18, 20, 18)
        panel_layout.setSpacing(12)
        self.master_switch = QCheckBox("启用自动连接与远程维护")
        self.master_switch.setObjectName("masterSwitch")
        self.master_switch.setChecked(self.initial_enabled)
        panel_layout.addWidget(self.master_switch)
        explanation = QLabel(
            "关闭后，后台不再自动认证校园网，也不再自动恢复远程控制软件；"
            "本窗口仍可打开，并可随时重新开启。"
        )
        explanation.setObjectName("muted")
        explanation.setWordWrap(True)
        panel_layout.addWidget(explanation)

        campus_state = "运行中" if status.campus_service == "active" else "已停止"
        remote_state = {
            "active": "运行中",
            "inactive": "已停止",
            "missing": "未安装（不影响总开关）",
        }.get(status.remote_service, "待确认")
        service_summary = QLabel(
            f"校园认证：{campus_state}  ·  远程维护：{remote_state}"
        )
        service_summary.setObjectName("serviceSummary")
        service_summary.setWordWrap(True)
        panel_layout.addWidget(service_summary)
        layout.addWidget(panel)

        self.progress_label = QLabel("更改开关时，系统会要求管理员授权。")
        self.progress_label.setObjectName("muted")
        layout.addWidget(self.progress_label)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.apply_button = QPushButton("保存并应用")
        self.apply_button.setProperty("primary", True)
        self.apply_button.clicked.connect(self.apply_change)
        actions.addWidget(cancel)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)

    def apply_change(self) -> None:
        target = self.master_switch.isChecked()
        if target == self.initial_enabled:
            self.accept()
            return
        if not target:
            answer = QMessageBox.question(
                self,
                "关闭自动化功能",
                "关闭后，校园网掉线不会自动认证，远程软件掉线也不会自动恢复。\n\n确认关闭吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self.master_switch.setChecked(True)
                return
        self.apply_button.setEnabled(False)
        self.master_switch.setEnabled(False)
        self.progress_label.setText("正在等待系统授权并更新后台服务…")
        self.worker = ControlWorker("master", target, parent=self)
        self.worker.completed.connect(lambda result: self.finish_change(result, target))
        self.worker.start()

    def finish_change(self, result: desktop_control.ControlResult, target: bool) -> None:
        self.apply_button.setEnabled(True)
        self.master_switch.setEnabled(True)
        if not result.ok:
            self.progress_label.setText(control_error_message(result.code))
            QMessageBox.warning(self, "设置未生效", control_error_message(result.code))
            return
        self.initial_enabled = target
        self.progress_label.setText("自动化功能已开启。" if target else "自动化功能已关闭。")
        self.main_window.refresh_status()
        QMessageBox.information(
            self,
            "设置已生效",
            "校园网自动认证与远程维护已开启。"
            if target
            else "校园网自动认证与远程维护已停止；界面仍可继续使用。",
        )
        self.accept()

    def reject(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        super().reject()


class AccountDialog(QDialog):
    def __init__(self, parent: "MainWindow") -> None:
        super().__init__(parent)
        self.main_window = parent
        self.worker: Optional[ControlWorker] = None
        self.setWindowTitle("重新认证 · 校园网连接助手")
        self.setMinimumWidth(620)
        self.resize(650, 780)
        self.setMaximumHeight(820)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("dialogContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(18)
        title = QLabel("校园网认证")
        title.setObjectName("dialogTitle")
        subtitle = QLabel("可以使用已保存配置重新检测，也可以更换学校、认证方式和校园网账号。")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        current_panel = QFrame()
        current_panel.setObjectName("dialogPanel")
        current_layout = QHBoxLayout(current_panel)
        current_layout.setContentsMargins(20, 17, 20, 17)
        current_text = QVBoxLayout()
        current_title = QLabel("使用已保存账号")
        current_title.setObjectName("sectionTitle")
        current_detail = QLabel("立即重启认证检测；不会显示或修改已保存密码。")
        current_detail.setObjectName("muted")
        current_text.addWidget(current_title)
        current_text.addWidget(current_detail)
        current_layout.addLayout(current_text)
        current_layout.addStretch()
        self.reauth_button = QPushButton("立即重新检测")
        self.reauth_button.clicked.connect(self.reauthenticate)
        current_layout.addWidget(self.reauth_button)
        layout.addWidget(current_panel)

        account_panel = QFrame()
        account_panel.setObjectName("dialogPanel")
        account_layout = QVBoxLayout(account_panel)
        account_layout.setContentsMargins(20, 18, 20, 18)
        account_layout.setSpacing(11)
        account_title = QLabel("更换学校、认证方式或账号")
        account_title.setObjectName("sectionTitle")
        account_layout.addWidget(account_title)

        school_label = QLabel("学校与认证方式")
        school_label.setObjectName("fieldLabel")
        self.school_type = QComboBox()
        self.school_type.addItem("重庆大学（CQU / Dr.COM 预设）", "cqu")
        self.school_type.addItem("其他学校（Dr.COM / ePortal）", "drcom")
        self.school_type.addItem("深澜 SRUN 校园网", "srun")
        self.school_type.addItem("通用 HTTP 门户", "generic")
        self.school_type.currentIndexChanged.connect(self.update_portal_fields)
        self.school_hint = QLabel("")
        self.school_hint.setObjectName("schoolHint")
        self.school_hint.setWordWrap(True)
        account_layout.addWidget(school_label)
        account_layout.addWidget(self.school_type)
        account_layout.addWidget(self.school_hint)

        self.login_url_label = QLabel("认证 API 地址")
        self.login_url_label.setObjectName("fieldLabel")
        self.login_url = QLineEdit()
        self.login_url.setMaxLength(2048)
        self.account_prefix_label = QLabel("账号字段前缀")
        self.account_prefix_label.setObjectName("fieldLabel")
        self.account_prefix = QLineEdit(",0,")
        self.account_prefix.setMaxLength(256)
        self.base_url_label = QLabel("SRUN 认证服务器 Base URL")
        self.base_url_label.setObjectName("fieldLabel")
        self.base_url = QLineEdit()
        self.base_url.setMaxLength(2048)
        self.ac_id_label = QLabel("AC ID")
        self.ac_id_label.setObjectName("fieldLabel")
        self.ac_id = QLineEdit("1")
        self.ac_id.setMaxLength(64)
        self.method_label = QLabel("请求方法")
        self.method_label.setObjectName("fieldLabel")
        self.method = QComboBox()
        self.method.addItems(("POST", "GET"))
        self.success_label = QLabel("认证成功标识")
        self.success_label.setObjectName("fieldLabel")
        self.success_contains = QLineEdit()
        self.success_contains.setPlaceholderText("响应正文中代表认证成功的文字")
        self.success_contains.setMaxLength(512)
        self.portal_fields = (
            (self.login_url_label, self.login_url),
            (self.account_prefix_label, self.account_prefix),
            (self.base_url_label, self.base_url),
            (self.ac_id_label, self.ac_id),
            (self.method_label, self.method),
            (self.success_label, self.success_contains),
        )
        for label, field in self.portal_fields:
            account_layout.addWidget(label)
            account_layout.addWidget(field)

        username_label = QLabel("新账号")
        username_label.setObjectName("fieldLabel")
        self.username = QLineEdit()
        self.username.setPlaceholderText("输入新的校园网账号")
        self.username.setMaxLength(256)
        password_label = QLabel("新密码")
        password_label.setObjectName("fieldLabel")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("密码仅通过受保护管道提交")
        self.password.setMaxLength(1024)
        confirm_label = QLabel("确认新密码")
        confirm_label.setObjectName("fieldLabel")
        self.password_confirm = QLineEdit()
        self.password_confirm.setEchoMode(QLineEdit.Password)
        self.password_confirm.setPlaceholderText("再次输入新密码")
        self.password_confirm.setMaxLength(1024)
        for label, field in (
            (username_label, self.username),
            (password_label, self.password),
            (confirm_label, self.password_confirm),
        ):
            account_layout.addWidget(label)
            account_layout.addWidget(field)
        self.update_portal_fields()

        privacy = QLabel(
            "已保存密码不会回显。新密码不会进入命令行、环境变量或应用日志；"
            "保存失败时自动恢复原配置。"
        )
        privacy.setObjectName("privacyNote")
        privacy.setWordWrap(True)
        account_layout.addWidget(privacy)
        layout.addWidget(account_panel)

        self.progress_label = QLabel("更换账号会请求管理员授权，并短暂重启认证服务。")
        self.progress_label.setObjectName("muted")
        layout.addWidget(self.progress_label)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("关闭")
        cancel.clicked.connect(self.reject)
        self.save_button = QPushButton("保存学校与账号")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self.replace_account)
        actions.addWidget(cancel)
        actions.addWidget(self.save_button)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        actions.setContentsMargins(28, 10, 28, 22)
        outer.addLayout(actions)

    def set_busy(self, busy: bool, message: str) -> None:
        self.reauth_button.setEnabled(not busy)
        self.save_button.setEnabled(not busy)
        self.username.setEnabled(not busy)
        self.password.setEnabled(not busy)
        self.password_confirm.setEnabled(not busy)
        self.school_type.setEnabled(not busy)
        for _label, field in self.portal_fields:
            field.setEnabled(not busy)
        self.progress_label.setText(message)

    def update_portal_fields(self) -> None:
        school_type = str(self.school_type.currentData())
        visible = {
            "login_url": school_type in ("drcom", "generic"),
            "account_prefix": school_type == "drcom",
            "base_url": school_type == "srun",
            "ac_id": school_type == "srun",
            "method": school_type == "generic",
            "success_contains": school_type == "generic",
        }
        for key, pair in zip(
            ("login_url", "account_prefix", "base_url", "ac_id", "method", "success_contains"),
            self.portal_fields,
        ):
            pair[0].setVisible(visible[key])
            pair[1].setVisible(visible[key])
        if school_type == "cqu":
            self.school_hint.setText("已内置重庆大学 Dr.COM 认证地址与账号前缀，无需填写服务器参数。")
        elif school_type == "drcom":
            self.school_hint.setText("适用于使用 Dr.COM 或 ePortal 登录接口的其他学校。")
            self.login_url.setPlaceholderText("例如 https://portal.example.edu/eportal/portal/login")
        elif school_type == "srun":
            self.school_hint.setText("填写服务器根地址，不要包含 /cgi-bin/get_challenge。")
            self.base_url.setPlaceholderText("例如 http://10.0.0.55")
        else:
            self.school_hint.setText("适用于简单 GET/POST 表单；复杂参数仍建议使用配置文件向导。")
            self.login_url.setPlaceholderText("例如 https://portal.example.edu/login")

    def reauthenticate(self) -> None:
        self.set_busy(True, "正在等待系统授权并重新启动认证检测…")
        self.worker = ControlWorker("reauthenticate", parent=self)
        self.worker.completed.connect(self.finish_reauthentication)
        self.worker.start()

    def finish_reauthentication(self, result: desktop_control.ControlResult) -> None:
        self.set_busy(False, "认证检测操作已结束。")
        if not result.ok:
            QMessageBox.warning(self, "重新认证未完成", control_error_message(result.code))
            return
        self.main_window.refresh_status()
        QMessageBox.information(self, "已重新检测", "认证服务已重新启动并立即检查当前网络状态。")

    def replace_account(self) -> None:
        username = self.username.text().strip()
        password = self.password.text()
        confirmation = self.password_confirm.text()
        if not username:
            QMessageBox.warning(self, "账号不能为空", "请输入新的校园网账号。")
            self.username.setFocus()
            return
        if not password:
            QMessageBox.warning(self, "密码不能为空", "更换账号时必须同时输入该账号的密码。")
            self.password.setFocus()
            return
        if password != confirmation:
            QMessageBox.warning(self, "密码不一致", "两次输入的新密码不一致。")
            self.password_confirm.setFocus()
            return
        school_type = str(self.school_type.currentData())
        required_field: Optional[tuple[QLineEdit, str]] = None
        if school_type == "drcom" and not self.login_url.text().strip():
            required_field = (self.login_url, "请输入 Dr.COM / ePortal 认证 API 地址。")
        elif school_type == "srun" and not self.base_url.text().strip():
            required_field = (self.base_url, "请输入 SRUN 认证服务器 Base URL。")
        elif school_type == "srun" and not self.ac_id.text().strip():
            required_field = (self.ac_id, "请输入 SRUN 的 AC ID。")
        elif school_type == "generic" and not self.login_url.text().strip():
            required_field = (self.login_url, "请输入通用门户认证地址。")
        elif school_type == "generic" and not self.success_contains.text().strip():
            required_field = (self.success_contains, "请输入认证成功响应中会出现的文字。")
        if required_field is not None:
            QMessageBox.warning(self, "认证参数不完整", required_field[1])
            required_field[0].setFocus()
            return
        profile = {
            "username": username,
            "password": password,
            "school_type": school_type,
            "login_url": self.login_url.text().strip(),
            "account_prefix": self.account_prefix.text(),
            "base_url": self.base_url.text().strip(),
            "ac_id": self.ac_id.text().strip(),
            "method": self.method.currentText(),
            "success_contains": self.success_contains.text(),
        }
        self.set_busy(True, "正在校验学校、认证协议与账号，并安全更新配置…")
        self.worker = ControlWorker("replace-profile", profile, parent=self)
        self.password.clear()
        self.password_confirm.clear()
        password = ""
        confirmation = ""
        self.worker.completed.connect(self.finish_account_change)
        self.worker.start()

    def finish_account_change(self, result: desktop_control.ControlResult) -> None:
        self.set_busy(False, "账号更换操作已结束。")
        if not result.ok:
            QMessageBox.warning(self, "账号未更换", control_error_message(result.code))
            return
        self.username.clear()
        self.main_window.refresh_status()
        QMessageBox.information(
            self,
            "认证配置已更新",
            "学校、认证方式和新账号已安全保存，认证服务已经重新检测当前网络。",
        )

    def reject(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        self.password.clear()
        self.password_confirm.clear()
        super().reject()


class MainWindow(QMainWindow):
    def __init__(self, version: Version) -> None:
        super().__init__()
        self.version = version
        self.status = DesktopStatus()
        self.status_worker: Optional[StatusWorker] = None
        self.update_worker: Optional[UpdateWorker] = None
        self.tray: Optional[QSystemTrayIcon] = None
        self.setWindowTitle("校园网连接助手")
        self.setWindowIcon(application_icon())
        self.setMinimumSize(980, 650)
        self.resize(1060, 700)
        self.build_ui()
        self.apply_styles()
        self.setup_tray()
        self.refresh_status()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_status)
        self.refresh_timer.start(30_000)

    def build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(34, 28, 34, 30)
        outer.setSpacing(24)

        header = QHBoxLayout()
        brand = QVBoxLayout()
        kicker = QLabel("CAMPUS / LINK CONTROL")
        kicker.setObjectName("kicker")
        title = QLabel("校园网连接助手")
        title.setObjectName("appTitle")
        brand.addWidget(kicker)
        brand.addWidget(title)
        header.addLayout(brand)
        header.addStretch()
        self.checked_label = QLabel("正在读取本机状态…")
        self.checked_label.setObjectName("headerMeta")
        version = QLabel(f"v{self.version}  PREVIEW")
        version.setObjectName("versionPill")
        header.addWidget(self.checked_label)
        header.addSpacing(16)
        header.addWidget(version)
        outer.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(22)

        hero = QFrame()
        hero.setObjectName("hero")
        hero.setMinimumWidth(550)
        shadow(hero, 34, 95)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(30, 26, 30, 26)
        hero_layout.setSpacing(12)
        top = QHBoxLayout()
        text = QVBoxLayout()
        state_eyebrow = QLabel("当前链路")
        state_eyebrow.setObjectName("eyebrow")
        self.hero_title = QLabel("状态检测中")
        self.hero_title.setObjectName("heroTitle")
        self.hero_subtitle = QLabel("正在读取 NetworkManager 与后台服务")
        self.hero_subtitle.setObjectName("heroSubtitle")
        text.addWidget(state_eyebrow)
        text.addWidget(self.hero_title)
        text.addWidget(self.hero_subtitle)
        top.addLayout(text)
        top.addStretch()
        self.orb = StatusOrb()
        top.addWidget(self.orb)
        hero_layout.addLayout(top)

        self.route = RouteDiagram()
        hero_layout.addWidget(self.route)
        route_labels = QHBoxLayout()
        for label_text in ("设备", "校园网认证", "公网"):
            label = QLabel(label_text)
            label.setObjectName("routeLabel")
            route_labels.addWidget(label)
            if label_text != "公网":
                route_labels.addStretch()
        hero_layout.addLayout(route_labels)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setObjectName("divider")
        hero_layout.addWidget(divider)

        details = QHBoxLayout()
        self.connection_value = self.detail_block("连接", "--")
        self.interface_value = self.detail_block("接口", "--")
        self.internet_value = self.detail_block("公网", "--")
        for block in (self.connection_value[0], self.interface_value[0], self.internet_value[0]):
            details.addWidget(block, 1)
        hero_layout.addLayout(details)
        body.addWidget(hero, 3)

        side = QVBoxLayout()
        side.setSpacing(16)
        self.auth_card = MetricCard("AUTH SERVICE", "检测中", "校园网认证后台", COLORS["amber"])
        self.remote_card = MetricCard("远程软件在线维护", "检测中", "进程、服务与中心连接", COLORS["cyan"])
        self.update_card = MetricCard("RELEASE CHANNEL", f"当前 v{self.version}", "自动检查尚未启用", COLORS["amber"])
        side.addWidget(self.auth_card)
        side.addWidget(self.remote_card)
        side.addWidget(self.update_card)
        side.addStretch()
        body.addLayout(side, 2)
        outer.addLayout(body, 1)

        actions = QFrame()
        actions.setObjectName("actionBar")
        action_layout = QHBoxLayout(actions)
        action_layout.setContentsMargins(18, 14, 18, 14)
        action_layout.setSpacing(12)
        self.refresh_button = self.button("立即检测", primary=True)
        self.refresh_button.clicked.connect(self.refresh_status)
        auth_button = self.button("重新认证")
        auth_button.clicked.connect(self.show_account_dialog)
        remote_button = self.button("远程维护")
        remote_button.clicked.connect(self.show_remote_status)
        logs_button = self.button("查看记录")
        logs_button.clicked.connect(self.show_logs)
        self.update_button = self.button("检查更新")
        self.update_button.clicked.connect(self.check_updates)
        settings_button = self.button("设置")
        settings_button.clicked.connect(self.show_settings_dialog)
        for button in (
            self.refresh_button,
            auth_button,
            remote_button,
            logs_button,
            self.update_button,
            settings_button,
        ):
            action_layout.addWidget(button)
        action_layout.addStretch()
        safety = QLabel("敏感操作会请求系统授权 · 密码不写入日志")
        safety.setObjectName("safety")
        action_layout.addWidget(safety)
        outer.addWidget(actions)

    def detail_block(self, label_text: str, value_text: str) -> tuple[QWidget, QLabel]:
        block = QWidget()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(label_text)
        label.setObjectName("detailLabel")
        value = QLabel(value_text)
        value.setObjectName("detailValue")
        value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(label)
        layout.addWidget(value)
        return block, value

    def button(self, text: str, *, primary: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setCursor(Qt.PointingHandCursor)
        button.setProperty("primary", primary)
        button.setMinimumHeight(42)
        return button

    def apply_styles(self) -> None:
        self.setStyleSheet(
            f"""
            QWidget {{
                font-family: 'Noto Sans CJK SC';
                color: {COLORS['text']};
                font-size: 14px;
            }}
            QMainWindow, QWidget#root {{
                background: {COLORS['ink']};
            }}
            QLabel#kicker {{
                color: {COLORS['cyan']};
                font-family: 'DejaVu Sans Mono';
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 2px;
            }}
            QLabel#appTitle {{
                color: {COLORS['text']};
                font-size: 28px;
                font-weight: 800;
            }}
            QLabel#headerMeta {{ color: {COLORS['muted']}; font-size: 12px; }}
            QLabel#versionPill {{
                color: {COLORS['amber']};
                background: #2b251a;
                border: 1px solid #5e4c2d;
                border-radius: 13px;
                padding: 6px 12px;
                font-family: 'DejaVu Sans Mono';
                font-size: 11px;
                font-weight: 700;
            }}
            QFrame#hero, QFrame#metricCard {{
                background: {COLORS['panel']};
                border: 1px solid {COLORS['line']};
                border-radius: 18px;
            }}
            QLabel#eyebrow {{
                color: {COLORS['muted']};
                font-family: 'DejaVu Sans Mono';
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 1.5px;
            }}
            QLabel#heroTitle {{ font-size: 35px; font-weight: 900; }}
            QLabel#heroSubtitle {{ color: {COLORS['muted']}; font-size: 14px; }}
            QLabel#routeLabel {{ color: {COLORS['muted']}; font-size: 11px; }}
            QFrame#divider {{ color: {COLORS['line']}; background: {COLORS['line']}; max-height: 1px; }}
            QLabel#detailLabel {{ color: {COLORS['muted']}; font-size: 11px; }}
            QLabel#detailValue {{ color: {COLORS['text']}; font-size: 15px; font-weight: 700; }}
            QLabel#metricTitle {{ font-size: 19px; font-weight: 800; }}
            QLabel#metricDetail {{ color: {COLORS['muted']}; font-size: 12px; }}
            QFrame#actionBar {{
                background: #0a1519;
                border: 1px solid {COLORS['line']};
                border-radius: 14px;
            }}
            QPushButton {{
                color: {COLORS['text']};
                background: {COLORS['panel_alt']};
                border: 1px solid #29434b;
                border-radius: 10px;
                padding: 8px 17px;
                font-weight: 650;
            }}
            QPushButton:hover {{ background: #173039; border-color: {COLORS['cyan']}; }}
            QPushButton:pressed {{ background: #0b2027; }}
            QPushButton[primary="true"] {{
                color: {COLORS['ink']};
                background: {COLORS['green']};
                border-color: {COLORS['green']};
            }}
            QPushButton[primary="true"]:hover {{ background: #76edb2; }}
            QPushButton:disabled {{ color: #61726d; background: #101a1d; border-color: #1b292d; }}
            QLabel#safety {{ color: {COLORS['muted']}; font-size: 11px; }}
            QLabel#dialogTitle {{ font-size: 23px; font-weight: 800; }}
            QLabel#muted {{ color: {COLORS['muted']}; }}
            QLabel#logLine {{
                color: #bcd0ca;
                background: {COLORS['panel_alt']};
                border-left: 3px solid {COLORS['green']};
                padding: 10px 12px;
                font-family: 'DejaVu Sans Mono';
                font-size: 11px;
            }}
            QFrame#dialogPanel {{
                background: {COLORS['panel']};
                border: 1px solid {COLORS['line']};
                border-radius: 14px;
            }}
            QLabel#sectionTitle {{ font-size: 17px; font-weight: 800; }}
            QLabel#fieldLabel {{ color: {COLORS['muted']}; font-size: 11px; font-weight: 700; }}
            QLabel#serviceSummary {{
                color: {COLORS['cyan']};
                background: #0a171b;
                border-radius: 8px;
                padding: 9px 11px;
                font-family: 'DejaVu Sans Mono';
                font-size: 11px;
            }}
            QLabel#privacyNote {{
                color: {COLORS['green']};
                background: {COLORS['green_dark']};
                border: 1px solid #285b47;
                border-radius: 9px;
                padding: 10px 12px;
                font-size: 11px;
            }}
            QLabel#schoolHint {{
                color: {COLORS['cyan']};
                background: #0a171b;
                border-left: 3px solid {COLORS['cyan']};
                padding: 8px 10px;
                font-size: 11px;
            }}
            QLineEdit, QComboBox {{
                color: {COLORS['text']};
                background: #091519;
                border: 1px solid #29434b;
                border-radius: 9px;
                padding: 10px 12px;
                selection-background-color: {COLORS['cyan']};
                min-height: 22px;
            }}
            QLineEdit:focus, QComboBox:focus {{ border-color: {COLORS['cyan']}; }}
            QLineEdit:disabled, QComboBox:disabled {{ color: #61726d; background: #101a1d; }}
            QComboBox QAbstractItemView {{
                color: {COLORS['text']};
                background: {COLORS['panel_alt']};
                border: 1px solid #29434b;
                selection-background-color: #173f31;
                padding: 5px;
            }}
            QCheckBox#masterSwitch {{
                color: {COLORS['text']};
                font-size: 17px;
                font-weight: 800;
                spacing: 13px;
            }}
            QCheckBox#masterSwitch::indicator {{
                width: 44px;
                height: 24px;
                border-radius: 12px;
                border: 1px solid #496169;
                background: #17272c;
            }}
            QCheckBox#masterSwitch::indicator:checked {{
                background: {COLORS['green']};
                border-color: {COLORS['green']};
                image: none;
            }}
            QDialog, QScrollArea, QWidget#dialogContent {{
                background: {COLORS['ink']};
                border: none;
            }}
            QScrollBar:vertical {{ background: #0a1519; width: 9px; }}
            QScrollBar::handle:vertical {{ background: #31505a; border-radius: 4px; min-height: 28px; }}
            """
        )

    def setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(application_icon(), self)
        menu = QMenu()
        show_action = menu.addAction("打开状态窗口")
        show_action.triggered.connect(self.show_normal)
        refresh_action = menu.addAction("立即检测")
        refresh_action.triggered.connect(self.refresh_status)
        menu.addSeparator()
        quit_action = menu.addAction("退出界面（后台服务继续）")
        quit_action.triggered.connect(QApplication.instance().quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_normal() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.setToolTip("校园网连接助手")
        self.tray.show()

    def show_normal(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: object) -> None:
        if self.tray is not None and self.tray.isVisible():
            self.hide()
            event.ignore()
            self.tray.showMessage("校园网连接助手", "界面已隐藏，现有后台服务继续运行。")
            return
        event.accept()

    def refresh_status(self) -> None:
        if self.status_worker is not None and self.status_worker.isRunning():
            return
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("检测中…")
        self.checked_label.setText("正在读取本机状态…")
        self.status_worker = StatusWorker(self)
        self.status_worker.completed.connect(self.apply_status)
        self.status_worker.finished.connect(lambda: self.refresh_button.setEnabled(True))
        self.status_worker.finished.connect(lambda: self.refresh_button.setText("立即检测"))
        self.status_worker.start()

    def apply_status(self, status: DesktopStatus) -> None:
        self.status = status
        state = status.overall
        self.orb.set_state(state)
        title = "链路稳定" if state == "online" else "需要关注" if state == "attention" else "当前离线"
        subtitle = (
            "目标网络与认证服务正在工作"
            if state == "online"
            else "网络存在，但部分状态尚未确认"
            if state == "attention"
            else "未检测到可用的默认网络"
        )
        self.hero_title.setText(title)
        self.hero_subtitle.setText(subtitle)
        self.connection_value[1].setText(status.connection_name)
        self.interface_value[1].setText(status.interface)
        internet_text = {
            "online": "在线",
            "limited": "受限",
            "portal": "需要认证",
            "offline": "离线",
        }.get(status.internet_state, "等待确认")
        self.internet_value[1].setText(internet_text)
        self.checked_label.setText(f"最近检测 {status.checked_at} · {QT_BINDING}")

        auth_ok = status.campus_service == "active"
        self.auth_card.update_content(
            "运行中" if auth_ok else "未运行",
            status.campus_service_name or "未发现校园网服务",
            COLORS["green"] if auth_ok else COLORS["red"],
        )
        monitor_active = status.remote_service == "active"
        client_detected = status.remote_client != "未检测到"
        remote_ok = monitor_active and status.remote_state == "healthy"
        if not client_detected:
            remote_title = "未检测到远程客户端"
        elif monitor_active and status.remote_state == "healthy":
            remote_title = "自动维护运行中"
        elif monitor_active and status.remote_state == "unhealthy":
            remote_title = "自动维护已发现异常"
        elif monitor_active:
            remote_title = "自动维护运行中 · 待确认"
        elif status.remote_state == "healthy":
            remote_title = "客户端在线 · 维护未启用"
        elif status.remote_state == "unhealthy":
            remote_title = "客户端异常 · 维护未启用"
        else:
            remote_title = "自动维护未启用"
        remote_detail = (
            f"{status.remote_client} · {status.remote_detail}"
            if client_detected
            else status.remote_detail
        )
        self.remote_card.update_content(
            remote_title,
            remote_detail,
            COLORS["green"] if remote_ok else COLORS["red"] if status.remote_state == "unhealthy" else COLORS["amber"],
        )
        if self.tray is not None:
            self.tray.setToolTip(f"校园网连接助手 · {title}")

    def show_settings_dialog(self) -> None:
        dialog = SettingsDialog(self.status, self)
        dialog.setStyleSheet(self.styleSheet())
        dialog.exec() if hasattr(dialog, "exec") else dialog.exec_()

    def show_account_dialog(self) -> None:
        dialog = AccountDialog(self)
        dialog.setStyleSheet(self.styleSheet())
        dialog.exec() if hasattr(dialog, "exec") else dialog.exec_()

    def show_remote_status(self) -> None:
        monitor = (
            f"运行中（{self.status.remote_service_name}）"
            if self.status.remote_service == "active"
            else "未运行"
        )
        clients = "\n".join(self.status.remote_apps) or self.status.remote_client
        QMessageBox.information(
            self,
            "远程软件在线维护",
            f"维护服务：{monitor}\n"
            f"客户端：{clients}\n"
            f"深度状态：{self.status.remote_detail}\n\n"
            "运行机制：公网在线时持续检查客户端进程、后台服务和可用的深度连接状态；"
            "连续异常达到阈值后先重启客户端，仍未恢复时才尝试重启其后台服务。\n\n"
            "当前窗口只读取健康状态；自动恢复由已安装的监控服务独立执行，关闭本窗口不会停止维护。",
        )

    def show_logs(self) -> None:
        dialog = LogDialog(self.status.log_lines, self)
        dialog.setStyleSheet(self.styleSheet())
        dialog.exec() if hasattr(dialog, "exec") else dialog.exec_()

    def check_updates(self) -> None:
        if self.update_worker is not None and self.update_worker.isRunning():
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("检查中…")
        self.update_card.update_content("正在检查", "连接 GitHub Releases", COLORS["amber"])
        self.update_worker = UpdateWorker(self.version)
        self.update_worker.completed.connect(self.apply_update_result)
        self.update_worker.finished.connect(lambda: self.update_button.setEnabled(True))
        self.update_worker.finished.connect(lambda: self.update_button.setText("检查更新"))
        self.update_worker.start()

    def apply_update_result(self, result: CheckResult) -> None:
        if result.status == "update_available":
            self.update_card.update_content(
                f"发现 v{result.latest_version}",
                "已确认来自官方 GitHub Release",
                COLORS["green"],
            )
            QMessageBox.information(
                self,
                "发现新版本",
                f"发现 v{result.latest_version}。\n\n{result.release_page}\n\n预览版只提醒，不会自动下载安装。",
            )
        elif result.status == "error":
            self.update_card.update_content("检查失败", result.error_code or "未知错误", COLORS["red"])
        else:
            self.update_card.update_content(f"当前 v{self.version}", "已经是最新版本", COLORS["green"])


def run_gui(version: Version) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Campus Network Assistant")
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName("campus-network-assistant")
    app.setWindowIcon(application_icon())
    app.setQuitOnLastWindowClosed(True)
    lock_path = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "campus-network-assistant-preview.lock"
    lock = QLockFile(str(lock_path))
    lock.setStaleLockTime(30_000)
    if not lock.tryLock(100):
        QMessageBox.information(None, "校园网连接助手", "窗口已经在运行。")
        return 0
    app.setFont(QFont("Noto Sans CJK SC", 10))
    window = MainWindow(version)
    if window.tray is not None:
        app.setQuitOnLastWindowClosed(False)
    window.show()
    result = app.exec() if hasattr(app, "exec") else app.exec_()
    lock.unlock()
    return result
