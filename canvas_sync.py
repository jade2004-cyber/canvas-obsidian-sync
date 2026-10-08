#!/usr/bin/env python3
"""Read-only Canvas-to-Obsidian synchronizer for course materials.

The script only performs HTTP GET requests. By default it downloads files into
the course's Canvas/ tree; an optional layout config can target existing course
folders instead. The API token is read from macOS Keychain and is never written
to disk or included in output.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import re
import socket
import ssl
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "canvas_courses.json"
STATE_NAME = ".canvas-sync-state.json"
INDEX_NAME = "索引.md"
USER_AGENT = "canvas-obsidian-read-only-sync/0.2.2"
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
MAX_ATTEMPTS = 8
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


class SyncError(RuntimeError):
    """A user-facing synchronization error."""


class TransientCanvasError(RuntimeError):
    """A retryable Canvas connection or rate-limit error."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Course:
    code: str
    canvas_id: int
    vault_directory: str
    sync_directory: str = "Canvas"
    material_directory: str = "课程资料"
    week_directory: str = ""
    index_path: str = INDEX_NAME
    archive_directory: str = "_Archived"
    preserve_existing_paths: bool = False
    generate_index: bool = True


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward the Canvas bearer token to a different host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).netloc.casefold()
        new_host = urllib.parse.urlsplit(newurl).netloc.casefold()
        if old_host != new_host:
            redirected.remove_header("Authorization")
        return redirected


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SyncError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SyncError(f"Invalid JSON in {path}: {exc}") from exc
    required = {"base_url", "keychain_service", "vault_path", "courses"}
    missing = required.difference(data)
    if missing:
        raise SyncError(f"Config is missing: {', '.join(sorted(missing))}")
    data.setdefault("max_file_size_mb", 500)
    data.setdefault("archive_removed", True)
    return data


def retry_delay(error: TransientCanvasError, attempt: int) -> float:
    if error.retry_after is not None:
        return max(0.0, min(error.retry_after, 120.0))
    return float(min(2**attempt, 30))


def get_keychain_token(service: str) -> str:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SyncError("macOS security command is unavailable") from exc
    except subprocess.CalledProcessError as exc:
        raise SyncError(
            f"Cannot read Keychain item {service!r}. Check its access permissions."
        ) from exc
    token = result.stdout.strip()
    if not token:
        raise SyncError(f"Keychain item {service!r} contains an empty token")
    return token


class CanvasClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        self._opener = urllib.request.build_opener(SafeRedirectHandler())

    def _open(
        self, url: str, extra_headers: dict[str, str] | None = None
    ) -> urllib.response.addinfourl:
        headers = dict(self._headers)
        headers.update(extra_headers or {})
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            return self._opener.open(request, timeout=90)
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_HTTP_CODES:
                retry_after = None
                value = exc.headers.get("Retry-After")
                if value:
                    try:
                        retry_after = float(value)
                    except ValueError:
                        retry_after = None
                raise TransientCanvasError(
                    f"Canvas temporarily returned HTTP {exc.code}", retry_after
                ) from exc
            detail = ""
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
                detail = payload.get("message", "") if isinstance(payload, dict) else ""
            except (json.JSONDecodeError, AttributeError):
                pass
            suffix = f": {detail}" if detail else ""
            raise SyncError(f"Canvas API returned HTTP {exc.code}{suffix}") from exc
        except urllib.error.URLError as exc:
            if isinstance(
                exc.reason,
                (
                    TimeoutError,
                    socket.timeout,
                    socket.gaierror,
                    ssl.SSLError,
                    ConnectionResetError,
                    http.client.RemoteDisconnected,
                ),
            ):
                raise TransientCanvasError(
                    f"Temporary Canvas connection error: {exc.reason}"
                ) from exc
            raise SyncError(f"Cannot connect to Canvas: {exc.reason}") from exc
        except (
            ssl.SSLError,
            ConnectionResetError,
            http.client.RemoteDisconnected,
        ) as exc:
            raise TransientCanvasError(
                f"Temporary Canvas connection error: {exc}"
            ) from exc

    def _read_json_page(self, url: str) -> tuple[Any, str]:
        for attempt in range(MAX_ATTEMPTS):
            try:
                with self._open(url) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    return payload, response.headers.get("Link", "")
            except (
                TimeoutError,
                socket.timeout,
                ssl.SSLError,
                ConnectionResetError,
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
                TransientCanvasError,
            ) as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise SyncError(
                        f"Canvas request failed after {MAX_ATTEMPTS} attempts: {exc}"
                    ) from exc
                transient = (
                    exc
                    if isinstance(exc, TransientCanvasError)
                    else TransientCanvasError(str(exc))
                )
                time.sleep(retry_delay(transient, attempt))
        raise AssertionError("unreachable")

    def get_json(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}{endpoint}"
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        payload, _ = self._read_json_page(url)
        return payload

    def get_all(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}{endpoint}"
        query_params = dict(params or {})
        query_params.setdefault("per_page", 30)
        query = urllib.parse.urlencode(query_params, doseq=True)
        url = f"{url}{'&' if '?' in url else '?'}{query}"
        output: list[dict[str, Any]] = []
        while url:
            page, link_header = self._read_json_page(url)
            if not isinstance(page, list):
                raise SyncError("Canvas pagination endpoint returned non-list JSON")
            output.extend(item for item in page if isinstance(item, dict))
            url = parse_next_link(link_header)
        return output

    def download(
        self,
        url: str,
        destination: Path,
        expected_size: int | None = None,
        max_bytes: int | None = None,
    ) -> str:
        temporary = destination.with_name(f".{destination.name}.canvas-download")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        if (
            max_bytes is not None
            and expected_size is not None
            and expected_size > max_bytes
        ):
            raise SyncError(
                f"File is {human_size(expected_size)}, above the configured "
                f"limit of {human_size(max_bytes)}"
            )

        for attempt in range(MAX_ATTEMPTS):
            offset = temporary.stat().st_size if temporary.exists() else 0
            if expected_size is not None and offset > expected_size:
                temporary.unlink(missing_ok=True)
                offset = 0
            if (
                expected_size is not None
                and temporary.exists()
                and offset == expected_size
            ):
                digest = sha256_file(temporary)
                temporary.replace(destination)
                return digest
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with self._open(url, headers) as response:
                    status = getattr(response, "status", response.getcode())
                    append = offset > 0 and status == 206
                    if offset and not append:
                        offset = 0
                    mode = "ab" if append else "wb"
                    with temporary.open(mode) as handle:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            handle.write(chunk)
                            if max_bytes is not None and handle.tell() > max_bytes:
                                raise SyncError(
                                    f"Download exceeded the configured limit of "
                                    f"{human_size(max_bytes)}"
                                )
                actual_size = temporary.stat().st_size
                if expected_size is not None and actual_size != expected_size:
                    raise TransientCanvasError(
                        f"Incomplete download: expected {expected_size} bytes, "
                        f"received {actual_size}"
                    )
                digest = sha256_file(temporary)
                temporary.replace(destination)
                return digest
            except SyncError:
                temporary.unlink(missing_ok=True)
                raise
            except (
                TimeoutError,
                socket.timeout,
                ssl.SSLError,
                ConnectionResetError,
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
                TransientCanvasError,
            ) as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise SyncError(
                        f"Download failed after {MAX_ATTEMPTS} attempts: {exc}"
                    ) from exc
                transient = (
                    exc
                    if isinstance(exc, TransientCanvasError)
                    else TransientCanvasError(str(exc))
                )
                time.sleep(retry_delay(transient, attempt))
        raise AssertionError("unreachable")


def parse_next_link(value: str) -> str:
    for part in value.split(","):
        match = re.match(r'\s*<([^>]+)>;\s*rel="([^"]+)"', part)
        if match and match.group(2) == "next":
            return match.group(1)
    return ""


def sanitize_filename(name: str, fallback: str) -> str:
    clean = unicodedata.normalize("NFKC", name).strip()
    clean = clean.replace("/", "-").replace("\\", "-").replace("\x00", "")
    clean = re.sub(r"[\r\n\t]+", " ", clean)
    clean = re.sub(r"\s{2,}", " ", clean).strip(" .")
    return clean or fallback


