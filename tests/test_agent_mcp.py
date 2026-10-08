"""Offline tests at the Agent client and MCP public boundaries."""
import json
import base64
from io import BytesIO
import tempfile
import unittest
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from PIL import Image

from src.agent_client import AgentClient, AgentSettings, AgentClientError


class AgentClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = AgentSettings(
            base_url="http://127.0.0.1:8000",
            api_key="offline-test-only",
            output_dir=Path(self.directory.name) / "outputs",
        )

    async def test_lists_service_models_without_inventing_aliases(self):
        async def service(request):
            self.assertEqual(request.url.path, "/v1/agent/models")
            self.assertEqual(request.headers["authorization"], "Bearer offline-test-only")
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "fixture-image", "type": "image", "available": True}
            ]})

        async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
            result = await client.list_models()
        self.assertEqual(result["data"][0]["id"], "fixture-image")
        self.assertEqual(len(result["data"]), 1)

    async def test_reference_image_submission_returns_job_without_polling(self):
        image_path = Path(self.directory.name) / "reference.png"
        Image.new("RGB", (3, 2), "red").save(image_path)
        requests = []

        async def service(request):
            requests.append(request)
            if request.url.path == "/v1/agent/models":
                return httpx.Response(200, json={"data": [{"id": "fixture-image", "type": "image", "available": True, "max_reference_images": 1}]})
            self.assertEqual(request.url.path, "/v1/agent/generations")
            payload = json.loads(request.content)
            self.assertEqual(payload["request_id"], "reference-edit-1")
            self.assertEqual(payload["model"], "fixture-image")
            self.assertEqual(base64.b64decode(payload["images"][0].split(",", 1)[1]), image_path.read_bytes())
            return httpx.Response(202, json={"id": "job-1", "status": "queued", "model": "fixture-image", "media": [], "error": None, "warnings": []})

        async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
            result = await client.submit("image", "fixture-image", "keep product", [str(image_path)], "reference-edit-1")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(len(requests), 2)

    async def test_completed_image_is_saved_and_existing_files_are_not_overwritten(self):
        buffer = BytesIO()
        Image.new("RGB", (3, 2), "blue").save(buffer, format="PNG")
        content = buffer.getvalue()

        async def service(request):
            if request.url.path == "/v1/agent/generations/job-1":
                return httpx.Response(200, json={"id": "job-1", "status": "completed", "model": "fixture-image", "media": [{"type": "image", "url": "http://127.0.0.1:8000/tmp/result.png"}], "warnings": [], "error": None})
            self.assertEqual(request.url.path, "/tmp/result.png")
            return httpx.Response(200, content=content, headers={"Content-Type": "image/png"})

        async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
            first = await client.get_generation("job-1")
            second = await client.get_generation("job-1")
        first_file = Path(first["media"][0]["local_path"])
        second_file = Path(second["media"][0]["local_path"])
        self.assertNotEqual(first_file, second_file)
        self.assertTrue(first_file.is_relative_to(self.settings.output_dir.resolve()))
        self.assertEqual(first_file.read_bytes(), content)
        self.assertEqual(second["media"][0]["width"], 3)

    async def test_external_redirect_is_pinned_to_public_ip_and_receives_no_api_key(self):
        buffer = BytesIO()
        Image.new("RGB", (3, 2)).save(buffer, format="PNG")
        seen = []

        async def service(request):
            seen.append(request)
            if request.url.path.startswith("/v1/"):
                return httpx.Response(200, json={"id": "job-1", "status": "completed", "media": [{"type": "image", "url": "http://127.0.0.1:8000/tmp/a.png"}]})
            if request.url.host == "127.0.0.1":
                return httpx.Response(302, headers={"Location": "https://media.example/a.png"})
            self.assertEqual(request.url.host, "93.184.216.34")
            self.assertEqual(request.headers["host"], "media.example")
            self.assertEqual(request.extensions["sni_hostname"], "media.example")
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, content=buffer.getvalue())

        async def resolver(host):
            self.assertEqual(host, "media.example")
            return ["93.184.216.34"]

        async with AgentClient(self.settings, transport=httpx.MockTransport(service), resolver=resolver) as client:
            result = await client.get_generation("job-1")
        self.assertTrue(Path(result["media"][0]["local_path"]).exists())
        self.assertEqual(len(seen), 3)

    async def test_submit_timeout_reports_unknown_and_never_retries(self):
        posts = []
        async def service(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "fixture-video", "type": "video", "available": True}]})
            posts.append(request)
            raise httpx.ReadTimeout("offline-test-only private detail", request=request)

        async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
            result = await client.submit("video", "fixture-video", "waves", [], "video-request-1")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["request_id"], "video-request-1")
        self.assertFalse(result["error"]["retryable"])
        self.assertNotIn("offline-test-only", json.dumps(result))
        self.assertEqual(len(posts), 1)

    async def test_video_download_checks_mp4_container_before_saving(self):
        def box(kind, payload):
            return (8 + len(payload)).to_bytes(4, "big") + kind + payload
        content = box(b"ftyp", b"isom\x00\x00\x02\x00isommp42") + box(b"moov", b"fixture") + box(b"mdat", b"fixture")
        async def service(request):
            if request.url.path.startswith("/v1/"):
                return httpx.Response(200, json={"id": "video-1", "status": "completed", "media": [{"type": "video", "url": "http://127.0.0.1:8000/tmp/video.mp4"}]})
            return httpx.Response(200, content=content)
        async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
            result = await client.get_generation("video-1")
        path = Path(result["media"][0]["local_path"])
        self.assertEqual(path.suffix, ".mp4")
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(result["media"][0]["validation"], "mp4_container_only")

    async def test_mcp_exposes_four_tools_and_submits_video_as_a_tool(self):
        from agent_mcp import create_server
        async def service(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "fixture-video", "type": "video", "available": True}]})
            return httpx.Response(202, json={"id": "video-1", "status": "queued", "media": [], "error": None})
        server = create_server(self.settings, transport=httpx.MockTransport(service))
        tools = await server.list_tools()
        self.assertEqual({tool.name for tool in tools}, {"list_models", "generate_image", "submit_video", "get_generation"})
        result = await server.call_tool("submit_video", {"model": "fixture-video", "prompt": "ocean", "request_id": "mcp-video-1"})
        self.assertIn("queued", str(result))
        self.assertIn("video-1", str(result))
        self.assertNotIn("offline-test-only", str(result))

    async def test_stdio_protocol_can_initialize_list_and_call_offline_service(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        buffer = BytesIO()
        Image.new("RGB", (4, 3), "green").save(buffer, format="PNG")
        png = buffer.getvalue()
        reference = Path(self.directory.name) / "mcp-reference.png"
        reference.write_bytes(png)
        def box(kind, payload):
            return (8 + len(payload)).to_bytes(4, "big") + kind + payload
        mp4 = box(b"ftyp", b"isom\x00\x00\x02\x00isommp42") + box(b"moov", b"fixture") + box(b"mdat", b"fixture")
        submitted = []

        class Service(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, content, mime="application/json"):
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.end_headers()
                self.wfile.write(content if isinstance(content, bytes) else json.dumps(content).encode())

            def do_GET(self):
                if self.path == "/v1/agent/models":
                    self.respond({"object": "list", "data": [
                        {"id": "offline-image", "type": "image", "available": True, "max_reference_images": 1},
                        {"id": "offline-video", "type": "video", "available": True, "max_reference_images": 0},
                    ]})
                elif self.path in {"/v1/agent/generations/job-image", "/v1/agent/generations/job-video"}:
                    kind = self.path.rsplit("-", 1)[-1]
                    suffix = "png" if kind == "image" else "mp4"
                    self.respond({"id": "job-" + kind, "status": "completed", "model": "offline-" + kind, "media": [{"type": kind, "url": "http://127.0.0.1:" + str(self.server.server_port) + "/tmp/result." + suffix}], "error": None, "warnings": []})
                elif self.path == "/tmp/result.png":
                    self.respond(png, "image/png")
                elif self.path == "/tmp/result.mp4":
                    self.respond(mp4, "video/mp4")
                else:
                    self.send_error(404)

            def do_POST(self):
                if self.path != "/v1/agent/generations":
                    self.send_error(404)
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                submitted.append(body)
                kind = body["model"].removeprefix("offline-")
                self.respond({"id": "job-" + kind, "request_id": body["request_id"], "status": "queued", "model": body["model"], "media": [], "error": None, "warnings": []})

        def payload(result):
            return result.structuredContent or json.loads(next(item.text for item in result.content if item.type == "text"))

        service = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[str(Path(__file__).resolve().parents[1] / "agent_mcp.py")],
                env={"FLOW2API_BASE_URL": "http://127.0.0.1:" + str(service.server_port), "FLOW2API_API_KEY": "offline-test-only", "FLOW2API_API_KEY_FILE": "", "FLOW2API_OUTPUT_DIR": str(self.settings.output_dir)},
            )
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    listed = await session.list_tools()
                    called = await session.call_tool("list_models", {})
                    self.assertEqual(initialized.serverInfo.name, "Flow2API")
                    definitions = {tool.name: tool for tool in listed.tools}
                    self.assertEqual(set(definitions), {"list_models", "generate_image", "submit_video", "get_generation"})
                    self.assertIn("request_id", definitions["generate_image"].inputSchema["required"])
                    self.assertIn("generation_id", definitions["get_generation"].inputSchema["properties"])
                    self.assertIn("request_id", definitions["get_generation"].inputSchema["properties"])
                    self.assertFalse(called.isError)
                    self.assertIn("offline-image", str(called))
                    self.assertNotIn("offline-test-only", str(called))
                    for kind, tool, content in [("image", "generate_image", png), ("video", "submit_video", mp4)]:
                        started = await session.call_tool(tool, {"model": "offline-" + kind, "prompt": "offline media test", "request_id": "stdio-" + kind + "-1", "image_paths": [str(reference)] if kind == "image" else []})
                        started_payload = payload(started)
                        self.assertEqual(started_payload["status"], "queued")
                        finished = payload(await session.call_tool("get_generation", {"generation_id": started_payload["id"]}))
                        self.assertEqual(finished["status"], "completed")
                        self.assertEqual(Path(finished["media"][0]["local_path"]).read_bytes(), content)
                    self.assertEqual(len(submitted), 2)
                    self.assertEqual(base64.b64decode(submitted[0]["images"][0].split(",", 1)[1]), png)
        finally:
            service.shutdown()
            service.server_close()
            thread.join(timeout=2)

    async def test_invalid_references_and_unavailable_models_never_submit(self):
        bad = Path(self.directory.name) / "not-an-image.png"
        bad.write_text("private placeholder; never upload")
        posts = []
        async def service(request):
            if request.method == "POST":
                posts.append(request)
            return httpx.Response(200, json={"data": [
                {"id": "fixture-image", "type": "image", "available": True, "max_reference_images": 1},
                {"id": "disabled-image", "type": "image", "available": False, "max_reference_images": 1},
            ]})
        async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
            for model, paths, code in [
                ("fixture-image", [str(bad)], "invalid_image"),
                ("fixture-image", [str(bad), str(bad)], "too_many_images"),
                ("disabled-image", [], "model_unavailable"),
                ("invented-latest", [], "invalid_model"),
            ]:
                with self.subTest(code=code), self.assertRaises(AgentClientError) as caught:
                    await client.submit("image", model, "test", paths, "request-test-1")
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(posts, [])

    async def test_private_remote_destinations_and_local_path_escape_are_blocked(self):
        requests = []
        for url in ["http://public.example/a.png", "https://private.example/a.png", "http://127.0.0.1:8000/tmp/../../data/file", "http://127.0.0.1:8000/api/tokens"]:
            async def service(request):
                requests.append(request)
                self.assertTrue(request.url.path.startswith("/v1/"))
                return httpx.Response(200, json={"id": "job-1", "status": "completed", "media": [{"type": "image", "url": url}]})
            async def resolver(_):
                return ["127.0.0.1"]
            async with AgentClient(self.settings, transport=httpx.MockTransport(service), resolver=resolver) as client:
                with self.subTest(url=url), self.assertRaises(AgentClientError) as caught:
                    await client.get_generation("job-1")
                self.assertEqual(caught.exception.code, "unsafe_media_url")
        self.assertEqual(len(requests), 4)

    async def test_service_errors_and_echoed_credentials_do_not_leak(self):
        from agent_mcp import create_server
        async def service(request):
            return httpx.Response(500, text="Authorization Bearer offline-test-only")
        server = create_server(self.settings, transport=httpx.MockTransport(service))
        result = await server.call_tool("list_models", {})
        self.assertIn("service_error", str(result))
        self.assertNotIn("offline-test-only", str(result))
        async def echo(request):
            return httpx.Response(200, json={"data": [], "detail": "offline-test-only"})
        async with AgentClient(self.settings, transport=httpx.MockTransport(echo)) as client:
            self.assertNotIn("offline-test-only", str(await client.list_models()))

    async def test_key_file_configuration_is_private_and_requires_output_directory(self):
        key = Path(self.directory.name) / "secret.key"
        key.write_text("offline-test-only\n")
        settings = AgentSettings.from_env({"FLOW2API_API_KEY_FILE": str(key), "FLOW2API_OUTPUT_DIR": str(self.settings.output_dir)})
        self.assertEqual(settings.api_key, "offline-test-only")
        self.assertNotIn("offline-test-only", repr(settings))
        with self.assertRaises(AgentClientError):
            AgentSettings.from_env({"FLOW2API_API_KEY_FILE": str(key)})

    async def test_ambiguous_submit_server_failure_preserves_request_id(self):
        async def service(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "fixture-video", "type": "video", "available": True}]})
            return httpx.Response(502, text="upstream connection interrupted")
        async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
            result = await client.submit("video", "fixture-video", "test", [], "same-request-1")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["request_id"], "same-request-1")

    async def test_required_reference_image_is_validated_before_submission(self):
        async def service(request):
            self.assertEqual(request.method, "GET")
            return httpx.Response(200, json={"data": [{"id": "reference-video", "type": "video", "available": True, "min_reference_images": 1, "max_reference_images": 3}]})
        async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
            with self.assertRaises(AgentClientError) as caught:
                await client.submit("video", "reference-video", "test", [], "reference-test-1")
        self.assertEqual(caught.exception.code, "missing_reference_image")

    async def test_invalid_media_is_not_saved_and_unknown_tasks_do_not_download(self):
        for status, kind in [("completed", "image"), ("completed", "video"), ("unknown", "video")]:
            async def service(request):
                if request.url.path.startswith("/v1/"):
                    return httpx.Response(200, json={"id": "job-1", "status": status, "media": [] if status == "unknown" else [{"type": kind, "url": "http://127.0.0.1:8000/tmp/file"}]})
                return httpx.Response(200, content=b"<html>not a generated file</html>")
            async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
                if status == "unknown":
                    self.assertEqual((await client.get_generation("job-1"))["status"], "unknown")
                else:
                    with self.assertRaises(AgentClientError):
                        await client.get_generation("job-1")
        self.assertFalse(self.settings.output_dir.exists())

    async def test_json_escaped_api_key_is_redacted_in_success_payload(self):
        key = 'offline-"quoted"-key'
        settings = AgentSettings(self.settings.base_url, key, self.settings.output_dir)
        async def service(request):
            return httpx.Response(200, json={"data": [], "detail": key})
        async with AgentClient(settings, transport=httpx.MockTransport(service)) as client:
            result = await client.list_models()
        self.assertEqual(result["detail"], "[redacted]")

    async def test_inline_upscaled_image_is_decoded_saved_and_not_echoed_as_base64(self):
        buffer = BytesIO()
        Image.new("RGB", (4, 3), "green").save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        async def service(request):
            self.assertTrue(request.url.path.startswith("/v1/"))
            return httpx.Response(200, json={"id": "upscaled-1", "status": "completed", "media": [{"type": "image", "url": "data:image/png;base64," + encoded}]})
        async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
            result = await client.get_generation("upscaled-1")
        self.assertEqual(Path(result["media"][0]["local_path"]).read_bytes(), buffer.getvalue())
        self.assertEqual(result["media"][0]["width"], 4)
        self.assertNotIn(encoded, json.dumps(result))

    async def test_inline_images_reject_bad_base64_mime_mismatch_and_video_data(self):
        buffer = BytesIO()
        Image.new("RGB", (4, 3)).save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        for uri in ["data:image/png;base64,%%%bad%%", "data:image/jpeg;base64," + encoded, "data:video/mp4;base64," + encoded]:
            async def service(request):
                return httpx.Response(200, json={"id": "job-1", "status": "completed", "media": [{"type": "image", "url": uri}]})
            async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
                with self.subTest(uri=uri[:30]), self.assertRaises(AgentClientError):
                    await client.get_generation("job-1")
        self.assertFalse(self.settings.output_dir.exists())

    async def test_timeout_task_can_be_recovered_read_only_by_request_id(self):
        methods = []
        async def service(request):
            methods.append(request.method)
            if request.url.path == "/v1/agent/models":
                return httpx.Response(200, json={"data": [{"id": "fixture-video", "type": "video", "available": True}]})
            if request.method == "POST":
                raise httpx.ReadTimeout("response lost after task creation", request=request)
            self.assertEqual(request.url.path, "/v1/agent/generations/by-request/recover-video-1")
            return httpx.Response(200, json={"id": "recovered-job", "request_id": "recover-video-1", "status": "running", "media": [], "error": None})
        async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
            unknown = await client.submit("video", "fixture-video", "test", [], "recover-video-1")
            recovered = await client.get_generation(request_id=unknown["request_id"])
        self.assertEqual(recovered["id"], "recovered-job")
        self.assertEqual(recovered["status"], "running")
        self.assertEqual(methods, ["GET", "POST", "GET"])

    async def test_task_lookup_requires_exactly_one_identifier(self):
        async def service(request):
            self.fail("invalid lookup must not send HTTP")
        async with AgentClient(self.settings, transport=httpx.MockTransport(service)) as client:
            for kwargs in [{}, {"generation_id": "job-1", "request_id": "request-1"}, {"request_id": "../escape"}]:
                with self.subTest(kwargs=kwargs), self.assertRaises(AgentClientError):
                    await client.get_generation(**kwargs)

    async def test_public_media_tries_ipv4_after_ipv6_connection_failure(self):
        buffer = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(buffer, format="PNG")
        attempted = []
        async def resolver(_):
            return ["2606:4700:4700::1111", "93.184.216.34"]
        async def service(request):
            self.assertEqual(request.method, "GET")
            if request.url.path.startswith("/v1/"):
                return httpx.Response(200, json={"id": "job-1", "status": "completed", "media": [{"type": "image", "url": "https://media.example/image.png"}]})
            attempted.append(request.url.host)
            self.assertEqual(request.headers["host"], "media.example")
            self.assertEqual(request.extensions["sni_hostname"], "media.example")
            self.assertNotIn("authorization", request.headers)
            if request.url.host == "2606:4700:4700::1111":
                raise httpx.ConnectError("no IPv6 route", request=request)
            return httpx.Response(200, content=buffer.getvalue())
        async with AgentClient(self.settings, transport=httpx.MockTransport(service), resolver=resolver) as client:
            result = await client.get_generation("job-1")
        self.assertEqual(attempted, ["2606:4700:4700::1111", "93.184.216.34"])
        self.assertEqual(Path(result["media"][0]["local_path"]).read_bytes(), buffer.getvalue())

    async def test_media_read_timeout_does_not_start_over_at_next_ip(self):
        attempted = []
        async def resolver(_):
            return ["2606:4700:4700::1111", "93.184.216.34"]
        async def service(request):
            if request.url.path.startswith("/v1/"):
                return httpx.Response(200, json={"id": "job-1", "status": "completed", "media": [{"type": "video", "url": "https://media.example/video.mp4"}]})
            attempted.append(request.url.host)
            raise httpx.ReadTimeout("media interrupted", request=request)
        async with AgentClient(self.settings, transport=httpx.MockTransport(service), resolver=resolver) as client:
            with self.assertRaises(AgentClientError) as caught:
                await client.get_generation("job-1")
        self.assertEqual(caught.exception.code, "download_failed")
        self.assertEqual(attempted, ["2606:4700:4700::1111"])


if __name__ == "__main__":
    unittest.main()
