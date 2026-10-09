"""Generation boundary regressions; all account/network data is synthetic."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.config import config
from src.core.database import Database
from src.core.models import Token, Project
from src.services.generation_handler import GenerationHandler
from src.services.flow_client import FlowClient
from src.services.token_manager import TokenManager
from src.services.load_balancer import LoadBalancer
from src.services.generation_policy import GenerationOutcomeUnknown, set_no_submit_retry, reset_no_submit_retry


class GenerationContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_submission_outcome_unknown_survives_public_handler_response(self):
        response = await self.generate_fixture_image("gemini-3.1-flash-image-landscape",
            generation_error=GenerationOutcomeUnknown("submission outcome unknown"))
        self.assertIs(response["error"]["outcome_unknown"], True)

    async def test_agent_upscale_timeout_does_not_resubmit(self):
        context = set_no_submit_retry()
        try:
            response = await self.generate_fixture_image("gemini-3.1-flash-image-landscape-2k",
                upsample_error=TimeoutError("request timed out"), max_retries=3, expected_upsample_calls=1)
        finally:
            reset_no_submit_retry(context)
        self.assertTrue(response["degraded"])
        self.assertEqual(response["media"][0]["url"], "https://example.invalid/image/result.png")

    async def test_video_delivery_failure_after_upsample_does_not_submit_upsample_again(self):
        context = set_no_submit_retry()
        try:
            response = await self.generate_fixture_image("veo_3_1_t2v_fast_4k",
                video_delivery_retry=True)
        finally:
            reset_no_submit_retry(context)
        self.assertIs(response["error"]["outcome_unknown"], True)

    async def test_omni_reference_result_identifies_the_resolved_reference_mapping(self):
        response = await self.generate_fixture_image("omni-1.1-flash-10s-portrait", images=[b"reference"])
        self.assertEqual(response["media"], [{"url": "https://example.invalid/video/result.mp4", "type": "video", "mime_type": "video/mp4"}])
        self.assertEqual(response["resolved_model"], "abra_r2v_10s")
        self.assertFalse(response["upstream_model_verified"])

    async def test_upscale_and_cache_failures_return_explicit_degraded_result(self):
        response = await self.generate_fixture_image("gemini-3.1-flash-image-landscape-2k",
                                                     cache_fails=True, upsample_fails=True)
        self.assertTrue(response["degraded"])
        self.assertEqual({warning["code"] for warning in response["warnings"]},
                         {"image_upsample_failed", "cache_failed"})
        self.assertEqual(response["media"][0]["url"], "https://example.invalid/image/result.png")
        self.assertNotIn("width", response["media"][0])

    async def test_image_completion_provides_media_and_model_metadata(self):
        model = "gemini-3.1-flash-image-landscape"
        response = await self.generate_fixture_image(model)
        self.assertEqual(response["media"], [{"url": "https://example.invalid/image/result.png", "type": "image", "mime_type": "image/png"}])
        self.assertEqual(response["requested_model"], model)
        self.assertEqual(response["resolved_model"], "NARWHAL")
        self.assertFalse(response["upstream_model_verified"])
        self.assertFalse(response["degraded"])

    async def generate_fixture_image(self, model, *, cache_fails=False, upsample_fails=False, images=None,
                                     generation_error=None, upsample_error=None, max_retries=1, expected_upsample_calls=None,
                                     video_delivery_retry=False):
        settings = {
            "captcha": {"captcha_method": "yescaptcha", "yescaptcha_api_key": "test-only", "personal_project_pool_size": 1},
            "flow": {"labs_base_url": "https://example.invalid", "api_base_url": "https://example.invalid", "max_retries": max_retries, "poll_interval": 0.1},
            "cache": {"enabled": cache_fails},
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(config, "_config", settings):
            db = Database(db_path=f"{temp_dir}/test.db")
            await db.init_db()
            token_id = await db.add_token(Token(st="test-only", at="test-only", email="test@example.invalid",
                at_expires=datetime.now(timezone.utc) + timedelta(hours=5), user_paygate_tier="PAYGATE_TIER_TWO"))
            await db.add_project(Project(project_id="test-project", token_id=token_id, project_name="test"))
            client = FlowClient(proxy_manager=None, db=db)
            manager = TokenManager(db, client)
            handler = GenerationHandler(client, manager, LoadBalancer(manager), db, None, None)
            external_result = {"media": [{"name": "test-media", "image": {"generatedImage": {"fifeUrl": "https://example.invalid/image/result.png"}}}]}
            video_operation = {"operation": {"name": "test-operation", "metadata": {"video": {
                "fifeUrl": "https://example.invalid/video/result.mp4", "mediaGenerationId": "test-operation",
                "mediaName": "test-media", "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE"}}},
                "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL"}
            with patch.object(client, "get_credits", AsyncMock(return_value={"credits": 100, "userPaygateTier": "PAYGATE_TIER_TWO"})), \
                 patch.object(client, "generate_image", AsyncMock(return_value=(external_result, "test-session", {}), side_effect=generation_error)), \
                 patch.object(client, "generate_video_text", AsyncMock(return_value={"operations": [video_operation]})), \
                 patch.object(client, "generate_video_reference_images", AsyncMock(return_value={"direct_media": True, "video_url": "https://example.invalid/video/result.mp4"})), \
                 patch.object(client, "check_video_status", AsyncMock(return_value={"operations": [video_operation]})), \
                 patch.object(client, "upsample_video", AsyncMock(side_effect=GenerationOutcomeUnknown("test uncertain upsample"))) as video_upsample, \
                 patch.object(client, "upload_image", AsyncMock(return_value="test-reference")), \
                 patch.object(client, "upsample_image", AsyncMock(side_effect=upsample_error or (RuntimeError("test upscale failure") if upsample_fails else None))) as upsample, \
                 patch.object(db, "update_task", AsyncMock(side_effect=OSError("test delivery write failure") if video_delivery_retry else db.update_task)), \
                 patch.object(handler.file_cache, "download_and_cache", AsyncMock(side_effect=OSError("test disk failure"))):
                chunks = [chunk async for chunk in handler.handle_generation(model, "测试", images=images, stream=False, preserve_parameters=True)]
                if expected_upsample_calls is not None:
                    self.assertEqual(upsample.await_count, expected_upsample_calls)
                if video_delivery_retry:
                    self.assertEqual(video_upsample.await_count, 1)
            response = json.loads(chunks[-1])
            if generation_error is None and not video_delivery_retry:
                self.assertNotIn("error", response)
            return response

    async def test_browser_passes_selected_model_ratio_and_reference_to_upstream_rpc(self):
        settings = {
            "captcha": {"captcha_method": "browser", "yescaptcha_api_key": "test-only"},
            "flow": {"labs_base_url": "https://example.invalid", "api_base_url": "https://example.invalid", "max_retries": 1},
        }
        media_url = "https://example.invalid/image/result"
        with patch.object(config, "_config", settings):
            client = FlowClient(proxy_manager=None)
            # These are the Google RPC / CAPTCHA service boundaries; no network is allowed.
            with patch.object(client, "_current_rpc", AsyncMock(return_value=[[["media-id", media_url]]])) as rpc, \
                 patch.object(client, "_get_recaptcha_token", AsyncMock(return_value=("test-captcha", "1:test-session"))) as captcha, \
                 patch.object(client, "_call_flow_stream_chat", AsyncMock(side_effect=AssertionError("StreamChat must not discard parameters"))), \
                 patch.object(client, "_handle_retryable_generation_error", AsyncMock()), \
                 patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()):
                result, _, _ = await client.generate_image(
                    "test-at", "test-project", "保留商品", "NARWHAL", "IMAGE_ASPECT_RATIO_PORTRAIT",
                    image_inputs=[{"name": "reference-id"}], google_cookies="SID=test-only")
            self.assertEqual(result["media"][0]["image"]["generatedImage"]["fifeUrl"], media_url)
            wire_request = rpc.await_args.kwargs["argument"][1][0]
            self.assertEqual(wire_request[2], [["reference-id", None, None, None, 1]])
            self.assertEqual(wire_request[4], 2)
            self.assertEqual(wire_request[5], "NARWHAL")
            self.assertEqual(captcha.await_args.kwargs["method_override"], "yescaptcha")

    async def test_native_default_browser_mode_remains_available_without_paid_captcha(self):
        settings = {
            "captcha": {"captcha_method": "browser"},
            "flow": {"labs_base_url": "https://example.invalid", "api_base_url": "https://example.invalid", "max_retries": 1},
        }
        with patch.object(config, "_config", settings):
            client = FlowClient(proxy_manager=None)
            media = {"image": {"generatedImage": {"fifeUrl": "https://example.invalid/image/result"}}}
            with patch.object(client, "_get_recaptcha_token", AsyncMock(return_value=("test-captcha", "1:test-session"))), \
                 patch.object(client, "_call_flow_stream_chat", AsyncMock(return_value={"mediaIds": ["test-media"]})), \
                 patch.object(client, "get_media", AsyncMock(return_value=media)), \
                 patch.object(client, "_current_rpc", AsyncMock(side_effect=AssertionError("Unexpected paid RPC"))), \
                 patch.object(client, "_handle_retryable_generation_error", AsyncMock()), \
                 patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()):
                result, _, _ = await client.generate_image("test-at", "test-project", "测试", "NARWHAL",
                    "IMAGE_ASPECT_RATIO_LANDSCAPE", google_cookies="SID=test-only")
            self.assertEqual(result["frontendRpc"], "StreamChat")
            with self.assertRaisesRegex(ValueError, "第三方"):
                await client.generate_image("test-at", "test-project", "测试", "NARWHAL",
                    "IMAGE_ASPECT_RATIO_LANDSCAPE", google_cookies="SID=test-only", preserve_parameters=True)

    async def test_browser_reference_request_rejected_before_any_external_work(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = SimpleNamespace(clear_request_fingerprint=lambda: None)
        # No account, database or network is supplied: preflight must finish before those boundaries.
        with patch.object(config, "_config", {"captcha": {"captcha_method": "browser"}}):
            chunks = [chunk async for chunk in handler.handle_generation(
                "gemini-3.1-flash-image-portrait", "保留参考商品", [b"reference"], False)]
        response = json.loads(chunks[-1])
        self.assertIn("第三方", response["error"]["message"])
        self.assertEqual(response["error"]["status_code"], 400)


if __name__ == "__main__":
    unittest.main()
