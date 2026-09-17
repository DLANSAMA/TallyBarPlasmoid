"""http_helpers credential redaction. scrub_credentials runs on the error strings that
land in provider 'message' fields and are persisted to ~/.tallybar/last_snapshot.json, so
a regex regression that stopped matching would write a raw token to disk."""

import sys
from pathlib import Path

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

from http_helpers import scrub_credentials  # noqa: E402


def test_scrub_redacts_bearer_token():
    assert scrub_credentials("Authorization: Bearer abc123_DEF-xyz.7 failed") == \
        "Authorization: Bearer [REDACTED] failed"


def test_scrub_redacts_sk_api_key():
    assert scrub_credentials("openai key sk-ABCdef_012-9 rejected") == \
        "openai key sk-[REDACTED] rejected"


def test_scrub_passes_through_benign_text():
    assert scrub_credentials("connection refused on 127.0.0.1") == \
        "connection refused on 127.0.0.1"


def test_scrub_benign_http_status_unchanged():
    # A plain error string with no credential material must pass through verbatim.
    assert scrub_credentials("HTTP 403 from claude.ai") == "HTTP 403 from claude.ai"


def test_scrub_redacts_claude_session_key():
    out = scrub_credentials("Cookie: sessionKey=sk-ant-sid01-AbC_dEf-123 dropped")
    assert out == "Cookie: sessionKey=[REDACTED] dropped"
    assert "sk-ant-sid01" not in out


def test_scrub_redacts_csrf_token():
    assert scrub_credentials("agy --csrf_token=abc123_DEF-xyz spawned") == \
        "agy --csrf_token=[REDACTED] spawned"
    # colon / quoted forms
    assert scrub_credentials('{"csrf_token": "abc123DEF"}') == \
        '{"csrf_token=[REDACTED]"}'


def test_scrub_redacts_google_api_key():
    key = "AIza" + "B" * 35
    out = scrub_credentials(f"key {key} invalid")
    assert out == "key AIza[REDACTED] invalid"
    assert key not in out


def test_scrub_redacts_oauth_body_tokens():
    assert scrub_credentials("access_token=ya29xyz_abc-1&refresh_token=1//longvalue_here") == \
        "access_token=[REDACTED]&refresh_token=[REDACTED]"


def test_scrub_non_string_passthrough():
    assert scrub_credentials(None) is None
    assert scrub_credentials(42) == 42


# --- http_json / http_text transport branches ------------------------------
# Every provider funnels through these two functions, yet providers mock the higher-level
# http_*_async / http_text, leaving the real body-cap and HTTPError paths untested. These
# drive them directly with a fake opener.

import io  # noqa: E402
import urllib.error  # noqa: E402
from http.cookiejar import CookieJar  # noqa: E402
from unittest.mock import patch  # noqa: E402

