"""FastAPI routes, response handling and exception mapping."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse

from .auth import authenticate
from .config import Settings
from .conversion import convert, inspect_pdf
from .gate import ConversionGate
from .logs import log_request
from .schemas import DOCX_MEDIA_TYPE, OCR_MODES, ApiError, internal_error, invalid_ocr_param
from .upload import receive_upload, safe_stem


class CleanupFileResponse(FileResponse):
    """FileResponse that always removes the request directory afterwards.

    The `finally` also covers client disconnects, where Starlette would skip
    background tasks.
    """

    def __init__(self, *args, workdir: Path, **kwargs):
        super().__init__(*args, background=BackgroundTask(shutil.rmtree, workdir, ignore_errors=True), **kwargs)
        self._workdir = workdir

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            shutil.rmtree(self._workdir, ignore_errors=True)


def _error_response(request: Request, exc: ApiError) -> JSONResponse:
    request.state.info["code"] = exc.code
    return JSONResponse(
        {"detail": exc.detail, "code": exc.code},
        status_code=exc.status,
        headers=exc.headers,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.gate = ConversionGate(settings.queue_size, settings.retry_after_seconds)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        request.state.info = {}
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        info = request.state.info
        log_request(
            request_id=request.state.request_id,
            status=response.status_code,
            code=info.get("code", "OK" if response.status_code < 400 else "ERROR"),
            duration_ms=int((time.monotonic() - started) * 1000),
            info=info,
        )
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return _error_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        request.state.info["code"] = code
        return JSONResponse(
            {"detail": "Request not allowed." if exc.status_code == 405 else "Not found.", "code": code},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        request.state.info["error_class"] = type(exc).__name__
        return _error_response(request, internal_error())

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/convert/pdf/to/docx")
    async def convert_pdf_to_docx(request: Request):
        info = request.state.info
        authenticate(request)

        ocr_values = request.query_params.getlist("ocr")
        ocr_mode = ocr_values[0] if len(ocr_values) == 1 else ("auto" if not ocr_values else "")
        if ocr_mode not in OCR_MODES:
            raise invalid_ocr_param()
        info["ocr_mode"] = ocr_mode

        workdir = Path(tempfile.mkdtemp(prefix="pdfconv-"))
        handed_off = False
        try:
            upload = await receive_upload(request, workdir / "input.pdf", settings.max_upload_bytes)
            info["bytes"] = upload.size
            info["pages"] = await asyncio.to_thread(inspect_pdf, workdir / "input.pdf", settings.max_pages)

            async with app.state.gate.slot():
                output = await convert(workdir, settings, ocr_mode)

            response = CleanupFileResponse(
                output,
                media_type=DOCX_MEDIA_TYPE,
                filename=f"{safe_stem(upload.filename)}.docx",
                workdir=workdir,
            )
            handed_off = True
            return response
        except ApiError:
            raise
        except Exception as exc:
            info["error_class"] = type(exc).__name__
            raise internal_error() from exc
        finally:
            if not handed_off:
                shutil.rmtree(workdir, ignore_errors=True)

    return app
