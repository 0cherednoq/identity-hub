"""Browser fingerprint generators and client-specific argument shapes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from identity_hub.errors import ValidationFailed
from identity_hub.formats.models import (
    BrowserFingerprint,
    Geolocation,
    Screen,
    UserAgentData,
    Viewport,
    WebGl,
    format_loss,
)
from identity_hub.formats.proxy import ProxyUrl

__all__ = ["IMPERSONATION_TARGETS", "dump", "load"]


# Target lists verified against the named client releases. Each tuple is
# (browser major version, public target name), sorted oldest to newest.
IMPERSONATION_TARGETS: dict[str, dict[str, Any]] = {
    "curl_cffi": {
        "verified_version": "0.16.3",
        "chrome": [
            (99, "chrome99"),
            (100, "chrome100"),
            (101, "chrome101"),
            (104, "chrome104"),
            (107, "chrome107"),
            (110, "chrome110"),
            (116, "chrome116"),
            (119, "chrome119"),
            (120, "chrome120"),
            (123, "chrome123"),
            (124, "chrome124"),
        ],
        "firefox": [(110, "firefox110"), (120, "firefox120"), (133, "firefox133")],
        "safari": [(15, "safari15_5"), (17, "safari17_0"), (18, "safari18_0")],
    },
    "tls_client": {
        "verified_version": "2.0.0",
        "chrome": [
            (103, "chrome_103"),
            (104, "chrome_104"),
            (105, "chrome_105"),
            (106, "chrome_106"),
            (107, "chrome_107"),
            (108, "chrome_108"),
            (109, "chrome_109"),
            (110, "chrome_110"),
            (111, "chrome_111"),
            (112, "chrome_112"),
            (116, "chrome_116_PSK"),
            (117, "chrome_117"),
            (120, "chrome_120"),
        ],
        "firefox": [
            (102, "firefox_102"),
            (104, "firefox_104"),
            (108, "firefox_108"),
            (110, "firefox_110"),
            (117, "firefox_117"),
            (120, "firefox_120"),
        ],
        "safari": [(15, "safari_15_6_1"), (16, "safari_16_0")],
    },
    "primp": {
        "verified_version": "2.0.1",
        "chrome": [(version, f"chrome_{version}") for version in range(144, 154)],
        "firefox": [(140, "firefox_140")] + [(version, f"firefox_{version}") for version in range(146, 152)],
        "safari": [(18, "safari_18.5"), (26, "safari_26")],
    },
    "rnet": {
        "verified_version": "3.0.0rc22",
        "chrome": [
            (version, f"chrome_{version}")
            for version in (
                100,
                101,
                104,
                105,
                106,
                107,
                108,
                109,
                110,
                114,
                116,
                117,
                118,
                119,
                120,
                123,
                124,
                *range(126, 150),
            )
        ],
        "firefox": [
            (109, "firefox_109"),
            (117, "firefox_117"),
            (128, "firefox_128"),
            (133, "firefox_133"),
            (135, "firefox_135"),
            (136, "firefox_136"),
            (139, "firefox_139"),
            (142, "firefox_142"),
            (143, "firefox_143"),
            (144, "firefox_144"),
            (145, "firefox_145"),
            (146, "firefox_146"),
            (147, "firefox_147"),
            (148, "firefox_148"),
            (149, "firefox_149"),
        ],
        "safari": [(15, "safari_15.3"), (16, "safari_16"), (17, "safari_17.0"), (18, "safari_18")],
    },
}


def _get(data: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return default


def _browser(ua: str) -> tuple[str, str]:
    patterns = (
        ("edge", r"(?:Edg|Edge)/([\d.]+)"),
        ("chrome", r"(?:Chrome|CriOS)/([\d.]+)"),
        ("firefox", r"(?:Firefox|FxiOS)/([\d.]+)"),
        ("safari", r"Version/([\d.]+).*Safari/"),
    )
    for browser, pattern in patterns:
        match = re.search(pattern, ua)
        if match:
            return browser, match.group(1)
    raise ValidationFailed("не удалось определить браузер и версию из user agent")


def _os(ua: str) -> tuple[str, str | None]:
    if match := re.search(r"Windows NT ([\d.]+)", ua):
        return "windows", match.group(1)
    if match := re.search(r"Android ([\d.]+)", ua):
        return "android", match.group(1)
    if match := re.search(r"(?:iPhone OS|CPU OS) ([\d_]+)", ua):
        return "ios", match.group(1).replace("_", ".")
    if match := re.search(r"Mac OS X ([\d_]+)", ua):
        return "macos", match.group(1).replace("_", ".")
    if "Linux" in ua:
        return "linux", None
    raise ValidationFailed("не удалось определить ОС из user agent")


def _screen(data: Any) -> Screen | None:
    if not isinstance(data, Mapping) or not data.get("width") or not data.get("height"):
        return None
    return Screen(
        width=data["width"],
        height=data["height"],
        avail_width=_get(data, "availWidth", "avail_width"),
        avail_height=_get(data, "availHeight", "avail_height"),
        color_depth=_get(data, "colorDepth", "color_depth"),
        device_pixel_ratio=_get(data, "devicePixelRatio", "device_pixel_ratio", "pixelRatio"),
    )


def _viewport(data: Any) -> Viewport | None:
    if not isinstance(data, Mapping) or not data.get("width") or not data.get("height"):
        return None
    return Viewport(
        width=data["width"],
        height=data["height"],
        device_scale_factor=_get(data, "deviceScaleFactor", "device_scale_factor"),
        is_mobile=_get(data, "isMobile", "is_mobile"),
        has_touch=_get(data, "hasTouch", "has_touch"),
    )


def load(data: Any, *, source: str, strict: bool = False) -> BrowserFingerprint:
    del strict
    if not isinstance(data, Mapping):
        raise ValidationFailed("отпечаток должен быть JSON-объектом")
    raw = deepcopy(dict(data))
    payload = data.get("fingerprint", data) if source == "fingerprint-suite" else data
    if not isinstance(payload, Mapping):
        raise ValidationFailed("fingerprint-suite: fingerprint должен быть объектом")
    navigator = payload.get("navigator", {})
    if not isinstance(navigator, Mapping):
        navigator = {}
    ua = str(_get(navigator, "userAgent", "user_agent", default=payload.get("userAgent", "")))
    if not ua:
        raise ValidationFailed("отпечаток не содержит navigator.userAgent")
    browser, browser_version = _browser(ua)
    os_name, os_version = _os(ua)
    language = _get(navigator, "language", default=payload.get("locale"))
    languages = list(_get(navigator, "languages", default=[]) or [])
    if language and not languages:
        languages = [str(language)]
    ua_data = _get(navigator, "userAgentData", "user_agent_data")
    video = _get(payload, "videoCard", "video_card", "webgl", default={})
    if not isinstance(video, Mapping):
        video = {}
    headers = payload.get("headers", data.get("headers", {}))
    device = "tablet" if "iPad" in ua else "mobile" if "Mobile" in ua or os_name == "android" else "desktop"
    return BrowserFingerprint(
        user_agent=ua,
        browser=browser,
        browser_version=browser_version,
        os=os_name,
        os_version=os_version,
        device=device,
        platform=_get(navigator, "platform"),
        user_agent_data=UserAgentData.model_validate(ua_data) if isinstance(ua_data, Mapping) else None,
        locale=str(language) if language else None,
        languages=languages,
        timezone=_get(payload, "timezone", "timezoneId", "timezone_id"),
        geolocation=(
            Geolocation.model_validate(payload["geolocation"])
            if isinstance(payload.get("geolocation"), Mapping)
            else None
        ),
        screen=_screen(payload.get("screen")),
        viewport=_viewport(payload.get("viewport")),
        hardware_concurrency=_get(navigator, "hardwareConcurrency", "hardware_concurrency"),
        device_memory=_get(navigator, "deviceMemory", "device_memory"),
        max_touch_points=_get(navigator, "maxTouchPoints", "max_touch_points"),
        webgl=WebGl(vendor=video.get("vendor"), renderer=video.get("renderer")) if video else None,
        fonts=list(payload.get("fonts", []) or []),
        webrtc=payload.get("webrtc"),
        headers=dict(headers) if isinstance(headers, Mapping) else {},
        source=source,
        raw=raw,
    )


def _playwright(
    value: BrowserFingerprint, *, proxy: ProxyUrl | str | Mapping[str, str] | None
) -> dict[str, Any]:
    result: dict[str, Any] = {"user_agent": value.user_agent}
    if value.locale is not None:
        result["locale"] = value.locale
    if value.timezone is not None:
        result["timezone_id"] = value.timezone
    if value.viewport is not None:
        result["viewport"] = {"width": value.viewport.width, "height": value.viewport.height}
        if value.viewport.device_scale_factor is not None:
            result["device_scale_factor"] = value.viewport.device_scale_factor
        if value.viewport.is_mobile is not None:
            result["is_mobile"] = value.viewport.is_mobile
        if value.viewport.has_touch is not None:
            result["has_touch"] = value.viewport.has_touch
    if value.screen is not None:
        result["screen"] = {"width": value.screen.width, "height": value.screen.height}
        if "device_scale_factor" not in result and value.screen.device_pixel_ratio is not None:
            result["device_scale_factor"] = value.screen.device_pixel_ratio
    if value.geolocation is not None:
        result["geolocation"] = value.geolocation.model_dump(exclude_none=True)
    if value.headers:
        result["extra_http_headers"] = dict(value.headers)
    if proxy is not None:
        if isinstance(proxy, str):
            proxy = ProxyUrl(proxy)
        result["proxy"] = proxy.dump.playwright() if isinstance(proxy, ProxyUrl) else dict(proxy)
    return result


def _camoufox(value: BrowserFingerprint, *, strict: bool) -> dict[str, Any]:
    if value.browser != "firefox":
        format_loss(
            "Camoufox предназначен для Firefox, а отпечаток относится к другому браузеру", strict=strict
        )
    result: dict[str, Any] = {"navigator.userAgent": value.user_agent}
    if value.screen is not None:
        result.update({"screen.width": value.screen.width, "screen.height": value.screen.height})
    if value.webgl is not None:
        result.update({"webGl:vendor": value.webgl.vendor, "webGl:renderer": value.webgl.renderer})
    if value.timezone is not None:
        result["timezone"] = value.timezone
    if value.locale is not None:
        language, _, region = value.locale.partition("-")
        result["locale:language"] = language
        if region:
            result["locale:region"] = region
    if value.geolocation is not None:
        result.update(
            {
                "geolocation:latitude": value.geolocation.latitude,
                "geolocation:longitude": value.geolocation.longitude,
                "geolocation:accuracy": value.geolocation.accuracy,
            }
        )
    return {key: item for key, item in result.items() if item is not None}


def _impersonate(value: BrowserFingerprint, client: str, *, strict: bool) -> str:
    client_data = IMPERSONATION_TARGETS.get(client)
    if client_data is None:
        raise ValidationFailed("неизвестный клиент impersonate; ожидаются curl_cffi, tls_client, primp, rnet")
    targets = client_data.get(value.browser)
    if not targets:
        raise ValidationFailed(f"{client} не содержит целей для браузера {value.browser}")
    try:
        requested = int(value.browser_version.split(".", 1)[0])
    except ValueError:
        raise ValidationFailed("browser_version должен начинаться с числа") from None
    eligible = [item for item in targets if item[0] <= requested]
    if not eligible:
        raise ValidationFailed(f"у {client} нет цели {value.browser} не новее UA")
    version, target = max(eligible, key=lambda item: item[0])
    if version != requested:
        format_loss(
            f"{client}: версия TLS-профиля {version} отличается от UA {requested}",
            strict=strict,
        )
    return str(target)


def _antidetect(value: BrowserFingerprint, *, gologin: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"ua": value.user_agent}
    if value.screen is not None:
        resolution = f"{value.screen.width}x{value.screen.height}"
        result["resolution" if gologin else "screen_resolution"] = resolution
    if value.webgl is not None:
        key = "webGLMetadata" if gologin else "webgl_config"
        result[key] = {
            "unmasked_vendor": value.webgl.vendor,
            "unmasked_renderer": value.webgl.renderer,
        }
    if value.timezone is not None:
        result["timezone"] = value.timezone
    if value.locale is not None:
        result["language"] = value.locale
    return result


def dump(value: BrowserFingerprint, format_name: str, *, strict: bool = False, **options: Any) -> Any:
    if format_name == "playwright_context":
        return _playwright(value, proxy=options.get("proxy"))
    if format_name == "camoufox":
        return _camoufox(value, strict=strict)
    if format_name == "impersonate":
        return _impersonate(value, str(options.get("client", "")), strict=strict)
    if format_name == "adspower":
        return _antidetect(value, gologin=False)
    if format_name == "gologin":
        return _antidetect(value, gologin=True)
    raise ValidationFailed("неизвестный формат отпечатка")
