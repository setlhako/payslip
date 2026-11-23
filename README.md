# Payslip Sender

Upload payslip PDFs in bulk and send each one to the right employee on WhatsApp.
See [CLAUDE.md](CLAUDE.md) for the full design.

## One-time setup (Windows, PowerShell, in the project folder)

1. Install Python 3.11 or newer (https://www.python.org/downloads/ — tick "Add python to PATH").
2. Start **XAMPP** and click **Start** next to **MySQL**.
3. Create the virtual environment and install packages:
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
   If PowerShell refuses to run `Activate.ps1`, run once:
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
4. Create your settings file and fill it in:
   ```powershell
   Copy-Item .env.example .env
   ```
   Then edit `.env`: set the password in `DATABASE_URL`, and create the login-cookie secret automatically:
   ```powershell
   python -m app.scripts.generate_secret
   ```
   (It writes `SESSION_SECRET` into `.env` and never prints it.)

   Also create the key that encrypts employees' ID numbers. **`ID_ENCRYPTION_KEY` must be in `.env` before you start the
   app (or run `alembic`), otherwise it refuses to start:**
   ```powershell
   python -m app.scripts.generate_encryption_key
   ```
   It writes `ID_ENCRYPTION_KEY` into `.env` and never prints it. **Back up your `.env` file somewhere safe** (not in git):
   without this key the stored ID numbers cannot be read again (you would have to import them again).
5. Create the database and a limited user in XAMPP's MySQL (change the password!):
   ```powershell
   C:\xampp\mysql\bin\mysql.exe -u root -e "CREATE DATABASE payslip_sender CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER 'payslip_app'@'localhost' IDENTIFIED BY 'CHOOSE_A_STRONG_PASSWORD'; GRANT ALL PRIVILEGES ON payslip_sender.* TO 'payslip_app'@'localhost'; FLUSH PRIVILEGES;"
   ```
   Put the same password into `DATABASE_URL` in `.env`.
6. Create the tables:
   ```powershell
   alembic upgrade head
   ```

## Create your first admin login (once)

In a **terminal** (not the VS Code "Run" button), with the virtual environment active:
```powershell
python -m app.scripts.create_admin
```
It asks for a username and a password (at least 12 characters). The password is typed hidden and never
appears on screen or in your shell history. After that, **admins add everyone else on the Users page** (below).

**Locked out?** The command line is the emergency path. On the server:
```powershell
python -m app.scripts.reset_admin_password            # asks for the username and a new password
python -m app.scripts.reset_admin_password --enable   # also re-enables an account that was disabled
```
(`create_admin --role hr` / `--role viewer` / `--must-change` also exist.) The website can never reset an admin without being
signed in as an admin.

## Users and roles (admins only)

Open **Users** in the sidebar. Every person has one of three roles, and the server enforces it on every page, not just by hiding buttons:

| Role | Can do |
|---|---|
| **Admin** | Everything, plus Users, the Audit log and System status. |
| **HR** | Employees (view, add, edit, import, deactivate, **show full ID numbers**) and Batches (create, upload, review, send, retry). |
| **Viewer** | Look only: the employee list (ID numbers are **always hidden**, no Show IDs button), batches and send progress. Cannot change or send anything. |

On the Users page you can: **add a user** (username, full name, role), **edit** their name or role, **disable / enable**, **reset password**,
**unlock** an account that was locked by too many wrong passwords, and **sign out everywhere** (ends all their open sessions).

- **Temporary passwords.** When you add a user or reset a password, the app makes a temporary password and shows it **once** on screen.
  Copy it then; it is not stored anywhere readable, so if you lose it, reset the password again. At their first login the person must choose
  their own password (at least 12 characters, different from the temporary one) before they can use anything else.
- **Safeguards.** The last active admin can never be disabled or demoted, and nobody can disable or demote **themselves**. Usernames are unique
  (not case-sensitive). Every action is written to the audit log (who did it, to whom; never a password).
- **My account** (click your name in the sidebar): change your own password and see your recent logins. Changing it signs out your other sessions.

### Try each role
1. Log in as your admin, open **Users > Add user** and create three people: for example `test.hr` (role HR), `test.viewer` (role Viewer).
   Copy each temporary password when it appears.
2. Open a private/incognito browser window (so you stay logged in as admin in the normal one). Log in as `test.hr` with the temporary password,
   choose a new password when asked, and look around: you can add employees, upload and send batches, and press **Show IDs**, but there is
   no Users, Audit log or System status in the sidebar (typing `/users` gives "Your role does not allow you to do that").
3. In another private window do the same as `test.viewer`: the employee list shows dots for every ID, there is no Show IDs button and no
   Add/Import/Edit buttons, and batches show no action buttons.
4. Back as admin, try **Sign out everywhere** on `test.hr`: their window is logged out on its next click. Try **Disable** on yourself: it
   refuses.

## Audit log and System status (admins only)

- **Audit log** lists who did what and when. Filter by date range, person and kind of action, search the text, page through the results, and
  **Download CSV** of exactly what you are looking at. It never shows or exports ID numbers, passwords, tokens or full phone numbers.
- **System status** is a read-only page: MOCK or LIVE (very visible), the template name and language, whether the allowed-recipients safety list
  is on and how many numbers it holds, whether the required settings are filled in (yes/no only, never their values), when the last
  WhatsApp webhook event arrived, whether the database answers, the app version and how many batches are open or old. Settings are still changed
  in the `.env` file, not in the website.

## Look and feel

- **Dark theme:** click the moon/sun button at the top right. The app remembers your choice in this browser; before you choose it follows your
  computer's light/dark setting.
- A **MOCK** or **LIVE** badge is always visible at the top right of every page.
- Messages appear as small notices at the bottom right and fade away (errors stay until you close them). Risky buttons (delete, disable, stop
  sending, reset password) ask "Are you sure?" first.
- Everything works with the keyboard (Tab, Enter, Esc, arrow keys in menus), respects "reduce motion", and wide tables scroll sideways on a phone
  while keeping the first column visible.

## Run the tests

```powershell
.\dev.ps1 test
```
(that is `pip install -r requirements-dev.txt` once, then `pytest`; `.\dev.ps1 install` does the installing, `.\dev.ps1 audit` checks the
packages for known security problems). Tests use a temporary in-memory database; they never touch your real data.

## Employees: add, edit, import

Log in, then click **Employees**.
- **Add employee**, **Edit**, **Deactivate / Reactivate**. Nothing is ever deleted: a deactivated person is just hidden
  from the default list (choose *Inactive* or *All* to see them).
- **Search** by name, phone or employee code. **Show** switches between Active / Inactive / All.
- **Search** by name, phone or code; **Active / Inactive / All** chips; click **Name** or **Status** to sort; choose how many rows per page.
- Each person has an **ID type** (Omang or passport) and an **ID number**. The full number is stored **encrypted**; its **last 4
  characters** (in capitals) also become the password that opens that person's payslip PDF later (case-sensitive, e.g. passport
  AB1234567 -> `4567`, ab12cd34 -> `CD34`).
