# Putting Payslip Sender on a server (AWS EC2, Ubuntu 24.04)

A step-by-step guide for someone who has never run a server. Do the steps in order. Nothing here sends a real WhatsApp message until
the **go-live checklist** at the end: the app starts in **MOCK mode** (pretend sends) and stays there until you change it on purpose.

**What you will end up with**

```
Employees' phones <-- WhatsApp (Meta) <-->  https://payslips.yourcompany.example   (HTTPS, certificate renewed by itself)
                                                         |
                                                    [ Caddy ]  ports 80/443, the only thing the internet can reach
                                                         |  127.0.0.1:8000 (this machine only)
                                                  [ Payslip Sender ]  ONE process, runs as the "payslip" user, restarts if it crashes
                                                         |
                                                  [ MariaDB ]  same machine, nothing outside can reach it
```

**Time:** about 1 to 2 hours the first time. **Cost:** roughly 15 to 25 US dollars a month for a small server, an Elastic IP while it is
attached is free, plus pennies for disk and backups (check AWS's pricing page for today's numbers).

**Honest note.** The server files in `deploy/` were written and tested for syntax, logic and safety rules on a Windows PC (no Ubuntu server or
Caddy was available there). They follow each tool's documentation, and the setup script checks the Caddy config before using it, but the
first run on a real server is the real test. Do your first run on this server *before* any employee data is on it, and read the output of each step.

