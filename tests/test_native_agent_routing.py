"""Native UI routing contracts; no browser, account, or external request is used."""
import json
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.config import config
from src.services import model_capabilities
from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler, MODEL_CONFIG
from src.core.database import Database
from src.core.models import Token, Project
from src.services.token_manager import TokenManager
from src.services.load_balancer import LoadBalancer
from src.services.generation_policy import GenerationOutcomeUnknown, set_native_credit_limit, reset_native_credit_limit


class NativeAgentRoutingTests(unittest.IsolatedAsyncioTestCase):
    settings = {
        "captcha": {"captcha_method": "personal", "personal_project_pool_size": 1},
        "flow": {"labs_base_url": "https://example.invalid", "api_base_url": "https://example.invalid", "max_retries": 3},
    }

    async def test_personal_catalog_exposes_native_two_point_one_without_a_rpc_model_claim(self):
        with patch.object(config, "_config", self.settings):
            entry = model_capabilities.validate_generation_request("gemini-nano-banana-2.1-portrait", 0)
            self.assertTrue(entry["available"])
            self.assertEqual(entry["generation_transport"], "native_ui")
            self.assertEqual(entry["ui_model_label"], "Nano Banana 2.1")
            self.assertEqual(entry["aspect_ratio"], "9:16")
            self.assertEqual(entry["image_count"], 1)
            self.assertEqual(entry["max_reference_images"], 0)
            self.assertFalse(entry["requires_third_party_captcha"])
            self.assertEqual(entry["verification_state"], "ui_option_observed_generation_not_live_verified")
            self.assertIsNone(MODEL_CONFIG[entry["id"]]["model_name"])

    async def test_native_request_uses_verified_ui_options_and_reads_batchexecute_result(self):
        options = {"model_label": "Nano Banana 2.1", "aspect_ratio": "9:16", "image_count": 1, "reference_images_count": 0}
        settings = {key: value for key, value in options.items() if key != "reference_images_count"}
        settings["verified_before_submit"] = True
        settings.update(credits_shown=0, max_credits=0)
        media_url = "https://example.invalid/image/native.png"
        response = json.dumps([["wrb.fr", "ogiZ0b", json.dumps([[["native-media", media_url]]])]])
        browser = SimpleNamespace(generate_native_image=AsyncMock(return_value={
            "frontendRpc": "ogiZ0b", "responseText": response, "native_settings": settings,
        }))
        with patch.object(config, "_config", self.settings):
            client = FlowClient(proxy_manager=None)
            client._personal_browser_service = browser
            with patch.object(client, "_current_rpc", AsyncMock(side_effect=AssertionError("must not submit a guessed RPC"))), \
                 patch.object(client, "_get_recaptcha_token", AsyncMock(side_effect=AssertionError("must not use paid captcha"))), \
                 patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()):
                result, _, trace = await client.generate_image(
                    "test-at", "test-project", "a candle", None, "IMAGE_ASPECT_RATIO_PORTRAIT",
                    google_cookies="SID=test-only", preserve_parameters=True, native_options=options,
                )
        self.assertEqual(browser.generate_native_image.await_count, 1)
        self.assertEqual(browser.generate_native_image.await_args.kwargs["native_options"], {**options, "max_credits": 0})
        self.assertEqual(result["media"][0]["image"]["generatedImage"]["fifeUrl"], media_url)
        self.assertEqual(result["native_settings"], settings)
        self.assertEqual(trace["max_retries"], 1)

    async def test_native_image_streamchat_result_resolves_its_single_media_read_only(self):
        options = {"model_label": "Nano Banana 2.1", "aspect_ratio": "16:9", "image_count": 1, "reference_images_count": 0, "max_credits": 0}
        settings = {key: value for key, value in options.items() if key != "reference_images_count"}
        settings.update(verified_before_submit=True, credits_shown=0)
        media_id = "22222222-2222-4222-8222-222222222222"
        response = json.dumps([["wrb.fr", None, json.dumps([["media_id", [None, None, media_id]]])]])
        media = {"name": media_id, "image": {"generatedImage": {"fifeUrl": "https://example.invalid/image/native.png"}}}
        browser = SimpleNamespace(generate_native_image=AsyncMock(return_value={
            "frontendRpc": "StreamChat", "responseText": response, "native_settings": settings,
        }))
        with patch.object(config, "_config", self.settings):
            client = FlowClient(proxy_manager=None)
            client._personal_browser_service = browser
            with patch.object(client, "get_media", AsyncMock(return_value=media)) as read_media, \
                 patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()):
                result, _, _ = await client.generate_image("test-at", "test-project", "test", None, "IMAGE_ASPECT_RATIO_LANDSCAPE",
                    google_cookies="SID=test-only", preserve_parameters=True, native_options=options)
        self.assertEqual(result["media"], [media])
        self.assertEqual(read_media.await_args.args[1], media_id)
        self.assertEqual(browser.generate_native_image.await_count, 1)

    async def test_native_image_mismatched_settings_cannot_be_reported_as_success(self):
        options = {"model_label": "Nano Banana 2.1", "aspect_ratio": "16:9", "image_count": 1, "reference_images_count": 0, "max_credits": 0}
        settings = {"model_label": "Nano Banana Pro", "aspect_ratio": "16:9", "image_count": 1, "max_credits": 0,
            "credits_shown": 0, "verified_before_submit": True}
        browser = SimpleNamespace(generate_native_image=AsyncMock(return_value={
            "frontendRpc": "ogiZ0b", "responseText": "[]", "native_settings": settings,
        }))
        with patch.object(config, "_config", self.settings):
            client = FlowClient(proxy_manager=None)
            client._personal_browser_service = browser
            with patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()):
                with self.assertRaises(GenerationOutcomeUnknown):
                    await client.generate_image("test-at", "test-project", "test", None, "IMAGE_ASPECT_RATIO_LANDSCAPE",
                        google_cookies="SID=test-only", preserve_parameters=True, native_options=options)
        self.assertEqual(browser.generate_native_image.await_count, 1)

    async def test_handler_delivers_native_selection_evidence_without_claiming_rpc_identity(self):
        model = "gemini-nano-banana-2.1-square"
        native_settings = {"model_label": "Nano Banana 2.1", "aspect_ratio": "1:1", "image_count": 1, "verified_before_submit": True, "credits_shown": 0, "max_credits": 0}
        media_url = "https://example.invalid/image/native.png"
        response = json.dumps([["wrb.fr", "ogiZ0b", json.dumps([[["native-media", media_url]]])]])
        browser = SimpleNamespace(generate_native_image=AsyncMock(return_value={
            "frontendRpc": "ogiZ0b", "responseText": response, "native_settings": native_settings,
        }))
        result, _ = await self._run_handler(browser, model)
        self.assertNotIn("error", result)
        self.assertEqual(result["media"][0]["url"], media_url)
        self.assertEqual(result["native_settings"], native_settings)
        self.assertEqual(result["generation_transport"], "native_ui")
        self.assertIsNone(result["resolved_model"])
        self.assertFalse(result["upstream_model_verified"])
        self.assertEqual(result["actual_upstream_model"], "unknown")

    async def _run_handler(self, browser, model="gemini-nano-banana-2.1-square", status_payload=None):
        settings = {**self.settings, "cache": {"enabled": False}, "flow": {**self.settings["flow"], "poll_interval": 0.1}}
        async def read_only_rpc(**kwargs):
            if status_payload is not None and kwargs["rpc_id"] == "jwpduf":
                return status_payload
            raise AssertionError("no direct generation RPC")
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "_config", settings):
            db = Database(db_path=f"{directory}/test.db")
            await db.init_db()
            await db.init_config_from_toml({"global": {"admin_username": "test-only", "admin_password": "test-only", "api_key": "test-only"}})
            token_id = await db.add_token(Token(st="test-only", at="test-only", email="test@example.invalid",
                google_cookies="SID=test-only", at_expires=datetime.now(timezone.utc) + timedelta(hours=5), user_paygate_tier="PAYGATE_TIER_TWO"))
            await db.add_project(Project(project_id="test-project", token_id=token_id, project_name="test"))
            client = FlowClient(proxy_manager=None, db=db)
            client._personal_browser_service = browser
            manager = TokenManager(db, client)
            handler = GenerationHandler(client, manager, LoadBalancer(manager), db, None, None)
            with patch.object(client, "get_credits", AsyncMock(return_value={"credits": 100, "userPaygateTier": "PAYGATE_TIER_TWO"})), \
                 patch.object(client, "_current_rpc", AsyncMock(side_effect=read_only_rpc)), \
                 patch.object(client, "_get_recaptcha_token", AsyncMock(side_effect=AssertionError("no paid captcha"))), \
                 patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()):
                chunks = [chunk async for chunk in handler.handle_generation(model, "native test", preserve_parameters=True)]
            result = json.loads(chunks[-1])
            stats = await db.get_token_stats(token_id)
        return result, stats

    async def test_handler_does_not_penalize_account_when_ui_settings_or_budget_reject_before_submit(self):
        class UIError(RuntimeError):
            submission_started = False
        browser = SimpleNamespace(generate_native_image=AsyncMock(side_effect=UIError("page cost exceeds budget")))
        result, stats = await self._run_handler(browser)
        self.assertIn("page cost exceeds budget", result["error"]["message"])
        self.assertNotEqual(result["error"].get("code"), "outcome_unknown")
        self.assertEqual(stats.consecutive_error_count, 0)
        self.assertEqual(browser.generate_native_image.await_count, 1)

    async def test_handler_exposes_only_safe_credit_limit_fields_for_pre_submit_budget_rejection(self):
        class BudgetError(RuntimeError):
            submission_started = False
            code = "native_credit_limit"
            credits_shown = 12
            max_credits = 0
        browser = SimpleNamespace(generate_native_image=AsyncMock(side_effect=BudgetError("fixture-private-text")))
        result, stats = await self._run_handler(browser)
        error = result["error"]
        self.assertEqual(error["code"], "native_credit_limit")
        self.assertEqual((error["credits_shown"], error["max_credits"], error["status_code"]), (12, 0, 400))
        self.assertNotIn("fixture-private-text", json.dumps(result))
        self.assertFalse(error.get("outcome_unknown", False))
        self.assertEqual(stats.consecutive_error_count, 0)
        self.assertEqual(browser.generate_native_image.await_count, 1)

    async def test_native_reference_images_are_rejected_before_account_or_upload_access(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = SimpleNamespace(clear_request_fingerprint=lambda: None)
        with patch.object(config, "_config", self.settings):
            with self.assertRaises(ValueError):
                model_capabilities.validate_generation_request("gemini-nano-banana-2.1", 1)
            chunks = [chunk async for chunk in handler.handle_generation(
                "gemini-nano-banana-2.1", "keep the reference", images=[b"fixture"], preserve_parameters=True)]
        error = json.loads(chunks[-1])["error"]
        self.assertEqual(error["status_code"], 400)
        self.assertIn("参考图", error["message"])

    async def test_native_submission_timeout_is_unknown_without_any_retry(self):
        options = {"model_label": "Nano Banana 2.1", "aspect_ratio": "16:9", "image_count": 1, "reference_images_count": 0}
        browser = SimpleNamespace(generate_native_image=AsyncMock(side_effect=TimeoutError("fixture response timeout")))
        with patch.object(config, "_config", self.settings):
            client = FlowClient(proxy_manager=None)
            client._personal_browser_service = browser
            with patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()):
                with self.assertRaises(GenerationOutcomeUnknown):
                    await client.generate_image("test-at", "test-project", "test", None, "IMAGE_ASPECT_RATIO_LANDSCAPE",
                        google_cookies="SID=test-only", preserve_parameters=True, native_options=options)
        self.assertEqual(browser.generate_native_image.await_count, 1)

    async def test_ui_preflight_failure_is_not_mislabeled_as_unknown(self):
        class UIError(RuntimeError):
            submission_started = False
        options = {"model_label": "Nano Banana 2.1", "aspect_ratio": "16:9", "image_count": 1, "reference_images_count": 0}
        browser = SimpleNamespace(generate_native_image=AsyncMock(side_effect=UIError("settings could not be read")))
        with patch.object(config, "_config", self.settings):
            client = FlowClient(proxy_manager=None)
            client._personal_browser_service = browser
            with patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()), \
                 patch.object(client, "_handle_retryable_generation_error", AsyncMock()):
                with self.assertRaises(UIError):
                    await client.generate_image("test-at", "test-project", "test", None, "IMAGE_ASPECT_RATIO_LANDSCAPE",
                        google_cookies="SID=test-only", preserve_parameters=True, native_options=options)
        self.assertEqual(browser.generate_native_image.await_count, 1)

    async def test_personal_catalog_has_native_video_with_observed_options_and_no_reference_support(self):
        with patch.object(config, "_config", self.settings):
            video = model_capabilities.validate_generation_request("native-omni-1.1-flash-portrait-360p-4s", 0)
            self.assertTrue(video["available"])
            self.assertEqual(video["generation_transport"], "native_ui")
            self.assertEqual(video["duration_seconds"], 4)
            self.assertEqual(video["resolution"], "360p")
            self.assertEqual(video["max_reference_images"], 0)
            self.assertFalse(video["live_generation_verified"])
            self.assertFalse(video["upstream_model_verified"])
            with self.assertRaises(ValueError):
                model_capabilities.validate_generation_request(video["id"], 1)
            legacy = next(item for item in model_capabilities.get_model_capabilities() if item["id"] == "omni-1.1-flash")
            self.assertFalse(legacy["available"])

    async def test_native_video_submission_and_read_only_status_use_observed_ui_and_existing_normalizer(self):
        options = {"model_label": "Omni 1.1 Flash", "aspect_ratio": "9:16", "resolution": "360p", "duration_seconds": 4,
            "video_count": 1, "reference_images_count": 0, "max_credits": 0}
        settings = {key: value for key, value in options.items() if key != "reference_images_count"}
        settings.update(verified_before_submit=True, credits_shown=0)
        operation_id, media_id = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
        record = [operation_id, "test-project", media_id]
        response = json.dumps([["wrb.fr", "YhhmEf", json.dumps([[record]])]])
        browser = SimpleNamespace(generate_native_video=AsyncMock(return_value={
            "frontendRpc": "YhhmEf", "responseText": response, "native_settings": settings,
        }))
        with patch.object(config, "_config", self.settings):
            client = FlowClient(proxy_manager=None)
            client._personal_browser_service = browser
            with patch.object(client, "_current_rpc", AsyncMock(side_effect=AssertionError("no direct generation RPC"))), \
                 patch.object(client, "_get_recaptcha_token", AsyncMock(side_effect=AssertionError("no paid captcha"))), \
                 patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()):
                result = await client.generate_video_text("test-at", "test-project", "test video", None, "VIDEO_ASPECT_RATIO_PORTRAIT",
                    google_cookies="SID=test-only", native_options=options)
            self.assertEqual(result["operations"][0]["name"], operation_id)
            self.assertIsNone(result["operations"][0]["modelKey"])
            self.assertEqual(result["native_settings"], settings)
            video_url = "https://example.invalid/video/native.mp4"
            with patch.object(client, "_current_rpc", AsyncMock(return_value=[[record + [video_url]]])) as status_rpc:
                status = await client.check_video_status("test-at", result["operations"], google_cookies="SID=test-only")
            self.assertEqual(status_rpc.await_args.kwargs["rpc_id"], "jwpduf")
            self.assertEqual(status["operations"][0]["status"], "MEDIA_GENERATION_STATUS_SUCCESSFUL")
        self.assertEqual(browser.generate_native_video.await_count, 1)
        self.assertEqual(browser.generate_native_video.await_args.kwargs["native_options"], options)

    async def test_native_video_handler_delivers_ui_evidence_without_rpc_identity(self):
        settings = {"model_label": "Omni 1.1 Flash", "aspect_ratio": "16:9", "resolution": "720p", "duration_seconds": 6,
            "video_count": 1, "max_credits": 0, "credits_shown": 0, "verified_before_submit": True}
        media_url = "https://example.invalid/video/native.mp4"
        response = json.dumps([["wrb.fr", "YhhmEf", json.dumps([[media_url]])]])
        browser = SimpleNamespace(generate_native_video=AsyncMock(return_value={
            "frontendRpc": "YhhmEf", "responseText": response, "native_settings": settings,
        }))
        result, _ = await self._run_handler(browser, "native-omni-1.1-flash-landscape-720p-6s")
        self.assertNotIn("error", result)
        self.assertEqual(result["media"][0]["url"], media_url)
        self.assertEqual(result["native_settings"], settings)
        self.assertEqual(result["generation_transport"], "native_ui")
        self.assertIsNone(result["resolved_model"])
        self.assertEqual(result["actual_upstream_model"], "unknown")

    async def test_native_video_handler_persists_operation_and_delivers_polled_media(self):
        settings = {"model_label": "Omni 1.1 Flash", "aspect_ratio": "16:9", "resolution": "360p", "duration_seconds": 4,
            "video_count": 1, "max_credits": 0, "credits_shown": 0, "verified_before_submit": True, "input_mode": "素材"}
        record = ["11111111-1111-4111-8111-111111111111", "test-project", "22222222-2222-4222-8222-222222222222"]
        media_url = "https://example.invalid/video/native.mp4"
        response = json.dumps([["wrb.fr", "MZZa6b", json.dumps([[record]])]])
        browser = SimpleNamespace(generate_native_video=AsyncMock(return_value={
            "frontendRpc": "MZZa6b", "responseText": response, "native_settings": settings,
        }))
        result, stats = await self._run_handler(browser, "native-omni-1.1-flash-landscape-360p-4s", [[record + [media_url]]])
        self.assertNotIn("error", result)
        self.assertEqual(result["media"][0]["url"], media_url)
        self.assertEqual(result["native_settings"], settings)
        self.assertEqual(stats.video_count, 1)
        self.assertEqual(browser.generate_native_video.await_count, 1)

    async def test_native_video_unexpected_rpc_and_multiple_operations_remain_unknown(self):
        options = {"model_label": "Omni 1.1 Flash", "aspect_ratio": "16:9", "resolution": "360p", "duration_seconds": 4,
            "video_count": 1, "reference_images_count": 0, "max_credits": 0}
        settings = {key: value for key, value in options.items() if key != "reference_images_count"}
        settings.update(verified_before_submit=True, credits_shown=0, input_mode="素材")
        records = [["11111111-1111-4111-8111-111111111111", "test-project", "22222222-2222-4222-8222-222222222222"],
                   ["33333333-3333-4333-8333-333333333333", "test-project", "44444444-4444-4444-8444-444444444444"]]
        for rpc in ("unexpected", "MZZa6b"):
            with self.subTest(rpc=rpc), patch.object(config, "_config", self.settings):
                browser = SimpleNamespace(generate_native_video=AsyncMock(return_value={
                    "frontendRpc": rpc, "responseText": json.dumps([["wrb.fr", rpc, json.dumps([records])]]), "native_settings": settings}))
                client = FlowClient(proxy_manager=None)
                client._personal_browser_service = browser
                with patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()):
                    with self.assertRaises(GenerationOutcomeUnknown):
                        await client.generate_video_text("test-at", "test-project", "test", None, "VIDEO_ASPECT_RATIO_LANDSCAPE",
                            google_cookies="SID=test-only", native_options=options)
                self.assertEqual(browser.generate_native_video.await_count, 1)

    async def test_native_video_reference_images_fail_before_any_account_lookup(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = SimpleNamespace(clear_request_fingerprint=lambda: None)
        with patch.object(config, "_config", self.settings):
            chunks = [chunk async for chunk in handler.handle_generation(
                "native-omni-1.1-flash-landscape-360p-4s", "reference", images=[b"fixture"], preserve_parameters=True)]
        self.assertEqual(json.loads(chunks[-1])["error"]["status_code"], 400)

    async def test_native_video_missing_or_mismatched_evidence_cannot_report_success(self):
        options = {"model_label": "Omni 1.1 Flash", "aspect_ratio": "16:9", "resolution": "360p", "duration_seconds": 4,
            "video_count": 1, "reference_images_count": 0, "max_credits": 0}
        confirmed = {key: value for key, value in options.items() if key != "reference_images_count"}
        confirmed.update(verified_before_submit=True, credits_shown=0)
        cases = [None, {**confirmed, "resolution": "720p"}, {**confirmed, "credits_shown": 1}]
        for evidence in cases:
            with self.subTest(evidence=evidence), patch.object(config, "_config", self.settings):
                browser = SimpleNamespace(generate_native_video=AsyncMock(return_value={
                    "frontendRpc": "YhhmEf", "responseText": "[]", "native_settings": evidence}))
                client = FlowClient(proxy_manager=None)
                client._personal_browser_service = browser
                with patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()):
                    with self.assertRaises(GenerationOutcomeUnknown):
                        await client.generate_video_text("test-at", "test-project", "test video", None, "VIDEO_ASPECT_RATIO_LANDSCAPE",
                            google_cookies="SID=test-only", native_options=options)
                self.assertEqual(browser.generate_native_video.await_count, 1)

    async def test_native_video_ambiguous_submission_is_unknown_and_never_resubmitted(self):
        options = {"model_label": "Omni 1.1 Flash", "aspect_ratio": "16:9", "resolution": "360p", "duration_seconds": 4,
            "video_count": 1, "reference_images_count": 0, "max_credits": 0}
        browser = SimpleNamespace(generate_native_video=AsyncMock(side_effect=TimeoutError("fixture video timeout")))
        with patch.object(config, "_config", self.settings):
            client = FlowClient(proxy_manager=None)
            client._personal_browser_service = browser
            with patch.object(client, "_notify_browser_captcha_request_finished", AsyncMock()):
                with self.assertRaises(GenerationOutcomeUnknown):
                    await client.generate_video_text("test-at", "test-project", "test video", None, "VIDEO_ASPECT_RATIO_LANDSCAPE",
                        google_cookies="SID=test-only", native_options=options)
        self.assertEqual(browser.generate_native_video.await_count, 1)

    async def test_native_options_carry_the_request_credit_budget(self):
        with patch.object(config, "_config", self.settings):
            model = MODEL_CONFIG["gemini-nano-banana-2.1"]
            self.assertEqual(model_capabilities.get_native_image_options(model)["max_credits"], 0)
            context = set_native_credit_limit(5)
            try:
                self.assertEqual(model_capabilities.get_native_image_options(model)["max_credits"], 5)
            finally:
                reset_native_credit_limit(context)


if __name__ == "__main__":
    unittest.main()
