from __future__ import annotations

import io
import types
import unittest
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