- The list shows the **ID number** as dots (•••••••••). Press **Show IDs** at the top to see the full numbers for the people on that page
  (they hide again after a minute, when you press **Hide IDs**, or when you switch tabs). The edit page has its own **Show ID** button.
  Every time you show IDs it is recorded in the audit log (who, and how many people, never the numbers), and you can do it at most
  10 times a minute. Opening someone's edit page is recorded too.
- People saved before this feature show only `••••` and their last 4 with the note "full ID not imported yet". Import your file again
  (same file is fine) and their full IDs are filled in.

### Import from Excel or CSV
1. Employees > **Import from Excel / CSV** > **Download import template**. Delete the example row and fill in your people.
   Columns: `first_name`, `surname`, `phone`, `id_number` (the FULL number), `id_type` (optional: omang or passport), `employee_code` (optional).
   An Omang that is not 9 digits, or a passport outside 6-9 characters, is accepted with a warning; a passport must be 5-15 characters.
   The old column name `omang` still works for `id_number`. If `id_type` is blank, all-digit numbers are treated as an Omang and numbers
   with letters as a passport, and the preview warns you to check.
2. Upload the file (step 1 of 3). Step 2 is a **review** (nothing is saved yet) listing every row as New / Updated / Unchanged / Error,
   with row numbers and plain-language messages. Rows with a warning are still saved; rows with an error are skipped.
