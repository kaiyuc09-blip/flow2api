"""Fail closed when persisted settings disagree with a dedicated local launch."""
from pathlib import Path


def validate_native_runtime_config(config, credentials, *, workers=1):
    if any(getattr(config, key) != credentials.get(key)
           for key in ("admin_username", "admin_password", "api_key")):
        raise RuntimeError(
            "Local startup stopped: database credentials differ from the private credential files. "
            "Reconcile the local configuration before restarting; nothing was overwritten."
        )
    if config.server_host != "127.0.0.1" or type(workers) is not int or workers != 1:
        raise RuntimeError(
            "Local startup stopped: the private service must bind to 127.0.0.1 "
            "with exactly one worker. Nothing was overwritten."
        )


def validate_personal_browser(config, browser_path):
    """Check Chrome only after the saved captcha mode has been loaded."""
    if config.captcha_method == "personal" and (
        browser_path is None or not Path(browser_path).is_file()
    ):
        raise RuntimeError(
            "Personal mode requires an installed Chrome executable. "
            "Set --browser to its path; this launcher does not install browsers."
        )
