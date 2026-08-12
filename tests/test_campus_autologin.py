from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import campus_autologin as app


class FakeResponse:
    def __init__(self, body: str, status: int = 200) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class SequenceOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[object] = []

    def open(self, request: object, timeout: int) -> FakeResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def make_config(portal: dict[str, object]) -> app.Config:
    return app.Config(
        username="student01",
        password="p@ss word",
        network_name="Campus-WiFi",
        interface="wlan0",
        portal=app.validate_portal(portal),
        online_interval=30,
        offline_interval=10,
        timeout=5,
        failure_threshold=2,
        network_recovery_after=300,
        network_recovery_cooldown=900,
        connectivity_checks=(app.ConnectivityCheck("https://example.test/check", 204),),
    )


class ConfigurationTests(unittest.TestCase):
    def test_all_example_configs_are_valid(self) -> None:
        examples = Path(__file__).resolve().parents[1] / "examples"
        for path in examples.glob("*.json"):
            with self.subTest(path=path.name):
                app.load_config(path)

    def test_v3_cqu_config_is_loaded(self) -> None:
        source = {
            "student_id": "20240001",
            "password": "secret",
            "interface": "wlp1s0",
            "connection_name": "CQU-WiFi",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            config = app.load_config(path)
        self.assertEqual(config.username, "20240001")
        self.assertEqual(config.network_name, "CQU-WiFi")
        self.assertEqual(config.portal["type"], "drcom")
        self.assertIn("login.cqu.edu.cn", config.portal["login_url"])
        self.assertEqual(config.network_recovery_after, 300)
        self.assertEqual(config.network_recovery_cooldown, 900)

    def test_generic_portal_requires_success_marker(self) -> None:
        with self.assertRaisesRegex(ValueError, "success response marker"):
            app.validate_portal(
                {
                    "type": "generic",
                    "login_url": "https://portal.example/login",
                    "method": "POST",
                    "parameters": {},
                }
            )

    def test_future_config_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            value = {
                "version": app.CONFIG_VERSION + 1,
                "username": "u", "password": "p", "network_name": "n", "interface": "",
                "portal": {"type": "drcom", "login_url": "https://portal.example/login"},
            }
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported config version"):
                app.load_config(path)

    def test_unknown_legacy_config_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            value = {
                "version": 2,
                "username": "u", "password": "p", "network_name": "n", "interface": "",
                "portal": {"type": "drcom", "login_url": "https://portal.example/login"},
            }
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported config version"):
                app.load_config(path)

    def test_connectivity_policy_all_requires_all_checks(self) -> None:
        config = make_config({"type": "drcom", "login_url": "https://portal.example/login"})
        config = app.Config(**{**config.__dict__, "connectivity_policy": "all",
                              "connectivity_checks": (
                                  app.ConnectivityCheck("https://one.test", 204),
                                  app.ConnectivityCheck("https://two.test", 204),
                              )})
        opener = SequenceOpener([FakeResponse("", 204), FakeResponse("", 500)])
        self.assertFalse(app.internet_online(opener, config))

    def test_connectivity_policy_quorum_requires_strict_majority(self) -> None:
        config = make_config({"type": "drcom", "login_url": "https://portal.example/login"})
        config = app.Config(**{**config.__dict__, "connectivity_policy": "quorum",
                              "connectivity_checks": (
                                  app.ConnectivityCheck("https://one.test", 204),
                                  app.ConnectivityCheck("https://two.test", 204),
                              )})
        opener = SequenceOpener([FakeResponse("", 204), FakeResponse("", 500)])
        self.assertFalse(app.internet_online(opener, config))


class NetworkDetectionTests(unittest.TestCase):
    def test_linux_connection_name_uses_utf8_locale(self) -> None:
        completed = mock.Mock(returncode=0, stdout="有线连接 1\n")
        with mock.patch.object(app.subprocess, "run", return_value=completed) as run:
            connection = app.linux_active_connection("eno1", 5)

        self.assertEqual(connection, "有线连接 1")
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C.UTF-8")

    def test_sustained_outage_obeys_delay_and_cooldown(self) -> None:
        state = app.NetworkRecoveryState(recovery_after=300, cooldown=900)
        self.assertFalse(state.observe_offline(1000))
        self.assertFalse(state.observe_offline(1299))
        self.assertTrue(state.observe_offline(1300))
        self.assertFalse(state.observe_offline(2199))
        self.assertTrue(state.observe_offline(2200))
        state.observe_online()
        self.assertFalse(state.observe_offline(3000))

    def test_linux_recovery_refreshes_dns_and_cycles_selected_profile(self) -> None:
        config = make_config(
            {
                "type": "drcom",
                "login_url": "https://portal.example/login",
            }
        )
        outputs = ["wifi", "Campus-WiFi", "profile-uuid", "", "", "", "activated"]
        with (
            mock.patch.object(app.platform, "system", return_value="Linux"),
            mock.patch.object(app, "run_command", side_effect=outputs) as run,
            mock.patch.object(app, "interruptible_sleep"),
            mock.patch.object(app, "network_matches", return_value=True),
            mock.patch.object(app, "get_ipv4", return_value="10.0.0.2"),
        ):
            self.assertTrue(app.recover_linux_network(config))

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["resolvectl", "flush-caches"], commands)
        self.assertIn(
            ["nmcli", "connection", "down", "uuid", "profile-uuid"], commands
        )
        self.assertIn(
            [
                "nmcli",
                "connection",
                "up",
                "uuid",
                "profile-uuid",
                "ifname",
                "wlan0",
            ],
            commands,
        )


