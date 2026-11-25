# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Payslip Sender — project brief (final design)

A web app where ONE HR admin uploads employees' payslip PDFs in bulk and sends each to the right
employee on WhatsApp. The owner is a beginner on Windows (PowerShell, VS Code): explain things in
simple terms, run commands yourself where possible, and give exact commands for anything they must run.

## Commands (quick reference; details under "Local dev")
Run from the project root in PowerShell. Use `python -m ...`, never the bare `.exe` launchers (blocked on the dev PC).
- Run the app: `.\run.ps1` (= `python -m uvicorn app.main:app --reload`; XAMPP MariaDB must be running)
- All tests: `.\dev.ps1 test` (~3 minutes) or `python -m pytest -q`; one file: `python -m pytest tests/test_ids.py -q`;
  one test: `python -m pytest tests/test_ids.py::test_name -q`; by keyword: `python -m pytest -k passport -q`
- Install pinned deps: `.\dev.ps1 install`; security audit: `.\dev.ps1 audit`; production readiness (FAILs on APP_ENV on a dev PC, expected): `.\dev.ps1 check`
- Migrations: `python -m alembic revision --autogenerate -m "msg"` then `python -m alembic upgrade head` (tests use SQLite `create_all`,
  so a model change without a migration does NOT fail them)
- Upgrading a DB that already has employees (unique ID/phone, two migrations): `python -m alembic upgrade head` (the second one refuses with a
  plain message until the data is ready) -> `python -m app.scripts.backfill_id_hash` (hashes + lists duplicates) -> fix them -> `upgrade head` again.
- No linter/formatter is configured.

## Architecture in one paragraph
Routers (`app/routers/`) only handle HTTP and permissions; business logic lives in `app/services/` (pure normalisers `names/phones/ids`,
import pipeline, matching, sender worker, status path). Cross-cutting guards are app-wide dependencies/middleware in `app/security/`
(login, CSRF, headers, per-route `require("<permission>")`). The send engine, import-preview store and one-time passwords live in PROCESS
MEMORY, which is why production must run exactly one process. All WhatsApp traffic goes through the `WhatsAppClient` interface
(mock by default), and every delivery status, from the worker, the mock or the webhook, flows through `send_status.apply_status_event`.

## Stack
- Python 3.11+, FastAPI, Jinja2 templates with simple HTML (no heavy JS framework)
- MySQL/MariaDB via SQLAlchemy 2.0.x + PyMySQL (utf8mb4), migrations with Alembic
  - SQLAlchemy is pinned `<2.1`: 2.1's compiled file was blocked by Windows Application Control on the dev PC.
  - No engine-specific DB features (portable; may move to RDS). Enums are stored as VARCHAR.
- pypdf (+ cryptography) for reading PDF text and AES-256 password protection
- openpyxl for .xlsx, csv module for CSV; httpx for the WhatsApp Cloud API (Meta Graph API)
- pydantic-settings reads `.env`; argon2-cffi for password hashing
- Deployment later: Ubuntu on AWS EC2 behind Caddy (HTTPS), MySQL on the same box (maybe RDS later)

## How the app works
1. **Employees**: first_name + surname (required), phone (E.164, Botswana +267...), an ID: `id_type`
   ("omang" or "passport"), the FULL number stored ENCRYPTED (`id_number_encrypted`) and `id_last4` = the last 4
   CHARACTERS of it in plain text (uppercase, alphanumeric; the PDF password), active
   flag, optional employee_code. HR adds/edits/deactivates on a page and imports from .xlsx or CSV with columns
   `first_name, surname, phone, id_number, id_type (optional), employee_code (optional)`; the old header `omang` is
   still accepted as an alias for id_number. Importer rules: the full number is validated, ENCRYPTED the moment the
   row is parsed (the in-memory preview only holds the ciphertext + last 4) and last 4 is derived from it; id_type
   blank => guess (all digits = omang, contains letters = passport) and show a WARNING on that row so HR confirms;
   omang = digits only, WARN (not error) unless exactly 9 digits (shorter than 4 is left-padded with zeros + warning,
   because Excel drops leading zeros); passport = letters/digits only (spaces/dashes ignored), 5 to 15 characters
   (error outside), WARN outside 6 to 9, stored upper-case; normalise phones (71234567 / 26771234567 / +26771234567 -> +26771234567) and flag
   invalid ones; report errors by row number; upsert by employee_code if present, else by normalised
   first_name+surname; show a PREVIEW before saving; offer a downloadable template file.
2. **Login**: single admin to start; hashed password, session cookie, CSRF protection, login rate limiting.
3. **Batch**: HR creates a batch (pay month label e.g. "November 2026") and uploads MANY PDFs (multi-select or
   one ZIP). Files are named `name_surname.pdf` (no codes). Matching: normalise names (lowercase, strip accents,
   split filename on `_ - .` and spaces, drop extension and any digits) and compare as a SET of words, so
   `thabo_molefe.pdf` and `molefe_thabo.pdf` both match "Thabo Molefe". Per-file result:
   - **exact**: word set equals exactly ONE employee's first_name+surname -> auto-matched, pre-selected
   - **needs review**: partial/fuzzy/typo/middle names, or same name shared by >1 employee -> best suggestions
   - **unmatched**
   HR must confirm everything that is not exact. Cross-check: read PDF text with pypdf; if the matched
   employee's full name is NOT in the text but another employee's name is, flag a possible mismatch; if the PDF
   has no text (scanned), skip the check and say so.
4. **Preview screen**: every file with its match, match status, and the LAST 3 DIGITS of the phone; flags:
   unmatched file, employee without phone, two files for one employee, active employees with no file. HR can fix
   matches. NOTHING is sent until HR clicks Send.
5. **Send** (PDF password = the last 4 CHARACTERS of the Omang or passport number, UPPERCASE, case-sensitive,
   e.g. passport AB1234567 -> `4567`, passport ab12cd34 -> `CD34`): password-protect each PDF with AES-256 using the employee's `id_last4`, upload to the WhatsApp
   media endpoint, send a template message. Template name from env `WHATSAPP_TEMPLATE_NAME` (default
   `payslip_`), language `en`, header = the PDF document, body params by position: {{1}} = employee full name,
   {{2}} = pay month label. A second mode sends a plain document message (no template) for testing inside the
   24-hour window (`WHATSAPP_SEND_KIND=document`).
6. **Status tracking**: webhook `/webhook/whatsapp` — GET for Meta verification (verify token); POST for
   delivery/read/failed statuses, verifying `X-Hub-Signature-256` with the app secret. Statuses: pending,
   queued, sending, sent, delivered, read, failed, uncertain (+ error code/message). HR can retry failed ones, and uncertain
   ones only explicitly (see "Sending").
7. **WHATSAPP_MODE**: `mock` (default; simulates sends and statuses locally, no network) or `live`.

## Payslip batches (built: upload, matching, review, confirm)
Code: `routers/batches.py`; services `batch_ingest` (upload + ZIP safety), `batch_storage` (private files), `matching`
(file name -> employee), `pdf_check` (PDF text), `batch_jobs` (background matching), `readiness` (row problems + ready
rules), `batch_review` (HR actions + audit). Pages (all behind `require_login`, nav item "Batches"): `/batches` (list),
`/batches/new`, `/batches/{id}` (review), `/batches/{id}/confirm` (final summary + "I have checked the matches" +
Mark ready). The Send button is disabled until the batch is ready, then opens the send confirmation (see "Sending").
- **Matching (FINAL design, `services/matching.py`).** A file name becomes a SET of words: extension removed first, then
  lowercase, accents stripped, apostrophes dropped, split on anything that is not a letter (`_ - .` space), digits dropped
  (so `Thabo_Molefe_2.pdf` still matches; the test-PDF script relies on this). Each ACTIVE employee has the word set of
  first_name + surname (inactive employees are never auto-matched). Result per file:
  - `exact`: the sets are equal for exactly ONE employee -> auto-matched (`match_method=filename_exact`, confirmed).
  - `needs_review`: typo, partial name, middle names, or the same name shared by 2+ employees -> NOT assigned; top 3
    suggestions with a similarity score 0-100 stored as JSON ids+scores in `payslips.suggestions` (only scores >= 50 are
    suggested; shared names list every owner at 100).
  - `unmatched`: nobody scores 50 or more. Score = 100 * 2 * (sum of pair ratios) / (words in file + words in name);
    words pair one-to-one, difflib ratio >= 0.8 counts as the same word (typo tolerance). Speed: per distinct file word
    cache against the distinct employee words, only employees owning a close word are scored (600 files x 300 employees
    runs in well under a second; a test pins < 3 s).
  - PDF text cross-check (`pdf_check.py`, pypdf, first 3 pages, only for exact matches): matched employee's first AND
    surname words all in the text -> `ok`; else another ACTIVE employee's full name is in the text -> `possible_mismatch`
    (needs review: `confirmed=False`, shows whose name, HR must Confirm or change it); fewer than 3 words of text ->
    `no_text` (information only: "Could not verify against PDF content"); text without any employee name ->
    `name_not_found` (information only). When HR picks the employee by hand the check re-runs for the new person, the
    result is shown as a note but HR's choice stands (`confirmed=True`).
  - Every match HR changes or accepts is `match_method="manual"` (audit `batch_match_changed`, ids only).
