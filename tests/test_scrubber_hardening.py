"""Hardening coverage for scrub_credentials: Google session cookies (incl. __Secure- variants)
and JWTs, without regressing the existing bearer/API-key/OAuth/batchexecute patterns."""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "io.github.dlansama.tallybar", "contents", "code"))

from http_helpers import scrub_credentials  # noqa: E402


# --- new: Google browser session cookies ---

def test_scrub_redacts_secure_1psid_cookie_header():
    out = scrub_credentials("Cookie: __Secure-1PSID=g.a000secret; SID=g.a000other")
    assert "g.a000secret" not in out
    assert "g.a000other" not in out
    assert "__Secure-[REDACTED]" in out
    assert "SID=[REDACTED]" in out


def test_scrub_cookie_semicolon_delimiter_survives():
    # The ';' delimiter must NOT be swallowed by the cookie value class — each cookie in a
    # multi-cookie header is redacted independently and the separators are preserved.
    out = scrub_credentials("__Secure-1PSID=x; SID=y")
    assert out == "__Secure-[REDACTED]; SID=[REDACTED]"
    assert "; " in out


def test_scrub_redacts_secure_variants_case_insensitive():
    for name in ("__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID", "__Secure-OSID"):
        out = scrub_credentials(f"{name}=topsecretvalue123")
        assert "topsecretvalue123" not in out
        assert "[REDACTED]" in out
    # mixed case name still hit
    out = scrub_credentials("__secure-1psid=lowercasesecret")
    assert "lowercasesecret" not in out


def test_scrub_redacts_bare_google_cookies_standalone_and_mixed_case():
    for name in ("SID", "HSID", "SSID", "APISID", "SAPISID", "OSID"):
        out = scrub_credentials(f"{name}=mysecretcookieval")
        assert "mysecretcookieval" not in out, name
        assert f"{name}=[REDACTED]" == out.strip()
    out = scrub_credentials("sapisid=mixedcasesecret")
    assert "mixedcasesecret" not in out


def test_scrub_cookies_leave_benign_sid_words_untouched():
    for benign in ("inside=5", "consider the options", "reside=here", "besides that"):
        assert scrub_credentials(benign) == benign


# --- new: JWTs ---

def test_scrub_redacts_jwt_standalone():
    jwt = ("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
           "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
    out = scrub_credentials(f"id_token {jwt} rejected")
    assert jwt not in out
    assert "[REDACTED-JWT]" in out


def test_scrub_redacts_jwt_in_authorization_header_form():
    jwt = "eyJabc_def-123.eyJpayload_x.sigpart-99"
    out = scrub_credentials(f"Cookie: token={jwt}")
    assert jwt not in out
    assert "[REDACTED-JWT]" in out


# --- regression: existing patterns still fire ---

def test_scrub_bearer_still_redacted():
    assert scrub_credentials("Authorization: Bearer abc123_DEF-xyz.7 x") == \
        "Authorization: Bearer [REDACTED] x"


def test_scrub_ya29_still_redacted():
    out = scrub_credentials("token ya29.aBc_dEf-123 bad")
    assert "aBc_dEf-123" not in out
    assert "ya29.[REDACTED]" in out


def test_scrub_sk_and_gocspx_still_redacted():
    out = scrub_credentials("sk-ABCdef_012-9 and GOCSPX-secret_val-1")
    assert "ABCdef_012-9" not in out
    assert "GOCSPX-[REDACTED]" in out


def test_scrub_refresh_token_1slashslash_still_redacted():
    out = scrub_credentials("1//0longrefreshvalue_here-abcdef")
    assert "0longrefreshvalue_here-abcdef" not in out
    assert "1//[REDACTED]" in out


def test_scrub_aiza_still_redacted():
    key = "AIza" + "B" * 35
    out = scrub_credentials(f"key {key} invalid")
    assert key not in out
    assert "AIza[REDACTED]" in out


def test_scrub_session_key_still_redacted():
    out = scrub_credentials("Cookie: sessionKey=sk-ant-sid01-AbC_dEf-123 dropped")
    assert "sk-ant-sid01-AbC_dEf-123" not in out
    assert "sessionKey=[REDACTED]" in out


def test_scrub_csrf_token_both_forms_still_redacted():
    assert scrub_credentials("agy --csrf_token=abc123_DEF-xyz spawned") == \
        "agy --csrf_token=[REDACTED] spawned"
    assert scrub_credentials('{"csrf_token": "abc123DEF"}') == \
        '{"csrf_token=[REDACTED]"}'


def test_scrub_oauth_body_tokens_still_redacted():
    assert scrub_credentials("access_token=ya29xyz_abc-1&refresh_token=1//longvalue_here") == \
        "access_token=[REDACTED]&refresh_token=[REDACTED]"


def test_scrub_batchexecute_tokens_still_redacted():
    out = scrub_credentials("at=SNlM0e_secret&f.sid=FdrFJe_x&sid=abc_1")
    assert "SNlM0e_secret" not in out
    assert "FdrFJe_x" not in out
    assert "at=[REDACTED]" in out
    assert "f.sid=[REDACTED]" in out


# --- identity / passthrough ---

def test_scrub_no_secret_string_unchanged():
    s = "connection refused on 127.0.0.1 (HTTP 403 from claude.ai)"
    assert scrub_credentials(s) == s


def test_scrub_non_string_passthrough():
    assert scrub_credentials(None) is None
    assert scrub_credentials(42) == 42
