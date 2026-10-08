"""Honest, configuration-aware capability metadata for the agent interface.

Availability describes an implemented transport with its configuration present;
it does not prove account access, upstream model identity, or a live generation.
"""

import re
from typing import Any

from ..core.config import config


THIRD_PARTY_CAPTCHA_METHODS = ("yescaptcha", "capmonster", "ezcaptcha", "capsolver", "captcharun")
BROWSER_CAPTCHA_METHODS = ("browser", "personal", "remote_browser", "extension")


def get_parameter_preserving_captcha_method() -> str:
    """Resolve only transports that actually send all requested generation settings."""
    selected = config.captcha_method
    candidates = THIRD_PARTY_CAPTCHA_METHODS if selected in BROWSER_CAPTCHA_METHODS else (selected,)
    for method in candidates:
        if method in THIRD_PARTY_CAPTCHA_METHODS and str(getattr(config, f"{method}_api_key", "") or "").strip():
            return method
    raise ValueError("当前生成通道无法保留模型、比例和参考图参数；请配置第三方验证码服务 API Key")


def allows_native_default_image(model: dict[str, Any], images_count: int) -> bool:
    """Legacy native text-only mode uses the website default, whose identity is unknown."""
    return (
        config.captcha_method in ("browser", "personal")
        and model.get("type") == "image"
        and model.get("model_name") == "NARWHAL"
        and model.get("aspect_ratio") == "IMAGE_ASPECT_RATIO_LANDSCAPE"
        and images_count == 0
        and not model.get("upsample")
    )


def validate_generation_transport(model: dict[str, Any], images_count: int, *, preserve_parameters: bool = False) -> None:
    if not preserve_parameters and allows_native_default_image(model, images_count):
        return
    get_parameter_preserving_captcha_method()


def _capability(model_id: str, model: dict[str, Any]) -> dict[str, Any]:
    is_image = model["type"] == "image"
    aspect = model.get("aspect_ratio", "")
    aspect_ratios = {
        "IMAGE_ASPECT_RATIO_LANDSCAPE": "16:9", "VIDEO_ASPECT_RATIO_LANDSCAPE": "16:9",
        "IMAGE_ASPECT_RATIO_PORTRAIT": "9:16", "VIDEO_ASPECT_RATIO_PORTRAIT": "9:16",
        "IMAGE_ASPECT_RATIO_SQUARE": "1:1", "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE": "4:3",
        "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR": "3:4",
    }
    duration = re.search(r"(?:-|_)(\d+)s(?:-|_|$)", model_id)
    upsample = model.get("upsample")
    resolution = (upsample.get("resolution") if isinstance(upsample, dict) else upsample)
    if resolution:
        resolution = "4K" if "4K" in resolution else "2K" if "2K" in resolution else "1080p"
    entry = {
        "id": model_id,
        "type": model["type"],
        "aspect_ratio": aspect_ratios.get(aspect),
        "resolution": resolution,  # None means the upstream default, not a verified pixel size.
        "duration_seconds": None if is_image else int(duration.group(1)) if duration else 8,
        "min_reference_images": 0 if is_image else int(model.get("min_images", 0)),
        "max_reference_images": 3 if is_image else int(model.get("max_images", 0)),
        "limits_source": "local_policy" if is_image else "implemented_contract",
        "available": True,
        "unavailable_reason": None,
        "verification_state": "implemented_not_live_verified",
        "requires_account": True,
        "requires_third_party_captcha": True,
    }
    try:
        get_parameter_preserving_captcha_method()
    except ValueError as exc:
        entry["available"] = False
        entry["unavailable_reason"] = str(exc)
    if model.get("video_type") == "extend" and "lite" not in model_id:
        entry.update(available=False, verification_state="needs_protocol_verification",
                     unavailable_reason="当前 Flow 仅确认 Lite 续写；此续写模型需要重新验证协议")
    elif model.get("video_type") == "extend":
        entry.update(available=False, unavailable_reason="视频续写需要源视频标识，当前 Agent 请求尚不支持此输入")
    return entry


def get_model_capabilities() -> list[dict[str, Any]]:
    # Import lazily so the handler can use the transport preflight before uploads.
    from .generation_handler import MODEL_CONFIG

    entries = [_capability(model_id, model) for model_id, model in MODEL_CONFIG.items()
               if model.get("listed", True)]
    entries.append({
        "id": "gemini-nano-banana-2.1",
        "type": "image",
        "aspect_ratio": None,
        "resolution": None,
        "duration_seconds": None,
        "max_reference_images": 0,
        "available": False,
        "unavailable_reason": "Nano Banana 2.1 的 Flow 模型键尚未经过协议验证",
        "verification_state": "needs_protocol_verification",
    })
    return entries


def validate_generation_request(model: str, images_count: int) -> dict[str, Any]:
    """Return accepted capability metadata, or fail before any external work."""
    entry = next((item for item in get_model_capabilities() if item["id"] == model), None)
    if entry is None:
        raise ValueError(f"不支持的模型: {model}")
    if not entry["available"]:
        raise ValueError(entry["unavailable_reason"])
    if type(images_count) is not int or images_count < 0:
        raise ValueError("参考图片数量必须是非负整数")
    minimum, maximum = entry.get("min_reference_images", 0), entry["max_reference_images"]
    if not minimum <= images_count <= maximum:
        raise ValueError(f"模型 {model} 需要 {minimum}–{maximum} 张参考图片，当前为 {images_count} 张")
    return entry