- **Row display status** (`readiness.display_status`): invalid / pending / exact / confirmed (by HR) / needs_review /
  possible_mismatch / unmatched. `match_status` in the DB: pending, invalid, exact, needs_review, unmatched.
- **Readiness problems are computed LIVE** from current employee data (not stored): invalid file (reason), still checking,
  not matched, not confirmed / possible mismatch, employee inactive, no phone, no `id_last4` (password), another file in the
  batch for the same employee, same file name uploaded twice. Editing an employee fixes the batch without a re-run.
- **Ready rule** (`ready_blockers` + `mark_ready`): batch is draft, files not purged, matching finished, at least one file,
  and EVERY file (invalid ones too: remove them) has no problems, AND the checkbox was ticked. Employees without a file are
  allowed (listed under "Active employees with no payslip in this batch"). Status values: draft, ready (+ sending,
  completed for later). `Reopen` returns ready -> draft. A ready, purged or still-processing batch refuses edits.
- **Upload rules** (`batch_ingest.py`, settings `MAX_FILES_PER_BATCH` 300, `MAX_UPLOAD_MB` 10 per PDF,
  `MAX_BATCH_TOTAL_MB` 200, `MAX_UNZIPPED_MB` 250). Files stream to disk in chunks (never fully in memory). A bad FILE
  (not .pdf, empty, no `%PDF-` magic, pypdf cannot open it, password-protected, over 10 MB) becomes an "Invalid file" row
  with a plain reason and is NOT stored. A bad UPLOAD (too many files, batch too big, bad/encrypted ZIP, nested archive,
  zip bomb, unsafe paths) is rejected as a whole and everything it wrote is removed (a new batch is not even created).
  ZIP: entry paths are never trusted: `..`, absolute, drive-letter or NUL paths reject the whole ZIP; folders, hidden
  files, `__MACOSX`, Thumbs.db/desktop.ini are ignored; names are flattened to the base name. Zip-bomb guard = declared
  total, expansion ratio (> 100x on entries > 5 MB), entry count (5000) and actual unpacked bytes. Duplicate file names
  are kept and flagged. `body_limit.UPLOAD_PATHS` gives `/batches/new` and `/batches/<id>/upload` the batch-total limit.
  Starlette may spool big multipart bodies to the OS temp folder while parsing (PDFs only; employee imports never do).
- **Private storage**: `PRIVATE_STORAGE_DIR/batches/<batch id>/<random uuid>.pdf` (`batch_storage.py`); never under
  `app/static`, never served; the original name exists ONLY in the database (`payslips.original_filename`);
  `stored_path()` refuses any name that is not `<32 hex>.pdf`. Removing a file, deleting/cancelling a batch deletes the
  files. Retention: `python -m app.scripts.purge_old_batches` deletes stored files of batches older than
  `BATCH_RETENTION_DAYS` (30) (keeps the rows, sets `files_deleted_at`, skips batches that are `sending`, removes orphan
  folders). Run it from a daily scheduler once deployed.
- **Background matching**: after the upload response, FastAPI `BackgroundTasks` runs `batch_jobs.process_batch`
  (own DB session from the `get_session_factory` dependency; tests override it with their in-memory factory, and
  TestClient runs the task before returning). Progress lives on the batch row (`processing_state` processing/done/failed,
  `processing_total`, `processing_done`, `processing_heartbeat_at`); the page shows `<progress>` and auto-refreshes with
  `<meta http-equiv="refresh">` (no JS). No progress for 2 minutes = interrupted -> state failed -> "Re-run matching"
  (keeps manual choices). Edits are refused while processing. Single-process assumption, as for the import preview.
- **Review UI**: summary bar of 6 counts, problem rows first, "Need attention" filter, per-row actions: accept a
  suggestion (one click), Change match (searchable `<datalist>` of active employees: option value `Full Name (#id)`,
  parsed server-side by the `(#id)` suffix), Confirm match, Remove; "Add more files" panel. Phones show `***567`
  (`phones.mask_phone`, last 3 digits). No inline scripts/styles anywhere (a test checks the batch pages).
- **Privacy**: never log or audit file names, employee names or phones. Flash messages carry counts only. Audit actions:
  batch_created, batch_files_uploaded (`accepted= invalid= ignored=`), batch_match_changed, batch_match_confirmed,
  batch_file_removed, batch_marked_ready, batch_reopened, batch_deleted, batch_files_purged (ids/counts only).
- **Tables**: migration `c4a7e1d92b50` adds only columns (existing batches become `processing_state='done'`):
  batches `ready_at, files_deleted_at, processing_*`; payslips `page_count, invalid_reason, match_score, suggestions,
  text_names_employee_id`. `payslips.employee_id` is the matched employee. Tested in tests/test_batch_tools.py.
- **Test data**: `python -m app.scripts.make_test_payslips` (needs fpdf2 from requirements-dev) reads ACTIVE employee NAMES
  from the database and writes fake PDFs + a ZIP + WHAT_TO_EXPECT.txt into `test_payslips/` (git-ignored): exact, swapped
  order, typo, middle name, unknown, same-name (if two employees share a name), file-name-vs-text mismatch, image-only,
  password-protected, non-PDF named .pdf. It draws "exact" cases from employees with unique names.

## Sending (built: protection, WhatsApp client, worker, progress, webhook)
Code: `services/pdf_protect.py`, `whatsapp.py` (client), `whatsapp_errors.py` (explanations), `sender.py` (queue + worker),
`send_status.py` (the ONE status code path), `send_view.py` (progress/JSON/CSV); routers `sending.py` and `webhook.py`;
`static/progress.js`; scripts `fake_webhook`. Default `WHATSAPP_MODE=mock`. Tests NEVER use live mode or the real network
(`conftest.py` blocks `httpx.HTTPTransport`, sets mock mode, and `LiveWhatsAppClient` refuses to be built without a mocked
transport while `PYTEST_CURRENT_TEST` is set).
- **PDF protection** (`protect_pdf`): pypdf AES-256 (V5/R6), user password = `id_last4` (uppercase, case-sensitive), random
  owner password never stored, built IN MEMORY (bytes) and dropped after the upload; no protected copy ever touches disk.
  VERIFIED before use: must be AES-256, refuse no/wrong/wrong-case passwords, open with the right one; otherwise ProtectError
  and nothing is sent (never falls back to the unprotected file). No `id_last4` -> the row fails with a clear reason.
- **Client interface** (`WhatsAppClient`): `check_recipient`, `upload_media(bytes, filename)`, `send_template(...)`,
  `send_document(...)`; errors are `WhatsAppError(kind, code, message, http_status, retry_after)` with kind transient /
  ambiguous / auth / permanent (`str(error)` = kind:code only). `build_client(settings, status_sink)` picks mock or live;
  live refuses (WhatsAppConfigError) if a required setting is missing. `Settings` ALSO refuses to start in live mode when
  `WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN, GRAPH_API_VERSION` is empty,
  a `your-...` placeholder, or the version is not like `v23.0` (names only in the message). `WHATSAPP_ALLOWED_RECIPIENTS`
  (comma-separated E.164) is validated at startup and enforced by the live client AND by the worker before anything is
  uploaded (`LOCAL_NOT_ALLOWED`). httpx/httpcore logging is forced to WARNING so URLs are never logged.
