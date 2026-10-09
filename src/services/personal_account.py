"""Explicit local account handoff from an already-running dedicated browser.

This module does not launch browsers, navigate, install software, or expose
credentials. The authenticated local admin action is the only caller.
"""
import asyncio
import os
from pathlib import Path
import re
from urllib.parse import urlparse

from ..core.config import config
from .browser_cookie_utils import (
    FLOW_SESSION_COOKIE_NAMES, GOOGLE_ACCOUNT_COOKIE_NAMES,
    normalize_cookie_storage_text, validate_flow_cookie_storage,
)

_connect_lock = asyncio.Lock()
_PROJECT_ID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_COOKIE_NAMES = FLOW_SESSION_COOKIE_NAMES | GOOGLE_ACCOUNT_COOKIE_NAMES | {
    "SIDCC", "__Secure-1PSIDTS", "__Secure-3PSIDTS", "__Secure-1PSIDCC",
    "__Secure-3PSIDCC", "__Secure-1PAPISID", "__Secure-3PAPISID",
}


class PersonalAccountError(ValueError):
    """Safe, fixed user-facing message; never wrap a browser exception string."""


def dedicated_profile_path() -> Path:
    raw = os.environ.get("PERSONAL_BROWSER_USER_DATA_DIR", "").strip()
    if not raw or not Path(raw).expanduser().is_absolute():
        raise PersonalAccountError("请先配置独立浏览器资料目录")
    candidate = Path(raw).expanduser().resolve()
    roots = [
        Path.home() / "Library/Application Support/Google/Chrome",
        Path.home() / "Library/Application Support/Google/Chrome Beta",
        Path.home() / "Library/Application Support/Google/Chrome Canary",
        Path.home() / "Library/Application Support/Chromium",
        Path.home() / ".config/google-chrome",
        Path.home() / ".config/chromium",
    ]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data")
    if candidate == Path.home().resolve() or any(
        candidate == root.resolve() or root.resolve() in candidate.parents
        or candidate in root.resolve().parents for root in roots
    ):
        raise PersonalAccountError("请使用专用目录，不能连接日常 Chrome 资料目录")
    return candidate


def _project_from_tabs(browser, provided):
    ids = set()
    for tab in getattr(browser, "tabs", []):
        try:
            url = urlparse(str(getattr(tab, "url", "")))
            if url.scheme != "https" or url.hostname != "flow.google.com" or url.username or url.password:
                continue
            match = re.match(r"^/project/([^/]+)(?:/|$)", url.path)
            if match and _PROJECT_ID.fullmatch(match.group(1)):
                ids.add(match.group(1).lower())
        except ValueError:
            continue
    if provided:
        if not isinstance(provided, str) or not _PROJECT_ID.fullmatch(provided):
            raise PersonalAccountError("项目 ID 格式无效，请填写已有 Flow 项目的 ID")
        return provided.lower()
    if len(ids) != 1:
        raise PersonalAccountError("请在专用浏览器打开一个已有 Flow 项目，或填写项目 ID")
    return ids.pop()


def _google_page_connection(browser, isolated_context_ids):
    # Network.getCookies is a page CDP command, not a browser-root command.
    for tab in getattr(browser, "tabs", []):
        try:
            url = urlparse(str(getattr(tab, "url", "")))
            context = getattr(getattr(tab, "target", None), "browser_context_id", None)
            if (url.scheme == "https" and url.hostname in {"flow.google.com", "accounts.google.com", "www.google.com"}
                    and not url.username and not url.password and context not in isolated_context_ids
                    and callable(getattr(tab, "send", None))):
                return tab
        except ValueError:
            continue
    raise PersonalAccountError("请在专用浏览器的普通窗口打开 Google Flow")


async def connect_personal_account(manager, service, project_id=None):
    async with _connect_lock:
        profile = dedicated_profile_path()
        if (config.captcha_method != "personal" or config.browser_count != 1
                or config.personal_project_pool_size != 1 or config.personal_max_resident_tabs != 1):
            raise PersonalAccountError("原生账号连接需要 personal 模式、一个浏览器、一个标签页和一个项目")
        if service is None:
            raise PersonalAccountError("请先启动专用原生浏览器并登录 Google Flow")
        workers = getattr(service, "_workers", None)
        if workers is not None and len(workers) != 1:
            raise PersonalAccountError("只支持一个专用浏览器")
        worker = workers[0] if workers else service
        browser = getattr(worker, "browser", None)
        if (not browser or getattr(browser, "stopped", True)
                or not getattr(worker, "_initialized", False) or worker.headless
                or Path(worker.user_data_dir).resolve() != profile
                or Path(browser.config.user_data_dir).resolve() != profile):
            raise PersonalAccountError("已运行的浏览器不是配置的独立登录窗口")
        existing = await manager.get_all_tokens()
        if existing:
            raise PersonalAccountError("当前仅支持连接一个账号；已有账号时请先停用服务并处理账号配置")
        chosen_project = _project_from_tabs(browser, project_id)
        try:
            from nodriver import cdp
            # CDP excludes the persistent default context from this list.
            isolated_context_ids = set(await asyncio.wait_for(
                browser.send(cdp.target.get_browser_contexts()), timeout=15
            ))
            page = _google_page_connection(browser, isolated_context_ids)
            cookies = await asyncio.wait_for(page.send(cdp.network.get_cookies(
                urls=["https://flow.google.com/", "https://www.google.com/"]
            )), timeout=15)
            selected = []
            for cookie in cookies:
                value = cookie if isinstance(cookie, dict) else cookie.to_json()
                domain = str(value.get("domain", "")).lstrip(".").lower()
                if domain not in {"google.com", "flow.google.com", "www.google.com"}:
                    continue
                if value.get("name") not in _COOKIE_NAMES or not value.get("value"):
                    continue
                selected.append({key: value[key] for key in (
                    "name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires"
                ) if key in value})
            storage = normalize_cookie_storage_text(selected)
            validate_flow_cookie_storage(storage)
            connected = await manager.add_token(
                st="", google_cookies=storage, project_id=chosen_project,
                project_name="Native browser project", remark="原生浏览器账号",
                account_source="personal_browser", protocol_mode="protocol",
                image_concurrency=1, video_concurrency=1, auto_refresh_enabled=False,
            )
        except Exception:
            raise PersonalAccountError("连接未完成；请确认专用窗口已登录并能打开 Flow 项目，然后重试") from None
        return connected
