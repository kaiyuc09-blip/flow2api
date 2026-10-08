"""Fail closed when persisted settings disagree with a dedicated local launch."""


def validate_native_runtime_config(config, credentials):
    if any(getattr(config, key) != credentials.get(key)
           for key in ("admin_username", "admin_password", "api_key")):
        raise RuntimeError(
            "Native startup stopped: database credentials differ from the private credential files. "
            "Reconcile the local configuration before restarting; nothing was overwritten."
        )
    if (config.captcha_method != "personal" or config.browser_count != 1
            or config.personal_project_pool_size != 1 or config.personal_max_resident_tabs != 1):
        raise RuntimeError(
            "Native startup stopped: the saved configuration must use personal mode, "
            "one browser, one resident tab and one project. Nothing was overwritten."
        )