3. **Save** to confirm (step 3 is a summary; everything is saved in one go) or **Cancel**. The preview disappears after 15 minutes anyway.

Good to know: the uploaded file is never saved on disk (it contains full Omang or passport numbers). The numbers are encrypted as soon
as the file is read, and the preview shows them hidden. Existing employees are matched by employee code, or by name when there is no code. Empty phone/ID cells never
erase saved data. Two people with the same name need different employee codes.

### Try it with fake data
```powershell
python -m app.scripts.make_sample_import
```
creates `sample_import.xlsx` and `sample_import.csv` (fake names, phones, Omang and passport numbers; some rows are wrong on
purpose). Upload either one and look at the preview.

## Payslip batches: upload, match, review

Make a batch for each pay run. Nothing is sent to anyone yet (sending is the next step to be built).

1. Click **Batches** > **New batch**. Type the pay month (for example `November 2026`) and choose either several PDF files or one ZIP.
   Name each file like `thabo_molefe.pdf` (first name and surname, either order; numbers in the name are ignored).
   Limits: 300 files per batch, 10 MB per PDF, 200 MB in total. Folders and hidden files in a ZIP are ignored; a ZIP inside a ZIP is refused.
2. The files are matched to your active employees in the background (a progress bar shows while it runs). Each file gets one of:
   **Exact** (matched automatically), **Needs review** (typo, middle name, partial name, or two employees with the same name:
   suggestions with a similarity score are shown), **Possible mismatch** (the file name says one person but the text inside the PDF
   names another), **Unmatched**, or **Invalid file** (not a real PDF, damaged, password-protected or empty: the reason is shown).
   A scanned PDF with no readable text just gets a note: "Could not verify against PDF content".
3. On the batch page, problem files are listed first. For each file you can **Use** a suggestion (one click), **Change match**
   (type to search your employees), **Confirm match**, or **Remove** it. **Add more files** works any time before the batch is marked ready.
   The *Ready to send?* column tells you in plain words what is wrong (no phone number, no ID number so no PDF password, employee
   inactive, two files for one employee, ...). Phone numbers show only their last 3 digits.
4. Press **Review and confirm**. When every file is fine, tick **I have checked the matches** and press **Mark as ready to send**.
   Employees with no file are allowed (they are listed). You can **Reopen** a ready batch to change it. A ready batch can be sent: see **Sending payslips** below.
5. **Cancel and delete this batch** removes the batch and all its PDFs. Uploaded PDFs live in `private_storage/` under random names
   (never on the website). Stored PDFs are also deleted automatically: `SENT_RETENTION_DAYS` (default 7) after a batch finished sending,
   and at the latest `BATCH_RETENTION_DAYS` (default 30) after it was created, by (on a server a daily timer runs it):
   ```powershell
   python -m app.scripts.purge_old_batches
   ```

### Try it with fake payslip PDFs
```powershell
pip install -r requirements-dev.txt
python -m app.scripts.make_test_payslips
```
This reads your employees' **names** and writes fake PDFs into `test_payslips\pdfs\`, a ZIP `test_payslips\test_payslips.zip`, and
`test_payslips\WHAT_TO_EXPECT.txt` (which file tests which case). The folder is ignored by git. Upload the PDFs (or the ZIP) in
**Batches > New batch** and compare with the list.

## Sending payslips (mock mode first)

`WHATSAPP_MODE=mock` is the default: **nothing is really sent**, and a blue **MOCK MODE** banner shows on every page.
Each PDF is password-protected (AES-256) with the employee's ID password (the last 4 characters, capitals, case-sensitive)
in memory only, then "sent" and tracked.

1. Open a batch that is **Ready to send** and press **Send payslips**. The next page shows the counts and a very visible
   **MOCK** or **LIVE** banner. Type the pay month (for example `November 2026`) to confirm, then press **Send**.
2. The batch page becomes a live screen: counters and a status per employee (Queued, Sending, Sent, Delivered, Read, Failed,
   Uncertain) that update by themselves. Buttons: **Stop** (cancels what is still queued; **Resume sending** continues),
   **Retry failed**, a **Retry** on each failed row, and **Retry selected uncertain rows**, and **Download send report** (CSV:
   names, last 3 digits of phones, status, times, errors; never ID numbers).
