"""Render, explicitly install, or uninstall this user's Flow2API LaunchAgent.

Rendering never installs anything. Installation is a separate, user-authorized
operation; it preserves the private runtime and refuses existing plist files.
"""
import argparse
import errno
import os
from pathlib import Path
import plistlib
import subprocess
import sys

LABEL = "com.flow2api.local"
ROOT = Path(__file__).resolve().parents[1]


def build_plist(python: Path, repo: Path, private_dir: Path, port: int = 8000):
    if not 1024 <= port <= 65535:
        raise ValueError("Use a port from 1024 to 65535")
    paths = [Path(p).expanduser() for p in (python, repo, private_dir)]
    if not all(p.is_absolute() for p in paths):
        raise ValueError("All runtime paths must be absolute")
    python, repo, private_dir = paths
    repo, private_dir = repo.resolve(), private_dir.resolve()
    if private_dir == repo or private_dir.is_relative_to(repo):
        raise ValueError("Keep the private runtime outside the repository")
    return {
        "Label": LABEL,
        # Preserve the venv executable path instead of resolving its symlink.
        "ProgramArguments": [str(python), str(repo / "scripts" / "run_native.py"),
                             "--private-dir", str(private_dir), "--port", str(port)],
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "ProcessType": "Background",
        "Umask": 0o077,
        # launchd owns the foreground process; --background would detach it
        # from launchd's supervision and can cause duplicate relaunches.
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def write_plist(payload, destination: Path):
    destination = Path(destination)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    return destination


def _launchctl(*arguments, allow_missing=False):
    result = subprocess.run(["/bin/launchctl", *arguments], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if result.returncode and not (allow_missing and result.returncode == errno.ESRCH):
        raise RuntimeError(f"launchctl {arguments[0]} did not succeed (exit {result.returncode}); inspect the local job before retrying.")


def install_agent(payload, agents_dir=None):
    if sys.platform != "darwin":
        raise RuntimeError("Installing a LaunchAgent requires macOS")
    # Do not terminate or take over an existing manually started service.
    from scripts.run_native import ensure_port_available
    ensure_port_available(int(payload["ProgramArguments"][-1]))
    directory = Path(agents_dir) if agents_dir is not None else Path.home() / "Library" / "LaunchAgents"
    if directory.is_symlink():
        raise ValueError("The LaunchAgents directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    destination = write_plist(payload, directory / (LABEL + ".plist"))
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(destination))
    return destination


def uninstall_agent(payload, agents_dir=None):
    if sys.platform != "darwin":
        raise RuntimeError("Uninstalling a LaunchAgent requires macOS")
    directory = Path(agents_dir) if agents_dir is not None else Path.home() / "Library" / "LaunchAgents"
    destination = directory / (LABEL + ".plist")
    if directory.is_symlink() or destination.is_symlink():
        raise ValueError("Refusing a symlinked LaunchAgent")
    with destination.open("rb") as handle:
        installed = plistlib.load(handle)
    if installed != payload:
        raise ValueError("The existing LaunchAgent differs from this configuration; it was not changed")
    _launchctl("bootout", f"gui/{os.getuid()}", str(destination), allow_missing=True)
    # Only remove the exact job we read and successfully unloaded. Never touch
    # credentials, browser data, media, or an unrelated LaunchAgent.
    if destination.is_symlink() or plistlib.loads(destination.read_bytes()) != payload:
        raise ValueError("The LaunchAgent changed during unload; its file was preserved")
    destination.unlink()
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("render", "install", "uninstall"))
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--output", type=Path, help="New plist path; required only for render")
    args = parser.parse_args()
    if (args.action == "render") != (args.output is not None):
        parser.error("Use --output only with render; install/uninstall target the user's LaunchAgents directory")
    try:
        payload = build_plist(args.python, args.repo, args.private_dir, args.port)
        if args.action == "render":
            destination = write_plist(payload, args.output)
            print(f"Plist generated only; not installed: {destination}")
        elif args.action == "install":
            print(f"LaunchAgent installed: {install_agent(payload)}")
        else:
            print(f"LaunchAgent unloaded and its plist removed: {uninstall_agent(payload)}")
    except (OSError, ValueError, RuntimeError, plistlib.InvalidFileException):
        parser.error("Operation did not complete. Check paths, an existing plist or service, and launchctl status locally. Private runtime data was preserved.")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
