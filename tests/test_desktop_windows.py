from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from campus_desktop import windows


class WindowsControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.json"
        self.directory = self.root / ".desktop-transaction"
        self.original = {
            "version": 4, "username": "student", "password": "original-fixture",
            "network_name": "Campus", "interface": "Wi-Fi",
            "portal": {"type": "drcom", "login_url": "https://portal.example.edu/login",
                       "parameters": {"custom": "yes"}},
        }
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        self.profile = {
            "username": "other", "password": "replacement-fixture", "school_type": "keep",
            "login_url": "", "base_url": "", "account_prefix": "", "ac_id": "",
            "method": "POST", "success_contains": "",
        }
        self.states = {windows.CAMPUS_TASK: windows.TaskState(True, True)}
        self.calls = []

        def apply(name, state):
            self.calls.append((name, state))
            self.states[name] = state

        def create_private(script, request):
            self.assertEqual(script, windows.PRIVATE_DIRECTORY)
            self.assertEqual(set(request), {"path", "profile"})
            self.assertEqual(Path(request["path"]), self.directory)
            self.directory.mkdir()
            return {}

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(
            windows, "mutation", side_effect=lambda: nullcontext((self.path, self.directory))))
        stack.enter_context(mock.patch.object(windows, "snapshots", side_effect=lambda: self.states.copy()))
        self.apply = stack.enter_context(mock.patch.object(windows, "apply_task", side_effect=apply))
        self.shell = stack.enter_context(mock.patch.object(windows, "powershell", side_effect=create_private))

    def test_disabled_manual_check_never_starts_task(self):
        self.states[windows.CAMPUS_TASK] = windows.TaskState(False, False)
        with mock.patch.object(windows.core, "run", return_value=0) as run:
            self.assertEqual(windows.reauthenticate(), (True, "ok"))
            self.assertTrue(run.call_args.kwargs["once"])
        self.assertEqual(self.calls, [])

    def test_check_failure_restores_running_task(self):
        with mock.patch.object(windows.core, "run", return_value=4):
            self.assertEqual(windows.reauthenticate(), (False, "authentication_failed"))
        self.assertEqual(self.states[windows.CAMPUS_TASK], windows.TaskState(True, True))
        self.assertFalse(self.directory.exists())

    def test_config_update_preserves_site_options_and_disabled_state(self):
        self.states[windows.CAMPUS_TASK] = windows.TaskState(False, False)
        self.assertEqual(windows.replace_profile(self.profile), (True, "ok"))
        saved = json.loads(self.path.read_bytes())
        self.assertEqual(saved["portal"], self.original["portal"])
        self.assertEqual(saved["password"], "replacement-fixture")
        self.assertFalse(self.apply.called)
        self.assertFalse(self.directory.exists())

    def test_failed_restart_preserves_both_original_bytes_and_running_state(self):
        original = self.path.read_bytes()
        calls = []
        def fail_once(name, state):
            calls.append(state.running)
            if calls == [False, True]:
                raise windows.ControlError("service_operation_failed")
            self.states[name] = state
        self.apply.side_effect = fail_once
        self.assertEqual(windows.replace_profile(self.profile), (False, "service_operation_failed"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.states[windows.CAMPUS_TASK], windows.TaskState(True, True))
        self.assertFalse(self.directory.exists())

    def test_master_switch_rolls_back_mixed_state_on_remote_failure(self):
        self.states[windows.REMOTE_TASK] = windows.TaskState(True, False)
        original = self.states.copy()
        def fail_remote(name, state):
            if name == windows.REMOTE_TASK and not state.enabled:
                raise windows.ControlError("service_operation_failed")
            self.states[name] = state
        self.apply.side_effect = fail_remote
        self.assertEqual(windows.set_master_enabled(False), (False, "service_operation_failed"))
        self.assertEqual(self.states, original)

    def test_interrupted_profile_update_restores_disk_and_task(self):
        original = self.path.read_bytes()
        self.directory.mkdir()
        (self.directory / "original").write_bytes(original)
        windows.marker(self.directory, {
            "profile": True,
            "tasks": {windows.CAMPUS_TASK: {"enabled": True, "running": True}},
        })
        self.path.write_text("pending")
        windows.recover_transaction(self.directory, self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.calls, [
            (windows.CAMPUS_TASK, windows.TaskState(True, False)),
            (windows.CAMPUS_TASK, windows.TaskState(True, True)),
        ])
        self.assertFalse(self.directory.exists())

    def test_failed_acl_setup_leaves_original_in_place(self):
        self.shell.side_effect = windows.ControlError("service_operation_failed")
        with mock.patch.object(windows, "write_bytes") as write:
            self.assertFalse(windows.replace_profile(self.profile)[0])
        self.assertFalse(write.called)
        self.assertEqual(json.loads(self.path.read_bytes()), self.original)


class WindowsStatusTests(unittest.TestCase):
    def test_no_unbound_probe_for_a_different_network(self):
        config = SimpleNamespace(timeout=3, network_name="Campus")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(windows, "snapshots", return_value={}))
            stack.enter_context(mock.patch.object(windows.core, "load_config", return_value=config))
            selection = stack.enter_context(mock.patch.object(windows.core, "select_network"))
            binding = stack.enter_context(mock.patch.object(windows.core, "interface_binding", return_value=None))
            online = stack.enter_context(mock.patch.object(windows.core, "internet_online"))
            selection.return_value = SimpleNamespace(state="other", interface="Wi-Fi")
            self.assertEqual(windows.read_status()["network_state"], "other")
            binding.assert_not_called()
            selection.return_value = SimpleNamespace(state="connected", interface="Wi-Fi")
            self.assertEqual(windows.read_status()["internet_state"], "unknown")
            binding.assert_called_once_with("Wi-Fi", 3)
            online.assert_not_called()

    def test_probe_is_bound_to_selected_adapter_index(self):
        config = SimpleNamespace(timeout=3, network_name="Campus")
        binding = SimpleNamespace(index=31)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(windows, "snapshots", return_value={
                windows.CAMPUS_TASK: windows.TaskState(True, True)}))
            stack.enter_context(mock.patch.object(windows.core, "load_config", return_value=config))
            stack.enter_context(mock.patch.object(windows.core, "select_network",
                                                 return_value=SimpleNamespace(state="connected", interface="Wi-Fi")))
            stack.enter_context(mock.patch.object(windows.core, "interface_binding", return_value=binding))
            opener = stack.enter_context(mock.patch.object(windows.core, "build_opener"))
            check = stack.enter_context(mock.patch.object(windows.core, "internet_online", return_value=True))
            shell = stack.enter_context(mock.patch.object(windows, "powershell",
                                                        return_value={"kind": "wifi", "name": "Campus"}))
            status = windows.read_status()
            opener.assert_called_once_with(follow_redirects=False, binding=binding)
            check.assert_called_once_with(opener.return_value, config)
            shell.assert_called_once_with(windows.ADAPTER_DETAIL, {"index": 31})
            self.assertEqual((status["internet_state"], status["campus_enabled"], status["remote_service"]),
                             ("online", True, "missing"))

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 and NTFS")
    def test_stage_acl_is_private_before_first_credential_write(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root, "stage")
            windows.powershell(windows.PRIVATE_DIRECTORY, {"path": str(directory), "profile": True})
            script = """
$acl = Get-Acl -LiteralPath (Join-Path $request.path 'candidate')
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User
$rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
@{protected=$acl.AreAccessRulesProtected; current=($rules.Count -eq 1 -and
    $rules[0].IdentityReference.Value -eq $me.Value)} | ConvertTo-Json -Compress
"""
            self.assertEqual(windows.powershell(script, {"path": str(directory)}),
                             {"protected": True, "current": True})
            self.assertEqual((directory / "candidate").stat().st_size, 0)
