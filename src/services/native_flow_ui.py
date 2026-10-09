"""Select native Flow image controls and fail closed if their state cannot be read."""
import asyncio
import json
import re

NATIVE_IMAGE_MODELS = frozenset({"Nano Banana 2.1", "Nano Banana Pro", "Nano Banana 2 Lite"})
NATIVE_IMAGE_RATIOS = frozenset({"16:9", "4:3", "1:1", "3:4", "9:16"})
NATIVE_VIDEO_MODELS = frozenset({"Omni 1.1 Flash"})
NATIVE_VIDEO_RATIOS = frozenset({"16:9", "9:16"})


class NativeFlowUIError(RuntimeError):
    """Settings could not be confirmed, before any generation was submitted."""

    submission_started = False


class NativeCreditLimitError(NativeFlowUIError):
    """A page-visible quote exceeds this request's explicit budget."""

    code = "native_credit_limit"

    def __init__(self, credits_shown: int, max_credits: int):
        self.credits_shown = credits_shown
        self.max_credits = max_credits
        super().__init__(f"网页显示需 {credits_shown} 点数，超过本次 {max_credits} 点数预算；请求未提交")


def validate_native_image_options(options: dict) -> dict:
    if not isinstance(options, dict):
        raise ValueError("原生图片设置必须是对象")
    allowed = {"model_label", "aspect_ratio", "image_count", "reference_images_count", "max_credits"}
    if set(options) - allowed:
        raise ValueError("原生图片设置包含不支持的参数")
    if options.get("model_label") not in NATIVE_IMAGE_MODELS:
        raise ValueError("原生图片模型未支持；必须选择明确的网页模型名称")
    if options.get("aspect_ratio") not in NATIVE_IMAGE_RATIOS:
        raise ValueError("原生图片宽高比未支持")
    if type(options.get("image_count", 1)) is not int or options.get("image_count", 1) != 1:
        raise ValueError("原生图片目前每次只允许生成 1 张")
    if type(options.get("reference_images_count", 0)) is not int or options.get("reference_images_count", 0) != 0:
        raise ValueError("原生图片暂不支持参考图；本次请求未提交")
    if type(options.get("max_credits", 0)) is not int or options.get("max_credits", 0) < 0:
        raise ValueError("原生生成点数预算必须是非负整数")
    return {"model_label": options["model_label"], "aspect_ratio": options["aspect_ratio"],
            "image_count": 1, "reference_images_count": 0, "max_credits": options.get("max_credits", 0)}


def validate_native_video_options(options: dict) -> dict:
    if not isinstance(options, dict):
        raise ValueError("原生视频设置必须是对象")
    allowed = {"model_label", "aspect_ratio", "resolution", "duration_seconds", "video_count",
               "reference_images_count", "max_credits"}
    if set(options) - allowed:
        raise ValueError("原生视频设置包含不支持的参数")
    if options.get("model_label") not in NATIVE_VIDEO_MODELS:
        raise ValueError("原生视频目前仅支持 Omni 1.1 Flash")
    if options.get("aspect_ratio") not in NATIVE_VIDEO_RATIOS:
        raise ValueError("原生视频仅支持 16:9 或 9:16")
    if options.get("resolution") not in {"360p", "720p"}:
        raise ValueError("原生视频仅支持 360p 或 720p")
    if type(options.get("duration_seconds")) is not int or options["duration_seconds"] not in {4, 6, 8, 10}:
        raise ValueError("原生视频仅支持 4、6、8 或 10 秒")
    if type(options.get("video_count", 1)) is not int or options.get("video_count", 1) != 1:
        raise ValueError("原生视频每次只允许生成 1 条")
    if type(options.get("reference_images_count", 0)) is not int or options.get("reference_images_count", 0) != 0:
        raise ValueError("原生视频暂不支持参考图或首尾帧；本次请求未提交")
    if type(options.get("max_credits", 0)) is not int or options.get("max_credits", 0) < 0:
        raise ValueError("原生生成点数预算必须是非负整数")
    return {"model_label": options["model_label"], "aspect_ratio": options["aspect_ratio"],
            "resolution": options["resolution"], "duration_seconds": options["duration_seconds"],
            "video_count": 1, "reference_images_count": 0, "max_credits": options.get("max_credits", 0)}


