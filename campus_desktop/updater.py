"""Privacy-preserving GitHub Release checks for the desktop companion."""

from __future__ import annotations

import contextlib
import json
import os
import re
import ssl
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # Unix
    msvcrt = None


REPOSITORY = "fan693/campus-network-autologin"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=20"
RELEASE_PATH_PREFIX = f"/{REPOSITORY}/releases/tag/"
APP_TAG_PREFIX = "app-v"
USER_AGENT_PRODUCT = "campus-network-assistant"
AUTO_CHECK_INTERVAL = timedelta(hours=24)
CONNECT_TIMEOUT = 5
MAX_RESPONSE_BYTES = 1_048_576
VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]{0,5})\."
    r"(0|[1-9][0-9]{0,5})\."
    r"(0|[1-9][0-9]{0,5})$"
)


class UpdateError(Exception):
    """Base class for update errors with a stable public code."""

    code = "update_invalid_response"


class UpdateUnavailable(UpdateError):
    code = "update_unreachable"


class UpdateInvalidResponse(UpdateError):
    code = "update_invalid_response"


class UpdateRateLimited(UpdateError):
    code = "update_unreachable"

    def __init__(self, reset_at: Optional[datetime]) -> None:
        super().__init__(self.code)
        self.reset_at = reset_at


class UpdateBusy(UpdateError):
    code = "operation_conflict"


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str, *, allow_v_prefix: bool = False) -> "Version":
        text = value.strip()
        if allow_v_prefix and text.startswith("v"):
            text = text[1:]
        match = VERSION_PATTERN.fullmatch(text)
        if match is None:
            raise ValueError("version must use MAJOR.MINOR.PATCH")
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class ReleaseInfo:
    version: Version
    tag_name: str
    html_url: str
    published_at: Optional[str]


@dataclass(frozen=True)
class FetchResult:
    state: str
    release: Optional[ReleaseInfo]
    etag: Optional[str]


@dataclass
class UpdateCache:
    schema_version: int = 1
    last_auto_attempt_at: Optional[str] = None
    last_success_at: Optional[str] = None
    etag: Optional[str] = None
    latest_version: Optional[str] = None
    release_page: Optional[str] = None
    ignored_version: Optional[str] = None
    notified_version: Optional[str] = None
    snoozed_version: Optional[str] = None
    snoozed_until: Optional[str] = None
    rate_limit_until: Optional[str] = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "UpdateCache":
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported cache schema")
        allowed = {item.name for item in fields(cls)}
        values: dict[str, Any] = {"schema_version": 1}
        for key in allowed - {"schema_version"}:
            value = raw.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"invalid cache field: {key}")
            values[key] = value
        return cls(**values)


@dataclass
class UpdatePreferences:
    schema_version: int = 1
    update_check_enabled: bool = False
    consent_recorded: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "UpdatePreferences":
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported preferences schema")
        enabled = raw.get("update_check_enabled")
        consent = raw.get("consent_recorded")
        if not isinstance(enabled, bool) or not isinstance(consent, bool):
            raise ValueError("invalid update preferences")
        return cls(update_check_enabled=enabled, consent_recorded=consent)


