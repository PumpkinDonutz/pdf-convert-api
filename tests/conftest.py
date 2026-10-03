"""Shared fixtures. All PDFs are generated in-process; no real documents are used."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app import conversion
from app.config import Settings
from app.main import create_app

TOKEN = "test-token-0123456789abcdef"
URL = "/v1/convert/pdf/to/docx"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_text_pdf(pages: int = 1, text: str = "Quarterly invoice summary") -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"{text} page {i + 1}", fontsize=20)
        page.insert_text((72, 140), "The quick brown fox jumps over the lazy dog.", fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


def make_scanned_pdf(pages: int = 1) -> bytes:
    """A PDF whose pages are pure images (no text layer), like a scan."""
    source = pymupdf.open(stream=make_text_pdf(pages, "Scanned receipt total"), filetype="pdf")
    out = pymupdf.open()
    for page in source:
        pix = page.get_pixmap(dpi=200)
        new_page = out.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=pix.tobytes("jpeg", jpg_quality=80))
    data = out.tobytes()
    out.close()
    source.close()
    return data


def make_mixed_pdf() -> bytes:
    text_doc = pymupdf.open(stream=make_text_pdf(1), filetype="pdf")
    scan_doc = pymupdf.open(stream=make_scanned_pdf(1), filetype="pdf")
    text_doc.insert_pdf(scan_doc)
    data = text_doc.tobytes()
    text_doc.close()
    scan_doc.close()
    return data


def make_encrypted_pdf() -> bytes:
    doc = pymupdf.open(stream=make_text_pdf(1), filetype="pdf")
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    doc.close()
    return data


@pytest.fixture
def settings() -> Settings:
    return Settings(api_tokens=(TOKEN,), conversion_timeout_seconds=120, max_pages=5, queue_size=2)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def tmp_root(tmp_path, monkeypatch) -> Path:
    """Redirect request work directories so tests can assert nothing is left behind."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


@pytest.fixture
def run_spy(monkeypatch):
    """Record every subprocess argv the pipeline launches, while still running it."""
    calls: list[list[str]] = []
    real = conversion._run

    async def spy(argv, **kwargs):
        calls.append(list(argv))
        return await real(argv, **kwargs)

    monkeypatch.setattr(conversion, "_run", spy)
    return calls


def post_pdf(client, data: bytes, *, params: str = "", filename: str = "sample.pdf", headers=AUTH):
    return client.post(
        URL + params,
        files={"file": (filename, data, "application/pdf")},
        headers=headers,
    )


def leftovers(root: Path) -> list[Path]:
    return [p for p in root.iterdir() if p.name.startswith("pdfconv-")]
