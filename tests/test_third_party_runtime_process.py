"""Real local HTTP process; empty temporary DB and fake captcha key only."""
import asyncio
from copy import deepcopy
import os
from pathlib import Path
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import httpx

from scripts.run_native import ROOT, prepare_private_runtime, service_ready
from src.core.config import config
from src.core.database import Database


@unittest.skipUnless(os.name == "posix", "Local process/browser trap uses POSIX")
class ThirdPartyRuntimeProcessTests(unittest.TestCase):
    def test_real_service_uses_saved_third_party_mode_without_starting_browser(self):
        with tempfile.TemporaryDirectory(prefix="flow2api-offline-") as base:
            private, credentials = prepare_private_runtime(Path(base) / "private")
            settings = deepcopy(config.get_raw_config())
            settings["global"].update(credentials)
            settings["captcha"].update(captcha_method="yescaptcha", yescaptcha_api_key="synthetic-offline-only")
            settings["flow"]["max_retries"] = 7
            settings["debug"] = {"enabled": False}
            db = Database(db_path=str(private / "flow.db"))

            async def seed():
                await db.init_db()
                await db.init_config_from_toml(settings, is_first_startup=True)

            asyncio.run(seed())
            marker = Path(base) / "browser-was-started"
            browser = Path(base) / "browser-trap"
            browser.write_text("#!/bin/sh\n: > " + shlex.quote(str(marker)) + "\nexit 1\n")
            browser.chmod(0o700)
            with socket.socket() as reservation:
                try:
                    reservation.bind(("127.0.0.1", 0))
                except PermissionError:
                    self.skipTest("Sandbox disallows a temporary loopback listener")
                port = reservation.getsockname()[1]
            child = subprocess.Popen([sys.executable, str(ROOT / "scripts/run_native.py"),
                "--private-dir", str(private), "--port", str(port), "--browser", str(browser)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                deadline = time.monotonic() + 30
                ready = False
                while time.monotonic() < deadline:
                    self.assertIsNone(child.poll(), "Isolated service exited before readiness")
                    if service_ready(port, credentials["api_key"]):
                        ready = True
                        break
                    time.sleep(0.1)
                self.assertTrue(ready, "Isolated local API did not become ready")
                with httpx.Client(trust_env=False, follow_redirects=False, timeout=3) as client:
                    response = client.get(f"http://127.0.0.1:{port}/v1/agent/models",
                                          headers={"Authorization": "Bearer " + credentials["api_key"]})
                self.assertEqual(response.status_code, 200)
                models = {entry["id"]: entry for entry in response.json()["data"]}
                self.assertTrue(models["gemini-3.1-flash-image-square"]["available"])
                self.assertEqual(models["gemini-3.1-flash-image-square"]["max_reference_images"], 3)
                self.assertFalse(any(name.startswith(("gemini-nano-banana-2.1", "native-omni-1.1-flash-")) for name in models))
                self.assertFalse(marker.exists(), "Third-party startup attempted to launch a browser")
                with sqlite3.connect((private / "flow.db").as_uri() + "?mode=ro", uri=True) as connection:
                    self.assertEqual(connection.execute("SELECT max_retries FROM generation_config WHERE id=1").fetchone()[0], 7)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM tokens").fetchone()[0], 0)
            finally:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