@dataclass(frozen=True)
class CheckResult:
    status: str
    current_version: str
    latest_version: Optional[str] = None
    release_page: Optional[str] = None
    notification_required: bool = False
    error_code: Optional[str] = None
    next_allowed_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def default_state_dir(
    *,
    system: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    current = system or ("Windows" if os.name == "nt" else "Linux")
    variables = os.environ if environ is None else environ
    if current == "Windows":
        base = variables.get("LOCALAPPDATA")
        if not base:
            base = str((home or Path.home()) / "AppData" / "Local")
        return Path(base) / "CampusNetworkAssistant"
    base = variables.get("XDG_STATE_HOME")
    return Path(base) / "campus-network-assistant" if base else (
        (home or Path.home()) / ".local" / "state" / "campus-network-assistant"
    )


def default_preferences_path(
    *,
    system: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    current = system or ("Windows" if os.name == "nt" else "Linux")
    variables = os.environ if environ is None else environ
    if current == "Windows":
        return default_state_dir(system=current, environ=variables, home=home) / "preferences.json"
    base = variables.get("XDG_CONFIG_HOME")
    root = Path(base) if base else (home or Path.home()) / ".config"
    return root / "campus-network-assistant" / "preferences.json"


def local_version_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "VERSION"
    return Path(__file__).resolve().parents[1] / "VERSION"


def load_local_version(path: Optional[Path] = None) -> Version:
    source = path or local_version_path()
    try:
        return Version.parse(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError("local VERSION file is unavailable") from exc


def validate_release_page(url: str, tag_name: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if (
        parts.scheme != "https"
        or parts.hostname != "github.com"
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.query
        or parts.fragment
    ):
        return False
    return parts.path == f"{RELEASE_PATH_PREFIX}{tag_name}"


class NoCrossOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        current = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if (
            target.scheme == "https"
            and target.hostname == current.hostname == "api.github.com"
            and target.port in (None, 443)
            and target.username is None
            and target.password is None
        ):
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        return None


class GitHubReleaseClient:
    def __init__(
        self,
        current_version: Version,
        *,
        opener: Optional[urllib.request.OpenerDirector] = None,
    ) -> None:
        self.current_version = current_version
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            NoCrossOriginRedirect(),
        )

    def fetch_latest(self, etag: Optional[str] = None) -> FetchResult:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{USER_AGENT_PRODUCT}/{self.current_version}",
        }
        if etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(RELEASES_API, headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=CONNECT_TIMEOUT) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise UpdateInvalidResponse("response is too large")
                response_etag = response.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return FetchResult("not_modified", None, etag)
            if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                reset_at = _rate_limit_timestamp(exc.headers.get("X-RateLimit-Reset"))
                raise UpdateRateLimited(reset_at) from exc
            raise UpdateUnavailable("GitHub Release request failed") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpdateUnavailable("GitHub Release request failed") from exc

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateInvalidResponse("GitHub returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise UpdateInvalidResponse("GitHub response is not a release list")
        releases: list[ReleaseInfo] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get("draft") is not False or item.get("prerelease") is not False:
                continue
            tag_name = item.get("tag_name")
            html_url = item.get("html_url")
            published_at = item.get("published_at")
            if not isinstance(tag_name, str) or not tag_name.startswith(APP_TAG_PREFIX):
                continue
            if not isinstance(html_url, str) or not validate_release_page(html_url, tag_name):
                raise UpdateInvalidResponse("Release page is outside the trusted repository")
            if published_at is not None and not isinstance(published_at, str):
                raise UpdateInvalidResponse("Release publication time is invalid")
            try:
                version = Version.parse(tag_name[len(APP_TAG_PREFIX):])
            except ValueError:
                continue
            releases.append(ReleaseInfo(version, tag_name, html_url, published_at))
        if not releases:
            raise UpdateInvalidResponse("no stable desktop application Release was found")
        latest = max(releases, key=lambda release: release.version)
        return FetchResult(
            "ok",
            latest,
            response_etag,
        )


def _rate_limit_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value or not value.isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _read_json(path: Path) -> Mapping[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("JSON root must be an object")
    return raw


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class UpdateStore:
    def __init__(
        self,
        state_dir: Optional[Path] = None,
        preferences_path: Optional[Path] = None,
    ) -> None:
        self.state_dir = state_dir or default_state_dir()
        self.cache_path = self.state_dir / "update.json"
        self.lock_path = self.state_dir / "update.lock"
        self.preferences_path = preferences_path or default_preferences_path()

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.state_dir.chmod(0o700)
        handle = self.lock_path.open("a+b")
        if os.name != "nt":
            self.lock_path.chmod(0o600)
        try:
            self._acquire_lock(handle)
            yield
        finally:
            self._release_lock(handle)
            handle.close()

    @staticmethod
    def _acquire_lock(handle: Any) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            if msvcrt is not None:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
        except OSError as exc:
            raise UpdateBusy("another update check is running") from exc
        raise UpdateBusy("file locking is unavailable")

    @staticmethod
    def _release_lock(handle: Any) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

    def load_cache(self) -> tuple[UpdateCache, Optional[datetime]]:
        try:
            return UpdateCache.from_mapping(_read_json(self.cache_path)), None
        except FileNotFoundError:
            return UpdateCache(), None
        except (OSError, ValueError, json.JSONDecodeError):
            try:
                fallback = datetime.fromtimestamp(
                    self.cache_path.stat().st_mtime, timezone.utc
                )
            except OSError:
                fallback = None
            return UpdateCache(), fallback

    def save_cache(self, cache: UpdateCache) -> None:
        _atomic_write_json(self.cache_path, asdict(cache))

    def load_preferences(self) -> UpdatePreferences:
        try:
            return UpdatePreferences.from_mapping(_read_json(self.preferences_path))
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            return UpdatePreferences()

    def save_preferences(self, preferences: UpdatePreferences) -> None:
        _atomic_write_json(self.preferences_path, asdict(preferences))

    def set_update_check_enabled(self, enabled: bool) -> UpdatePreferences:
        preferences = UpdatePreferences(
            update_check_enabled=enabled,
            consent_recorded=True,
        )
        self.save_preferences(preferences)
        return preferences


class UpdateChecker:
    def __init__(
        self,
        current_version: Version,
        *,
        store: Optional[UpdateStore] = None,
        client: Optional[GitHubReleaseClient] = None,
    ) -> None:
        self.current_version = current_version
        self.store = store or UpdateStore()
        self.client = client or GitHubReleaseClient(current_version)

    def check(self, *, automatic: bool = False, now: Optional[datetime] = None) -> CheckResult:
        current_time = (now or utc_now()).astimezone(timezone.utc)
        try:
            with self.store.lock():
                return self._check_locked(automatic=automatic, now=current_time)
        except UpdateBusy as exc:
            return self._error_result(exc.code)

    def _check_locked(self, *, automatic: bool, now: datetime) -> CheckResult:
        preferences = self.store.load_preferences()
        cache, corrupted_at = self.store.load_cache()

        if automatic and not (
            preferences.consent_recorded and preferences.update_check_enabled
        ):
            return CheckResult("skipped_disabled", str(self.current_version))

        if automatic:
            next_allowed = self._next_automatic_check(cache, corrupted_at)
            if next_allowed is not None and now < next_allowed:
                return CheckResult(
                    "skipped_interval",
                    str(self.current_version),
                    next_allowed_at=format_timestamp(next_allowed),
                )
            cache.last_auto_attempt_at = format_timestamp(now)
            self.store.save_cache(cache)

        try:
            fetched = self.client.fetch_latest(cache.etag)
        except UpdateRateLimited as exc:
            lower_bound = now + AUTO_CHECK_INTERVAL if automatic else now
            reset_at = exc.reset_at or lower_bound
            cache.rate_limit_until = format_timestamp(max(reset_at, lower_bound))
            self.store.save_cache(cache)
            return self._error_result(exc.code, cache.rate_limit_until)
        except UpdateError as exc:
            self.store.save_cache(cache)
            return self._error_result(exc.code)

        cache.last_success_at = format_timestamp(now)
        cache.rate_limit_until = None
        if fetched.etag is not None:
            cache.etag = fetched.etag
        if fetched.release is not None:
            cache.latest_version = str(fetched.release.version)
            cache.release_page = fetched.release.html_url
        self.store.save_cache(cache)
        return self._result_from_cache(cache, now)

    def _next_automatic_check(
        self,
        cache: UpdateCache,
        corrupted_at: Optional[datetime],
    ) -> Optional[datetime]:
        candidates = [
            value
            for value in (
                parse_timestamp(cache.last_auto_attempt_at),
                parse_timestamp(cache.rate_limit_until),
                corrupted_at,
            )
            if value is not None
        ]
        if not candidates:
            return None
        last_attempt = parse_timestamp(cache.last_auto_attempt_at)
        interval_end = last_attempt + AUTO_CHECK_INTERVAL if last_attempt else None
        if interval_end is not None:
            candidates.append(interval_end)
        if corrupted_at is not None:
            candidates.append(corrupted_at + AUTO_CHECK_INTERVAL)
        return max(candidates)

    def _result_from_cache(self, cache: UpdateCache, now: datetime) -> CheckResult:
        if not cache.latest_version or not cache.release_page:
            return CheckResult("up_to_date", str(self.current_version))
        try:
            latest = Version.parse(cache.latest_version)
        except ValueError:
            return self._error_result("update_invalid_response")
        if latest <= self.current_version:
            return CheckResult(
                "up_to_date",
                str(self.current_version),
                str(latest),
                cache.release_page,
            )
        if cache.ignored_version == str(latest):
            status = "ignored"
        elif (
            cache.snoozed_version == str(latest)
            and (parse_timestamp(cache.snoozed_until) or now) > now
        ):
            status = "snoozed"
        else:
            status = "update_available"
        return CheckResult(
            status,
            str(self.current_version),
            str(latest),
            cache.release_page,
            notification_required=self.should_notify(cache, latest, now),
        )

    @staticmethod
    def should_notify(cache: UpdateCache, latest: Version, now: datetime) -> bool:
        value = str(latest)
        if cache.ignored_version == value:
            return False
        if cache.snoozed_version == value:
            until = parse_timestamp(cache.snoozed_until)
            return until is not None and now >= until
        return cache.notified_version != value

    def mark_notified(self, version: Version) -> None:
        with self.store.lock():
            cache, _ = self.store.load_cache()
            value = str(version)
            cache.notified_version = value
            if cache.snoozed_version == value:
                cache.snoozed_version = None
                cache.snoozed_until = None
            self.store.save_cache(cache)

    def ignore(self, version: Version) -> None:
        with self.store.lock():
            cache, _ = self.store.load_cache()
            cache.ignored_version = str(version)
            cache.snoozed_version = None
            cache.snoozed_until = None
            self.store.save_cache(cache)

    def snooze(
        self,
        version: Version,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        current_time = (now or utc_now()).astimezone(timezone.utc)
        with self.store.lock():
            cache, _ = self.store.load_cache()
            cache.notified_version = str(version)
            cache.snoozed_version = str(version)
            cache.snoozed_until = format_timestamp(current_time + AUTO_CHECK_INTERVAL)
            self.store.save_cache(cache)

    def _error_result(
        self,
        code: str,
        next_allowed_at: Optional[str] = None,
    ) -> CheckResult:
        return CheckResult(
            "error",
            str(self.current_version),
            error_code=code,
            next_allowed_at=next_allowed_at,
        )
