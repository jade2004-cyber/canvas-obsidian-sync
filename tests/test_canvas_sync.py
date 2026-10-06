import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import canvas_sync
import canvas_scheduled_sync
import manager
import setup


class CanvasSyncTests(unittest.TestCase):
    @staticmethod
    def _course_fixture(root: Path):
        vault = root / "vault"
        course_dir = vault / "COURSE101"
        canvas_root = course_dir / "Canvas"
        canvas_root.mkdir(parents=True)
        course = canvas_sync.Course("COURSE101", 123, "COURSE101")
        return vault, canvas_root, course

    def test_parse_next_link(self):
        value = '<https://example.test/api?page=2>; rel="next", <https://example.test/api?page=3>; rel="last"'
        self.assertEqual(
            canvas_sync.parse_next_link(value), "https://example.test/api?page=2"
        )

    def test_week_number(self):
        self.assertEqual(canvas_sync.week_number("Course files/Week 7"), 7)
        self.assertEqual(canvas_sync.week_number("Lec10 confidence intervals.pdf"), 10)
        self.assertEqual(canvas_sync.week_number("L03_Rintro.Rmd"), 3)
        self.assertIsNone(canvas_sync.week_number("Syllabus.pdf"))

    def test_choose_section(self):
        self.assertEqual(
            canvas_sync.choose_section("slides.pdf", "course files/Week 03"),
            "Week 03",
        )
        self.assertEqual(canvas_sync.choose_section("Course Outline.pdf"), "课程资料")
        self.assertEqual(
            canvas_sync.choose_section(
                "COURSE101 Course Outline.pdf", "course files/Week 01"
            ),
            "课程资料",
        )
        self.assertEqual(
            canvas_sync.choose_section(
                "Lecture 1 introduction.pdf", "course files/Course Information"
            ),
            "Week 01",
        )
        self.assertEqual(
            canvas_sync.choose_section("data.xlsx", "", ["Week 9 exercises"]),
            "Week 09",
        )

    def test_sanitize_filename(self):
        self.assertEqual(
            canvas_sync.sanitize_filename(" A/B\nC.pdf ", "x"), "A-B C.pdf"
        )
        self.assertEqual(canvas_sync.sanitize_filename("...", "fallback"), "fallback")

    def test_human_size(self):
        self.assertEqual(canvas_sync.human_size(1024), "1.0 KB")

    def test_unique_destination_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "slides.pdf").write_bytes(b"existing")
            result = canvas_sync.unique_destination(directory, "slides.pdf", 42)
            self.assertEqual(result.name, "slides [Canvas 42].pdf")

    def test_scheduled_sync_iso_date(self):
        self.assertEqual(
            canvas_scheduled_sync.iso_date("2030-11-28").isoformat(),
            "2030-11-28",
        )

    def test_retry_delay_honors_retry_after(self):
        error = canvas_sync.TransientCanvasError("limited", retry_after=17)
        self.assertEqual(canvas_sync.retry_delay(error, 0), 17)

    def test_retry_delay_caps_server_value(self):
        error = canvas_sync.TransientCanvasError("limited", retry_after=999)
        self.assertEqual(canvas_sync.retry_delay(error, 0), 120)

    def test_download_promotes_already_complete_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "slides.pdf"
            partial = destination.with_name(".slides.pdf.canvas-download")
            content = b"already complete"
            partial.write_bytes(content)
            client = canvas_sync.CanvasClient("https://example.test", "token")

            with mock.patch.object(client, "_open") as open_request:
                digest = client.download(
                    "https://example.test/file",
                    destination,
                    expected_size=len(content),
                )

            open_request.assert_not_called()
            self.assertEqual(destination.read_bytes(), content)
            self.assertFalse(partial.exists())
            self.assertEqual(digest, hashlib.sha256(content).hexdigest())

    def test_safe_canvas_path_rejects_parent_traversal(self):
        with self.assertRaises(canvas_sync.SyncError):
            canvas_sync.safe_canvas_path(Path("/tmp/vault"), Path("../secret"))

    def test_archive_file_preserves_local_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Week 01" / "slides.pdf"
            source.parent.mkdir()
            source.write_bytes(b"slides")
            archived = canvas_sync.archive_file(
                root, source, Path("Week 01/slides.pdf"), 42
            )
            self.assertFalse(source.exists())
            self.assertEqual(archived.read_bytes(), b"slides")
            self.assertIn("_Archived", archived.parts)

    def test_notification_is_quiet_when_unchanged(self):
        summary = {"unchanged": 12, "new": 0, "updated": 0}
        self.assertEqual(canvas_scheduled_sync.change_count(summary), 0)

    def test_notification_message_summarizes_changes(self):
        summary = {"new": 2, "updated": 1, "archived": 1}
        self.assertEqual(
            canvas_scheduled_sync.notification_message("COURSE101", summary),
            "COURSE101: 2 new, 1 updated, 1 archived",
        )

    def test_attention_fingerprint_detects_due_date_change(self):
        original = canvas_sync.attention_fingerprint(
            [{"id": 1, "name": "Homework", "due_at": "2030-01-01T10:00:00Z"}],
            [],
        )
        changed = canvas_sync.attention_fingerprint(
            [{"id": 1, "name": "Homework", "due_at": "2030-01-02T10:00:00Z"}],
            [],
        )
        self.assertNotEqual(original, changed)

    def test_parse_meetings(self):
        self.assertEqual(
            setup.parse_meetings("Mon 09:00, Wed 19:00"),
            [(1, 9, 0), (3, 19, 0)],
        )
        self.assertEqual(
            setup.parse_meetings("周一 09:00，周三 19:00"), [(1, 9, 0), (3, 19, 0)]
        )

    def test_lead_time_can_cross_midnight(self):
        self.assertEqual(setup.subtract_lead_time((1, 0, 30), 45), (0, 23, 45))

    def test_is_inside_documents_style_path(self):
        self.assertTrue(
            setup.is_inside(Path("/tmp/Documents/Vault"), Path("/tmp/Documents"))
        )
        self.assertFalse(
            setup.is_inside(Path("/tmp/Other/Vault"), Path("/tmp/Documents"))
        )

    def test_plist_payload_supports_multiple_meetings(self):
        payload = setup.plist_payload(
            "com.example.test",
            Path("/usr/bin/python3"),
            "COURSE101",
            "2030-01-01",
            "2030-04-01",
            [(1, 8, 15), (3, 8, 15)],
        )
        self.assertEqual(len(payload["StartCalendarInterval"]), 2)

    def test_next_scheduled_run(self):
        now = canvas_sync.dt.datetime.fromisoformat("2030-01-07T08:00:00+08:00")
        result = manager.next_scheduled_run(
            [[1, 8, 15], [3, 8, 15]], "2030-01-01", "2030-04-01", now
        )
        self.assertEqual(result.isoformat(), "2030-01-07T08:15:00+08:00")

    def test_record_status_writes_course_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            status_path = Path(temporary) / "status.json"
            with mock.patch("canvas_scheduled_sync.STATUS_PATH", status_path):
                canvas_scheduled_sync.record_status("COURSE101", "success", {"new": 2})
            status = setup.read_json(status_path)
            self.assertEqual(status["courses"]["COURSE101"]["result"], "success")
            self.assertEqual(status["courses"]["COURSE101"]["summary"]["new"], 2)

    def test_installer_does_not_load_jobs_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_dir = root / "app"
            launch_agents = root / "agents"
            config = {
                "base_url": "https://example.test",
                "keychain_service": "test-service",
                "vault_path": str(root / "vault"),
                "courses": {
                    "COURSE101": {
                        "canvas_id": 123,
                        "vault_directory": "COURSE101",
                    }
                },
            }
            schedules = {"COURSE101": [(1, 8, 15)]}
            details = {
                "lead_minutes": 45,
                "class_times": {"COURSE101": [[1, 9, 0]]},
            }
            validation_error = subprocess.CalledProcessError(1, ["canvas_sync.py"])
            config_path = app_dir / "canvas_courses.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("original configuration", encoding="utf-8")

            with (
                mock.patch("setup.platform.system", return_value="Darwin"),
                mock.patch(
                    "setup.collect_settings",
                    return_value=(
                        config,
                        schedules,
                        "2030-01-01",
                        "2030-04-01",
                        details,
                    ),
                ),
                mock.patch("setup.ensure_keychain_token"),
                mock.patch("setup.is_inside", return_value=False),
                mock.patch("setup.APP_DIR", app_dir),
                mock.patch("setup.CONFIG_PATH", config_path),
                mock.patch("setup.LOG_DIR", root / "logs"),
                mock.patch("setup.LAUNCH_AGENTS", launch_agents),
                mock.patch("setup.MANIFEST", app_dir / "install-manifest.json"),
                mock.patch("setup.subprocess.run", side_effect=validation_error),
                mock.patch("setup.launchctl") as launchctl,
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    setup.install()

            launchctl.assert_not_called()
            self.assertEqual(
                config_path.read_text(encoding="utf-8"), "original configuration"
            )
            self.assertFalse((app_dir / ".canvas_courses.candidate.json").exists())

    def test_successful_install_writes_v2_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_dir = root / "app"
            config_path = app_dir / "canvas_courses.json"
            manifest_path = app_dir / "install-manifest.json"
            launch_agents = root / "agents"
            config = {
                "base_url": "https://example.test",
                "keychain_service": "test-service",
                "vault_path": str(root / "vault"),
                "courses": {
                    "COURSE101": {
                        "canvas_id": 123,
                        "vault_directory": "COURSE101",
                    }
                },
            }
            schedules = {"COURSE101": [(1, 8, 15)]}
            details = {
                "lead_minutes": 45,
                "class_times": {"COURSE101": [[1, 9, 0]]},
            }

            with (
                mock.patch("setup.platform.system", return_value="Darwin"),
                mock.patch(
                    "setup.collect_settings",
                    return_value=(
                        config,
                        schedules,
                        "2030-01-01",
                        "2030-04-01",
                        details,
                    ),
                ),
                mock.patch("setup.ensure_keychain_token"),
                mock.patch("setup.is_inside", return_value=False),
                mock.patch("setup.APP_DIR", app_dir),
                mock.patch("setup.LOG_DIR", root / "logs"),
                mock.patch("setup.LAUNCH_AGENTS", launch_agents),
                mock.patch("setup.MANIFEST", manifest_path),
                mock.patch("setup.CONFIG_PATH", config_path),
                mock.patch(
                    "setup.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
                mock.patch("setup.launchctl") as launchctl,
            ):
                setup.install()

            manifest = setup.read_json(manifest_path)
            self.assertEqual(manifest["version"], 2)
            self.assertEqual(manifest["lead_minutes"], 45)
            self.assertEqual(
                manifest["courses"]["COURSE101"]["intervals"], [[1, 8, 15]]
            )
            self.assertEqual(setup.read_json(config_path)["courses"], config["courses"])
            self.assertTrue(launchctl.called)

    def test_sync_moves_renamed_file_without_downloading(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault, canvas_root, course = self._course_fixture(Path(temporary))
            old_file = canvas_root / "Week 01" / "Lecture 1.pdf"
            old_file.parent.mkdir()
            old_file.write_bytes(b"slides")
            canvas_sync.write_json(
                canvas_root / canvas_sync.STATE_NAME,
                {
                    "files": {
                        "1": {
                            "path": "Week 01/Lecture 1.pdf",
                            "display_name": "Lecture 1.pdf",
                            "updated_at": "2030-01-01T00:00:00Z",
                            "size": 6,
                            "sha256": "oldhash",
                        }
                    }
                },
            )
            data = (
                {"name": "Course"},
                [
                    {
                        "id": 1,
                        "display_name": "Lecture 2.pdf",
                        "updated_at": "2030-01-01T00:00:00Z",
                        "size": 6,
                        "folder_id": 10,
                        "url": "https://example.test/file",
                    }
                ],
                [{"id": 10, "full_name": "Week 02"}],
                [],
                [],
                [],
            )
            client = mock.Mock()
            with mock.patch("canvas_sync.collect_course_data", return_value=data):
                stats = canvas_sync.sync_course(
                    client,
                    course,
                    vault,
                    "https://example.test",
                    False,
                    True,
                    500,
                    True,
                )
            self.assertEqual(stats["moved"], 1)
            self.assertFalse(old_file.exists())
            self.assertTrue((canvas_root / "Week 02" / "Lecture 2.pdf").exists())
            client.download.assert_not_called()

    def test_sync_archives_file_removed_from_canvas(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault, canvas_root, course = self._course_fixture(Path(temporary))
            old_file = canvas_root / "Week 01" / "slides.pdf"
            old_file.parent.mkdir()
            old_file.write_bytes(b"slides")
            canvas_sync.write_json(
                canvas_root / canvas_sync.STATE_NAME,
                {"files": {"1": {"path": "Week 01/slides.pdf"}}},
            )
            data = ({"name": "Course"}, [], [], [], [], [])
            with mock.patch("canvas_sync.collect_course_data", return_value=data):
                stats = canvas_sync.sync_course(
                    mock.Mock(),
                    course,
                    vault,
                    "https://example.test",
                    False,
                    True,
                    500,
                    True,
                )
            self.assertEqual(stats["archived"], 1)
            self.assertFalse(old_file.exists())
            archived = list((canvas_root / "_Archived").rglob("slides.pdf"))
            self.assertEqual(len(archived), 1)

    def test_sync_skips_file_above_size_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault, _canvas_root, course = self._course_fixture(Path(temporary))
            data = (
                {"name": "Course"},
                [
                    {
                        "id": 1,
                        "display_name": "large.zip",
                        "updated_at": "2030-01-01T00:00:00Z",
                        "size": 2 * 1024 * 1024,
                        "folder_id": 10,
                        "url": "https://example.test/file",
                    }
                ],
                [{"id": 10, "full_name": "Course Files"}],
                [],
                [],
                [],
            )
            client = mock.Mock()
            with mock.patch("canvas_sync.collect_course_data", return_value=data):
                stats = canvas_sync.sync_course(
                    client, course, vault, "https://example.test", False, True, 1, True
                )
            self.assertEqual(stats["oversized"], 1)
            client.download.assert_not_called()

    def test_sync_does_not_repeat_unchanged_oversized_alert(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault, _canvas_root, course = self._course_fixture(Path(temporary))
            data = (
                {"name": "Course"},
                [
                    {
                        "id": 1,
                        "display_name": "large.zip",
                        "updated_at": "2030-01-01T00:00:00Z",
                        "size": 2 * 1024 * 1024,
                        "folder_id": 10,
                        "url": "https://example.test/file",
                    }
                ],
                [{"id": 10, "full_name": "Course Files"}],
                [],
                [],
                [],
            )
            client = mock.Mock()
            with mock.patch("canvas_sync.collect_course_data", return_value=data):
                first = canvas_sync.sync_course(
                    client, course, vault, "https://example.test", False, True, 1, True
                )
                second = canvas_sync.sync_course(
                    client, course, vault, "https://example.test", False, True, 1, True
                )

            self.assertEqual(first["oversized"], 1)
            self.assertEqual(second["oversized"], 0)
            self.assertEqual(second["unchanged"], 1)
            client.download.assert_not_called()

    def test_sync_downloads_oversized_update_after_limit_is_increased(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault, canvas_root, course = self._course_fixture(Path(temporary))
            local_file = canvas_root / "课程资料" / "large.zip"
            local_file.parent.mkdir()
            local_file.write_bytes(b"old")
            canvas_sync.write_json(
                canvas_root / canvas_sync.STATE_NAME,
                {
                    "files": {
                        "1": {
                            "display_name": "large.zip",
                            "path": "课程资料/large.zip",
                            "updated_at": "2029-01-01T00:00:00Z",
                            "size": 3,
                            "sha256": "oldhash",
                        }
                    }
                },
            )
            remote_size = 2 * 1024 * 1024
            data = (
                {"name": "Course"},
                [
                    {
                        "id": 1,
                        "display_name": "large.zip",
                        "updated_at": "2030-01-01T00:00:00Z",
                        "size": remote_size,
                        "folder_id": 10,
                        "url": "https://example.test/file",
                    }
                ],
                [{"id": 10, "full_name": "Course Files"}],
                [],
                [],
                [],
            )
            client = mock.Mock()

            def download(_url, destination, **_kwargs):
                destination.write_bytes(b"new")
                return "newhash"

            client.download.side_effect = download
            with mock.patch("canvas_sync.collect_course_data", return_value=data):
                limited = canvas_sync.sync_course(
                    client, course, vault, "https://example.test", False, True, 1, True
                )
                allowed = canvas_sync.sync_course(
                    client, course, vault, "https://example.test", False, True, 3, True
                )

            self.assertEqual(limited["oversized"], 1)
            self.assertEqual(allowed["updated"], 1)
            self.assertEqual(allowed["moved"], 0)
            self.assertEqual(local_file.read_bytes(), b"new")
            client.download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
