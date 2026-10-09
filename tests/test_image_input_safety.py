"""Reference image validation through the compatible HTTP API boundary."""

import base64
import io
import json
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from curl_cffi import CurlOpt
from PIL import Image

from src.api import routes
from src.core.auth import verify_api_key_flexible
from src.core.config import config


def png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(output, format="PNG")
    return output.getvalue()


class CaptureHandler:
    def __init__(self, cache_dir):
        self.file_cache = SimpleNamespace(cache_dir=cache_dir)
        self.received_images = None

    async def handle_generation(self, **kwargs):
        self.received_images = kwargs["images"]
        yield json.dumps({"choices": [{"message": {"content": "accepted"}}]})


class DownloadResponse:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.aclose = AsyncMock()

    async def aiter_content(self):
        yield self.content


class ImageInputSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.png = png_bytes()
        (self.cache / "valid.png").write_bytes(self.png)
        (self.root / "private.png").write_bytes(self.png)
        self.handler = CaptureHandler(self.cache)
        previous = routes.generation_handler
        routes.set_generation_handler(self.handler)
        self.addCleanup(routes.set_generation_handler, previous)
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[verify_api_key_flexible] = lambda: "synthetic-test-key"
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def submit(self, uri):
        return self.client.post("/v1/chat/completions", json={
            "model": "gemini-3.1-flash-image",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "edit this image"},
                {"type": "image_url", "image_url": {"url": uri}},
            ]}],
        })

    def test_local_cache_reference_cannot_escape_cache_directory(self):
        with patch("src.api.routes.AsyncSession") as network:
            for uri in ("/tmp/../private.png", "/tmp/%2e%2e/private.png",
                        "/tmp/%252e%252e%252fprivate.png"):
                with self.subTest(uri=uri):
                    self.assertEqual(self.submit(uri).status_code, 400)
            network.assert_not_called()
        self.assertIsNone(self.handler.received_images)

    def test_reference_images_require_valid_base64_and_decodable_image_content(self):
        encoded = base64.b64encode(self.png).decode("ascii")
        self.assertEqual(self.submit("data:image/png;base64," + encoded).status_code, 200)
        self.assertEqual(self.handler.received_images, [self.png])
        for payload in (encoded + "!!", "a", base64.b64encode(b"not an image").decode()):
            with self.subTest(payload=payload):
                self.assertEqual(self.submit("data:image/png;base64," + payload).status_code, 400)
        self.assertEqual(self.submit("data:image/jpeg;base64," + encoded).status_code, 400)

    def test_gemini_inline_images_use_the_same_validation_boundary(self):
        for encoded, expected in ((base64.b64encode(self.png).decode(), 200),
                                  ("not-base64!", 400),
                                  (base64.b64encode(b"not an image").decode(), 400)):
            with self.subTest(encoded=encoded):
                response = self.client.post(
                    "/v1beta/models/gemini-3.1-flash-image:generateContent",
                    json={"contents": [{"role": "user", "parts": [
                        {"text": "edit this image"},
                        {"inlineData": {"mimeType": "image/png", "data": encoded}},
                    ]}]},
                )
                self.assertEqual(response.status_code, expected)

    def test_cache_reads_only_valid_images_in_its_own_directory(self):
        (self.cache / "linked.png").symlink_to(self.root / "private.png")
        (self.cache / "broken.png").write_bytes(b"this is not an image")
        with patch("src.api.routes.AsyncSession") as network:
            self.assertEqual(self.submit("/tmp/valid.png").status_code, 200)
            for uri in ("/tmp/linked.png", "/tmp/broken.png", "/tmp/missing.png"):
                with self.subTest(uri=uri):
                    self.assertEqual(self.submit(uri).status_code, 400)
            network.assert_not_called()

    def test_private_network_image_urls_are_rejected_before_download(self):
        uris = (
            "http://127.0.0.1/private.png", "http://169.254.169.254/latest/meta-data",
            "http://192.168.1.1/a.png", "http://[::1]/a.png", "http://10.0.0.1/a.png",
            "http://[::ffff:127.0.0.1]/a.png", "http://2130706433/a.png",
        )
        with patch("src.api.routes.AsyncSession") as network:
            for uri in uris:
                with self.subTest(uri=uri):
                    self.assertEqual(self.submit(uri).status_code, 400)
            with patch("socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
            ]):
                self.assertEqual(self.submit("http://private.example/a.png").status_code, 400)
            network.assert_not_called()

    def test_configured_cache_url_is_local_only_and_does_not_trust_other_origins(self):
        with patch.dict(config._config, {"cache": {"base_url": "https://cache.example/media"}}):
            with patch("src.api.routes.AsyncSession") as network:
                self.assertEqual(self.submit("https://cache.example/media/tmp/valid.png").status_code, 200)
                self.assertEqual(self.handler.received_images, [self.png])
                self.assertEqual(self.submit("https://cache.example/media/tmp/missing.png").status_code, 400)
                self.assertEqual(self.submit("https://cache.example/media/tmp/%2e%2e/private.png").status_code, 400)
                network.assert_not_called()

    def test_remote_download_checks_tls_and_never_reads_cache_for_foreign_tmp_urls(self):
        other_image = io.BytesIO()
        Image.new("RGB", (5, 5), "red").save(other_image, format="PNG")
        remote_bytes = other_image.getvalue()
        with patch("socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ]), patch("src.api.routes.AsyncSession") as network:
            session = network.return_value.__aenter__.return_value
            session.get.return_value = DownloadResponse(content=remote_bytes)
            result = self.submit("https://other.example/tmp/valid.png")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.handler.received_images, [remote_bytes])
            self.assertTrue(session.get.await_args.kwargs["verify"])
            self.assertFalse(session.get.await_args.kwargs["allow_redirects"])

    def test_redirects_allow_public_images_but_reject_private_destinations(self):
        with patch("socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ]), patch("src.api.routes.AsyncSession") as network:
            session = network.return_value.__aenter__.return_value
            session.get.side_effect = [
                DownloadResponse(302, headers={"Location": "/image.png"}),
                DownloadResponse(content=self.png),
            ]
            self.assertEqual(self.submit("https://other.example/redirect").status_code, 200)
            self.assertEqual(self.handler.received_images, [self.png])
            session.get.reset_mock()
            session.get.side_effect = [DownloadResponse(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data"}
            )]
            self.assertEqual(self.submit("https://other.example/redirect").status_code, 400)
            self.assertEqual(session.get.await_count, 1)

    def test_remote_images_are_decoded_and_download_size_is_bounded(self):
        with patch("socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ]), patch("src.api.routes.AsyncSession") as network:
            session = network.return_value.__aenter__.return_value
            session.get.return_value = DownloadResponse(content=b"not an image")
            self.assertEqual(self.submit("https://other.example/image.png").status_code, 400)
            with patch("src.api.routes.MAX_IMAGE_BYTES", 64):
                response = DownloadResponse(content=self.png)
                session.get.return_value = response
                self.assertEqual(self.submit("https://other.example/image.png").status_code, 413)
                response.aclose.assert_awaited()
                self.assertEqual(self.submit("/tmp/valid.png").status_code, 413)
                self.assertEqual(self.submit("data:image/png;base64," + base64.b64encode(self.png).decode()).status_code, 413)

    def test_image_errors_never_echo_credentials_or_signed_urls(self):
        secret = "synthetic-url-secret"
        for uri in (f"file:///tmp/missing.png?token={secret}",
                    f"/tmp/missing.png?signature={secret}",
                    f"javascript:{secret}",
                    f"https://user:{secret}@images.example/image.png"):
            with self.subTest(uri=uri), patch("src.api.routes.AsyncSession") as network:
                response = self.submit(uri)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(secret, response.text)
                network.assert_not_called()

    def test_remote_destination_is_pinned_and_implicit_proxies_are_disabled(self):
        with patch("socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ]), patch("src.api.routes.AsyncSession") as network:
            network.return_value.__aenter__.return_value.get.return_value = DownloadResponse(content=self.png)
            self.assertEqual(self.submit("https://other.example/image.png").status_code, 200)
            options = network.call_args.kwargs["curl_options"]
            self.assertEqual(options[CurlOpt.PROXY], "")
            self.assertEqual(options[CurlOpt.CONNECT_TO], ["other.example:443:8.8.8.8:443"])


if __name__ == "__main__":
    unittest.main()
