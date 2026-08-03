from __future__ import annotations

import json
import unittest

from campus_desktop import updater


class FakeResponse:
    def __init__(self, payload: object, etag: str = '"desktop-etag"') -> None:
        self.body = json.dumps(payload).encode("utf-8")
        self.headers = {"ETag": etag}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.body


class FakeOpener:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.requests: list[object] = []

    def open(self, request: object, timeout: int) -> FakeResponse:
        self.requests.append((request, timeout))
        return FakeResponse(self.payload)


def release(tag: str, *, draft: bool = False, prerelease: bool = False) -> dict[str, object]:
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/fan693/campus-network-autologin/releases/tag/{tag}",
        "published_at": "2026-08-03T12:00:00Z",
        "draft": draft,
        "prerelease": prerelease,
    }


class DesktopReleaseChannelTests(unittest.TestCase):
    def test_app_channel_ignores_higher_script_versions(self) -> None:
        opener = FakeOpener(
            [
                release("v4.2.0"),
                release("app-v1.1.0"),
                release("app-v1.2.0", draft=True),
                release("app-v1.0.1"),
            ]
        )
        client = updater.GitHubReleaseClient(
            updater.Version.parse("1.0.0"),
            opener=opener,
        )
        result = client.fetch_latest()
        self.assertEqual(result.state, "ok")
        self.assertIsNotNone(result.release)
        self.assertEqual(str(result.release.version), "1.1.0")
        self.assertEqual(result.release.tag_name, "app-v1.1.0")
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, updater.RELEASES_API)
        self.assertEqual(timeout, updater.CONNECT_TIMEOUT)

    def test_release_list_without_app_tag_is_rejected(self) -> None:
        client = updater.GitHubReleaseClient(
            updater.Version.parse("1.0.0"),
            opener=FakeOpener([release("v4.2.0")]),
        )
        with self.assertRaises(updater.UpdateInvalidResponse):
            client.fetch_latest()

    def test_app_release_page_must_remain_in_official_repository(self) -> None:
        item = release("app-v1.1.0")
        item["html_url"] = "https://example.com/app-v1.1.0"
        client = updater.GitHubReleaseClient(
            updater.Version.parse("1.0.0"),
            opener=FakeOpener([item]),
        )
        with self.assertRaises(updater.UpdateInvalidResponse):
            client.fetch_latest()


if __name__ == "__main__":
    unittest.main()
