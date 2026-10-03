import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, Settings, parse_tokens
from app.main import create_app

from .conftest import TOKEN, URL, make_text_pdf, post_pdf


def test_healthz_needs_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_missing_token(client):
    r = post_pdf(client, make_text_pdf(), headers={})
    assert r.status_code == 401
    assert r.json()["code"] == "MISSING_TOKEN"
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong-token-0123456789abcdef",
        "Basic " + TOKEN,
        "Bearer",
        "Bearer ",
        TOKEN,
        f"Bearer  {TOKEN}",
        f"Bearer {TOKEN} extra",
        f"Bearer {TOKEN[:-1]}",
    ],
)
def test_invalid_or_malformed_token(client, header):
    r = post_pdf(client, make_text_pdf(), headers={"Authorization": header})
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_TOKEN"
    assert r.headers["www-authenticate"] == "Bearer"


def test_duplicate_authorization_headers_rejected(client):
    r = client.post(
        URL,
        files={"file": ("a.pdf", make_text_pdf(), "application/pdf")},
        headers=[("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")],
    )
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_TOKEN"


def test_auth_failure_does_not_leak_details(client):
    r = post_pdf(client, make_text_pdf(), headers={"Authorization": "Bearer nope-nope-nope-nope-nope"})
    body = r.text
    assert TOKEN not in body
    assert "nope-nope" not in body
    assert set(r.json()) == {"detail", "code"}


def test_token_in_query_string_is_not_accepted(client):
    r = client.post(URL + f"?token={TOKEN}", files={"file": ("a.pdf", make_text_pdf(), "application/pdf")})
    assert r.status_code == 401


def test_auth_checked_before_body_is_processed(client):
    r = client.post(URL, content=b"not multipart at all", headers={"Content-Type": "text/plain"})
    assert r.status_code == 401
    assert r.json()["code"] == "MISSING_TOKEN"


def test_multiple_tokens_all_valid_for_rotation():
    old, new = "old-token-0123456789abcdef", "new-token-0123456789abcdef"
    settings = Settings(api_tokens=(old, new))
    with TestClient(create_app(settings)) as c:
        for token in (old, new):
            assert post_pdf(c, make_text_pdf(), params="?ocr=never", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_unknown_route_and_method_use_error_contract(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    r = client.get(URL, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 405 and r.json()["code"] == "METHOD_NOT_ALLOWED"


def test_parse_tokens_formats():
    assert parse_tokens("a-token-0123456789, b-token-0123456789 ,") == ("a-token-0123456789", "b-token-0123456789")
    assert parse_tokens('["x-token-0123456789abc","y-token-0123456789abc"]') == ("x-token-0123456789abc", "y-token-0123456789abc")
    with pytest.raises(ConfigError):
        parse_tokens("[not json")


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"API_TOKENS": "short"},
        {"API_TOKENS": TOKEN, "CONVERSION_TIMEOUT_SECONDS": "0"},
        {"API_TOKENS": TOKEN, "MAX_PAGES": "-1"},
        {"API_TOKENS": TOKEN, "MAX_PAGES": "abc"},
        {"API_TOKENS": TOKEN, "OCR_LANGUAGE": "english; rm -rf /"},
    ],
)
def test_startup_config_validation(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_config_defaults():
    s = Settings.from_env({"API_TOKENS": TOKEN})
    assert (s.conversion_timeout_seconds, s.max_pages, s.ocr_language, s.max_upload_bytes) == (300, 200, "eng", 10 * 1024 * 1024)
