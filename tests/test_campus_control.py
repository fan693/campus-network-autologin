from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import campus_control
from campus_desktop import control as desktop_control


def valid_config(username: str = "old-user", password: str = "old-password") -> dict[str, object]:
    return {
        "version": 4,
        "username": username,
        "password": password,
        "network_name": "Campus Ethernet",
        "interface": "eno1",
        "portal": {
            "type": "drcom",
            "login_url": "https://login.cqu.edu.cn:802/eportal/portal/login",
            "account_prefix": ",0,",
        },
        "online_interval": 30,
        "offline_interval": 10,
        "timeout": 5,
        "failure_threshold": 2,
        "network_recovery_after": 300,
        "network_recovery_cooldown": 900,
        "connectivity_checks": [
            {"url": "https://www.gstatic.com/generate_204", "status": 204}
        ],
    }


def profile(school_type: str = "cqu") -> dict[str, str]:
    return {
        "username": "new-user",
        "password": "new-password",
        "school_type": school_type,
        "login_url": "https://portal.example.edu/eportal/portal/login",
        "account_prefix": ",0,",
        "base_url": "http://10.0.0.55",
        "ac_id": "1",
        "method": "POST",
        "success_contains": "success",
    }


class ProtocolTests(unittest.TestCase):
    def test_strict_object_rejects_duplicates_and_non_finite_numbers(self) -> None:
        with self.assertRaises(campus_control.ControlError):
            campus_control.strict_object('{"enabled":true,"enabled":false}')
        with self.assertRaises(campus_control.ControlError):
            campus_control.strict_object('{"value":NaN}')

    def test_set_enabled_accepts_only_a_boolean(self) -> None:
        with mock.patch.object(campus_control, "set_master_enabled") as set_master:
            campus_control.handle("set-enabled", {"enabled": False})
            set_master.assert_called_once_with(False)
            with self.assertRaises(campus_control.ControlError):
                campus_control.handle("set-enabled", {"enabled": "false"})


