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
                f"Exec=env GDK_BACKEND=x11 {executable.as_posix()} --connect=%U\n",
                encoding="utf-8",
            )
            definition = replace(recovery.APP_DEFINITIONS[0], linux_paths=())
            with mock.patch.object(recovery, "APP_DEFINITIONS", (definition,)):
                apps = recovery.detect_linux_apps([root])
        self.assertEqual([app.key for app in apps], ["todesk"])
        self.assertEqual(Path(apps[0].command[0]), executable)

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

    def test_todesk_center_requires_auth_and_live_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service_log = root / "service-test.log"
            service_log.write_text(
                "2026-07-30 client create connect to comet sock=8 ip=118.24.224.62 port=443\n"
                "2026-07-30 CCenterClientr doAuth connect AuthOk\n",
                encoding="utf-8",
            )
            self.assertEqual(
                recovery.todesk_center_state(root, {"118.24.224.62:443"}),
                "online",
            )
            self.assertEqual(recovery.todesk_center_state(root, set()), "offline")

    def test_todesk_center_disconnect_overrides_previous_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service-test.log").write_text(
                "client create connect to comet sock=8 ip=118.24.224.62 port=443\n"
                "CCenterClientr doAuth connect AuthOk\n"
                "center client disconnect!! sock=8\n",
                encoding="utf-8",
            )
            self.assertEqual(
                recovery.todesk_center_state(root, {"118.24.224.62:443"}),
                "offline",
            )

    def test_generic_health_checks_declared_background_service(self) -> None:
        app = recovery.RemoteApp(recovery.APP_DEFINITIONS[2], ("/usr/bin/anydesk",))
        with mock.patch.object(recovery, "app_running", return_value=True):
            with mock.patch.object(recovery, "app_service_state", return_value="offline"):
                status = recovery.app_health(app)
        self.assertEqual(status.state, "unhealthy")
        self.assertIn("background service", status.reason)

    def test_todesk_health_detects_center_socket_loss(self) -> None:
        app = recovery.RemoteApp(recovery.APP_DEFINITIONS[0], ("/opt/todesk/bin/ToDesk",))
        with mock.patch.object(recovery, "app_running", return_value=True):
            with mock.patch.object(recovery, "app_service_state", return_value="online"):
                with mock.patch.object(recovery, "local_port_open", return_value=True):
                    with mock.patch.object(recovery, "todesk_center_state", return_value="offline"):
                        with mock.patch.object(recovery.platform, "system", return_value="Linux"):
                            status = recovery.app_health(app)
        self.assertEqual(status.state, "unhealthy")
        self.assertEqual(status.failure_threshold, 6)

    def test_anydesk_vendor_status_detects_offline(self) -> None:
        app = recovery.RemoteApp(recovery.APP_DEFINITIONS[2], ("/usr/bin/anydesk",))
        result = mock.Mock(returncode=0, stdout="offline\n", stderr="")
        with mock.patch.object(recovery.subprocess, "run", return_value=result):
            self.assertEqual(recovery.command_connection_state(app), "offline")

    def test_graphical_environment_imports_user_manager_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            authority = Path(directory) / "Xauthority"
            authority.write_text("", encoding="utf-8")
            result = mock.Mock(
                returncode=0,
                stdout=f"DISPLAY=:1\nXAUTHORITY={authority}\nXDG_RUNTIME_DIR=/run/user/1000\n",
            )
            with mock.patch.object(recovery.platform, "system", return_value="Linux"):
                with mock.patch.object(recovery.subprocess, "run", return_value=result):
                    environment = recovery.graphical_environment({})
        self.assertEqual(environment["DISPLAY"], ":1")
        self.assertEqual(environment["XAUTHORITY"], str(authority))

    def test_linux_stop_escalates_when_process_ignores_term(self) -> None:
        app = recovery.RemoteApp(recovery.APP_DEFINITIONS[0], ("/opt/todesk/bin/ToDesk",))
        with mock.patch.object(recovery.platform, "system", return_value="Linux"):
            with mock.patch.object(recovery, "app_running", return_value=True):
                with mock.patch.object(recovery.time, "sleep"):
                    with mock.patch.object(recovery.subprocess, "run") as run:
                        recovery.stop_app(app)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any(command[1] == "-TERM" for command in commands))
        self.assertTrue(any(command[1] == "-KILL" for command in commands))


class RecoveryTests(unittest.TestCase):
    def test_linux_app_can_escalate_to_declared_service(self) -> None:
        app = recovery.RemoteApp(recovery.APP_DEFINITIONS[2], ("/usr/bin/anydesk",))
        with mock.patch.object(recovery.platform, "system", return_value="Linux"):
            with mock.patch.object(recovery, "restart_app", return_value=True) as restart:
                with mock.patch.object(recovery, "wait_for_app_ready", side_effect=[False, True]):
                    with mock.patch.object(recovery, "installed_service_name", return_value="anydesk.service"):
                        with mock.patch.object(recovery, "restart_app_service", return_value=True):
                            recovered, service_restarted = recovery.recover_app(app, "offline", True)
        self.assertTrue(recovered)
        self.assertTrue(service_restarted)
        self.assertEqual(restart.call_count, 2)


if __name__ == "__main__":
    unittest.main()
