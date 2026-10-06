#!/usr/bin/env python3
"""查看 Canvas → Obsidian Sync 状态并生成安全诊断信息。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

VERSION = "0.2.0"
APP_DIR = Path.home() / "Library" / "Application Support" / "CanvasObsidianSync"
LOG_DIR = Path.home() / "Library" / "Logs" / "CanvasObsidianSync"
CONFIG_PATH = APP_DIR / "canvas_courses.json"
MANIFEST_PATH = APP_DIR / "install-manifest.json"
STATUS_PATH = APP_DIR / "status.json"


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def next_scheduled_run(
    intervals: list[list[int]],
    start_date: str,
    end_date: str,
    now: dt.datetime | None = None,
) -> dt.datetime | None:
    now = now or dt.datetime.now().astimezone()
    try:
        start = dt.date.fromisoformat(start_date)
        end = dt.date.fromisoformat(end_date)
    except ValueError:
        return None
    for offset in range(15):
        day = now.date() + dt.timedelta(days=offset)
        if day < start or day > end:
            continue
        launchd_weekday = (day.weekday() + 1) % 7
        for interval in sorted(intervals):
            if len(interval) != 3 or int(interval[0]) != launchd_weekday:
                continue
            candidate = dt.datetime.combine(
                day,
                dt.time(int(interval[1]), int(interval[2])),
                tzinfo=now.tzinfo,
            )
            if candidate > now:
                return candidate
    return None


def summary_text(summary: dict[str, Any]) -> str:
    labels = (
        ("new", "新增"),
        ("updated", "更新"),
        ("moved", "移动"),
        ("archived", "归档"),
        ("oversized", "超大文件"),
    )
    parts = [
        f"{label} {int(summary.get(key, 0) or 0)}"
        for key, label in labels
        if int(summary.get(key, 0) or 0)
    ]
    return "，".join(parts) if parts else "无文件变化"


def installed_courses(
    config: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    courses = manifest.get("courses")
    if isinstance(courses, dict) and courses:
        return courses
    return {
        code: {"label": f"com.canvas-obsidian-sync.{code.casefold()}", "intervals": []}
        for code in config.get("courses", {})
    }


def show_status() -> int:
    config = read_json(CONFIG_PATH)
    manifest = read_json(MANIFEST_PATH)
    status = read_json(STATUS_PATH).get("courses", {})
    if not config or not manifest:
        print("尚未完成安装。请双击 Install.command。")
        return 1

    print(f"Canvas → Obsidian Sync v{VERSION}")
    print(
        f"教学期：{manifest.get('start_date', '未知')} 至 {manifest.get('end_date', '未知')}"
    )
    print(f"提前量：{manifest.get('lead_minutes', '未知')} 分钟")
    print()
    now = dt.datetime.now().astimezone()
    for code, details in installed_courses(config, manifest).items():
        record = status.get(code, {}) if isinstance(status, dict) else {}
        result = record.get("result")
        result_text = {"success": "成功", "failed": "失败"}.get(result, "尚未运行")
        last_run = record.get("last_run", "—")
        next_run = next_scheduled_run(
            details.get("intervals", []),
            str(manifest.get("start_date", "")),
            str(manifest.get("end_date", "")),
            now,
        )
        print(f"{code}")
        print(f"  上次同步：{last_run}（{result_text}）")
        print(
            "  上次结果：" + summary_text(record.get("summary", {}))
            if result == "success"
            else f"  详情：{record.get('detail') or '—'}"
        )
        print(
            "  下次同步："
            + (next_run.strftime("%Y-%m-%d %H:%M") if next_run else "教学期内暂无")
        )
        print()
    return 0


def command_ok(command: list[str]) -> bool:
    try:
        return (
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def show_diagnostics() -> int:
    config = read_json(CONFIG_PATH)
    manifest = read_json(MANIFEST_PATH)
    print("Canvas → Obsidian Sync 诊断报告（不包含 Token）")
    print(f"版本：{VERSION}")
    print(f"系统：{platform.platform()}")
    print(f"Python：{sys.version.split()[0]} ({Path(sys.executable).resolve()})")
    print(f"程序目录：{'正常' if APP_DIR.is_dir() else '不存在'}")
    print(f"配置文件：{'正常' if config else '缺失或损坏'}")
    print(f"安装记录：{'正常' if manifest else '缺失或损坏'}")
    if config:
        vault = Path(str(config.get("vault_path", ""))).expanduser()
        print(f"Obsidian 目录：{'可访问' if vault.is_dir() else '不可访问'}")
        print(f"Obsidian 写入权限：{'正常' if os.access(vault, os.W_OK) else '不可写'}")
        service = str(config.get("keychain_service", ""))
        token_ok = bool(service) and command_ok(
            ["security", "find-generic-password", "-s", service]
        )
        print(f"Keychain Token：{'存在' if token_ok else '未找到'}")
    for code, details in installed_courses(config, manifest).items():
        label = str(details.get("label", ""))
        loaded = bool(label) and command_ok(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"]
        )
        print(f"定时任务 {code}：{'已加载' if loaded else '未加载'}")
    for name in ("canvas-sync.log", "canvas-sync-error.log"):
        path = LOG_DIR / name
        if path.exists():
            modified = dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            print(
                f"日志 {name}：{path.stat().st_size} bytes，更新于 {modified.isoformat(timespec='minutes')}"
            )
        else:
            print(f"日志 {name}：尚未生成")
    print("\n分享此报告前仍建议检查其中的本机路径和课程代码。")
    return 0 if config and manifest else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "diagnostics"))
    return parser


def main(argv: list[str] | None = None) -> int:
    action = build_parser().parse_args(argv).action
    return show_status() if action == "status" else show_diagnostics()


if __name__ == "__main__":
    raise SystemExit(main())
