# Payslip Sender — project brief (final design)

A web app where ONE HR admin uploads employees' payslip PDFs in bulk and sends each to the right
employee on WhatsApp. The owner is a beginner on Windows (PowerShell, VS Code): explain things in
simple terms, run commands yourself where possible, and give exact commands for anything they must run.

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
1. **Employees**: first_name + surname (required), phone (E.164, Botswana +267...), omang_last4 (ONLY the last
   4 digits of the national ID), active flag, optional employee_code. HR adds/edits/deactivates on a page and
   imports from .xlsx or CSV with columns `first_name, surname, phone, omang, employee_code(optional)`.
   Importer rules: accept a full Omang but store only the last 4 digits and discard the rest; normalise phones
   (71234567 / 26771234567 / +26771234567 -> +26771234567) and flag invalid ones; numeric values shorter than 4
   digits are left-padded with zeros to 4 with a warning (Excel drops leading zeros); report errors by row
   number; upsert by employee_code if present, else by normalised first_name+surname; show a PREVIEW before
   saving; offer a downloadable template file.
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
5. **Send**: password-protect each PDF with AES-256 using the employee's omang_last4, upload to the WhatsApp
   media endpoint, send a template message. Template name from env `WHATSAPP_TEMPLATE_NAME` (default
   `payslip_`), language `en`, header = the PDF document, body params by position: {{1}} = employee full name,
   {{2}} = pay month label. A second mode sends a plain document message (no template) for testing inside the
   24-hour window (`WHATSAPP_SEND_KIND=document`).
6. **Status tracking**: webhook `/webhook/whatsapp` — GET for Meta verification (verify token); POST for
   delivery/read/failed statuses, verifying `X-Hub-Signature-256` with the app secret. Statuses: pending,
   queued, sent, delivered, read, failed (+ error message). HR can retry failed ones.
7. **WHATSAPP_MODE**: `mock` (default; simulates sends and statuses locally, no network) or `live`.

## Database tables
users, employees (with indexed normalised `name_key` for matching), batches, payslips (batch, employee nullable
until matched, original filename, stored/encrypted filenames, match_method, match_status, confirmed, pdf_check,
status, whatsapp_media_id, whatsapp_message_id, error, attempts, timestamps), audit_log (who/what/when),
login_attempts (rate limiting). See `app/models/`. Migrations in `alembic/versions/`.

## Security rules (non-negotiable)
- NEVER store full Omang numbers anywhere (database, logs, files, audit details).
- No secrets in code. Everything in `.env`; `.env.example` has placeholders only; `.env` is git-ignored.
- Never log access tokens, passwords, or full phone numbers. Mask phones in logs/screens (show last 3 digits).
- Uploaded and encrypted PDFs live in the private folder (`PRIVATE_STORAGE_DIR`, default `private_storage/`)
  outside any public/static path; validate they are real PDFs (magic bytes `%PDF-`, size limit); delete after
  sending (or after the `RETENTION_HOURS` setting).
- Meta Graph API version is the setting `GRAPH_API_VERSION` — never hardcode it in several places.
- `/health` is public and returns only `{"status":"ok"}` / `{"status":"error"}` (503). It logs only the exception type.

## Authentication (built)
- Admins are created ONLY from the command line (`python -m app.scripts.create_admin`, `reset_admin_password`);
  no web route can create/reset admins. Passwords: min 12 chars, hashed with Argon2id (`app/security/passwords.py`).
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
- Audit actions: login_success, login_failure, login_lockout, logout, admin_created, admin_password_reset.
  IPs are stored masked (192.168.1.x). Failed-login audit rows name the username only if it exists.
- Security headers on every response (`app/security/headers.py`): CSP (no inline scripts/styles: put CSS in
  `app/static/`), X-Frame-Options DENY, nosniff, Referrer-Policy, no-store, HSTS in production.
- Behind Caddy later: run uvicorn with `--proxy-headers --forwarded-allow-ips=127.0.0.1` so the real client IP
  is used for rate limiting. Limit request body size in Caddy (forms are parsed before login is checked).
- Known limits: no per-IP-only throttle (an attacker can try 5 passwords per username per 15 min); cookie
  sessions cannot be revoked server-side except by changing the password or rotating SESSION_SECRET.

## Layout
```
app/            main.py (create_app) config.py (settings) database.py templating.py
  security/     passwords, sessions, csrf, auth (require_login), headers
  scripts/      CLI: create_admin, reset_admin_password, generate_secret
  static/       public CSS only
  models/       SQLAlchemy models        routers/   web routes (health, pages, ...)
  services/     business logic (matching, import, pdf, whatsapp ...)   templates/  Jinja2 HTML
alembic/        migrations (env.py reads DATABASE_URL via app.config)
private_storage/  uploaded/encrypted PDFs (git-ignored)
```

## Local dev
- XAMPP MariaDB on 127.0.0.1:3306; database `payslip_sender`, app user `payslip_app` (only privileges on that
  DB). Root was used once for setup only. DATABASE_URL lives in `.env`.
- Run: `.\.venv\Scripts\Activate.ps1` then `uvicorn app.main:app --reload`.
- Tests: `pip install -r requirements-dev.txt` then `pytest` (uses in-memory SQLite + a fake secret, never the real DB).
- New migration: `alembic revision --autogenerate -m "message"` then `alembic upgrade head`.

## Build status
- DONE (task 1): project skeleton, settings, models, Alembic + first migration, `/health`.
- DONE (task 2): admin login (CLI admin creation, Argon2, sessions, CSRF, rate limiting, audit, security
  headers, placeholder dashboard at /), 30 pytest tests.
- NOT YET BUILT: employees page + import, batch upload + matching, preview, WhatsApp client,
  send flow, webhook, retention cleanup.
