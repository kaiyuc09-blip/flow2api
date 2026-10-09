"""Current flow.google.com RPC implementation.

The public Flow web application exposes protobuf messages through Google
batchexecute.  This mixin overrides every runtime path that previously called
the retired Labs tRPC and AI Sandbox REST endpoints.
"""

import asyncio
import base64
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..core.config import config
from ..core.logger import debug_logger
from .browser_cookie_utils import validate_flow_cookie_storage
from .model_capabilities import (
    allows_native_default_image, get_parameter_preserving_captcha_method,
    validate_native_image_options, NATIVE_IMAGE_ASPECTS, validate_native_video_options, NATIVE_VIDEO_ASPECTS,
)
from .generation_policy import submission_attempts, raise_if_submission_uncertain, GenerationOutcomeUnknown, get_native_credit_limit


@dataclass
class CaptchaCallObservation:
    """Observed create-task dispatches, not provider billing or result polls."""

    persist: Optional[Callable[[int], Awaitable[None]]] = None
    count: Optional[int] = None
    pending: set = field(default_factory=set)

    async def _persist(self, count):
        try:
            await self.persist(count)
        except Exception:
            debug_logger.log_warning("Captcha call count persistence unavailable")

    async def flush(self):
        if self.pending:
            await asyncio.gather(*tuple(self.pending))


_captcha_call_observation: ContextVar[Optional[CaptchaCallObservation]] = ContextVar(
    "flow_captcha_call_observation", default=None
)


def start_captcha_call_observation(persist=None):
    observation = CaptchaCallObservation(persist=persist)
    return observation, _captcha_call_observation.set(observation)


def reset_captcha_call_observation(token):
    _captcha_call_observation.reset(token)


async def record_captcha_create_call():
    """Call only at a provider create-task dispatch; pass no credential data."""
    observation = _captcha_call_observation.get()
    if observation is None:
        return
    observation.count = (observation.count or 0) + 1
    if observation.persist is not None:
        # Do not delay or cancel the provider dispatch on database I/O. The job
        # drains these writes before its final result; crash recovery uses null.
        task = asyncio.create_task(observation._persist(observation.count))
        observation.pending.add(task)
        task.add_done_callback(observation.pending.discard)


