#!/usr/bin/env bash
# =====================================================================================================================
# Payslip Sender: set up a FRESH Ubuntu 24.04 server (AWS EC2). Safe to run again: every step checks before it changes anything,
# and nothing that already exists (database, passwords, keys, .env, your Caddyfile edits) is replaced.
#
# Run it ONCE, as root, from the code folder:
#     sudo /opt/payslip-sender/deploy/setup-server.sh --domain payslips.example.com --email you@example.com
# Options:  --with-aws-cli   also install the AWS command line (only needed for optional S3 backup uploads)
#
# It installs: Python, MariaDB (database on this same machine), Caddy (HTTPS), age (backup encryption), ufw (firewall),
# unattended security upgrades. It creates the "payslip" user, the folders, the database + its user (random password), and the
# .env file. It does NOT start the app and does NOT print any password or key.
# =====================================================================================================================
set -euo pipefail

APP_USER=payslip
APP_DIR=/opt/payslip-sender
DATA_DIR=/var/lib/payslip-sender
LOG_DIR=/var/log/payslip-sender
BACKUP_DIR=/var/backups/payslip-sender
CONF_DIR=/etc/payslip-sender
DB_NAME=payslip_sender
DB_USER=payslip_app
ENV_FILE="$APP_DIR/.env"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ---- 0. checks and options --------------------------------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "Run this with sudo (it installs software)."
DOMAIN=""; EMAIL=""; WITH_AWS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --email) EMAIL="${2:-}"; shift 2 ;;
    --with-aws-cli) WITH_AWS=1; shift ;;
    *) die "Unknown option: $1" ;;
  esac
