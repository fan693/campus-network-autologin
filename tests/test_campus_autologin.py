from __future__ import annotations

import io
import json
import socket
import struct
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from contextlib import ExitStack
from dataclasses import replace
from http.client import IncompleteRead
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
    def test_named_network_resolves_its_own_interface_on_both_platforms(self) -> None:
        config = replace(make_config({"type": "drcom", "login_url": "https://portal.test"}),
                         network_name="Campus:WiFi", interface="")
        with mock.patch.object(app.platform, "system", return_value="Linux"):
            with mock.patch.object(app, "run_command", return_value=r"Home:eth0" + "\n" + r"Campus\:WiFi:wlan0"):
                self.assertEqual(app.select_network(config), app.NetworkSelection("connected", "wlan0"))
        profiles = [{"Name": "Home", "InterfaceAlias": "Ethernet"}, {"Name": "Campus:WiFi", "InterfaceAlias": "Wi-Fi"}]
        with mock.patch.object(app.platform, "system", return_value="Windows"):
            with mock.patch.object(app, "windows_connection_profiles", return_value=profiles):
                self.assertEqual(app.select_network(config), app.NetworkSelection("connected", "Wi-Fi"))
            with mock.patch.object(app, "run_command", return_value='{"IPAddress":"192.0.2.2","InterfaceIndex":7}'):
                self.assertEqual(app.interface_binding("Wi-Fi", 5), app.InterfaceBinding("Wi-Fi", 7, "192.0.2.2"))

    def test_selected_interface_without_ipv4_never_uses_another_address(self) -> None:
        config = make_config({"type": "drcom", "login_url": "https://portal.test"})
        with ExitStack() as stack:
            system = stack.enter_context(mock.patch.object(app.platform, "system", return_value="Linux"))
            stack.enter_context(mock.patch.object(app, "linux_ipv4", return_value=None))
            stack.enter_context(mock.patch.object(socket, "if_nametoindex", return_value=2))
            stack.enter_context(mock.patch.object(app, "run_command", return_value=""))
            default_socket = stack.enter_context(mock.patch.object(socket, "socket"))
            self.assertIsNone(app.get_ipv4(config))
            system.return_value = "Windows"
            self.assertIsNone(app.get_ipv4(config))
            default_socket.assert_not_called()

    def test_old_linux_kernel_without_device_binding_rejects_a_different_route(self) -> None:
        binding = app.InterfaceBinding("campus0", 7, "192.0.2.8")
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", 80))
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(app.platform, "system", return_value="Linux"))
            stack.enter_context(mock.patch.object(socket, "SO_BINDTODEVICE", 25, create=True))
            stack.enter_context(mock.patch.object(socket, "getaddrinfo", return_value=[address]))
            tcp = stack.enter_context(mock.patch.object(socket, "socket")).return_value
            tcp.setsockopt.side_effect = [None, PermissionError()]
            route = stack.enter_context(mock.patch.object(app, "run_command", return_value='[{"dev":"other0"}]'))
            with self.assertRaisesRegex(OSError, "selected interface"):
                binding.connect(("probe.test", 80), 5)
            tcp.connect.assert_not_called()
            tcp.close.assert_called_once()
            self.assertIn(binding.ipv4, route.call_args.args[0])

    def test_bound_http_and_https_connect_via_the_selected_interface(self) -> None:
        binding = app.InterfaceBinding("campus0", 7, "192.0.2.8")
        for system in ("Linux", "Windows"):
            for scheme in ("http", "https"):
                with self.subTest(system=system, scheme=scheme), ExitStack() as stack:
                    stack.enter_context(mock.patch.object(app.platform, "system", return_value=system))
                    stack.enter_context(mock.patch.object(socket, "SO_BINDTODEVICE", 25, create=True))
                    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", 443))]
                    stack.enter_context(mock.patch.object(socket, "getaddrinfo", return_value=addresses))
                    tcp = stack.enter_context(mock.patch.object(socket, "socket")).return_value
                    tcp.makefile.return_value = io.BytesIO(b"HTTP/1.1 204 No Content\r\n\r\n")
                    tls = stack.enter_context(mock.patch.object(app.ssl.SSLContext, "wrap_socket", return_value=tcp))
                    opener = app.build_opener(follow_redirects=False, binding=binding)
                    with opener.open(scheme + "://portal.test/check", timeout=5) as response:
                        self.assertEqual(response.status, 204)
                    option = getattr(socket, "IP_UNICAST_IF", 31 if system == "Windows" else 50)
                    tcp.setsockopt.assert_any_call(socket.IPPROTO_IP, option, struct.pack("!I", 7))
                    tcp.bind.assert_called_once_with(("192.0.2.8", 0))
                    tcp.connect.assert_called_once_with(("192.0.2.20", 443))
                    if scheme == "https":
                        self.assertEqual(tls.call_args.kwargs["server_hostname"], "portal.test")

    def test_recovery_skips_other_profiles_and_failed_observation(self) -> None:
        config = make_config({"type": "drcom", "login_url": "https://portal.test"})
        for active in ("Home", None):
            with self.subTest(active=active), mock.patch.object(app.platform, "system", return_value="Linux"):
                network = mock.Mock()
                network.command.return_value = "wifi"
                network.active_connection.return_value = active
                self.assertFalse(app.recover_linux_network(config, network))
                self.assertEqual(network.command.call_count, 1)

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
        active_path = "/org/freedesktop/NetworkManager/ActiveConnection/3"
        outputs = [
            "wifi", "Campus-WiFi", "profile-uuid", active_path, "", "",
            "Campus-WiFi", "", "", "30 (disconnected)", "activated",
        ]
        with mock.patch.object(app.platform, "system", return_value="Linux"):
            with mock.patch.object(app, "run_command", side_effect=outputs) as run:
                with mock.patch.object(app, "interruptible_sleep"):
                    with mock.patch.object(app, "network_matches", return_value=True):
                        with mock.patch.object(app, "get_ipv4", return_value="10.0.0.2"):
                            self.assertTrue(app.recover_linux_network(config))

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["resolvectl", "flush-caches"], commands)
        self.assertIn(
            ["nmcli", "connection", "down", "apath", active_path], commands
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
    def test_portal_redirect_limits_headers_to_the_configured_origin(self) -> None:
        request = urllib.request.Request(
            "https://campus.example:443/login", data=b"example=1",
            headers={"X-Profile": "test", "Accept": "application/json", "User-Agent": "custom"},
        )
        handler = app.PortalRedirect()

        same = handler.redirect_request(request, None, 302, "Found", {}, "https://campus.example/result")
        self.assertEqual(same.get_header("X-profile"), "test")
        self.assertEqual(same.get_header("User-agent"), "custom")
        for url in ("https://status.example/result", "https://campus.example:444/result"):
            with self.subTest(url=url):
                crossed = handler.redirect_request(request, None, 302, "Found", {}, url)
                self.assertIsNone(crossed.data)
                self.assertEqual(dict(crossed.header_items()), {"User-agent": app.USER_AGENT, "Accept": "*/*"})
        self.assertIsNone(handler.redirect_request(request, None, 302, "Found", {}, "http://campus.example/result"))

    def test_incomplete_responses_are_retryable_and_portal_bodies_are_not_logged(self) -> None:
        for portal in (
            {"type": "drcom", "login_url": "https://portal.test"},
            {"type": "srun", "base_url": "https://portal.test"},
            {"type": "generic", "login_url": "https://portal.test", "success_contains": ["OK"]},
        ):
            with self.subTest(portal=portal["type"]), mock.patch.object(app, "log") as log:
                config = make_config(portal)
                interrupted = mock.Mock()
                interrupted.open.side_effect = IncompleteRead(b"", 2)
                self.assertFalse(app.internet_online(interrupted, config))
                self.assertFalse(app.portal_login(interrupted, config, "192.0.2.2", ""))
                self.assertFalse(app.portal_login(SequenceOpener([FakeResponse("private form data")]), config, "192.0.2.2", ""))
                self.assertNotIn("private form data", str(log.call_args_list))
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
        self.selection = app.NetworkSelection("connected", "wlan0")
        self.binding = app.InterfaceBinding("wlan0", 4, "10.0.0.2")

    def tearDown(self) -> None:
        app.RUNNING = True

    def test_if_network_changes_after_probe_the_portal_is_not_contacted(self) -> None:
        config = make_config({"type": "drcom", "login_url": "https://portal.test"})
        other = app.NetworkSelection("other", "wlan0")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(app, "select_network", side_effect=[self.selection, other]))
            stack.enter_context(mock.patch.object(app, "interface_binding", return_value=self.binding))
            opening = stack.enter_context(mock.patch.object(app, "build_opener"))
            stack.enter_context(mock.patch.object(app, "internet_online", return_value=False))
            login = stack.enter_context(mock.patch.object(app, "portal_login"))
            status = mock.Mock()
            self.assertEqual(app.run(config, once=True, report_status=status), 2)
            self.assertEqual(opening.call_args_list, [
                mock.call(follow_redirects=False, binding=self.binding),
                mock.call(follow_redirects=True, binding=self.binding),
            ])
            status.assert_called_with("other", "unknown", "wlan0")
            login.assert_not_called()

    def test_other_network_and_telemetry_failure_never_trigger_recovery(self) -> None:
        config = make_config({"type": "drcom", "login_url": "https://portal.test"})
        for state in ("other", "unknown"):
            with self.subTest(state=state), ExitStack() as stack:
                app.RUNNING = True
                stack.enter_context(mock.patch.object(app.platform, "system", return_value="Linux"))
                stack.enter_context(mock.patch.object(app, "select_network", return_value=app.NetworkSelection(state, "wlan0")))
                stack.enter_context(mock.patch.object(app.NetworkRecoveryState, "observe_offline", return_value=True))
                stack.enter_context(mock.patch.object(app, "interruptible_sleep", side_effect=lambda _: app.stop_handler(0, None)))
                recover = stack.enter_context(mock.patch.object(app, "recover_linux_network"))
                self.assertEqual(app.run(config), 0)
                recover.assert_not_called()

    def test_once_does_not_login_when_network_does_not_match(self) -> None:
        config = make_config({"type": "drcom", "login_url": "https://portal.example/login"})
        with mock.patch.object(app, "build_opener"):
            with mock.patch.object(app, "select_network", return_value=app.NetworkSelection("other", "wlan0")):
                with mock.patch.object(app, "portal_login") as login:
                    self.assertEqual(app.run(config, once=True), 2)
        login.assert_not_called()

    def test_once_returns_failure_when_portal_rejects_login(self) -> None:
        config = make_config({"type": "drcom", "login_url": "https://portal.example/login"})
        with mock.patch.object(app, "build_opener"):
            with mock.patch.object(app, "select_network", return_value=self.selection):
                with mock.patch.object(app, "interface_binding", return_value=self.binding):
                    with mock.patch.object(app, "internet_online", return_value=False):
                        with mock.patch.object(app, "get_ipv6", return_value=""):
                            with mock.patch.object(app, "portal_login", return_value=False):
                                self.assertEqual(app.run(config, once=True), 4)

    def test_stop_handler_ends_monitor_loop(self) -> None:
        config = make_config({"type": "drcom", "login_url": "https://portal.example/login"})
        def stop(_seconds: int) -> None:
            app.stop_handler(15, None)
        with mock.patch.object(app, "build_opener"):
            with mock.patch.object(app, "select_network", return_value=self.selection):
                with mock.patch.object(app, "interface_binding", return_value=self.binding):
                    with mock.patch.object(app, "internet_online", return_value=True):
                        with mock.patch.object(app, "interruptible_sleep", side_effect=stop):
                            self.assertEqual(app.run(config), 0)


if __name__ == "__main__":
    unittest.main()