3. **Uncertain** means WhatsApp gave no clear answer (a timeout, or the app restarted mid-send). The payslip *may already have
   been delivered*, so the app never retries it by itself. Check with the employee, then tick the row, tick "I understand it may be
   delivered twice", and press **Retry selected uncertain rows**.
4. Nothing is ever sent twice unless you press a retry button yourself.

**Try every path in mock mode.** The last 3 digits of an employee's phone number choose what happens:

| Phone ends in | What the mock simulates | Result you should see |
|---|---|---|
| anything else | normal | Sent, then Delivered, then Read (a few seconds apart) |
| `000` | the number is not on WhatsApp | Sent, then **Failed**: "may not be on WhatsApp" (code 131026) |
| `111` | WhatsApp rate limit | the app waits and retries 3 times, then **Failed** (code 130429). Retry fails again, by design |
| `222` | timeout | **Uncertain** (never retried automatically) |

Change an employee's phone in **Employees > Edit** (for example `+26771230000`), then make a batch for them.

**Try the webhook** (the page where WhatsApp reports delivery). With the app running, in a second PowerShell window:
```powershell
python -m app.scripts.fake_webhook              # newest sent payslip: "delivered", then "read"
python -m app.scripts.fake_webhook --status failed --error-code 131026
python -m app.scripts.fake_webhook --bad-signature   # must be rejected with 403
python -m app.scripts.fake_webhook --verify          # the handshake Meta does when you set the webhook URL
```
It signs each event with `WHATSAPP_APP_SECRET` from your `.env` (never printed). Watch the batch screen update. In mock mode the
mock itself reports statuses through the same code, so you will usually see Read before you run it.

### Check your live settings first (sends nothing)
Before switching to live mode, run the preflight check. It **never sends a message**: it only asks Meta two read-only questions and
tells you what to fix. It never prints your token, secrets or full phone numbers.
```powershell
python -m app.scripts.check_live_config
```
It checks, in order: (1) the eight live settings are present (`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
`WHATSAPP_BUSINESS_ACCOUNT_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `GRAPH_API_VERSION`, `WHATSAPP_TEMPLATE_NAME`,
`WHATSAPP_TEMPLATE_LANGUAGE`); (2) the token works and the phone number ID is valid (shows the number as `***567`, its quality rating and
name status); (3) your template exists, is **APPROVED**, and has your language (if the language differs it lists the languages Meta has and
which setting to change); (4) whether `WHATSAPP_ALLOWED_RECIPIENTS` is set and how many numbers it holds. It ends with **RESULT: PASS** or
**RESULT: FAIL** and a numbered list of next steps. Run it again after each fix.

