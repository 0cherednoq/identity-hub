from __future__ import annotations

import pytest

from identity_hub import Service


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("privy.io/path?x=1#fragment", "https://privy.io"),
        ("HTTPS://Пример.РФ:443/path", "https://xn--e1afmkfd.xn--p1ai"),
        ("http://1.2.3.4:8000/path", "http://1.2.3.4:8000"),
        ("http://[2001:db8::1]:80/path", "http://[2001:db8::1]"),
    ],
)
def test_service_url_is_normalized_to_origin(raw: str, normalized: str) -> None:
    assert Service("web", "Web", url=raw).url == normalized


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "https://user:password@example.com",
        "http://01.2.3.4",
        "https://bad_host.example",
        "https://example.com:99999",
        "",
    ],
)
def test_invalid_service_url_is_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="url"):
        Service("web", "Web", url=url)


def test_service_url_is_optional() -> None:
    assert Service("non_web", "Non-web").url is None
