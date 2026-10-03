# Private PDF-to-DOCX API: design and build instructions (v2)

> This is a revision of `DESIGN_AND_BUILD_INSTRUCTIONS.md` incorporating a design discussion covering: conversion timeout, concurrency/scaling for a multi-user Svelte frontend, OCR auto-detection, oversized-input handling, and an expanded machine-readable error contract. Sections unchanged from v1 are kept verbatim; changed sections are marked with **(v2)**.

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

Note: "asynchronous job polling" stays out of scope for v2 — see section 2 for how legitimate multi-user concurrency is handled without a job/polling system.

## 2. Recommended decisions **(v2)**

| Decision | Recommendation | Reason |
| --- | --- | --- |
| Language/runtime | Python 3.12 with FastAPI | `pdf2docx`, PyMuPDF, and OCRmyPDF are mature Python integrations. Python is the simplest appropriate choice here. |
| OCR engine | OCRmyPDF + Tesseract, installed in the image | OCRmyPDF handles PDF rasterization and produces a searchable PDF; Tesseract supplies OCR. |
| OCR auto-detection | Delegate to OCRmyPDF's own `--skip-text` (per-page, built into the tool) instead of a hand-rolled character-count heuristic | OCRmyPDF already solves "OCR only the pages that need it" using proper text-layer inspection. Reinventing this with a naive character-count threshold is less robust and unnecessary. |
| Native conversion | `pdf2docx` | Matches the requested stack and has a straightforward PDF-to-DOCX API. |
| API authentication | Static bearer tokens in an ACA secret, comma-separated or JSON encoded | A handful of personal keys does not justify accounts, a database, or OAuth. Use constant-time comparison. |
| OCR default | `ocr=auto`; support `never` and `always` overrides | Gives correct default behavior while providing a recovery path for imperfect detection. |
| Conversion timeout | **300 seconds** default (configurable), validated against a real worst-case scanned fixture during the Docker smoke test | OCR on a dense scanned PDF plus cold start can exceed 120s; 120s risks spurious timeouts on the exact case OCR exists for. |
| Concurrency model | Keep direct synchronous HTTP responses (no job/polling system). Scale via `maxReplicas: 3`–`5` (still `minReplicas: 0`), `concurrentRequests: 1` per replica, plus a small bounded in-process queue per replica so brief bursts wait instead of being rejected outright | The Svelte frontend tolerates some delay but still expects a direct response. Replica scaling + a short queue gives real concurrency headroom without the storage/state a job-polling system would require. |
| Input ceiling | Hard 10 MiB byte cap **and** a max page-count cap, both fail-fast with no retry/leniency | A small-byte PDF can still have pathological page counts; failing fast on either limit protects cost and timeout budget. The caller bears the cost of oversized input. |
| Error contract | JSON body with both a human `detail` string and a stable machine-readable `code` (see section 3) | The Svelte frontend needs to branch reliably on failure type; messages can change wording, codes should not. Structured server-side logging alongside this gives real debuggability. |
| ACA scaling | Consumption workload profile, `minReplicas: 0`, `maxReplicas: 3`–`5`, HTTP concurrent requests: `1` per replica | Lowest idle cost while giving headroom for multiple simultaneous uploads from the Svelte app. Cold starts are expected. |
| Network exposure | External HTTPS ingress plus bearer authentication initially | Simple and usable from personal projects. An internal-only ACA requires VNet-connected callers and adds infrastructure. |

**Cost precedence rule:** Cost/price trumps every other preference in this document, including the managed-identity-over-passwords preference for registry access. When a cheaper or free option exists and the trade-off is acceptable for a private, personal-scale service, choose it. Stay inside free tiers and free grants wherever possible, and flag any resource that would incur a fixed monthly charge before creating it. Registry: use **GitHub Container Registry (GHCR)**, not Azure Container Registry (ACR, which has no free tier).

Cost is not guaranteed to be zero: the ACA free grant, image registry, Log Analytics retention, network egress, and OCR CPU time all affect billing. A few thousand small conversions can be inexpensive, but set an Azure budget alert before deployment.

**Why not a true async job queue?** A job model (`202 Accepted` + job ID + polling) would give the most headroom for spiky multi-user load, but it requires somewhere to park the finished DOCX until the client polls for it — which reintroduces the storage/state this design deliberately avoids, and adds real implementation cost during early development. Treat it as an escape hatch: only build it if real usage shows requests routinely queuing for a long time even with multiple replicas and the bounded queue below.

