"""Boot the actual application with an empty, isolated database; no Google calls."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


class AgentLifespanTests(unittest.IsolatedAsyncioTestCase):
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
