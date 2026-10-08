"""Start one local native-browser service using a private directory outside Git."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]


def prepare_private_runtime(directory: Path):
    if directory.expanduser().is_symlink():
        raise ValueError("The private runtime directory must not be a symlink")
    directory = directory.expanduser().resolve()
    if directory == ROOT or directory.is_relative_to(ROOT):
        raise ValueError("Choose a private directory outside the repository")
    if directory.exists() and not (directory / "service-credentials.json").exists() and any(directory.iterdir()):
        raise ValueError("Choose a new empty directory; unrelated existing files were not changed")
    for name in ("flow.db", "flow.db-wal", "flow.db-shm", "flow.db-journal", "service.log"):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--browser", type=Path, default=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    parser.add_argument("--prepare-only", action="store_true", help="Create local credential files; do not start a browser or service")
    args = parser.parse_args()
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
    if not args.browser.is_file():
        parser.error("An installed Chrome executable is required; this launcher does not install browsers")
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
    settings["captcha"].update(captcha_method="personal", browser_count=1, personal_max_resident_tabs=1, personal_project_pool_size=1)
    from src.main import app
    app.state.native_launch_credentials = credentials
    import uvicorn
    print(f"Local management: http://127.0.0.1:{args.port}")
    print("Use the private service-credentials.json locally; never paste its values into chat.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
