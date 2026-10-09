"""Actual create-task observations reach durable public Agent job results."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import aiosqlite

from src.services.agent_jobs import AgentJobManager


class FakeCaptchaTransport:
    """Only the external HTTP seam is replaced; every provider loop is real."""

    def __init__(self, creates, polls):
        self.creates = iter(creates)
        self.polls = iter(polls)
        self.create_actions = []
        self.poll_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        if url.endswith(("/createTask", "/v2/tasks")):
            body = kwargs["json"]
            self.create_actions.append(body.get("siteAction") or body["task"]["pageAction"])
            response = next(self.creates)
        elif url.endswith("/getTaskResult"):
            self.poll_count += 1
            response = next(self.polls)
        else:
            raise AssertionError("Unexpected fake HTTP destination")
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(status_code=200, json=lambda: response)

    async def get(self, url, **kwargs):
        if "/v2/tasks/" not in url:
            raise AssertionError("Unexpected fake HTTP destination")
        self.poll_count += 1
        response = next(self.polls)
        return SimpleNamespace(status_code=200, json=lambda: response)


class FakeGeneration:
    async def handle_generation(self, **request):
        from src.services.flow_current import record_captcha_create_call
        # One observed upload solve, then one observed generation solve.
        await record_captcha_create_call()
        await record_captcha_create_call()
        yield {"media": [{"url": "https://example.invalid/result.png", "type": "image"}]}


class CaptchaCallCountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "jobs.db"
        self.manager = AgentJobManager(self.db_path, FakeGeneration())
        await self.manager.start()
        self.addAsyncCleanup(self.manager.close)

    async def wait_for_terminal(self, job_id):
        for _ in range(100):
            result = await self.manager.get(job_id)
            if result["status"] not in {"queued", "running"}:
                return result
            await asyncio.sleep(0.01)
        self.fail("Fake job did not finish")

    async def run_provider(self, method, transport, *, generate=True, video=False):
        from src.core.config import config
        from src.services.flow_client import FlowClient

        settings = {
            "captcha": {"captcha_method": method, f"{method}_api_key": "synthetic-test-only",
                        f"{method}_base_url": "https://captcha.example.invalid"},
            "flow": {"labs_base_url": "https://flow.example.invalid",
                     "api_base_url": "https://flow.example.invalid", "max_retries": 2},
            "debug": {"enabled": False},
        }
        media_id = "11234567-89ab-cdef-0123-456789abcdef"
        media_url = f"https://flow.example.invalid/image/{media_id}"
        self.provider_errors = []
        provider_errors = self.provider_errors

        class FlowGeneration:
            async def handle_generation(self, **request):
                try:
                    await self.generate()
                except Exception as exc:
                    provider_errors.append(str(exc))
                    yield {"error": {"message": str(exc)}}
                    return
                yield {"media": [{"url": media_url, "type": "image"}]}

            async def generate(self):
                await flow.upload_image(
                    "synthetic-at", b"\xff\xd8\xffsynthetic", project_id="synthetic-project",
                    google_cookies="SID=synthetic-test-only",
                )
                if generate and video:
                    await flow.generate_video_start_image(
                        "synthetic-at", "synthetic-project", "synthetic prompt", "abra_t2v_4s",
                        "VIDEO_ASPECT_RATIO_LANDSCAPE", start_media_id=media_id,
                        google_cookies="SID=synthetic-test-only",
                    )
                elif generate:
                    await flow.generate_image(
                        "synthetic-at", "synthetic-project", "synthetic prompt", "NARWHAL",
                        "IMAGE_ASPECT_RATIO_LANDSCAPE", google_cookies="SID=synthetic-test-only",
                        preserve_parameters=True,
                    )

        with patch.object(config, "_config", settings):
            flow = FlowClient(proxy_manager=None)
            self.manager.handler = FlowGeneration()
            with (
                patch("src.services.flow_client.AsyncSession", return_value=transport),
                patch("src.services.flow_client.asyncio", SimpleNamespace(sleep=AsyncMock())),
                patch("src.services.flow_client.debug_logger.log_error", side_effect=provider_errors.append),
                patch.object(flow, "_call_flow_frontend_rpc", AsyncMock(return_value=[[[media_id, media_url]], f"https://flow.example.invalid/video/{media_id}"])),
            ):
                job = await self.manager.submit("synthetic-image", "synthetic", [], "provider-count-1")
                return await self.wait_for_terminal(job["id"])

    async def test_yescaptcha_counts_no_slot_retries_upload_and_generation_but_not_polls(self):
        ready = {"status": "ready", "solution": {"gRecaptchaResponse": "synthetic-solution"}}
        transport = FakeCaptchaTransport(
            [{"errorCode": "ERROR_NO_SLOT_AVAILABLE"}, {"errorCode": "ERROR_NO_SLOT_AVAILABLE_BLOCK"}, {"taskId": "synthetic-1"}, {"taskId": "synthetic-2"}],
            [{"status": "processing"}, ready, ready],
        )
        result = await self.run_provider("yescaptcha", transport)
        self.assertEqual(result["status"], "completed", self.provider_errors)
        self.assertEqual(transport.create_actions, ["UPLOAD_IMAGE"] * 3 + ["IMAGE_GENERATION"])
        self.assertEqual(transport.poll_count, 3)
        self.assertEqual(result["captcha_call_count"], 4)

    async def test_captcharun_counts_creations_without_counting_get_polls(self):
        ready = {"status": "success", "response": {"gRecaptchaResponse": "synthetic-solution"}}
        transport = FakeCaptchaTransport(
            [{"taskId": "synthetic-1"}, {"taskId": "synthetic-2"}],
            [{"status": "processing"}, ready, ready],
        )
        result = await self.run_provider("captcharun", transport)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(transport.create_actions, ["UPLOAD_IMAGE", "IMAGE_GENERATION"])
        self.assertEqual(transport.poll_count, 3)
        self.assertEqual(result["captcha_call_count"], 2)

    async def test_reference_upload_and_video_generation_share_the_job_counter(self):
        ready = {"status": "ready", "solution": {"gRecaptchaResponse": "synthetic-solution"}}
        transport = FakeCaptchaTransport(
            [{"taskId": "synthetic-1"}, {"taskId": "synthetic-2"}], [ready, ready],
        )
        result = await self.run_provider("yescaptcha", transport, video=True)
        self.assertEqual(result["status"], "completed", self.provider_errors)
        self.assertEqual(transport.create_actions, ["UPLOAD_IMAGE", "VIDEO_GENERATION"])
        self.assertEqual(result["captcha_call_count"], 2)

    async def test_no_slot_exhaustion_keeps_all_actual_requests_on_failure(self):
        transport = FakeCaptchaTransport([{"errorCode": "ERROR_NO_SLOT_AVAILABLE"}] * 5, [])
        result = await self.run_provider("yescaptcha", transport, generate=False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(transport.create_actions, ["UPLOAD_IMAGE"] * 5)
        self.assertEqual(transport.poll_count, 0)
        self.assertEqual(result["captcha_call_count"], 5)

    async def test_transport_failure_is_still_one_create_attempt(self):
        for method in ("yescaptcha", "captcharun"):
            with self.subTest(method=method):
                # Use distinct idempotency storage for each synthetic request.
                self.manager = AgentJobManager(Path(self.directory.name) / f"{method}.db", FakeGeneration())
                await self.manager.start()
                self.addAsyncCleanup(self.manager.close)
                transport = FakeCaptchaTransport([OSError("synthetic network failure")], [])
                result = await self.run_provider(method, transport, generate=False)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["captcha_call_count"], 1)

    async def test_observed_upload_and_generation_count_survives_restart(self):
        job = await self.manager.submit("synthetic-image", "synthetic prompt", [b"synthetic-image"], "counted-request-1")
        result = await self.wait_for_terminal(job["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["captcha_call_count"], 2)
        await self.manager.close()
        restored = AgentJobManager(self.db_path, FakeGeneration())
        await restored.start()
        self.addAsyncCleanup(restored.close)
        self.assertEqual((await restored.get(job["id"]))["captcha_call_count"], 2)

    async def test_concurrent_failure_and_retry_events_are_isolated(self):
        both_started = asyncio.Event()
        ready = set()

        class ConcurrentGeneration:
            async def handle_generation(self, **request):
                from src.services.flow_current import record_captcha_create_call
                name = request["prompt"]
                await record_captcha_create_call()
                ready.add(name)
                if len(ready) == 2:
                    both_started.set()
                await both_started.wait()
                if name == "fails":
                    yield {"error": {"message": "captcha failed with synthetic-private-detail"}}
                    return
                # Actual observed retry dispatches, not an estimate from settings.
                await record_captcha_create_call()
                await record_captcha_create_call()
                yield {"media": [{"url": "https://example.invalid/result.png", "type": "image"}]}

        self.manager.handler = ConcurrentGeneration()
        success, failure = await asyncio.gather(
            self.manager.submit("synthetic-image", "retries", [], "concurrent-1"),
            self.manager.submit("synthetic-image", "fails", [], "concurrent-2"),
        )
        succeeded, failed = await asyncio.gather(self.wait_for_terminal(success["id"]), self.wait_for_terminal(failure["id"]))
        self.assertEqual(succeeded["captcha_call_count"], 3)
        self.assertEqual(failed["captcha_call_count"], 1)
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("synthetic-private-detail", str(failed))

    async def test_unobserved_path_is_null_even_if_handler_supplies_an_estimate(self):
        class UnobservedGeneration:
            async def handle_generation(self, **request):
                yield {"media": [{"url": "https://example.invalid/result.png", "type": "image"}], "captcha_call_count": 99}

        self.manager.handler = UnobservedGeneration()
        job = await self.manager.submit("unobserved-image", "synthetic", [], "unknown-count-1")
        result = await self.wait_for_terminal(job["id"])
        self.assertEqual(result["status"], "completed")
        self.assertIn("captcha_call_count", result)
        self.assertIsNone(result["captcha_call_count"])

    async def test_running_count_and_cancelled_failure_are_durable(self):
        recorded = asyncio.Event()

        class WaitingGeneration:
            async def handle_generation(self, **request):
                from src.services.flow_current import record_captcha_create_call
                await record_captcha_create_call()
                recorded.set()
                await asyncio.Event().wait()
                yield {}

        self.manager.handler = WaitingGeneration()
        job = await self.manager.submit("synthetic-image", "synthetic", [], "cancelled-count-1")
        await recorded.wait()
        for _ in range(100):
            if (await self.manager.get(job["id"]))["captcha_call_count"] == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual((await self.manager.get(job["id"]))["captcha_call_count"], 1)
        await self.manager.close()
        result = await self.manager.get(job["id"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["captcha_call_count"], 1)
        restored = AgentJobManager(self.db_path, FakeGeneration())
        await restored.start()
        self.addAsyncCleanup(restored.close)
        self.assertEqual((await restored.get(job["id"]))["captcha_call_count"], 1)

    async def test_http_get_generation_returns_the_observed_count(self):
        import httpx
        from fastapi import FastAPI
        from src.api.agent import authenticate, router

        job = await self.manager.submit("synthetic-image", "synthetic", [], "http-count-1")
        await self.wait_for_terminal(job["id"])
        app = FastAPI()
        app.state.agent_jobs = self.manager
        app.include_router(router)
        app.dependency_overrides[authenticate] = lambda: None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            result = await client.get(f'/v1/agent/generations/{job["id"]}')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["captcha_call_count"], 2)

    async def test_unfinished_crash_snapshot_does_not_claim_a_complete_count(self):
        job = await self.manager.submit("synthetic-image", "synthetic", [], "crash-count-1")
        await self.wait_for_terminal(job["id"])
        await self.manager.close()
        # Synthetic crash fixture: only a partial count reached durable storage.
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE agent_generations SET status='running', captcha_call_count=1 WHERE id=?", (job["id"],))
            await db.commit()
        restored = AgentJobManager(self.db_path, FakeGeneration())
        await restored.start()
        self.addAsyncCleanup(restored.close)
        result = await restored.get(job["id"])
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["captcha_call_count"])

    async def test_existing_completed_jobs_without_observations_migrate_to_null(self):
        await self.manager.close()
        legacy_path = Path(self.directory.name) / "legacy.db"
        async with aiosqlite.connect(legacy_path) as db:
            await db.execute("""CREATE TABLE agent_generations (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL, model TEXT NOT NULL,
                status TEXT NOT NULL, result_json TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                max_credits INTEGER NOT NULL DEFAULT 0)""")
            await db.execute("INSERT INTO agent_generations VALUES ('legacy-id','legacy-request','synthetic-hash','synthetic-model','completed','{}',0,0,0)")
            await db.commit()
        legacy = AgentJobManager(legacy_path, FakeGeneration())
        await legacy.start()
        self.addAsyncCleanup(legacy.close)
        result = await legacy.get("legacy-id")
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["captcha_call_count"])


if __name__ == "__main__":
    unittest.main()