## 3. API contract **(v2: error contract expanded)**

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

`ocr` behavior **(v2)**:

- `auto`: run OCRmyPDF with `--skip-text` on every upload. OCRmyPDF inspects each page's existing text layer internally and OCRs only the pages that need it, leaving pages that already have usable text untouched. This replaces the v1 custom PyMuPDF character-count heuristic with OCRmyPDF's own built-in, better-tested per-page logic.
- `never`: convert the uploaded PDF directly, skipping OCRmyPDF entirely. Useful if OCR changes a document unexpectedly.
- `always`: run OCRmyPDF with `--force-ocr`, which OCRs every page even if it already has a (possibly misleading) text layer. Slower and may reduce visual fidelity, but useful for scans with bad embedded text.

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

### Error contract **(v2)**

Return JSON errors with both a human-readable message and a stable, machine-readable code. Do not include internal paths, command output, tokens, or stack traces:

```json
{"detail": "Human-readable safe error message", "code": "FILE_TOO_LARGE"}
```

The Svelte frontend should branch on `code`, not on `detail` text, since `detail` wording may change.

| HTTP | `code`                 | When                                                                                     |
| ---: | ---------------------- | ----------------------------------------------------------------------------------------- |
|  400 | `MISSING_FILE_FIELD`   | No `file` part in the multipart body.                                                     |
|  400 | `EMPTY_FILE`           | `file` field present but zero bytes.                                                      |
|  400 | `INVALID_OCR_PARAM`    | `ocr` query param is not `auto`, `never`, or `always`.                                    |
|  401 | `MISSING_TOKEN`        | No `Authorization` header. Include `WWW-Authenticate: Bearer`.                            |
|  401 | `INVALID_TOKEN`        | Token present but does not match any configured value.                                    |
|  413 | `FILE_TOO_LARGE`       | Upload exceeds 10 MiB (checked while streaming, not via `Content-Length`).                |
|  415 | `NOT_A_PDF`            | Magic-byte/content-type validation failed.                                                |
|  422 | `ENCRYPTED_PDF`        | PDF requires a password.                                                                  |
|  422 | `MALFORMED_PDF`        | PyMuPDF could not open/parse the file.                                                    |
|  422 | `PAGE_LIMIT_EXCEEDED`  | PDF exceeds the configured max page count (`MAX_PAGES`).                                  |
|  422 | `OCR_FAILED`           | OCRmyPDF exited non-zero.                                                                 |
|  422 | `OCR_TIMEOUT`          | OCRmyPDF exceeded `CONVERSION_TIMEOUT_SECONDS`.                                           |
|  422 | `CONVERSION_FAILED`    | `pdf2docx` raised or produced invalid output.                                              |
|  422 | `EMPTY_OUTPUT`         | Conversion "succeeded" but produced a zero-byte/invalid DOCX.                              |
| 429/503 | `SERVER_BUSY`       | The per-replica bounded queue (section 2/5) is full. Include a `Retry-After` header.       |
|  500 | `INTERNAL_ERROR`       | Unexpected failure. Log a request ID and the safe exception class only — never in the body. |

Each response also corresponds to a structured, safe server-side log line (request ID, `code`, byte count, OCR mode resolved, page count, duration, error class) so failures can be diagnosed from logs without ever exposing internals to the caller.

## 4. Conversion flow **(v2)**

1. Authenticate the `Authorization` header before doing conversion work.
2. Create one `TemporaryDirectory` under a writable local temp path such as `/tmp`.
3. Stream the uploaded file into `input.pdf` in that directory, counting bytes as they are written. Stop, delete, and return `413` / `FILE_TOO_LARGE` immediately after 10 MiB. Do not trust `Content-Length`, filename, or `Content-Type`.
4. Read the first bytes and require the PDF signature (`%PDF-`). Open it with PyMuPDF to ensure it is a readable, non-encrypted PDF (`422`/`MALFORMED_PDF` or `422`/`ENCRYPTED_PDF` otherwise).
5. **(v2)** Check the page count against a configured `MAX_PAGES` ceiling. Reject immediately with `422`/`PAGE_LIMIT_EXCEEDED` if it is exceeded — this protects timeout and memory budget against a small-byte-count PDF with a pathological page count, which the byte cap alone does not catch.
6. **(v2)** Resolve the effective OCR mode by choosing OCRmyPDF's flags directly; do not hand-roll per-page text detection:
   - `auto` → run OCRmyPDF with `--skip-text`.
   - `always` → run OCRmyPDF with `--force-ocr`.
   - `never` → skip OCRmyPDF and use the original file.
