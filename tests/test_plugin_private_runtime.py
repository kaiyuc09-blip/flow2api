"""Plugin account API in a temporary database; no browser or network is used."""
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
from src.core.models import Token
from src.services.token_manager import TokenManager


class PluginPrivateRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "private"
        self.directory.mkdir(mode=0o700)
        self.db = Database(str(self.directory / "flow.db"))
        await self.db.init_db()
        self.flow = SimpleNamespace(
            st_to_at=AsyncMock(return_value={"access_token": "synthetic-access", "user": {}}),
            get_credits=AsyncMock(return_value={"credits": 0}),
            create_project=AsyncMock(return_value="synthetic-project"),
        )
        self.manager = TokenManager(self.db, self.flow)
        self.app = FastAPI()
        self.app.include_router(admin.router)
        self.app.state.personal_browser_service = None
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 5000)),
            base_url="http://127.0.0.1:18081",
        )
        self.addAsyncCleanup(self.client.aclose)
        self.settings = {"captcha": {
            "captcha_method": "yescaptcha", "yescaptcha_api_key": "synthetic-test-only",
            "personal_project_pool_size": 1, "browser_count": 1,
            "personal_max_resident_tabs": 1,
        }}
        for replacement in (
            patch.object(config, "_config", self.settings),
            patch.object(admin, "db", self.db),
            patch.object(admin, "token_manager", self.manager),
            patch.object(admin, "concurrency_manager", None),
            patch.object(admin, "active_admin_tokens", {"synthetic-admin-session"}),
            patch.dict(os.environ, {"PERSONAL_BROWSER_USER_DATA_DIR": str(self.directory / "browser-profile")}),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.admin_headers = {"Authorization": "Bearer synthetic-admin-session", "Origin": "http://127.0.0.1:18081"}
        self.plugin_headers = {"Authorization": "Bearer synthetic-plugin-connection"}
        configured = await self.client.post("/api/plugin/config", headers=self.admin_headers, json={
            "connection_token": "synthetic-plugin-connection", "auto_enable_on_update": True,
        })
        self.assertEqual(configured.status_code, 200)

    async def push_account(self, email="plugin@example.invalid", cookies="SID=synthetic-first"):
        return await self.client.post("/api/plugin/update-token", headers=self.plugin_headers,
                                      json={"login_account": email, "google_cookies": cookies})

    async def add_preserved_native_account(self):
        account_id = await self.db.add_token(Token(
            st="synthetic-native-session", at="synthetic-native-access",
            email="native-browser@flow.local", google_cookies="SID=synthetic-native",
            account_source="personal_browser", auto_refresh_enabled=False,
        ))
        return await self.db.get_token(account_id)

    async def test_plugin_config_is_admin_authenticated_and_uses_local_connection_url(self):
        denied = await self.client.get("/api/plugin/config")
        self.assertEqual(denied.status_code, 401)
        response = await self.client.get("/api/plugin/config", headers=self.admin_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"], {
            "connection_token": "synthetic-plugin-connection",
            "connection_url": "http://127.0.0.1:18081/api/plugin/update-token",
            "auto_enable_on_update": True,
        })
        self.flow.st_to_at.assert_not_awaited()

    async def test_plugin_can_create_and_update_account_without_personal_browser(self):
        created = await self.push_account()
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["action"], "added")
        account_id = created.json()["token_id"]
        await self.manager.disable_token(account_id)
        updated = await self.push_account(cookies="SID=synthetic-second")
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["action"], "updated")
        self.assertTrue(updated.json()["auto_enabled"])
        accounts = await self.manager.get_all_tokens()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].id, account_id)
        self.assertEqual(accounts[0].google_cookies, "SID=synthetic-second")
        self.assertTrue(accounts[0].is_active)
        self.assertEqual(accounts[0].account_source, "manual")
        self.assertNotIn("synthetic-first", created.text)
        self.assertNotIn("synthetic-second", updated.text)
        self.assertNotIn("synthetic-access", created.text + updated.text)

    async def test_invalid_plugin_auth_fails_before_account_validation(self):
        response = await self.client.post("/api/plugin/update-token", headers=self.admin_headers,
                                          json={"login_account": "plugin@example.invalid", "google_cookies": "SID=synthetic-first"})
        self.assertEqual(response.status_code, 401)
        self.flow.st_to_at.assert_not_awaited()
        self.assertEqual(await self.manager.get_all_tokens(), [])

    async def test_yescaptcha_can_add_a_different_plugin_account_beside_preserved_native_account(self):
        native = await self.add_preserved_native_account()
        response = await self.push_account()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["action"], "added")
        self.assertEqual(len(await self.manager.get_all_tokens()), 2)
        self.assertEqual((await self.db.get_token(native.id)).model_dump(), native.model_dump())

    async def test_yescaptcha_can_update_plugin_account_beside_preserved_native_account(self):
        created = await self.push_account()
        self.assertEqual(created.status_code, 200, created.text)
        native = await self.add_preserved_native_account()
        response = await self.push_account(cookies="SID=synthetic-second")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["action"], "updated")
        self.assertEqual(len(await self.manager.get_all_tokens()), 2)
        self.assertEqual((await self.db.get_token(native.id)).model_dump(), native.model_dump())
        updated = await self.db.get_token(created.json()["token_id"])
        self.assertEqual(updated.google_cookies, "SID=synthetic-second")

    async def test_plugin_cannot_replace_native_account_with_same_identity(self):
        native = await self.add_preserved_native_account()
        response = await self.push_account(email=native.email)
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await self.db.get_token(native.id)).model_dump(), native.model_dump())

    async def test_personal_mode_keeps_single_native_account_guard(self):
        self.settings["captcha"]["captcha_method"] = "personal"
        native = await self.add_preserved_native_account()
        response = await self.push_account()
        self.assertEqual(response.status_code, 409)
        self.flow.st_to_at.assert_not_awaited()
        self.assertEqual(len(await self.manager.get_all_tokens()), 1)
        self.assertEqual((await self.db.get_token(native.id)).model_dump(), native.model_dump())

    async def test_browser_mode_keeps_native_account_coexistence_guard(self):
        self.settings["captcha"]["captcha_method"] = "browser"
        native = await self.add_preserved_native_account()
        response = await self.push_account()
        self.assertEqual(response.status_code, 409)
        self.flow.st_to_at.assert_not_awaited()
        self.assertEqual(len(await self.manager.get_all_tokens()), 1)
        self.assertEqual((await self.db.get_token(native.id)).model_dump(), native.model_dump())

    async def test_personal_connect_is_not_a_requirement_or_available_in_yescaptcha_mode(self):
        response = await self.client.post("/api/personal-account/connect", headers=self.admin_headers, json={})
        self.assertEqual(response.status_code, 400)
        self.flow.st_to_at.assert_not_awaited()
        self.assertEqual(await self.manager.get_all_tokens(), [])


if __name__ == "__main__":
    unittest.main()
