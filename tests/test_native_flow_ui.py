"""Native settings contract with a synthetic UI; never starts a browser."""
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.native_flow_ui import (
    NativeFlowUIError, configure_native_image_settings, validate_native_image_options,
    configure_native_video_settings,
    NodriverFlowSettingsUI,
    NativeCreditLimitError,
)


class SettingsPage:
    """Interactive controls matching the account-visible Flow settings contract."""
    def __init__(self):
        self.settings_open = False
        self.models_open = False
        self.mode = "视频"
        self.model = "Nano Banana 2.1"
        self.ratio = "1:1"
        self.count = 4
        self.cost = "0 个点数"
        self.resolution = "720p"
        self.duration = 8
        self.input_mode = "素材"
        self.clicks = []

    async def snapshot(self):
        controls = [{"selector": "#settings", "role": "button", "name": "设置触发器",
                     "expanded": self.settings_open}]
        if self.settings_open:
            if self.cost is not None:
                controls += [{"selector": "#cost", "role": "credit_cost", "name": self.cost}]
            controls += [{"selector": "#" + mode, "role": "radio", "name": mode,
                          "selected": self.mode == mode} for mode in ("图片", "视频")]
            controls += [{"selector": "#model", "role": "button", "name": "选择模型系列",
                          "text": "🍌 " + self.model, "expanded": self.models_open}]
            ratios = ("16:9", "9:16") if self.mode == "视频" else ("16:9", "4:3", "1:1", "3:4", "9:16")
            controls += [{"selector": "#ratio-" + ratio, "role": "radio", "name": "宽高比" + ratio,
                          "selected": self.ratio == ratio} for ratio in ratios]
            if self.mode == "视频":
                controls += [{"selector": "#input-" + mode, "role": "radio", "name": mode,
                              "selected": self.input_mode == mode} for mode in ("帧", "素材")]
                controls += [{"selector": "#resolution-" + resolution, "role": "radio", "name": resolution,
                              "selected": self.resolution == resolution} for resolution in ("360p", "720p")]
                controls += [{"selector": "#duration-" + str(duration), "role": "radio", "name": f"{duration}秒",
                              "selected": self.duration == duration} for duration in (4, 6, 8, 10)]
            controls += [{"selector": "#x" + str(count), "role": "radio", "name": "x" + str(count),
                          "selected": self.count == count} for count in range(1, 5)]
            if self.models_open:
                models = ("Omni 1.1 Flash", "Veo 3.1 Lite", "Veo 3.1 Fast", "Veo 3.1 Quality") if self.mode == "视频" else ("Nano Banana 2.1", "Nano Banana Pro", "Nano Banana 2 Lite")
                controls += [{"selector": "#" + model, "role": "option", "name": "🍌 " + model,
                              "selected": model == self.model} for model in models]
        return controls

    async def click(self, selector):
        self.clicks.append(selector)
        if selector == "#settings":
            self.settings_open = not self.settings_open
        elif selector == "#model":
            self.models_open = not self.models_open
        elif selector in ("#图片", "#视频"):
            self.mode = selector[1:]
        elif selector.startswith(("#Nano Banana", "#Omni")):
            self.model = selector[1:]
            self.models_open = False
        elif selector.startswith("#ratio-"):
            self.ratio = selector[len("#ratio-"):]
        elif selector.startswith("#x"):
            self.count = int(selector[2:])
        elif selector.startswith("#resolution-"):
            self.resolution = selector[len("#resolution-"):]
        elif selector.startswith("#duration-"):
            self.duration = int(selector[len("#duration-"):])
        else:
            raise AssertionError("Unexpected control")


class SyntheticTab:
    def __init__(self, page):
        self.page = page
        self.prompt = ""
        self.submissions = 0
        self.blocked = False
        self.insert_calls = 0

    async def select(self, selector, timeout=5):
        if selector == ".ProseMirror":
            return self
        element = MagicMock()
        async def click():
            await self.page.click(selector)
        element.click = click
        return element

    async def click(self):
        if self.page.settings_open:
            raise AssertionError("Settings must be closed before entering the prompt")

    async def send_keys(self, text):
        self.prompt += text

    async def send(self, command):
        request = next(command)
        if request["method"] == "Input.insertText":
            self.prompt += request["params"]["text"]
            self.insert_calls += 1
        if request["method"] == "Input.dispatchKeyEvent" and request["params"]["type"] == "keyDown":
            if request["params"].get("key") == "Enter":
                self.submissions += 1


