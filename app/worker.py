"""PDF -> DOCX worker, run as a subprocess so it can be timed out and killed.

Usage: python -m app.worker <input.pdf> <output.docx>

OCRmyPDF adds recognised text as an *invisible* layer (text render mode 3).
pdf2docx ignores invisible text by default (``ocr=0``), so a scanned page would
convert to just a picture. pdf2docx's ``ocr=2`` mode does the opposite: it keeps
only the invisible text. Native-text pages need ``ocr=0``, scanned pages need
``ocr=2``, and a mixed document needs both, so the mode is chosen per page by
wrapping pdf2docx's per-page text extraction (pdf2docx is pinned in
requirements.txt and the OCR content tests guard this behaviour).
"""
from __future__ import annotations

import logging
import sys

# pdf2docx's own progress logging is noise here; the parent discards output anyway.
logging.disable(logging.CRITICAL)

INVISIBLE_TEXT = 3  # PyMuPDF get_texttrace() span type for render mode 3


def page_has_invisible_text(page) -> bool:
    try:
        return any(span["type"] == INVISIBLE_TEXT for span in page.get_texttrace())
    except Exception:
        return False


def convert(src: str, dst: str) -> None:
    from pdf2docx import Converter
    from pdf2docx.page.RawPageFitz import RawPageFitz

    converter = Converter(src)
    try:
        ocr_pages = {i for i, page in enumerate(converter.fitz_doc) if page_has_invisible_text(page)}
        original = RawPageFitz._preprocess_text

        def per_page_mode(self, **settings):
            settings["ocr"] = 2 if self.page_engine.number in ocr_pages else 0
            return original(self, **settings)

        RawPageFitz._preprocess_text = per_page_mode
        converter.convert(dst)
    finally:
        converter.close()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return 2
    convert(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