7. Invoke OCRmyPDF as a subprocess with an explicit input path, output path, timeout, and no shell. Use a fixed initial OCR language, `eng`. Map a non-zero exit to `422`/`OCR_FAILED` and a timeout to `422`/`OCR_TIMEOUT`.
8. Call `pdf2docx.Converter` on the chosen PDF and write `output.docx` in the same temporary directory. Always close the converter in a `finally` block. Map a raised exception to `422`/`CONVERSION_FAILED`.
9. Verify that a non-empty, valid DOCX was produced (`422`/`EMPTY_OUTPUT` otherwise). Return a `FileResponse` with the original filename stem sanitized and a `.docx` extension. Attach a Starlette `BackgroundTask` that removes the request directory only after the response has finished streaming.
10. On every error, timeout, cancellation, or client disconnect, remove the request directory. Do not use a `with TemporaryDirectory(...)` block that exits before a `FileResponse` has streamed the DOCX.

Do not write PDF or DOCX bytes to application logs. Do not use `/tmp` as a cache.

### Implementation finding: invisible OCR text (verified during build)

OCRmyPDF stores recognised text as an *invisible* text layer. `pdf2docx` ignores invisible text by default (`ocr=0`), so a scanned page would otherwise convert to a bare picture with no editable text. Its `ocr=2` mode keeps only invisible text (and drops the page image) but would discard native text on the same page set. The worker (`app/worker.py`) therefore chooses the mode per page: pages with invisible text use `ocr=2`, all others `ocr=0`. This makes scanned and mixed documents produce editable text, and is guarded by `tests/test_ocr_content.py`. For OCR'd pages the DOCX contains recognised text only, not the scan image.

### Important OCR limitation

OCR makes scanned text searchable; it does not recreate the original Word structure. The OCR output is then passed to `pdf2docx`, which estimates paragraph, table, and image layout. Complex tables, multi-column pages, forms, unusual fonts, and low-quality scans will need occasional manual editing. This is normal for local/open-source PDF-to-DOCX conversion and should be documented for callers.

## 5. Security and operational requirements **(v2)**

### Authentication

- Read tokens from an ACA secret mounted as an environment variable, for example `API_TOKENS`.
- Permit multiple tokens during rotation. Store only token values; there is no need for user records in this private version.
- Parse only `Authorization: Bearer <token>` and compare each configured token with `secrets.compare_digest`.
- Reject missing, duplicate, malformed, and invalid credentials with the same 401 response (`MISSING_TOKEN` / `INVALID_TOKEN`).
- Never put tokens in query strings, source control, images, logs, or error messages.

### Input safety

- Enforce the 10 MiB limit while copying the upload. Configure the ingress/proxy request limit to the same limit if ACA or a fronting proxy provides one, but keep the application check as the authoritative check.
- **(v2)** Enforce a configurable `MAX_PAGES` ceiling right after opening the PDF with PyMuPDF, before any conversion work starts.
- Validate the PDF signature and open it with PyMuPDF before conversion.
- Reject encrypted/password-protected PDFs with 422 rather than accepting a password field.
- Generate all temporary filenames server-side. Never use the uploaded filename as a path.
- Invoke subprocesses with argument lists, never `shell=True`.
- Apply a conversion timeout. Default to **300 seconds**, validated against a real worst-case fixture, and make it configurable through `CONVERSION_TIMEOUT_SECONDS`.

### Concurrency **(v2 — replaces the single-replica-only guard)**

- Limit each replica to one active conversion at a time (`concurrentRequests: 1` at the ACA scale-rule level), since OCR/conversion is CPU-heavy.
- In front of that one slot, hold a small bounded in-process queue per replica (e.g. `asyncio.Queue(maxsize=5)`) so a short burst of simultaneous uploads waits briefly inside the request instead of being rejected outright.
- Only return `503`/`SERVER_BUSY` (with a `Retry-After` header) once that queue is also full.
- Scale real concurrency by raising `maxReplicas` to `3`–`5` rather than by building a job/polling system — this gives horizontal headroom for multiple simultaneous Svelte-app uploads while keeping the direct-response, stateless contract.
- Verify against the actual deployed ACA revision (not just local Docker) what a rejected/queued request looks like at the ingress scale-rule layer versus the application's own queue and 503 — these can differ, and only testing against the real environment confirms which one a caller actually sees.

### Logging and health

