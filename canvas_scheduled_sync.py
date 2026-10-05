#!/usr/bin/env python3
"""Run one scheduled, read-only Canvas sync during a configured date range."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import canvas_sync

LOCK_PATH = Path("/tmp/canvas-obsidian-sync.lock")


def iso_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO date: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a single-course Canvas sync during a teaching period."
    )
    parser.add_argument("--course", required=True)
    parser.add_argument("--config", type=Path, default=canvas_sync.DEFAULT_CONFIG)
    parser.add_argument("--start-date", type=iso_date, required=True)
    parser.add_argument("--end-date", type=iso_date, required=True)
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Disable macOS notifications for changes and failures.",
    )
    parser.add_argument(
        "--date",
        type=iso_date,
        help=argparse.SUPPRESS,
    )
    return parser


def change_count(summary: dict[str, object]) -> int:
    return sum(
        int(summary.get(key, 0) or 0)
        for key in ("new", "updated", "moved", "archived", "attention_changed")
    )


def notification_message(course: str, summary: dict[str, object]) -> str:
    parts = []
    labels = (
        ("new", "new"),
        ("updated", "updated"),
        ("moved", "moved"),
        ("archived", "archived"),
        ("attention_changed", "deadline/announcement change"),
        ("oversized", "too large"),
        ("skipped", "skipped"),
    )
    for key, label in labels:
        value = int(summary.get(key, 0) or 0)
        if value:
            parts.append(f"{value} {label}")
    return f"{course}: " + (", ".join(parts) if parts else "no changes")


def send_notification(title: str, message: str) -> None:
    script = (
        "on run argv\n"
        "display notification (item 1 of argv) with title (item 2 of argv)\n"
        "end run"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script, "--", message, title],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def run_sync(course: str, config: Path, notify: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="canvas-obsidian-sync-") as temporary:
        summary_path = Path(temporary) / "summary.json"
        command = [
            sys.executable,
            str(Path(canvas_sync.__file__).resolve()),
            "--config",
            str(config.expanduser().resolve()),
            "--course",
            course,
            "--quiet",
            "--summary-json",
            str(summary_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode != 0:
            if notify:
                send_notification(
                    "Canvas sync failed",
                    f"{course}: check the sync error log",
                )
            return result.returncode

        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if notify:
                send_notification("Canvas sync failed", f"{course}: summary missing")
            return 1
        if notify and (change_count(summary) or int(summary.get("oversized", 0) or 0)):
            send_notification(
                "Canvas materials updated", notification_message(course, summary)
            )
        return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    today = args.date or dt.datetime.now(canvas_sync.LOCAL_TZ).date()
    course = args.course.upper()

    if not args.start_date <= today <= args.end_date:
        print(
            f"[{course}] skipped: {today.isoformat()} is outside "
            f"{args.start_date.isoformat()}..{args.end_date.isoformat()}"
        )
        return 0

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"[{course}] skipped: another Canvas sync is already running")
            return 0
        return run_sync(course, args.config, not args.no_notify)


if __name__ == "__main__":
    raise SystemExit(main())