`WHATSAPP_BUSINESS_ACCOUNT_ID` is new: it is the WhatsApp **Business Account** ID, a *different* number from the phone number ID (find both in
Meta's WhatsApp Manager). Only this check uses it. Until you fill in real values the check says FAIL: that is expected.

### Going live (later, carefully)
Only when you are ready to send real WhatsApp messages (and `check_live_config` says PASS):
1. In Meta's dashboard create/approve a message template named like `WHATSAPP_TEMPLATE_NAME` (default `payslip_`, language `en`) with
   a **document header** and **two body variables**: `{{1}}` = employee name, `{{2}}` = pay month.
2. In `.env` set `WHATSAPP_MODE=live` and fill `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`,
   `WHATSAPP_VERIFY_TOKEN` and `GRAPH_API_VERSION`. **The app refuses to start in live mode if any of them is missing.**
3. **Safety switch:** set `WHATSAPP_ALLOWED_RECIPIENTS=+26771234567` (your own number; comma-separated for several). While it is set,
   live mode sends to nobody else and tells you why. Send one test batch to yourself first; clear the list only when it works.
4. Point Meta's webhook at `https://your-domain/webhook/whatsapp` with the same verify token.
5. The "plain document" mode (`WHATSAPP_SEND_KIND=document`) only works inside WhatsApp's 24-hour window and is for testing.
Details Meta's documentation does not confirm (the template document-header format, text-parameter limits) are listed in CLAUDE.md:
that is why step 3 exists. The live code has only ever been tested against a pretend (mocked) WhatsApp.

## Start the app (every time)

1. Start XAMPP -> MySQL. Make sure `ID_ENCRYPTION_KEY` is in `.env` (see setup step 4): without it the app refuses to start.
2. In the project folder:
   ```powershell
   .\.venv\Scripts\Activate.ps1
   python -m uvicorn app.main:app --reload
   ```
   (Use `python -m uvicorn`, not just `uvicorn`: Windows can block `uvicorn.exe` with an "Application Control policy" error.)

   Shortcut: just run `.\run.ps1` in the project folder. It does the same thing and needs no activation.
3. Open http://127.0.0.1:8000. You are sent to the login page; log in with your admin. http://127.0.0.1:8000/health should say `{"status":"ok"}`.
4. Stop the server with `Ctrl+C`.

## Changing the database later

After editing a model in `app/models/`:
```powershell
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

## Rotating the encryption key

Do this if the key may have leaked, or as a yearly routine. Stored IDs stay readable throughout. Keys are never shown on screen.

1. **Stop the app** and **back up `.env`**.
2. Make a new key and keep the old one for reading:
   ```powershell
   python -m app.scripts.generate_encryption_key --rotate
   ```
   (`.env` now has the new `ID_ENCRYPTION_KEY` and the old key in `ID_ENCRYPTION_KEY_PREVIOUS`.)
3. Re-encrypt every stored ID with the new key (it prints only counts):
   ```powershell
   python -m app.scripts.reencrypt_ids
   ```
4. Only when it says all IDs use the newest key: open `.env` and delete the value after `ID_ENCRYPTION_KEY_PREVIOUS=` (leave the name).
5. Start the app again (`.\run.ps1`) and press **Show IDs** to check.

If step 3 reports IDs it could not read, keep `ID_ENCRYPTION_KEY_PREVIOUS` as it is and import those people again.

## Putting it on a server (production)

The step-by-step guide for a beginner is **[DEPLOY.md](DEPLOY.md)** (AWS EC2, Ubuntu 24.04, Caddy for HTTPS, MySQL/MariaDB on the same machine,
the Meta webhook settings and a go-live checklist). This section explains the ideas behind it. Everything for the server is in the
[deploy/](deploy/) folder; nothing there changes how `.\run.ps1` works on your own PC.

### A fresh start on the server (empty database, new secrets, private database)
- The production database **starts empty**. Local test data is never copied; HR imports the real employees on the server and an admin is created there.
- Production has its **own newly generated `SESSION_SECRET` and `ID_ENCRYPTION_KEY`** (`setup-server.sh` makes them on the server and never prints them).
  Do not copy your PC's `.env` or keys.
- The database is **MariaDB** (Ubuntu 24.04's `mariadb-server`) on the same machine, bound to `127.0.0.1` only, with a dedicated database and user. Its random
  password is written straight into `.env` (mode 600) and a private backup options file, never printed. **Port 3306 is never opened** (the firewall allows
  only 22, 80 and 443, and so should the AWS security group).
- Back up the new `ID_ENCRYPTION_KEY` **separately from the database dumps** (see "Backups (database) and the encryption key" below for why).

### Development and production modes
`APP_ENV` in `.env` is `development` (default, your PC) or `production` (the server). In production:
- debug is off and `/docs`, `/redoc` and `/openapi.json` do not exist (they never exist, in either mode);
- session cookies are **Secure** (HTTPS only, plus HttpOnly and SameSite=Lax) and the app sends HSTS;
- the app **refuses to start** and says why (it names the setting, never the value) if `SESSION_SECRET`, `ID_ENCRYPTION_KEY` or
  `DATABASE_URL` is missing, still a placeholder, or too weak; if `DATABASE_URL` is not MySQL/MariaDB; if you switch the Secure cookie
  off; or if `APP_HOST` is anything but `127.0.0.1` (only Caddy may reach the app);
- the server is started with `python -m app.server`: one uvicorn process, listening only on `127.0.0.1`, trusting `X-Forwarded-*`
  headers **only from 127.0.0.1** (that is Caddy), uvicorn's own access log off (it would write search words and one-time links into a
  log), and our own structured log setup.

### Why exactly one process
The app must run as **one** process (one uvicorn worker), never `--workers 2` and never two copies. Reasons:
1. **The sending engine lives in the process.** Worker threads, the queue position, Stop/Resume and the delayed mock statuses are in
   memory. A second process would not know about them.
2. **Start-up recovery assumes nobody else is sending.** On every start the app turns payslips that were `sending` into `uncertain`
   (a message may or may not have left; it is never retried automatically) and resumes `queued` ones. A second process doing this while
   the first is mid-send would mark live sends `uncertain` and could send twice.
3. **Some state is in memory by design:** the employee-import preview and one-time temporary passwords.
   They are small and short-lived but not shared between processes.

It is enforced three ways: `app.server` always starts one worker and refuses `WEB_CONCURRENCY` above 1; the systemd unit sets
`WEB_CONCURRENCY=1`; and in production the app takes an exclusive **lock file** (`.instance.lock` in the private storage folder) and a
second copy refuses to start. The operating system drops the lock when the process ends, even in a crash, so restarting is always safe.
Restarting while a send runs is safe too (nothing is sent twice) but leaves in-flight payslips `uncertain`, which is why `update.sh`
refuses to run during a send. One process is plenty: sending is limited by WhatsApp, not by the app (hundreds of payslips take minutes).

### Checking a server: `production_check`
```bash
sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.production_check
sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.production_check --url https://payslips.example.com
```
It changes nothing and sends nothing. It prints PASS / FAIL / WARN lines and plain-language fixes: `APP_ENV`, Secure cookies, single
worker, strength of the session secret / encryption key / database URL (never shown), `.env` permissions (must be 600), whether
`WHATSAPP_MODE` is still `mock` (**warns loudly**: nothing real is sent), whether `WHATSAPP_ALLOWED_RECIPIENTS` is **still set in live
mode** (**warns loudly**: employees other than your test number will fail), the database connection and that migrations are applied,
the private storage folder (exists, writable, private, not under `static`), free disk space, and that `/docs` is off (in the code and on
the running app). With `--url` it also checks the public HTTPS address (health, HSTS, `/docs`). Exit code 0 = no FAIL. On your own PC it
says FAIL for `APP_ENV` because you are in development: that is expected (`.\dev.ps1 check`).

### Logging
Logs go to **stdout** (journald on the server: `sudo journalctl -u payslip-sender -f`, kept across reboots, 500 MB cap) and, when `LOG_DIR` is
set, to a **rotating file** (`payslip-sender.log`, `LOG_FILE_MAX_MB` x `LOG_FILE_BACKUPS`, readable by the app user only). `LOG_LEVEL`
(DEBUG/INFO/WARNING/ERROR) and `LOG_FORMAT` (`auto` = JSON lines in production, plain text in development) come from `.env`.

What is **never** logged: access tokens, passwords, the session secret, encryption keys, full phone numbers, ID numbers, names, PDF
contents. Two layers: (1) the code only ever logs counts, ids and exception *types* (a test reads every `log...()` call in `app/` and
fails if one formats anything else); (2) a safety net masks long digit runs, tokens, `Bearer ...`, webhook signatures and `password=...`
in every line and traceback before it is written. Caddy's access log has query strings, cookies and authorization headers removed.
Honest limit: the safety net is pattern-based; a freak exception message containing a name would not be caught, which is why (1) matters.

### Retention: when are stored PDFs deleted?
Set in `.env` (the production template has both):
- `SENT_RETENTION_DAYS` (default **7**): a batch that has **finished sending** loses its stored PDFs this many days later, **unless** it
  still has `failed` or `uncertain` payslips (HR may need the PDF to retry).
- `BATCH_RETENTION_DAYS` (default 30): any batch (an unfinished draft, or one with unresolved payslips) loses its PDFs this long after it was created.
Batches being sent right now are never touched. The batch and its list of files stay in the database (marked "files deleted").
On the server the **`payslip-purge.timer`** runs `python -m app.scripts.purge_old_batches` every day at about 03:10 (see
`sudo systemctl list-timers`). The same command also removes leftover folders and old login-attempt rows. Inside the running app a small
housekeeping task (every 5 minutes) clears expired import previews and one-time passwords from memory.

### Backups (database) and the encryption key
`deploy/backup.sh` (run nightly by `payslip-backup.timer`) dumps the database, compresses it and **encrypts it with [age](https://age-encryption.org)**
using your **public** key. The matching **private** key stays with you (password manager / USB stick), **not on the server**: a stolen server
or a stolen backup cannot be read without it. It keeps 14 days of files in `/var/backups/payslip-sender` and can also copy each one to an S3 bucket
(`S3_URI` in `/etc/payslip-sender/backup.conf`). It refuses to run, and writes nothing, until a public key is configured: it never saves an unencrypted dump.

> **`ID_ENCRYPTION_KEY` must be backed up SEPARATELY, and never in the same place as the database dumps.**
> The employees' full ID numbers inside a dump are encrypted with that key, and the key lives only in `.env` (it is *not* in the dump, on
> purpose). Lose the key and a restored database cannot show the ID numbers (the last 4 digits and everything else still work; you would
> import the file again). Keep the key and the backups in two different places: if both leak together, the encryption protects nothing.
> Also keep your **age private key** safe: without it the backups cannot be opened.

**Test a restore** (do it once now, then monthly; an untested backup is only a hope). It restores into a scratch database and never touches
the real one:
```bash
# on your PC: copy your age private key to the server's memory disk (it vanishes at reboot)
scp -i C:\path\to\your-key.pem C:\path\to\age-key.txt ubuntu@payslips.example.com:/dev/shm/age-key.txt
# on the server:
ls -lh /var/backups/payslip-sender                       # pick the newest file
sudo /opt/payslip-sender/deploy/restore.sh --key /dev/shm/age-key.txt /var/backups/payslip-sender/payslip-db-<date>.sql.gz.age
shred -u /dev/shm/age-key.txt                            # remove the key from the server again
```
A good restore prints the number of users, employees, batches, payslips and audit rows, then drops the scratch database. A real
restore (`--live`) needs you to type `RESTORE`, stops the app, saves the current database first, restores, migrates and restarts.
Backups do not include the stored PDFs on purpose (they are short-lived and re-uploadable).

### Updating and rolling back
`sudo /opt/payslip-sender/deploy/update.sh` makes a safety copy of the code and an encrypted database backup, pulls the new code, installs
the pinned packages, runs the migrations, restarts and checks `/health`. If `/health` fails it tells you the exact commands to go back.
Rollback in short: put the old code back (`git checkout <old commit>` or untar the `code-<date>.tar.gz` safety copy), reinstall the pinned
packages, restart. Most updates only add columns or tables, so the old code normally still runs on the newer database; some migrations
remove or rename things though, and if the old code then fails, restore the backup made a moment before with `restore.sh --live`. Full steps: DEPLOY.md, "Updating and rolling back".

### Dependencies are pinned
`requirements.txt` and `requirements-dev.txt` list **exact** versions (`==`), also for the packages those pull in, so a server built next
month runs exactly what the tests passed with. Every pin was checked to have a Linux wheel for Python 3.12 (Ubuntu 24.04's Python).
```powershell
.\dev.ps1 install    # install the pinned packages
.\dev.ps1 test       # all tests (about 3 minutes)
.\dev.ps1 audit      # pip-audit: known vulnerabilities in the pinned versions (needs internet)
.\dev.ps1 all        # all three
```
To upgrade on purpose: change a version number, then run `.\dev.ps1 test` and `.\dev.ps1 audit`. The last audit (23 Nov 2026) found no known vulnerabilities.

## Safety notes
- `.env` holds secrets (including `ID_ENCRYPTION_KEY`) and is never committed to git.
- WhatsApp access tokens and the app secret live only in `.env`, are never logged or shown, and mock mode never touches the network.
- Full Omang or passport numbers are stored only encrypted, are hidden by default, and are never logged or exported. The only way to
  see them is the **Show IDs** button (login + audit entry + rate limit).
- Going to a real server? Read [DEPLOY.md](DEPLOY.md) and the "Putting it on a server" section above first.
- Uploaded/encrypted PDFs go in `private_storage/`, which is private and git-ignored. They are stored under random names, never served
  by the website, and deleted with their batch or after the retention period.
