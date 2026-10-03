"""End-to-end content checks: OCR'd text must actually reach the DOCX.

pdf2docx drops invisible text by default, which is exactly what OCRmyPDF adds,
so these guard against a scanned PDF silently converting to a bare image.
"""
import io

from docx import Document

from .conftest import make_mixed_pdf, make_scanned_pdf, make_text_pdf, post_pdf


def docx_text(content: bytes) -> str:
    return " ".join(p.text for p in Document(io.BytesIO(content)).paragraphs)


def test_scanned_pdf_text_is_editable_in_docx(client):
    r = post_pdf(client, make_scanned_pdf())
    assert r.status_code == 200, r.text
    text = docx_text(r.content)
    assert "Scanned receipt total" in text
    assert "quick brown fox" in text


def test_scanned_pdf_without_ocr_has_no_text(client):
    r = post_pdf(client, make_scanned_pdf(), params="?ocr=never")
    assert r.status_code == 200
    assert "Scanned receipt" not in docx_text(r.content)


def test_mixed_pdf_keeps_native_and_ocr_text(client):
    r = post_pdf(client, make_mixed_pdf())
    assert r.status_code == 200, r.text
    text = docx_text(r.content)
    assert "Quarterly invoice summary" in text  # native-text page
    assert "Scanned receipt total" in text  # OCR'd page


def test_native_text_pdf_still_converts_unchanged(client):
    r = post_pdf(client, make_text_pdf(2))
    assert r.status_code == 200
    text = docx_text(r.content)
    assert "Quarterly invoice summary page 1" in text
    assert "Quarterly invoice summary page 2" in text


def test_force_ocr_on_text_pdf_keeps_text(client):
    r = post_pdf(client, make_text_pdf(), params="?ocr=always")
    assert r.status_code == 200
    assert "Quarterly invoice summary" in docx_text(r.content)