class ProtocolTests(unittest.TestCase):
    def test_drcom_login_renders_expected_fields(self) -> None:
        config = make_config(
            {
                "type": "drcom",
                "login_url": "https://portal.example/eportal/portal/login",
                "account_prefix": ",0,",
            }
        )
        opener = SequenceOpener([FakeResponse('dr1004({"result":1,"msg":"ok"})')])
        self.assertTrue(app.drcom_login(opener, config, "10.2.3.4", ""))
        request = opener.requests[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.assertEqual(query["user_account"], [",0,student01"])
        self.assertEqual(query["user_password"], ["p@ss word"])
        self.assertEqual(query["wlan_user_ip"], ["10.2.3.4"])

    def test_generic_post_templates(self) -> None:
        config = make_config(
            {
                "type": "generic",
                "login_url": "https://portal.example/login",
                "method": "POST",
                "parameters": {
                    "user": "{username}",
                    "pass": "{password}",
                    "ip": "{ipv4}",
                },
                "headers": {"X-Network": "{network_name}"},
                "success_contains": ["LOGIN_OK"],
            }
        )
        opener = SequenceOpener([FakeResponse("LOGIN_OK")])
        self.assertTrue(app.generic_login(opener, config, "10.8.0.9", ""))
        request = opener.requests[0]
        form = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(form["user"], ["student01"])
        self.assertEqual(form["pass"], ["p@ss word"])
        self.assertEqual(form["ip"], ["10.8.0.9"])
        self.assertEqual(request.headers["X-network"], "Campus-WiFi")

    def test_srun_login_uses_challenge_and_encrypted_fields(self) -> None:
        config = make_config(
            {
                "type": "srun",
                "base_url": "http://10.0.0.55",
                "ac_id": "8",
            }
        )
        token = "0123456789abcdef0123456789abcdef"
        opener = SequenceOpener(
            [
                FakeResponse(f'jsonp({{"challenge":"{token}","error":"ok"}})'),
                FakeResponse('jsonp({"error":"ok","suc_msg":"login_ok"})'),
            ]
        )
        self.assertTrue(app.srun_login(opener, config, "10.1.2.3"))
        challenge_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(opener.requests[0].full_url).query
        )
        login_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(opener.requests[1].full_url).query
        )
        self.assertEqual(challenge_query["username"], ["student01"])
        self.assertEqual(login_query["ac_id"], ["8"])
        self.assertTrue(login_query["password"][0].startswith("{MD5}"))
        self.assertTrue(login_query["info"][0].startswith("{SRBX1}"))
        self.assertRegex(login_query["chksum"][0], r"^[0-9a-f]{40}$")

    def test_srun_crypto_vector(self) -> None:
        encoded = app.srun_base64(app.srun_xencode(b"hello", b"token"), app.SRUN_ALPHABET)
        self.assertEqual(encoded, "KvJ+JR1KrGQDJwPD")


class ConnectivityTests(unittest.TestCase):
    def test_exact_204_is_online(self) -> None:
        config = make_config(
            {
                "type": "drcom",
                "login_url": "https://portal.example/login",
            }
        )
        opener = SequenceOpener([FakeResponse("", 204)])
        self.assertTrue(app.internet_online(opener, config))

    def test_redirect_is_not_online(self) -> None:
        config = make_config(
            {
                "type": "drcom",
                "login_url": "https://portal.example/login",
            }
        )
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://example.test/check", 302, "Found", {}, io.BytesIO()
        )
        self.assertFalse(app.internet_online(opener, config))


class RunLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        app.RUNNING = True

    def tearDown(self) -> None:
        app.RUNNING = True

    def test_once_does_not_login_when_network_does_not_match(self) -> None:
        config = make_config({"type": "drcom", "login_url": "https://portal.example/login"})
        with (
            mock.patch.object(app, "build_opener"),
            mock.patch.object(app, "network_matches", return_value=False),
            mock.patch.object(app, "portal_login") as login,
        ):
            self.assertEqual(app.run(config, once=True), 2)
        login.assert_not_called()

    def test_once_returns_failure_when_portal_rejects_login(self) -> None:
        config = make_config({"type": "drcom", "login_url": "https://portal.example/login"})
        with (
            mock.patch.object(app, "build_opener"),
            mock.patch.object(app, "network_matches", return_value=True),
            mock.patch.object(app, "get_ipv4", return_value="10.0.0.2"),
            mock.patch.object(app, "internet_online", return_value=False),
            mock.patch.object(app, "get_ipv6", return_value=""),
            mock.patch.object(app, "portal_login", return_value=False),
        ):
            self.assertEqual(app.run(config, once=True), 4)

    def test_stop_handler_ends_monitor_loop(self) -> None:
        config = make_config({"type": "drcom", "login_url": "https://portal.example/login"})
        def stop(_seconds: int) -> None:
            app.stop_handler(15, None)
        with (
            mock.patch.object(app, "build_opener"),
            mock.patch.object(app, "network_matches", return_value=True),
            mock.patch.object(app, "get_ipv4", return_value="10.0.0.2"),
            mock.patch.object(app, "internet_online", return_value=True),
            mock.patch.object(app, "interruptible_sleep", side_effect=stop),
        ):
            self.assertEqual(app.run(config), 0)


if __name__ == "__main__":
    unittest.main()
