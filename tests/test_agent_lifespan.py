"""Boot the actual application with an empty, isolated database; no Google calls."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import httpx


class AgentLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_launch_reloads_private_config_and_rejects_changed_key_before_browser(self):
        from src import main
        from src.services.browser_captcha_personal import BrowserCaptchaService

        settings = copy.deepcopy(main.config.get_raw_config())
        credentials = {"admin_username": "local", "admin_password": "synthetic-password", "api_key": "synthetic-key"}
        settings["global"].update(credentials)
        settings["captcha"].update(captcha_method="personal", browser_count=1,
            personal_project_pool_size=1, personal_max_resident_tabs=1)
        browser = AsyncMock()
        browser.warmup_resident_tabs.return_value = []
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "flow.db")
            with patch.object(main.config, "_config", settings), \
                 patch.object(main.config, "_admin_username", None), \
                 patch.object(main.config, "_admin_password", None), \
                 patch.object(main.db, "db_path", database), \
                 patch.object(main.agent_jobs, "db_path", database), \
                 patch.object(main.app.state, "native_launch_credentials", credentials, create=True), \
                 patch.object(BrowserCaptchaService, "get_instance", new=AsyncMock(return_value=browser)) as get_browser:
                async with main.lifespan(main.app):
                    self.assertIs(main.app.state.personal_browser_service, browser)
                    self.assertEqual(main.config.api_key, credentials["api_key"])
                browser.open_login_window.assert_awaited_once()
                self.assertIsNone(main.app.state.personal_browser_service)
                await main.db.update_admin_config(api_key="synthetic-changed-key")
                with self.assertRaisesRegex(RuntimeError, "database credentials differ"):
                    async with main.lifespan(main.app):
                        self.fail("A changed credential must stop startup")
                self.assertEqual(get_browser.await_count, 1)

    async def test_real_app_starts_agent_api_and_stops_cleanly_without_accounts(self):
        from src import main

        settings = copy.deepcopy(main.config.get_raw_config())
        settings.setdefault("captcha", {})["captcha_method"] = "yescaptcha"
        # These are synthetic test credentials, never a Google account.
        settings["captcha"]["yescaptcha_api_key"] = ""
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "flow.db")
            with patch.object(main.config, "_config", settings), \
                 patch.object(main.config, "_admin_username", None), \
                 patch.object(main.config, "_admin_password", None), \
                 patch.object(main.db, "db_path", database), \
                 patch.object(main.agent_jobs, "db_path", database):
                async with main.lifespan(main.app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver") as client:
                        missing = await client.get("/v1/agent/models")
                        self.assertEqual(missing.status_code, 401)
                        with patch("src.api.agent.AuthManager.verify_api_key", return_value=True):
                            response = await client.get("/v1/agent/models", headers={"Authorization": "Bearer test-only"})
                        self.assertEqual(response.status_code, 200)
                        self.assertTrue(response.json()["data"])
                        self.assertFalse(any(item["available"] for item in response.json()["data"]))
                    self.assertEqual(await main.token_manager.get_all_tokens(), [])
                self.assertFalse(main.agent_jobs._accepting)
