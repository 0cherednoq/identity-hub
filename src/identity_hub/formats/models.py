"""Стандартные модели credentials, sessions, cookie и browser fingerprint (D-314)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import struct
import time
import warnings
from collections.abc import Collection, Iterator, Mapping
from datetime import datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal, Self, cast
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from pydantic import BaseModel, Field, RootModel, SecretStr, field_validator, model_validator

from identity_hub.errors import ValidationFailed
from identity_hub.formats.namespaces import (
    DumpDescriptor,
    DumpNamespace,
    LoadDescriptor,
    LoadNamespace,
)

__all__ = [
    "ApiKey",
    "BrowserFingerprint",
    "BrowserSession",
    "ClientCertificate",
    "Cookie",
    "Cookies",
    "FormatLossWarning",
    "Geolocation",
    "HeaderSession",
    "OAuth1Client",
    "OAuth1Session",
    "OAuthClient",
    "OriginStorage",
    "OtpSecret",
    "PartitionKey",
    "PasswordCredentials",
    "PrivateKeyCredentials",
    "RecoveryCodes",
    "SameSite",
    "Screen",
    "SigningKey",
    "TokenSession",
    "UserAgentData",
    "Viewport",
    "WebGl",
]


class FormatLossWarning(UserWarning):
    """Целевой формат не умеет представить часть исходной модели."""


def format_loss(message: str, *, strict: bool = False) -> None:
    if strict:
        raise ValidationFailed(message)
    warnings.warn(message, FormatLossWarning, stacklevel=3)


class SameSite(StrEnum):
    STRICT = "strict"
    LAX = "lax"
    NONE = "none"


class PartitionKey(BaseModel):
    top_level_site: str
    has_cross_site_ancestor: bool | None = None


def _alias(data: dict[str, Any], target: str, *sources: str) -> None:
    if target in data:
        return
    for source in sources:
        if source in data:
            data[target] = data[source]
            return


def _expires(value: Any) -> float | None:
    if value is None or value == "" or value == "Infinity":
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(value)
                except (TypeError, ValueError):
                    raise ValidationFailed("неизвестный формат срока cookie") from None
            return parsed.timestamp()
    if not isinstance(value, int | float):
        raise ValidationFailed("срок cookie должен быть числом или датой")
    result = float(value)
    if result <= 0:
        return None
    if result > 11_644_473_600_000_000:  # микросекунды от 1601-01-01 (Chrome)
        result = result / 1_000_000 - 11_644_473_600
    elif result > 10_000_000_000:  # миллисекунды Unix
        result /= 1000
    return result


def _same_site(value: Any) -> SameSite | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("-", "_")
    if normalized in {"", "unspecified", "default", "-1"}:
        return None
    if normalized in {"none", "no_restriction", "0"}:
        return SameSite.NONE
    if normalized in {"lax", "1"}:
        return SameSite.LAX
    if normalized in {"strict", "2"}:
        return SameSite.STRICT
    raise ValidationFailed("неизвестное значение SameSite")


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none"}
    return bool(value)


class Cookie(BaseModel):
    name: str
    value: SecretStr
    domain: str
    host_only: bool = False
    path: str = "/"
    expires: float | None = None
    secure: bool = False
    http_only: bool = False
    same_site: SameSite | None = None
    partition_key: PartitionKey | None = None
    created_at: float | None = None
    extras: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_dialects(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        _alias(data, "name", "key")
        _alias(data, "host_only", "hostOnly")
        _alias(data, "http_only", "httpOnly")
        _alias(data, "same_site", "sameSite")
        _alias(data, "partition_key", "partitionKey")
        _alias(data, "created_at", "creation")
        _alias(data, "expires", "expiry", "expirationDate", "expires_utc")
        domain = str(data.get("domain", ""))
        if "host_only" not in data:
            data["host_only"] = not domain.startswith(".")
        data["domain"] = domain.lstrip(".").lower()
        if data.get("session") is True:
            data["expires"] = None
        data["expires"] = _expires(data.get("expires"))
        data["same_site"] = _same_site(data.get("same_site"))
        rest = data.get("rest")
        if "http_only" not in data and isinstance(rest, Mapping):
            for key, item in rest.items():
                if str(key).lower().replace("_", "") == "httponly":
                    data["http_only"] = _truthy(item if item is not None else True)
                    break
        elif "http_only" in data:
            data["http_only"] = _truthy(data["http_only"])
        return data

    @field_validator("name", "domain")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("поле cookie не может быть пустым")
        return value

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return value if value.startswith("/") else "/"

    @model_validator(mode="after")
    def validate_prefixes(self) -> Cookie:
        if self.name.startswith("__Secure-") and not self.secure:
            raise ValidationFailed("cookie с префиксом __Secure- требует Secure")
        if self.name.startswith("__Host-") and (not self.secure or not self.host_only or self.path != "/"):
            raise ValidationFailed("cookie с префиксом __Host- требует Secure, host-only и Path=/")
        if self.same_site is SameSite.NONE and not self.secure:
            format_loss("SameSite=None без Secure будет отвергнут браузером")
        if self.partition_key is not None and not self.secure:
            format_loss("partitioned cookie без Secure будет отвергнута браузером")
        return self

    @property
    def key(self) -> tuple[str, bool, str, str, str | None]:
        partition = self.partition_key.top_level_site if self.partition_key else None
        return self.domain, self.host_only, self.path, self.name, partition

    def is_expired(self, now: float | None = None) -> bool:
        return self.expires is not None and self.expires <= (time.time() if now is None else now)

    def matches(self, url: str) -> bool:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if self.host_only:
            if host != self.domain:
                return False
        elif host != self.domain and not host.endswith(f".{self.domain}"):
            return False
        if self.secure and parsed.scheme not in {"https", "wss"}:
            return False
        request_path = parsed.path or "/"
        if request_path != self.path and not (
            request_path.startswith(self.path)
            and (self.path.endswith("/") or request_path[len(self.path) :].startswith("/"))
        ):
            return False
        return not self.is_expired()


class CookiesDump(DumpNamespace[Any]):
    def json(self, *, strict: bool = False) -> list[dict[str, Any]]:
        from identity_hub.formats import json as json_format

        result = json_format.dump(self._value, strict=strict)
        assert isinstance(result, list)
        return cast(list[dict[str, Any]], result)


class CookiesLoad(LoadNamespace[Any]):
    def json(self, data: Any, *, strict: bool = False) -> Cookies:
        from identity_hub.formats import json as json_format

        return json_format.load(Cookies, data, strict=strict)


class Cookies(RootModel[list[Cookie]]):
    root: list[Cookie] = Field(default_factory=list)

    dump: ClassVar[DumpDescriptor[Cookies, CookiesDump]] = DumpDescriptor(CookiesDump)
    load: ClassVar[LoadDescriptor[Cookies, CookiesLoad]] = LoadDescriptor(CookiesLoad)

    @model_validator(mode="after")
    def unique_keys(self) -> Cookies:
        by_key: dict[tuple[str, bool, str, str, str | None], Cookie] = {}
        for cookie in self.root:
            by_key[cookie.key] = cookie
        self.root = list(by_key.values())
        return self

    def __iter__(self) -> Iterator[Cookie]:  # type: ignore[override]
        return iter(self.root)

    def __len__(self) -> int:
        return len(self.root)

    def for_url(self, url: str) -> Cookies:
        return Cookies([cookie for cookie in self.root if cookie.matches(url)])

    def header_for(self, url: str) -> str:
        cookies = sorted(self.for_url(url).root, key=lambda item: len(item.path), reverse=True)
        return "; ".join(f"{item.name}={item.value.get_secret_value()}" for item in cookies)

    def get(self, name: str, *, domain: str | None = None, path: str | None = None) -> Cookie | None:
        candidates = [
            cookie
            for cookie in self.root
            if cookie.name == name
            and (domain is None or cookie.domain == domain.lstrip(".").lower())
            and (path is None or cookie.path == path)
        ]
        return max(candidates, key=lambda item: len(item.path), default=None)


class OriginStorage(BaseModel):
    origin: str
    local_storage: dict[str, str] = Field(default_factory=dict)
    session_storage: dict[str, str] = Field(default_factory=dict)


class BrowserSessionDump(DumpNamespace[Any]):
    def json(self, *, strict: bool = False) -> dict[str, Any]:
        from identity_hub.formats import json as json_format

        result = json_format.dump(self._value, strict=strict)
        assert isinstance(result, dict)
        return result


class BrowserSessionLoad(LoadNamespace[Any]):
    def json(self, data: Any, *, strict: bool = False) -> BrowserSession:
        from identity_hub.formats import json as json_format

        return json_format.load(BrowserSession, data, strict=strict)


class BrowserSession(BaseModel):
    cookies: Cookies = Field(default_factory=Cookies)
    origins: list[OriginStorage] = Field(default_factory=list)
    indexed_db: Any | None = None

    dump: ClassVar[DumpDescriptor[BrowserSession, BrowserSessionDump]] = DumpDescriptor(BrowserSessionDump)
    load: ClassVar[LoadDescriptor[BrowserSession, BrowserSessionLoad]] = LoadDescriptor(BrowserSessionLoad)

    def expires_at(self, auth_cookies: Collection[str]) -> float | None:
        matched = [cookie for cookie in self.cookies if cookie.name in auth_cookies]
        if not matched or any(cookie.expires is None for cookie in matched):
            return None
        return min(cookie.expires for cookie in matched if cookie.expires is not None)


class TokenSession(BaseModel):
    access_token: SecretStr
    token_type: str = "Bearer"
    refresh_token: SecretStr | None = None
    scope: str | None = None
    id_token: SecretStr | None = None
    expires_at: float | None = None

    def authorization_header(self) -> dict[str, str]:
        return {"Authorization": f"{self.token_type} {self.access_token.get_secret_value()}"}

    def is_expired(self, now: float | None = None, *, leeway: float = 60) -> bool:
        return (
            self.expires_at is not None and self.expires_at <= (time.time() if now is None else now) + leeway
        )

    def id_claims(self) -> dict[str, Any] | None:
        if self.id_token is None:
            return None
        try:
            payload = self.id_token.get_secret_value().split(".")[1]
            decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            result = json.loads(decoded)
        except (IndexError, ValueError, UnicodeDecodeError, binascii.Error):
            raise ValidationFailed("id_token не является JWT") from None
        if not isinstance(result, dict):
            raise ValidationFailed("payload id_token должен быть объектом")
        return result


class OAuth1Session(BaseModel):
    token: SecretStr
    token_secret: SecretStr


class HeaderSession(BaseModel):
    headers: dict[str, SecretStr]
    expires_at: float | None = None


class PasswordCredentials(BaseModel):
    login: str
    password: SecretStr
    email: str | None = None
    phone: str | None = None

    def basic_auth(self) -> dict[str, str]:
        raw = f"{self.login}:{self.password.get_secret_value()}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


class OtpSecret(BaseModel):
    type: Literal["totp", "hotp"] = "totp"
    secret: SecretStr
    issuer: str | None = None
    account: str | None = None
    algorithm: Literal["SHA1", "SHA256", "SHA512"] = "SHA1"
    digits: Literal[6, 8] = 6
    period: int = 30
    counter: int | None = None

    @model_validator(mode="after")
    def validate_kind(self) -> OtpSecret:
        if self.period <= 0:
            raise ValueError("period должен быть положительным")
        if self.type == "hotp" and (self.counter is None or self.counter < 0):
            raise ValueError("HOTP требует неотрицательный counter")
        return self

    @classmethod
    def from_uri(cls, uri: str) -> Self:
        parsed = urlsplit(uri)
        if parsed.scheme != "otpauth" or parsed.netloc not in {"totp", "hotp"}:
            raise ValidationFailed("ожидается otpauth://totp или otpauth://hotp")
        query = parse_qs(parsed.query)
        if not query.get("secret"):
            raise ValidationFailed("otpauth URI не содержит secret")
        label = unquote(parsed.path.lstrip("/"))
        label_issuer, separator, account = label.partition(":")
        issuer = query.get("issuer", [label_issuer if separator else None])[0]
        return cls(
            type=parsed.netloc,
            secret=query["secret"][0],
            issuer=issuer,
            account=account if separator else label or None,
            algorithm=query.get("algorithm", ["SHA1"])[0].upper(),
            digits=int(query.get("digits", ["6"])[0]),
            period=int(query.get("period", ["30"])[0]),
            counter=int(query["counter"][0]) if "counter" in query else None,
        )

    def to_uri(self) -> str:
        label = self.account or ""
        if self.issuer:
            label = f"{self.issuer}:{label}"
        query: list[tuple[str, str]] = [("secret", self.secret.get_secret_value().rstrip("="))]
        if self.issuer:
            query.append(("issuer", self.issuer))
        if self.algorithm != "SHA1":
            query.append(("algorithm", self.algorithm))
        if self.digits != 6:
            query.append(("digits", str(self.digits)))
        if self.type == "totp" and self.period != 30:
            query.append(("period", str(self.period)))
        if self.type == "hotp":
            query.append(("counter", str(self.counter)))
        return f"otpauth://{self.type}/{quote(label, safe='')}?{urlencode(query)}"

    def code(self, at: float) -> str:
        moving = int(at // self.period) if self.type == "totp" else self.counter
        if moving is None:
            raise ValidationFailed("HOTP требует counter")
        try:
            encoded = self.secret.get_secret_value().upper()
            key = base64.b32decode(encoded + "=" * (-len(encoded) % 8), casefold=True)
        except ValueError:
            raise ValidationFailed("OTP secret не является base32") from None
        digest = hmac.new(key, struct.pack(">Q", moving), getattr(hashlib, self.algorithm.lower())).digest()
        offset = digest[-1] & 0x0F
        binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
        return str(binary % (10**self.digits)).zfill(self.digits)


class RecoveryCodes(BaseModel):
    codes: list[SecretStr]
    used: frozenset[int] = frozenset()

    def take(self) -> tuple[str, Self]:
        for index, code in enumerate(self.codes):
            if index not in self.used:
                return code.get_secret_value(), self.model_copy(
                    update={"used": frozenset((*self.used, index))}
                )
        raise ValidationFailed("коды восстановления закончились")


class ApiKey(BaseModel):
    key: SecretStr
    secret: SecretStr | None = None


class SigningKey(BaseModel):
    key_id: str
    secret: SecretStr
    algorithm: Literal["HMAC-SHA256", "HMAC-SHA512"] = "HMAC-SHA256"


class OAuthClient(BaseModel):
    client_id: str
    client_secret: SecretStr | None = None
    token_url: str | None = None
    authorize_url: str | None = None
    scopes: list[str] = Field(default_factory=list)
    redirect_uri: str | None = None


class OAuth1Client(BaseModel):
    consumer_key: str
    consumer_secret: SecretStr


class PrivateKeyCredentials(BaseModel):
    key_id: str | None = None
    private_key: SecretStr
    algorithm: Literal["RS256", "ES256", "EdDSA"] = "RS256"
    issuer: str | None = None


class ClientCertificate(BaseModel):
    cert: str
    key: SecretStr
    passphrase: SecretStr | None = None


class Screen(BaseModel):
    width: int
    height: int
    avail_width: int | None = None
    avail_height: int | None = None
    color_depth: int | None = None
    device_pixel_ratio: float | None = None


class Viewport(BaseModel):
    width: int
    height: int
    device_scale_factor: float | None = None
    is_mobile: bool | None = None
    has_touch: bool | None = None


class WebGl(BaseModel):
    vendor: str | None = None
    renderer: str | None = None


class Geolocation(BaseModel):
    latitude: float
    longitude: float
    accuracy: float | None = None


class UserAgentData(BaseModel):
    brands: list[dict[str, str]] = Field(default_factory=list)
    full_version_list: list[dict[str, str]] = Field(default_factory=list)
    mobile: bool | None = None
    platform: str | None = None
    platform_version: str | None = None
    architecture: str | None = None
    bitness: str | None = None
    model: str | None = None


class BrowserFingerprint(BaseModel):
    user_agent: str
    browser: Literal["chrome", "firefox", "safari", "edge"]
    browser_version: str
    os: Literal["windows", "macos", "linux", "android", "ios"]
    os_version: str | None = None
    device: Literal["desktop", "mobile", "tablet"] = "desktop"
    platform: str | None = None
    user_agent_data: UserAgentData | None = None
    locale: str | None = None
    languages: list[str] = Field(default_factory=list)
    timezone: str | None = None
    geolocation: Geolocation | None = None
    screen: Screen | None = None
    viewport: Viewport | None = None
    hardware_concurrency: int | None = None
    device_memory: float | None = None
    max_touch_points: int | None = None
    webgl: WebGl | None = None
    fonts: list[str] = Field(default_factory=list)
    webrtc: Literal["real", "proxy", "disabled"] | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    source: Literal["browserforge", "fingerprint-suite", "manual"] | None = None
    raw: dict[str, Any] | None = None