def week_number(*texts: str) -> int | None:
    patterns = (
        r"\bweek\s*[-_ ]*0*(\d{1,2})\b",
        r"\b(?:lecture|lec)\s*[-_ ]*0*(\d{1,2})\b",
        r"\bL0*(\d{1,2})(?:\b|[_-])",
        r"(?:第\s*)?(\d{1,2})\s*周",
    )
    joined = " ".join(texts)
    for pattern in patterns:
        match = re.search(pattern, joined, flags=re.IGNORECASE)
        if match:
            value = int(match.group(1))
            if 1 <= value <= 30:
                return value
    return None


def is_course_material(*texts: str) -> bool:
    joined = " ".join(texts).lower()
    terms = (
        "syllabus",
        "course outline",
        "course information",
        "course schedule",
        "teaching schedule",
        "assessment schedule",
        "课程大纲",
        "课程资料",
    )
    return any(term in joined for term in terms)


def choose_section(
    filename: str, folder_path: str = "", module_names: Iterable[str] = ()
) -> str:
    if is_course_material(filename):
        return "课程资料"
    week = week_number(filename)
    if week is not None:
        return f"Week {week:02d}"
    context = [folder_path, *module_names]
    week = week_number(*context)
    if week is not None:
        return f"Week {week:02d}"
    if is_course_material(*context):
        return "课程资料"
    return "课程资料"


