"""Private launcher mode selection against isolated configuration and databases."""
from contextlib import nullcontext
import copy
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import run_native


class PrivateRuntimeModeTests(unittest.TestCase):
    def test_cli_defers_browser_requirement_until_database_mode_is_known(self):
        for background in (False, True):
            with self.subTest(background=background), tempfile.TemporaryDirectory() as base:
                args = ["run_native.py", "--private-dir", str(Path(base) / "private"),
                        "--browser", str(Path(base) / "not-installed")]
                if background:
                    args.append("--background")
                with patch.object(sys, "argv", args), \
                     patch.object(os, "umask"), \
                     patch.object(run_native, "acquire_runtime_lock", return_value=nullcontext()), \
                     patch.object(run_native, "ensure_port_available"), \
                     patch.object(run_native, "run_service") as foreground, \
                     patch.object(run_native, "start_background", return_value=123) as detached:
                    run_native.main()
                self.assertEqual(foreground.call_count, int(not background))
                self.assertEqual(detached.call_count, int(background))

    def test_run_service_leaves_all_captcha_settings_intact(self):
        settings = {"global": {}, "server": {}, "captcha": {
            "captcha_method": "yescaptcha", "yescaptcha_api_key": "synthetic-test-key",
            "browser_count": 3, "personal_max_resident_tabs": 4, "personal_project_pool_size": 5}}
        captcha = copy.deepcopy(settings["captcha"])
        config = SimpleNamespace(get_raw_config=lambda: settings)
        app = SimpleNamespace(state=SimpleNamespace())
        server = Mock()
        modules = {"src.core.config": SimpleNamespace(config=config), "src.main": SimpleNamespace(app=app),
                   "uvicorn": SimpleNamespace(run=server)}
        credentials = {"admin_username": "local", "admin_password": "synthetic-password", "api_key": "synthetic-key"}
        with tempfile.TemporaryDirectory() as base, patch.dict(sys.modules, modules), \
             patch.dict(os.environ), patch.object(os, "chdir"), patch.object(sys, "path", list(sys.path)):
            args = SimpleNamespace(port=8765, browser=Path(base) / "missing-browser")
            run_native.run_service(Path(base), credentials, args)
        self.assertEqual(settings["captcha"], captcha)
        self.assertEqual(settings["server"], {"host": "127.0.0.1", "port": 8765})
        self.assertEqual(app.state.native_launch_workers, 1)
        self.assertEqual(app.state.native_launch_browser_path, args.browser)
        server.assert_called_once_with(app, host="127.0.0.1", port=8765, workers=1, access_log=False)


class PrivateRuntimeLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_third_party_startup_keeps_saved_mode_key_and_retries_without_browser(self):
        from src import main

        settings = copy.deepcopy(main.config.get_raw_config())
        credentials = {"admin_username": "local", "admin_password": "synthetic-password", "api_key": "synthetic-key"}
        settings["global"].update(credentials)
        settings["server"]["host"] = "127.0.0.1"
        settings["flow"]["max_retries"] = 3
        settings["captcha"].update(captcha_method="yescaptcha", yescaptcha_api_key="synthetic-test-key")
        forbidden = Mock(side_effect=AssertionError("Third-party mode must not initialize a browser"))
        browser_modules = {name: SimpleNamespace(BrowserCaptchaService=SimpleNamespace(get_instance=forbidden))
                           for name in ("src.services.browser_captcha_personal", "src.services.browser_captcha")}
        with tempfile.TemporaryDirectory() as base:
            database = str(Path(base) / "flow.db")
            with patch.object(main.config, "_config", settings), \
                 patch.object(main.config, "_admin_username", None), \
                 patch.object(main.config, "_admin_password", None), \
                 patch.object(main.db, "db_path", database), \
                 patch.object(main.agent_jobs, "db_path", database), \
                 patch.object(main.app.state, "native_launch_credentials", credentials, create=True), \
                 patch.object(main.app.state, "native_launch_workers", 1, create=True), \
                 patch.object(main.app.state, "native_launch_browser_path", Path(base) / "missing-browser", create=True), \
                 patch.dict(sys.modules, browser_modules):
                async with main.lifespan(main.app):
                    self.assertIsNone(main.app.state.personal_browser_service)
                    self.assertEqual(main.config.flow_max_retries, 2)
                    captcha = await main.db.get_captcha_config()
                    self.assertEqual(captcha.captcha_method, "yescaptcha")
                    self.assertTrue(captcha.yescaptcha_api_key == "synthetic-test-key")
                await main.db.update_generation_config(max_retries=5)
                # Conflicting TOML-like settings must not replace persisted configuration.
                settings["captcha"].update(captcha_method="personal", yescaptcha_api_key="synthetic-other-key")
                settings["flow"]["max_retries"] = 3
                async with main.lifespan(main.app):
                    self.assertIsNone(main.app.state.personal_browser_service)
                    self.assertEqual(main.config.flow_max_retries, 5)
                    self.assertEqual(main.config.captcha_method, "yescaptcha")
                    self.assertTrue(main.config.yescaptcha_api_key == "synthetic-test-key")
                forbidden.assert_not_called()
