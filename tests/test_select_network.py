from __future__ import annotations

import io
import unittest
from unittest import mock

import select_network


class NetworkDiscoveryTests(unittest.TestCase):
    def test_only_connected_physical_networks_are_returned(self) -> None:
        responses = {
            ("nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"): (
                "eno1:ethernet:connected\n"
                "wlan0:wifi:disconnected\n"
                "docker0:bridge:connected\n"
            ),
            ("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", "eno1"): (
                "有线连接 1"
            ),
        }

        candidates = select_network.active_candidates(
            lambda command: responses[tuple(command)]
        )

        self.assertEqual(
            candidates,
            [select_network.NetworkCandidate("有线连接 1", "eno1", "ethernet")],
        )

    def test_command_uses_utf8_locale(self) -> None:
        completed = mock.Mock(returncode=0, stdout="有线连接 1\n")
        with mock.patch.object(
            select_network.subprocess, "run", return_value=completed
        ) as run:
            output = select_network.command_output(["nmcli", "general"])

        self.assertEqual(output, "有线连接 1")
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C.UTF-8")


class NetworkSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wired = select_network.NetworkCandidate("Campus LAN", "eno1", "ethernet")
        self.wifi = select_network.NetworkCandidate("Campus Wi-Fi", "wlan0", "wifi")

    def test_single_candidate_is_automatic(self) -> None:
        selected = select_network.choose_candidate(
            [self.wired],
            "eno1",
            interactive=True,
            input_stream=io.StringIO(),
            output_stream=io.StringIO(),
        )
        self.assertEqual(selected, self.wired)

    def test_enter_accepts_default_route_recommendation(self) -> None:
        selected = select_network.choose_candidate(
            [self.wifi, self.wired],
            "eno1",
            interactive=True,
            input_stream=io.StringIO("\n"),
            output_stream=io.StringIO(),
        )
        self.assertEqual(selected, self.wired)

    def test_user_can_choose_non_default_connection(self) -> None:
        selected = select_network.choose_candidate(
            [self.wifi, self.wired],
            "eno1",
            interactive=True,
            input_stream=io.StringIO("1\n"),
            output_stream=io.StringIO(),
        )
        self.assertEqual(selected, self.wifi)

    def test_noninteractive_mode_uses_default_route(self) -> None:
        selected = select_network.choose_candidate(
            [self.wifi, self.wired],
            "eno1",
            interactive=False,
            output_stream=io.StringIO(),
        )
        self.assertEqual(selected, self.wired)

    def test_noninteractive_mode_without_default_route_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "无法确定默认网络"):
            select_network.choose_candidate(
                [self.wifi, self.wired],
                "",
                interactive=False,
                output_stream=io.StringIO(),
            )


if __name__ == "__main__":
    unittest.main()
