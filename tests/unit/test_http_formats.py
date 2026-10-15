"""S6.3: HTTP cookie and proxy adapters."""

from __future__ import annotations

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request

import pytest

from identity_hub import Cookie, Cookies, FormatLossWarning, ProxyUrl, SecretValue, ValidationFailed

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "formats"


def test_cookiejar_round_trip_preserves_scope_and_curl_http_only_string() -> None:
    source = Cookies(
        [
            Cookie(
                name="sid",
                value="secret",
                domain="example.test",
                host_only=True,
                path="/account",
                expires=2_000_000_000,
                secure=True,
                http_only=True,
                same_site="lax",
            )
        ]
    )
    jar = source.dump.cookiejar()
    assert Cookies.load.cookiejar(jar) == source

    item = next(iter(jar))
    item._rest = {"http_only": "False"}  # curl_cffi response dialect
    loaded = Cookies.load.cookiejar(jar).root[0]
    assert loaded.http_only is False


def test_cookiejar_host_only_and_paths_do_not_leak() -> None:
    cookies = Cookies(
        [
            Cookie(name="sid", value="root", domain="example.test", host_only=True),
            Cookie(name="sid", value="deep", domain="example.test", host_only=True, path="/deep"),
        ]
    )
    assert cookies.header_for("https://example.test/deep/page") == "sid=deep; sid=root"
    assert cookies.header_for("https://sub.example.test/deep/page") == ""

    jar = cookies.dump.cookiejar()
    assert len(Cookies.load.cookiejar(jar)) == 2
    request = Request("http://sub.example.test/deep/page")
    jar.add_cookie_header(request)
    assert request.get_header("Cookie") is None


def test_set_cookie_fixture_parses_default_scope_and_expiry() -> None:
    headers = (FIXTURES / "set_cookie" / "headers.txt").read_text(encoding="utf-8").splitlines()
    cookies = Cookies.load.set_cookie(
        headers,
        url="https://example.test/account/login",
        received_at=1_700_000_000,
    )
    sid = cookies.get("sid")
    theme = cookies.get("theme")
    assert sid is not None and sid.host_only and sid.path == "/account"
    assert sid.expires == 1_700_003_600 and sid.http_only and sid.secure
    assert theme is not None and not theme.host_only

    default_path = Cookies.load.set_cookie(
        "draft=x", url="https://example.test/account/login", received_at=1_700_000_000
    ).get("draft")
    assert default_path is not None and default_path.path == "/account"

    deleted = Cookies.load.set_cookie("sid=gone; Max-Age=0", url="https://example.test/")
    assert len(deleted) == 0
    with pytest.raises(ValidationFailed, match="не соответствует"):
        Cookies.load.set_cookie("sid=x; Domain=other.test", url="https://example.test/")


def test_aiohttp_and_scrapy_shapes_and_losses() -> None:
    ordinary = Cookie(name="sid", value="secret", domain="example.test", host_only=True, path="/x")
    [(morsels, response_url)] = Cookies([ordinary]).dump.aiohttp()
    assert morsels["sid"].value == "secret"
    assert response_url == "http://example.test/x"

    rich = ordinary.model_copy(update={"http_only": True})
    with pytest.warns(FormatLossWarning, match="Scrapy"):
        assert Cookies([rich]).dump.scrapy()[0]["name"] == "sid"
    with pytest.raises(ValidationFailed, match="Scrapy"):
        Cookies([rich]).dump.scrapy(strict=True)


def test_proxy_url_is_masked_and_has_client_shapes() -> None:
    proxy = ProxyUrl("socks5://us%40er:p%3Ass@proxy.example:1080")
    assert isinstance(proxy, SecretValue)
    assert "p%3Ass" not in repr(proxy) and "p%3Ass" not in str(proxy)
    assert proxy.dump.playwright() == {
        "server": "socks5://proxy.example:1080",
        "username": "us@er",
        "password": "p:ss",
    }
    assert proxy.dump.httpx() == "socks5://us%40er:p%3Ass@proxy.example:1080"
    assert proxy.dump.requests() == {
        "http": proxy.dump.httpx(),
        "https": proxy.dump.httpx(),
    }
    if importlib.util.find_spec("aiohttp") is None:
        with pytest.raises(RuntimeError, match="aiohttp"):
            proxy.dump.aiohttp()


class _CookieEcho(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        payload = self.headers.get("Cookie", "").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: object) -> None:
        del args


@pytest.fixture
def cookie_server() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CookieEcho)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_installed_http_clients_respect_cookie_path(cookie_server: str) -> None:
    cookies = Cookies(
        [Cookie(name="sid", value="secret", domain="127.0.0.1", host_only=True, path="/allowed")]
    )

    import httpx
    import requests

    with httpx.Client(cookies=cookies.dump.cookiejar(), trust_env=False) as client:
        assert client.get(f"{cookie_server}/allowed/page").text == "sid=secret"
        assert client.get(f"{cookie_server}/other").text == ""

    session = requests.Session()
    session.trust_env = False
    session.cookies.update(cookies.dump.cookiejar())
    try:
        assert session.get(f"{cookie_server}/allowed/page", timeout=2).text == "sid=secret"
        assert session.get(f"{cookie_server}/other", timeout=2).text == ""
    finally:
        session.close()


async def test_optional_aiohttp_respects_cookie_path(cookie_server: str) -> None:
    aiohttp = pytest.importorskip("aiohttp")
    yarl = pytest.importorskip("yarl")
    cookies = Cookies(
        [Cookie(name="sid", value="secret", domain="127.0.0.1", host_only=True, path="/allowed")]
    )
    jar = aiohttp.CookieJar(unsafe=True)
    for morsels, response_url in cookies.dump.aiohttp():
        jar.update_cookies(morsels, response_url=yarl.URL(response_url))
    async with aiohttp.ClientSession(cookie_jar=jar) as client:
        async with client.get(f"{cookie_server}/allowed/page") as response:
            assert await response.text() == "sid=secret"
        async with client.get(f"{cookie_server}/other") as response:
            assert await response.text() == ""


def test_optional_curl_cffi_respects_cookie_path(cookie_server: str) -> None:
    curl_requests = pytest.importorskip("curl_cffi.requests")
    cookies = Cookies(
        [Cookie(name="sid", value="secret", domain="127.0.0.1", host_only=True, path="/allowed")]
    )
    with curl_requests.Session(cookies=cookies.dump.cookiejar()) as session:
        assert session.get(f"{cookie_server}/allowed/page", timeout=2).text == "sid=secret"
        assert session.get(f"{cookie_server}/other", timeout=2).text == ""
