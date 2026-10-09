import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from scripts.run_native import ROOT, prepare_private_runtime
from src.services.native_runtime import validate_native_runtime_config, validate_personal_browser


class NativeLauncherTests(unittest.TestCase):
    def test_preparation_is_private_and_keeps_existing_credentials(self):
        with tempfile.TemporaryDirectory() as base:
            private = Path(base) / "private"
            path, first = prepare_private_runtime(private)
            _, second = prepare_private_runtime(private)
            self.assertTrue(first == second)
            self.assertTrue((path / "api-key.txt").read_text().strip() == first["api_key"])
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
                self.assertEqual((path / "service-credentials.json").stat().st_mode & 0o777, 0o600)

    def test_refuses_to_put_credentials_inside_repository(self):
        with self.assertRaises(ValueError):
            prepare_private_runtime(ROOT / "private")

    def test_mismatching_existing_key_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as base:
            private, _ = prepare_private_runtime(Path(base) / "private")
            key = private / "api-key.txt"
            key.write_text("synthetic-mismatch")
            with self.assertRaises(ValueError):
                prepare_private_runtime(private)
            self.assertEqual(key.read_text(), "synthetic-mismatch")

    def test_refuses_unrelated_existing_directory(self):
        with tempfile.TemporaryDirectory() as base:
            directory = Path(base)
            document = directory / "existing-document.txt"
            document.write_text("user data")
            with self.assertRaises(ValueError):
                prepare_private_runtime(directory)
            self.assertEqual(document.read_text(), "user data")
            self.assertFalse((directory / "service-credentials.json").exists())

    def test_refuses_database_sidecar_and_log_symlinks_without_touching_targets(self):
        for name in ("flow.db", "flow.db-wal", "flow.db-shm", "flow.db-journal", "service.log"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as base:
                private, _ = prepare_private_runtime(Path(base) / "private")
                target = Path(base) / "user-file"
                target.write_text("keep this")
                (private / name).symlink_to(target)
                mode = target.stat().st_mode
                with self.assertRaises(ValueError):
                    prepare_private_runtime(private)
                self.assertEqual(target.read_text(), "keep this")
                self.assertEqual(target.stat().st_mode, mode)

    def test_private_credentials_local_binding_and_single_worker_are_required(self):
        credentials = {"admin_username": "local", "admin_password": "synthetic-password", "api_key": "synthetic-key"}
        settings = {**credentials, "server_host": "127.0.0.1"}
        validate_native_runtime_config(SimpleNamespace(**settings), credentials)
        for key, value in {"api_key": "different", "admin_password": "different", "admin_username": "different",
                           "server_host": "0.0.0.0"}.items():
            with self.subTest(key=key), self.assertRaises(RuntimeError) as caught:
                validate_native_runtime_config(SimpleNamespace(**{**settings, key: value}), credentials)
            self.assertNotIn("synthetic", str(caught.exception))
            self.assertNotIn("different", str(caught.exception))
        for workers in (0, 2, True, "1"):
            with self.subTest(workers=workers), self.assertRaises(RuntimeError):
                validate_native_runtime_config(SimpleNamespace(**settings), credentials, workers=workers)

    def test_saved_captcha_mode_and_browser_capacity_are_not_overridden(self):
        credentials = {"admin_username": "local", "admin_password": "synthetic-password", "api_key": "synthetic-key"}
        for method in ("personal", "yescaptcha", "capmonster", "ezcaptcha", "capsolver", "captcha_run"):
            settings = {**credentials, "server_host": "127.0.0.1", "captcha_method": method,
                        "browser_count": 2, "personal_project_pool_size": 4, "personal_max_resident_tabs": 5}
            original = settings.copy()
            validate_native_runtime_config(SimpleNamespace(**settings), credentials)
            self.assertEqual(settings, original)

    def test_only_personal_mode_requires_an_existing_browser(self):
        with tempfile.TemporaryDirectory() as base:
            missing = Path(base) / "missing-browser"
            for method in ("yescaptcha", "capmonster", "ezcaptcha", "capsolver", "captcha_run"):
                validate_personal_browser(SimpleNamespace(captcha_method=method), missing)
            with self.assertRaisesRegex(RuntimeError, "Chrome"):
                validate_personal_browser(SimpleNamespace(captcha_method="personal"), missing)
            installed = Path(base) / "synthetic-browser"
            installed.touch()
            validate_personal_browser(SimpleNamespace(captcha_method="personal"), installed)
