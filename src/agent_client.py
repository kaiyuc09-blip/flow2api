"""Small HTTP client for Agent tools; never imports the Flow application."""
from dataclasses import dataclass, field
from pathlib import Path
import base64
from io import BytesIO
import re
import warnings
import hashlib
import uuid
from urllib.parse import urlsplit
from urllib.parse import unquote
import asyncio
import ipaddress
import socket
import os

import httpx
from PIL import Image

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_REFERENCE_IMAGES = 8
MAX_ENCODED_REFERENCES = 28 * 1024 * 1024
MAX_MEDIA_BYTES = 200 * 1024 * 1024
IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class AgentClientError(Exception):
    """A deliberately safe error suitable for returning to an Agent."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _validate_image(content: bytes) -> tuple[str, int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if image.format not in IMAGE_MIMES or image.width * image.height > 40_000_000:
                    raise ValueError()
                result = IMAGE_MIMES[image.format], image.width, image.height
                image.verify()
            with Image.open(BytesIO(content)) as image:
                image.load()
        return result
    except Exception:
        raise AgentClientError("invalid_image", "A reference or result must be a valid PNG, JPEG or WebP image (up to 40 megapixels).") from None


def _validate_mp4(content: bytes):
    """Validate bounded ISO BMFF boxes, not playback or semantic video quality."""
    position, boxes = 0, set()
    while position < len(content):
        if len(content) - position < 8:
            break
        size = int.from_bytes(content[position:position + 4], "big")
        kind = content[position + 4:position + 8]
        header = 8
        if size == 1:
            if len(content) - position < 16:
                break
            size = int.from_bytes(content[position + 8:position + 16], "big")
            header = 16
        elif size == 0:
            size = len(content) - position
        if size < header or position + size > len(content):
            break
        if kind in {b"moov", b"mdat"} and size == header:
            break
        if position == 0 and (kind != b"ftyp" or size < 20):
            break
        boxes.add(kind)
        position += size
    if position != len(content) or not {b"ftyp", b"moov", b"mdat"}.issubset(boxes):
        raise AgentClientError("invalid_video", "The downloaded result is not a complete MP4 container.")


@dataclass(frozen=True)
class AgentSettings:
    base_url: str
    api_key: str = field(repr=False)
    output_dir: Path

    def __post_init__(self):
        try:
            url = httpx.URL(self.base_url)
            local_http = url.scheme == "http" and (url.host in {"localhost", "127.0.0.1", "::1"})
            if (url.scheme != "https" and not local_http) or not url.host or url.userinfo or url.query or url.fragment:
                raise ValueError()
            if not self.api_key or any(character.isspace() for character in self.api_key) or len(self.api_key) > 4096:
                raise ValueError()
            output = Path(self.output_dir).expanduser()
            if not output.is_absolute():
                raise ValueError()
            object.__setattr__(self, "output_dir", output.resolve())
        except (ValueError, OSError, httpx.InvalidURL):
            raise AgentClientError("invalid_configuration", "Configure a local HTTP or remote HTTPS service URL, a nonempty API key, and an absolute output directory.") from None

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        key = env.get("FLOW2API_API_KEY", "").strip()
        if not key and env.get("FLOW2API_API_KEY_FILE"):
            try:
                with Path(env["FLOW2API_API_KEY_FILE"]).expanduser().open("r", encoding="utf-8") as handle:
                    key = handle.read(4097).strip()
            except (OSError, UnicodeError):
                raise AgentClientError("configuration_missing", "The configured API key file is unreadable.") from None
        if not key or not env.get("FLOW2API_OUTPUT_DIR"):
            raise AgentClientError("configuration_missing", "Set FLOW2API_API_KEY_FILE (or FLOW2API_API_KEY) and FLOW2API_OUTPUT_DIR before using tools.")
        return cls(env.get("FLOW2API_BASE_URL", "http://127.0.0.1:8000"), key, Path(env["FLOW2API_OUTPUT_DIR"]))


async def _resolve_public_host(host: str) -> list[str]:
    records = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM), 10
    )
    return list(dict.fromkeys(record[4][0] for record in records))


class AgentClient:
    def __init__(self, settings: AgentSettings, *, transport=None, resolver=None):
        self.settings = settings
        self.resolve_host = resolver or _resolve_public_host
        self.http = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(60, connect=10))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.http.aclose()

    async def list_models(self):
        return await self._api("GET", "/v1/agent/models")

    async def _api(self, method: str, path: str, **kwargs):
        try:
            response = await self.http.request(
                method, self.settings.base_url.rstrip("/") + path,
                headers={"Authorization": "Bearer " + self.settings.api_key}, **kwargs,
            )
        except httpx.RequestError:
            raise AgentClientError("submission_unknown" if method == "POST" else "service_unreachable", "Service connection failed; do not automatically submit another generation.") from None
        if response.status_code not in {200, 202}:
            if method == "POST" and response.status_code >= 500:
                raise AgentClientError("submission_unknown", "The service failed after submission may have begun. Preserve request_id; do not automatically regenerate.")
            code = {401: "authentication_failed", 403: "permission_denied", 404: "not_found", 409: "request_conflict", 422: "invalid_request", 429: "rate_limited"}.get(response.status_code, "service_error")
            raise AgentClientError(code, "Flow2API rejected the request (HTTP " + str(response.status_code) + ").")
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError()
            # The upstream must not be able to echo our service credential to an Agent.
            return self._redact(payload)
        except (ValueError, TypeError):
            raise AgentClientError("submission_unknown" if method == "POST" else "invalid_response", "The service returned an invalid response; do not automatically resubmit.") from None

    def _redact(self, value):
        if isinstance(value, str):
            return value.replace(self.settings.api_key, "[redacted]")
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        return value

    async def get_generation(self, generation_id: str | None = None, request_id: str | None = None):
        if (generation_id is None) == (request_id is None):
            raise AgentClientError("invalid_lookup", "Provide exactly one generation_id or request_id.")
        if request_id is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,127}", request_id):
                raise AgentClientError("invalid_request_id", "Use the original request_id from submission.")
            path = "/v1/agent/generations/by-request/" + request_id
        else:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", generation_id):
                raise AgentClientError("invalid_generation_id", "Use the generation ID returned by submission.")
            path = "/v1/agent/generations/" + generation_id
        result = await self._api("GET", path)
        if result.get("status") == "completed":
            for media in result.get("media", []):
                media.update(await self._save_media(media))
                if str(media.get("url", "")).startswith("data:"):
                    media.pop("url", None)
                    media["source"] = "inline_image"
        return result

    async def _save_media(self, media: dict):
        url = str(media.get("url") or "")
        if url.startswith("data:"):
            if media.get("type") != "image":
                raise AgentClientError("invalid_media_type", "Inline data is supported only for image results.")
            header, separator, encoded = url.partition(";base64,")
            declared_mime = header.removeprefix("data:")
            if not separator or declared_mime not in IMAGE_MIMES.values():
                raise AgentClientError("invalid_inline_image", "Unsupported inline image encoding or MIME type.")
            if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                raise AgentClientError("media_too_large", "Inline image exceeds the 20 MiB limit.")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, UnicodeError):
                raise AgentClientError("invalid_inline_image", "Invalid base64 image data.") from None
            if len(content) > MAX_IMAGE_BYTES:
                raise AgentClientError("media_too_large", "Inline image exceeds the 20 MiB limit.")
            if _validate_image(content)[0] != declared_mime:
                raise AgentClientError("invalid_inline_image", "Inline MIME type does not match the actual image.")
        else:
            content = await self._download(url)
        if media.get("type") == "image":
            mime, width, height = _validate_image(content)
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
            metadata = {"mime_type": mime, "width": width, "height": height, "validation": "image_decoded"}
        elif media.get("type") == "video":
            _validate_mp4(content)
            suffix, metadata = ".mp4", {"mime_type": "video/mp4", "validation": "mp4_container_only"}
        else:
            raise AgentClientError("invalid_media_type", "The result must specify image or video media.")
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        output = self.settings.output_dir / (uuid.uuid4().hex + suffix)
        with output.open("xb") as handle:
            handle.write(content)
        return {"local_path": str(output.resolve()), "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(), **metadata}

    async def _download(self, url: str) -> bytes:
        base = httpx.URL(self.settings.base_url)
        for _ in range(6):
            try:
                target = httpx.URL(url)
                parsed = urlsplit(url)
            except (ValueError, httpx.InvalidURL):
                raise AgentClientError("unsafe_media_url", "Invalid media URL.") from None
            if target.userinfo or target.fragment or not target.host:
                raise AgentClientError("unsafe_media_url", "Invalid media URL.")
            headers, extensions = {}, {}
            targets = [target]
            same_origin = (target.scheme, target.host, target.port) == (base.scheme, base.host, base.port)
            if same_origin:
                path = unquote(parsed.path)
                cache_prefix = base.path.rstrip("/") + "/tmp/"
                if not path.startswith(cache_prefix) or any(part in {".", ".."} for part in path.split("/")) or "\\" in path:
                    raise AgentClientError("unsafe_media_url", "Only the configured service cache can be downloaded locally.")
                headers["Authorization"] = "Bearer " + self.settings.api_key
            else:
                if target.scheme != "https" or target.port not in (None, 443):
                    raise AgentClientError("unsafe_media_url", "External media must use public HTTPS on port 443.")
                try:
                    addresses = await self.resolve_host(target.host)
                    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
                        raise ValueError()
                except (OSError, ValueError, TimeoutError):
                    raise AgentClientError("unsafe_media_url", "External media must resolve exclusively to public addresses.") from None
                # Pin the validated IP so a second DNS lookup cannot redirect to a local address.
                headers["Host"] = target.netloc.decode("ascii")
                extensions["sni_hostname"] = target.host
                targets = [target.copy_with(host=address) for address in dict.fromkeys(addresses)]
            for index, pinned_target in enumerate(targets):
                connected = False
                try:
                    async with self.http.stream("GET", pinned_target, headers=headers, extensions=extensions) as response:
                        connected = True
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise AgentClientError("download_failed", "Media redirect has no destination.")
                            url = str(httpx.URL(url).join(location))
                            # Revalidate the redirect destination in the outer loop.
                            break
                        if response.status_code != 200:
                            raise AgentClientError("download_failed", "The media download failed; query the existing task again.")
                        chunks, total = [], 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_MEDIA_BYTES:
                                raise AgentClientError("media_too_large", "The result exceeds the 200 MiB download limit.")
                            chunks.append(chunk)
                        return b"".join(chunks)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    # Only an external media GET may try its next validated IP.
                    # A started body/read timeout must never restart a whole video.
                    if not connected and not same_origin and index + 1 < len(targets):
                        continue
                    raise AgentClientError("download_failed", "Could not connect to the media download destination.") from None
                except httpx.RequestError:
                    raise AgentClientError("download_failed", "The media download was interrupted; no automatic restart was attempted.") from None
        raise AgentClientError("download_failed", "Too many media redirects.")

    async def submit(self, kind: str, model: str, prompt: str, image_paths: list[str], request_id: str, max_credits: int = 0):
        if type(max_credits) is not int or not 0 <= max_credits <= 1000:
            raise AgentClientError("invalid_credit_limit", "max_credits must be an explicitly approved integer from 0 to 1000; default 0 permits only a native UI showing zero credits.")
        if not prompt.strip() or len(prompt) > 16_000:
            raise AgentClientError("invalid_prompt", "Provide a nonempty prompt of at most 16000 characters.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,127}", request_id):
            raise AgentClientError("invalid_request_id", "Use a stable unique request_id of 8-128 letters, digits, dots, underscores or hyphens.")
        catalog = await self.list_models()
        entry = next((item for item in catalog.get("data", []) if item.get("id") == model), None)
        if not entry or entry.get("type") != kind:
            raise AgentClientError("invalid_model", "Choose an exact model ID of the correct media type from list_models.")
        if not entry.get("available"):
            raise AgentClientError("model_unavailable", "This model is unavailable with the current service configuration.")
        maximum = min(int(entry.get("max_reference_images") or 0), MAX_REFERENCE_IMAGES)
        if len(image_paths) < int(entry.get("min_reference_images") or 0):
            raise AgentClientError("missing_reference_image", "This model requires reference images; consult list_models.")
        if len(image_paths) > maximum:
            raise AgentClientError("too_many_images", "Too many reference images for this model.")
        images, encoded_size = [], 0
        for raw_path in image_paths:
            try:
                path = Path(raw_path).expanduser()
                if not path.is_absolute() or not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
                    raise ValueError()
                with path.open("rb") as handle:
                    content = handle.read(MAX_IMAGE_BYTES + 1)
                if len(content) > MAX_IMAGE_BYTES:
                    raise ValueError()
            except (OSError, ValueError):
                raise AgentClientError("invalid_image_path", "Reference images must be readable absolute file paths of at most 20 MiB each.") from None
            mime, _, _ = _validate_image(content)
            encoded = "data:" + mime + ";base64," + base64.b64encode(content).decode("ascii")
            encoded_size += len(encoded)
            if encoded_size > MAX_ENCODED_REFERENCES:
                raise AgentClientError("references_too_large", "Reference images exceed the 28 MiB total encoded request limit.")
            images.append(encoded)
        try:
            return await self._api("POST", "/v1/agent/generations", json={"model": model, "prompt": prompt, "images": images, "request_id": request_id, "max_credits": max_credits})
        except AgentClientError as error:
            if error.code != "submission_unknown":
                raise
            return {"id": None, "request_id": request_id, "model": model, "status": "unknown", "media": [], "warnings": [], "error": {"code": error.code, "message": str(error), "retryable": False}}
