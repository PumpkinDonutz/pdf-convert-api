# Private PDF-to-DOCX API. Linux image; same image runs locally and on ACA.
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TMPDIR=/tmp \
    HOME=/home/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        ghostscript \
        qpdf \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 app

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app

# Test stage: same image plus pytest and the tests (not shipped).
FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY tests ./tests
COPY pytest.ini .
USER app
ENTRYPOINT ["tini", "--"]
CMD ["pytest", "-q"]

# Runtime stage (default target).
FROM base AS runtime
USER app
EXPOSE 8000
# tini reaps killed OCR/Ghostscript children so they never linger as zombies.
ENTRYPOINT ["tini", "--"]
# One worker per replica: one conversion at a time, bounded memory.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
