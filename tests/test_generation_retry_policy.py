"""No live network: ambiguous mutations must not be submitted twice by Agent jobs."""
import json
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

from src.core.config import config
from src.services.flow_client import FlowClient
from src.services.generation_policy import (
    GenerationOutcomeUnknown, reset_no_submit_retry, set_no_submit_retry,
)


class GenerationRetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    settings = {
        "captcha": {"captcha_method": "yescaptcha", "yescaptcha_api_key": "test-only"},
        "flow": {"labs_base_url": "https://example.invalid", "api_base_url": "https://example.invalid", "max_retries": 2},
    }

    async def submit(self, client, media_type):
        if media_type == "image":
            return await client.generate_image("test-at", "test-project", "test", "NARWHAL",
                "IMAGE_ASPECT_RATIO_LANDSCAPE", google_cookies="SID=test-only", preserve_parameters=True)
        common = dict(at="test-at", project_id="test-project", google_cookies="SID=test-only")
        if media_type == "image_upscale":
            return await client.upsample_image(**common, media_id="test-media")
        video = dict(**common, aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE", model_key="abra_t2v_4s")
        if media_type == "video_upscale":
            return await client.upsample_video(**video, video_media_id="test-media", resolution="720p")
        video["prompt"] = "test"
        if media_type == "video_reference":
            return await client.generate_video_reference_images(**video, reference_images=[{"name": "test-media"}])
        if media_type == "video_start":
            return await client.generate_video_start_image(**video, start_media_id="test-media")
        if media_type == "video_start_end":
            return await client.generate_video_start_end(**video, start_media_id="test-media", end_media_id="test-end")
        if media_type == "video_extend":
            return await client.generate_video_extend(**video, video_media_id="test-media")
        return await client.generate_video_text(**video)

    async def test_agent_image_and_video_timeout_submit_once_but_legacy_keeps_retry(self):
        for media_type in ("image", "video", "video_reference", "video_start", "video_start_end", "video_extend", "video_upscale", "image_upscale"):
            for safe in (True, False):
                with self.subTest(media_type=media_type, safe=safe), patch.object(config, "_config", self.settings):
                    client = FlowClient(proxy_manager=None)
                    context = set_no_submit_retry(safe)
                    try:
                        with patch.object(client, "_current_rpc", AsyncMock(side_effect=TimeoutError("test timeout"))) as rpc, \
                             patch.object(client, "_get_recaptcha_token", AsyncMock(return_value=("test-captcha", None))), \
                             patch.object(client, "_handle_retryable_generation_error", AsyncMock()), \
                             patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()):
                            with self.assertRaises(GenerationOutcomeUnknown if safe else TimeoutError):
                                await self.submit(client, media_type)
                            self.assertEqual(rpc.await_count, 1 if safe or media_type == "image_upscale" else 2)
                    finally:
                        reset_no_submit_retry(context)

    async def test_agent_xsrf_challenge_does_not_repost_and_sends_bootstrap_token_first(self):
        await self.assert_xsrf_posts(safe=True, expected_posts=1)

    async def test_legacy_xsrf_challenge_repost_remains_compatible(self):
        await self.assert_xsrf_posts(safe=False, expected_posts=2)

    async def test_agent_readonly_status_queries_can_complete_xsrf_challenge(self):
        await self.assert_xsrf_posts(safe=True, expected_posts=2, rpc_id="jwpduf")

    async def assert_xsrf_posts(self, *, safe, expected_posts, rpc_id="ogiZ0b"):
        bootstrap = 'boq_labs-ai-sandbox-frontend_test "FdrFJe":"1" "SNlM0e":"test-bootstrap"'
        challenge = '[["xsrf","test-challenge"]]'
        success = json.dumps([["wrb.fr", rpc_id, json.dumps(["test-result"])]])
        with patch.object(config, "_config", self.settings):
            client = FlowClient(proxy_manager=None)
            context = set_no_submit_retry(safe)
            try:
                with patch.object(client, "_load_flow_frontend_bootstrap", AsyncMock(return_value=bootstrap)), \
                     patch.object(client, "_make_text_request", AsyncMock(side_effect=[challenge, success])) as transport:
                    call = client._call_flow_frontend_rpc(rpc_id, [], "SID=test-only", 90, "test-project")
                    if expected_posts == 1:
                        with self.assertRaises(GenerationOutcomeUnknown):
                            await call
                    else:
                        self.assertEqual(await call, ["test-result"])
                    self.assertEqual(transport.await_count, expected_posts)
                    first_form = parse_qs(transport.await_args_list[0].kwargs["raw_body"])
                    if safe:
                        self.assertEqual(first_form["at"], ["test-bootstrap"])
            finally:
                reset_no_submit_retry(context)


if __name__ == "__main__":
    unittest.main()
