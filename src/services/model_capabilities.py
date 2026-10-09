"""Honest, configuration-aware capability metadata for the agent interface.

Availability describes an implemented transport with its configuration present;
it does not prove account access, upstream model identity, or a live generation.
"""

import re
from typing import Any

from ..core.config import config
from .generation_policy import get_native_credit_limit


THIRD_PARTY_CAPTCHA_METHODS = ("yescaptcha", "capmonster", "ezcaptcha", "capsolver", "captcharun")
BROWSER_CAPTCHA_METHODS = ("browser", "personal", "remote_browser", "extension")
NATIVE_IMAGE_MODEL_LABEL = "Nano Banana 2.1"
NATIVE_IMAGE_ASPECTS = {
    "IMAGE_ASPECT_RATIO_LANDSCAPE": "16:9",
    "IMAGE_ASPECT_RATIO_PORTRAIT": "9:16",
    "IMAGE_ASPECT_RATIO_SQUARE": "1:1",
    "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE": "4:3",
    "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR": "3:4",
}
NATIVE_VIDEO_ASPECTS = {"VIDEO_ASPECT_RATIO_LANDSCAPE": "16:9", "VIDEO_ASPECT_RATIO_PORTRAIT": "9:16"}


def validate_native_image_options(options: dict[str, Any], images_count: int = 0) -> None:
    """Reject unsupported native requests before account lookup, uploads or clicks."""
    if config.captcha_method != "personal":
        raise ValueError("Nano Banana 2.1 仅支持 personal 浏览器原生模式；RPC 模型协议尚未验证")
    if images_count or options.get("reference_images_count") != 0:
        raise ValueError("浏览器原生图片模式尚不支持可靠上传参考图，请使用纯文生图")
    if options.get("model_label") != NATIVE_IMAGE_MODEL_LABEL:
        raise ValueError("未实现的浏览器原生图片模型")
    if options.get("aspect_ratio") not in NATIVE_IMAGE_ASPECTS.values():
        raise ValueError("未实现的浏览器原生图片比例")
    if type(options.get("image_count")) is not int or options["image_count"] != 1:
        raise ValueError("浏览器原生图片模式仅支持每次生成 1 张")
    if type(options.get("max_credits", 0)) is not int or not 0 <= options.get("max_credits", 0) <= 1000:
        raise ValueError("浏览器原生生成预算必须为 0–1000 之间的整数")


def get_native_image_options(model: dict[str, Any], images_count: int = 0) -> dict[str, Any] | None:
    if model.get("generation_transport") != "native_ui" or model.get("type") != "image":
        return None
    if model.get("upsample"):
        raise ValueError("浏览器原生模式尚未实现图片放大")
    options = {
        "model_label": model.get("native_model_label"),
        "aspect_ratio": NATIVE_IMAGE_ASPECTS.get(model.get("aspect_ratio")),
        "image_count": 1,
        "reference_images_count": 0,
        "max_credits": get_native_credit_limit(),
    }
    validate_native_image_options(options, images_count)
    return options


def validate_native_video_options(options: dict[str, Any], images_count: int = 0) -> None:
    if config.captcha_method != "personal":
        raise ValueError("原生 Omni 文生视频仅支持 personal 浏览器模式")
    if images_count or options.get("reference_images_count") != 0:
        raise ValueError("浏览器原生视频尚不支持可靠上传参考图，请使用纯文生视频")
    if options.get("model_label") != "Omni 1.1 Flash" or options.get("aspect_ratio") not in NATIVE_VIDEO_ASPECTS.values():
        raise ValueError("未实现的浏览器原生视频模型或比例")
    if options.get("resolution") not in ("360p", "720p"):
        raise ValueError("浏览器原生视频仅支持 360p 或 720p")
    if type(options.get("duration_seconds")) is not int or options["duration_seconds"] not in (4, 6, 8, 10):
        raise ValueError("浏览器原生视频时长必须为 4、6、8 或 10 秒")
    if type(options.get("video_count")) is not int or options["video_count"] != 1:
        raise ValueError("浏览器原生视频仅支持每次生成 1 个")
    if type(options.get("max_credits", 0)) is not int or not 0 <= options.get("max_credits", 0) <= 1000:
        raise ValueError("浏览器原生生成预算必须为 0–1000 之间的整数")


def get_native_video_options(model: dict[str, Any], images_count: int = 0) -> dict[str, Any] | None:
    if model.get("generation_transport") != "native_ui" or model.get("type") != "video":
        return None
    if model.get("video_type") != "t2v" or model.get("upsample"):
        raise ValueError("浏览器原生视频仅支持文生视频")
    options = {
        "model_label": model.get("native_model_label"),
        "aspect_ratio": NATIVE_VIDEO_ASPECTS.get(model.get("aspect_ratio")),
        "resolution": model.get("resolution"),
        "duration_seconds": model.get("duration_seconds"),
        "video_count": 1,
        "reference_images_count": 0,
        "max_credits": get_native_credit_limit(),
    }
    validate_native_video_options(options, images_count)
    return options


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
    if get_native_image_options(model, images_count) is not None:
        return
    if get_native_video_options(model, images_count) is not None:
        return
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
    if model.get("generation_transport") == "native_ui":
        entry.update(
            generation_transport="native_ui",
            ui_model_label=model.get("native_model_label"),
            max_reference_images=0,
            requires_third_party_captcha=False,
            verification_state="ui_option_observed_generation_not_live_verified",
            ui_option_observed=True,
            live_generation_verified=False,
            upstream_model_verified=False,
        )
        entry["image_count" if is_image else "video_count"] = 1
        if not is_image:
            entry.update(resolution=model.get("resolution"), duration_seconds=model.get("duration_seconds"))
        try:
            (get_native_image_options if is_image else get_native_video_options)(model, 0)
        except ValueError as exc:
            entry.update(available=False, unavailable_reason=str(exc), verification_state="needs_protocol_verification")
        return entry
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

    native_ui_enabled = config.captcha_method == "personal"
    entries = [_capability(model_id, model) for model_id, model in MODEL_CONFIG.items()
               if model.get("listed", True)
               and (native_ui_enabled or model.get("generation_transport") != "native_ui")]
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