- **Live requests** (checked against Meta's docs, Nov 2026). Media: `POST {WHATSAPP_BASE_URL}/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/media`,
  `Authorization: Bearer`, multipart with `messaging_product=whatsapp`, `type=application/pdf` AND the file part typed
  `application/pdf` (Meta's table lists `type` as required but its own example only types the file part: both are sent);
  answer `{"id": ...}`. Message: `POST .../messages` JSON `{messaging_product, recipient_type: "individual", to: "+267...",
  type: "template", template: {name, language: {code}, components: [header(document: id + filename), body(2 positional
  text params, no parameter_name)]}}`; answer `messages[0].id` (`message_status` accepted/held/paused is optional). Plain
  document: `type: "document", document: {id, filename, caption}` (24-hour window only; error 131047 outside it).
  Header file name `Payslip - <pay month>.pdf`. Template params are whitespace-collapsed (no newlines/tabs).
  NOT CONFIRMED by any Meta page I could read: the document-header SEND shape for templates (the id+filename form above is
  the documented DIRECT-SEND document header and the usual template form; verify with one real send to your own number),
  send-time limits on text parameters, error 131030 ("recipient not in allowed list": absent from Meta's current error
  page; kept in the explanations because Meta's test numbers do use it), request timeouts, and any idempotency key
  (Meta has none: that is why uncertain rows exist). Meta's throughput is 80 msg/s by default; its pair rate limit is 1
  message / 6 s to the same number (131056); its suggested backoff is 4^attempt seconds.
- **Worker rules** (`sender.py`): statuses pending, queued, sending, sent, delivered, read, failed, uncertain.
  Start = `start_send` (batch must be `ready`, the pay month typed exactly, 0 readiness problems): pending -> queued, batch
  `sending`, `send_state=running`, `send_mode` recorded. The worker CLAIMS rows with an atomic UPDATE queued -> sending and
  COMMITS before any network call. `SEND_CONCURRENCY` (default 5; 1 runs inline) threads, `SEND_INTERVAL_MS` gap between
  request starts (all workers), `SEND_MAX_RETRIES` (3). Automatic retry with backoff (1, 4, 16 s or Retry-After) ONLY for
  HTTP 429 / 5xx / cannot-connect (nothing was sent). Ambiguous results (timeout/connection dropped after sending, unreadable
  answer, or 5xx that never clears on the MESSAGE call) -> `uncertain`, NEVER retried automatically. Permanent errors ->
  `failed`. Auth/permission errors (190, 0, 102, 3, 10, 131005, 200, HTTP 401) fail the row and HALT the batch
  (`send_state=halted`, banner, queued -> pending, audit `send_token_error`). Rows can only be queued by: Send/Resume
  (pending), Retry failed / per-row Retry (failed), Retry selected uncertain (uncertain + the "may be delivered twice" box).
  sent/delivered/read/sending/queued rows can never be queued again (tests pin this). Stop: queued -> pending,
  `send_state=stopped`, in-flight rows finish; Resume re-queues pending. A batch becomes `completed` when nothing is
  pending/queued/sending. On app start (`main.lifespan` -> `recover_after_restart`): `sending` -> `uncertain`
  (`LOCAL_RESTART`), queued rows of a running batch resume. One worker thread per batch (`SendLauncher`); tests override
  `get_send_launcher` with `InlineLauncher` (runs the worker in the request, no real waiting, mock events COLLECTED in
  `app.state.mock_events` and fired with `fire_events(app)`).
- **Status path** (`send_status.py`): the worker, the mock client and the webhook ALL call `apply_status_event`/`apply_status`.
  Rules: forward only (sent < delivered < read), `read` without `delivered` sets both timestamps, `failed` only from
  sending/uncertain/sent (never over delivered/read), repeats change nothing, timestamps set once, a report resolves an
  `uncertain` row and clears its error. The worker sets `sent` when Meta ACCEPTS the message (the mock also emits a `sent`
  event: a no-op). Out-of-order delivery is not described in Meta's docs, hence the rules.
- **Errors**: store Meta's code in `payslips.error_code` and the (digit-masked) message in `error`; own checks use
  `LOCAL_*` codes (`whatsapp_errors.py`). The UI shows `explain(code, message)`: plain text for 131026 (not on WhatsApp),
  131030/LOCAL_NOT_ALLOWED (allowed list), 132000/132012 (parameter mismatch), 132001 (template missing/unapproved),
  132015/132016/132007/132005, 130429/80007/4/131056/131048 (rate limits), 190/0/102/3/10/131005/200 (token/permissions),
  131047 (24-hour window), 131049/131050/131021/131031/368/131037/131042/131045/133010/131053/131000/131016/133004 and the
  local ones; UNKNOWN codes show `WhatsApp returned error <code>: <message>`. Add new codes there.
- **Mock mode** (`MockWhatsAppClient`): recipient number ending **000** = accepted, then a `failed` status 131026 (not on
  WhatsApp); **111** = HTTP 429 every time (backoff, then `failed` 130429); **222** = timeout (`uncertain`); others =
  sent, then delivered, then read after random delays (0.5-1.5 s, 1-3 s, 2-5 s) through the webhook's status path. A MOCK
  banner shows on every page (`live_whatsapp_mode()` template global); the send confirmation shows a big MOCK/LIVE banner.
- **Pages** (login + CSRF): `/batches/{id}/send` (confirmation: counts, MOCK/LIVE banner, allowlist warning, a PREVIEW of what each employee gets (`sending.message_preview`: sample name "Thabo Molefe", pay month, file name, password rule; template wording from `WHATSAPP_TEMPLATE_PREVIEW_TEXT`, escaped; never a real employee), type the pay month),
  POST `.../send`, `.../stop`, `.../resume`, `.../retry-failed`, `.../payslips/{id}/retry`, `.../retry-uncertain`
  (checkboxes via the `form=` attribute + `understood`), GET `.../status.json` (poll: counts + per-row status/label/detail;
  NO names, phones or IDs), GET `.../report.csv` (employee name, masked phone, status, UTC times, error code + explanation;
  formula-injection guarded; no IDs/full phones). While a batch is `sending`/`completed`, `/batches/{id}` renders
  `batch_progress.html` (counters, table, Stop/Resume/Retry, uncertain warning). `static/progress.js` polls every 2 s by
  changing text only (one `fetch`, URL from `data-status-url`, no inline script) and reloads once when a row enters/leaves
  failed/uncertain or the send state changes (to show the right buttons); `<noscript>` meta refresh as fallback.
- **Webhook** (`routers/webhook.py`, no login, CSRF-exempt): GET handshake needs `hub.mode=subscribe` and
  `hub.verify_token` == `WHATSAPP_VERIFY_TOKEN` (constant time) and echoes `hub.challenge` (else 403). POST: raw body bytes,
  `X-Hub-Signature-256` must be `sha256=` + HMAC-SHA256(raw body, `WHATSAPP_APP_SECRET`) compared with `hmac.compare_digest`;
  otherwise 403 BEFORE parsing (empty secret = reject all), audit `webhook_rejected` (`reason=bad_signature`, at most one per
  minute). Valid: every `entry[].changes[].value.statuses[]` goes through `apply_status_event`; `value.messages` (customers'
  messages) are never read or logged; odd/unknown payloads still get 200; body limit 2 MB (Meta batches up to 1000 updates;
  it retries unacknowledged posts for 7 days and may duplicate: hence idempotency). `python -m app.scripts.fake_webhook`
  sends correctly signed fake events (also `--bad-signature`, `--verify`, `--status failed --error-code 131026`).
- **Audit actions**: send_started (`files= mode=`), batch_stopped, send_resumed, retry_clicked (`kind= count=`),
  webhook_rejected, send_token_error (`code=`), report_downloaded (`rows=`). Counts/codes only; logs hold counts, ids and
  exception types only (tests assert tokens, full phones, names and message ids never appear in logs/pages/JSON).
- **Preflight** (`python -m app.scripts.check_live_config`, `scripts/check_live_config.py`): sends NO message; only two kinds of GET
  requests (a test pins this, and that the source has no POST/`/messages`): `GET /{ver}/{WHATSAPP_PHONE_NUMBER_ID}?fields=id,
  display_phone_number,verified_name,quality_rating,code_verification_status,name_status` and `GET /{ver}/{WHATSAPP_BUSINESS_ACCOUNT_ID}/
  message_templates?fields=name,language,status,category,parameter_format,components&limit=100` (fields/values checked against Meta's
  reference pages, Nov 2026). Meta documents NO name/language/status filter on the list, so ALL pages are read (`after` cursor, max 20
  pages) and filtered locally by exact name; `paging.next` is NEVER followed or printed because Meta puts the access token in that URL.
  It reads the WhatsApp settings straight from `.env` + environment (`load_env`, only those names, no validation), so it works while
  mode is still mock or settings are incomplete (the app itself refuses to start in live mode in that case). Checks: 8 settings present
  (placeholders `your-...` count as missing; IDs digits only; version `v23.0` style; names only, never values); token + phone id
  (masked number `***567`, quality GREEN/YELLOW/RED/NA, name status, code verification); template exists, language matches EXACTLY
  (suggests the exact code or lists approved languages and which setting to change), status APPROVED (help text per status:
  PENDING/REJECTED/PAUSED/DISABLED/...), plus ADVISORY warnings (no DOCUMENT header, not 2 positional body variables: inferred from the
  documented component schema, not a Meta example); allowed recipients (count only; invalid entries FAIL; unset = WARN). Output
  `[PASS]/[FAIL]/[WARN]/[INFO]/[SKIP]`, `RESULT: PASS|FAIL`, numbered next steps, exit code 0/1. Meta error text is scrubbed (secrets
  -> `[hidden]`, long digit runs masked). A rejected token skips the template check; a wrong phone id does not. In tests `main()` refuses
  to build a real HTTP client. `WHATSAPP_BUSINESS_ACCOUNT_ID` is a new setting used ONLY by this command (live startup does not need it).
- **Going live checklist**: run the preflight until RESULT: PASS, set the five live settings, put your own test number in `WHATSAPP_ALLOWED_RECIPIENTS`, make sure
  the template (`WHATSAPP_TEMPLATE_NAME`, language) is APPROVED with a DOCUMENT header and two body variables, point Meta's
  webhook at `https://<host>/webhook/whatsapp` with the verify token, send ONE batch to yourself, then clear the allowlist.
- **Tables**: migration `e2a6d5b81f39` adds batches `send_state, send_mode, halted_reason, stopped_at, completed_at` and
  payslips `error_code` (columns only; existing rows kept; tested). Batch `status`: draft, ready, sending, completed.

## Database tables
users, employees (with indexed normalised `name_key` for matching), batches, payslips (batch, employee nullable
until matched, original filename, stored/encrypted filenames, match_method, match_status, confirmed, pdf_check,
status, whatsapp_media_id, whatsapp_message_id, error, attempts, timestamps), audit_log (who/what/when),
login_attempts (rate limiting). See `app/models/`. Migrations in `alembic/versions/`.

## Security rules (non-negotiable)
- **Full ID numbers (owner's decision, replaces the earlier "never store the full ID" rule).** HR wants to see and keep the
  full Omang/passport number, so it IS stored, but ONLY as `employees.id_number_encrypted` (Fernet = authenticated
  encryption, key `ID_ENCRYPTION_KEY` in `.env`; `app/services/id_crypto.py`). Safeguards that make this acceptable, all
  enforced by tests (`tests/test_full_ids.py`): the key never lives in code/git/logs/screens; the full ID is NEVER
  in page HTML by default, in logs, audit details, session, flash messages, error messages, the import preview, the
  template or any export; the ONLY way to read it is `POST /employees/reveal-ids` (login + CSRF + audited with
  `ids_revealed` + rate-limited + `Cache-Control: no-store`). There must be no export/CSV/API that returns full IDs.
  Plain text elsewhere is still only `id_last4` (+ `id_type`). Never decrypt except through `id_reveal.reveal` (and the
  import planner's in-memory comparison).
- No secrets in code. Everything in `.env`; `.env.example` has placeholders only; `.env` is git-ignored.
- Never log access tokens, passwords, or full phone numbers. Mask phones in logs/screens (show last 3 digits).
- Uploaded and encrypted PDFs live in the private folder (`PRIVATE_STORAGE_DIR`, default `private_storage/`)
  outside any public/static path; validate they are real PDFs (magic bytes `%PDF-`, size limit); delete after
  sending (`SENT_RETENTION_DAYS`, default 7; see "Production deployment").
- Meta Graph API version is the setting `GRAPH_API_VERSION` — never hardcode it in several places.
- `/health` is public and returns only `{"status":"ok"}` / `{"status":"error"}` (503). It logs only the exception type.

## Users, roles and permissions (built)
Code: `security/permissions.py` (the ONE table), `services/users.py`, `services/one_time.py`, `routers/users.py`, `routers/account.py`;
CLI emergency path `services/admins.py` + `scripts/create_admin.py` / `reset_admin_password.py`.
- **Roles** (`users.role`, default `viewer`; the migration made every EXISTING user an admin): `admin` = everything incl. Users, Audit
  log, System status. `hr` = employees (view/add/edit/import/deactivate, reveal IDs) + batches (create/upload/review/send/retry).
  `viewer` = read-only: employee list with IDs ALWAYS masked (no `data-secret`, no `data-id-slot`, no last 4, no password column, no
  reveal button/form), batches and send progress/report; cannot change or send anything.
- **Enforcement is on the SERVER.** Every protected route declares `dependencies=[Depends(require("<permission>"))]` (routers whose
  every route shares one permission put it on the router: users/audit/system/account). `require()` logs the user in (303 to /login),
  then 403 + audit `permission_denied` (`permission= method=`) unless the role has it. Permissions: dashboard.view, account.self,
  employees.view, employees.edit, ids.reveal, batches.view, batches.manage, batches.send, users.manage, audit.view, system.view.
  `can(user, perm)` is also a Jinja global (it only HIDES buttons). NEW ROUTES MUST DECLARE A PERMISSION: `tests/test_roles.py` fails
  otherwise, and runs every role against every route (`test_every_role_against_every_route`: denied = 403, allowed = anything else).
  Existing safety net `test_every_route_requires_login_except_the_public_ones` also still applies (public: /health, /login,
  /webhook/whatsapp).
- **Users page** (`/users`, admin): list (name, username, role, Active/Disabled, Locked out, Temporary password, last login), search,
  add (username lower-cased, unique case-insensitively, 3-150 chars; full name; role), edit name/role, disable/enable, reset password,
  unlock, sign out everywhere. Risky actions use the confirm dialog (`data-confirm`).
- **Temporary passwords**: `generate_temp_password()` = 16 chars from an unambiguous alphabet in 4 groups; only its Argon2 hash is stored;
  the plain text goes through `one_time.temp_passwords` (process memory, tied to the creating admin, ONE read, 5 minute TTL), shown on
  `/users/temp-password?t=...` after a redirect (so refreshing cannot repeat the action; a second visit says it was already shown). Never in
  the database, cookie, audit details or logs (tests assert this). Create/reset set `must_change_password` and bump `session_version`.
- **Forced change**: `require_login` redirects a user with `must_change_password` to `/account/password` from every page except that
  page, `/logout` and static files (`PENDING_ALLOWED_PATHS`); login redirects there too. `change_own_password`: current password
  required (wrong tries are rate-limited with `login_limiter` key `pwchange:<user>`), new password through `password_problem` (>= 12
  characters etc.), must differ from the current one, must match the confirmation; success clears the flag, sets
  `password_changed_at`, bumps `session_version` (every OTHER session ends) and starts a fresh session for this browser.
- **Sessions**: the cookie also carries `sv` = the user's `session_version`; a mismatch ends the session. Bumped by sign-out-everywhere,
  disable, role change, password change/reset. (`password_hash` fingerprint still ends sessions on a CLI reset.)
- **Safeguards** (in `services/users.py`, so they hold for every caller; tested at service AND route level): the last active admin can
  never be disabled or demoted; an admin can never disable or demote THEMSELVES (they may rename themselves); usernames unique and
  case-insensitive; every action audits (acting user_id, `entity_type=user`, `entity_id`=target; actions user_created / user_updated
  (`role a->b`, `name`) / user_disabled / user_enabled / user_password_reset / user_unlocked / user_sessions_revoked / password_changed),
  never a password. Locked out = >= LOGIN_MAX_ATTEMPTS failures from one address inside the lockout window (`locked_usernames`); Unlock clears them.
- **My account** (`/account`, any role): details, role, last password change, last 8 `login_success` audit rows (masked addresses),
  link to `/account/password`.
- **CLI (emergency path)**: `create_admin [--role admin|hr|viewer] [--name ..] [--must-change]` and `reset_admin_password [--enable]
  [--must-change]` (re-enables a disabled account, ends sessions, clears lockout). Passwords typed at the keyboard are not forced to
  change unless `--must-change`.

## Audit log and System status (built, admin only)
- `/audit` (`routers/audit_page.py`, `services/audit_view.py`): filters (from/to date inclusive, person incl. "system", action),
  free-text search (action, details, target type, username; `%` and `_` are literal), 50 per page (page clamped), newest first (by id),
  plain-language labels (`LABELS`, unknown actions are humanised). `/audit/export.csv` exports the SAME filters (cap 50,000, spreadsheet
  formula guard, no-store) and writes `audit_exported` (`rows=N`). Everything shown or exported goes through `scrub()`: digit runs of 7+ ->
  `***`, token-like strings -> `[hidden]`, on top of audit rows never holding IDs/tokens/full phones in the first place.
- `/system` (`routers/system_page.py`, `services/system_status.py`): read-only. Big MOCK/LIVE block, template name/language/kind, allowed-
  recipients list active + COUNT, "Settings present" yes/no only (placeholders `your-...` count as missing), last SIGNED webhook event
  (`system_state` table, key `last_webhook_at`, written only after the signature verified), database health (`SELECT 1`), app version
  (`app.__version__`), batches being prepared / being sent / old batches still holding files. No secret value is ever rendered; settings
  stay in `.env`.

## Production deployment (built; guides: DEPLOY.md and the README section "Putting it on a server")
Target: Ubuntu 24.04 on AWS EC2, Caddy (HTTPS) in front, MariaDB on the same machine, ONE app process. Code: `app/server.py`,
`logging_setup.py`, `single_instance.py`, `services/housekeeping.py`, `scripts/production_check.py`, `config.py` (production rules);
files in `deploy/`. NONE of the `deploy/` files has run on a real Ubuntu server or Caddy yet (written on Windows; tests check syntax with
`bash -n` and pin the safety properties as text): treat the first real deployment as the real test.
- **APP_ENV=production** (`Settings._production_rules`, shared helpers `session_secret_problem`/`encryption_key_problem`/`database_url_problem`
  also used by production_check): the app REFUSES TO START (messages name settings, never values) when SESSION_SECRET is missing/placeholder/
  predictable, ID_ENCRYPTION_KEY is missing/placeholder/readable text (a real key needs >= 16 distinct bytes: the test key is rejected on purpose),
  DATABASE_URL is a placeholder or not mysql/mariadb, `SESSION_COOKIE_SECURE=false`, or `APP_HOST` is not 127.0.0.1/localhost/::1. Secure cookies
  default on in production; HSTS is sent when `secure_cookies`. `create_app(settings=None)` always has `debug=False` and NO /docs, /redoc,
  /openapi.json in ANY mode (the role matrix test would otherwise see them). Dev stays easy: placeholders are fine when APP_ENV=development.
- **Exactly ONE process.** `python -m app.server` (what systemd runs) starts uvicorn with workers=1, no reload, `proxy_headers=True`,
  `forwarded_allow_ips="127.0.0.1"`, `access_log=False` (uvicorn's lines would carry query strings), `log_config=None` (our logging decides),
  host/port from `APP_HOST`/`APP_PORT`; it refuses `WEB_CONCURRENCY` != 1 and prints bad settings as one plain "Refusing to start: ..." line.
  In production `main.lifespan` also takes an exclusive file lock (`single_instance.py`: fcntl on Linux, msvcrt on Windows, file
  `PRIVATE_STORAGE_DIR/.instance.lock`; the OS drops it when the process dies, so restarts always work); a second process raises
  `AlreadyRunning`. Reason (README "Why exactly one process"): the send engine, preview store and one-time passwords live in memory, and start-up
  recovery would mark another process's live sends `uncertain`. Development takes no lock (so `run.ps1 --reload` and tests are unaffected).
- **Start-up recovery (every start, idempotent):** `reset_interrupted_matching` (processing -> failed), `recover_after_restart` (sending ->
  uncertain `LOCAL_RESTART`, queued rows of a running batch resume; returns exactly `{"uncertain","resumed"}`: tests assert that dict), both inside
  try/except that logs only the exception type so a not-yet-migrated database cannot stop the app from booting. `housekeeping.loop` (asyncio
  task, every 300 s via `to_thread`) purges expired import previews and one-time passwords (`purge_expired()` on both stores) and old
  `login_attempts` (`login_limiter.purge_old`).
- **Logging** (`logging_setup.configure_logging`, called at import of `app.main`, idempotent, only removes handlers it added): stdout always + an
  optional `RotatingFileHandler` in `LOG_DIR` (`payslip-sender.log`, `LOG_FILE_MAX_MB`, `LOG_FILE_BACKUPS`, chmod 600); `LOG_LEVEL`,
  `LOG_FORMAT` auto|json|text (auto = json in production). EVERY line passes `redact()` (7+ digit runs, Bearer, `EAA` tokens, `sha256=`, Fernet
  `gAAAA`, `password|secret|token|api_key|authorization = value`, 40+ char token-like strings); the text formatter redacts only message and
  traceback (redacting the whole line used to turn the date into `***`). The rule that matters: log calls pass only exception TYPES and counts
  (`tests/test_production.py` parses every `log.*()` call in `app/` and fails on anything else, and bans `print()` outside scripts). Do not add log
  calls that format names, phones, ids of people, file names or PDF text.
- **Retention:** `SENT_RETENTION_DAYS` (default 7; replaced `RETENTION_HOURS`): `purge_old_batches.purge()` deletes the stored PDFs of a batch
  `completed` more than N days ago UNLESS it still has `failed`/`uncertain` payslips (HR may retry); everything else only at `BATCH_RETENTION_DAYS`
  (30) after creation; `sending` batches are never touched; orphan folders are removed only when older than `orphan_grace_seconds` (3600; an upload
  in progress is not an orphan). `purge()` still returns exactly `{"batches","files","orphan_folders"}`; `main()` also purges login attempts.
  Production runs it from `payslip-purge.timer` (daily ~03:10).
- **`production_check`** (`python -m app.scripts.production_check [--url https://host]`): read-only, injectable `Probes` (settings, db, head
  revision, disk, file_info, http_get, is_posix, euid) so tests use fakes; reads raw `.env` + environment like `check_live_config`, reuses its
  `Report`/`PASS|FAIL|WARN|INFO|SKIP`/`RESULT:`/next-steps style and exit code. Checks: APP_ENV, one worker, Secure cookies, secret/key/URL strength
  (never printed), "app would start" (shows the validator's reasons), `.env` mode (FAIL if group/other bits; skipped on Windows), WHATSAPP_MODE
  (mock = loud WARN; live + `WHATSAPP_ALLOWED_RECIPIENTS` still set = loud WARN, numbers never printed; unknown = FAIL), DB reachable (plain-language
  MySQL error codes) and migrated to alembic head, storage (exists, writable, 700, not under app/static), free disk (FAIL < 1 GB, WARN < 5 GB), /docs
  off in the built app and on the running one (`http://127.0.0.1:APP_PORT/docs` = 404), and with `--url` health + HSTS + /docs + http->https redirect.
  Under pytest `main()` refuses real probes. On a dev PC it FAILs for APP_ENV: expected.
- **`deploy/`** (install paths: code `/opt/payslip-sender` owned by `payslip`, `.env` there mode 600, data `/var/lib/payslip-sender/storage`, logs
  `/var/log/payslip-sender`, backups `/var/backups/payslip-sender`, config `/etc/payslip-sender/`): `payslip-sender.service` (User=payslip,
  `python -m app.server`, `Restart=on-failure`, `Wants=` not `Requires=` MariaDB so a DB restart never restarts the app mid-send, sandboxing with
  `ProtectSystem=strict` + `ReadWritePaths`), `payslip-purge.{service,timer}`, `payslip-backup.{service,timer}`, `Caddyfile.template`
  (`__DOMAIN__`/`__EMAIL__`; HSTS, `encode`, `-Server`, JSON access log with the query string/cookies/authorization removed, `request_body` 230 MB for
  `/batches/new` and `/batches/*/upload` (app limit = `MAX_BATCH_TOTAL_MB` + 20 MB overhead; a test pins Caddy >= app) and 3 MB for everything else,
  and a COMMENTED `@outsiders` block that restricts everything except `/webhook/whatsapp` and `/health` to listed IPs), `setup-server.sh` (idempotent:
  packages, official Caddy repo, unattended-upgrades, ufw 22/80/443 with SSH allowed before enabling, journald persistent, `payslip` system user,
  MariaDB on 127.0.0.1 with its own DB+user and a random password that is only ever written to `.env` and `/etc/payslip-sender/db-backup.cnf` (SQL via
  stdin, never printed or on a command line), venv + pinned packages, `generate_secret`/`generate_encryption_key`, `alembic upgrade head`, units,
  Caddyfile written once (an edited one is kept) and `caddy validate`d before reload; the app is NOT started), `update.sh` (refuses during a send,
  code tarball + encrypted DB backup, `git pull --ff-only` or `--archive`, pinned install, migrations, restart, `/health` poll, prints rollback commands;
  `--skip-backup`, `--force`), `backup.sh` (mysqldump `--single-transaction` | gzip | `age -R` public keys in `/etc/payslip-sender/backup-recipients.txt`;
  REFUSES to run without a key so no plaintext dump ever exists; 14 days; optional `S3_URI`), `restore.sh` (default: into scratch DB
  `payslip_restore_test`, prints row counts, drops it; `--live` asks to type RESTORE and saves the current DB first), `app-run.sh` (runs an `app.*`
  module as `payslip` from the app folder: the way to run any app script on the server), `env.production.example`. `.gitattributes` forces LF for
  `*.sh` and `deploy/*` (CRLF breaks bash on Linux). Tests: `tests/test_deploy_files.py` (static), `test_production.py`, `test_production_check.py`.
- **ID_ENCRYPTION_KEY and backups:** the key is NOT in the dumps and must be stored separately from them (and from the age private key, which is
  never on the server except briefly in `/dev/shm` for a restore test). Documented in README, DEPLOY.md, backup.sh. Dumps contain Fernet ciphertext of ID numbers.
- **Dependencies:** `requirements.txt` / `requirements-dev.txt` pin EXACT versions (`==`, also sub-dependencies; `colorama` win32-only, `uvloop`
  non-win32); a test rejects any `>=`/`~=`. All pins were verified to have Linux cp312 wheels (Ubuntu 24.04 has Python 3.12; the dev PC runs 3.15).
  `pip-audit` is a dev dependency; `.\dev.ps1 install|test|audit|check|all` wraps the commands. Last audit (23 Nov 2026): no known vulnerabilities.
  Keep SQLAlchemy `<2.1`.
- **Not done / assumptions to revisit:** no per-IP throttle on the webhook (Caddy IP restriction is optional and excludes the webhook on purpose);
  no fail2ban/CloudWatch; single server; code reaches the server by git clone or tarball (no CI/CD); S3 upload needs the AWS CLI (`--with-aws-cli`)
  and an IAM role; times in timers are server time (UTC).

## Authentication (built)
- Users are created by an ADMIN on the Users page (see "Users, roles and permissions"); the command line
  (`python -m app.scripts.create_admin`, `reset_admin_password`) stays as the emergency path. Passwords: min 12 chars, hashed with
  Argon2id (`app/security/passwords.py`).
- Session = signed cookie `payslip_session` (Starlette SessionMiddleware, key `SESSION_SECRET`), HttpOnly,
  SameSite=Lax, Secure via `SESSION_COOKIE_SECURE` (default: on when APP_ENV=production). Idle timeout 30 min,
  absolute 8 h (`app/security/sessions.py`). Session is tied to the password hash: a reset logs old sessions out.
- Protect pages with `APIRouter(dependencies=[Depends(require_login)])` (`app/security/auth.py`). A test fails if
  any route other than /health and /login lacks it. Not logged in -> 303 redirect to /login.
- CSRF: `verify_csrf` is an app-wide dependency on every POST/PUT/PATCH/DELETE. Put `{{ csrf_input(request) }}`
  inside every `<form method="post">`, or send an `X-CSRF-Token` header. `/webhook/whatsapp` is exempt (listed in
  `EXEMPT_PATHS` in `app/security/csrf.py`) and must verify Meta's X-Hub-Signature-256 instead.
- Rate limiting: 5 failed logins per username+IP -> 15 min lockout, stored in `login_attempts`
  (`app/services/login_limiter.py`). Same generic error for unknown user / wrong password; dummy hash keeps timing similar.
- Audit actions: login_success, login_failure, login_lockout, logout, admin_created, admin_password_reset (+ the user-management
  ones, permission_denied and audit_exported listed under Users / Audit).
  IPs are stored masked (192.168.1.x). Failed-login audit rows name the username only if it exists.
- Security headers on every response (`app/security/headers.py`): CSP (no inline scripts/styles: put CSS in
  `app/static/`), X-Frame-Options DENY, nosniff, Referrer-Policy, no-store, HSTS in production.
- Behind Caddy later: run uvicorn with `--proxy-headers --forwarded-allow-ips=127.0.0.1` so the real client IP
  is used for rate limiting. Limit request body size in Caddy (forms are parsed before login is checked).
- Known limits: no per-IP-only throttle (an attacker can try 5 passwords per username per 15 min); cookie
  sessions cannot be revoked server-side except by changing the password or rotating SESSION_SECRET.

## Employees (built)
- Pages (all behind `require_login`): `/employees` (search name/phone/code incl. accent-insensitive, number-like
  queries such as "71 234 567" match as one phone; filter active/inactive/all; 25 per page), `/employees/new`,
  `/employees/{id}/edit`, POST `.../deactivate` and `.../activate`. NO hard delete exists (a test enforces it).
  Add/edit BLOCKS a phone or ID number that belongs to another employee (error names the owner, never a value); a duplicate normalised NAME
  only warns; "Save anyway" works through a hidden `ack` fingerprint of the exact warnings shown. Edit also has "Remove the saved ID number"
  (`remove_id`) to free an ID.
- **Unique ID and phone (task 11).** `employees.phone` and `employees.id_number_hash` have UNIQUE indexes (`uq_employees_phone`,
  `uq_employees_id_number_hash`, declared in the model `__table_args__`), across ALL employees, active or not (a deactivated employee keeps their
  number until edited; owner's decision). NULLs may repeat. `id_number_hash` = `id_crypto.id_blind_index(number)` = HMAC-SHA256 of the NORMALISED
  number (`normalise_id`: upper-case, no spaces/dashes, NO id_type, so Omang "123456789" and passport "123456789" collide) keyed by
  HKDF(newest ID_ENCRYPTION_KEY, info `id-blind-index`): no extra secret. **Rotating the key changes every hash**: `reencrypt_ids` re-hashes in
  the same pass (`IdCipher.blind_index`). The hash is as private as the ciphertext: never shown, logged, exported (tests check). Three layers:
  `employees.validate_form` (`phone_owner` / `id_owner` / `clash_message`), `import_planner` (in-file duplicates are errors on EVERY involved row;
  a row whose phone/ID belongs to a DIFFERENT existing employee is an error; the same person re-imported is fine; `ParsedRow.id_hash` is
  `repr=False`), and the database (`IntegrityError` on commit is caught in `_save_form` and `import_confirm`: rollback, re-validate so the message
  names whoever won the race). `apply_values` now needs BOTH `id_number_encrypted` and `id_number_hash` keyword args (no defaults): to KEEP the stored ID
  pass both from the employee. Legacy rows keep a NULL hash until `python -m app.scripts.backfill_id_hash` (decrypts in memory, prints names and
  employee ids only, exit 1 while duplicates/unreadable IDs remain). Migrations: `a8d4e2b61c90` adds the column + NON-unique indexes (cannot fail);
  `b9e5f3c72da1` drops them and creates the UNIQUE ones but raises `CommandError` (changes nothing) while any stored ID lacks its hash or any phone/hash
  is duplicated. `alembic/env.py` uses `transaction_per_migration=True` so step 1 stays recorded when step 2 refuses. Tests: `tests/test_unique_ids_phones.py`.
- Normalisers (pure, tested): `app/services/names.py` (name_key = sorted set of words: no accents, lowercase,
  apostrophes dropped, split on non-letters; REUSE for payslip filename matching), `phones.py`, `ids.py`
  (`parse_id(number, type)` -> `IdResult(id_type, number, last4, warnings, error)`). The plain number exists only in
  memory (inside `parse_id`, then encrypted at once by the callers) and is never put in errors, warnings, logs, session or
  preview; `ParsedRow.id_encrypted` is `repr=False` so it cannot leak through logging. Invariant: `id_last4` ==
  last 4 of the stored full number. `apply_values(..., *, id_number_encrypted, id_number_hash)` has no defaults on purpose: to KEEP the
  stored full ID pass `employee.id_number_encrypted` and `employee.id_number_hash`. Spaces and dashes are ignored; unknown id_type is an error; blank id_type is guessed with a warning.
- Phone rules: strip spaces - ( ) . ; `00` -> `+`; 8 digits or 267+8 digits => Botswana. Botswana national part must
  be 8 digits (error otherwise); not starting with 7 => accepted with a "not a mobile" warning. Other countries need
  `+` and 8-15 digits, first digit not 0. Blank phone/ID is allowed (warned) and never erases saved values.
- Import pipeline: `import_parser.py` (file -> ParsedRow, in memory) -> `import_planner.py` (`plan_import` reads
  the DB and decides new/update/unchanged/error; `apply_import` writes, caller commits ONCE) ->
  `import_preview.py` (in-memory store, per user, TTL `IMPORT_PREVIEW_MINUTES`, cleared on confirm/cancel/logout).
  Confirm re-plans against current data, then saves in one transaction together with its audit row.
  Single-process assumption: with several uvicorn workers the preview store must move to a shared place.
- Upsert: employee_code present -> match by code only (case-insensitive; not found = NEW even if the name exists,
  with a warning); no code -> match by name_key (0 = new, 1 = update, >1 = error). In-file duplicates (same name w/o
  code, same code, two rows hitting one employee) are errors on all involved rows. Name-matched updates keep the
  stored spelling; code-matched updates may rename. The import never reactivates anyone.
- Files: `.xlsx` (openpyxl read-only, first sheet; macros/`.xlsm`/`.xls` rejected; defusedxml + unpacked-size guard)
  or `.csv` (utf-8-sig then cp1252; delimiter `,` `;` or tab). Limits: ~950 KB file, `IMPORT_MAX_ROWS` (2000).
  The size limit sits below Starlette's 1 MiB point where uploads spill to a temp file, and
  `app/services/body_limit.py` returns 413 for oversized bodies (default 100 KB for every POST). When adding the
  payslip PDF upload, RAISE its path in `body_limit.LIMITS`. Those files go to private storage by design.
- Template: `/employees/import/template` (xlsx, Text-formatted phone/id_number/id_type/code, an id_type dropdown, two fake example
  rows (Example Person = omang, Example Traveller = passport) that the importer rejects if left in). Fake test files: `python -m app.scripts.make_sample_import` (git-ignored).
- Audit actions: employee_created / employee_edited / employee_deactivated / employee_activated and
  **employee_edit_opened** (every time an edit form is opened, because it can put the stored last-4 on the page; entity id
  only, no details) and employees_import_confirmed (counts only), **ids_revealed** (ONE row per reveal request: user_id,
  details `username 'x', employees=N`, entity id only for the single-employee reveal from the edit page; never any ID
  value; no row when nothing was revealed) and id_keys_rotated (counts only), **employees_exported** (`rows=N`), **id_hashes_backfilled**
  (counts), and one `employee_deactivated` row per person in a bulk deactivate (`details=bulk`).
- **Employee list tools (task 11).** URL param `show` (omang|passport|no_id|no_phone|dup_name; `employees.FILTERS`, one source for the "Show" chips and
  the dashboard links) is part of `_clean_list_params` (7-tuple) and of the reveal form. Columns: ID type TAG, "Last payslip" (`last_payslips`: newest
  payslip with status != pending: pay month + status only). Bulk: editors get a checkbox per ACTIVE row (`form="bulk-form"`), `POST /employees/bulk-deactivate`
  (`employees.edit`, max 200, keeps list params, redirects back). `GET /employees/export.csv` (`employees.view`; the CURRENT list, all pages): First
  name, Surname, Phone, Status ONLY, formula-guarded, no-store, audited; it must never gain an ID/ID type/last-4 column (tests scan for the secrets).
  `test_the_reveal_route_is_the_only_one_that_can_return_full_ids` lists the allowed file endpoints: update it deliberately if you add one.
- ID display: list columns "ID type", "ID number" and "Password (last 4)". "ID number" shows 9 dots (`id_mask`, same for
  everyone) in a `data-id-slot="<employee id>"` span; people with only a last 4 saved show `••••1234` + "full ID not
  imported yet" (no slot). The last-4 password column sits in `data-secret` attributes, shown as •••• until **Show IDs**.
  **Show IDs** (app/static/app.js) POSTs the hidden `form[data-reveal-form]` (CSRF token + the list's q/status/sort/dir/
  per_page/page) with `fetch`; the server re-runs the SAME list query and returns `{"ids": {employee_id: number}}` for
  just that page (max 100), fills the slots, and unmasks the last-4s. Re-masks on Hide IDs, after 60 s, when the tab is
  hidden and on back-navigation; the text is replaced, nothing is kept in JS variables. app.js makes exactly this one
  request (a test pins it). The edit page has its own reveal form (`employee_id` field) and "Show ID" button. Rate
  limit: `REVEAL_MAX_PER_MINUTE` (10) per admin, counted from the `ids_revealed` audit rows, so it survives restarts.
  The edit form never pre-fills the ID number box: it shows the saved type + masked ID; an empty "New ID number" keeps
  what is saved (including the encrypted value); changing the type needs the number typed again.
- Key management: `ID_ENCRYPTION_KEY` is REQUIRED (Settings refuses to start without a valid one, and
  `hide_input_in_errors` keeps keys out of startup errors). `python -m app.scripts.generate_encryption_key` writes it into
  `.env` without printing; it never overwrites an existing key. Rotation: `--rotate` moves the current key to
  `ID_ENCRYPTION_KEY_PREVIOUS` (comma-separated, decrypt-only, `MultiFernet`) and makes a new one, then
  `python -m app.scripts.reencrypt_ids` re-encrypts every row (steps in README). Losing the key = stored full IDs are
  unreadable (the last 4s still work); fix by re-importing the file. Tests use a fixed throw-away key from conftest.
- Importing a file again FILLS IN full IDs for people who only had a last 4 (planner: "The full ID number will be stored");
  a different full ID with the same last 4 is reported as "will change"; rows without an ID never erase a stored one.
- Migration `2419d27d8159` replaced `omang_last4` with `id_type` + `id_last4` (existing values became
  id_type='omang'); downgrade drops passport IDs (the old column cannot hold them). Migration `9b3f6c1d4e27` adds
  `id_number_encrypted` (NULL for existing rows; downgrade drops only that column). Both tested in tests/test_migration.py.
  Alembic's env.py loads Settings, so `ID_ENCRYPTION_KEY` must be in `.env` before `alembic upgrade head` too.
- Known gap (pre-existing): the add/edit form never pre-fills the ID box, so when it re-renders with warnings ("Save
  anyway") the typed ID number is not carried over and a browser re-submit sends an empty box (tests re-send the field).
- Import flow is 3 steps: Upload (`/employees/import`) -> Review (`/employees/import/preview`: summary bar, problem rows
  sorted first, filter chips) -> Done (`/employees/import/done`, counts only, shown once).

## UI (built)
- **UI upgrade (task 9).** Theme: ALL colours are CSS variables; `:root` = light, `:root[data-theme="dark"]` and
  `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) }` = dark (the two dark blocks MUST be identical; a test checks it,
  checks WCAG AA 4.5:1 for every text/background pair in both themes and 3:1 for the focus ring, and that no colour is hard-coded outside the
  variable blocks except the flag-stripe white). `static/theme.js` is loaded in `<head>` WITHOUT defer so the saved choice (localStorage
  `payslip-theme`, try/catch) applies before the first paint; the toggle button (`data-theme-toggle`, `aria-pressed`) is the "Dark theme" switch at the bottom of the sidebar (and a small button on the login card). The
  top bar also holds breadcrumbs (`{% from "_crumbs.html" import crumbs %}` + `{% block breadcrumbs %}` in each deep page) and the ALWAYS-visible
  MOCK/LIVE pill (`live_whatsapp_mode()` template global; do not name a context variable `whatsapp_mode`/`role_label`/`can`: it shadows the
  global). Flash messages are TOASTS: same `.flash` markup in a fixed `.toasts` live region after `</main>`; app.js removes success/info after
  7 s (paused on hover/focus), errors stay. Risky buttons carry `data-confirm`, `data-confirm-title`, `data-confirm-label`: app.js opens ONE
  native `<dialog id="confirm-dialog">` (focus trap + Esc from the browser) and re-submits the form with `requestSubmit`; with no JS the
  button just submits. Menus: arrow keys. Busy submit buttons get a spinner (`.is-loading`). The delivery RIBBON (`_ribbon.html`: an SVG of
  rects whose `x`/`width` are presentation ATTRIBUTES, because inline `style` is blocked by the CSP; `progress.js` updates them) is used on
  the dashboard, the batch list and the send screen, always with a legend/aria-label so colour is never the only clue. The finished send screen
  shows a "what to do next" summary (failed -> Retry failed; uncertain -> ask the employee first; pending -> Resume); `progress.js` reloads once
  when `data-active` flips to finished. Scrollable tables are `role="region" tabindex="0" aria-label` (a test checks every template) and keep
  their first column sticky below 900 px. `prefers-reduced-motion` switches animations off. No inline script/style/handler and no external
  resource anywhere (a test scans every template and the stylesheet).
- Layout (task 11): `templates/base.html` = left sidebar (`#sidebar`: logo + collapse button; GET search form to `/employees?q=..&status=all`, `/` focuses it
  (or the page's `[data-page-search]`); nav pills with `nav-label` + `nav-tip` tooltip spans; Users/Audit log/System status under an "Admin" heading for
  admins only; at the bottom the theme SWITCH (`data-theme-toggle`, same attributes theme.js and the tests expect), name + role link to My account, log out),
  a top bar (hamburger on small screens, breadcrumbs, MOCK/LIVE pill) and a page header block (`heading`, `lede`, `actions`; `{% block nav_key %}` highlights the
  nav item). `static/sidebar.js` (loaded in `<head>` WITHOUT defer, ASCII, no network): `html[data-sidebar=collapsed|expanded]` remembered in
  localStorage `payslip-sidebar`; collapsed = icons only (words stay in the DOM visually hidden, CSS tooltips on hover/focus; wide screens only); under
  900 px with scripts the sidebar is an off-canvas drawer (`html[data-drawer=open]`, `inert` on `.content`, focus trap, Esc, backdrop, focus returns to
  the menu button); WITHOUT scripts (`html:not(.js)`) it stays a plain block above the page. Keep `needs-js` specificity rules in mind (`.js .needs-js.menu-btn`).
  `login.html` and `error.html` replace the whole page block (so they have no sidebar, dialogs or session.js).
- **Login page**: glass card (`.glass-card`, backdrop blur + `@supports` fallback) over 4 CSS radial-gradient blobs (`.login-blob`, drift only under
  `prefers-reduced-motion: no-preference`), white circular badge with an inline SVG, pill inputs, show/hide password (`data-toggle-password`, in app.js),
  no remember-me, `<details class="forgot">` note. Colours are variables (`--glass-bg`, `--blob-*`, ...) in all THREE theme blocks; tests composite the glass colour
  over every blob and require 4.5:1 for text. Lockout (HTTP 429) renders `login-alert-lock`; the generic messages are unchanged.
- Design language: sky blue / black / white (the stripe mark in the logo and on the login screen), system font stack
  only, sentence-case labels, status badges are always a word plus dot/icon. ALL colours/radii/shadows are CSS
  variables in `:root` of `app/static/style.css`, so a dark theme only needs a second set of values.
- Self-hosted only (strict CSP: `default-src 'self'`, no inline script/style): `static/style.css`, `static/app.js`,
  `static/icons.svg` (sprite; icons use presentation attributes, NOT a `<style>` block, because CSP would block it).
  Template helpers in `app/templating.py`: `icon(name)`, `badge(kind, label)`, `avatar(first, surname, id)`,
  `csrf_input(request)`, `get_flashes(request)`. Add new icons to the sprite; a test checks every used icon exists.
- app.js (progressive enhancement; its ONE network request is the Show IDs reveal POST): Show/Hide IDs, dismissible flashes, row menus
  (`<details class="menu">`), auto-submit for `data-autosubmit` selects, double-submit guard for `form[data-once]`, the ONE `<dialog id="confirm-dialog">`
  (`role=alertdialog`; the app never uses `confirm()/alert()/prompt()`: a test scans all JS and templates), employee-list bulk selection (`data-select-*`,
  `data-bulk-button` rewrites its own confirm title with the count), show/hide password.
- **Inactivity warning** (`static/session.js`, loaded only on logged-in pages, own `fetch`es: `GET /session/status` and `POST /session/keepalive` with
  `X-CSRF-Token`, `redirect: "manual"`; a test pins that these are its only URLs). `routers/session.py` (permission `account.self`; both paths are in
  `PENDING_ALLOWED_PATHS`). `sessions.NO_TOUCH_PATHS = {"/session/status"}`: asking does NOT refresh `LAST_SEEN`, otherwise the warning would keep the
  session alive; `sessions.seconds_left` = min(idle left, absolute left). At 60 s left the script opens `#idle-dialog` (Stay signed in / Esc = keep-alive,
  Log out now, countdown, then `/login`); it re-asks the server first (other tabs and background polling extend the session).
- Employee list URL params: `q`, `status` (active|inactive|all), `sort` (name|status), `dir` (asc|desc), `per_page`
  (10|25|50|100), `page`; defaults are omitted from generated links. Chips show counts for the current search.
- Flash messages: `app/flash.py` (keep them free of personal data; they live in the cookie). Tables stay semantic
  (`<caption>`, `scope`, `aria-sort`); icon-only buttons need `aria-label` (a test enforces it).
- Checking the look: headless Edge can screenshot the app (see git history/notes); it enforces a ~500px minimum
  window, so test phone widths by showing the page inside a 390px-wide `<iframe>`.

## Layout
```
app/            main.py (create_app) config.py (settings) database.py templating.py
  security/     passwords, sessions, csrf, auth (require_login), headers
  server.py     production entry point (python -m app.server)    single_instance.py  one-process lock    logging_setup.py
  scripts/      CLI: production_check (server readiness), create_admin, reset_admin_password, generate_secret, generate_encryption_key, reencrypt_ids, backfill_id_hash,
                purge_old_batches, make_test_payslips (fake PDFs), fake_webhook (signed fake status events),
                check_live_config (read-only live preflight)
  static/       public, self-hosted: style.css, app.js, icons.svg (never payslip files)
  models/       SQLAlchemy models        routers/   web routes (health, pages, ...)
  services/     business logic (matching, import, pdf, whatsapp ...)   templates/  Jinja2 HTML
deploy/         server files for Ubuntu/Caddy/systemd (see "Production deployment"); DEPLOY.md = beginner guide
alembic/        migrations (env.py reads DATABASE_URL via app.config)
private_storage/  uploaded/encrypted PDFs (git-ignored)
```

## Local dev
- XAMPP MariaDB on 127.0.0.1:3306; database `payslip_sender`, app user `payslip_app` (only privileges on that
  DB). Root was used once for setup only. DATABASE_URL and `ID_ENCRYPTION_KEY` live in `.env` (the app will not start without the key).
- Run: `.\.venv\Scripts\Activate.ps1` then `python -m uvicorn app.main:app --reload` (app at http://127.0.0.1:8000; XAMPP MySQL must be
  running). Use `python -m uvicorn`, NOT bare `uvicorn`: Windows Application Control blocks `uvicorn.exe` on the dev PC.
  `.\run.ps1` wraps exactly this command (no venv activation needed).
  The same applies to other venv `.exe` launchers (`alembic`, `pytest`): prefer `python -m alembic`, `python -m pytest`.
- `.\dev.ps1 install|test|audit|check|all` wraps install / pytest / pip-audit / production_check (uses the venv's Python, like run.ps1).
- Tests: `pip install -r requirements-dev.txt` (includes fpdf2, which the PDF tests need) then `pytest` (uses in-memory SQLite + a fake secret, never the real DB).
  One file: `pytest tests/test_ids.py`; one test: `pytest tests/test_ids.py::test_name -q`; by keyword: `pytest -k passport`.
  `tests/conftest.py` sets the env vars BEFORE importing `app`, and provides `db_factory`/`db`/`app`/`client`/`admin`
  fixtures (the `app` fixture overrides `get_db`). Keep new test modules on those fixtures; never point tests at MariaDB.
- New migration: `alembic revision --autogenerate -m "message"` then `alembic upgrade head`. Tests build tables with
  `Base.metadata.create_all` (SQLite), so a model change that lacks a migration will NOT fail the normal tests.
- CLI scripts (run from the project root with the venv active): `python -m app.scripts.create_admin`,
  `reset_admin_password`, `generate_secret` (writes `SESSION_SECRET` into `.env`), `generate_encryption_key` (writes
  `ID_ENCRYPTION_KEY`, `--rotate` for rotation), `reencrypt_ids` (also re-hashes), `backfill_id_hash`, `make_sample_import` (fake import files).
- No linter/formatter is configured (no ruff/black/pyproject); match the surrounding style.
- Add any new setting to BOTH `app/config.py` and `.env.example`.

## Build status
- DONE (task 1): project skeleton, settings, models, Alembic + first migration, `/health`.
- DONE (task 2): admin login (CLI admin creation, Argon2, sessions, CSRF, rate limiting, audit, security
  headers, placeholder dashboard at /), tests.
- DONE (task 3): employees list/add/edit/deactivate + Excel/CSV import with preview, template, audit, tests.
- DONE (task 4): ID types (Omang or passport) with migration; ID last-4 hidden/Show IDs; edit-opened audit; full UI
  redesign (sidebar layout, sortable/paged tables, chips, 3-step import, icons, accessible forms).
- DONE (task 5): full ID numbers stored encrypted (Fernet) with `id_number_encrypted`, key helper + rotation, audited
  rate-limited reveal endpoint, masked ID column / edit form, soft ID validation, importer fills in full IDs.
- DONE (task 6): Batches: bulk PDF/ZIP upload with safety rules, final matching design, PDF text cross-check, background
  matching with progress, review page, readiness checks, confirm + "ready" status, purge command, fake test PDFs.
- DONE (task 7): sending engine: AES-256 PDF protection (verified, in memory), WhatsApp client (mock default + live, tested
  only with a mocked HTTP layer), allowed-recipients safety switch, background worker with retries/uncertain/halt/stop/
  resume/restart recovery, live progress screen + JSON poll + CSV report, signed webhook, fake_webhook script.
- DONE (task 8): `check_live_config` preflight (settings, token/phone number, template/language/status, allowed recipients).
- DONE (task 9): user management with roles (admin/hr/viewer) enforced per route, temporary passwords shown once, forced change,
  sign-out-everywhere, last-admin/self safeguards, My account, admin-only Audit log (filters + CSV) and System status, dashboard with the
  latest batch ribbon, dark theme + toggle, toasts, confirm dialog, breadcrumbs, accessible scroll regions, mobile sticky columns.
- DONE (task 10): production deployment: `APP_ENV=production` rules (refuses unsafe settings), no /docs, Secure cookies, exactly one process
  (`app.server` + instance lock), safe start-up recovery + in-app housekeeping, structured redacted logging, `SENT_RETENTION_DAYS` + daily purge
  timer, `production_check`, `deploy/` (systemd, Caddyfile, setup/update/backup/restore scripts), pinned dependencies + pip-audit + `dev.ps1`,
  DEPLOY.md. The deploy files have never run on a real server.
- DONE (task 11): unique ID + phone (blind index, form/import/DB, two migrations, `backfill_id_hash`), glass login page, new sidebar layout (collapsible,
  drawer, `/` search, theme switch), send-screen message preview (`WHATSAPP_TEMPLATE_PREVIEW_TEXT`, preview only, never sent), dashboard "Needs attention"
  card + admin banners (`dashboard.admin_alerts`: token rejected = latest `send_token_error` audit with no payslip `sent_at` after it, 30 days; webhook silent =
  live mode, a live payslip `sent` for > 10 min and no signed webhook since it was sent), employee list tools (see above), inactivity warning, in-page modals.
  Tests: `tests/test_unique_ids_phones.py`, `tests/test_ui_upgrade.py`.
- NOT YET BUILT: per-IP rate limit on the webhook, a real live send
  (never exercised against Meta: do the going-live checklist first).