- Add `GET /healthz`, which returns 200 without authentication and does not test an external service.
- Log structured, safe fields only: request ID, `code`, input byte count, OCR mode selected, page count, result, duration, and error class.
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
6. Start Uvicorn with one worker per replica. Multiple workers multiply memory and can violate the intended one-conversion-per-replica limit.
7. **(v2)** Run a Docker smoke test using one text PDF, one scanned PDF, and one worst-case fixture (large, dense, many-page scan near the 10 MiB cap). Measure wall-clock time (to validate the 300s timeout) and peak memory (`docker stats`, to validate the ACA memory allocation in section 7) against that worst-case fixture before deployment.

A Linux container is required. Do not develop the conversion pipeline against Windows-only behavior; test it inside the image that ACA will run.

## 7. Azure Container Apps deployment shape **(v2)**

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
      maxReplicas: 3
      rules:
        - name: http
          http:
            metadata:
              concurrentRequests: "1"
    containers:
      - name: pdf-convert-api
        image: ghcr.io/pumpkindonutz/pdf-convert-api:<immutable-tag>
        resources:
          cpu: 0.5
          memory: 1Gi
        env:
          - name: API_TOKENS
            secretRef: api-tokens
          - name: CONVERSION_TIMEOUT_SECONDS
            value: "300"
          - name: OCR_LANGUAGE
            value: "eng"
          - name: MAX_PAGES
            value: "200"
