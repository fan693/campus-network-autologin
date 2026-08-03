from __future__ import annotations

import json
import tempfile
import unittest
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

    def test_set_enabled_uses_only_detected_fixed_service(self) -> None:
        with (
            mock.patch.object(campus_control, "loaded_service", return_value="cqu-autologin.service"),
            mock.patch.object(campus_control, "run_systemctl") as run,
        ):
            campus_control.handle("set-enabled", {"enabled": False})
        run.assert_called_once_with("disable", "--now", "cqu-autologin.service")


class AccountTransactionTests(unittest.TestCase):
    def test_account_update_replaces_credentials_and_removes_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(valid_config()), encoding="utf-8")
            transactions = root / "transactions"
            with (
                mock.patch.object(campus_control, "CONFIG_CANDIDATES", (config_path,)),
                mock.patch.object(campus_control, "TRANSACTION_ROOT", transactions),
                mock.patch.object(campus_control, "loaded_service", return_value="cqu-autologin.service"),
                mock.patch.object(campus_control, "run_systemctl"),
                mock.patch.object(campus_control.os, "fchown"),
            ):
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
            with (
                mock.patch.object(campus_control, "CONFIG_CANDIDATES", (config_path,)),
                mock.patch.object(campus_control, "TRANSACTION_ROOT", root / "transactions"),
                mock.patch.object(campus_control, "loaded_service", return_value="cqu-autologin.service"),
                mock.patch.object(campus_control, "run_systemctl"),
                mock.patch.object(campus_control.os, "fchown"),
            ):
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
            with (
                mock.patch.object(campus_control, "CONFIG_CANDIDATES", (config_path,)),
                mock.patch.object(campus_control, "TRANSACTION_ROOT", root / "transactions"),
                mock.patch.object(campus_control, "loaded_service", return_value="campus-autologin.service"),
                mock.patch.object(campus_control, "run_systemctl"),
                mock.patch.object(campus_control.os, "fchown"),
            ):
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
            with (
                mock.patch.object(campus_control, "CONFIG_CANDIDATES", (config_path,)),
                mock.patch.object(campus_control, "TRANSACTION_ROOT", root / "transactions"),
                mock.patch.object(campus_control.os, "fchown"),
            ):
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
            with (
                mock.patch.object(campus_control, "CONFIG_CANDIDATES", (config_path,)),
                mock.patch.object(campus_control, "TRANSACTION_ROOT", root / "transactions"),
                mock.patch.object(campus_control, "loaded_service", return_value="cqu-autologin.service"),
                mock.patch.object(
                    campus_control,
                    "run_systemctl",
                    side_effect=campus_control.ControlError("service_operation_failed"),
                ),
                mock.patch.object(campus_control.os, "fchown"),
            ):
                with self.assertRaises(campus_control.ControlError):
                    campus_control.update_account("new-user", "new-password")
            self.assertEqual(config_path.read_bytes(), original)


class DesktopBridgeTests(unittest.TestCase):
    def test_master_switch_rolls_back_system_service_when_remote_fails(self) -> None:
        with (
            mock.patch.object(
                desktop_control,
                "invoke_privileged",
                side_effect=[
                    desktop_control.ControlResult(True, "ok"),
                    desktop_control.ControlResult(True, "ok"),
                ],
            ) as privileged,
            mock.patch.object(desktop_control, "user_service", return_value="todesk-monitor.service"),
            mock.patch.object(desktop_control, "set_user_service", return_value=False),
        ):
            result = desktop_control.set_master_enabled(False)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "remote_service_failed")
        self.assertEqual(
            privileged.call_args_list,
            [
                mock.call("set-enabled", {"enabled": False}),
                mock.call("set-enabled", {"enabled": True}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
