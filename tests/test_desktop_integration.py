import configparser
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from campus_desktop.status import DesktopStatusWriter

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux desktop installation")
class DesktopIntegrationTests(unittest.TestCase):
    def test_installed_menu_command_works_outside_source_tree(self):
        with tempfile.TemporaryDirectory(prefix="campus desktop ") as directory:
            prefix = Path(directory)
            script = 'source ./install.sh; PROGRAM_DIR="$1/lib/campus-autologin"; install_desktop "$1"'
            subprocess.run(["bash", "-c", script, "test", directory], cwd=ROOT, check=True)
            menu = configparser.ConfigParser(interpolation=None)
            menu.read(prefix / "share/applications/campus-network-assistant.desktop", encoding="utf-8")
            command = menu["Desktop Entry"]["Exec"]
            env = {**os.environ, "PATH": str(prefix / "bin") + os.pathsep + os.defpath, "PYTHONPATH": ""}
            result = subprocess.run([command, "--version"], cwd=prefix, env=env,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), (ROOT / "VERSION").read_text().strip())
            self.assertTrue((prefix / "lib/campus-autologin/campus_desktop/gui.py").is_file())

    def test_status_writer_publishes_only_scoped_nonsecret_state(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "desktop-status.json"
            config = SimpleNamespace(online_interval=30, offline_interval=10, timeout=5,
                                     connectivity_checks=[1, 2], username="private-user")
            writer = DesktopStatusWriter(config, snapshot)
            with mock.patch("socket.if_nametoindex", return_value=8):
                writer("connected", "offline", "private-interface")
            data = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertEqual(set(data), {"interface_index", "network_state", "internet_state",
                                         "schema_version", "updated_at", "expires_at"})
            self.assertEqual((data["interface_index"], data["internet_state"]), (8, "offline"))
            self.assertGreater(data["expires_at"], data["updated_at"])
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(snapshot.parent.iterdir()), [snapshot])