def human_size(value: int) -> str:
    size = float(max(value, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def unique_destination(directory: Path, filename: str, canvas_id: int) -> Path:
    preferred = directory / filename
    if not preferred.exists():
        return preferred
    stem, suffix = preferred.stem, preferred.suffix
    tagged = directory / f"{stem} [Canvas {canvas_id}]{suffix}"
    return tagged


def safe_canvas_path(canvas_root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise SyncError(f"Unsafe path in sync state: {relative}")
    return canvas_root / relative


def safe_config_path(value: str, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise SyncError(f"Unsafe {label}: {value}")
    return path


def archive_file(
    canvas_root: Path,
    source: Path,
    relative: Path,
    canvas_id: int,
    archive_directory: Path = Path("_Archived"),
) -> Path:
    archive_root = canvas_root / archive_directory / dt.date.today().isoformat()
    destination = archive_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination = unique_destination(
            destination.parent, destination.name, canvas_id
        )
    source.replace(destination)
    return destination


def parse_canvas_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_local_time(value: str | None) -> str:
    parsed = parse_canvas_time(value)
    if not parsed:
        return "未设置"
    return parsed.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def markdown_link(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    label = path.name.replace("|", "-").replace("]", "）")
    target = relative.replace("|", "-").replace("]", "）")
    return f"[[{target}|{label}]]"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "files": {}}
    if not isinstance(data.get("files"), dict):
        data["files"] = {}
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def module_file_map(modules: list[dict[str, Any]]) -> dict[int, list[str]]:
    mapping: dict[int, list[str]] = {}
    for module in modules:
        module_name = str(module.get("name", ""))
        for item in module.get("items", []) or []:
            if not isinstance(item, dict) or item.get("type") != "File":
                continue
            try:
                file_id = int(item["content_id"])
            except (KeyError, TypeError, ValueError):
                continue
            mapping.setdefault(file_id, []).append(module_name)
    return mapping


def attention_fingerprint(
    assignments: list[dict[str, Any]], announcements: list[dict[str, Any]]
) -> str:
    assignment_fields = ("id", "name", "due_at", "updated_at", "points_possible")
    announcement_fields = ("id", "title", "posted_at", "updated_at")
    payload = {
        "assignments": sorted(
            ({key: item.get(key) for key in assignment_fields} for item in assignments),
            key=lambda item: str(item.get("id", "")),
        ),
        "announcements": sorted(
            (
                {key: item.get(key) for key in announcement_fields}
                for item in announcements
            ),
            key=lambda item: str(item.get("id", "")),
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def render_index(
    course: Course,
    base_url: str,
    canvas_root: Path,
    assignments: list[dict[str, Any]],
    announcements: list[dict[str, Any]],
    synced_at: dt.datetime,
    material_directory: Path = Path("课程资料"),
    week_directory: Path = Path(),
) -> str:
    lines = [
        "---",
        f"课程代码: {course.code}",
        f"Canvas课程ID: {course.canvas_id}",
        f"最后同步: {synced_at.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')}",
        "---",
        "",
        f"# {course.code} Canvas 原始资料索引",
        "",
        f"[Canvas 主页]({base_url}/courses/{course.canvas_id})",
        "",
    ]

    sections: list[tuple[str, list[Path]]] = []
    material_dir = canvas_root / material_directory
    material_files = (
        sorted(
            (p for p in material_dir.rglob("*") if p.is_file()),
            key=lambda p: p.as_posix().casefold(),
        )
        if material_dir.exists()
        else []
    )
    sections.append(("课程资料", material_files))
    week_root = canvas_root / week_directory
    week_dirs = sorted(
        (p for p in week_root.glob("Week [0-9][0-9]") if p.is_dir()),
        key=lambda p: p.name,
    )
    sections.extend(
        (
            week_dir.name,
            sorted(
                (p for p in week_dir.rglob("*") if p.is_file()),
                key=lambda p: p.as_posix().casefold(),
            ),
        )
        for week_dir in week_dirs
    )
    for heading, files in sections:
        if not files:
            continue
        lines.extend([f"## {heading}", ""])
        lines.extend(f"- {markdown_link(path, canvas_root)}" for path in files)
        lines.append("")

    now = synced_at.astimezone(dt.timezone.utc)
    dated_assignments = []
    for assignment in assignments:
        due = parse_canvas_time(assignment.get("due_at"))
        if due is not None and due >= now - dt.timedelta(days=7):
            dated_assignments.append((due, assignment))
    dated_assignments.sort(key=lambda pair: pair[0])
    lines.extend(["## 需要关注", ""])
    if dated_assignments:
        lines.append("### 截止日期")
        lines.append("")
        for due, assignment in dated_assignments:
            name = str(assignment.get("name", "未命名作业")).replace("\n", " ")
            due_text = format_local_time(assignment.get("due_at"))
            url = (
                assignment.get("html_url")
                or f"{base_url}/courses/{course.canvas_id}/assignments"
            )
            points = assignment.get("points_possible")
            suffix = f"，{points:g} 分" if isinstance(points, (int, float)) else ""
            status = "（已截止）" if due < now else ""
            lines.append(f"- {due_text}{status}：[{name}]({url}){suffix}")
        lines.append("")
    else:
        lines.extend(["- 当前没有发现未来截止或最近 7 天内截止的作业。", ""])

    if announcements:
        lines.extend(["### 最近公告", ""])
        recent = sorted(
            announcements,
            key=lambda item: item.get("posted_at") or "",
            reverse=True,
        )[:10]
        for item in recent:
            title = str(item.get("title", "未命名公告")).replace("\n", " ")
            posted = format_local_time(item.get("posted_at"))
            url = (
                item.get("html_url")
                or f"{base_url}/courses/{course.canvas_id}/announcements"
            )
            lines.append(f"- {posted}：[{title}]({url})")
        lines.append("")

    lines.extend(
        [
            "> [!info] 同步说明",
            "> 本页由只读 Canvas 同步工具生成。原始文件不会被用于提交作业，也不会修改 Canvas 内容。",
            "",
        ]
    )
    return "\n".join(lines)


def collect_course_data(client: CanvasClient, course: Course) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    course_info = client.get_json(f"/api/v1/courses/{course.canvas_id}")
    folders = client.get_all(f"/api/v1/courses/{course.canvas_id}/folders")
    modules = client.get_all(
        f"/api/v1/courses/{course.canvas_id}/modules", {"include[]": "items"}
    )
    for module in modules:
        expected = int(module.get("items_count") or 0)
        current = module.get("items") or []
        if module.get("id") is not None and expected > len(current):
            module["items"] = client.get_all(
                f"/api/v1/courses/{course.canvas_id}/modules/{module['id']}/items"
            )
    try:
        files = client.get_all(f"/api/v1/courses/{course.canvas_id}/files")
    except SyncError as exc:
        if "HTTP 403" not in str(exc):
            raise
        file_ids = sorted(
            {
                int(item["content_id"])
                for module in modules
                for item in (module.get("items") or [])
                if isinstance(item, dict)
                and item.get("type") == "File"
                and item.get("content_id") is not None
            }
        )
        files = []
        inaccessible = 0
        for file_id in file_ids:
            try:
                metadata = client.get_json(f"/api/v1/files/{file_id}")
            except SyncError:
                inaccessible += 1
                continue
            if isinstance(metadata, dict):
                files.append(metadata)
        print(
            f"[{course.code}] course file listing is restricted; "
            f"using {len(files)} file(s) exposed through modules"
            + (f" ({inaccessible} inaccessible)" if inaccessible else "")
        )
    assignments = client.get_all(f"/api/v1/courses/{course.canvas_id}/assignments")
    start_date = (
        (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)).date().isoformat()
    )
    announcements = client.get_all(
        "/api/v1/announcements",
        {
            "context_codes[]": f"course_{course.canvas_id}",
            "start_date": start_date,
            "end_date": dt.datetime.now(dt.timezone.utc).date().isoformat(),
        },
    )
    return course_info, files, folders, modules, assignments, announcements


def sync_course(
    client: CanvasClient,
    course: Course,
    vault: Path,
    base_url: str,
    dry_run: bool,
    quiet: bool,
    max_file_size_mb: float,
    archive_removed: bool,
) -> dict[str, int]:
    course_dir = vault / course.vault_directory
    if not course_dir.is_dir():
        raise SyncError(f"Course directory does not exist: {course_dir}")
    sync_directory = safe_config_path(course.sync_directory, "sync_directory")
    material_directory = safe_config_path(
        course.material_directory, "material_directory"
    )
    week_directory = safe_config_path(course.week_directory, "week_directory")
    index_path = safe_config_path(course.index_path, "index_path")
    archive_directory = safe_config_path(course.archive_directory, "archive_directory")
    canvas_root = course_dir / sync_directory
    state_path = canvas_root / STATE_NAME
    state = load_state(state_path)
    old_files = state.setdefault("files", {})

    course_info, files, folders, modules, assignments, announcements = (
        collect_course_data(client, course)
    )
    current_attention = attention_fingerprint(assignments, announcements)
    previous_attention = state.get("attention_sha256")
    folder_paths = {
        int(folder["id"]): str(folder.get("full_name") or folder.get("name") or "")
        for folder in folders
        if folder.get("id") is not None
    }
    module_names = module_file_map(modules)
    max_bytes = max(0, int(max_file_size_mb * 1024 * 1024))
    stats = {
        "new": 0,
        "updated": 0,
        "moved": 0,
        "archived": 0,
        "unchanged": 0,
        "oversized": 0,
        "attention_changed": int(previous_attention != current_attention),
        "skipped": 0,
        "bytes": 0,
    }
    next_state: dict[str, Any] = {
        "version": 2,
        "course_id": course.canvas_id,
        "course_name": course_info.get("name", course.code),
        "synced_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "attention_sha256": current_attention,
        "files": {},
    }
    remote_ids: set[str] = set()

    for remote in sorted(
        files, key=lambda item: str(item.get("display_name", "")).casefold()
    ):
        try:
            file_id = int(remote["id"])
        except (KeyError, TypeError, ValueError):
            stats["skipped"] += 1
            continue
        remote_ids.add(str(file_id))
        filename = sanitize_filename(
            str(remote.get("display_name") or remote.get("filename") or ""),
            f"canvas-file-{file_id}",
        )
        folder_path = folder_paths.get(int(remote.get("folder_id") or 0), "")
        section = choose_section(filename, folder_path, module_names.get(file_id, []))
        directory = (
            canvas_root / material_directory
            if section == "课程资料"
            else canvas_root / week_directory / section
        )
        previous = old_files.get(str(file_id), {})
        previous_path = Path(previous.get("path", "")) if previous.get("path") else None
        previous_full = (
            safe_canvas_path(canvas_root, previous_path) if previous_path else None
        )
        destination = (
            previous_full
            if course.preserve_existing_paths
            and previous_full is not None
            and previous_full.is_file()
            else directory / filename
        )
        if destination.exists() and destination != previous_full:
            destination = unique_destination(directory, filename, file_id)
        relative_path = destination.relative_to(canvas_root).as_posix()
        remote_size = remote.get("size")
        metadata_unchanged = previous.get("updated_at") == remote.get(
            "updated_at"
        ) and previous.get("size") == remote.get("size")
        oversized_unchanged = (
            previous.get("status") == "oversized"
            and metadata_unchanged
            and previous.get("target_path", previous.get("path")) == relative_path
            and isinstance(remote_size, int)
            and bool(max_bytes)
            and remote_size > max_bytes
        )
        if oversized_unchanged:
            stats["unchanged"] += 1
            next_state["files"][str(file_id)] = previous
            continue
        unchanged = (
            previous.get("status") != "oversized"
            and previous_full is not None
            and previous_full.is_file()
            and metadata_unchanged
            and previous_path is not None
            and previous_path.as_posix() == relative_path
        )
        if unchanged:
            stats["unchanged"] += 1
            next_state["files"][str(file_id)] = previous
            continue

        if (
            previous.get("status") != "oversized"
            and previous_full is not None
            and previous_full.is_file()
            and metadata_unchanged
        ):
            if not quiet:
                print(f"[{course.code}] moved: {previous_path} -> {relative_path}")
            if not dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
                previous_full.replace(destination)
                updated_entry = dict(previous)
                updated_entry.update({"display_name": filename, "path": relative_path})
                next_state["files"][str(file_id)] = updated_entry
            stats["moved"] += 1
            continue

        action = (
            "updated"
            if previous_full is not None and previous_full.is_file()
            else "new"
        )
        if not quiet:
            print(f"[{course.code}] {action}: {relative_path}")
        if isinstance(remote_size, int):
            stats["bytes"] += remote_size
            if max_bytes and remote_size > max_bytes:
                stats["oversized"] += 1
                stats["bytes"] -= remote_size
                oversized_entry = dict(previous)
                oversized_entry.update(
                    {
                        "display_name": filename,
                        "path": (
                            previous.get("path")
                            if previous_full is not None and previous_full.is_file()
                            else relative_path
                        ),
                        "target_path": relative_path,
                        "updated_at": remote.get("updated_at"),
                        "size": remote_size,
                        "status": "oversized",
                    }
                )
                next_state["files"][str(file_id)] = oversized_entry
                if not quiet:
                    print(
                        f"[{course.code}] oversized: {relative_path} "
                        f"({human_size(remote_size)} > {human_size(max_bytes)})"
                    )
                continue
        if dry_run:
            stats[action] += 1
            continue
        url = str(remote.get("url") or "")
        if not url:
            stats["skipped"] += 1
            continue
        digest = client.download(
            url,
            destination,
            expected_size=remote_size if isinstance(remote_size, int) else None,
            max_bytes=max_bytes or None,
        )
        if (
            previous_full is not None
            and previous_full.is_file()
            and previous_full != destination
        ):
            archive_file(
                canvas_root,
                previous_full,
                previous_path,
                file_id,
                archive_directory,
            )
            stats["archived"] += 1
        next_state["files"][str(file_id)] = {
            "display_name": filename,
            "path": relative_path,
            "updated_at": remote.get("updated_at"),
            "size": remote.get("size"),
            "sha256": digest,
        }
        stats[action] += 1

    for file_id_text in sorted(set(old_files).difference(remote_ids)):
        previous = old_files.get(file_id_text, {})
        previous_path_text = previous.get("path")
        if not previous_path_text:
            continue
        previous_path = Path(previous_path_text)
        previous_full = safe_canvas_path(canvas_root, previous_path)
        if any(
            entry.get("path") == previous_path_text
            for entry in next_state["files"].values()
            if isinstance(entry, dict)
        ):
            continue
        if not previous_full.is_file():
            continue
        if not quiet:
            action = "archive" if archive_removed else "leave local copy"
            print(f"[{course.code}] remote file missing; {action}: {previous_path}")
        if archive_removed:
            if not dry_run:
                try:
                    archive_id = int(file_id_text)
                except ValueError:
                    archive_id = 0
                archive_file(
                    canvas_root,
                    previous_full,
                    previous_path,
                    archive_id,
                    archive_directory,
                )
            stats["archived"] += 1
        else:
            next_state["files"][file_id_text] = previous

    if not dry_run:
        canvas_root.mkdir(parents=True, exist_ok=True)
        write_json(state_path, next_state)
        if course.generate_index:
            index_text = render_index(
                course,
                base_url,
                canvas_root,
                assignments,
                announcements,
                dt.datetime.now(dt.timezone.utc),
                material_directory,
                week_directory,
            )
            index_full = canvas_root / index_path
            index_full.parent.mkdir(parents=True, exist_ok=True)
            index_full.write_text(index_text, encoding="utf-8")
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only incremental sync from Canvas to an Obsidian vault."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--vault",
        type=Path,
        help="Override vault_path from the JSON config.",
    )
    parser.add_argument(
        "--course",
        action="append",
        dest="courses",
        help="Course code to sync; repeat for multiple courses. Default: all configured courses.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect Canvas without writing any files.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Show summaries without listing every file.",
    )
    parser.add_argument(
        "--summary-json",
        type=Path,
        help="Write a machine-readable summary for schedulers and notifications.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        layout = config.get("layout", {})
        configured = {
            code.upper(): Course(
                code=code.upper(),
                canvas_id=int(details["canvas_id"]),
                vault_directory=str(details["vault_directory"]),
                sync_directory=str(
                    details.get(
                        "sync_directory", layout.get("sync_directory", "Canvas")
                    )
                ),
                material_directory=str(
                    details.get(
                        "material_directory",
                        layout.get("material_directory", "课程资料"),
                    )
                ),
                week_directory=str(
                    details.get("week_directory", layout.get("week_directory", ""))
                ),
                index_path=str(
                    details.get("index_path", layout.get("index_path", INDEX_NAME))
                ),
                archive_directory=str(
                    details.get(
                        "archive_directory",
                        layout.get("archive_directory", "_Archived"),
                    )
                ),
                preserve_existing_paths=bool(
                    details.get(
                        "preserve_existing_paths",
                        layout.get("preserve_existing_paths", False),
                    )
                ),
                generate_index=bool(
                    details.get("generate_index", layout.get("generate_index", True))
                ),
            )
            for code, details in config["courses"].items()
        }
        requested = (
            [code.upper() for code in args.courses]
            if args.courses
            else list(configured)
        )
        unknown = [code for code in requested if code not in configured]
        if unknown:
            raise SyncError(f"Unknown course code(s): {', '.join(unknown)}")
        token = get_keychain_token(str(config["keychain_service"]))
        client = CanvasClient(str(config["base_url"]), token)
        vault = args.vault or Path(str(config["vault_path"]))
        max_file_size_mb = float(config.get("max_file_size_mb", 500))
        archive_removed = bool(config.get("archive_removed", True))
        mode = "DRY RUN" if args.dry_run else "SYNC"
        print(f"Canvas {mode}: {', '.join(requested)}")
        total = {
            "new": 0,
            "updated": 0,
            "moved": 0,
            "archived": 0,
            "unchanged": 0,
            "oversized": 0,
            "attention_changed": 0,
            "skipped": 0,
            "bytes": 0,
        }
        for code in requested:
            stats = sync_course(
                client,
                configured[code],
                vault.expanduser().resolve(),
                str(config["base_url"]).rstrip("/"),
                args.dry_run,
                args.quiet,
                max_file_size_mb,
                archive_removed,
            )
            for key, value in stats.items():
                total[key] += value
            print(
                f"[{code}] new={stats['new']} updated={stats['updated']} "
                f"moved={stats['moved']} archived={stats['archived']} "
                f"unchanged={stats['unchanged']} oversized={stats['oversized']} "
                f"attention_changed={stats['attention_changed']} "
                f"skipped={stats['skipped']} "
                f"transfer={human_size(stats['bytes'])}"
            )
        print(
            f"Total: new={total['new']} updated={total['updated']} "
            f"moved={total['moved']} archived={total['archived']} "
            f"unchanged={total['unchanged']} oversized={total['oversized']} "
            f"attention_changed={total['attention_changed']} "
            f"skipped={total['skipped']} "
            f"transfer={human_size(total['bytes'])}"
        )
        if args.summary_json:
            write_json(
                args.summary_json,
                {
                    "status": "ok",
                    "courses": requested,
                    "dry_run": args.dry_run,
                    **total,
                },
            )
        return 0
    except SyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