@unittest.skipUnless(os.name == "posix", "Linux system-service transactions")
class AccountTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        running = mock.patch.object(campus_control, "service_running", return_value=True)
        self.running = running.start()
        self.addCleanup(running.stop)

    def test_account_update_replaces_credentials_and_removes_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(valid_config()), encoding="utf-8")
            transactions = root / "transactions"
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(campus_control, "CONFIG_CANDIDATES", (config_path,)))
                stack.enter_context(mock.patch.object(campus_control, "TRANSACTION_ROOT", transactions))
                stack.enter_context(mock.patch.object(campus_control, "loaded_service", return_value="cqu-autologin.service"))
                stack.enter_context(mock.patch.object(campus_control, "run_systemctl"))
                stack.enter_context(mock.patch.object(campus_control.os, "fchown"))
                campus_control.update_account("new-user", "new-password")
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["username"], "new-user")
            self.assertEqual(updated["password"], "new-password")
            self.assertEqual(list(transactions.iterdir()), [])

    def test_legacy_account_update_preserves_legacy_schema(self) -> None:
        legacy = {
            "student_id": "old-user",
            "password": "old-password",
            "connection_name": "Campus Ethernet",
            "interface": "eno1",
            "online_interval": 30,
            "offline_interval": 10,
            "timeout": 5,
            "failure_threshold": 2,
            "network_recovery_after": 300,
            "network_recovery_cooldown": 900,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(legacy), encoding="utf-8")
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(campus_control, "CONFIG_CANDIDATES", (config_path,)))
                stack.enter_context(mock.patch.object(campus_control, "TRANSACTION_ROOT", root / "transactions"))
                stack.enter_context(mock.patch.object(campus_control, "loaded_service", return_value="cqu-autologin.service"))
                stack.enter_context(mock.patch.object(campus_control, "run_systemctl"))
                stack.enter_context(mock.patch.object(campus_control.os, "fchown"))
                campus_control.update_account("new-user", "new-password")
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["student_id"], "new-user")
            self.assertEqual(updated["password"], "new-password")
            self.assertNotIn("portal", updated)
            self.assertNotIn("username", updated)

    def test_modern_profile_can_switch_to_srun(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(valid_config()), encoding="utf-8")
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(campus_control, "CONFIG_CANDIDATES", (config_path,)))
                stack.enter_context(mock.patch.object(campus_control, "TRANSACTION_ROOT", root / "transactions"))
                stack.enter_context(mock.patch.object(campus_control, "loaded_service", return_value="campus-autologin.service"))
                stack.enter_context(mock.patch.object(campus_control, "run_systemctl"))
                stack.enter_context(mock.patch.object(campus_control.os, "fchown"))
                campus_control.update_profile(profile("srun"))
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["portal"]["type"], "srun")
            self.assertEqual(updated["portal"]["base_url"], "http://10.0.0.55")
            self.assertEqual(updated["portal"]["ac_id"], "1")

    def test_legacy_service_rejects_non_cqu_school_without_changes(self) -> None:
        legacy = {
            "student_id": "old-user",
            "password": "old-password",
            "connection_name": "Campus Ethernet",
            "interface": "eno1",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            original = json.dumps(legacy).encode("utf-8")
            config_path.write_bytes(original)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(campus_control, "loaded_service", return_value="cqu-autologin.service"))
                stack.enter_context(mock.patch.object(campus_control, "CONFIG_CANDIDATES", (config_path,)))
                stack.enter_context(mock.patch.object(campus_control, "TRANSACTION_ROOT", root / "transactions"))
                stack.enter_context(mock.patch.object(campus_control.os, "fchown"))
                with self.assertRaises(campus_control.ControlError) as raised:
                    campus_control.update_profile(profile("srun"))
            self.assertEqual(raised.exception.code, "legacy_service_incompatible")
            self.assertEqual(config_path.read_bytes(), original)

    def test_failed_restart_restores_original_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            original = json.dumps(valid_config(), ensure_ascii=False).encode("utf-8")
            config_path.write_bytes(original)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(campus_control, "CONFIG_CANDIDATES", (config_path,)))
                stack.enter_context(mock.patch.object(campus_control, "TRANSACTION_ROOT", root / "transactions"))
                stack.enter_context(mock.patch.object(campus_control, "loaded_service", return_value="cqu-autologin.service"))
                stack.enter_context(mock.patch.object(
                    campus_control,
                    "run_systemctl",
                    side_effect=campus_control.ControlError("service_operation_failed"),
                ))
                stack.enter_context(mock.patch.object(campus_control.os, "fchown"))
                with self.assertRaises(campus_control.ControlError):
                    campus_control.update_account("new-user", "new-password")
            self.assertEqual(config_path.read_bytes(), original)


    def test_credentials_only_keep_the_entire_portal(self) -> None:
        raw = valid_config()
        raw["portal"] = {"type": "generic", "login_url": "https://school.test/login",
                         "parameters": {"studentNo": "{username}", "realm": "students"},
                         "headers": {"Accept": "application/json"}, "success_contains": ["accepted"]}
        updated = campus_control.config_with_profile(raw, profile("keep"))
        self.assertEqual(updated["portal"], raw["portal"])
        self.assertEqual(updated["username"], "new-user")

    def test_editing_same_generic_portal_keeps_custom_fields(self) -> None:
        raw = valid_config()
        raw["portal"] = {"type": "generic", "login_url": "https://portal.example.edu:443/old",
                         "parameters": {"realm": "students"},
                         "headers": {"Accept": "text/plain"}, "already_online_contains": ["already"]}
        updated = campus_control.config_with_profile(raw, profile("generic"))
        for field in ("parameters", "headers", "already_online_contains"):
            self.assertEqual(updated["portal"][field], raw["portal"][field])
        self.assertEqual(updated["portal"]["method"], "POST")

    def test_changing_school_does_not_carry_previous_headers_to_another_origin(self) -> None:
        raw = valid_config()
        raw["portal"] = {"type": "generic", "login_url": "https://another-school.example/login",
                         "headers": {"X-School": "old"}, "parameters": {"department": "old"}}
        updated = campus_control.config_with_profile(raw, profile("generic"))
        self.assertEqual(updated["portal"]["headers"], {})
        self.assertNotIn("department", updated["portal"]["parameters"])

    def test_inactive_service_stays_stopped_after_profile_edit(self) -> None:
        self.running.return_value = False
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "config.json"
            target.write_text(json.dumps(valid_config()), encoding="utf-8")
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(campus_control, "CONFIG_CANDIDATES", (target,)))
                stack.enter_context(mock.patch.object(campus_control, "TRANSACTION_ROOT", root / "transactions"))
                stack.enter_context(mock.patch.object(campus_control, "loaded_service", return_value="campus-autologin.service"))
                stack.enter_context(mock.patch.object(campus_control.os, "fchown", create=True))
                run = stack.enter_context(mock.patch.object(campus_control, "run_systemctl"))
                campus_control.update_profile(profile("keep"))
                run.assert_not_called()

    def test_recover_interruption_between_replace_and_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "config.json"
            target.write_bytes(b"new")
            transaction = root / "transactions" / "interrupted"
            transaction.mkdir(parents=True)
            (transaction / "config.backup").write_bytes(b"old")
            marker = {"phase": "prepared", "config_path": str(target),
                      "service": "campus-autologin.service", "was_running": True}
            (transaction / "marker.json").write_text(json.dumps(marker), encoding="utf-8")
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(campus_control, "CONFIG_CANDIDATES", (target,)))
                stack.enter_context(mock.patch.object(campus_control, "TRANSACTION_ROOT", transaction.parent))
                stack.enter_context(mock.patch.object(campus_control.os, "fchown", create=True))
                run = stack.enter_context(mock.patch.object(campus_control, "run_systemctl"))
                campus_control.recover_incomplete_transactions()
                run.assert_called_once_with("restart", "campus-autologin.service")
                self.assertEqual(target.read_bytes(), b"old")
                self.assertFalse(transaction.exists())

    def test_control_lock_rejects_overlapping_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(campus_control, "TRANSACTION_ROOT", Path(directory) / "tx"):
                with campus_control.control_lock():
                    with self.assertRaises(campus_control.ControlError) as error:
                        with campus_control.control_lock():
                            self.fail("second helper acquired the transaction lock")
                self.assertEqual(error.exception.code, "control_busy")

    def test_reauthenticate_stopped_monitor_runs_once_without_starting_it(self) -> None:
        self.running.return_value = False
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(campus_control, "loaded_service", return_value="campus-autologin.service"))
            stack.enter_context(mock.patch.object(campus_control, "installed_config", return_value=Path("/example/config")))
            stack.enter_context(mock.patch.object(campus_control.shutil, "which", return_value="/usr/sbin/runuser"))
            run = stack.enter_context(mock.patch.object(campus_control.subprocess, "run", return_value=mock.Mock(returncode=0)))
            service = stack.enter_context(mock.patch.object(campus_control, "run_systemctl"))
            campus_control.handle("reauthenticate", {})
            self.assertIn("--once", run.call_args.args[0])
            self.assertIn("campus-autologin", run.call_args.args[0])
            service.assert_not_called()

    def test_partial_master_change_restores_independent_enable_and_runtime_flags(self) -> None:
        campus = {"name": "campus-autologin.service", "enabled": True, "running": False}
        remote = {"name": "campus-remote-recovery.service", "enabled": False, "running": True}
        states = {unit["name"]: dict(unit) for unit in (campus, remote)}
        def change(prefix, operation, name):
            if name == campus["name"] and operation == "stop":
                self.fail("originally stopped service was restarted")
            if name == remote["name"] and operation == "stop":
                raise campus_control.ControlError("service_operation_failed")
            if operation in ("enable", "disable"):
                states[name]["enabled"] = operation == "enable"
            else:
                states[name]["running"] = operation == "start"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(campus_control, "TRANSACTION_ROOT", root))
                stack.enter_context(mock.patch.object(campus_control.os, "fchown", create=True))
                stack.enter_context(mock.patch.dict(campus_control.os.environ, {"PKEXEC_UID": "1234"}))
                stack.enter_context(mock.patch.object(campus_control, "user_systemctl", return_value=["userctl"]))
                stack.enter_context(mock.patch.object(campus_control, "preferred_unit", side_effect=[campus,remote]))
                stack.enter_context(mock.patch.object(campus_control, "unit_snapshot", side_effect=lambda n, p: dict(states[n])))
                stack.enter_context(mock.patch.object(campus_control, "invoke_unit", side_effect=change))
                with self.assertRaises(campus_control.ControlError):
                    campus_control.set_master_enabled(False)
                self.assertEqual(states[campus["name"]], campus)
                self.assertEqual(states[remote["name"]], remote)
                self.assertEqual(list(root.iterdir()), [])


class DesktopBridgeTests(unittest.TestCase):
    def test_master_operation_is_a_single_privileged_transaction(self) -> None:
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(desktop_control.platform, "system", return_value="Linux"))
            helper = stack.enter_context(mock.patch.object(desktop_control, "invoke_privileged",
                                return_value=desktop_control.ControlResult(True, "ok")))
            self.assertTrue(desktop_control.set_master_enabled(False).ok)
            helper.assert_called_once_with("set-enabled", {"enabled": False})


if __name__ == "__main__":
    unittest.main()
