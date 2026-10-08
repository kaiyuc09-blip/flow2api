"""Cache writes, HTTP reads and logs share the configured private runtime."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PrivateRuntimePathTests(unittest.TestCase):
    def test_configured_cache_round_trip_and_redacted_private_log(self):
        with tempfile.TemporaryDirectory() as folder:
            private = Path(folder) / "private"
            private.mkdir()
            cache = private / "cache"
            log = private / "service.log"
            script = textwrap.dedent('''
                import asyncio, base64, io, os
                from pathlib import Path
                import httpx
                from PIL import Image
                from src.main import app, generation_handler
                from src.api.routes import retrieve_image_data
                from src.core.config import config
                from src.core.logger import debug_logger
                from src.services.file_cache import FileCache

                async def check():
                    output = io.BytesIO()
                    Image.new("RGB", (2, 2), "white").save(output, format="PNG")
                    expected = output.getvalue()
                    cache = Path(os.environ["FLOW2API_CACHE_DIR"])
                    assert generation_handler.file_cache.cache_dir == cache
                    assert FileCache().cache_dir == cache
                    filename = await generation_handler.file_cache.cache_base64_image(base64.b64encode(expected).decode())
                    assert (cache / filename).read_bytes() == expected
                    assert await retrieve_image_data("/tmp/" + filename) == expected
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                        response = await client.get("/tmp/" + filename)
                        assert response.status_code == 200
                        assert response.content == expected
                        assert (await client.get("/tmp/../service.log")).status_code == 404
                    config.set_debug_enabled(True)
                    debug_logger.log_info("private-path-test Authorization: Bearer synthetic-private-value")
                    for handler in debug_logger.logger.handlers:
                        handler.flush()
                    logged = Path(os.environ["FLOW2API_LOG_PATH"]).read_text()
                    assert "private-path-test" in logged
                    assert "synthetic-private-value" not in logged
                asyncio.run(check())
            ''')
            env = {**os.environ, "FLOW2API_CACHE_DIR": str(cache), "FLOW2API_LOG_PATH": str(log), "FLOW2API_DB_PATH": str(private / "test.db")}
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_default_locations_remain_compatible_without_overrides(self):
        script = textwrap.dedent('''
            from pathlib import Path
            from src.services.file_cache import FileCache
            from src.services.generation_handler import GenerationHandler
            from src.core.logger import debug_logger
            assert FileCache().cache_dir == Path("tmp")
            handler = GenerationHandler(None, None, None, None, None, None)
            assert handler.file_cache.cache_dir == Path.cwd() / "tmp"
            assert debug_logger.log_file == Path("logs.txt")
        ''')
        env = {key: value for key, value in os.environ.items() if key not in {"FLOW2API_CACHE_DIR", "FLOW2API_LOG_PATH"}}
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