done
[[ "$DOMAIN" =~ ^([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$ ]] || die "Give your domain: --domain payslips.example.com"
[[ "$EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || die "Give your email (for HTTPS certificate warnings): --email you@example.com"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ "$SCRIPT_DIR" = "$APP_DIR/deploy" ] || die "The code must be in $APP_DIR (this script is in $SCRIPT_DIR). See DEPLOY.md, 'Get the code onto the server'."
[ -f "$APP_DIR/requirements.txt" ] || die "$APP_DIR/requirements.txt is missing: the code is not complete."
. /etc/os-release
[ "${VERSION_ID:-}" = "24.04" ] || echo "WARNING: this script was written for Ubuntu 24.04 (this is ${PRETTY_NAME:-unknown}). Continuing."
export DEBIAN_FRONTEND=noninteractive
cd "$APP_DIR"            # later steps run as the app user, which must be able to see the current folder

# ---- 1. packages ------------------------------------------------------------------------------------------------------
say "Installing packages (this takes a few minutes the first time)"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip mariadb-server ufw unattended-upgrades age git curl gnupg openssl ca-certificates \
  apt-transport-https debian-keyring debian-archive-keyring rsync

# Caddy: Ubuntu's own copy is old, so use Caddy's official apt repository (added only once).
if [ ! -f /etc/apt/sources.list.d/caddy-stable.list ]; then
  say "Adding the official Caddy package repository"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
fi
apt-get install -y caddy

if [ "$WITH_AWS" -eq 1 ] && ! command -v aws >/dev/null 2>&1; then
  say "Installing the AWS command line (for optional S3 backups)"
  snap install aws-cli --classic
fi

# ---- 2. automatic security updates ------------------------------------------------------------------------------------
# Ubuntu installs security patches by itself every day. It does NOT reboot by itself: reboot yourself now and then
# (when /var/run/reboot-required exists), at a quiet time, never in the middle of a send.
say "Turning on automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

# ---- 3. firewall: only SSH (22), HTTP (80) and HTTPS (443) ------------------------------------------------------------
# SSH is allowed BEFORE the firewall is switched on, so this cannot lock you out. (The AWS "security group" is a second wall.)
say "Setting up the firewall (ports 22, 80, 443 only)"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# ---- 4. journald keeps logs across reboots ----------------------------------------------------------------------------
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/payslip.conf <<'CONF'
[Journal]
Storage=persistent
SystemMaxUse=500M
CONF
systemctl restart systemd-journald

# ---- 5. the app user and its folders ----------------------------------------------------------------------------------
say "Creating the '$APP_USER' user and folders"
if ! id "$APP_USER" >/dev/null 2>&1; then
  # A system user: cannot log in, has no password, no sudo rights.
  useradd --system --user-group --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR" "$DATA_DIR/storage" "$LOG_DIR" "$BACKUP_DIR"
install -d -m 750 -o root -g "$APP_USER" "$CONF_DIR"
install -d -o caddy -g caddy /var/log/caddy
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 755 "$APP_DIR"/deploy/*.sh

# ---- 6. MariaDB: database + its own user ------------------------------------------------------------------------------
say "Setting up the database"
systemctl enable --now mariadb
# Only this machine may connect to the database.
cat > /etc/mysql/mariadb.conf.d/99-payslip.cnf <<'CONF'
[mysqld]
bind-address = 127.0.0.1
character-set-server = utf8mb4
collation-server = utf8mb4_unicode_ci
CONF
systemctl restart mariadb

# Small helper to read/set one value in .env without ever printing it or putting it on a command line.
get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' || true; }
set_env() { # set_env NAME   (the value comes from the variable ENV_VALUE)
  local tmp; tmp="$(mktemp)"
  NAME="$1" awk 'BEGIN{n=ENVIRON["NAME"]; v=ENVIRON["ENV_VALUE"]; done=0}
       $0 ~ "^" n "=" { if(!done){print n "=" v; done=1}; next }
       { print }
       END { if(!done) print n "=" v }' "$ENV_FILE" > "$tmp"
  cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
}

# The .env file: created once from the production template, private to the app user.
if [ ! -f "$ENV_FILE" ]; then
  say "Creating $ENV_FILE"
  install -m 600 -o "$APP_USER" -g "$APP_USER" "$SCRIPT_DIR/env.production.example" "$ENV_FILE"
fi
chown "$APP_USER:$APP_USER" "$ENV_FILE"; chmod 600 "$ENV_FILE"

# Keep the existing database password on a re-run (changing it would break the running app); make one the first time.
CURRENT_URL="$(get_env DATABASE_URL)"
if [ -n "$CURRENT_URL" ] && [[ "$CURRENT_URL" != *YOUR_DB_PASSWORD* ]]; then
  DB_PASS="$(printf '%s' "$CURRENT_URL" | sed -E 's#^[^:]+://[^:]+:([^@]+)@.*#\1#')"
else
  DB_PASS="$(head -c 4096 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c 32)"      # letters+digits only: safe inside a URL
  ENV_VALUE="mysql+pymysql://$DB_USER:$DB_PASS@127.0.0.1:3306/$DB_NAME?charset=utf8mb4" set_env DATABASE_URL
fi
# The SQL goes in through standard input, so the password never appears in the process list. "root" uses the local socket.
mariadb <<SQL
DELETE FROM mysql.global_priv WHERE User='';
DROP DATABASE IF EXISTS test;
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
ALTER USER '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
FLUSH PRIVILEGES;
SQL
# The backup job logs in with this file (the same private user, read-only for the app user).
install -m 600 -o "$APP_USER" -g "$APP_USER" /dev/null "$CONF_DIR/db-backup.cnf"
printf '[client]\nuser=%s\npassword=%s\nhost=127.0.0.1\n' "$DB_USER" "$DB_PASS" > "$CONF_DIR/db-backup.cnf"
unset DB_PASS CURRENT_URL

# ---- 7. Python environment --------------------------------------------------------------------------------------------
say "Installing Python packages (exact pinned versions)"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

# ---- 8. secrets (written straight into .env, never shown) -------------------------------------------------------------
say "Creating the session secret and the ID encryption key (if missing)"
# Both commands do nothing when a good value already exists, so re-running this script never replaces a key.
sudo -u "$APP_USER" -H "$APP_DIR/.venv/bin/python" -m app.scripts.generate_secret
sudo -u "$APP_USER" -H "$APP_DIR/.venv/bin/python" -m app.scripts.generate_encryption_key

# ---- 9. database tables -----------------------------------------------------------------------------------------------
say "Creating the database tables (migrations)"
sudo -u "$APP_USER" -H bash -c "cd '$APP_DIR' && .venv/bin/python -m alembic upgrade head"

# ---- 10. backups: settings file (the private key is NOT here, see DEPLOY.md) --------------------------------------------------
if [ ! -f "$CONF_DIR/backup.conf" ]; then
  cat > "$CONF_DIR/backup.conf" <<'CONF'
# Settings for the nightly backup (deploy/backup.sh). Plain NAME=value lines.
BACKUP_DIR=/var/backups/payslip-sender
KEEP_DAYS=14
# Optional: also copy each backup to an S3 bucket, e.g. S3_URI=s3://my-payslip-backups/db   (needs --with-aws-cli and an IAM role or keys)
S3_URI=
CONF
fi
if [ ! -f "$CONF_DIR/backup-recipients.txt" ]; then
  # Paste your age PUBLIC key(s) here (lines starting with "age1..."). Public keys are safe to keep on the server;
  # the matching PRIVATE key stays with you. Until a key is here, the backup refuses to run (it never saves an unencrypted dump).
  printf '# Paste your age PUBLIC key here, one per line (it starts with age1). See DEPLOY.md, "Backups".\n' > "$CONF_DIR/backup-recipients.txt"
fi
chown root:"$APP_USER" "$CONF_DIR"/backup.conf "$CONF_DIR"/backup-recipients.txt
chmod 640 "$CONF_DIR"/backup.conf "$CONF_DIR"/backup-recipients.txt

# ---- 11. systemd: the app, the daily clean-up and the nightly backup --------------------------------------------------------
say "Installing the services and timers"
install -m 644 "$SCRIPT_DIR/payslip-sender.service" "$SCRIPT_DIR/payslip-purge.service" "$SCRIPT_DIR/payslip-purge.timer" \
  "$SCRIPT_DIR/payslip-backup.service" "$SCRIPT_DIR/payslip-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable payslip-sender            # starts at boot (NOT started now: finish DEPLOY.md first)
systemctl enable --now payslip-purge.timer payslip-backup.timer

# ---- 12. Caddy (HTTPS) --------------------------------------------------------------------------------------------------------
say "Configuring Caddy for $DOMAIN"
if [ -f /etc/caddy/Caddyfile ] && grep -q 'Caddy reverse proxy for Payslip Sender' /etc/caddy/Caddyfile; then
  echo "Keeping your existing /etc/caddy/Caddyfile (delete it and run this script again to start from the template)."
else
  [ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak-$(date +%Y%m%d%H%M%S)"
  sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__EMAIL__/$EMAIL/g" "$SCRIPT_DIR/Caddyfile.template" > /etc/caddy/Caddyfile
fi
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable caddy
systemctl reload-or-restart caddy

# ---- done -----------------------------------------------------------------------------------------------------------------------
say "Server setup finished. Nothing secret was printed."
cat <<DONE

Next steps (full details in DEPLOY.md):
  1. BACK UP the encryption key somewhere safe and SEPARATE from the database backups (it is not in them, on purpose):
         sudo grep ID_ENCRYPTION_KEY $ENV_FILE
     Copy that one line into your password manager, then clear your terminal. Without it, stored ID numbers cannot be read.
  2. Check everything:
         sudo $APP_DIR/deploy/app-run.sh app.scripts.production_check
  3. Start the app:   sudo systemctl start payslip-sender   then:   curl -s http://127.0.0.1:8000/health
  4. Create the first admin:   sudo $APP_DIR/deploy/app-run.sh app.scripts.create_admin
  5. Set up backups (paste your age public key into $CONF_DIR/backup-recipients.txt), then:  sudo systemctl start payslip-backup
DONE