def native_service_fixture(media_type="image"):
    from src.services.browser_captcha_personal import BrowserCaptchaService, ResidentTabInfo
    page = SettingsPage()
    tab = SyntheticTab(page)
    resident = ResidentTabInfo(tab, "test-slot", project_id="test-project")
    service = BrowserCaptchaService()
    for method in ("initialize", "_consume_resident_slot_reservation", "_release_resident_slot_reservation",
                   "_tab_get", "_wait_for_document_ready", "_dismiss_native_page_overlays",
                   "_cache_session_cookies_for_computed", "_maybe_execute_pending_fresh_profile_restart"):
        setattr(service, method, AsyncMock())
    service._ensure_resident_tab = AsyncMock(return_value=("test-slot", resident))
    service._load_token_cookie = AsyncMock(return_value="SID=test-only")
    resident.token_id = 1
    resident.cookie_signature = service._normalize_cookie_signature("SID=test-only")
    service._ensure_resident_token_binding = AsyncMock(return_value=True)
    service._wait_for_recaptcha = AsyncMock(return_value=True)
    service._wait_for_native_prompt_editor = AsyncMock(return_value=True)
    service._execute_recaptcha_on_tab = AsyncMock(side_effect=AssertionError("Explicit settings must use the normal UI"))
    service._submit_native_stream_chat_with_resident = AsyncMock(side_effect=AssertionError("No prompt-only direct fetch"))
    service._refresh_last_fingerprint = AsyncMock(return_value={})
    for method in ("_remember_fingerprint", "_remember_project_affinity", "_remember_token_affinity",
                   "_mark_browser_health", "_record_browser_solve_success"):
        setattr(service, method, MagicMock())

    async def evaluate(_tab, script, *, label, **kwargs):
        if not resident.solve_lock.locked():
            raise AssertionError("Settings and submission must hold the resident lock")
        if label.endswith(":settings"):
            return json.dumps(await page.snapshot())
        if label.endswith(":draft"):
            return json.dumps({"available": True, "empty": not bool(tab.prompt), "hasMedia": False})
        if label.endswith(":blocking_overlay"):
            return tab.blocked
        if label.endswith(":prompt_readback"):
            return tab.prompt
        if label == "install_native_generation_observer":
            return True
        if label.startswith("read_native_generation:"):
            return json.dumps({"done": tab.submissions == 1, "status": 200,
                "responseText": "synthetic-response", "frontendRpc": "YhhmEf" if media_type == "video" else "ogiZ0b", "requestBody": ""})
        raise AssertionError("Unexpected browser read")

    service._tab_evaluate = AsyncMock(side_effect=evaluate)
    return service, tab, page


