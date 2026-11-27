from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

try:
    from campus_desktop import gui
except ModuleNotFoundError as exc:
    if exc.name not in ("PySide6", "PyQt5"):
        raise
    gui = None


@unittest.skipIf(gui is None, "Qt is not installed")
class DesktopGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.environment = mock.patch.dict(os.environ, {
            "QT_QPA_PLATFORM": "offscreen",
            "XDG_RUNTIME_DIR": cls.scratch.name,
            "XDG_STATE_HOME": cls.scratch.name,
            "XDG_CONFIG_HOME": cls.scratch.name,
            "APPDATA": cls.scratch.name,
            "LOCALAPPDATA": cls.scratch.name,
        })
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        cls.app = gui.QApplication.instance() or gui.QApplication([])

    def test_mixed_services_can_be_explicitly_disabled(self) -> None:
        state = gui.DesktopStatus(campus_service="active", campus_enabled=True,
                                  remote_service="inactive", remote_enabled=False)
        parent = gui.QMainWindow()
        dialog = gui.SettingsDialog(state, parent)
        self.assertEqual(dialog.master_switch.checkState(), gui.Qt.PartiallyChecked)
        dialog.master_switch.setCheckState(gui.Qt.Unchecked)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Yes))
            worker = stack.enter_context(mock.patch.object(gui, "ControlWorker"))
            dialog.apply_change()
            worker.assert_called_once_with("master", False, parent=dialog)

    def test_unknown_or_unrelated_network_is_not_reported_online(self) -> None:
        for network, internet in (("other", "online"), ("connected", "unknown"), ("unknown", "online")):
            with self.subTest(network=network, internet=internet):
                status = gui.DesktopStatus(network_state=network, internet_state=internet, campus_service="active")
                self.assertEqual(status.overall, "attention")

    def test_expired_daemon_status_does_not_mark_target_connected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            target.write_text(json.dumps({
                "schema_version": 1, "network_state": "connected", "internet_state": "online",
                "interface_index": 0, "updated_at": 100, "expires_at": 200,
            }), encoding="utf-8")
            old = gui.DesktopStatus(campus_service="active")
            current = gui.DesktopStatus(campus_service="active")
            with mock.patch.object(gui.time, "time", return_value=201):
                gui.linux_target_status(old, target)
            with mock.patch.object(gui.time, "time", return_value=150):
                gui.linux_target_status(current, target)
            self.assertEqual(old.overall, "attention")
            self.assertEqual(current.overall, "online")

    def test_account_form_preserves_saved_portal_by_default(self) -> None:
        parent = gui.QMainWindow()
        dialog = gui.AccountDialog(parent)
        self.assertEqual(dialog.school_type.currentData(), "keep")
        self.assertTrue(dialog.login_url.isHidden())


if __name__ == "__main__":
    unittest.main()
