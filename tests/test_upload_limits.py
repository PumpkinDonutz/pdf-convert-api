"""Every 4xx code in the error contract, plus cleanup on failure."""
import pytest
from fastapi.testclient import TestClient

from app.config import MIB, Settings
from app.main import create_app

from .conftest import (
    AUTH,
    TOKEN,
    URL,
    leftovers,
    make_encrypted_pdf,
    make_text_pdf,
    post_pdf,
)


def assert_error(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()
    assert body["code"] == code
    assert isinstance(body["detail"], str) and body["detail"]
    assert set(body) == {"detail", "code"}
    assert "/tmp" not in r.text and "Traceback" not in r.text


def test_missing_file_field(client, tmp_root):
    r = client.post(URL, files={"other": (None, "value")}, headers=AUTH)
    assert_error(r, 400, "MISSING_FILE_FIELD")
    assert not leftovers(tmp_root)


def test_non_multipart_body(client, tmp_root):
    r = client.post(URL, content=b"%PDF-1.4", headers={**AUTH, "Content-Type": "application/pdf"})
    assert_error(r, 400, "MISSING_FILE_FIELD")
    assert not leftovers(tmp_root)


def test_empty_file(client, tmp_root):
    r = post_pdf(client, b"")
    assert_error(r, 400, "EMPTY_FILE")
    assert not leftovers(tmp_root)


@pytest.mark.parametrize("value", ["bogus", "", "AUTO", "never,always"])
def test_invalid_ocr_param(client, value, tmp_root):
    r = post_pdf(client, make_text_pdf(), params=f"?ocr={value}")
    assert_error(r, 400, "INVALID_OCR_PARAM")
    assert not leftovers(tmp_root)


def test_duplicate_ocr_param_rejected(client):
    r = post_pdf(client, make_text_pdf(), params="?ocr=never&ocr=always")
    assert_error(r, 400, "INVALID_OCR_PARAM")


def test_file_too_large_default_limit(tmp_root):
    settings = Settings(api_tokens=(TOKEN,))
    assert settings.max_upload_bytes == 10 * MIB
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        r = post_pdf(c, b"%PDF-" + b"0" * (10 * MIB - 5 + 1))
        assert_error(r, 413, "FILE_TOO_LARGE")
        assert not leftovers(tmp_root)


def test_file_exactly_at_limit_is_not_rejected_for_size(tmp_root):
    settings = Settings(api_tokens=(TOKEN,))
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        r = post_pdf(c, b"%PDF-" + b"0" * (10 * MIB - 5))
        assert_error(r, 422, "MALFORMED_PDF")  # past the size gate; fails PDF parsing instead
        assert not leftovers(tmp_root)


def test_too_large_is_enforced_while_streaming_not_via_content_length(tmp_root):
    """A lying Content-Length must not bypass the cap."""
    settings = Settings(api_tokens=(TOKEN,), max_upload_bytes=50_000)
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        boundary = "xyzboundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.pdf\"\r\n"
            "Content-Type: application/pdf\r\n\r\n"
        ).encode() + b"%PDF-" + b"0" * 60_000 + f"\r\n--{boundary}--\r\n".encode()
        r = c.post(
            URL,
            content=body,
            headers={**AUTH, "Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": "100"},
        )
        assert r.status_code in (413, 400)  # server may reject the mismatch itself; never 200/5xx
        assert not leftovers(tmp_root)


def test_huge_non_file_field_is_capped(tmp_root):
    settings = Settings(api_tokens=(TOKEN,), max_upload_bytes=50_000)
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        r = c.post(
            URL,
            files={"junk": (None, "x" * 200_000), "file": ("a.pdf", make_text_pdf(), "application/pdf")},
            headers=AUTH,
        )
        assert_error(r, 413, "FILE_TOO_LARGE")
        assert not leftovers(tmp_root)


@pytest.mark.parametrize("data", [b"hello, definitely not a pdf", b"PK\x03\x04zipfile", b" %PDF-1.4"])
def test_not_a_pdf(client, data, tmp_root):
    r = post_pdf(client, data)
    assert_error(r, 415, "NOT_A_PDF")
    assert not leftovers(tmp_root)


def test_content_type_and_extension_are_not_trusted(client, tmp_root):
    r = client.post(URL, files={"file": ("real.pdf", b"not a pdf", "application/pdf")}, headers=AUTH)
    assert_error(r, 415, "NOT_A_PDF")


def test_malformed_pdf(client, tmp_root):
    r = post_pdf(client, b"%PDF-1.4\nthis is garbage, not a real pdf structure\n")
    assert_error(r, 422, "MALFORMED_PDF")
    assert not leftovers(tmp_root)


def test_truncated_pdf(client, tmp_root):
    r = post_pdf(client, make_text_pdf()[:300])
    assert r.status_code == 422 and r.json()["code"] in ("MALFORMED_PDF",)
    assert not leftovers(tmp_root)


def test_encrypted_pdf(client, tmp_root):
    r = post_pdf(client, make_encrypted_pdf())
    assert_error(r, 422, "ENCRYPTED_PDF")
    assert not leftovers(tmp_root)


def test_page_limit_exceeded_before_any_work(client, run_spy, tmp_root):
    r = post_pdf(client, make_text_pdf(pages=6))  # fixture limit is 5
    assert_error(r, 422, "PAGE_LIMIT_EXCEEDED")
    assert run_spy == []  # no OCR or conversion subprocess was started
    assert not leftovers(tmp_root)


def test_page_limit_boundary_allowed(client, tmp_root):
    r = post_pdf(client, make_text_pdf(pages=5), params="?ocr=never")
    assert r.status_code == 200


def test_unexpected_failure_maps_to_internal_error(client, monkeypatch, tmp_root):
    def boom(*a, **k):
        raise RuntimeError("secret internal detail /var/lib/x")

    monkeypatch.setattr("app.main.inspect_pdf", boom)
    r = post_pdf(client, make_text_pdf())
    assert_error(r, 500, "INTERNAL_ERROR")
    assert "secret" not in r.text
    assert not leftovers(tmp_root)