**Three rules this guide follows (on purpose):**
1. **The production database starts EMPTY.** Nothing is copied from your PC: no local test employees, batches or users. You add the real employees by import and create the admin on the server.
2. **Production gets its own NEW secrets.** The setup script generates a fresh `SESSION_SECRET` and a fresh `ID_ENCRYPTION_KEY` on the server. Never copy your PC's `.env` or its keys to the server (and never reuse the key you used with test data).
3. **The database is private to the machine.** MariaDB (Ubuntu's `mariadb-server`) listens on `127.0.0.1` only, with its own database and user and a generated password that is written to files and never printed. **Port 3306 is never opened**: not in `ufw`, not in the AWS security group. Do not add it, not even "temporarily".

---

## 0. What you need before you start

- An **AWS account** with a payment method.
- A **domain name** you control (for example `yourcompany.co.bw`) so you can create a *subdomain* like `payslips.yourcompany.co.bw`.
- A **Meta (WhatsApp Business) account** with an approved message template (see step 12). You can build and test everything else first.
- Your **code**, in a place you can copy it from (step 6).
- The free tool **age** on your own PC, to make the key that protects your backups (step 1).

## 1. Make your backup key (on your own PC, 5 minutes)

Backups are encrypted with a **public key** that lives on the server. The matching **private key** stays with you, so even someone who
steals the server or a backup file cannot read it. Without the private key *you* cannot open the backups either, so keep it safe.

In PowerShell:
```powershell
winget install FiloSottile.age
# close and reopen PowerShell, then:
age-keygen -o age-key.txt
```
It prints a line like `Public key: age1qqq...`. **Copy that whole `age1...` line somewhere** (you will paste it on the server in step 11).
The file `age-key.txt` is the **private key**: put a copy in your password manager and one on a USB stick, then delete it from Downloads/Desktop.
**Never** put it on the server except for a few minutes while testing a restore (step 11).

## 2. Launch the server (AWS EC2)

1. Sign in to the AWS console. Top right, choose a **region** close to you: *Africa (Cape Town) af-south-1* (it may need enabling first under
   Account > AWS Regions) or *Europe (Ireland) eu-west-1*.
2. **EC2 > Launch instance**.
   - **Name:** `payslip-sender`.
   - **Image:** *Ubuntu Server 24.04 LTS* (64-bit x86).
   - **Instance type:** `t3.small` (2 GB memory). A `t3.micro` (1 GB) is too tight for the database plus the app.
   - **Key pair:** *Create new key pair*, type RSA, format `.pem`. It downloads **once**: keep it safe (it is your only way in).
   - **Network settings > Create security group**, with exactly these rules:

     | Type  | Port | Source |
     |-------|------|--------|
     | SSH   | 22   | **My IP** (only your current address) |
     | HTTP  | 80   | Anywhere (needed to get the HTTPS certificate) |
     | HTTPS | 443  | Anywhere |

   - **Storage:** 20 GiB `gp3`. Tick **Encrypted**.
3. **Launch instance.**

Your home IP can change. If you cannot connect over SSH later, edit the SSH rule in the security group to your new IP (*EC2 > Security Groups > Edit inbound rules > My IP*).

## 3. Give it a fixed address (Elastic IP)

Without this the public address changes when the server restarts and your DNS record would break.
**EC2 > Elastic IPs > Allocate Elastic IP address > Allocate**, then **Actions > Associate Elastic IP address**, choose your instance. Write down the address.

## 4. Point a subdomain at it (DNS)

Where you manage your domain, add an **A record**: name `payslips` (or whatever you chose), value = the Elastic IP, TTL 300.
Check it from PowerShell (it can take from a minute to a few hours):
```powershell
nslookup payslips.yourcompany.example
```
The answer must show your Elastic IP. **Do not continue until it does**: Caddy can only get the HTTPS certificate once the name points at the server.

## 5. Connect to the server (SSH from Windows PowerShell)

Put the `.pem` file in a folder such as `C:\Users\you\aws\`. Windows insists that only you can read it:
```powershell
cd C:\Users\you\aws
icacls .\payslip-key.pem /inheritance:r
icacls .\payslip-key.pem /grant:r "$($env:USERNAME):(R)"
ssh -i .\payslip-key.pem ubuntu@payslips.yourcompany.example
```
The first time it asks *Are you sure you want to continue connecting?*: type `yes`. You now see a prompt like `ubuntu@ip-172-31-...:~$`.
Everything in the **bash** boxes below is typed there. `Ctrl+D` (or `exit`) leaves. If it says *Permission denied (publickey)*, the key file or the
user name is wrong (the user is `ubuntu`). If it times out, your IP no longer matches the security group.

## 6. Get the code onto the server

The code must end up in **`/opt/payslip-sender`**. Pick ONE option.

**Option A: from a private git repository (best, updates become one command).** Put the project in a *private* repository (GitHub, GitLab, ...).
Never a public one. On the server:
```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/YOU/payslip-sender.git /opt/payslip-sender
```
(For a private repo git asks for a user name and a **personal access token** with read-only access to that one repository, instead of your password.)

**Option B: copy a zip from your PC (no git needed).** In PowerShell on **your PC**:
```powershell
cd C:\Development\payslip-sender
tar -czf $env:TEMP\payslip-sender.tar.gz --exclude=.venv --exclude=.env --exclude=private_storage --exclude=test_payslips --exclude=__pycache__ --exclude=.git --exclude=.pytest_cache .
scp -i C:\Users\you\aws\payslip-key.pem $env:TEMP\payslip-sender.tar.gz ubuntu@payslips.yourcompany.example:/tmp/
```
On the server:
```bash
sudo mkdir -p /opt/payslip-sender
sudo tar -xzf /tmp/payslip-sender.tar.gz -C /opt/payslip-sender
```
Your real `.env` (secrets) is deliberately **not** copied. The server makes its own.

Check: `ls /opt/payslip-sender/deploy` lists `setup-server.sh`, `update.sh`, `backup.sh` ... If a script later says *bad interpreter* or *^M*, a Windows
editor changed its line endings; fix with `sudo sed -i 's/\r$//' /opt/payslip-sender/deploy/*.sh`.

## 7. Run the setup script (installs everything, ~10 minutes)

```bash
sudo bash /opt/payslip-sender/deploy/setup-server.sh --domain payslips.yourcompany.example --email you@yourcompany.example
```
(The email is only for certificate expiry warnings.) It is **safe to run again** if something goes wrong halfway: it never replaces your database,
passwords, keys, `.env` or a Caddyfile you edited. Every step is commented in the file if you want to read it. It:

| Step | What |
|------|------|
| Packages | Python, MariaDB, Caddy (official repository), age, ufw, unattended-upgrades |
| Security updates | Ubuntu installs security patches every day by itself (it does **not** reboot by itself; see "Day to day") |
| Firewall | `ufw`: only ports 22, 80, 443 open, everything else closed. SSH is allowed first, so you cannot lock yourself out |
| User + folders | a `payslip` user with no login and no sudo; private folders for the data (`/var/lib/payslip-sender`), logs and backups |
| Database | MariaDB listening on this machine only; a database and its own user with a **random password written to a file, never printed** |
| App | a Python virtual environment with the exact pinned packages, `.env` (mode 600, only the `payslip` user can read it), a session secret and the ID encryption key (created, **never printed**), database tables |
| Services | the app (systemd, restarts on failure), a daily clean-up timer, a nightly backup timer |
| Caddy | HTTPS for your domain, HSTS, compression, upload size limit, access log without query strings |

It ends with a short list of next steps. The app is **not started yet**.

## 8. Back up the encryption key (do not skip)

The employees' full ID numbers are stored encrypted. This server's key was generated here (it is not the key from your PC). It lives only in `/opt/payslip-sender/.env` and is **not** in the database backups on purpose.
**Lose the key and stored ID numbers become unreadable.** Never keep it in the same place as the database backups.
```bash
sudo grep ID_ENCRYPTION_KEY /opt/payslip-sender/.env
```
Copy that **one line** into your password manager (a different place from your age private key and from any backup files), then clear the screen with `clear`.

## 9. Fill in the settings

The setup created `/opt/payslip-sender/.env` for you. Open it as the app user:
```bash
sudo -u payslip nano /opt/payslip-sender/.env
```
(nano: arrow keys move, type to edit, `Ctrl+O` then `Enter` saves, `Ctrl+X` leaves.) For now change only what you know; **leave `WHATSAPP_MODE=mock`**.
You can fill the WhatsApp values in step 12. Do **not** touch `SESSION_SECRET`, `ID_ENCRYPTION_KEY`, `DATABASE_URL` or `APP_ENV`.

## 10. First start

```bash
sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.production_check
```
Read it. Expect **PASS** with one **WARN** that `WHATSAPP_MODE` is MOCK (correct for now). Any **FAIL** comes with the fix in plain words. Then:
```bash
sudo systemctl start payslip-sender
curl -s http://127.0.0.1:8000/health            # prints {"status":"ok"}
sudo systemctl status payslip-sender --no-pager  # "active (running)"
sudo journalctl -u payslip-sender -n 30 --no-pager
```
Create the first admin (you are asked for a user name and a password of 12+ characters):
```bash
sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.create_admin
```
Now open `https://payslips.yourcompany.example` in your browser. The first visit can take up to a minute while Caddy gets the certificate.
You should see the login page with a padlock and a **MOCK** pill at the top. Log in, add an employee, upload a fake payslip batch and send it: in mock mode
nothing real leaves the server. Check the whole server (including the public side) from the server:
```bash
sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.production_check --url https://payslips.yourcompany.example
```

## 11. Backups and a restore test

1. Give the server your **public** key (the `age1...` line from step 1, not the private file):
   ```bash
   echo "age1PASTE-YOUR-PUBLIC-KEY-HERE" | sudo tee -a /etc/payslip-sender/backup-recipients.txt
   ```
2. Run a backup now and look at it:
   ```bash
   sudo systemctl start payslip-backup
   sudo journalctl -u payslip-backup -n 15 --no-pager
   ls -lh /var/backups/payslip-sender
   ```
   It also runs by itself every night (about 02:30 server time, which is UTC) and keeps 14 days. List the schedule with `systemctl list-timers`.
3. **Test a restore now, and once a month.** On your PC copy the private key over, on the server restore into a *scratch* database (the real one is
   untouched), then remove the key again:
   ```powershell
   scp -i C:\Users\you\aws\payslip-key.pem C:\path\to\age-key.txt ubuntu@payslips.yourcompany.example:/tmp/age-key.txt
   ```
   ```bash
   sudo mv /tmp/age-key.txt /dev/shm/age-key.txt        # /dev/shm is memory: it disappears at reboot
   sudo /opt/payslip-sender/deploy/restore.sh --key /dev/shm/age-key.txt /var/backups/payslip-sender/payslip-db-DATE.sql.gz.age
   sudo shred -u /dev/shm/age-key.txt
   ```
   You want to see row counts (users, employees, batches, payslips, audit_log) and "Restore worked". If you cannot open it, your key does not match: fix that *today*.
4. **Optional: copy backups off the server (S3).** Create a private S3 bucket, give the instance an IAM role that may only write to it, run the setup
   again with `--with-aws-cli`, then set `S3_URI=s3://your-bucket/payslip-db` in `/etc/payslip-sender/backup.conf`. Backups are already encrypted before they leave.
5. A real disaster (the server is gone): launch a new server (steps 2 to 7), restore with `restore.sh --live`, put `ID_ENCRYPTION_KEY` back into `.env` **before**
   starting the app, start it. The backups alone cannot show ID numbers without that key.

Remember the two secrets to keep apart: the **age private key** (opens the backups) and the **ID encryption key** (opens the ID numbers inside them).

## 12. Connect WhatsApp (Meta)

Meta's screens change often; the names below are the ones to look for. Official help: Meta for Developers > WhatsApp Cloud API > Get started and > Webhooks.

1. **Message template.** In WhatsApp Manager create a template named like `WHATSAPP_TEMPLATE_NAME` (default `payslip_`), language `en`, category *Utility*, with a
   **document header** and a body with exactly **two variables**: `{{1}}` = employee name, `{{2}}` = pay month. Wait until it says **APPROVED**.
2. **Values for `.env`** (`sudo -u payslip nano /opt/payslip-sender/.env`), keeping `WHATSAPP_MODE=mock` for now:
   - `WHATSAPP_PHONE_NUMBER_ID` and `WHATSAPP_BUSINESS_ACCOUNT_ID`: two *different* numbers, both shown in WhatsApp Manager / the API setup page.
   - `WHATSAPP_ACCESS_TOKEN`: use a **permanent** token from a *System User* in Meta Business Settings (with the WhatsApp messaging and management permissions). The
     temporary 24-hour token on the getting-started page stops working the next day.
   - `WHATSAPP_APP_SECRET`: App settings > Basic > App secret. It is used to check that webhook calls really come from Meta.
   - `WHATSAPP_VERIFY_TOKEN`: **you invent it**: a long random text, for example the output of `openssl rand -hex 24`. Meta will send it back when you set the webhook.
   - `GRAPH_API_VERSION` (like `v23.0`): check Meta's current version.
   - `WHATSAPP_ALLOWED_RECIPIENTS=+26771234567`: **your own number**. Keep it until the go-live checklist says to clear it.
   Restart: `sudo systemctl restart payslip-sender`.
3. **Preflight (sends nothing):**
   ```bash
   sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.check_live_config
   ```
   Repeat until it says `RESULT: PASS`.
4. **Webhook.** In the Meta App Dashboard: *WhatsApp > Configuration > Webhook > Edit*:
   - **Callback URL:** `https://payslips.yourcompany.example/webhook/whatsapp`
   - **Verify token:** the exact `WHATSAPP_VERIFY_TOKEN` value from `.env`
   - Press **Verify and save**. If it fails, the app is not reachable, the token differs, or the app was not restarted after you edited `.env`
     (`sudo journalctl -u payslip-sender -n 30 --no-pager`; a wrong token shows as a 403).
   - Under **Webhook fields** press **Subscribe** next to **`messages`**. (Delivered/read/failed updates for the payslips you send arrive through this field.)
   Once the app has seen a correctly signed call from Meta, **System status** (admin) shows "Last webhook event". Nothing in the app can be changed by anyone who
   does not know your app secret: unsigned calls are rejected with 403.

## 13. Go-live checklist

Tick every line, in order. Do not send real payslips before all are ticked.

- [ ] DNS points at the Elastic IP; `https://payslips.yourcompany.example` shows the login page with a padlock.
- [ ] SSH rule in the security group is **only your IP**; ports 80/443 are open to the world; nothing else (check *EC2 > Security Groups*).
- [ ] `production_check --url https://...` says **RESULT: PASS** (only the MOCK/allowlist warnings may remain, and you understand them).
- [ ] The ID encryption key is saved in your password manager, **separately** from your backups.
- [ ] A backup file exists in `/var/backups/payslip-sender`, and you **restored it successfully** (step 11.3) with your age private key.
- [ ] Your age private key is stored in two safe places that are not the server.
- [ ] You created an admin; the other people have `hr` or `viewer` accounts, with temporary passwords they changed ("Users" page).
- [ ] Employees and their Omang/passport numbers and phone numbers are imported; spot-check a few.
- [ ] The Meta template is **APPROVED**, with a document header and two variables.
- [ ] `check_live_config` says **RESULT: PASS**.
- [ ] The webhook is verified and subscribed to `messages`.
- [ ] `WHATSAPP_ALLOWED_RECIPIENTS` contains only **your own number**. In `.env` set `WHATSAPP_MODE=live`, then `sudo systemctl restart payslip-sender`.
      The top bar now says **LIVE**.
- [ ] You sent ONE batch (for example one payslip for an employee record that has *your* phone number) and: it arrived on your phone, the PDF asks for the
      password (last 4 characters of the ID), the password opens it, and the status reached **delivered / read** in the app (this proves the webhook works).
      Also check that a number outside the allowlist fails with a clear reason (that is the safety switch working).
- [ ] You cleared `WHATSAPP_ALLOWED_RECIPIENTS` in `.env` and restarted. `production_check` no longer warns about it.
- [ ] You know who to tell and what to do if a payslip goes to the wrong person (stop the batch; the audit log shows who sent what).
- [ ] Sending the first real batch: start with a **small** group, check a couple of them yourself, then do the rest.

## Updating and rolling back

**Update** (new version of the code):
```bash
sudo /opt/payslip-sender/deploy/update.sh                                    # option A: the code folder is a git clone
sudo /opt/payslip-sender/deploy/update.sh --archive /tmp/payslip-sender.tar.gz   # option B: you copied a new zip (step 6B)
```
It refuses to run while a send is in progress (a restart would leave in-flight payslips "uncertain"). It saves the current code and an encrypted
database backup, gets the new code, installs the pinned packages, runs the database migrations, restarts the app and checks `/health`.
(`--skip-backup` exists for the very first updates before backups are set up; `--force` ignores the send check: use neither casually.)

**The update that makes phone and ID numbers unique** (two migrations, see the README section "Unique ID and phone numbers"): on a server that
already has employees the second migration may stop with *"Cannot make phone and ID numbers unique yet"*. Nothing was broken or changed; the
update stops before restarting, so the old version keeps running. Do this, then run `update.sh` again (or the commands below by hand):
```bash
sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.backfill_id_hash     # hashes the stored IDs and lists duplicates (names and employee numbers only)
# fix what it lists in the app (change or clear a phone; "Remove the saved ID number"), run the line above again until it says "No duplicates found"
cd /opt/payslip-sender && sudo -u payslip -H .venv/bin/python -m alembic upgrade head      # now the unique indexes are created
sudo systemctl restart payslip-sender
```

**Rolling back** if the update fails or the new version misbehaves (`update.sh` prints these exact commands when `/health` fails):
1. Put the old code back. Git clone: `sudo -u payslip git -C /opt/payslip-sender checkout OLD_COMMIT` (the update printed it; or `git log --oneline`).
   Zip: `sudo tar -xzf /var/backups/payslip-sender/code-DATE.tar.gz -C /opt/payslip-sender`.
2. `sudo -u payslip /opt/payslip-sender/.venv/bin/python -m pip install -r /opt/payslip-sender/requirements.txt`
3. `sudo systemctl restart payslip-sender` and check `curl -s http://127.0.0.1:8000/health`.

Most updates only *add* columns or tables, so the old code keeps working on the newer database. Some migrations remove or rename things; if the old code then
fails, restore the encrypted backup that `update.sh` made just before (`restore.sh --live`, see step 11). Anything sent between the update and the rollback is lost with it, so roll back quickly.

## Day to day

| I want to | Command (on the server) |
|-----------|-------------------------|
| See if the app is running | `sudo systemctl status payslip-sender --no-pager` |
| Watch the log live | `sudo journalctl -u payslip-sender -f` (Ctrl+C stops watching) |
| Restart the app | `sudo systemctl restart payslip-sender` (avoid during a send: in-flight payslips become "uncertain") |
| Check the whole setup | `sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.production_check` |
| See the timers (backup, clean-up) | `systemctl list-timers` |
| Run the clean-up now | `sudo systemctl start payslip-purge` |
| See disk space | `df -h /` |
| Caddy log (no query strings) | `sudo tail -f /var/log/caddy/payslip-access.log` |
| Forgot the admin password | `sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.reset_admin_password` |

- **Security updates** install by themselves. A few times a year the server asks for a reboot (`/var/run/reboot-required` exists, or the login banner says so).
  Do it when no send is running: `sudo reboot`. Everything comes back by itself (app, database, Caddy, timers). If a send *was* running, those payslips come back
  as "uncertain" and are never re-sent automatically.
- **HTTPS certificates** renew by themselves (Caddy). Keep ports 80 and 443 open.
- **Stored payslip PDFs** are deleted 7 days after a batch finished sending (30 days at most): `SENT_RETENTION_DAYS` / `BATCH_RETENTION_DAYS` in `.env`, run daily by `payslip-purge.timer`.
- **Disk space:** the database and 14 days of backups are small. If `production_check` warns about space, enlarge the volume in the AWS console (EC2 > Volumes > Modify) and run `sudo growpart /dev/nvme0n1 1 && sudo resize2fs /dev/nvme0n1p1`.
- **Do not** stop the instance to save money without remembering that the Elastic IP then costs a little while it is not attached.

## Optional: only let your office reach the admin pages

By default anybody on the internet can *see the login page* (they cannot do anything without a password; five wrong tries lock that username for 15 minutes).
To hide the whole app except from your office, edit the Caddyfile: `sudo nano /etc/caddy/Caddyfile`, find the block starting `# @outsiders {`, put your
addresses after `remote_ip` (single addresses or ranges such as `203.0.113.0/24`), remove the `#` at the start of those five lines, then:
```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && sudo systemctl reload caddy
```
Everyone else gets a plain "404 Not Found". **`/webhook/whatsapp` and `/health` stay open** on purpose: Meta must reach the webhook (it is protected by its signature,
not by address). If your own address changes you lock yourself out of the web pages; fix it over SSH by putting the `#` back. Re-running `setup-server.sh` keeps your edited Caddyfile.

## When something goes wrong

| Symptom | Likely cause and fix |
|---------|----------------------|
| Browser: "502 Bad Gateway" | The app is not running: `sudo systemctl status payslip-sender`, then `sudo journalctl -u payslip-sender -n 50 --no-pager` |
| Browser: certificate error / cannot connect | DNS does not point at the Elastic IP yet, ports 80/443 closed in the security group, or `sudo systemctl status caddy` / `sudo journalctl -u caddy -n 50 --no-pager` |
| App will not start: "Refusing to start: ..." | The message names the setting that is unsafe or missing. Fix it in `.env`, then `sudo systemctl restart payslip-sender` |
| "Another payslip-sender process is already running" | Two copies are trying to run. `sudo systemctl status payslip-sender`; make sure you did not also start it by hand |
| `production_check`: cannot connect to the database | `sudo systemctl status mariadb`; compare the user/password in `DATABASE_URL` (setup wrote it; re-running setup repairs the database user) |
| Meta says it could not verify the callback | Token differs from `WHATSAPP_VERIFY_TOKEN`, the app was not restarted after editing `.env`, or the URL is wrong. A 403 in the app log = wrong token |
| Statuses stay "sent" and never reach "delivered" | The webhook is not verified/subscribed to `messages`, or `WHATSAPP_APP_SECRET` is wrong (the log says "bad or missing signature") |
| Everything fails with "not on the allowed list" | `WHATSAPP_ALLOWED_RECIPIENTS` is still set (clear it after your test, step 13) |
| Uploads of big batches fail | The limits are 10 MB per PDF and 200 MB per batch (`MAX_UPLOAD_MB`, `MAX_BATCH_TOTAL_MB`); Caddy allows 230 MB per upload request |
| SSH "timed out" | Your public IP changed: update the SSH rule in the security group |
| Locked out of the app (no admin) | Use the `reset_admin_password` command in "Day to day" over SSH |

## What this setup protects, and what it does not

Protected: traffic is HTTPS only; the app and the database are not reachable from the internet; the firewall and the security group allow only 22/80/443; SSH only from your IP;
secrets live in one file only the app user can read; the app runs without root rights in a sandbox; security updates install by themselves; payslip PDFs are deleted after 7 days;
ID numbers are encrypted; backups are encrypted with a key that is not on the server; logs never hold tokens, passwords, phone numbers, ID numbers or PDF content.

Not covered (be aware, decide later): one server means no automatic failover (a restore takes time); no web application firewall or DDoS protection; no intrusion monitoring
(for example fail2ban or CloudWatch alarms); anyone with admin rights in your AWS account or SSH to the server can read the `.env`; malware on an HR user's PC can do what that user can.
These are normal for a small deployment; revisit them as the company grows.
