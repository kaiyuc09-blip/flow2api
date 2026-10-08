"""Persistent dedicated profiles survive shutdown and recovery without a browser."""
import builtins
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.services import browser_captcha_personal as personal
from src.services.personal_account import PersonalAccountError


class PersonalProfileLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_and_recovery_preserve_explicit_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder).resolve() / "private-profile"
            profile.mkdir()
            marker = profile / "synthetic-profile-marker"
            marker.write_text("synthetic-only")
            with patch.dict(os.environ, {"PERSONAL_BROWSER_USER_DATA_DIR": str(profile)}):
                service = personal.BrowserCaptchaService()
                await service.close()
                self.assertEqual(Path(service.user_data_dir), profile)
                self.assertTrue(marker.exists())
                await service._purge_runtime_profile_dirs("test-recovery")
                self.assertEqual(Path(service.user_data_dir), profile)
                self.assertTrue(marker.exists())
                self.assertEqual(service._collect_runtime_profile_cleanup_targets(), [])

    async def test_daily_chrome_profile_is_rejected_before_browser_start(self):
        with patch.dict(os.environ, {"PERSONAL_BROWSER_USER_DATA_DIR": str(Path.home() / "Library/Application Support/Google/Chrome")}):
            with self.assertRaises(PersonalAccountError):
                personal.BrowserCaptchaService()

    async def test_missing_dependency_never_installs_packages(self):
        original_import = builtins.__import__
        def fake_import(name, *args, **kwargs):
            if name == "nodriver":
                raise ImportError("synthetic missing dependency")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=fake_import), patch.object(personal, "_run_pip_install") as install:
            self.assertFalse(personal._ensure_nodriver_installed())
            install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
