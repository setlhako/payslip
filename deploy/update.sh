#!/usr/bin/env bash
# =====================================================================================================================
# Update Payslip Sender to a newer version of the code. Run as root from anywhere:
#     sudo /opt/payslip-sender/deploy/update.sh                       (the code folder is a git clone: pulls the latest)
#     sudo /opt/payslip-sender/deploy/update.sh --archive /tmp/payslip-sender.tar.gz     (you uploaded a new copy instead)
# It: (1) makes a safety copy of the code and an encrypted database backup, (2) gets the new code, (3) installs the pinned packages,
# (4) runs the database migrations, (5) restarts the app and (6) checks /health. If /health fails it says so and tells you how to go back.
# It refuses to run while a send is in progress (a restart would interrupt it and leave rows "uncertain").
# =====================================================================================================================
set -euo pipefail

APP_USER=payslip
APP_DIR=/opt/payslip-sender
BACKUP_DIR=/var/backups/payslip-sender
PORT="$(grep -E '^APP_PORT=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' || true)"; PORT="${PORT:-8000}"
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }
as_app() { sudo -u "$APP_USER" -H "$@"; }

[ "$(id -u)" -eq 0 ] || die "Run this with sudo."
ARCHIVE=""; FORCE=0; SKIP_BACKUP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --archive) ARCHIVE="${2:-}"; shift 2 ;;
    --skip-backup) SKIP_BACKUP=1; shift ;;     # only until backups are set up (DEPLOY.md, Backups)
    --force) FORCE=1; shift ;;       # update even though a send looks active (only if you are sure)
    *) die "Unknown option: $1" ;;
  esac
done
cd "$APP_DIR"

# ---- 0. never restart in the middle of a send -------------------------------------------------------------------------------
if [ "$FORCE" -eq 0 ] && systemctl is-active --quiet payslip-sender; then
  ACTIVE="$(as_app "$APP_DIR/.venv/bin/python" - <<'PY' 2>/dev/null || echo unknown
from app.database import SessionLocal
from sqlalchemy import text
with SessionLocal() as db:
    print(db.execute(text("SELECT COUNT(*) FROM payslips WHERE status IN ('queued','sending')")).scalar())
PY
)"
  if [ "$ACTIVE" = "unknown" ]; then
    echo "WARNING: could not ask the database whether a send is running (is it down?). Continuing."
  elif [ "$ACTIVE" != "0" ]; then
    die "A send looks active (payslips queued or sending: $ACTIVE). Wait for it to finish, or press Stop on the batch page, then run this again. (--force overrides.)"
  fi
fi

# ---- 1. safety copies (the way back) ------------------------------------------------------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
say "Saving the current code to $BACKUP_DIR/code-$STAMP.tar.gz"
tar -czf "$BACKUP_DIR/code-$STAMP.tar.gz" --exclude=.venv --exclude=.env --exclude=private_storage --exclude=__pycache__ -C "$APP_DIR" .
chown "$APP_USER:$APP_USER" "$BACKUP_DIR/code-$STAMP.tar.gz"; chmod 600 "$BACKUP_DIR/code-$STAMP.tar.gz"
OLD_COMMIT=""
[ -d .git ] && OLD_COMMIT="$(git rev-parse HEAD)"
if [ "$SKIP_BACKUP" -eq 1 ]; then
  echo "WARNING: skipping the database backup (--skip-backup). A migration could not be undone."
else
  say "Making an encrypted database backup first (so a migration can be undone)"
  if ! as_app "$APP_DIR/deploy/backup.sh"; then
    die "The backup failed, so I stopped before changing anything. Fix the backup (DEPLOY.md, Backups), or run again with --skip-backup if you accept the risk."
  fi
fi

# ---- 2. new code --------------------------------------------------------------------------------------------------------------
if [ -n "$ARCHIVE" ]; then
  [ -f "$ARCHIVE" ] || die "Archive not found: $ARCHIVE"
  say "Unpacking $ARCHIVE over the code folder (.env, private storage and .venv are kept)"
  tar -xzf "$ARCHIVE" -C "$APP_DIR" --exclude=.env --exclude=.venv --exclude=private_storage
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
elif [ -d .git ]; then
  say "Pulling the latest code"
  as_app git -C "$APP_DIR" pull --ff-only
else
  die "This folder is not a git clone. Upload a new copy and use:  update.sh --archive /path/to/file.tar.gz"
fi

# ---- 3. packages, migrations, services ---------------------------------------------------------------------------------------
say "Installing the pinned Python packages"
as_app "$APP_DIR/.venv/bin/python" -m pip install -r requirements.txt
say "Running database migrations"
as_app bash -c "cd '$APP_DIR' && .venv/bin/python -m alembic upgrade head"
say "Refreshing the service files"
install -m 644 deploy/payslip-sender.service deploy/payslip-purge.service deploy/payslip-purge.timer deploy/payslip-backup.service deploy/payslip-backup.timer /etc/systemd/system/
chmod 755 deploy/*.sh
systemctl daemon-reload
say "Restarting the app"
systemctl restart payslip-sender

# ---- 4. is it healthy? ---------------------------------------------------------------------------------------------------------
for i in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"status":"ok"'; then
    say "Update finished: /health says ok."
    echo "Check the app in your browser too. Code safety copy: $BACKUP_DIR/code-$STAMP.tar.gz"
    exit 0
  fi
  sleep 2
done
cat >&2 <<MSG

The app did NOT become healthy within 40 seconds. Look at why:   sudo journalctl -u payslip-sender -n 50 --no-pager
To go back to the previous version (see DEPLOY.md, "Rolling back"):
$( [ -n "$OLD_COMMIT" ] && echo "    sudo -u $APP_USER git -C $APP_DIR checkout $OLD_COMMIT" || echo "    sudo tar -xzf $BACKUP_DIR/code-$STAMP.tar.gz -C $APP_DIR" )
    sudo -u $APP_USER $APP_DIR/.venv/bin/python -m pip install -r $APP_DIR/requirements.txt
    sudo systemctl restart payslip-sender
If the update changed the database and the old code cannot work with it, restore the encrypted backup made a moment ago (restore.sh).
MSG
exit 1