class CurrentFlowClientMixin:
    FRONTEND_ACCESS_TOKEN = "flow-frontend-cookie"

    def _get_runtime_config(self):
        raise NotImplementedError

    async def _frontend_cookie(
        self,
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> str:
        return await self._resolve_flow_frontend_cookie_header(
            google_cookies=google_cookies,
            token_id=token_id,
        )

    @staticmethod
    def _normalize_media_name(media_name: str) -> str:
        normalized = str(media_name or "").strip().rstrip("/")
        if "/" in normalized:
            normalized = normalized.rsplit("/", 1)[-1]
        return normalized

    @classmethod
    def _find_uuid_value(
        cls,
        value: Any,
        excluded: Optional[set[str]] = None,
    ) -> str:
        excluded = excluded or set()
        if isinstance(value, str):
            for candidate in re.findall(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                value,
                re.IGNORECASE,
            ):
                if candidate.lower() not in {item.lower() for item in excluded}:
                    return candidate
            return ""
        if isinstance(value, list):
            for item in value:
                candidate = cls._find_uuid_value(item, excluded)
                if candidate:
                    return candidate
        elif isinstance(value, dict):
            for item in value.values():
                candidate = cls._find_uuid_value(item, excluded)
                if candidate:
                    return candidate
        return ""

    @classmethod
    def _find_media_url(cls, payload: Any) -> str:
        return cls._find_flow_content_url(
            payload, "video"
        ) or cls._find_flow_content_url(payload, "image")

    @classmethod
    def _find_encoded_media(cls, payload: Any) -> str:
        if isinstance(payload, str):
            if payload.startswith("https://"):
                return ""
            if len(payload) >= 512 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", payload):
                return payload
            return ""
        if isinstance(payload, list):
            for item in payload:
                encoded = cls._find_encoded_media(item)
                if encoded:
                    return encoded
        elif isinstance(payload, dict):
            for item in payload.values():
                encoded = cls._find_encoded_media(item)
                if encoded:
                    return encoded
        return ""

    async def _current_rpc(
        self,
        *,
        rpc_id: str,
        argument: List[Any],
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
        project_id: Optional[str] = None,
        source_path: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Any:
        cookie_storage = await self._resolve_flow_frontend_cookie_storage(
            google_cookies=google_cookies,
            token_id=token_id,
        )
        return await self._call_flow_frontend_rpc(
            rpc_id=rpc_id,
            argument=argument,
            cookie_header="",
            timeout=timeout or self._get_control_plane_timeout(),
            project_id=project_id,
            source_path=source_path,
            cookie_storage=cookie_storage,
        )

    async def st_to_at(
        self,
        st: str = "",
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> dict:
        if str(google_cookies or "").strip():
            validate_flow_cookie_storage(google_cookies)
        credits = await self.get_credits(
            self.FRONTEND_ACCESS_TOKEN,
            google_cookies=google_cookies,
            token_id=token_id,
        )
        expires = datetime.now(timezone.utc) + timedelta(minutes=55)
        return {
            "access_token": self.FRONTEND_ACCESS_TOKEN,
            "expires": expires.isoformat().replace("+00:00", "Z"),
            "user": {},
            "credits": credits.get("credits", 0),
        }

    async def create_project(
        self,
        st: str,
        title: str,
        google_cookies: Optional[str] = None,
    ) -> str:
        payload = await self._current_rpc(
            rpc_id="jHPbke",
            argument=["projects/*", [None, [title]], [None, 22]],
            google_cookies=google_cookies,
            timeout=max(self._get_control_plane_timeout(), 20),
        )
        project_id = self._extract_flow_project_id(payload)
        if not project_id:
            raise RuntimeError("Flow frontend createProject response missing projectId")
        return project_id

    async def delete_project(
        self,
        st: str,
        project_id: str,
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> None:
        await self._current_rpc(
            rpc_id="QI2zvc",
            argument=[project_id],
            google_cookies=google_cookies,
            token_id=token_id,
            project_id=project_id,
        )

    async def get_credits(
        self,
        at: str = "",
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> dict:
        payload = await self._current_rpc(
            rpc_id="nzlxg",
            argument=[],
            google_cookies=google_cookies,
            token_id=token_id,
        )
        values = payload if isinstance(payload, list) else []
        credits = next(
            (
                int(value)
                for value in values
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ),
            0,
        )
        return {
            "credits": credits,
            "userPaygateTier": (
                "PAYGATE_TIER_ONE" if credits > 0 else "PAYGATE_TIER_NOT_PAID"
            ),
            "frontendRpc": "nzlxg",
        }

    async def get_media(
        self,
        at: str,
        media_name: str,
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
        project_id: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> dict:
        normalized_name = self._normalize_media_name(media_name)
        normalized_operation = self._normalize_media_name(operation_id or "")
        payload = await self._current_rpc(
            rpc_id="as29s",
            argument=[normalized_operation or normalized_name],
            google_cookies=google_cookies,
            token_id=token_id,
            project_id=project_id,
            source_path=(
                f"/project/{project_id}/edit/{normalized_name}" if project_id else None
            ),
        )
        media_url = self._find_media_url(payload)
        project_id = (
            str(payload[1] or "")
            if isinstance(payload, list) and len(payload) > 1
            else ""
        )
        return {
            "name": normalized_name,
            "projectId": project_id,
            "video": {
                "fifeUrl": media_url,
                "generatedVideo": {"fifeUrl": media_url},
            },
            "image": {"generatedImage": {"fifeUrl": media_url}},
            "frontendRpc": "as29s",
        }

    async def get_media_url_redirect(
        self,
        st: str,
        media_name: str,
        media_url_type: str = "MEDIA_URL_TYPE_FULL_MEDIA",
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> Optional[str]:
        media = await self.get_media(
            self.FRONTEND_ACCESS_TOKEN,
            media_name,
            google_cookies=google_cookies,
            token_id=token_id,
            project_id=project_id,
        )
        return str((media.get("video") or {}).get("fifeUrl") or "").strip() or None

    async def delete_media(
        self,
        st: str,
        media_names: List[str],
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> None:
        names = [self._normalize_media_name(name) for name in media_names if name]
        if not names:
            return
        await self._current_rpc(
            rpc_id="cz8Z4b",
            argument=[names, None, project_id],
            google_cookies=google_cookies,
            token_id=token_id,
            project_id=project_id,
        )

    async def upload_image(
        self,
        at: str,
        image_bytes: bytes,
        aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        google_cookies: Optional[str] = None,
    ) -> str:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            raise ValueError("Flow frontend image upload requires project_id")
        mime_type = self._detect_image_mime_type(image_bytes)
        extension = "png" if "png" in mime_type else "jpg"
        filename = f"flow2api_{uuid.uuid4().hex[:12]}.{extension}"
        recaptcha_token, browser_id = await self._get_recaptcha_token(
            normalized_project_id,
            action="UPLOAD_IMAGE",
            token_id=token_id,
            website_url=self._build_flow_frontend_project_page_url(
                normalized_project_id
            ),
            method_override=self._resolve_batchexecute_captcha_override(),
        )
        if not recaptcha_token:
            raise RuntimeError("Failed to obtain reCAPTCHA token for image upload")
        try:
            context = self._frontend_project_context(
                normalized_project_id,
                recaptcha_token,
            )
            payload = await self._current_rpc(
                rpc_id="maseQ",
                argument=[
                    context,
                    base64.b64encode(image_bytes).decode("ascii"),
                    mime_type,
                    1,
                    None,
                    None,
                    None,
                    None,
                    filename,
                    None,
                    str(uuid.uuid4()).upper(),
                    str(uuid.uuid4()).upper(),
                ],
                google_cookies=google_cookies,
                token_id=token_id,
                project_id=normalized_project_id,
                timeout=max(self._get_control_plane_timeout(), 90),
            )
            media_url = self._find_flow_content_url(payload, "image")
            media_id = ""
            if media_url:
                match = re.search(r"/image/([0-9a-f-]{36})", media_url, re.I)
                media_id = match.group(1) if match else ""
            media_id = media_id or self._find_uuid_value(
                payload,
                excluded={normalized_project_id},
            )
            if not media_id:
                raise RuntimeError("Flow frontend upload response missing mediaId")
            return media_id
        finally:
            await self._notify_browser_captcha_request_finished(browser_id)

    async def generate_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_name: str,
        aspect_ratio: str,
        image_inputs: Optional[List[Dict]] = None,
        token_id: Optional[int] = None,
        token_image_concurrency: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int], Awaitable[None]]] = None,
        google_cookies: Optional[str] = None,
        preserve_parameters: bool = False,
        native_options: Optional[Dict[str, Any]] = None,
    ) -> tuple[dict, str, Dict[str, Any]]:
        native_explicit = native_options is not None
        if native_explicit:
            if not isinstance(native_options, dict):
                raise ValueError("Invalid native image options")
            native_options = dict(native_options)
            native_options.setdefault("max_credits", get_native_credit_limit())
            validate_native_image_options(native_options, len(image_inputs or []))
            if model_name or NATIVE_IMAGE_ASPECTS.get(aspect_ratio) != native_options["aspect_ratio"]:
                raise ValueError("Native image selection must match its UI model and aspect ratio, without a guessed RPC key")
        native_default = not native_explicit and not preserve_parameters and allows_native_default_image(
            {"type": "image", "model_name": model_name, "aspect_ratio": aspect_ratio},
            len(image_inputs or []),
        )
        captcha_override = None if native_default or native_explicit else get_parameter_preserving_captcha_method()
        await self._frontend_cookie(google_cookies, token_id)
        max_retries = 1 if native_explicit else submission_attempts(self._get_runtime_config().flow_max_retries)
        trace: Dict[str, Any] = {"max_retries": max_retries, "generation_attempts": []}
        last_error: Optional[Exception] = None
        cookie_storage = await self._resolve_flow_frontend_cookie_storage(
            google_cookies=google_cookies,
            token_id=token_id,
        )
        if not str(cookie_storage or "").strip():
            raise RuntimeError("Flow frontend generation requires Google session cookies")
        for retry_attempt in range(max_retries):
            started_at = time.time()
            browser_id = None
            submitted = False
            attempt = {"attempt": retry_attempt + 1, "recaptcha_ok": False}
            try:
                if progress_callback:
                    await progress_callback("solving_image_captcha", 38)
                personal_mode = config.captcha_method == "personal"
                personal_native_compatible = (
                    personal_mode
                    and (native_explicit or (
                        native_default and not image_inputs
                        and str(model_name or "").upper() == "NARWHAL"
                        and str(aspect_ratio or "").upper() == "IMAGE_ASPECT_RATIO_LANDSCAPE"
                    ))
                )
                if personal_native_compatible:
                    from .browser_captcha_personal import BrowserCaptchaService

                    personal_service = getattr(
                        self, "_personal_browser_service", None
                    )
                    if personal_service is None:
                        personal_service = await BrowserCaptchaService.get_instance(
                            getattr(self, "db", None)
                        )
                    attempt["recaptcha_ok"] = True
                    if progress_callback:
                        await progress_callback("submitting_image", 48)
                    submitted = True
                    native_result = await personal_service.generate_native_image(
                        project_id=project_id,
                        prompt=prompt,
                        token_id=token_id,
                        timeout=max(
                            self._get_runtime_config().flow_image_request_timeout, 120
                        ),
                        **({"native_options": native_options} if native_explicit else {}),
                    )
                    if not native_result:
                        raise RuntimeError("Personal native image generation failed")

                    session_id = str(native_result.get("session_id") or "").strip()
                    browser_id = f"personal:{session_id}" if session_id else None
                    response_text = str(native_result.get("responseText") or "")
                    frames = self._parse_batchexecute_frames(response_text)
                    native_settings = native_result.get("native_settings")
                    if native_explicit:
                        if not isinstance(native_settings, dict) or native_settings.get("verified_before_submit") is not True or any(
                            native_settings.get(key) != native_options[key]
                            for key in ("model_label", "aspect_ratio", "image_count", "max_credits")
                        ):
                            raise RuntimeError("Native UI model, ratio and count were not confirmed before submission")
                        if type(native_settings.get("credits_shown")) is not int or not 0 <= native_settings["credits_shown"] <= native_options["max_credits"]:
                            raise RuntimeError("Native UI credit estimate was not confirmed within the approved budget")

                    fingerprint = native_result.get("fingerprint")
                    if isinstance(fingerprint, dict) and fingerprint:
                        self._set_request_fingerprint(fingerprint)

                    if progress_callback:
                        await progress_callback("processing_image", 72)
                    frontend_rpc = native_result.get("frontendRpc") or (None if native_explicit else "StreamChat")
                    if frontend_rpc == "ogiZ0b":
                        payload = self._extract_batchexecute_payload(frames, "ogiZ0b")
                        media = self._normalize_frontend_image_generation_response(payload)["media"]
                    elif frontend_rpc == "StreamChat":
                        media_ids = self._extract_stream_chat_media_ids(frames)
                        if native_explicit and len(media_ids) != 1:
                            raise RuntimeError("Native image response did not contain exactly one generated image")
                        media = []
                        for media_id in media_ids[:2]:
                            media_entry = await self.get_media(
                                at, media_id, google_cookies=google_cookies,
                                token_id=token_id, project_id=project_id,
                            )
                            if str((media_entry.get("image") or {}).get("generatedImage", {}).get("fifeUrl") or ""):
                                media.append(media_entry)
                    else:
                        raise RuntimeError("Native image generation used an unrecognized response protocol")
                    if not media:
                        raise RuntimeError(
                            "Personal native StreamChat media could not be resolved"
                        )

                    if native_explicit and len(media) != 1:
                        raise RuntimeError("Native image response did not contain exactly one generated image")
                    result = {"media": media, "frontendRpc": frontend_rpc}
                    if native_explicit:
                        result.update(native_settings=dict(native_settings), generation_transport="native_ui")
                    attempt["success"] = True
                    attempt["duration_ms"] = int((time.time() - started_at) * 1000)
                    trace["generation_attempts"].append(attempt)
                    trace["final_success_attempt"] = retry_attempt + 1
                    return result, session_id, trace

                # 通道与 action 必须按打码方式配套：
                # - personal 默认纯文生横图已在上面的原子浏览器流程处理；
                #   其他 personal 参数组合使用第三方 token + ogiZ0b，避免丢失参数。
                # - browser 仅在默认横图且无参考图时沿用网页默认模型；其身份未验证。
                # - 原生模型的精确 UI 选择已在上方处理；其他显式参数走完整 RPC。
                # - 第三方打码 (yescaptcha 等)：token 只被 batchexecute 通道接受，
                #   走原 ogiZ0b 链路，action 保持上游既有的 IMAGE_GENERATION。
                stream_transport = config.captcha_method == "browser" and native_default
                token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action=(
                        "CHAT_GENERATION" if stream_transport else "IMAGE_GENERATION"
                    ),
                    token_id=token_id,
                    website_url=self._build_flow_frontend_project_page_url(project_id),
                    method_override=captcha_override,
                )
                attempt["recaptcha_ok"] = bool(token)
                if not token:
                    raise RuntimeError("Failed to obtain reCAPTCHA token")
                if progress_callback:
                    await progress_callback("submitting_image", 48)
                harvest_session_id = (
                    self._extract_browser_captcha_session_id(browser_id)
                    if stream_transport
                    else None
                )
                if stream_transport and not harvest_session_id:
                    raise RuntimeError(
                        "Browser harvest returned no Flow session id"
                    )
                if harvest_session_id:
                    request_timeout = max(
                        self._get_runtime_config().flow_image_request_timeout, 90
                    )
                    submitted = True
                    stream_result = await self._call_flow_stream_chat(
                        project_id=project_id,
                        prompt=prompt,
                        recaptcha_token=token,
                        session_id=harvest_session_id,
                        cookie_header="",
                        timeout=request_timeout,
                        cookie_storage=cookie_storage,
                    )
                    media_ids = list(stream_result.get("mediaIds") or [])
                    if not media_ids:
                        raise RuntimeError(
                            "Flow StreamChat returned no generated media "
                            f"(rawLength={stream_result.get('rawLength')})"
                        )
                    if progress_callback:
                        await progress_callback("processing_image", 72)
                    # 媒体 id 需通过 as29s 换取带签名的 flow-content 下载地址
                    media = []
                    for media_id in media_ids[:2]:
                        media_entry = await self.get_media(
                            at,
                            media_id,
                            google_cookies=google_cookies,
                            token_id=token_id,
                            project_id=project_id,
                        )
                        if str(
                            (media_entry.get("image") or {})
                            .get("generatedImage", {})
                            .get("fifeUrl")
                            or ""
                        ):
                            media.append(media_entry)
                    if not media:
                        raise RuntimeError(
                            "Flow StreamChat media could not be resolved to a download URL"
                        )
                    result = {"media": media, "frontendRpc": "StreamChat"}
                    attempt["success"] = True
                    attempt["duration_ms"] = int((time.time() - started_at) * 1000)
                    trace["generation_attempts"].append(attempt)
                    trace["final_success_attempt"] = retry_attempt + 1
                    return result, harvest_session_id, trace
                session_id = str(uuid.uuid4()).upper()
                submitted = True
                payload = await self._current_rpc(
                    rpc_id="ogiZ0b",
                    argument=self._build_frontend_image_generation_argument(
                        project_id=project_id,
                        prompt=prompt,
                        model_name=model_name,
                        aspect_ratio=aspect_ratio,
                        recaptcha_token=token,
                        session_id=session_id,
                        image_inputs=image_inputs,
                    ),
                    google_cookies=google_cookies,
                    token_id=token_id,
                    project_id=project_id,
                    timeout=max(
                        self._get_runtime_config().flow_image_request_timeout, 90
                    ),
                )
                result = self._normalize_frontend_image_generation_response(payload)
                attempt["success"] = True
                attempt["duration_ms"] = int((time.time() - started_at) * 1000)
                trace["generation_attempts"].append(attempt)
                trace["final_success_attempt"] = retry_attempt + 1
                return result, session_id, trace
            except Exception as error:
                last_error = error
                attempt["success"] = False
                attempt["error"] = str(error)[:240]
                attempt["duration_ms"] = int((time.time() - started_at) * 1000)
                trace["generation_attempts"].append(attempt)
                if native_explicit and getattr(error, "submission_started", None) is False:
                    submitted = False
                if native_explicit and submitted and not getattr(error, "outcome_unknown", False):
                    raise GenerationOutcomeUnknown(
                        "Native UI submission outcome is unknown; no automatic resubmission was attempted"
                    ) from error
                raise_if_submission_uncertain(error, submitted=submitted)
                retry_handler = getattr(
                    self, "_handle_retryable_generation_error", None
                )
                if retry_handler is not None:
                    await retry_handler(
                        error,
                        retry_attempt,
                        max_retries,
                        browser_id,
                        project_id,
                        "Personal image generation",
                    )
                if retry_attempt >= max_retries - 1:
                    raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        raise last_error or RuntimeError("Flow frontend image generation failed")

    def _resolve_batchexecute_captcha_override(self) -> Optional[str]:
        """batchexecute 类 RPC（SPrCad 放大、YhhmEf 视频等）只接受第三方打码 token。

        browser/personal harvest 的 token 会被 UNUSUAL_ACTIVITY 拒绝（实测矩阵）。
        browser 类模式下必须配置第三方打码密钥，否则直接返回明确配置错误。
        """
        if config.captcha_method not in ("browser", "personal", "remote_browser", "extension"):
            return None
        method = self._resolve_third_party_captcha_method()
        if not method:
            raise RuntimeError(
                "当前浏览器打码模式不支持该 Flow RPC；请先配置 YesCaptcha、"
                "Captcha.run、CapMonster、EzCaptcha 或 CapSolver API Key"
            )
        debug_logger.log_info(
            f"[batchexecute] 使用第三方打码方式: {method} (configured={config.captcha_method})"
        )
        return method

    async def upsample_image(
        self,
        at: str,
        project_id: str,
        media_id: str,
        target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_4K",
        user_paygate_tier: str = "PAYGATE_TIER_NOT_PAID",
        session_id: Optional[str] = None,
        token_id: Optional[int] = None,
        google_cookies: Optional[str] = None,
    ) -> str:
        # SPrCad(batchexecute) 只接受第三方打码 token；browser/personal harvest
        # 的 token 会被 UNUSUAL_ACTIVITY 拒绝（实测矩阵）。
        token, browser_id = await self._get_recaptcha_token(
            project_id,
            action="IMAGE_GENERATION",
            token_id=token_id,
            website_url=self._build_flow_frontend_project_page_url(project_id),
            method_override=self._resolve_batchexecute_captcha_override(),
        )
        if not token:
            raise RuntimeError("Failed to obtain reCAPTCHA token for image upsample")
        try:
            # The page sends enum values (verified by capturing the real UI request):
            # 1 = 2K, 2 = 4K.
            resolution = 2 if "4K" in str(target_resolution).upper() else 1
            payload = await self._current_rpc(
                rpc_id="SPrCad",
                argument=[
                    self._normalize_media_name(media_id),
                    resolution,
                    self._frontend_project_context(project_id, token),
                ],
                google_cookies=google_cookies,
                token_id=token_id,
                project_id=project_id,
                timeout=max(
                    int(self._get_runtime_config().upsample_timeout or 300), 90
                ),
            )
            return self._find_media_url(payload) or self._find_encoded_media(payload)
        except Exception as error:
            raise_if_submission_uncertain(error, submitted=True)
            raise
        finally:
            await self._notify_browser_captcha_request_finished(browser_id)

    async def _current_generate_video(
        self,
        *,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        mode: str,
        token_id: Optional[int],
        token_video_concurrency: Optional[int],
        google_cookies: Optional[str],
        reference_media_ids: Optional[List[str]] = None,
        start_media_id: Optional[str] = None,
        end_media_id: Optional[str] = None,
        video_media_id: Optional[str] = None,
        resolution: Optional[str] = None,
    ) -> dict:
        await self._frontend_cookie(google_cookies, token_id)
        captcha_override = self._resolve_batchexecute_captcha_override()
        max_retries = submission_attempts(self._get_runtime_config().flow_max_retries)
        last_error: Optional[Exception] = None
        for retry_attempt in range(max_retries):
            browser_id = None
            submitted = False
            try:
                token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                    website_url=self._build_flow_frontend_project_page_url(project_id),
                    method_override=captcha_override,
                )
                if not token:
                    raise RuntimeError("Failed to obtain reCAPTCHA token")
                rpc_id, argument = self._build_frontend_video_generation_argument(
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                    recaptcha_token=token,
                    # 视频 batchexecute 通道固定使用第三方打码，每次请求生成
                    # 独立 session id，不复用任何 browser harvest 会话。
                    session_id=str(uuid.uuid4()).upper(),
                    mode=mode,
                    reference_media_ids=reference_media_ids,
                    start_media_id=start_media_id,
                    end_media_id=end_media_id,
                    video_media_id=video_media_id,
                    resolution=resolution,
                )
                submitted = True
                payload = await self._current_rpc(
                    rpc_id=rpc_id,
                    argument=argument,
                    google_cookies=google_cookies,
                    token_id=token_id,
                    project_id=project_id,
                    timeout=max(self._get_video_submit_timeout(), 90),
                )
                return self._normalize_frontend_video_submission(
                    payload,
                    project_id=project_id,
                    aspect_ratio=aspect_ratio,
                    model_key=model_key,
                    rpc_id=rpc_id,
                )
            except Exception as error:
                last_error = error
                raise_if_submission_uncertain(error, submitted=submitted)
                # MODEL_ACCESS_DENIED 为账号权限拒绝，重试不会改变结果
                if "MODEL_ACCESS_DENIED" in str(error):
                    raise
                if retry_attempt >= max_retries - 1:
                    raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        raise last_error or RuntimeError("Flow frontend video generation failed")

    async def _current_generate_native_video(
        self, *, project_id: str, prompt: str, model_key: Optional[str], aspect_ratio: str,
        token_id: Optional[int], google_cookies: Optional[str], native_options: Dict[str, Any],
    ) -> dict:
        if not isinstance(native_options, dict):
            raise ValueError("Invalid native video options")
        native_options = dict(native_options)
        native_options.setdefault("max_credits", get_native_credit_limit())
        validate_native_video_options(native_options)
        if model_key or NATIVE_VIDEO_ASPECTS.get(aspect_ratio) != native_options["aspect_ratio"]:
            raise ValueError("Native video selection must match its UI model and aspect ratio, without a guessed RPC key")
        await self._frontend_cookie(google_cookies, token_id)
        from .browser_captcha_personal import BrowserCaptchaService

        personal_service = getattr(self, "_personal_browser_service", None)
        if personal_service is None:
            personal_service = await BrowserCaptchaService.get_instance(getattr(self, "db", None))
        browser_id = None
        try:
            native_result = await personal_service.generate_native_video(
                project_id=project_id, prompt=prompt, token_id=token_id,
                timeout=max(self._get_video_submit_timeout(), 120), native_options=native_options,
            )
            if not isinstance(native_result, dict):
                raise RuntimeError("Personal native video generation returned no submission result")
            native_settings = native_result.get("native_settings")
            if not isinstance(native_settings, dict) or native_settings.get("verified_before_submit") is not True or any(
                native_settings.get(key) != native_options[key]
                for key in ("model_label", "aspect_ratio", "resolution", "duration_seconds", "video_count", "max_credits")
            ):
                raise RuntimeError("Native video UI settings were not confirmed before submission")
            if type(native_settings.get("credits_shown")) is not int or not 0 <= native_settings["credits_shown"] <= native_options["max_credits"]:
                raise RuntimeError("Native video UI credit estimate was not confirmed within the approved budget")
            session_id = str(native_result.get("session_id") or "").strip()
            browser_id = f"personal:{session_id}" if session_id else None
            fingerprint = native_result.get("fingerprint")
            if isinstance(fingerprint, dict) and fingerprint:
                self._set_request_fingerprint(fingerprint)
            frontend_rpc = native_result.get("frontendRpc")
            if frontend_rpc not in ("YhhmEf", "MZZa6b"):
                raise RuntimeError("Native text video result used an unrecognized submission protocol")
            frames = self._parse_batchexecute_frames(str(native_result.get("responseText") or ""))
            payload = self._extract_batchexecute_payload(frames, frontend_rpc)
            result = self._normalize_frontend_video_submission(
                payload, project_id=project_id, aspect_ratio=aspect_ratio, model_key=None, rpc_id=frontend_rpc,
            )
            if not result.get("direct_media") and len(result.get("operations") or []) != 1:
                raise RuntimeError("Native video result did not contain exactly one generation operation")
            result.update(native_settings=native_settings, generation_transport="native_ui")
            return result
        except Exception as error:
            if getattr(error, "submission_started", None) is False or getattr(error, "outcome_unknown", False):
                raise
            raise GenerationOutcomeUnknown("原生视频生成已尝试提交，但结果无法确认；请核对 Flow 原任务，不要重新提交") from error
        finally:
            await self._notify_browser_captcha_request_finished(browser_id)

    async def generate_video_text(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        google_cookies: Optional[str] = None,
        native_options: Optional[Dict[str, Any]] = None,
    ) -> dict:
        if native_options is not None:
            return await self._current_generate_native_video(
                project_id=project_id, prompt=prompt, model_key=model_key, aspect_ratio=aspect_ratio,
                token_id=token_id, google_cookies=google_cookies, native_options=native_options,
            )
        return await self._current_generate_video(
            project_id=project_id,
            prompt=prompt,
            model_key=model_key,
            aspect_ratio=aspect_ratio,
            mode="text",
            token_id=token_id,
            token_video_concurrency=token_video_concurrency,
            google_cookies=google_cookies,
        )

    async def generate_video_reference_images(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        reference_images: List[Dict],
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        google_cookies: Optional[str] = None,
    ) -> dict:
        media_ids = [
            str(item.get("mediaId") or item.get("name") or "").strip()
            for item in reference_images or []
            if isinstance(item, dict)
        ]
        return await self._current_generate_video(
            project_id=project_id,
            prompt=prompt,
            model_key=model_key,
            aspect_ratio=aspect_ratio,
            mode="references",
            reference_media_ids=media_ids,
            token_id=token_id,
            token_video_concurrency=token_video_concurrency,
            google_cookies=google_cookies,
        )

    async def generate_video_start_end(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        end_media_id: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        google_cookies: Optional[str] = None,
    ) -> dict:
        return await self._current_generate_video(
            project_id=project_id,
            prompt=prompt,
            model_key=model_key,
            aspect_ratio=aspect_ratio,
            mode="start_end",
            start_media_id=start_media_id,
            end_media_id=end_media_id,
            token_id=token_id,
            token_video_concurrency=token_video_concurrency,
            google_cookies=google_cookies,
        )

    async def generate_video_start_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        google_cookies: Optional[str] = None,
    ) -> dict:
        return await self._current_generate_video(
            project_id=project_id,
            prompt=prompt,
            model_key=model_key,
            aspect_ratio=aspect_ratio,
            mode="start",
            start_media_id=start_media_id,
            token_id=token_id,
            token_video_concurrency=token_video_concurrency,
            google_cookies=google_cookies,
        )

    async def generate_video_extend(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        video_media_id: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        google_cookies: Optional[str] = None,
    ) -> dict:
        return await self._current_generate_video(
            project_id=project_id,
            prompt=prompt,
            model_key=model_key,
            aspect_ratio=aspect_ratio,
            mode="extend",
            video_media_id=video_media_id,
            token_id=token_id,
            token_video_concurrency=token_video_concurrency,
            google_cookies=google_cookies,
        )

    async def generate_omni_reference_video(
        self,
        at: str,
        st: str,
        project_id: str,
        prompt: str,
        aspect_ratio: str,
        reference_media_ids: List[str],
        model_usage_key: str = "abra_r2v_10s",
        model_display_name: str = "Omni Flash",
        duration: int = 10,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        google_cookies: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._current_generate_video(
            project_id=project_id,
            prompt=prompt,
            model_key=model_usage_key,
            aspect_ratio=aspect_ratio,
            mode="references",
            reference_media_ids=reference_media_ids,
            token_id=token_id,
            token_video_concurrency=token_video_concurrency,
            google_cookies=google_cookies,
        )

    async def upsample_video(
        self,
        at: str,
        project_id: str,
        video_media_id: str,
        aspect_ratio: str,
        resolution: str,
        model_key: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        google_cookies: Optional[str] = None,
    ) -> dict:
        return await self._current_generate_video(
            project_id=project_id,
            prompt="",
            model_key=model_key,
            aspect_ratio=aspect_ratio,
            mode="upsample",
            video_media_id=video_media_id,
            resolution=resolution,
            token_id=token_id,
            token_video_concurrency=token_video_concurrency,
            google_cookies=google_cookies,
        )

    async def check_video_status(
        self,
        at: str,
        operations: List[Dict],
        token_id: Optional[int] = None,
        google_cookies: Optional[str] = None,
    ) -> dict:
        operation_ids: List[str] = []
        for operation in operations or []:
            operation_id = str(
                (operation or {}).get("name")
                or ((operation or {}).get("operation") or {}).get("name")
                or ""
            ).strip()
            if operation_id and operation_id not in operation_ids:
                operation_ids.append(operation_id)
        if not operation_ids:
            raise ValueError("视频状态查询缺少 Flow frontend operation ID")
        first_operation = operations[0] if operations else {}
        project_id = str((first_operation or {}).get("projectId") or "").strip()
        payload = await self._current_rpc(
            rpc_id="jwpduf",
            argument=[None, None, [[operation_id] for operation_id in operation_ids]],
            google_cookies=google_cookies,
            token_id=token_id,
            project_id=project_id or None,
            timeout=max(self._get_video_poll_timeout(), 30),
        )
        normalized = self._normalize_frontend_video_status(payload, operations)
        checked_operations = normalized.get("operations") or []
        for operation in checked_operations:
            if "SUCCESSFUL" in str(operation.get("status") or ""):
                continue
            operation_body = operation.get("operation") or {}
            metadata = operation_body.get("metadata") or {}
            video_info = (
                metadata.get("video") if isinstance(metadata.get("video"), dict) else {}
            )
            media_name = str(
                operation.get("mediaName")
                or video_info.get("mediaName")
                or operation.get("name")
                or operation_body.get("name")
                or ""
            ).strip()
            if not media_name:
                continue
            try:
                operation_id = str(
                    operation.get("name") or operation_body.get("name") or ""
                ).strip()
                media = await self.get_media(
                    at,
                    media_name,
                    google_cookies=google_cookies,
                    token_id=token_id,
                    project_id=project_id or None,
                    operation_id=operation_id or None,
                )
            except Exception as media_error:
                debug_logger.log_warning(
                    f"[VIDEO POLL] media fallback failed for {media_name}: {media_error}"
                )
                continue
            video_url = str((media.get("video") or {}).get("fifeUrl") or "").strip()
            if "/video/" not in video_url:
                continue
            video_info = dict(video_info or {})
            video_info["fifeUrl"] = video_url
            video_info.setdefault("mediaName", media_name)
            metadata = dict(metadata)
            metadata["video"] = video_info
            operation_body = dict(operation_body)
            operation_body["metadata"] = metadata
            operation["operation"] = operation_body
            operation["status"] = "MEDIA_GENERATION_STATUS_SUCCESSFUL"
            operation["progress"] = 100
        return normalized

    async def run_concatenation(
        self,
        at: str,
        original_media_id: str,
        extend_media_id: str,
        **_: Any,
    ) -> dict:
        raise RuntimeError(
            "Current Flow frontend does not expose a media concatenation RPC"
        )

    async def poll_concatenation_status(
        self,
        at: str,
        operation_name: str,
        timeout: int = 300,
        poll_interval: int = 3,
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> dict:
        raise RuntimeError(
            "Current Flow frontend does not expose a media concatenation RPC"
        )

    async def get_flow_creation_agent_session(
        self,
        at: str,
        project_id: str,
        *,
        account_id: Optional[str] = None,
        allow_global_fallback: bool = True,
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> Optional[str]:
        payload = await self._current_rpc(
            rpc_id="mrlkwd",
            argument=[project_id],
            google_cookies=google_cookies,
            token_id=token_id,
            project_id=project_id,
        )
        return self._find_uuid_value(payload, excluded={project_id}) or None

    async def get_flow_creation_agent_session_detail(
        self,
        at: str,
        agent_session_id: str,
        *,
        account_id: Optional[str] = None,
        google_cookies: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = await self._current_rpc(
            rpc_id="GN0Bre",
            argument=[agent_session_id],
            google_cookies=google_cookies,
            token_id=token_id,
        )
        return {"session": payload, "frontendRpc": "GN0Bre"}

    async def create_flow_entity(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError(
            "Flow entities are submitted through the current reference-media RPC"
        )

    async def copy_project_media_to_character_slot(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        raise RuntimeError(
            "Character media is submitted through the current reference-media RPC"
        )

    async def stream_flow_creation_agent(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        raise RuntimeError(
            "Direct AI Sandbox agent streaming is retired; use current generation RPCs"
        )

    async def _warmup_flow_video_frontend_context(
        self, *args: Any, **kwargs: Any
    ) -> None:
        return None

    async def _labs_trpc_get_with_st(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise RuntimeError("The retired Labs tRPC transport is disabled")

    async def _labs_trpc_post_with_st(
        self, *args: Any, **kwargs: Any
    ) -> Dict[str, Any]:
        raise RuntimeError("The retired Labs tRPC transport is disabled")

    async def _aisandbox_request(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise RuntimeError("The retired AI Sandbox REST transport is disabled")