import http_helpers  # noqa: E402


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._buf = io.BytesIO(body)
        self.read_caps = []

    def read(self, n=-1):
        self.read_caps.append(n)
        return self._buf.read() if n is None or n < 0 else self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    def open(self, request, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._resp


def _http_error(code, body):
    return urllib.error.HTTPError("http://x", code, "Boom", {}, io.BytesIO(body))


def test_http_json_success_and_body_cap():
    resp = _FakeResp(200, b'{"a": 1}')
    with patch.object(http_helpers.urllib.request, "build_opener", return_value=_FakeOpener(resp)):
        status, data = http_helpers.http_json("https://x", CookieJar(), 1.0)
    assert status == 200 and data == {"a": 1}
    assert resp.read_caps == [http_helpers.MAX_JSON_BODY_BYTES]  # read is bounded, not unbounded


def test_http_json_non_json_success_falls_back_to_text():
    resp = _FakeResp(200, b"<html>not json</html>")
    with patch.object(http_helpers.urllib.request, "build_opener", return_value=_FakeOpener(resp)):
        status, data = http_helpers.http_json("https://x", CookieJar(), 1.0)
    assert status == 200 and data["text"].startswith("<html>")


def test_http_json_http_error_with_json_body():
    err = _http_error(429, b'{"error": "rate limited"}')
    with patch.object(http_helpers.urllib.request, "build_opener", return_value=_FakeOpener(exc=err)):
        status, data = http_helpers.http_json("https://x", CookieJar(), 1.0)
    assert status == 429 and data == {"error": "rate limited"}


def test_http_json_http_error_non_json_body_uses_reason():
    err = _http_error(500, b"Internal Server Error")
    with patch.object(http_helpers.urllib.request, "build_opener", return_value=_FakeOpener(exc=err)):
        status, data = http_helpers.http_json("https://x", CookieJar(), 1.0)
    assert status == 500 and data == {"error": "Boom"}


def test_http_text_success_and_error_caps():
    resp = _FakeResp(200, b"hello")
    with patch.object(http_helpers.urllib.request, "build_opener", return_value=_FakeOpener(resp)):
        status, text = http_helpers.http_text("https://x", CookieJar(), 1.0)
    assert status == 200 and text == "hello"
    assert resp.read_caps == [http_helpers.MAX_TEXT_BODY_BYTES]

    err = _http_error(403, b"forbidden")
    with patch.object(http_helpers.urllib.request, "build_opener", return_value=_FakeOpener(exc=err)):
        status, text = http_helpers.http_text("https://x", CookieJar(), 1.0)
    assert status == 403 and text == "forbidden"


# --- bounded_provider fallback shapes --------------------------------------

import asyncio  # noqa: E402
import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_bounded_provider_timeout_shape():
    async def _slow():
        await asyncio.sleep(10)
    out = await http_helpers.bounded_provider(_slow(), timeout=0.01, fallback={"label": "Gemini"})
    assert out["status"] == "timeout" and "Gemini" in out["message"]


@pytest.mark.asyncio
async def test_bounded_provider_error_shape_is_redacted():
    async def _boom():
        raise RuntimeError("token Bearer abc123DEF leaked")
    out = await http_helpers.bounded_provider(_boom(), timeout=1.0, fallback={"label": "Codex"})
    assert out["status"] == "api-error"
    assert "Bearer [REDACTED]" in out["message"] and "abc123DEF" not in out["message"]


# --- scheme gate -----------------------------------------------------------------
# http_json/http_text are the CREDENTIALED path: every caller passes a CookieJar of real
# browser session cookies, and urllib's default opener follows redirects. _require_https
# keeps a file://, ftp:// or plaintext http:// URL from ever reaching that opener.

import pytest  # noqa: E402

from http_helpers import _require_https, http_json, http_text  # noqa: E402


@pytest.mark.parametrize("url", [
    "http://claude.ai/api/organizations",        # plaintext downgrade
    "file:///etc/passwd",                        # urllib would happily open this
    "ftp://example.com/x",
    "HTTP://claude.ai/x",                        # case-insensitive check
    "",
    None,
])
def test_require_https_rejects_non_https(url):
    with pytest.raises(ValueError):
        _require_https(url)


@pytest.mark.parametrize("url", [
    "https://claude.ai/api/organizations",
    "HTTPS://gemini.google.com/usage",           # scheme compare is lowercased
])
def test_require_https_accepts_https(url):
    _require_https(url)  # must not raise


def test_http_helpers_reject_non_https_before_opening(monkeypatch):
    """The gate must fire BEFORE any opener is built — no socket, no cookie on the wire."""
    def _explode(*a, **k):
        raise AssertionError("build_opener must not be reached for a non-https URL")

    import urllib.request
    monkeypatch.setattr(urllib.request, "build_opener", _explode)

    from http.cookiejar import CookieJar
    for fn in (http_json, http_text):
        with pytest.raises(ValueError):
            fn("http://claude.ai/x", CookieJar(), 1.0)
