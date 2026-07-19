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


if __name__ == "__main__":
    unittest.main()
