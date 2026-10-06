#!/usr/bin/env python3
"""Canvas → Obsidian Sync 的交互式 macOS 安装器。"""

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
CONFIG_PATH = APP_DIR / "canvas_courses.json"
RUNTIME_FILES = ("canvas_sync.py", "canvas_scheduled_sync.py", "manager.py")
WEEKDAYS = {
    "sun": 0,
    "sunday": 0,
    "周日": 0,
    "星期日": 0,
    "mon": 1,
    "monday": 1,
    "周一": 1,
    "星期一": 1,
    "tue": 2,
    "tues": 2,
    "tuesday": 2,
    "周二": 2,
    "星期二": 2,
    "wed": 3,
    "wednesday": 3,
    "周三": 3,
    "星期三": 3,
    "thu": 4,
    "thur": 4,
    "thurs": 4,
    "thursday": 4,
    "周四": 4,
    "星期四": 4,
    "fri": 5,
    "friday": 5,
    "周五": 5,
    "星期五": 5,
    "sat": 6,
    "saturday": 6,
    "周六": 6,
    "星期六": 6,
}
WEEKDAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def prompt_bool(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{label} [{hint}]: ").strip().casefold()
    if not value:
        return default
    return value in {"y", "yes", "是", "好", "确认"}


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
    for item in re.split(r"[,，]", value):
        match = re.fullmatch(
            r"\s*([A-Za-z]+|周[一二三四五六日]|星期[一二三四五六日])\s+(\d{1,2}):(\d{2})\s*",
            item,
        )
        if not match:
            raise ValueError(f"无法识别上课时间：{item.strip()!r}")
        weekday_text, hour_text, minute_text = match.groups()
        weekday = WEEKDAYS.get(weekday_text.casefold())
        hour, minute = int(hour_text), int(minute_text)
        if weekday is None or not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError(f"上课时间无效：{item.strip()!r}")
        meetings.append((weekday, hour, minute))
    if not meetings:
        raise ValueError("至少需要输入一个上课时间")
    return meetings


def meetings_to_text(meetings: list[tuple[int, int, int]]) -> str:
    return ", ".join(
        f"{WEEKDAY_NAMES[weekday]} {hour:02d}:{minute:02d}"
        for weekday, hour, minute in meetings
    )


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
            str(CONFIG_PATH),
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


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, data: dict[str, Any], mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if mode is not None:
        temporary.chmod(mode)
    temporary.replace(path)


def copy_runtime() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    for filename in RUNTIME_FILES:
        source = SOURCE_DIR / filename
        if not source.is_file():
            raise FileNotFoundError(f"缺少程序文件：{source}")
        shutil.copy2(source, APP_DIR / filename)


def ensure_keychain_token(service: str) -> None:
    existing = subprocess.run(
        ["security", "find-generic-password", "-s", service],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if existing.returncode == 0 and not prompt_bool(
        f"钥匙串中已经存在 {service!r}，是否替换 Token", False
    ):
        return
    print("\n请在接下来的安全输入框中粘贴 Canvas Token。")
    print("Token 不会写入配置文件或终端历史。")
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


def collect_settings(
    existing_config: dict[str, Any] | None = None,
    existing_manifest: dict[str, Any] | None = None,
) -> tuple[
    dict[str, Any],
    dict[str, list[tuple[int, int, int]]],
    str,
    str,
    dict[str, Any],
]:
    existing_config = existing_config or {}
    existing_manifest = existing_manifest or {}
    today = dt.date.today()
    print("Canvas → Obsidian Sync 设置向导\n")
    print("直接按回车可接受方括号中的默认值。\n")
    base_url = prompt(
        "Canvas 地址",
        str(existing_config.get("base_url") or "https://canvas.example.edu"),
    ).rstrip("/")
    vault_default = str(existing_config.get("vault_path") or "") or None
    vault_path = Path(prompt("Obsidian 学期目录", vault_default)).expanduser().resolve()
    if not vault_path.is_dir():
        raise ValueError(f"Obsidian 学期目录不存在：{vault_path}")
    service = prompt(
        "钥匙串服务名称",
        str(existing_config.get("keychain_service") or "canvas-obsidian-sync"),
    )
    start_date = iso_date(
        prompt(
            "教学期开始日期（YYYY-MM-DD）",
            str(existing_manifest.get("start_date") or today.isoformat()),
        )
    )
    end_date = iso_date(
        prompt(
            "教学期结束日期（YYYY-MM-DD）",
            str(
                existing_manifest.get("end_date")
                or (today + dt.timedelta(days=120)).isoformat()
            ),
        )
    )
    if end_date < start_date:
        raise ValueError("教学期结束日期不能早于开始日期")
    lead_minutes = int(
        prompt("提前多少分钟同步", str(existing_manifest.get("lead_minutes", 45)))
    )
    if lead_minutes < 0:
        raise ValueError("提前时间不能为负数")
    max_file_size_mb = float(
        prompt(
            "单个文件大小上限（MB）",
            str(existing_config.get("max_file_size_mb", 500)),
        )
    )
    if max_file_size_mb <= 0:
        raise ValueError("文件大小上限必须大于 0")

    existing_courses = existing_config.get("courses", {})
    existing_times = existing_manifest.get("class_times", {})
    reuse_courses = bool(existing_courses and existing_times) and prompt_bool(
        "是否保留现有课程和上课时间", True
    )
    courses: dict[str, dict[str, Any]] = {}
    class_times: dict[str, list[tuple[int, int, int]]] = {}
    if reuse_courses:
        courses = dict(existing_courses)
        class_times = {
            code: [tuple(int(value) for value in meeting) for meeting in meetings]
            for code, meetings in existing_times.items()
            if code in courses
        }
        if set(class_times) != set(courses):
            raise ValueError("旧配置缺少部分课程的上课时间，请重新输入课程")
    else:
        print("\n课程 ID 可从 Canvas 课程网址 /courses/123456 中找到。")
        print("上课时间示例：Mon 09:00, Wed 09:00（也支持 周一 09:00）\n")
        while True:
            code = prompt("课程代码" if not courses else "下一门课程代码（留空结束）")
            if not code:
                if courses:
                    break
                print("至少需要配置一门课程。")
                continue
            code = code.upper()
            canvas_id = int(prompt(f"{code} 的 Canvas 课程 ID"))
            directory = prompt(f"{code} 的 Obsidian 文件夹名称", code)
            course_directory = (vault_path / directory).resolve()
            if not is_inside(course_directory, vault_path):
                raise ValueError("课程文件夹必须位于 Obsidian 学期目录内部")
            if not course_directory.exists():
                if prompt_bool(f"{course_directory} 不存在，是否创建", True):
                    course_directory.mkdir(parents=True)
                else:
                    raise ValueError(f"课程文件夹不存在：{course_directory}")
            while True:
                try:
                    meetings = parse_meetings(
                        prompt(f"{code} 的上课时间（逗号分隔）", "Mon 09:00")
                    )
                    break
                except ValueError as exc:
                    print(exc)
            courses[code] = {"canvas_id": canvas_id, "vault_directory": directory}
            class_times[code] = meetings

    schedules = {
        code: [subtract_lead_time(meeting, lead_minutes) for meeting in meetings]
        for code, meetings in class_times.items()
    }
    config = {
        "base_url": base_url,
        "keychain_service": service,
        "vault_path": str(vault_path),
        "max_file_size_mb": max_file_size_mb,
        "archive_removed": True,
        "courses": courses,
    }
    existing_layout = existing_config.get("layout")
    if isinstance(existing_layout, dict):
        config["layout"] = dict(existing_layout)
    details = {
        "lead_minutes": lead_minutes,
        "class_times": {
            code: [list(meeting) for meeting in meetings]
            for code, meetings in class_times.items()
        },
    }
    return config, schedules, start_date, end_date, details


def install(configure: bool = False) -> None:
    if platform.system() != "Darwin":
        raise SystemExit("自动安装器目前只支持 macOS。")
    installed_manifest = read_json(MANIFEST)
    old_config = read_json(CONFIG_PATH) if configure else {}
    old_manifest = installed_manifest if configure else {}
    config, schedules, start_date, end_date, details = collect_settings(
        old_config, old_manifest
    )
    ensure_keychain_token(str(config["keychain_service"]))

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    copy_runtime()
    candidate_config = APP_DIR / ".canvas_courses.candidate.json"
    write_json(candidate_config, config, mode=0o600)

    python_path = Path(sys.executable).resolve()
    if is_inside(Path(str(config["vault_path"])), Path.home() / "Documents"):
        print("\n你的 Obsidian 目录位于 Documents。")
        print("请打开：系统设置 → 隐私与安全性 → 完全磁盘访问权限")
        print(f"为以下 Python 程序开启权限：\n{python_path}")
        input("完成后按回车继续：")

    print("\n正在只读验证 Canvas Token、课程 ID 和目录……")
    try:
        subprocess.run(
            [
                sys.executable,
                str(APP_DIR / "canvas_sync.py"),
                "--config",
                str(candidate_config),
                "--dry-run",
            ],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        candidate_config.unlink(missing_ok=True)
        raise
    candidate_config.replace(CONFIG_PATH)

    uid = os.getuid()
    labels: list[str] = []
    plist_paths: list[str] = []
    courses_manifest: dict[str, Any] = {}
    try:
        for course, intervals in schedules.items():
            label = f"com.canvas-obsidian-sync.{slugify(course)}"
            plist_path = LAUNCH_AGENTS / f"{label}.plist"
            with plist_path.open("wb") as handle:
                plistlib.dump(
                    plist_payload(
                        label,
                        python_path,
                        course,
                        start_date,
                        end_date,
                        intervals,
                    ),
                    handle,
                    sort_keys=False,
                )
            launchctl("bootout", f"gui/{uid}/{label}", check=False)
            launchctl("bootstrap", f"gui/{uid}", str(plist_path))
            labels.append(label)
            plist_paths.append(str(plist_path))
            courses_manifest[course] = {
                "label": label,
                "intervals": [list(interval) for interval in intervals],
            }
    except (OSError, subprocess.CalledProcessError):
        for label in labels:
            launchctl("bootout", f"gui/{uid}/{label}", check=False)
        raise

    previous_labels = set(installed_manifest.get("labels", []))
    for label in previous_labels.difference(labels):
        launchctl("bootout", f"gui/{uid}/{label}", check=False)
    current_plists = set(plist_paths)
    for value in installed_manifest.get("plist_paths", []):
        path = Path(value)
        if (
            str(path) not in current_plists
            and path.parent == LAUNCH_AGENTS
            and path.name.startswith("com.canvas-obsidian-sync.")
        ):
            path.unlink(missing_ok=True)

    write_json(
        MANIFEST,
        {
            "version": 2,
            "labels": labels,
            "plist_paths": plist_paths,
            "start_date": start_date,
            "end_date": end_date,
            "lead_minutes": details["lead_minutes"],
            "class_times": details["class_times"],
            "courses": courses_manifest,
        },
    )
    print("\n✅ 安装完成")
    print(f"配置：{CONFIG_PATH}")
    print(f"日志：{LOG_DIR}")
    print("以后可双击 Status.command 查看同步状态。")


def refresh() -> None:
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError("尚未安装，请先运行 Install.command")
    copy_runtime()
    print("✅ 后台程序已更新；课程配置和定时任务保持不变。")


def uninstall() -> None:
    if not MANIFEST.exists():
        print("没有发现已安装的定时任务。")
        return
    data = read_json(MANIFEST)
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
    print("✅ 定时任务已移除。配置、Token、日志和课程资料均已保留。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--configure", action="store_true", help="修改现有设置")
    action.add_argument("--refresh", action="store_true", help="更新后台程序文件")
    action.add_argument("--uninstall", action="store_true", help="卸载定时任务")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.uninstall:
            uninstall()
        elif args.refresh:
            refresh()
        else:
            install(configure=args.configure)
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"\n❌ 操作失败：命令返回错误代码 {exc.returncode}", file=sys.stderr)
        print("请双击 Diagnostics.command 查看诊断信息。", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"\n❌ 操作失败：{exc}", file=sys.stderr)
        print("请检查输入，或双击 Diagnostics.command 查看诊断信息。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n操作已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
