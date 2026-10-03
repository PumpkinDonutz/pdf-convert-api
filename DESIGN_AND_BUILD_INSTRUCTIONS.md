# Private PDF-to-DOCX API: design and build instructions

## 1. Purpose and scope

Build one private, stateless HTTP API that converts a PDF upload into a DOCX download.

The API is deliberately limited to PDF-to-DOCX. It accepts both normal, text-based PDFs and scanned PDFs. It automatically chooses OCR when needed, while allowing a caller to explicitly disable or force OCR.

### In scope

- Python, FastAPI, Docker, and Azure Container Apps (ACA)
- Bearer-token authentication using a small set of personal API keys
- `multipart/form-data` upload with a `file` field
- Maximum **10 MiB** input file size
- Direct DOCX response; no object storage, database, queue, account system, or conversion history
- Normal conversion with `pdf2docx` (which uses PyMuPDF)
- OCR preprocessing with OCRmyPDF and Tesseract when required
- Temporary local files that are removed before every request completes (but not during testing)

### Out of scope

- More conversion formats, batch jobs, asynchronous job polling, web UI, user management, permanent storage, retries, billing, or document sharing
- Password-protected PDF support in the first version
- Guaranteed visual fidelity for arbitrary PDFs

## 2. Recommended decisions

| Decision           | Recommendation                                                                                  | Reason                                                                                                                  |
| ------------------ | ----------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Language/runtime   | Python 3.12 with FastAPI                                                                        | `pdf2docx`, PyMuPDF, and OCRmyPDF are mature Python integrations. Python is the simplest appropriate choice here.       |
| OCR engine         | OCRmyPDF + Tesseract, installed in the image                                                    | OCRmyPDF handles PDF rasterization and produces a searchable PDF; Tesseract supplies OCR.                               |
| Native conversion  | `pdf2docx`                                                                                      | Matches the requested stack and has a straightforward PDF-to-DOCX API.                                                  |
| API authentication | Static bearer tokens in an ACA secret, comma-separated or JSON encoded                          | A handful of personal keys does not justify accounts, a database, or OAuth. Use constant-time comparison.               |
| OCR default        | `ocr=auto`; support `never` and `always` overrides                                              | Gives correct default behavior while providing a recovery path for imperfect detection.                                 |
| ACA scaling        | Consumption workload profile, `minReplicas: 0`, `maxReplicas: 1`, HTTP concurrent requests: `1` | Lowest idle cost and avoids running multiple CPU-heavy conversions in a small replica. Cold starts are expected.        |
| Network exposure   | External HTTPS ingress plus bearer authentication initially                                     | Simple and usable from personal projects. An internal-only ACA requires VNet-connected callers and adds infrastructure. |

Cost is not guaranteed to be zero: the ACA free grant, image registry, Log Analytics retention, network egress, and OCR CPU time all affect billing. A few thousand small conversions can be inexpensive, but set an Azure budget alert before deployment.

## 3. API contract

### Endpoint

```http
POST /v1/convert/pdf/to/docx?ocr=auto
Authorization: Bearer <api-key>
Content-Type: multipart/form-data
```

Multipart form fields:

| Field  | Required | Type            | Rules                                        |
| ------ | --------:| --------------- | -------------------------------------------- |
| `file` | yes      | file            | A PDF with an actual size of at most 10 MiB. |
| `ocr`  | no       | query parameter | `auto` (default), `never`, or `always`.      |

`ocr` behavior:

- `auto`: inspect PDF pages. If every page has meaningful extractable text, convert directly. If any page does not, run OCRmyPDF with `--skip-text` first, then convert its output. This preserves normal text pages and OCRs scanned pages in a mixed document.
- `never`: convert the uploaded PDF directly. This is useful if OCR changes a document unexpectedly.
- `always`: OCR all pages before conversion. This is slower and may reduce visual fidelity, but is useful for scans with misleading embedded text.

### Successful response

```http
200 OK
Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document
Content-Disposition: attachment; filename="source.docx"
```

The API returns the DOCX bytes directly. It does not return a URL or retain the result after the response is finished.

### Example

```bash
curl --fail-with-body \
  --request POST 'https://<app-url>/v1/convert/pdf/to/docx?ocr=auto' \
  --header 'Authorization: Bearer YOUR_PRIVATE_KEY' \
  --form 'file=@invoice.pdf;type=application/pdf' \
  --output invoice.docx
```

### Error contract

Return JSON errors in this shape and do not include internal paths, command output, tokens, or stack traces:

```json
{"detail":"Human-readable safe error message"}
```

| Status | When                                                                                                                             |
| ------:| -------------------------------------------------------------------------------------------------------------------------------- |
| 401    | Missing, malformed, or invalid bearer token. Include `WWW-Authenticate: Bearer`.                                                 |
| 413    | Uploaded file exceeds 10 MiB.                                                                                                    |
| 415    | Upload is not a PDF (validate magic bytes as well as the supplied content type).                                                 |
| 422    | The PDF is unreadable, encrypted, malformed, or cannot be converted.                                                             |
| 500    | An unexpected server failure. Log a request ID and the safe exception summary only.                                              |
| 503    | The service cannot accept work because its single conversion slot is busy. This is preferable to overloading a low-cost replica. |