def _normal(value) -> str:
    return re.sub(r"\s+", "", str(value or "").replace("🍌", "").replace("×", "x")).casefold()


def _find(controls, roles, names):
    names = {_normal(name) for name in names}
    matches = [control for control in controls if control.get("role") in roles
               and any(_normal(control.get(field)) in names for field in ("name", "text"))]
    if len(matches) > 1:
        raise NativeFlowUIError("网页设置控件不唯一；本次请求未提交")
    if matches and matches[0].get("disabled"):
        raise NativeFlowUIError("请求的网页设置当前不可选；本次请求未提交")
    return matches[0] if matches else None


def _model_button(controls):
    return _find(controls, {"button"}, {"选择模型系列", "Select model family", "Select model"})


def _settings_button(controls):
    return _find(controls, {"button"}, {"设置触发器", "设置", "Settings", "Generation settings"})


def _model_option(controls, model):
    trigger = _model_button(controls)
    candidates = [control for control in controls if not trigger or control["selector"] != trigger["selector"]]
    return _find(candidates, {"option", "menuitem", "menuitemradio", "radio", "button"}, {model})


def _radio(controls, names):
    return _find(controls, {"radio"}, names)


def _ratio_names(ratio):
    return {ratio, f"宽高比{ratio}", f"{ratio}宽高比", f"Aspect ratio {ratio}"}


def _read_model(controls):
    models = NATIVE_IMAGE_MODELS | NATIVE_VIDEO_MODELS
    selected = [model for model in models
                if (option := _model_option(controls, model))
                and option.get("selected") is True]
    if len(selected) == 1:
        return selected[0]
    if len(selected) > 1:
        raise NativeFlowUIError("网页模型选中状态不唯一；本次请求未提交")
    trigger = _model_button(controls)
    if trigger:
        labels = [model for model in models
                  if _normal(model) in _normal(trigger.get("text"))]
        if len(labels) == 1:
            return labels[0]
    return None


async def _wait_for(ui, predicate, failure):
    for attempt in range(40):
        controls = await ui.snapshot()
        if not isinstance(controls, list):
            raise NativeFlowUIError("无法读取网页设置；本次请求未提交")
        value = predicate(controls)
        if value:
            return value
        if attempt < 39:
            await asyncio.sleep(0.1)
    raise NativeFlowUIError(failure)


async def _choose_radio(ui, names):
    control = await _wait_for(ui, lambda controls: _radio(controls, names), "请求的网页选项不存在；本次请求未提交")
    if control.get("selected") is not True:
        await ui.click(control["selector"])
    await _wait_for(ui, lambda controls: (current := _radio(controls, names)) and current.get("selected") is True,
                    "网页没有确认所选参数；本次请求未提交")


def _read_settings(controls, expected):
    image = _radio(controls, {"图片", "Image", "Images"})
    video = _radio(controls, {"视频", "Video", "Videos"})
    is_video = "video_count" in expected
    ratios = [ratio for ratio in (NATIVE_VIDEO_RATIOS if is_video else NATIVE_IMAGE_RATIOS)
              if (radio := _radio(controls, _ratio_names(ratio))) and radio.get("selected") is True]
    counts = [count for count in range(1, 5)
              if (radio := _radio(controls, {f"x{count}"})) and radio.get("selected") is True]
    chosen, other = (video, image) if is_video else (image, video)
    verified = (chosen and chosen.get("selected") is True and not (other and other.get("selected") is True)
                and ratios == [expected["aspect_ratio"]] and counts == [1]
                and _read_model(controls) == expected["model_label"])
    if is_video:
        resolutions = [value for value in ("360p", "720p")
                       if (radio := _radio(controls, {value, f"分辨率{value}", f"Resolution {value}"}))
                       and radio.get("selected") is True]
        durations = [value for value in (4, 6, 8, 10)
                     if (radio := _radio(controls, _duration_names(value))) and radio.get("selected") is True]
        verified = verified and resolutions == [expected["resolution"]] and durations == [expected["duration_seconds"]]
        verified = verified and _read_video_input_mode(controls) is not None
    return verified


