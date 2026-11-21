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
5. Create the database and a limited user in XAMPP's MySQL (change the password!):
   ```powershell
   C:\xampp\mysql\bin\mysql.exe -u root -e "CREATE DATABASE payslip_sender CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER 'payslip_app'@'localhost' IDENTIFIED BY 'CHOOSE_A_STRONG_PASSWORD'; GRANT ALL PRIVILEGES ON payslip_sender.* TO 'payslip_app'@'localhost'; FLUSH PRIVILEGES;"
   ```
   Put the same password into `DATABASE_URL` in `.env`.
6. Create the tables:
   ```powershell
   alembic upgrade head
   ```

## Create your admin login (once)

In a **terminal** (not the VS Code "Run" button), with the virtual environment active:
```powershell
python -m app.scripts.create_admin
```
It asks for a username and a password (at least 12 characters). The password is typed hidden and never
appears on screen or in your shell history. Run it again to add another admin.
Forgot the password? `python -m app.scripts.reset_admin_password`.
There is deliberately no way to create or reset admins from the website.

## Run the tests

```powershell
pip install -r requirements-dev.txt
pytest
```
Tests use a temporary in-memory database; they never touch your real data.

## Start the app (every time)

1. Start XAMPP -> MySQL.
2. In the project folder:
   ```powershell
   .\.venv\Scripts\Activate.ps1
   uvicorn app.main:app --reload
   ```
3. Open http://127.0.0.1:8000. You are sent to the login page; log in with your admin. http://127.0.0.1:8000/health should say `{"status":"ok"}`.
4. Stop the server with `Ctrl+C`.

## Changing the database later

After editing a model in `app/models/`:
```powershell
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

## Safety notes
- `.env` holds secrets and is never committed to git.
- Full Omang numbers are never stored — only the last 4 digits.
- Uploaded/encrypted PDFs go in `private_storage/`, which is private and git-ignored.
