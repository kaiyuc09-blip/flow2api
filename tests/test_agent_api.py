import asyncio
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from PIL import Image

from src.api.agent import router
from src.core.auth import AuthManager
from src.services.agent_jobs import AgentJobManager


class FakeGenerator:
    async def handle_generation(self, **kwargs):
        yield json.dumps({"media": [{"url": "https://example.com/apple.png", "type": "image"}], "resolved_model": kwargs["model"]})


class AgentApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = AgentJobManager(Path(self.temp.name) / "jobs.db", FakeGenerator())
        await self.manager.start()
        self.app = FastAPI()
        self.app.state.agent_jobs = self.manager
        self.app.include_router(router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        self.auth_patch = patch.object(AuthManager, "verify_api_key", side_effect=lambda key: key == "local-test-only")
        self.auth_patch.start()
        # Configuration substitutes a captcha credential, never an upstream call.
        self.captcha_patch = patch("src.core.config.config._config", {
            "captcha": {"captcha_method": "yescaptcha", "yescaptcha_api_key": "local-test-only"}
        })
        self.captcha_patch.start()
        self.headers = {"Authorization": "Bearer local-test-only"}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.manager.close()
        self.auth_patch.stop()
        self.captcha_patch.stop()
        self.temp.cleanup()

    async def test_agent_http_submit_query_and_idempotency(self):
        request = {"model": "gemini-3.1-flash-image-landscape", "prompt": "an apple", "request_id": "integration-1"}
        response = await self.client.post("/v1/agent/generations", json=request, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        for _ in range(100):
            result = await self.client.get(f"/v1/agent/generations/{job['id']}", headers=self.headers)
            if result.json()["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(result.json()["status"], "completed", result.text)
        self.assertEqual(result.json()["media"][0]["type"], "image")
        repeated = await self.client.post("/v1/agent/generations", json=request, headers=self.headers)
        self.assertEqual(repeated.json()["id"], job["id"])

    async def test_all_agent_routes_require_bearer_authentication(self):
        for path in ("/v1/agent/models", "/v1/agent/generations/missing"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 401)
        response = await self.client.post("/v1/agent/generations", json={
            "model": "gemini-3.1-flash-image-landscape", "prompt": "an apple"
        }, headers={"Authorization": "Bearer wrong-test-only"})
        self.assertEqual(response.status_code, 401)

    async def test_rpc_capability_catalog_omits_native_ui_models(self):
        response = await self.client.get("/v1/agent/models", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn("gemini-3.1-flash-image-square", ids)
        self.assertFalse(any(model.startswith(("gemini-nano-banana-2.1", "native-omni-1.1-flash-")) for model in ids))

    async def test_unverified_model_is_rejected_before_submission(self):
        response = await self.client.post("/v1/agent/generations", headers=self.headers, json={
            "model": "gemini-nano-banana-2.1", "prompt": "an apple"
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unsupported_generation")

    async def test_unsupported_extra_parameters_do_not_silently_disappear(self):
        response = await self.client.post("/v1/agent/generations", headers=self.headers, json={
            "model": "gemini-3.1-flash-image-landscape", "prompt": "an apple", "aspect_ratio": "9:16"
        })
        self.assertEqual(response.status_code, 422)

    async def test_credit_limit_is_strict_and_part_of_request_identity(self):
        request = {"model": "gemini-3.1-flash-image-landscape", "prompt": "an apple", "request_id": "credit-budget-1"}
        for limit in (True, -1, 1001, "12", 1.5):
            with self.subTest(limit=limit):
                rejected = await self.client.post("/v1/agent/generations", headers=self.headers,
                    json={**request, "max_credits": limit})
                self.assertEqual(rejected.status_code, 422)
        first = await self.client.post("/v1/agent/generations", headers=self.headers,
            json={**request, "max_credits": 12})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["max_credits"], 12)
        changed = await self.client.post("/v1/agent/generations", headers=self.headers,
            json={**request, "max_credits": 13})
        self.assertEqual(changed.status_code, 409)

    async def test_validation_errors_do_not_echo_reference_data(self):
        private_reference = "https://private.example/image.png?token=never-echo-this-test-value"
        response = await self.client.post("/v1/agent/generations", headers=self.headers, json={
            "model": "gemini-3.1-flash-image-landscape", "prompt": "an apple", "images": [private_reference]
        })
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("never-echo", response.text)

    async def test_bad_image_bytes_fail_at_http_boundary(self):
        response = await self.client.post("/v1/agent/generations", headers=self.headers, json={
            "model": "gemini-3.1-flash-image-landscape", "prompt": "an apple",
            "images": ["data:image/png;base64," + base64.b64encode(b"not an image").decode()]
        })
        self.assertEqual(response.status_code, 400)

    async def test_actual_reference_image_is_accepted(self):
        image = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(image, format="PNG")
        response = await self.client.post("/v1/agent/generations", headers=self.headers, json={
            "model": "gemini-3.1-flash-image-landscape", "prompt": "an apple",
            "images": ["data:image/png;base64," + base64.b64encode(image.getvalue()).decode()]
        })
        self.assertEqual(response.status_code, 202, response.text)

    async def test_conflicting_retry_is_rejected(self):
        request = {"model": "gemini-3.1-flash-image-landscape", "prompt": "an apple", "request_id": "same-request-1"}
        first = await self.client.post("/v1/agent/generations", headers=self.headers, json=request)
        self.assertEqual(first.status_code, 202)
        request["prompt"] = "a different apple"
        second = await self.client.post("/v1/agent/generations", headers=self.headers, json=request)
        self.assertEqual(second.status_code, 409)

    async def test_lost_submission_response_can_be_recovered_without_resubmitting(self):
        response = await self.client.post("/v1/agent/generations", headers=self.headers, json={
            "model": "gemini-3.1-flash-image-landscape", "prompt": "an apple", "request_id": "lost-response-1"
        })
        expected_id = response.json()["id"]
        restored = await self.client.get("/v1/agent/generations/by-request/lost-response-1", headers=self.headers)
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["id"], expected_id)
        unauthorized = await self.client.get("/v1/agent/generations/by-request/lost-response-1")
        self.assertEqual(unauthorized.status_code, 401)
        missing = await self.client.get("/v1/agent/generations/by-request/never-submitted", headers=self.headers)
        self.assertEqual(missing.status_code, 404)

    async def test_retry_recovers_existing_job_after_configuration_changes(self):
        request = {"model": "gemini-3.1-flash-image-landscape", "prompt": "an apple", "request_id": "config-change-1"}
        first = await self.client.post("/v1/agent/generations", headers=self.headers, json=request)
        with patch("src.core.config.config._config", {"captcha": {"captcha_method": "yescaptcha", "yescaptcha_api_key": ""}}):
            repeated = await self.client.post("/v1/agent/generations", headers=self.headers, json=request)
            self.assertEqual(repeated.status_code, 202)
            self.assertEqual(repeated.json()["id"], first.json()["id"])
            changed = await self.client.post("/v1/agent/generations", headers=self.headers, json={**request, "prompt": "different"})
            self.assertEqual(changed.status_code, 409)


if __name__ == "__main__":
    unittest.main()