## 4. Conversion flow

1. Authenticate the `Authorization` header before doing conversion work.
2. Create one `TemporaryDirectory` under a writable local temp path such as `/tmp`.
3. Stream the uploaded file into `input.pdf` in that directory, counting bytes as they are written. Stop, delete, and return `413` immediately after 10 MiB. Do not trust `Content-Length`, filename, or `Content-Type`.
4. Read the first bytes and require the PDF signature (`%PDF-`). Open it with PyMuPDF to ensure it is a readable, non-encrypted PDF.
5. Resolve the effective OCR mode.
   - In `auto`, extract text from every page with PyMuPDF and count non-whitespace characters. Treat a page with fewer than 20 characters as needing OCR. If any page needs OCR, use OCRmyPDF with `--skip-text`.
   - In `always`, OCR every page.
   - In `never`, use the original file.
6. Invoke OCRmyPDF as a subprocess with an explicit input path, output path, timeout, and no shell. Use a fixed initial OCR language, `eng`.
7. Call `pdf2docx.Converter` on the chosen PDF and write `output.docx` in the same temporary directory. Always close the converter in a `finally` block.
8. Verify that a non-empty DOCX was produced. Return a `FileResponse` with the original filename stem sanitized and a `.docx` extension. Attach a Starlette `BackgroundTask` that removes the request directory only after the response has finished streaming.
9. On every error, timeout, cancellation, or client disconnect, remove the request directory. Do not use a `with TemporaryDirectory(...)` block that exits before a `FileResponse` has streamed the DOCX.

Do not write PDF or DOCX bytes to application logs. Do not use `/tmp` as a cache.

### Important OCR limitation

OCR makes scanned text searchable; it does not recreate the original Word structure. The OCR output is then passed to `pdf2docx`, which estimates paragraph, table, and image layout. Complex tables, multi-column pages, forms, unusual fonts, and low-quality scans will need occasional manual editing. This is normal for local/open-source PDF-to-DOCX conversion and should be documented for callers.

## 5. Security and operational requirements

### Authentication

- Read tokens from an ACA secret mounted as an environment variable, for example `API_TOKENS`.
- Permit multiple tokens during rotation. Store only token values; there is no need for user records in this private version.
- Parse only `Authorization: Bearer <token>` and compare each configured token with `secrets.compare_digest`.
- Reject missing, duplicate, malformed, and invalid credentials with the same 401 response.
- Never put tokens in query strings, source control, images, logs, or error messages.

### Input safety

- Enforce the 10 MiB limit while copying the upload. Configure the ingress/proxy request limit to the same limit if ACA or a fronting proxy provides one, but keep the application check as the authoritative check.
- Validate the PDF signature and open it with PyMuPDF before conversion.
- Reject encrypted/password-protected PDFs with 422 rather than accepting a password field.
- Generate all temporary filenames server-side. Never use the uploaded filename as a path.
- Invoke subprocesses with argument lists, never `shell=True`.
- Apply a conversion timeout. Start with 120 seconds and make it configurable through `CONVERSION_TIMEOUT_SECONDS`.
- Limit the process to one conversion at a time per replica. A process-local semaphore is sufficient because ACA is capped at one replica in the initial deployment.

### Logging and health

- Add `GET /healthz`, which returns 200 without authentication and does not test an external service.
- Log structured, safe fields only: request ID, input byte count, OCR mode selected, page count, result, duration, and error class.
- Do not log authorization headers, filenames, document content, temporary paths, subprocess output, or full stack traces in production responses.
- Configure short log retention or a low-cost logging option. ACA diagnostic logs can otherwise become a noticeable cost for this small service.

## 6. Container design

Use a non-root user and a writable `/tmp`. The final image needs:

- Python 3.12
- `fastapi`, `uvicorn`, `python-multipart`, `pymupdf`, `pdf2docx`, and `ocrmypdf`
- System OCR dependencies compatible with the selected OCRmyPDF version: at minimum Tesseract with English language data, Ghostscript, and QPDF; install any additional packages required by the pinned OCRmyPDF release

Build instructions:

1. Pin Python dependencies in a lock file or requirements file. Pin the base image by a maintained Python minor version and review it regularly.
2. Install system packages in one Docker layer and remove package-list caches in that layer.
3. Copy only dependency files before installing Python packages so Docker can cache that layer.
4. Copy the application source, create a non-root user, and use that user at runtime.
5. Set `TMPDIR=/tmp` and make `/tmp` writable by the runtime user.
6. Start Uvicorn with one worker. Multiple workers multiply memory and can violate the intended one-conversion limit.
7. Run a Docker smoke test using one text PDF and one scanned PDF before deployment.

A Linux container is required. Do not develop the conversion pipeline against Windows-only behavior; test it inside the image that ACA will run.

## 7. Azure Container Apps deployment shape

Initial deployment target:

