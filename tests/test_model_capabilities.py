import unittest
from unittest.mock import patch

from src.core.config import config
from src.services import model_capabilities


class ModelCapabilitiesTests(unittest.TestCase):
    def test_extension_needing_source_video_is_unavailable_in_image_only_agent_request(self):
        with patch.object(config, "_config", {"captcha": {"captcha_method": "yescaptcha", "yescaptcha_api_key": "test-only"}}):
            with self.assertRaisesRegex(ValueError, "源视频"):
                model_capabilities.validate_generation_request("veo-3.1-lite-extend-8s-landscape", 0)

    def test_agent_rejects_missing_channel_and_invalid_image_counts(self):
        with patch.object(config, "_config", {"captcha": {"captcha_method": "personal"}}):
            with self.assertRaisesRegex(ValueError, "第三方"):
                model_capabilities.validate_generation_request("gemini-3.1-flash-image-landscape", 0)
        with patch.object(config, "_config", {"captcha": {"captcha_method": "yescaptcha", "yescaptcha_api_key": "test-only"}}):
            for model, count in (("not-a-model", 0), ("gemini-3.1-flash-image-landscape", 4),
                                 ("gemini-3.1-flash-image-landscape", -1), ("veo-3.1-fast", 1),
                                 ("veo-3.1-fast-i2v-4s-landscape", 0)):
                with self.subTest(model=model, count=count), self.assertRaises(ValueError):
                    model_capabilities.validate_generation_request(model, count)

    def test_agent_does_not_advertise_unverified_fast_or_quality_extension(self):
        with patch.object(config, "_config", {"captcha": {"captcha_method": "yescaptcha", "yescaptcha_api_key": "test-only"}}):
            for family in ("fast", "quality"):
                with self.assertRaisesRegex(ValueError, "续写|protocol|协议"):
                    model_capabilities.validate_generation_request(f"veo-3.1-{family}-extend-8s-landscape", 0)

    def test_configured_omni_keeps_duration_and_rejects_extra_references(self):
        with patch.object(config, "_config", {"captcha": {"captcha_method": "yescaptcha", "yescaptcha_api_key": "test-only"}}):
            entry = model_capabilities.validate_generation_request("omni-1.1-flash-10s-portrait", 3)
            self.assertEqual(entry["duration_seconds"], 10)
            self.assertEqual(entry["aspect_ratio"], "9:16")
            self.assertEqual(entry["verification_state"], "implemented_not_live_verified")
            with self.assertRaisesRegex(ValueError, "3"):
                model_capabilities.validate_generation_request("omni-1.1-flash-10s-portrait", 4)

    def test_new_model_requires_protocol_evidence_before_generation(self):
        with patch.object(config, "_config", {"captcha": {"captcha_method": "yescaptcha", "yescaptcha_api_key": "test-only"}}):
            model = next(item for item in model_capabilities.get_model_capabilities()
                         if item["id"] == "gemini-nano-banana-2.1")
            self.assertFalse(model["available"])
            self.assertEqual(model["verification_state"], "needs_protocol_verification")
            with self.assertRaisesRegex(ValueError, "protocol|协议"):
                model_capabilities.validate_generation_request("gemini-nano-banana-2.1", 0)


if __name__ == "__main__":
    unittest.main()
