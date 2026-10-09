import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import launchagent


class LaunchAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "repo with spaces"
        self.private = self.base / "private"
        self.python = self.base / "venv/bin/python"
        self.payload = launchagent.build_plist(self.python, self.repo, self.private, 18001)

    def test_plist_preserves_venv_path_and_launchd_owns_the_single_process(self):
        self.assertEqual(self.payload["ProgramArguments"], [str(self.python),
            str(self.repo / "scripts/run_native.py"), "--private-dir", str(self.private), "--port", "18001"])
        self.assertNotIn("--background", self.payload["ProgramArguments"])
        self.assertTrue(self.payload["RunAtLoad"])
        self.assertEqual(self.payload["KeepAlive"], {"SuccessfulExit": False})
        self.assertGreaterEqual(self.payload["ThrottleInterval"], 30)
        self.assertEqual(self.payload["Umask"], 0o077)
        self.assertEqual(self.payload["StandardOutPath"], "/dev/null")
        self.assertEqual(self.payload["StandardErrorPath"], "/dev/null")
        self.assertNotIn("EnvironmentVariables", self.payload)

    def test_generation_only_writes_a_private_plist_in_the_requested_directory(self):
        path = self.base / "generated.plist"
        with patch.object(launchagent, "_launchctl") as launch:
            launchagent.write_plist(self.payload, path)
        self.assertEqual(plistlib.loads(path.read_bytes()), self.payload)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        launch.assert_not_called()
        self.assertFalse(self.private.exists())

    def test_refuses_existing_file_and_symlink_without_touching_data(self):
        path = self.base / "existing"
        path.write_text("keep")
        link = self.base / "link"
        link.symlink_to(path)
        for target in (path, link):
            with self.subTest(target=target), self.assertRaises(FileExistsError):
                launchagent.write_plist(self.payload, target)
        self.assertEqual(path.read_text(), "keep")

    def test_rejects_relative_paths_private_inside_repo_and_invalid_port(self):
        for args in ((Path("python"), self.repo, self.private, 8000),
                     (self.python, self.repo, self.repo / "private", 8000),
                     (self.python, self.repo, self.private, 80)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                launchagent.build_plist(*args)

    def test_install_and_uninstall_use_only_explicit_mocked_user_job(self):
        agents = self.base / "LaunchAgents"
        with patch.object(sys, "platform", "darwin"), \
             patch("scripts.run_native.ensure_port_available") as check, \
             patch.object(launchagent, "_launchctl") as launch:
            path = launchagent.install_agent(self.payload, agents)
            check.assert_called_once_with(18001)
            launch.assert_called_once_with("bootstrap", f"gui/{os.getuid()}", str(path))
            self.private.mkdir()
            retained = self.private / "existing-data"
            retained.write_text("keep")
            launchagent.uninstall_agent(self.payload, agents)
            self.assertFalse(path.exists())
            self.assertEqual(retained.read_text(), "keep")
            self.assertEqual(launch.call_args.args[:2], ("bootout", f"gui/{os.getuid()}"))

    def test_uninstall_refuses_an_unrelated_configuration(self):
        agents = self.base / "LaunchAgents"
        agents.mkdir()
        path = agents / (launchagent.LABEL + ".plist")
        changed = {**self.payload, "Label": "unrelated.job"}
        launchagent.write_plist(changed, path)
        with patch.object(sys, "platform", "darwin"), \
             patch.object(launchagent, "_launchctl") as launch, self.assertRaises(ValueError):
            launchagent.uninstall_agent(self.payload, agents)
        self.assertEqual(plistlib.loads(path.read_bytes()), changed)
        launch.assert_not_called()

    def test_existing_listener_blocks_install_before_any_files_are_created(self):
        agents = self.base / "LaunchAgents"
        with patch.object(sys, "platform", "darwin"), \
             patch("scripts.run_native.ensure_port_available", side_effect=RuntimeError("occupied")), \
             patch.object(launchagent, "_launchctl") as launch, self.assertRaises(RuntimeError):
            launchagent.install_agent(self.payload, agents)
        self.assertFalse(agents.exists())
        launch.assert_not_called()

    def test_render_cli_does_not_install_or_create_private_runtime(self):
        path = self.base / "rendered.plist"
        result = subprocess.run([sys.executable, str(launchagent.ROOT / "scripts/launchagent.py"),
            "render", "--private-dir", str(self.private), "--output", str(path)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not installed", result.stdout)
        self.assertEqual(plistlib.loads(path.read_bytes())["Label"], launchagent.LABEL)
        self.assertFalse(self.private.exists())


if __name__ == "__main__":
    unittest.main()
