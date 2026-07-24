from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import remote_recovery as recovery


class ConnectivityStateTests(unittest.TestCase):
    def test_requires_confirmed_outage_and_recovery(self) -> None:
        state = recovery.ConnectivityState(failure_threshold=2, recovery_threshold=2)
        self.assertIsNone(state.observe(True))
        self.assertEqual(state.observe(True), "online")
        self.assertIsNone(state.observe(False))
        self.assertEqual(state.observe(False), "offline")
        self.assertIsNone(state.observe(True))
        self.assertEqual(state.observe(True), "recovered")

    def test_single_failure_does_not_change_online_state(self) -> None:
        state = recovery.ConnectivityState(failure_threshold=2, recovery_threshold=1)
        self.assertEqual(state.observe(True), "online")
        self.assertIsNone(state.observe(False))
        self.assertEqual(state.state, "online")


class DetectionTests(unittest.TestCase):
    def test_linux_detects_todesk_desktop_entry_with_env_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "ToDesk"
            executable.write_text("", encoding="utf-8")
            executable.chmod(0o755)
            desktop = root / "todesk.desktop"
            desktop.write_text(
                "[Desktop Entry]\nName=ToDesk\n"
                f"Exec=env GDK_BACKEND=x11 {executable} --connect=%U\n",
                encoding="utf-8",
            )
            definition = replace(recovery.APP_DEFINITIONS[0], linux_paths=())
            with mock.patch.object(recovery, "APP_DEFINITIONS", (definition,)):
                apps = recovery.detect_linux_apps([root])
        self.assertEqual([app.key for app in apps], ["todesk"])
        self.assertEqual(apps[0].command, (str(executable),))

    def test_windows_detects_sunlogin_from_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "SunloginClient.exe"
            executable.write_text("", encoding="utf-8")
            apps = recovery.detect_windows_apps(
                environ={},
                registry_entries=[("Sunlogin Client", directory, f'"{executable}",0')],
            )
        self.assertEqual([app.key for app in apps], ["sunlogin"])
        self.assertEqual(apps[0].command, (str(executable),))

    def test_no_supported_software_returns_empty_list(self) -> None:
        self.assertEqual(recovery.detect_windows_apps(environ={}, registry_entries=[]), [])


class ProcessTests(unittest.TestCase):
    def test_app_running_is_case_insensitive(self) -> None:
        app = recovery.RemoteApp(recovery.APP_DEFINITIONS[0], ("ToDesk.exe",))
        self.assertTrue(recovery.app_running(app, {"todesk"}))
        self.assertFalse(recovery.app_running(app, {"other.exe"}))


if __name__ == "__main__":
    unittest.main()
