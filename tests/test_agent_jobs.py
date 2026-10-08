import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from src.services.agent_jobs import AgentJobManager, RequestConflict
from src.services.generation_policy import no_submit_retry


class ControlledGenerator:
    """Local upstream substitute: no account, network, or billable generation."""

    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.requests = 0

    async def handle_generation(self, **request):
        self.requests += 1
        self.started.set()
        await self.release.wait()
        yield json.dumps({
            "media": [{"url": "https://example.com/result.png", "type": "image", "mime_type": "image/png"}],
            "resolved_model": request["model"],
            "warnings": [],
            "degraded": False,
        })


class AgentJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "jobs.db"
        self.generator = ControlledGenerator()
        self.manager = AgentJobManager(self.db_path, self.generator)
        await self.manager.start()

    async def asyncTearDown(self):
        await self.manager.close()
        self.directory.cleanup()

    async def test_repeated_request_returns_same_job_without_resubmitting(self):
        job = await self.manager.submit("sample-image", "an apple", [], "request-1")
        await self.generator.started.wait()
        repeated = await self.manager.submit("sample-image", "an apple", [], "request-1")
        self.assertEqual(repeated["id"], job["id"])
        self.generator.release.set()
        result = await self.wait_for_completion(job["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["media"][0]["type"], "image")
        self.assertEqual(self.generator.requests, 1)

    async def wait_for_completion(self, job_id):
        for _ in range(100):
            result = await self.manager.get(job_id)
            if result["status"] not in ("queued", "running"):
                return result
            await asyncio.sleep(0.01)
        self.fail("Local fake generation did not finish")

    async def test_same_request_id_cannot_change_prompt_or_reference(self):
        await self.manager.submit("sample-image", "an apple", [b"first"], "request-1")
        with self.assertRaises(RequestConflict):
            await self.manager.submit("sample-image", "an apple", [b"second"], "request-1")

    async def test_interrupted_job_remains_queryable_and_never_resubmits(self):
        job = await self.manager.submit("sample-image", "an apple", [], "request-1")
        await self.generator.started.wait()
        await self.manager.close()
        self.manager = AgentJobManager(self.db_path, self.generator)
        await self.manager.start()
        restored = await self.manager.get(job["id"])
        self.assertEqual(restored["status"], "unknown")
        self.assertFalse(restored["error"]["retryable"])
        repeated = await self.manager.submit("sample-image", "an apple", [], "request-1")
        self.assertEqual(repeated["id"], job["id"])
        self.assertEqual(self.generator.requests, 1)

    async def test_result_persists_across_restart(self):
        self.generator.release.set()
        job = await self.manager.submit("sample-image", "an apple", [], "request-1")
        result = await self.wait_for_completion(job["id"])
        await self.manager.close()
        self.manager = AgentJobManager(self.db_path, self.generator)
        await self.manager.start()
        restored = await self.manager.get(job["id"])
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["media"], result["media"])

    async def test_raw_upstream_error_never_leaks_into_public_result(self):
        class RejectedGenerator:
            async def handle_generation(self, **kwargs):
                yield json.dumps({"error": {"message": "captcha failed; fake-secret-for-test-only", "code": "generation_failed"}})
        self.manager.handler = RejectedGenerator()
        job = await self.manager.submit("sample-image", "an apple", [], "request-1")
        result = await self.wait_for_completion(job["id"])
        self.assertEqual(result["error"]["code"], "captcha_failed")
        self.assertNotIn("fake-secret-for-test-only", json.dumps(result))

    async def test_plain_chat_success_is_not_mistaken_for_generated_media(self):
        class EmptyGenerator:
            async def handle_generation(self, **kwargs):
                yield json.dumps({"choices": [{"message": {"content": "Done"}}]})
        self.manager.handler = EmptyGenerator()
        job = await self.manager.submit("sample-image", "an apple", [], "request-1")
        result = await self.wait_for_completion(job["id"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["media"], [])

    async def test_concurrent_duplicates_reuse_one_upstream_submission(self):
        jobs = await asyncio.gather(*(
            self.manager.submit("sample-image", "an apple", [], "request-1") for _ in range(5)
        ))
        self.assertEqual(len({job["id"] for job in jobs}), 1)
        await self.generator.started.wait()
        self.assertEqual(self.generator.requests, 1)

    async def test_delivery_warning_preserves_reason_without_upstream_text(self):
        class DegradedGenerator:
            async def handle_generation(self, **kwargs):
                yield {"media": [{"url": "https://example.com/result.png", "type": "image"}],
                       "degraded": True, "warnings": [{"code": "image_upsample_failed", "message": "fake-secret"}]}
        self.manager.handler = DegradedGenerator()
        job = await self.manager.submit("sample-image", "an apple", [], "request-warning")
        result = await self.wait_for_completion(job["id"])
        self.assertEqual(result["warnings"][0]["code"], "image_upsample_failed")
        self.assertTrue(result["degraded"])
        self.assertNotIn("fake-secret", json.dumps(result))

    async def test_uncertain_upstream_submission_is_never_reported_as_definite_failure(self):
        class UncertainGenerator:
            async def handle_generation(self, **kwargs):
                yield {"error": {"message": "connection reset", "outcome_unknown": True}}
        self.manager.handler = UncertainGenerator()
        job = await self.manager.submit("sample-image", "an apple", [], "uncertain-request")
        result = await self.wait_for_completion(job["id"])
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["error"]["retryable"])

    async def test_agent_policy_does_not_change_other_requests(self):
        class PolicyGenerator:
            async def handle_generation(inner, **kwargs):
                self.assertTrue(no_submit_retry())
                yield {"media": [{"url": "https://example.com/result.png", "type": "image"}]}
        self.manager.handler = PolicyGenerator()
        job = await self.manager.submit("sample-image", "an apple", [], "policy-isolation")
        result = await self.wait_for_completion(job["id"])
        self.assertEqual(result["status"], "completed")
        self.assertFalse(no_submit_retry())


if __name__ == "__main__":
    unittest.main()
