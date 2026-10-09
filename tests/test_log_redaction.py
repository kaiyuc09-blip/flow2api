"""Credential safety at the debug logger's public output boundary."""

import io
import json
import logging
import unittest
from unittest.mock import patch

from src.core.config import config
from src.core.logger import debug_logger


class LogRedactionTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.handler = logging.StreamHandler(self.output)
        self.previous = list(debug_logger.logger.handlers)
        debug_logger.logger.handlers = [self.handler]
        self.addCleanup(setattr, debug_logger.logger, "handlers", self.previous)
        enabled = patch.dict(config._config, {"debug": {
            "enabled": True, "log_requests": True, "log_responses": True,
            "mask_token": False,
        }})
        enabled.start()
        self.addCleanup(enabled.stop)

    def test_request_headers_never_emit_credentials_even_with_masking_disabled(self):
        secrets = ["synthetic-cookie-secret", "synthetic-auth-secret",
                   "synthetic-api-secret", "synthetic-goog-secret", "short"]
        debug_logger.log_request("POST", "https://images.example/generate", {
            "cOoKiE": "SID=" + secrets[0],
            "AUTHORIZATION": "Bearer " + secrets[1],
            "X-API-KEY": secrets[2], "x-goog-api-key": secrets[3],
            "Proxy-Authorization": "Basic " + secrets[4],
            "Content-Type": "application/json",
        })
        output = self.output.getvalue()
        for secret in secrets:
            self.assertNotIn(secret, output)
        self.assertIn("application/json", output)
        self.assertIn("POST", output)

    def test_nested_credentials_are_removed_from_requests_responses_and_errors(self):
        secret = "synthetic-nested-secret"
        payload = {"status": "pending", "items": [
            {"google_cookies": secret, "password": secret, "accessToken": secret,
             "nested": {"clientKey": secret, "session_token": secret}}
        ]}
        debug_logger.log_request("POST", "https://images.example/", {}, payload)
        debug_logger.log_request("POST", "https://images.example/", {}, json.dumps(payload))
        debug_logger.log_response(200, {"sEt-CoOkIe": secret}, payload)
        debug_logger.log_error("failed", 503, json.dumps(payload))
        output = self.output.getvalue()
        self.assertNotIn(secret, output)
        self.assertIn("pending", output)
        self.assertEqual(payload["items"][0]["password"], secret)

    def test_urls_proxy_credentials_and_freeform_errors_are_redacted(self):
        secret = "synthetic-url-secret"
        signed = f"https://images.example/output.png?x-custom-signature={secret}&width=1024"
        debug_logger.log_request("GET", signed, {}, proxy=f"http://user:{secret}@proxy.example:8080")
        debug_logger.log_response(302, {"Location": signed}, {"url": signed})
        debug_logger.log_info(f"download {signed}")
        debug_logger.log_warning(f"retry Authorization: Bearer {secret}")
        debug_logger.log_error(f"upstream Cookie: SID={secret}; other={secret}")
        debug_logger.log_error(f"credentials password='{secret}' access_token={secret}")
        output = self.output.getvalue()
        self.assertNotIn(secret, output)
        self.assertIn("images.example/output.png", output)
        self.assertIn("302", output)

    def test_freeform_token_diagnostics_never_keep_a_credential_prefix(self):
        secret = "synthetic-full-token-value"
        debug_logger.log_info(f"[reCAPTCHA] get_token 返回: {secret[:15]}...")
        debug_logger.log_warning(f"request failed with Bearer {secret}")
        debug_logger.log_error(f"accessToken: {secret}")
        output = self.output.getvalue()
        self.assertNotIn(secret[:15], output)
        self.assertIn("[reCAPTCHA]", output)

    def test_inline_image_payloads_are_omitted_from_debug_output(self):
        secret_image = "SYNTHETICBASE64IMAGEPAYLOAD"
        payload = {"images": ["data:image/png;base64," + secret_image],
                   "inlineData": {"mimeType": "image/png", "data": secret_image}}
        debug_logger.log_request("POST", "https://images.example/", {}, payload)
        debug_logger.log_response(200, {}, {"encodedImage": secret_image})
        debug_logger.log_error("input data:image/png;base64," + secret_image)
        self.assertNotIn(secret_image, self.output.getvalue())
        self.assertIn("image/png", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
