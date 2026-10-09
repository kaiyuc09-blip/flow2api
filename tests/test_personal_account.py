"""Local authenticated account connection using an isolated synthetic browser."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import admin
from src.core.config import config
from src.core.database import Database
from src.services.token_manager import TokenManager

PROJECT = "12345678-1234-4234-8234-123456789abc"


class PersonalAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name) / "private" / "browser-profile"
        self.profile.mkdir(parents=True)
        self.db = Database(str(Path(self.temp.name) / "flow.db"))
        await self.db.init_db()
        self.flow = SimpleNamespace(
            st_to_at=AsyncMock(return_value={"access_token": "synthetic-access", "user": {}}),
            get_credits=AsyncMock(return_value={"credits": 0}),
            create_project=AsyncMock(side_effect=AssertionError("Must reuse existing project")),
        )
        self.manager = TokenManager(self.db, self.flow)
        self.browser = SimpleNamespace(
            stopped=False,
            config=SimpleNamespace(user_data_dir=str(self.profile)),
            tabs=[SimpleNamespace(url=f"https://flow.google.com/project/{PROJECT}")],
            send=AsyncMock(return_value=[
                {"name": "SID", "value": "synthetic-google-login", "domain": ".google.com", "path": "/", "secure": True},
                {"name": "OSID", "value": "synthetic-flow-login", "domain": "flow.google.com", "path": "/", "secure": True},
                {"name": "unrelated", "value": "synthetic-unrelated", "domain": "other.example", "path": "/"},
            ]),
        )
        self.cookie_reader = self.browser.send
        self.browser.send = AsyncMock(return_value=[])
        self.worker = SimpleNamespace(browser=self.browser, user_data_dir=str(self.profile), headless=False, _initialized=True, _resident_tabs={})
        self.browser.tabs[0].send = self.cookie_reader
        self.app = FastAPI()
        self.app.state.personal_browser_service = self.worker
        self.app.include_router(admin.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 5000)), base_url="http://127.0.0.1:8000")
        self.addAsyncCleanup(self.client.aclose)
        for replacement in (
            patch.dict(os.environ, {"PERSONAL_BROWSER_USER_DATA_DIR": str(self.profile)}),
            patch.dict(config._config, {"captcha": {"captcha_method": "personal", "browser_count": 1, "personal_project_pool_size": 1, "personal_max_resident_tabs": 1}}),
            patch.object(admin, "token_manager", self.manager),
            patch.object(admin, "db", self.db),
            patch.object(admin, "concurrency_manager", None),
            patch.object(admin, "active_admin_tokens", {"synthetic-admin-session"}),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.headers = {"Authorization": "Bearer synthetic-admin-session", "Origin": "http://127.0.0.1:8000"}

    async def test_connection_requires_local_authenticated_same_origin_action(self):
        response = await self.client.post("/api/personal-account/connect", json={})
        self.assertEqual(response.status_code, 401)
        response = await self.client.post("/api/personal-account/connect", json={}, headers={**self.headers, "Origin": "https://other.example"})
        self.assertEqual(response.status_code, 403)
        self.cookie_reader.assert_not_awaited()

    async def test_connects_logged_in_dedicated_profile_without_exposing_credentials(self):
        self.flow.st_to_at.return_value["user"] = {"email": "synthetic-private@example.com", "name": "Synthetic Private Name"}
        response = await self.client.post("/api/personal-account/connect", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {"success", "status", "account_id"})
        self.assertEqual(response.json()["status"], "connected")
        self.flow.create_project.assert_not_awaited()
        listed = await self.client.get("/api/tokens", headers=self.headers)
        self.assertEqual(len(listed.json()), 1)
        row = listed.json()[0]
        self.assertEqual(row["id"], response.json()["account_id"])
        self.assertTrue(row["personal_browser"])
        self.assertEqual(row["google_cookies"], "")
        self.assertEqual(row["email"], "")
        self.assertNotIn("synthetic-google-login", response.text + listed.text)
        self.assertNotIn("synthetic-flow-login", response.text + listed.text)
        self.assertNotIn("synthetic-private@example.com", response.text + listed.text)
        stored = await self.manager.get_token(response.json()["account_id"])
        self.assertEqual(stored.email, "native-browser@flow.local")
        command = next(self.cookie_reader.await_args.args[0])
        self.assertEqual(command["method"], "Network.getCookies")
        self.assertEqual(command["params"]["urls"], ["https://flow.google.com/", "https://www.google.com/"])
        uploaded = self.flow.st_to_at.await_args.kwargs["google_cookies"]
        self.assertIn("synthetic-flow-login", uploaded)
        self.assertNotIn("synthetic-unrelated", uploaded)

    async def test_native_account_cannot_be_overwritten_by_ordinary_edit(self):
        response = await self.client.post("/api/personal-account/connect", json={}, headers=self.headers)
        account_id = response.json()["account_id"]
        response = await self.client.put(f"/api/tokens/{account_id}", json={
            "remark": "manual", "account_source": "manual", "google_cookies": "replacement"
        }, headers=self.headers)
        self.assertEqual(response.status_code, 409, response.text)
        stored = await self.manager.get_token(account_id)
        self.assertEqual(stored.account_source, "personal_browser")
        self.assertIn("synthetic-flow-login", stored.google_cookies)
        self.flow.st_to_at.assert_awaited_once()
        response = await self.client.post(f"/api/tokens/{account_id}/disable", headers=self.headers)
        self.assertEqual(response.status_code, 200)

    async def test_native_connection_prevents_adding_a_second_account(self):
        await self.client.post("/api/personal-account/connect", json={}, headers=self.headers)
        response = await self.client.post("/api/tokens", json={
            "login_account": "synthetic@example.com", "google_cookies": "synthetic-second-login"
        }, headers=self.headers)
        self.assertEqual(response.status_code, 409, response.text)
        self.flow.st_to_at.assert_awaited_once()
        self.assertEqual(len(await self.manager.get_all_tokens()), 1)

    async def test_native_account_never_creates_extra_projects_after_config_change(self):
        response = await self.client.post("/api/personal-account/connect", json={}, headers=self.headers)
        with patch.dict(config._config["captcha"], {"personal_project_pool_size": 4}):
            selected = await self.manager.ensure_project_exists(response.json()["account_id"])
        self.assertEqual(selected, PROJECT)
        self.flow.create_project.assert_not_awaited()

    async def test_default_missing_and_mismatched_profiles_fail_before_cookie_read(self):
        candidates = ["", str(Path.home() / "Library/Application Support/Google/Chrome"), str(self.profile / "other")]
        for candidate in candidates:
            with self.subTest(candidate=candidate), patch.dict(os.environ, {"PERSONAL_BROWSER_USER_DATA_DIR": candidate}):
                response = await self.client.post("/api/personal-account/connect", json={}, headers=self.headers)
                self.assertEqual(response.status_code, 400)
        self.cookie_reader.assert_not_awaited()

    async def test_only_existing_google_project_and_single_account_are_accepted(self):
        self.browser.tabs = [SimpleNamespace(url=f"https://flow.google.com.evil.example/project/{PROJECT}")]
        response = await self.client.post("/api/personal-account/connect", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.cookie_reader.assert_not_awaited()
        self.browser.tabs = [SimpleNamespace(url="https://accounts.google.com/", send=self.cookie_reader)]
        response = await self.client.post("/api/personal-account/connect", json={"project_id": PROJECT}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.cookie_reader.reset_mock()
        response = await self.client.post("/api/personal-account/connect", json={"project_id": PROJECT}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.cookie_reader.assert_not_awaited()
        self.flow.create_project.assert_not_awaited()

    async def test_cookie_read_failure_has_fixed_safe_error(self):
        self.cookie_reader.side_effect = RuntimeError("synthetic-private-cookie-value")
        response = await self.client.post("/api/personal-account/connect", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("synthetic-private-cookie-value", response.text)
        self.assertEqual(await self.manager.get_all_tokens(), [])

    async def test_default_context_id_is_allowed_but_incognito_is_not(self):
        self.browser.tabs[0].target = SimpleNamespace(browser_context_id="synthetic-incognito")
        self.browser.send.return_value = ["synthetic-incognito"]
        response = await self.client.post("/api/personal-account/connect", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.cookie_reader.assert_not_awaited()
        self.browser.tabs[0].target.browser_context_id = "synthetic-default"
        response = await self.client.post("/api/personal-account/connect", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)

    async def test_nonlocal_client_and_multiple_browsers_are_rejected(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=("192.0.2.5", 5000)), base_url="http://127.0.0.1:8000") as remote:
            response = await remote.post("/api/personal-account/connect", json={}, headers=self.headers)
            self.assertEqual(response.status_code, 403)
        with patch.dict(config._config["captcha"], {"browser_count": 2}):
            response = await self.client.post("/api/personal-account/connect", json={}, headers=self.headers)
            self.assertEqual(response.status_code, 400)
        self.cookie_reader.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
