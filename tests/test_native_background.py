"""Background launcher contracts; never start Chrome or the Flow service."""
from contextlib import ExitStack
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from scripts import run_native


@unittest.skipUnless(os.name == "posix", "Runtime locking uses POSIX flock")
class NativeRuntimeLockTests(unittest.TestCase):
    def test_symlink_lock_does_not_touch_the_target(self):
        with tempfile.TemporaryDirectory() as base:
            directory = Path(base) / "private"
            directory.mkdir()
            target = Path(base) / "existing-file"
            target.write_text("preserve this")
            mode = target.stat().st_mode
            (directory / "service.lock").symlink_to(target)
            with self.assertRaises(RuntimeError):
                run_native.acquire_runtime_lock(directory)
            self.assertEqual(target.read_text(), "preserve this")
            self.assertEqual(target.stat().st_mode, mode)

    def test_lock_excludes_another_process_and_close_releases_it(self):
        child_code = """
import sys
from pathlib import Path
from scripts.run_native import acquire_runtime_lock
try:
    handle = acquire_runtime_lock(Path(sys.argv[1]))
except RuntimeError:
    sys.exit(42)
handle.close()
"""
        with tempfile.TemporaryDirectory() as base:
            directory = Path(base)
            handle = run_native.acquire_runtime_lock(directory)
            try:
                blocked = subprocess.run(
                    [sys.executable, "-c", child_code, str(directory)],
                    cwd=run_native.ROOT, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(blocked.returncode, 42, blocked.stderr)
            finally:
                handle.close()
            released = subprocess.run(
                [sys.executable, "-c", child_code, str(directory)],
                cwd=run_native.ROOT, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(released.returncode, 0, released.stderr)

    def test_different_runtime_directories_have_independent_locks(self):
        with tempfile.TemporaryDirectory() as base:
            first = Path(base) / "first"
            second = Path(base) / "second"
            first.mkdir()
            second.mkdir()
            first_lock = run_native.acquire_runtime_lock(first)
            try:
                second_lock = run_native.acquire_runtime_lock(second)
                second_lock.close()
            finally:
                first_lock.close()


class NativeRuntimePortTests(unittest.TestCase):
    def bind_loopback(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        try:
            listener.bind(("127.0.0.1", 0))
        except PermissionError:
            self.skipTest("This sandbox does not permit binding a loopback socket")
        return listener

    def test_occupied_port_is_rejected_without_disrupting_its_owner(self):
        listener = self.bind_loopback()
        listener.listen(1)
        port = listener.getsockname()[1]
        with self.assertRaises(RuntimeError):
            run_native.ensure_port_available(port)
        self.assertEqual(listener.getsockname()[1], port)
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            listener.settimeout(1)
            accepted, _ = listener.accept()
            accepted.close()

    def test_a_released_port_is_available(self):
        listener = self.bind_loopback()
        port = listener.getsockname()[1]
        listener.close()
        run_native.ensure_port_available(port)


@unittest.skipUnless(os.name == "posix", "Detached background mode requires POSIX")
class NativeBackgroundTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.browser = self.directory / "Chrome executable"
        self.credentials = {
            "admin_username": "synthetic-admin",
            "admin_password": "synthetic-password-never-in-argv",
            "api_key": "synthetic-api-key-never-in-argv",
        }
        self.port = 18081
        self.child = Mock()
        self.child.pid = 24680
        self.child.poll.return_value = None
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        # These are the public operating-system/readiness boundaries.  No real
        # service, browser, HTTP request, or private configuration is involved.
        self.spawn = self.patches.enter_context(patch("subprocess.Popen", return_value=self.child))
        self.ready = self.patches.enter_context(
            patch.object(run_native, "service_ready", create=True, return_value=False)
        )
        self.port_check = self.patches.enter_context(
            patch.object(run_native, "ensure_port_available", create=True)
        )
        self.patches.enter_context(patch("time.sleep", return_value=None))

    def start(self, timeout=0.1):
        return run_native.start_background(
            self.directory, self.credentials, self.port, self.browser, timeout=timeout,
        )

    def assert_child_was_not_stopped(self):
        self.child.terminate.assert_not_called()
        self.child.kill.assert_not_called()

    def test_ready_service_is_reused_without_spawning_or_requiring_a_free_port(self):
        self.ready.return_value = True
        self.assertIsNone(self.start())
        self.spawn.assert_not_called()
        self.port_check.assert_not_called()
        self.ready.assert_called_with(self.port, self.credentials["api_key"])

    def test_spawn_detaches_terminal_and_waits_for_authenticated_readiness(self):
        self.ready.side_effect = [False, False, True]
        self.assertEqual(self.start(), self.child.pid)
        self.spawn.assert_called_once()
        args, kwargs = self.spawn.call_args
        command = list(args[0])
        self.assertTrue(all(isinstance(value, str) for value in command))
        self.assertIn(str(run_native.ROOT / "scripts" / "run_native.py"), command)
        for flag, expected in (
            ("--private-dir", str(self.directory)),
            ("--port", str(self.port)),
            ("--browser", str(self.browser.resolve())),
        ):
            self.assertIn(flag, command)
            self.assertEqual(command[command.index(flag) + 1], expected)
        self.assertFalse(any(value.startswith("--background") for value in command))
        serialized_command = " ".join(command)
        for value in self.credentials.values():
            self.assertNotIn(value, serialized_command)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertIs(kwargs["close_fds"], True)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(Path(kwargs["cwd"]), run_native.ROOT)
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(self.ready.call_count, 3)
        for call in self.ready.call_args_list:
            self.assertEqual(call.args, (self.port, self.credentials["api_key"]))
        self.port_check.assert_called_once_with(self.port)
        self.assert_child_was_not_stopped()

    def test_an_unrelated_service_on_the_port_prevents_spawning(self):
        self.port_check.side_effect = RuntimeError("Port already in use")
        with self.assertRaises(RuntimeError):
            self.start()
        self.spawn.assert_not_called()

    def test_exited_child_is_a_failure_and_is_not_respawned(self):
        self.child.poll.return_value = 17
        with self.assertRaises(RuntimeError):
            self.start()
        self.spawn.assert_called_once()
        self.assert_child_was_not_stopped()

    def test_readiness_timeout_is_a_failure_without_respawn_or_kill(self):
        with self.assertRaises(RuntimeError):
            self.start(timeout=0.01)
        self.spawn.assert_called_once()
        self.assertGreaterEqual(self.ready.call_count, 2)
        self.assert_child_was_not_stopped()

    def test_process_creation_failure_is_reported_without_retry(self):
        self.spawn.side_effect = OSError("Synthetic executable could not start")
        with self.assertRaises(RuntimeError) as caught:
            self.start()
        self.spawn.assert_called_once()
        self.assertNotIn("Synthetic executable", str(caught.exception))


class NativeReadinessTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("httpx.Client")
        self.client_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.client_factory.return_value.__enter__.return_value
        self.response = self.client.get.return_value
        self.response.status_code = 200
        self.response.json.return_value = {"data": [{"id": "synthetic-model"}]}

    def test_models_are_checked_locally_without_environment_proxies_or_redirects(self):
        self.assertIs(run_native.service_ready(18081, "synthetic-key"), True)
        kwargs = self.client_factory.call_args.kwargs
        self.assertIs(kwargs["trust_env"], False)
        self.assertIs(kwargs["follow_redirects"], False)
        self.assertGreater(kwargs["timeout"], 0)
        self.client.get.assert_called_once_with(
            "http://127.0.0.1:18081/v1/agent/models",
            headers={"Authorization": "Bearer synthetic-key"},
        )

    def test_status_200_without_a_nonempty_model_list_is_not_ready(self):
        for payload in ({}, {"data": []}, {"data": None}, {"data": "models"}, []):
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                self.assertIs(run_native.service_ready(18081, "synthetic-key"), False)

    def test_redirect_and_authentication_errors_are_not_ready(self):
        for status in (302, 401, 403, 500):
            with self.subTest(status=status):
                self.response.status_code = status
                self.assertIs(run_native.service_ready(18081, "synthetic-key"), False)

    def test_transport_failure_is_not_ready(self):
        self.client.get.side_effect = httpx.ConnectError("Synthetic local service is unavailable")
        self.assertIs(run_native.service_ready(18081, "synthetic-key"), False)

    def test_non_json_success_response_is_not_ready(self):
        self.response.json.side_effect = ValueError("Synthetic invalid JSON")
        self.assertIs(run_native.service_ready(18081, "synthetic-key"), False)


if __name__ == "__main__":
    unittest.main()
