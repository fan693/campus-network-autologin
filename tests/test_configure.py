from __future__ import annotations

import io
import types
import unittest
from unittest import mock

import configure


class ConfigureWizardTests(unittest.TestCase):
    def test_password_is_echoed_for_confirmation(self) -> None:
        args = types.SimpleNamespace(network_name="Campus", interface="wlan0")
        answers = ["", "", "student", "visible-password", ""]
        output = io.StringIO()
        with mock.patch.object(configure, "detect_network", return_value=("", "")):
            with mock.patch("builtins.input", side_effect=answers):
                with mock.patch("sys.stdout", output):
                    config = configure.build_config(args, {})
        self.assertEqual(config["password"], "visible-password")
        self.assertIn("visible-password", output.getvalue())
        self.assertIn("login.cqu.edu.cn", config["portal"]["login_url"])


if __name__ == "__main__":
    unittest.main()
