"""Publish the daemon's scoped connectivity result for the unprivileged tray."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from pathlib import Path

from campus_autologin import Config, log


class DesktopStatusWriter:
    def __init__(self, config: Config, path: Path = Path("/run/campus-autologin/desktop-status.json")) -> None:
        self.path = path
        self.online_ttl = max(90, config.online_interval + config.timeout * len(config.connectivity_checks) + 30)
        self.offline_ttl = max(90, config.offline_interval + 30)
        self.failed = False

    def __call__(self, network: str, internet: str, interface: str) -> None:
        try:
            index = socket.if_nametoindex(interface) if interface else 0
        except OSError:
            index = 0
        now = int(time.time())
        record = {
            "schema_version": 1,
            "updated_at": now,
            "expires_at": now + (self.online_ttl if internet == "online" else self.offline_ttl),
            "network_state": network,
            "internet_state": internet,
            "interface_index": index,
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".desktop-status-", delete=False) as file:
                temporary = Path(file.name)
                json.dump(record, file)
                file.flush()
                os.fchmod(file.fileno(), 0o644)
            os.replace(temporary, self.path)
            self.failed = False
        except OSError:
            if not self.failed:
                log("desktop status could not be published")
                self.failed = True
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
