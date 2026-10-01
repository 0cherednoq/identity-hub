"""Очистка свободного текста от секретов (S-8, F1.4).

Свободный текст приходит извне: сообщения исключений, `raw_status` и `code` из классификатора
ошибок и проб, `hint` запроса challenge, поля логов. В нём встречаются URL с ключами, URL прокси
с паролем и заголовки авторизации. Такой текст сохраняется в истории аренды, событиях, вебхуках
и карточке аккаунта, поэтому перед записью проходит `sanitize_text`.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "REDACTED",
    "TEXT_LIMIT",
    "has_inline_secrets",
    "is_secret_key",
    "redact_inline_secrets",
    "sanitize_text",
]

REDACTED: Final = "[redacted]"
TEXT_LIMIT: Final = 256

# scheme://user:pass@host → scheme://[redacted]@host
_USERINFO = re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^/\s@'\"]+@", re.I)
# query-строка URL или пути: в ней живут api_key, token, подписи
_QUERY = re.compile(r"((?:\b[a-z][a-z0-9+.\-]*://|/)[^\s?'\"]*)\?[^\s'\"]+", re.I)
_AUTH_SCHEME = re.compile(r"\b(Bearer|Basic|Bot)\s+[A-Za-z0-9._~+/=:\-]{4,}", re.I)
_ASSIGNMENT = re.compile(
    r"\b(pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|auth[_-]?key|access[_-]?key|private[_-]?key"
    r"|session(?:[_-]?(?:id|key))?|cookie|authorization|otp|jwt)\b(\s*[=:]\s*)(?!\[redacted\])[^\s,;&'\"]+",
    re.I,
)

_SECRET_WORDS: Final = frozenset(
    {
        "apikey",
        "authkey",
        "authorization",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "evidence",
        "jwt",
        "otp",
        "passphrase",
        "passwd",
        "password",
        "passwords",
        "proxy",
        "pwd",
        "secret",
        "secrets",
        "seed",
        "sid",
        "token",
        "tokens",
        "totp",
    }
)
# составные имена: последний сегмент сам по себе не секретен (`key`, `url`)
_SECRET_COMPOUNDS: Final = frozenset(
    {
        ("access", "key"),
        ("api", "key"),
        ("auth", "key"),
        ("private", "key"),
        ("proxy", "url"),
        ("secret", "key"),
        ("session", "key"),
        ("signing", "key"),
    }
)
_SEGMENT = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def is_secret_key(name: str) -> bool:
    """Имя поля называет секрет: главное слово (последний сегмент) или составное имя из списка.

    `bot_token`, `AuthKey`, `api_key` — секреты; `tokens_used`, `password_changed_at`, `proxy_id` — нет:
    у них главное слово не секретное.
    """
    segments = [segment.lower() for segment in _SEGMENT.findall(name)]
    if not segments:
        return False
    if segments[-1] in _SECRET_WORDS or "".join(segments) in _SECRET_WORDS:
        return True
    return len(segments) >= 2 and (segments[-2], segments[-1]) in _SECRET_COMPOUNDS


def redact_inline_secrets(text: str) -> str:
    """Вырезать из строки учётные данные URL, query-строки, `Bearer …` и `password=…`; длина не меняется."""
    text = _USERINFO.sub(rf"\1{REDACTED}@", text)
    text = _QUERY.sub(rf"\1?{REDACTED}", text)
    text = _AUTH_SCHEME.sub(rf"\1 {REDACTED}", text)
    return _ASSIGNMENT.sub(rf"\1\2{REDACTED}", text)


def has_inline_secrets(text: str) -> bool:
    return redact_inline_secrets(text) != text


def sanitize_text(value: str | None, *, limit: int = TEXT_LIMIT) -> str | None:
    """Текст для хранения и событий: без встроенных секретов и не длиннее `limit` символов."""
    if value is None:
        return None
    return redact_inline_secrets(str(value)[: limit * 4])[:limit]
