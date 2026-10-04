# pdf-convert-api

A small, private, stateless HTTP API that converts a PDF upload into a DOCX download.
Python 3.12 + FastAPI, `pdf2docx` for conversion, OCRmyPDF + Tesseract for scanned PDFs,
deployed as a container on Azure Container Apps (scale-to-zero).

Design: `DESIGN_AND_BUILD_INSTRUCTIONS_v2.md`. Deployment pipeline: `TOOLCHAIN_SETUP.md`.

## API

```bash
curl --fail-with-body \
  --request POST 'https://<app-url>/v1/convert/pdf/to/docx?ocr=auto' \
  --header 'Authorization: Bearer YOUR_PRIVATE_KEY' \
  --form 'file=@invoice.pdf;type=application/pdf' \
  --output invoice.docx
```

- `GET /healthz`: unauthenticated liveness check.
- `POST /v1/convert/pdf/to/docx`: multipart upload with a `file` field, at most 10 MiB.
- `ocr` query parameter:
  - `auto` (default): OCRmyPDF `--skip-text`; only pages without a text layer are OCR'd.
  - `never`: no OCR; convert the PDF as-is.
  - `always`: OCRmyPDF `--force-ocr`; every page is OCR'd.

Errors are JSON, `{"detail": "...", "code": "..."}`. Branch on `code`, not `detail`:

| HTTP | code | meaning |
| ---: | --- | --- |
| 400 | `MISSING_FILE_FIELD` / `EMPTY_FILE` / `INVALID_OCR_PARAM` | bad request |
| 401 | `MISSING_TOKEN` / `INVALID_TOKEN` | authentication (`WWW-Authenticate: Bearer`) |
| 413 | `FILE_TOO_LARGE` | over 10 MiB (enforced while streaming) |
| 415 | `NOT_A_PDF` | not a PDF |
| 422 | `ENCRYPTED_PDF` / `MALFORMED_PDF` / `PAGE_LIMIT_EXCEEDED` | unusable PDF |
| 422 | `OCR_FAILED` / `OCR_TIMEOUT` / `CONVERSION_FAILED` / `EMPTY_OUTPUT` | processing failure |
| 503 | `SERVER_BUSY` | queue full; honour `Retry-After` |
| 500 | `INTERNAL_ERROR` | unexpected; quote the `X-Request-ID` response header |

Every response carries an `X-Request-ID` header matching one structured log line.

### Conversion caveats

OCR makes scanned text editable; it does not recreate Word structure. Complex tables,
multi-column pages, forms and low-quality scans may need manual cleanup. For OCR'd pages
the DOCX contains the recognised text only (the scan image is not embedded). Native-text
pages convert as usual, and mixed documents are handled page by page. Password-protected
PDFs are not supported.

## Configuration (environment)

| Variable | Default | Notes |
| --- | --- | --- |
| `API_TOKENS` | required | Comma-separated or JSON list; each at least 16 characters. Several allowed for rotation. |
| `CONVERSION_TIMEOUT_SECONDS` | `300` | Total budget for OCR + conversion. Not yet validated against a worst-case scan. |
| `MAX_PAGES` | `200` | Hard cap, checked before any OCR or conversion. |
| `OCR_LANGUAGE` | `eng` | Only English data is installed in the image. |
| `QUEUE_SIZE` | `5` | Requests that may wait for the single conversion slot before `SERVER_BUSY`. |
| `RETRY_AFTER_SECONDS` | `30` | Value of the `Retry-After` header on `SERVER_BUSY`. |

The app refuses to start if the configuration is invalid.

## Develop and test (Docker only; no local Python needed)

```bash
docker build --target test -t pdf-convert-api:test .
docker run --rm pdf-convert-api:test          # runs the full test suite

docker build -t pdf-convert-api:local .
docker run --rm -p 8000:8000 -e API_TOKENS=local-dev-token-0123456789 pdf-convert-api:local
```

## Layout

```text
app/main.py        routes, response handling, exception mapping
app/auth.py        bearer-token parsing, constant-time verification
app/upload.py      streaming multipart upload with the byte cap
app/conversion.py  PDF validation, OCR + converter subprocesses, DOCX verification
app/worker.py      pdf2docx subprocess (per-page OCR-text handling)
app/gate.py        one-conversion-per-replica gate with a bounded queue
app/config.py      validated environment settings
app/schemas.py     error contract
tests/             pytest suite (runs inside the image)
```

## Deployment (live)

- Azure Container Apps `pdf-convert-api` in resource group `pdf-convert-rg` (West US), Consumption plan, scale 0 to 3, one concurrent request per replica. Image: `ghcr.io/pumpkindonutz/pdf-convert-api:<git sha>` (public package).
- Push to `main` runs `.github/workflows/deploy.yml`: tests, then build, push to GHCR, `az containerapp update`, and a `/healthz` check. Azure login uses OIDC (no stored Azure password); the federated credential is limited to `main` and uses GitHub's immutable-ID subject (`repo:<owner>@<id>/<repo>@<id>:ref:refs/heads/main`).
- The `API_TOKENS` secret lives only in the ACA secret store. To rotate: add a new token to the secret, create a new revision, switch callers, then remove the old token.
- Budget: $5/month on the resource group with email alerts (50%, 80%, 100% actual, 100% forecast).

### Observed behaviour under load (verified against the live revision)

Twelve simultaneous OCR requests against a cold single replica: 6 completed (1 active + 5 queued, serialised at about 6 s each) and 6 received `503 SERVER_BUSY` with `Retry-After: 30` within about 0.5 s. ACA scaled out to 3 replicas afterwards, but a burst that arrives before scale-out lands on the one existing replica, so the application queue and 503 are what a caller sees first. Callers should honour `Retry-After` and retry. Cold start from zero adds a few seconds to the first request.

## Operations notes

- One Uvicorn worker per replica; ACA scales out via `maxReplicas`, one concurrent request per replica.
- Temp files live in a per-request directory under `/tmp` and are removed after success, error, timeout and client disconnect.
- OCR and conversion run in their own process groups and are killed on timeout or cancellation (`tini` reaps them).
- Never commit tokens or real documents. `API_TOKENS` lives only in the ACA secret store.
