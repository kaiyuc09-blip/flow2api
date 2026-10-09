"""Configured model catalog contracts; all captcha credentials are synthetic."""
import unittest
from unittest.mock import patch

from src.core.config import config
from src.services.model_capabilities import get_model_capabilities, validate_generation_request


class CaptchaCatalogTests(unittest.TestCase):
    def test_yescaptcha_catalog_enables_rpc_references_and_omits_native_ui(self):
        with patch.object(config, "_config", {"captcha": {
            "captcha_method": "yescaptcha", "yescaptcha_api_key": "synthetic-test-only",
        }}):
            entries = get_model_capabilities()
            image = validate_generation_request("gemini-3.1-flash-image-square", 3)
            self.assertTrue(image["available"])
            self.assertEqual(image["max_reference_images"], 3)
            self.assertEqual(image["limits_source"], "local_policy")
            self.assertEqual(image["verification_state"], "implemented_not_live_verified")
            self.assertFalse(any(entry.get("generation_transport") == "native_ui" for entry in entries))
            self.assertFalse(any(entry["id"].startswith(("gemini-nano-banana-2.1", "native-omni-1.1-flash-")) for entry in entries))
            with self.assertRaises(ValueError):
                validate_generation_request("gemini-3.1-flash-image-square", 4)

    def test_native_models_remain_listed_only_for_personal_mode(self):
        for method in ("personal", "browser", "remote_browser", "extension", "yescaptcha", "capmonster", "ezcaptcha", "capsolver", "captcharun"):
            with self.subTest(method=method), patch.object(config, "_config", {"captcha": {"captcha_method": method}}):
                ids = {entry["id"] for entry in get_model_capabilities()}
                self.assertEqual("gemini-nano-banana-2.1-square" in ids, method == "personal")
                self.assertEqual("native-omni-1.1-flash-landscape-360p-4s" in ids, method == "personal")

    def test_unconfigured_yescaptcha_does_not_claim_rpc_available(self):
        with patch.object(config, "_config", {"captcha": {"captcha_method": "yescaptcha"}}):
            image = next(entry for entry in get_model_capabilities() if entry["id"] == "gemini-3.1-flash-image-square")
            self.assertFalse(image["available"])
            with self.assertRaises(ValueError):
                validate_generation_request(image["id"], 0)


if __name__ == "__main__":
    unittest.main()
