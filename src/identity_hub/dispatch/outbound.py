"""Исходящие HTTP-запросы библиотеки: защита от SSRF и секреты в логах httpx (S-9, F1.5).

Адрес вебхука задаёт оператор пространства, а запрос уходит из сети приложения. Поэтому:

- хост проверяется во всех формах, которые резолвер понимает как IPv4 (`2130706433`, `0x7f000001`,
  `127.1`, `0177.0.0.1`), а имя резолвится, и каждый полученный адрес обязан быть глобальным;
- соединение идёт с уже проверенным адресом (`PinnedBackend`): повторный резолв в момент
  соединения (DNS rebinding) невозможен. SNI и `Host` остаются исходным именем;
- клиенты создаются с `trust_env=False`: `HTTP_PROXY`/`ALL_PROXY` окружения не подхватываются;
- URL с секретом (путь и query вебхука, токен бота Telegram) маскируется в логах httpx.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from identity_hub.errors import ValidationFailed

if TYPE_CHECKING:
    import httpx

__all__ = [
    "ForbiddenTarget",
    "PinnedBackend",
    "Resolver",
    "create_client",
    "host_address",
    "install_log_filter",
    "is_public_address",
    "mask_url",
    "masked_in_logs",
    "system_resolver",
    "url_log_masks",
]

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


class ForbiddenTarget(ValidationFailed):
    """Адрес назначения исходящего запроса — приватный, служебный или не резолвится."""


def _legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Формы `inet_aton`: 1–4 части в десятичной, восьмеричной (`0177`) или шестнадцатеричной записи."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    numbers: list[int] = []
    for part in parts:
        try:
            if part[:2].lower() == "0x":
                numbers.append(int(part[2:] or "0", 16))
            elif len(part) > 1 and part[0] == "0":
                numbers.append(int(part, 8))
            elif part.isascii() and part.isdigit():
                numbers.append(int(part))
            else:
                return None
        except ValueError:
            return None
    *head, tail = numbers
    if any(number > 0xFF for number in head) or tail >= 256 ** (4 - len(head)):
        return None
    value = tail
    for index, number in enumerate(head):
        value |= number << (8 * (3 - index))
    return ipaddress.IPv4Address(value)


def host_address(host: str) -> IPAddress | None:
    """IP-адрес, если хост — литерал в любой форме, понятной резолверу; иначе `None` (это имя)."""
    candidate = host.strip("[]")
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return _legacy_ipv4(candidate)


def is_public_address(address: IPAddress) -> bool:
    """Глобально маршрутизируемый адрес; IPv4-mapped IPv6 проверяется по вложенному IPv4."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return is_public_address(address.ipv4_mapped)
    return address.is_global and not address.is_multicast


async def system_resolver(host: str, port: int) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


# ---------------------------------------------------------------- секреты в логах httpx

_masked: ContextVar[tuple[tuple[str, str], ...]] = ContextVar("identity_hub_masked_in_logs", default=())
_HTTP_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)


def mask_url(url: str) -> str:
    """`scheme://host[:port]/***` — путь и query вебхука могут содержать секрет получателя."""
    parsed = urlsplit(url)
    host = parsed.hostname or "unknown"
    if ":" in host:
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{host}{port}/***"


def url_log_masks(url: str) -> tuple[tuple[str, str], ...]:
    """Пары для `masked_in_logs`: путь и query URL заменяются на `/***`."""
    parsed = urlsplit(url)
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return ((target, "/***"),) if target not in ("", "/") else ()


@contextmanager
def masked_in_logs(*pairs: tuple[str, str]) -> Iterator[None]:
    """На время запроса заменять подстроки (`секрет → замена`) в записях логгеров httpx и httpcore."""
    token = _masked.set((*_masked.get(), *((needle, to) for needle, to in pairs if needle)))
    try:
        yield
    finally:
        _masked.reset(token)


class _MaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        pairs = _masked.get()
        if pairs:
            try:
                message = record.getMessage()
            except Exception:
                return True
            clean = message
            for needle, replacement in pairs:
                clean = clean.replace(needle, replacement)
            if clean != message:
                record.msg, record.args = clean, ()
        return True


_FILTER = _MaskingFilter()


def install_log_filter() -> None:
    """Повесить фильтр на логгеры httpx/httpcore (идемпотентно). Действует только внутри `masked_in_logs`."""
    for name in _HTTP_LOGGERS:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)


# ---------------------------------------------------------------- закрепление адреса


class PinnedBackend:
    """Сетевой бэкенд httpcore: резолвит имя сам, проверяет каждый адрес и соединяется с проверенным IP.

    URL запроса, заголовок `Host` и SNI не меняются — подменяется только адрес TCP-соединения, поэтому
    повторный резолв в момент соединения (DNS rebinding) невозможен. `allow_hosts` — dev-хосты
    (`IdentityHub(webhook_dev_hosts=...)`): для них проверки нет.
    """

    def __init__(
        self, inner: Any, *, resolver: Resolver = system_resolver, allow_hosts: Iterable[str] = ()
    ) -> None:
        self._inner = inner
        self._resolver = resolver
        self._allow = frozenset(host.casefold().rstrip(".") for host in allow_hosts)

    async def connect_tcp(self, host: str, port: int, **kwargs: Any) -> Any:
        if host.casefold().rstrip(".") in self._allow:
            return await self._inner.connect_tcp(host, port, **kwargs)
        addresses = await self.resolve(host, port)
        last: BaseException | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(address, port, **kwargs)
            except Exception as exc:  # следующий адрес того же имени
                last = exc
        assert last is not None
        raise last

    async def resolve(self, host: str, port: int) -> Sequence[str]:
        """Адреса для соединения; `ForbiddenTarget`, если хоть один из них не глобальный."""
        literal = host_address(host)
        if literal is not None:
            addresses: Sequence[str] = [str(literal)]
        else:
            try:
                addresses = await self._resolver(host, port)
            except OSError:
                addresses = []
        if not addresses:
            raise ForbiddenTarget("имя получателя не резолвится")
        for address in addresses:
            if not is_public_address(ipaddress.ip_address(address.split("%", 1)[0])):
                raise ForbiddenTarget("получатель резолвится в приватный или служебный адрес")
        return addresses

    async def connect_unix_socket(self, path: str, **kwargs: Any) -> Any:
        raise ForbiddenTarget("unix-сокеты для исходящих запросов запрещены")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def create_client(
    *,
    timeout: float,
    proxy: str | None = None,
    pin: bool = False,
    resolver: Resolver = system_resolver,
    allow_hosts: Iterable[str] = (),
) -> httpx.AsyncClient:
    """Клиент библиотеки: без переменных окружения, без редиректов, с маскированием секретов в логах.

    `pin=True` закрепляет проверенный адрес получателя. При явном `proxy` соединение идёт к прокси,
    а имя получателя резолвит он: закрепление не применяется, проверяются только литералы в URL.
    """
    import httpx

    install_log_filter()
    transport = httpx.AsyncHTTPTransport(trust_env=False, proxy=proxy)
    if pin and proxy is None:
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if pool is None or backend is None:  # устройство httpx изменилось: без закрепления не отправляем
            raise RuntimeError(
                "identity-hub не поддерживает эту версию httpx: нет сетевого бэкенда транспорта"
            )
        pool._network_backend = PinnedBackend(backend, resolver=resolver, allow_hosts=allow_hosts)
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False, transport=transport)
