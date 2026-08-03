"""Command-line entry point shared by the future desktop application."""

from __future__ import annotations

import argparse
import json
import sys

from .updater import UpdateChecker, UpdateStore, Version, load_local_version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Campus Network Assistant")
    parser.add_argument("--version", action="store_true", help="print the local version")
    parser.add_argument("--check-updates", action="store_true", help="check GitHub Releases")
    parser.add_argument(
        "--automatic",
        action="store_true",
        help="apply consent and 24-hour limits for a scheduled check",
    )
    preference = parser.add_mutually_exclusive_group()
    preference.add_argument("--enable-update-check", action="store_true")
    preference.add_argument("--disable-update-check", action="store_true")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        version = load_local_version()
    except ValueError:
        print("local VERSION file is invalid", file=sys.stderr)
        return 2

    if args.version:
        print(version)
        return 0

    store = UpdateStore()
    if args.enable_update_check or args.disable_update_check:
        enabled = bool(args.enable_update_check)
        preferences = store.set_update_check_enabled(enabled)
        output = {
            "schema_version": 1,
            "update_check_enabled": preferences.update_check_enabled,
            "consent_recorded": preferences.consent_recorded,
        }
        print(json.dumps(output, ensure_ascii=False) if args.json else (
            "Automatic update checks enabled" if enabled else "Automatic update checks disabled"
        ))
        return 0

    if args.check_updates:
        result = UpdateChecker(version, store=store).check(automatic=args.automatic)
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        elif result.status == "update_available":
            print(f"Update available: {result.latest_version} ({result.release_page})")
        elif result.status.startswith("skipped"):
            print(f"Update check skipped: {result.status}")
        elif result.status == "error":
            print(f"Update check failed: {result.error_code}", file=sys.stderr)
        else:
            print(f"No update available (current {version})")
        return 1 if result.status == "error" else 0

    try:
        from .gui import run_gui
    except ImportError as exc:
        print(f"Graphical toolkit unavailable: {exc}", file=sys.stderr)
        return 3
    return run_gui(version)


if __name__ == "__main__":
    sys.exit(main())