def _duration_names(seconds):
    return {f"{seconds}秒", f"{seconds}s", f"{seconds} seconds", f"时长{seconds}秒", f"Duration {seconds}s"}


def _read_video_input_mode(controls):
    selected = [label for label, names in (("帧", {"帧", "Frames"}), ("素材", {"素材", "Ingredients"}))
                if (radio := _radio(controls, names)) and radio.get("selected") is True]
    return selected[0] if len(selected) == 1 else None


def _read_credits(controls, maximum):
    costs = [control for control in controls if control.get("role") == "credit_cost"]
    if len(costs) != 1:
        raise NativeFlowUIError("无法确认网页生成费用；本次请求未提交")
    match = re.fullmatch(r"(\d+)\s*(?:个\s*点数|点数|credits?)", str(costs[0].get("name", "")).strip(), re.I)
    if not match:
        raise NativeFlowUIError("网页费用文字无法可靠读取；本次请求未提交")
    cost = int(match.group(1))
    if cost > maximum:
        raise NativeCreditLimitError(cost, maximum)
    return cost


async def configure_native_image_settings(ui, options: dict) -> dict:
    """Use only visible controls. This operation never submits a prompt."""
    return await _configure_native_settings(ui, validate_native_image_options(options))


async def configure_native_video_settings(ui, options: dict) -> dict:
    """Configure text-only Omni video without submitting or attaching media."""
    return await _configure_native_settings(ui, validate_native_video_options(options))


async def verify_native_generation_settings(ui, options: dict, *, media_type: str) -> dict:
    """Read settings and cost again after prompt entry without changing selections."""
    expected = validate_native_video_options(options) if media_type == "video" else validate_native_image_options(options)
    names = {"视频", "Video", "Videos"} if media_type == "video" else {"图片", "Image", "Images"}
    controls = await ui.snapshot()
    if not _radio(controls, names):
        trigger = _settings_button(controls)
        if not trigger:
            raise NativeFlowUIError("找不到网页设置入口；本次请求未提交")
        await ui.click(trigger["selector"])
    return await _verify_native_settings(ui, expected)


async def _configure_native_settings(ui, expected):
    is_video = "video_count" in expected
    mode_names = {"视频", "Video", "Videos"} if is_video else {"图片", "Image", "Images"}
    controls = await ui.snapshot()
    if not _radio(controls, mode_names):
        trigger = _settings_button(controls)
        if not trigger:
            raise NativeFlowUIError("找不到网页设置入口；本次请求未提交")
        await ui.click(trigger["selector"])
    await _choose_radio(ui, mode_names)
    trigger = await _wait_for(ui, _model_button, "找不到网页模型选择器；本次请求未提交")
    if trigger.get("expanded") is not True:
        await ui.click(trigger["selector"])
    option = await _wait_for(ui, lambda controls: _model_option(controls, expected["model_label"]),
        "账号网页未提供所选模型；本次请求未提交")
    await ui.click(option["selector"])
    # A few menu variants stay open after selection; close only if readback says so.
    controls = await ui.snapshot()
    trigger = _model_button(controls)
    if trigger and trigger.get("expanded") is True:
        await ui.click(trigger["selector"])
    await _choose_radio(ui, _ratio_names(expected["aspect_ratio"]))
    if is_video:
        resolution = expected["resolution"]
        await _choose_radio(ui, {resolution, f"分辨率{resolution}", f"Resolution {resolution}"})
        await _choose_radio(ui, _duration_names(expected["duration_seconds"]))
    await _choose_radio(ui, {"x1"})
    return await _verify_native_settings(ui, expected)


