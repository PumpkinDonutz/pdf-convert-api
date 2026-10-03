"""Small shared types: the API error contract and constants."""
from __future__ import annotations

OCR_MODES = ("auto", "never", "always")
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class ApiError(Exception):
    """An error that maps to the documented JSON error contract.

    `detail` must be safe to show to callers: no paths, command output,
    tokens, or stack traces.
    """

    def __init__(self, status: int, code: str, detail: str, headers: dict[str, str] | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers or {}


def missing_file_field() -> ApiError:
    return ApiError(400, "MISSING_FILE_FIELD", "The multipart body must include a 'file' field.")


def empty_file() -> ApiError:
    return ApiError(400, "EMPTY_FILE", "The uploaded file is empty.")


def invalid_ocr_param() -> ApiError:
    return ApiError(400, "INVALID_OCR_PARAM", "The 'ocr' parameter must be one of: auto, never, always.")


def missing_token() -> ApiError:
    return ApiError(401, "MISSING_TOKEN", "Authentication required.", {"WWW-Authenticate": "Bearer"})


def invalid_token() -> ApiError:
    return ApiError(401, "INVALID_TOKEN", "Invalid credentials.", {"WWW-Authenticate": "Bearer"})


def file_too_large(limit_bytes: int) -> ApiError:
    return ApiError(413, "FILE_TOO_LARGE", f"The uploaded file exceeds the {limit_bytes // (1024 * 1024)} MiB limit.")


def not_a_pdf() -> ApiError:
    return ApiError(415, "NOT_A_PDF", "The uploaded file is not a PDF.")


def encrypted_pdf() -> ApiError:
    return ApiError(422, "ENCRYPTED_PDF", "Password-protected PDFs are not supported.")


def malformed_pdf() -> ApiError:
    return ApiError(422, "MALFORMED_PDF", "The PDF could not be read.")


def page_limit_exceeded(max_pages: int) -> ApiError:
    return ApiError(422, "PAGE_LIMIT_EXCEEDED", f"The PDF exceeds the {max_pages}-page limit.")


def ocr_failed() -> ApiError:
    return ApiError(422, "OCR_FAILED", "OCR processing failed.")


def ocr_timeout() -> ApiError:
    return ApiError(422, "OCR_TIMEOUT", "OCR processing timed out.")


def conversion_failed(detail: str = "The PDF could not be converted to DOCX.") -> ApiError:
    return ApiError(422, "CONVERSION_FAILED", detail)


def empty_output() -> ApiError:
    return ApiError(422, "EMPTY_OUTPUT", "Conversion produced no usable DOCX output.")


def server_busy(retry_after: int) -> ApiError:
    return ApiError(503, "SERVER_BUSY", "The server is busy; retry shortly.", {"Retry-After": str(retry_after)})


def internal_error() -> ApiError:
    return ApiError(500, "INTERNAL_ERROR", "An unexpected error occurred.")