```

Note: confirm the `memory: 1Gi` allocation against the worst-case fixture's peak RSS measured in the section 6 smoke test (OCR rasterization can spike well above input file size); raise to `2Gi` if that measurement is close to the limit.

Deployment instructions:

1. Create an Azure resource group in the region nearest the callers.
2. Use GitHub Container Registry (`ghcr.io`) as the image registry; do not create an Azure Container Registry (cost precedence rule, section 2). GitHub Actions pushes with the built-in `GITHUB_TOKEN` (no stored credential). **Decision: the GHCR package (and, if needed, the repo) is public**, because private GHCR packages count against a small storage/transfer quota that a Tesseract-based image would exceed. ACA therefore needs no pull credential. This is acceptable only under these rules: never commit or bake secrets into the repo, image, build args, workflow files, or logs; `API_TOKENS` and any other secret live only in the ACA secret store (and GitHub Actions secrets for the three OIDC IDs); `.gitignore`/`.dockerignore` must exclude `.env`, key files, and test fixtures containing real documents; scan the diff for secrets before every push, since a public repo's history cannot be reliably un-published.
3. Create the ACA environment and container app with the shape above.
4. Add `API_TOKENS` as an ACA secret. Do not pass it as a build argument or bake it into an image.
5. Set a spending budget and alerts before exposing the URL.
6. Deploy an immutable image tag, run authenticated text-PDF and scanned-PDF smoke tests, then direct personal projects to the revision URL.
7. Rotate tokens by adding a new token to the secret, deploying a new revision, changing callers, then removing the old token.
8. **(v2)** After deployment, fire several concurrent requests at the real revision URL and confirm the busy-path response (queue wait, then `SERVER_BUSY` once the per-replica queue is full, or a new replica spinning up) matches what section 5's concurrency design expects — ACA's own scale-rule rejection behavior can differ from the application-level queue/503 and should be verified against the live environment, not assumed from local testing.

Start with external ingress and bearer authentication. If every caller later runs within an Azure VNet, change to internal ingress and use private DNS. Do not add API Management, Front Door, Redis, or a database until a concrete need appears. If real usage later shows requests routinely queuing for a long time even with `maxReplicas` raised further, revisit the async job-queue escape hatch noted in section 2 — that is the point at which it becomes worth the added storage/state.

## 8. Suggested repository layout

```text
pdf-convert-api/
├── app/
│   ├── main.py            # FastAPI routes, response handling, exception mapping
│   ├── auth.py            # bearer-token parsing and constant-time verification
│   ├── conversion.py      # validation, page-count check, OCR mode selection, OCR subprocess, pdf2docx conversion
│   ├── config.py          # environment-backed settings and validation
│   └── schemas.py         # small shared types, if needed
├── tests/
│   ├── test_auth.py
│   ├── test_upload_limits.py
│   ├── test_conversion.py # small text and scanned fixtures or generated fixtures
│   └── test_concurrency.py # bounded-queue / busy-path test (v2)
├── Dockerfile
├── requirements.txt       # or pyproject.toml plus a locked requirements file
├── .dockerignore
├── .gitignore
└── README.md
```

Keep the implementation smaller if this structure proves unnecessary. For example, a minimal first version may keep authentication and configuration in `main.py`, but conversion itself should remain separate because it is blocking, resource-sensitive code with several cleanup paths.

## 9. Implementation sequence **(v2)**

1. Create a FastAPI app with `GET /healthz` and the authenticated `POST /v1/convert/pdf/to/docx` route.
2. Implement configuration validation at startup: at least one token must be configured, the timeout and `MAX_PAGES` must be positive, and the OCR mode/language defaults must be valid.
3. Add streaming upload-to-temp-dir logic, byte counting, PDF signature validation, page-count check, and encrypted/corrupt-PDF handling. Write tests for every `4xx` code in the section 3 table.
4. Implement direct `pdf2docx` conversion and direct DOCX file response. Verify cleanup on success and conversion failure.
5. Add the `ocr=auto|never|always` behavior by selecting OCRmyPDF flags (`--skip-text` / `--force-ocr` / skip) rather than a custom per-page detector.
6. Add the OCRmyPDF subprocess with fixed arguments, timeout handling, safe failure mapping (`OCR_FAILED` / `OCR_TIMEOUT`), and tests using a small scan fixture.
7. **(v2)** Add the bounded per-replica queue in front of the one-slot concurrency guard; return `503`/`SERVER_BUSY` with `Retry-After` once the queue is full. Write a deterministic test that holds the semaphore directly (bypassing real conversion latency) to force the busy path.
8. Containerize it, run the test suite in the container, and smoke-test both conversion paths plus the worst-case fixture (timing and memory) locally.
9. Deploy to ACA with secret-backed tokens, `maxReplicas: 3`, a budget alert, and low-cost logging retention. Verify the live busy-path behavior per section 7, step 8.

## 10. Acceptance checks **(v2)**

Before calling the first version complete, verify all of the following:

- A valid text PDF under 10 MiB returns a downloadable, non-empty DOCX with `ocr=auto` and OCRmyPDF's `--skip-text` leaves it effectively unchanged.
- A scanned PDF under 10 MiB returns a non-empty DOCX with `ocr=auto` and OCR runs.
- A mixed PDF OCRs only the pages lacking text when `ocr=auto` (verified via OCRmyPDF's own `--skip-text` behavior, not a custom detector).
- `ocr=never` skips OCR and `ocr=always` forces it on every page.
- Missing and invalid tokens return 401 (`MISSING_TOKEN`/`INVALID_TOKEN`) without leaking authentication details.
- A 10 MiB-plus file returns 413/`FILE_TOO_LARGE` while it is being copied, and no temporary file remains.
- A PDF exceeding `MAX_PAGES` returns 422/`PAGE_LIMIT_EXCEEDED` before any OCR or conversion work starts.
- Invalid, encrypted, and malformed PDFs fail safely with the documented status and code.
- Temporary PDF, OCR intermediate, and DOCX files are removed after success, error, timeout, and client cancellation; specifically, successful `FileResponse` cleanup runs as a background task after streaming ends.
- The service does not write document data, bearer tokens, or temporary paths to logs; every response has a corresponding structured log line with the fields in section 5.
- A deterministic test (holding the conversion semaphore directly) confirms the bounded queue fills and then returns 503/`SERVER_BUSY` with `Retry-After`, rather than relying on real timing races.
- Firing concurrent requests against the actual deployed ACA revision produces the expected behavior at both the ACA scale-rule layer and the application queue layer (see section 7, step 8).
- The worst-case fixture (large scanned PDF near 10 MiB) completes within `CONVERSION_TIMEOUT_SECONDS` and peak memory stays under the configured container limit with margin, both measured in the Docker smoke test.
- The same tests pass inside the production Docker image.

## 11. Confirmed deployment decisions **(v2)**

- **OCR language:** English only (`eng`). Do not install additional Tesseract language packs.
- **Ingress:** Public HTTPS endpoint. Any client with a valid bearer token may call it; do not add IP allowlists or VNet-only ingress initially.
- **Capacity:** Start with a 300-second conversion timeout (validated against a worst-case fixture), one concurrent conversion per replica with a small bounded queue, and `maxReplicas: 3` to absorb simultaneous uploads from the Svelte frontend. Return 503/`SERVER_BUSY` once the queue is also full; increase `maxReplicas` or revisit the async job-queue escape hatch only after observing real queuing under load.
- **Oversized input:** Hard-fail on both the 10 MiB byte cap and a configurable `MAX_PAGES` cap, with no leniency or retry — the cost of oversized input is the caller's.