class NativeFlowUITests(unittest.IsolatedAsyncioTestCase):
    async def test_credit_cost_is_checked_again_after_prompt_entry(self):
        service, tab, page = native_service_fixture()
        original_send = tab.send
        async def change_quote_after_input(command):
            await original_send(command)
            if tab.insert_calls:
                page.cost = "12 个点数"
        tab.send = change_quote_after_input
        with self.assertRaisesRegex(NativeCreditLimitError, "超过") as error:
            await service.generate_native_image(project_id="test-project", prompt="test", token_id=1, timeout=30,
                native_options={"model_label": "Nano Banana 2.1", "aspect_ratio": "1:1"})
        self.assertEqual((error.exception.code, error.exception.credits_shown, error.exception.max_credits),
                         ("native_credit_limit", 12, 0))
        self.assertEqual(tab.insert_calls, 1)
        self.assertEqual(tab.submissions, 0)

    async def test_native_generation_rejects_failed_or_changed_account_binding(self):
        for binding_state in ("failed", "changed"):
            with self.subTest(binding_state=binding_state):
                service, tab, page = native_service_fixture()
                if binding_state == "failed":
                    service._ensure_resident_token_binding.return_value = False
                else:
                    service._ensure_resident_tab.return_value[1].token_id = 2
                with self.assertRaises(NativeFlowUIError):
                    await service.generate_native_image(project_id="test-project", prompt="test", token_id=1, timeout=30,
                        native_options={"model_label": "Nano Banana 2.1", "aspect_ratio": "1:1"})
                self.assertEqual(tab.submissions, 0)

    async def test_consent_overlay_is_never_auto_accepted(self):
        service, tab, page = native_service_fixture()
        tab.blocked = True
        with self.assertRaises(NativeFlowUIError):
            await service.generate_native_image(project_id="test-project", prompt="test", token_id=1, timeout=30,
                native_options={"model_label": "Nano Banana 2.1", "aspect_ratio": "1:1"})
        service._dismiss_native_page_overlays.assert_not_awaited()
        self.assertEqual(tab.submissions, 0)

    async def test_long_multiline_prompt_uses_one_insert_and_one_submission(self):
        service, tab, page = native_service_fixture()
        prompt = "产品第一行\n" + "long product description " * 400
        await service.generate_native_image(project_id="test-project", prompt=prompt, token_id=1, timeout=30,
            native_options={"model_label": "Nano Banana 2.1", "aspect_ratio": "1:1"})
        self.assertEqual((tab.insert_calls, tab.submissions, tab.prompt), (1, 1, prompt))
        service._dismiss_native_page_overlays.assert_not_awaited()

    async def test_replaced_settings_control_is_not_clicked(self):
        tab = MagicMock()
        element = MagicMock()
        element.click = AsyncMock(side_effect=AssertionError("Must not click a replaced control"))
        tab.select = AsyncMock(return_value=element)
        evaluate = AsyncMock(side_effect=[
            [{"selector": "#action", "role": "button", "name": "设置触发器"}],
            [{"selector": "#action", "role": "button", "name": "生成"}],
        ])
        ui = NodriverFlowSettingsUI(tab, evaluate, label="test")
        with self.assertRaises(NativeFlowUIError):
            await configure_native_image_settings(ui, {"model_label": "Nano Banana 2.1", "aspect_ratio": "1:1"})
        element.click.assert_not_awaited()

    async def test_public_video_zero_budget_blocks_paid_ui_and_invalid_inputs_never_initialize(self):
        options = {"model_label": "Omni 1.1 Flash", "aspect_ratio": "16:9", "resolution": "720p",
                   "duration_seconds": 8, "video_count": 1}
        service, tab, page = native_service_fixture("video")
        page.cost = "12 个点数"
        with self.assertRaisesRegex(NativeFlowUIError, "超过"):
            await service.generate_native_video(project_id="test-project", prompt="test", token_id=1,
                                                timeout=30, native_options=options)
        self.assertEqual(tab.submissions, 0)
        service.initialize.reset_mock()
        for invalid in ({"reference_images_count": 1}, {"duration_seconds": 12}, {"resolution": "1080p"},
                        {"model_label": "Veo 3.1 Lite"}, {"video_count": 4}, {"max_credits": -1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await service.generate_native_video(project_id="test-project", prompt="test", token_id=1,
                                                    timeout=30, native_options={**options, **invalid})
        service.initialize.assert_not_awaited()

    async def test_video_pool_forwards_settings_to_reserved_worker(self):
        from src.services.browser_captcha_personal import _PersonalBrowserPoolService
        pool = object.__new__(_PersonalBrowserPoolService)
        worker = MagicMock()
        worker.generate_native_video = AsyncMock(return_value={"frontendRpc": "YhhmEf"})
        pool._ensure_workers = AsyncMock()
        pool._acquire_worker = AsyncMock(return_value=(0, worker))
        pool._release_worker_reservation = AsyncMock()
        pool._remember_native_session_worker = MagicMock()
        options = {"model_label": "Omni 1.1 Flash", "aspect_ratio": "16:9", "resolution": "720p",
                   "duration_seconds": 8, "video_count": 1, "reference_images_count": 0, "max_credits": 0}
        result = await pool.generate_native_video(project_id="test-project", prompt="test", token_id=1,
                                                 timeout=30, native_options=options)
        self.assertEqual(result["frontendRpc"], "YhhmEf")
        self.assertEqual(worker.generate_native_video.await_args.kwargs["native_options"], options)
        pool._release_worker_reservation.assert_awaited_once_with(0)

    async def test_public_native_video_configures_omni_and_returns_observed_rpc_once(self):
        service, tab, page = native_service_fixture("video")
        page.cost = "12 个点数"
        result = await service.generate_native_video(project_id="test-project", prompt="slow camera motion", token_id=1, timeout=30,
            native_options={"model_label": "Omni 1.1 Flash", "aspect_ratio": "9:16", "video_count": 1,
                            "duration_seconds": 6, "resolution": "360p", "max_credits": 12})
        self.assertEqual((page.model, page.ratio, page.count, page.duration, page.resolution),
                         ("Omni 1.1 Flash", "9:16", 1, 6, "360p"))
        self.assertEqual((tab.prompt, tab.submissions), ("slow camera motion", 1))
        self.assertEqual(result["frontendRpc"], "YhhmEf")
        self.assertEqual(result["native_settings"]["credits_shown"], 12)

    async def test_omni_video_duration_resolution_and_single_output_are_read_back(self):
        for duration in (4, 6, 8, 10):
            for resolution in ("360p", "720p"):
                with self.subTest(duration=duration, resolution=resolution):
                    ui = SettingsPage()
                    result = await configure_native_video_settings(ui, {"model_label": "Omni 1.1 Flash",
                        "aspect_ratio": "16:9", "resolution": resolution, "duration_seconds": duration,
                        "video_count": 1, "reference_images_count": 0, "max_credits": 0})
                    self.assertEqual((ui.mode, ui.model, ui.ratio, ui.count, ui.resolution, ui.duration),
                                     ("视频", "Omni 1.1 Flash", "16:9", 1, resolution, duration))
                    self.assertEqual(result, {"model_label": "Omni 1.1 Flash", "aspect_ratio": "16:9",
                        "resolution": resolution, "duration_seconds": duration, "video_count": 1,
                        "verified_before_submit": True, "credits_shown": 0, "max_credits": 0,
                        "input_mode": "素材"})

    async def test_existing_prompt_draft_is_not_appended_to_or_submitted(self):
        service, tab, page = native_service_fixture()
        tab.prompt = "existing user draft"
        with self.assertRaises(NativeFlowUIError):
            await service.generate_native_image(project_id="test-project", prompt="new request", token_id=1, timeout=30,
                native_options={"model_label": "Nano Banana 2.1", "aspect_ratio": "1:1", "image_count": 1})
        self.assertEqual((tab.prompt, tab.submissions), ("existing user draft", 0))

    async def test_unconfirmed_quantity_and_excess_cost_never_submit(self):
        for failure in ("count", "cost"):
            with self.subTest(failure=failure):
                service, tab, page = native_service_fixture()
                if failure == "cost":
                    page.cost = "2 个点数"
                else:
                    original_click = page.click
                    async def refuse_quantity(selector):
                        if selector != "#x1":
                            await original_click(selector)
                    page.click = refuse_quantity
                with patch("src.services.native_flow_ui.asyncio.sleep", AsyncMock()), self.assertRaises(NativeFlowUIError) as error:
                    await service.generate_native_image(project_id="test-project", prompt="test", token_id=1, timeout=30,
                        native_options={"model_label": "Nano Banana 2.1", "aspect_ratio": "1:1", "image_count": 1})
                self.assertIs(error.exception.submission_started, False)
                self.assertEqual(tab.submissions, 0)

    async def test_lost_browser_response_after_enter_is_unknown_and_is_not_resubmitted(self):
        from src.services.browser_captcha_personal import NativeGenerationOutcomeUnknownError
        service, tab, page = native_service_fixture()
        original_send = tab.send
        async def lose_response(command):
            await original_send(command)
            if tab.submissions:
                raise TimeoutError("synthetic lost command response")
        tab.send = lose_response
        with self.assertRaises(NativeGenerationOutcomeUnknownError):
            await service.generate_native_image(project_id="test-project", prompt="test", token_id=1, timeout=30,
                native_options={"model_label": "Nano Banana 2.1", "aspect_ratio": "1:1", "image_count": 1})
        self.assertEqual(tab.submissions, 1)
        service._submit_native_stream_chat_with_resident.assert_not_awaited()

    async def test_public_native_generation_sets_reads_and_submits_once_under_resident_lock(self):
        service, tab, page = native_service_fixture()
        result = await service.generate_native_image(project_id="test-project", prompt="a product", token_id=1, timeout=30,
            native_options={"model_label": "Nano Banana Pro", "aspect_ratio": "9:16", "image_count": 1})
        self.assertEqual((page.model, page.ratio, page.count), ("Nano Banana Pro", "9:16", 1))
        self.assertEqual((tab.prompt, tab.submissions), ("a product", 1))
        self.assertTrue(result["native_settings"]["verified_before_submit"])
        self.assertEqual(result["frontendRpc"], "ogiZ0b")
        service._execute_recaptcha_on_tab.assert_not_awaited()
        service._submit_native_stream_chat_with_resident.assert_not_awaited()

    async def test_selected_model_ratio_and_single_output_are_read_back_from_ui(self):
        for model in ("Nano Banana 2.1", "Nano Banana Pro", "Nano Banana 2 Lite"):
            for ratio in ("16:9", "4:3", "1:1", "3:4", "9:16"):
                with self.subTest(model=model, ratio=ratio):
                    ui = SettingsPage()
                    result = await configure_native_image_settings(ui, {
                        "model_label": model, "aspect_ratio": ratio, "image_count": 1,
                        "reference_images_count": 0})
                    self.assertEqual((ui.mode, ui.model, ui.ratio, ui.count), ("图片", model, ratio, 1))
                    self.assertEqual(result, {"model_label": model, "aspect_ratio": ratio,
                                             "image_count": 1, "verified_before_submit": True,
                                             "credits_shown": 0, "max_credits": 0})
                    self.assertFalse(ui.settings_open)

    async def test_unknown_or_excessive_credit_cost_is_rejected_and_explicit_budget_is_honored(self):
        options = {"model_label": "Nano Banana 2.1", "aspect_ratio": "1:1", "image_count": 1}
        for cost in (None, "点数", "2 个点数"):
            with self.subTest(cost=cost), self.assertRaises(NativeFlowUIError):
                ui = SettingsPage()
                ui.cost = cost
                await configure_native_image_settings(ui, options)
        ui = SettingsPage()
        ui.cost = "2 个点数"
        result = await configure_native_image_settings(ui, {**options, "max_credits": 2})
        self.assertEqual((result["credits_shown"], result["max_credits"]), (2, 2))

    async def test_unsupported_references_and_output_count_fail_before_browser_access(self):
        ui = AsyncMock()
        valid = {"model_label": "Nano Banana 2.1", "aspect_ratio": "16:9",
                 "image_count": 1, "reference_images_count": 0}
        for invalid in ({"reference_images_count": 1}, {"image_count": 4},
                        {"model_label": "NARWHAL"}, {"aspect_ratio": "auto"},
                        {"max_credits": -1}, {"max_credits": True}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await configure_native_image_settings(ui, {**valid, **invalid})
        ui.snapshot.assert_not_awaited()
        ui.click.assert_not_awaited()

    async def test_public_native_generation_rejects_references_before_browser_initialization(self):
        from src.services.browser_captcha_personal import BrowserCaptchaService
        service = BrowserCaptchaService()
        service.initialize = AsyncMock()
        with self.assertRaisesRegex(ValueError, "参考图"):
            await service.generate_native_image(project_id="test-project", prompt="test", token_id=1, timeout=30,
                native_options={"model_label": "Nano Banana 2.1", "aspect_ratio": "1:1", "reference_images_count": 1})
        service.initialize.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