async def _verify_native_settings(ui, expected):
    is_video = "video_count" in expected
    controls = await ui.snapshot()
    if _read_model(controls) != expected["model_label"]:
        trigger = _model_button(controls)
        if trigger and trigger.get("expanded") is not True:
            await ui.click(trigger["selector"])
    await _wait_for(ui, lambda controls: _read_settings(controls, expected),
                    "网页最终模型、比例或数量与请求不一致；本次请求未提交")
    controls = await ui.snapshot()
    credits_shown = _read_credits(controls, expected["max_credits"])
    input_mode = _read_video_input_mode(controls) if is_video else None
    trigger = _model_button(controls)
    if trigger and trigger.get("expanded") is True:
        await ui.click(trigger["selector"])
    controls = await ui.snapshot()
    trigger = _settings_button(controls)
    if not trigger:
        raise NativeFlowUIError("无法关闭网页设置面板；本次请求未提交")
    await ui.click(trigger["selector"])
    return {**{key: value for key, value in expected.items() if key != "reference_images_count"},
            "verified_before_submit": True,
            "credits_shown": credits_shown, "max_credits": expected["max_credits"],
            **({"input_mode": input_mode} if is_video else {})}


class NodriverFlowSettingsUI:
    """DOM reads plus real element clicks on the already reserved account tab."""

    def __init__(self, tab, evaluate, *, label: str):
        self.tab, self.evaluate, self.label = tab, evaluate, label
        self._last_controls = {}

    async def snapshot(self):
        result = await self.evaluate(self.tab, NATIVE_SETTINGS_SNAPSHOT,
            label=f"{self.label}:settings", timeout_seconds=5.0, return_by_value=True)
        controls = json.loads(result) if isinstance(result, str) else result
        if not isinstance(controls, list):
            raise NativeFlowUIError("无法读取网页控件；本次请求未提交")
        self._last_controls = {control["selector"]: control for control in controls}
        return controls

    async def click(self, selector):
        expected = self._last_controls.get(selector)
        await self.snapshot()
        current = self._last_controls.get(selector)
        if (not expected or not current or current.get("disabled")
                or current.get("role") != expected.get("role")
                or _normal(current.get("name")) != _normal(expected.get("name"))):
            raise NativeFlowUIError("网页控件已变化，无法确认点击对象；本次请求未提交")
        element = await self.tab.select(selector, timeout=5)
        if element is None:
            raise NativeFlowUIError("网页设置控件已变化；本次请求未提交")
        await element.click()


NATIVE_SETTINGS_SNAPSHOT = r"""/* flow2api-native-settings */ (() => {
    const visible = el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden'
            && style.display !== 'none' && el.getAttribute('aria-hidden') !== 'true';
    };
    const selector = el => {
        const parts = [];
        for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
            if (node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
            let part = node.localName;
            if (node.parentElement) {
                const peers = Array.from(node.parentElement.children).filter(peer => peer.localName === node.localName);
                if (peers.length > 1) part += ':nth-of-type(' + (peers.indexOf(node) + 1) + ')';
            }
            parts.unshift(part);
        }
        return parts.join(' > ');
    };
    const controls = Array.from(document.querySelectorAll(
        'button, [role="button"], [role="radio"], input[type="radio"], [role="option"], [role="menuitem"], [role="menuitemradio"]'
    )).filter(visible).map(el => {
        const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
            .map(id => document.getElementById(id)?.textContent || '').join(' ').trim();
        const text = (el.innerText || el.textContent || '').trim();
        const name = el.getAttribute('aria-label') || labelled || el.labels?.[0]?.innerText || text;
        const selected = el.getAttribute('aria-checked') === 'true' || el.getAttribute('aria-selected') === 'true'
            || el.checked === true || ['checked', 'active', 'on'].includes(el.getAttribute('data-state'));
        return {selector: selector(el), role: el.getAttribute('role') || (el.type === 'radio' ? 'radio' : 'button'),
            name, text, selected, expanded: el.getAttribute('aria-expanded') === 'true',
            disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true'};
    });
    for (const link of Array.from(document.querySelectorAll('a[href]')).filter(visible)) {
        const url = new URL(link.href, location.href);
        if (url.hostname === 'support.google.com' && url.pathname.replace(/\/$/, '') === '/googleone'
            && url.searchParams.get('p') === 'g1_ai_credit_menu') {
            controls.push({selector: selector(link), role: 'credit_cost',
                name: (link.innerText || link.textContent || '').trim()});
        }
    }
    return JSON.stringify(controls);
})()"""
