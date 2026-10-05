#!/usr/bin/env python3
"""Interactive macOS installer for Canvas → Obsidian Sync."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SOURCE_DIR = Path(__file__).resolve().parent
APP_DIR = Path.home() / "Library" / "Application Support" / "CanvasObsidianSync"
LOG_DIR = Path.home() / "Library" / "Logs" / "CanvasObsidianSync"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
MANIFEST = APP_DIR / "install-manifest.json"
WEEKDAYS = {
    "sun": 0,
    "sunday": 0,
    "mon": 1,
    "monday": 1,
    "tue": 2,
    "tues": 2,
    "tuesday": 2,
    "wed": 3,
    "wednesday": 3,
    "thu": 4,
    "thur": 4,
    "thurs": 4,
    "thursday": 4,
    "fri": 5,
    "friday": 5,
    "sat": 6,
    "saturday": 6,
}


def prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def prompt_bool(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{label} [{hint}]: ").strip().casefold()
    if not value:
        return default
    return value in {"y", "yes"}


def iso_date(value: str) -> str:
    return dt.date.fromisoformat(value).isoformat()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "course"


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def parse_meetings(value: str) -> list[tuple[int, int, int]]:
    meetings: list[tuple[int, int, int]] = []
    for item in value.split(","):
        match = re.fullmatch(r"\s*([A-Za-z]+)\s+(\d{1,2}):(\d{2})\s*", item)
        if not match:
            raise ValueError(f"Invalid meeting time: {item.strip()!r}")
        weekday_text, hour_text, minute_text = match.groups()
        weekday = WEEKDAYS.get(weekday_text.casefold())
        hour, minute = int(hour_text), int(minute_text)
        if weekday is None or not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError(f"Invalid meeting time: {item.strip()!r}")
        meetings.append((weekday, hour, minute))
    if not meetings:
        raise ValueError("At least one meeting time is required")
    return meetings


def subtract_lead_time(
    meeting: tuple[int, int, int], lead_minutes: int
) -> tuple[int, int, int]:
    weekday, hour, minute = meeting
    total = hour * 60 + minute - lead_minutes
    while total < 0:
        total += 24 * 60
        weekday = (weekday - 1) % 7
    return weekday, total // 60, total % 60


def plist_payload(
    label: str,
    python_path: Path,
    course: str,
    start_date: str,
    end_date: str,
    intervals: list[tuple[int, int, int]],
) -> dict[str, Any]:
    calendar = [
        {"Weekday": weekday, "Hour": hour, "Minute": minute}
        for weekday, hour, minute in intervals
    ]
    return {
        "Label": label,
        "ProgramArguments": [
            str(python_path),
            str(APP_DIR / "canvas_scheduled_sync.py"),
            "--config",
            str(APP_DIR / "canvas_courses.json"),
            "--course",
            course,
            "--start-date",
            start_date,
            "--end-date",
            end_date,
        ],
        "StartCalendarInterval": calendar[0] if len(calendar) == 1 else calendar,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": str(LOG_DIR / "canvas-sync.log"),
        "StandardErrorPath": str(LOG_DIR / "canvas-sync-error.log"),
    }


def launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def ensure_keychain_token(service: str) -> None:
    existing = subprocess.run(
        ["security", "find-generic-password", "-s", service],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if existing.returncode == 0 and not prompt_bool(
        f"Keychain item {service!r} exists. Replace its Canvas token?", False
    ):
        return
    print("Enter the Canvas token at the secure Keychain prompt.")
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-a",
            getpass.getuser(),
            "-s",
            service,
            "-w",
        ],
        check=True,
    )


def collect_settings() -> (
    tuple[dict[str, Any], dict[str, list[tuple[int, int, int]]], str, str]
):
    print("Canvas → Obsidian Sync setup\n")
    base_url = prompt("Canvas base URL", "https://canvas.example.edu").rstrip("/")
    vault_path = Path(prompt("Semester vault directory")).expanduser().resolve()
    if not vault_path.is_dir():
        raise ValueError(f"Vault directory does not exist: {vault_path}")
    service = prompt("Keychain service name", "canvas-obsidian-sync")
    start_date = iso_date(prompt("Teaching period start (YYYY-MM-DD)"))
    end_date = iso_date(prompt("Teaching period end (YYYY-MM-DD)"))
    lead_minutes = int(prompt("Minutes before class to sync", "45"))
    max_file_size_mb = float(prompt("Maximum file size in MB", "500"))

    courses: dict[str, dict[str, Any]] = {}
    schedules: dict[str, list[tuple[int, int, int]]] = {}
    while True:
        code = prompt(
            "Course code" if not courses else "Next course code (blank to finish)"
        )
        if not code:
            if courses:
                break
            print("At least one course is required.")
            continue
        code = code.upper()
        canvas_id = int(prompt(f"{code} Canvas course ID"))
        directory = prompt(f"{code} Obsidian directory", code)
        course_directory = vault_path / directory
        if not course_directory.exists():
            if prompt_bool(f"Create {course_directory}?", True):
                course_directory.mkdir(parents=True)
            else:
                raise ValueError(f"Course directory does not exist: {course_directory}")
        while True:
            try:
                meetings = parse_meetings(
                    prompt(
                        f"{code} class times, comma separated",
                        "Mon 09:00",
                    )
                )
                break
            except ValueError as exc:
                print(exc)
        courses[code] = {"canvas_id": canvas_id, "vault_directory": directory}
        schedules[code] = [
            subtract_lead_time(meeting, lead_minutes) for meeting in meetings
        ]

    config = {
        "base_url": base_url,
        "keychain_service": service,
        "vault_path": str(vault_path),
        "max_file_size_mb": max_file_size_mb,
        "archive_removed": True,
        "courses": courses,
    }
    return config, schedules, start_date, end_date


def install() -> None:
    if platform.system() != "Darwin":
        raise SystemExit("The automatic installer currently supports macOS only.")
    config, schedules, start_date, end_date = collect_settings()
    service = str(config["keychain_service"])
    ensure_keychain_token(service)

    APP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    for filename in ("canvas_sync.py", "canvas_scheduled_sync.py"):
        shutil.copy2(SOURCE_DIR / filename, APP_DIR / filename)

    config_path = APP_DIR / "canvas_courses.json"
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    config_path.chmod(0o600)

    python_path = Path(sys.executable).resolve()
    if is_inside(Path(str(config["vault_path"])), Path.home() / "Documents"):
        print(
            "\nThe vault is inside Documents. Before background jobs are loaded, "
            "allow this executable in System Settings → Privacy & Security → "
            f"Full Disk Access:\n{python_path}"
        )
        input("Press Enter after granting permission (or if it is already enabled): ")

    print("\nValidating Canvas access and course settings (read-only)...")
    subprocess.run(
        [
            sys.executable,
            str(APP_DIR / "canvas_sync.py"),
            "--config",
            str(config_path),
            "--dry-run",
        ],
        check=True,
    )

    uid = os.getuid()
    labels: list[str] = []
    plist_paths: list[str] = []
    try:
        for course, intervals in schedules.items():
            label = f"com.canvas-obsidian-sync.{slugify(course)}"
            plist_path = LAUNCH_AGENTS / f"{label}.plist"
            payload = plist_payload(
                label,
                python_path,
                course,
                start_date,
                end_date,
                intervals,
            )
            with plist_path.open("wb") as handle:
                plistlib.dump(payload, handle, sort_keys=False)
            launchctl("bootout", f"gui/{uid}/{label}", check=False)
            launchctl("bootstrap", f"gui/{uid}", str(plist_path))
            labels.append(label)
            plist_paths.append(str(plist_path))
    except (OSError, subprocess.CalledProcessError):
        for label in labels:
            launchctl("bootout", f"gui/{uid}/{label}", check=False)
        raise

    MANIFEST.write_text(
        json.dumps(
            {"labels": labels, "plist_paths": plist_paths},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("\nInstallation complete.")
    print(f"Config: {config_path}")
    print(f"Logs:   {LOG_DIR}")
    print(f"Background Python: {python_path}")
    print("Run `python3 setup.py --uninstall` to remove the scheduled jobs.")


def uninstall() -> None:
    if not MANIFEST.exists():
        print("No installation manifest found; nothing to unload.")
        return
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    uid = os.getuid()
    for label in data.get("labels", []):
        launchctl("bootout", f"gui/{uid}/{label}", check=False)
    for value in data.get("plist_paths", []):
        path = Path(value)
        if path.parent == LAUNCH_AGENTS and path.name.startswith(
            "com.canvas-obsidian-sync."
        ):
            path.unlink(missing_ok=True)
    MANIFEST.unlink(missing_ok=True)
    print(
        "Scheduled jobs removed. Config, Keychain token, logs, and synced files were kept."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--uninstall", action="store_true", help="Unload and remove scheduled jobs."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        uninstall() if args.uninstall else install()
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nSetup cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