```yaml
properties:
  configuration:
    activeRevisionsMode: Single
    ingress:
      external: true
      targetPort: 8000
      transport: auto
  template:
    scale:
      minReplicas: 0
      maxReplicas: 1
      rules:
        - name: http
          http:
            metadata:
              concurrentRequests: "1"
    containers:
      - name: pdf-convert-api
        image: <your-registry>/pdf-convert-api:<immutable-tag>
        resources:
          cpu: 0.5
          memory: 1Gi
        env:
          - name: API_TOKENS
            secretRef: api-tokens
          - name: CONVERSION_TIMEOUT_SECONDS
            value: "120"
          - name: OCR_LANGUAGE
            value: "eng"
```

Deployment instructions:

1. Create an Azure resource group in the region nearest the callers.
2. Create an Azure Container Registry or use another private registry supported by ACA. Prefer managed identity from ACA to ACR over registry passwords.
3. Create the ACA environment and container app with the shape above.
4. Add `API_TOKENS` as an ACA secret. Do not pass it as a build argument or bake it into an image.
5. Set a spending budget and alerts before exposing the URL.
6. Deploy an immutable image tag, run authenticated text-PDF and scanned-PDF smoke tests, then direct personal projects to the revision URL.
7. Rotate tokens by adding a new token to the secret, deploying a new revision, changing callers, then removing the old token.

Start with external ingress and bearer authentication. If every caller later runs within an Azure VNet, change to internal ingress and use private DNS. Do not add API Management, Front Door, Redis, or a database until a concrete need appears.

## 8. Suggested repository layout

```text
pdf-convert-api/
├── app/
│   ├── main.py            # FastAPI routes, response handling, exception mapping
│   ├── auth.py            # bearer-token parsing and constant-time verification
│   ├── conversion.py      # validation, OCR detection, OCR subprocess, pdf2docx conversion
│   ├── config.py          # environment-backed settings and validation
│   └── schemas.py         # small shared types, if needed
├── tests/
│   ├── test_auth.py
│   ├── test_upload_limits.py
│   └── test_conversion.py # small text and scanned fixtures or generated fixtures
├── Dockerfile
├── requirements.txt       # or pyproject.toml plus a locked requirements file
├── .dockerignore
├── .gitignore
└── README.md
```

Keep the implementation smaller if this structure proves unnecessary. For example, a minimal first version may keep authentication and configuration in `main.py`, but conversion itself should remain separate because it is blocking, resource-sensitive code with several cleanup paths.

## 9. Implementation sequence

1. Create a FastAPI app with `GET /healthz` and the authenticated `POST /v1/convert/pdf/to/docx` route.
2. Implement configuration validation at startup: at least one token must be configured, the timeout must be positive, and the OCR mode/language defaults must be valid.
3. Add streaming upload-to-temp-dir logic, byte counting, PDF signature validation, and encrypted/corrupt-PDF handling. Write tests for 401, 413, 415, and 422.
4. Implement direct `pdf2docx` conversion and direct DOCX file response. Verify cleanup on success and conversion failure.
5. Add PyMuPDF page inspection and the `ocr=auto|never|always` behavior.
6. Add the OCRmyPDF subprocess with fixed arguments, timeout handling, safe failure mapping, and tests using a small scan fixture.
7. Add the one-slot concurrency guard and return 503 when busy. Keep ACA concurrency and max replicas aligned with it.
8. Containerize it, run the test suite in the container, and smoke-test both conversion paths locally.
9. Deploy to ACA with secret-backed tokens, a budget alert, and low-cost logging retention.

## 10. Acceptance checks

Before calling the first version complete, verify all of the following:

- A valid text PDF under 10 MiB returns a downloadable, non-empty DOCX with `ocr=auto` and does not invoke OCR.
- A scanned PDF under 10 MiB returns a non-empty DOCX with `ocr=auto` and does invoke OCR.
- A mixed PDF OCRs only pages lacking text when `ocr=auto`.
- `ocr=never` skips OCR and `ocr=always` forces it.
- Missing and invalid tokens return 401 without leaking authentication details.
- A 10 MiB-plus file returns 413 while it is being copied, and no temporary file remains.
- Invalid, encrypted, and malformed PDFs fail safely with a documented status.
- Temporary PDF, OCR intermediate, and DOCX files are removed after success, error, timeout, and client cancellation; specifically, successful `FileResponse` cleanup runs as a background task after streaming ends.
- The service does not write document data, bearer tokens, or temporary paths to logs.
- Running two requests concurrently causes one to proceed and the other to receive 503 rather than causing memory/CPU contention.
- The same tests pass inside the production Docker image.

## 11. Confirmed deployment decisions

- **OCR language:** English only (`eng`). Do not install additional Tesseract language packs.
- **Ingress:** Public HTTPS endpoint. Any client with a valid bearer token may call it; do not add IP allowlists or VNet-only ingress initially.
- **Capacity:** Start with a 120-second conversion timeout and one concurrent conversion. Return 503 while busy; increase capacity only after observing a real need.

# 
