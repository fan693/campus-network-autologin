from __future__ import annotations

import io
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import configure


class ConfigureWizardTests(unittest.TestCase):
    def test_network_detection_uses_utf8_locale(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout="有线连接 1:eno1:ethernet\n",
        )
        with mock.patch.object(configure.platform, "system", return_value="Linux"):
            with mock.patch.object(
                configure.subprocess, "run", return_value=completed
            ) as run:
                connection, interface = configure.detect_network()

        self.assertEqual((connection, interface), ("有线连接 1", "eno1"))
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C.UTF-8")

    def test_reconfiguration_keeps_custom_portal_options(self) -> None:
        portals = [
            {"type": "drcom", "login_url": "https://example.edu/login",
             "account_prefix": "isp-a", "parameters": {"ac_name": "campus"}},
            {"type": "srun", "base_url": "https://example.edu",
             "challenge_url": "https://example.edu/alternate"},
            {"type": "drcom", "login_url": "https://login.cqu.edu.cn:802/eportal/portal/login",
             "account_prefix": "", "mac": "custom"},
        ]
        for portal in portals:
            with self.subTest(kind=portal["type"], url=portal.get("login_url")):
                with mock.patch("builtins.input", return_value=""), mock.patch("builtins.print"):
                    updated = configure.choose_portal({"portal": portal})
                for key, value in portal.items():
                    self.assertEqual(updated[key], value)

    def test_new_portal_server_does_not_inherit_request_headers(self) -> None:
        old = {"type": "generic", "login_url": "https://first.example/login",
               "parameters": {"tenant": "original"}, "headers": {"Authorization": "fixture"}}
        answers = ["", "https://second.example/login", "", "", "", "", ""]
        with mock.patch("builtins.input", side_effect=answers), mock.patch("builtins.print"):
            changed = configure.choose_portal({"portal": old})
        self.assertEqual(changed["headers"], {})
        self.assertNotIn("tenant", changed["parameters"])

    def test_write_failure_leaves_original_intact_and_no_staged_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "config.json")
            path.write_text("original")
            with self.assertRaises(TypeError):
                configure.write_config(path, {"unsupported": object()})
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(list(path.parent.iterdir()), [path])

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_private_before_serialization(self) -> None:
        dump = json.dump

        def inspect_mode(data, handle, **kwargs):
            self.assertEqual(os.fstat(handle.fileno()).st_mode & 0o777, 0o600)
            return dump(data, handle, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "config.json")
            with mock.patch.object(configure.json, "dump", side_effect=inspect_mode):
                configure.write_config(path, {"password": "fixture"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_password_is_echoed_for_confirmation(self) -> None:
        args = types.SimpleNamespace(
            network_name="Campus",
            interface="wlan0",
            lock_network=False,
        )
        answers = ["", "", "student", "visible-password", ""]
        output = io.StringIO()
        with mock.patch.object(configure, "detect_network", return_value=("", "")):
            with mock.patch("builtins.input", side_effect=answers):
                with mock.patch("sys.stdout", output):
                    config = configure.build_config(args, {})
        self.assertEqual(config["password"], "visible-password")
        self.assertIn("visible-password", output.getvalue())
        self.assertIn("login.cqu.edu.cn", config["portal"]["login_url"])

    def test_installer_network_selection_is_not_prompted_again(self) -> None:
        args = types.SimpleNamespace(
            network_name="校园有线",
            interface="eno1",
            lock_network=True,
        )
        answers = ["student", "visible-password", ""]
        output = io.StringIO()
        with mock.patch.object(configure, "detect_network", return_value=("", "")):
            with mock.patch("builtins.input", side_effect=answers):
                with mock.patch("sys.stdout", output):
                    config = configure.build_config(args, {})

        self.assertEqual(config["network_name"], "校园有线")
        self.assertEqual(config["interface"], "eno1")


if __name__ == "__main__":
    unittest.main()
