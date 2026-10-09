"""Start one local Flow2API service using a private directory outside Git."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


class LauncherError(RuntimeError):
    """Safe startup diagnostics, with no credentials or raw transport errors."""


def prepare_private_runtime(directory: Path):
    if directory.expanduser().is_symlink():
        raise ValueError("The private runtime directory must not be a symlink")
    directory = directory.expanduser().resolve()
    if directory == ROOT or directory.is_relative_to(ROOT):
        raise ValueError("Choose a private directory outside the repository")
    if directory.exists() and not (directory / "service-credentials.json").exists() and any(directory.iterdir()):
        raise ValueError("Choose a new empty directory; unrelated existing files were not changed")
    for name in ("flow.db", "flow.db-wal", "flow.db-shm", "flow.db-journal", "service.log", "service.lock"):
        file = directory / name
        if file.is_symlink() or (file.exists() and not file.is_file()):
            raise ValueError("Private runtime files must be regular files, not symlinks")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    credentials = directory / "service-credentials.json"
    if credentials.is_symlink():
        raise ValueError("Credential files must not be symlinks")
    if not credentials.exists():
        values = {"admin_username": "local", "admin_password": secrets.token_urlsafe(32), "api_key": secrets.token_urlsafe(36)}
        descriptor = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(values, handle)
    with credentials.open() as handle:
        values = json.load(handle)
    if not all(isinstance(values.get(key), str) and values[key] for key in ("admin_username", "admin_password", "api_key")):
        raise ValueError("The existing service credential file is invalid; it was not changed")
    credentials.chmod(0o600)
    key_file = directory / "api-key.txt"
    if key_file.is_symlink():
        raise ValueError("The API key file must not be a symlink")
    if key_file.exists():
        if key_file.read_text().strip() != values["api_key"]:
            raise ValueError("Existing API key files disagree; no credential was overwritten")
    else:
        descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(values["api_key"] + "\n")
    key_file.chmod(0o600)
    for name in ("browser-profile", "media", "cache"):
        child = directory / name
        if child.is_symlink():
            raise ValueError("Private runtime directories must not be symlinks")
        child.mkdir(exist_ok=True, mode=0o700)
        child.chmod(0o700)
    return directory, values


def acquire_runtime_lock(directory: Path):
    """Hold one POSIX service owner per database/profile until the handle closes."""
    import fcntl

    descriptor = None
    try:
        descriptor = os.open(directory / "service.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError()
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "r+")
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise LauncherError("This private runtime is already in use or its service lock is unsafe. No additional service was started.") from None


def ensure_port_available(port: int):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
    except OSError:
        raise LauncherError("The local port is already in use. No additional browser was started.") from None


def service_ready(port: int, api_key: str) -> bool:
    """Verify our authenticated model endpoint without invoking generation."""
    import httpx

    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=2) as client:
            response = client.get(f"http://127.0.0.1:{port}/v1/agent/models",
                                  headers={"Authorization": "Bearer " + api_key})
        if response.status_code != 200:
            return False
        payload = response.json()
        return isinstance(payload, dict) and isinstance(payload.get("data"), list) and bool(payload["data"])
    except (httpx.HTTPError, ValueError):
        return False


def start_background(directory: Path, credentials: dict, port: int, browser: Path, timeout=90):
    """Detach once, then confirm readiness. Never kill or retry a slow startup."""
    if os.name != "posix":
        raise LauncherError("Background mode currently requires macOS or Linux; use foreground mode on this platform.")
    if service_ready(port, credentials["api_key"]):
        return None
    ensure_port_available(port)
    try:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--private-dir", str(directory),
             "--port", str(port), "--browser", str(browser.resolve())],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
    except OSError:
        raise LauncherError("Could not create the background service process. No automatic retry was made.") from None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise LauncherError(f"The background service exited before readiness (exit {child.returncode}). Check the private runtime locally; no automatic retry was made.")
        if service_ready(port, credentials["api_key"]) and child.poll() is None:
            return child.pid
        time.sleep(0.25)
    raise LauncherError(f"Readiness is not confirmed for background process {child.pid}. It may still be starting; no additional process was started.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--browser", type=Path, default=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                        help="Installed Chrome executable; required only for saved personal mode")
    parser.add_argument("--prepare-only", action="store_true", help="Create local credential files; do not start a browser or service")
    parser.add_argument("--background", action="store_true", help="Start independently of this terminal on macOS/Linux; wait for the local API to be ready")
    args = parser.parse_args()
    if args.prepare_only and args.background:
        parser.error("Choose either --prepare-only or --background")
    if not 1024 <= args.port <= 65535:
        parser.error("Use a port from 1024 to 65535")
    os.umask(0o077)
    try:
        directory, credentials = prepare_private_runtime(args.private_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        parser.error("Could not prepare the private runtime. Existing credentials were not overwritten.")
    print(f"Private runtime prepared: {directory}")
    if args.prepare_only:
        return
    try:
        if args.background:
            pid = start_background(directory, credentials, args.port, args.browser)
            print("Existing local service is ready." if pid is None else f"Background service ready (PID {pid}).")
            print(f"Local management: http://127.0.0.1:{args.port}")
            return
        # Lock before app import/startup, which can open the dedicated browser.
        with acquire_runtime_lock(directory) if os.name == "posix" else nullcontext():
            ensure_port_available(args.port)
            run_service(directory, credentials, args)
    except RuntimeError as error:
        # Only launcher errors have safe text; application errors are not printed here.
        parser.error(str(error) if type(error) is LauncherError else "Could not start the local service. Check the private runtime locally.")


def run_service(directory, credentials, args):
    os.environ.update({
        "FLOW2API_DB_PATH": str(directory / "flow.db"),
        "FLOW2API_CACHE_DIR": str(directory / "cache"),
        "FLOW2API_LOG_PATH": str(directory / "service.log"),
        "PERSONAL_BROWSER_USER_DATA_DIR": str(directory / "browser-profile"),
        "PERSONAL_BROWSER_HEADLESS": "false",
        # Native UI uses normal Chrome defaults instead of the legacy captcha flags.
        "PERSONAL_BROWSER_BARE_MODE": os.environ.get("PERSONAL_BROWSER_BARE_MODE", "1"),
        "PERSONAL_BROWSER_FRESH_RESTART_EVERY_N_SOLVES": "0",
        "BROWSER_EXECUTABLE_PATH": str(args.browser),
    })
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    from src.core.config import config
    settings = config.get_raw_config()
    settings["global"].update(credentials)
    settings["server"].update(host="127.0.0.1", port=args.port)
    from src.main import app
    app.state.native_launch_credentials = credentials
    app.state.native_launch_workers = 1
    app.state.native_launch_browser_path = args.browser
    import uvicorn
    print(f"Local management: http://127.0.0.1:{args.port}")
    print("Use the private service-credentials.json locally; never paste its values into chat.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
