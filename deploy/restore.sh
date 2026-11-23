#!/usr/bin/env bash
# =====================================================================================================================
# Restore an encrypted backup made by backup.sh. Needs YOUR age PRIVATE key file (it is not on the server).
#
# 1) TEST a restore (safe, touches nothing real): restores into a scratch database and prints what is in it.
#        sudo ./restore.sh --key /dev/shm/my-age-key.txt /var/backups/payslip-sender/payslip-db-20261123T023000Z.sql.gz.age
#    Copy the private key to /dev/shm (that is RAM, it disappears at reboot) and delete it afterwards:  shred -u /dev/shm/my-age-key.txt
#    Do this test once a month (DEPLOY.md, "Test a restore").
# 2) Restore for REAL (replaces the live database with the backup; the app is stopped meanwhile; the current database is first
#    saved to a safety file next to the backups):
#        sudo ./restore.sh --key /dev/shm/my-age-key.txt --live /var/backups/payslip-sender/payslip-db-....sql.gz.age
#    You must then type RESTORE to continue.
# Needs root (it talks to MariaDB through the local root socket).
# =====================================================================================================================
set -euo pipefail
umask 077

DB_NAME=payslip_sender
SCRATCH_DB=payslip_restore_test
BACKUP_DIR=/var/backups/payslip-sender
die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo."
KEY=""; LIVE=0; FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --key) KEY="${2:-}"; shift 2 ;;
    --live) LIVE=1; shift ;;
    -*) die "Unknown option: $1" ;;
    *) FILE="$1"; shift ;;
  esac
done
[ -n "$FILE" ] && [ -f "$FILE" ] || die "Give the backup file to restore (a payslip-db-....sql.gz.age file)."
[ -n "$KEY" ] && [ -f "$KEY" ] || die "Give your age private key file with --key (it is never kept on the server)."
command -v age >/dev/null || die "age is not installed."

load() { age -d -i "$KEY" "$FILE" | gunzip; }     # decrypts and decompresses to the screen/pipe, never to a file

# First make sure the file can be opened at all, before touching any database.
load | head -n 5 | grep -q . || die "Could not decrypt the backup: wrong key, or the file is damaged."

if [ "$LIVE" -eq 0 ]; then
  echo "Restoring into the scratch database '$SCRATCH_DB' (the real '$DB_NAME' is not touched)..."
  mariadb -e "DROP DATABASE IF EXISTS \`$SCRATCH_DB\`; CREATE DATABASE \`$SCRATCH_DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
  load | mariadb "$SCRATCH_DB"
  echo; echo "Restore worked. Rows per table in the restored copy:"
  mariadb -N "$SCRATCH_DB" -e "SELECT 'users', COUNT(*) FROM users UNION ALL SELECT 'employees', COUNT(*) FROM employees UNION ALL SELECT 'batches', COUNT(*) FROM batches UNION ALL SELECT 'payslips', COUNT(*) FROM payslips UNION ALL SELECT 'audit_log', COUNT(*) FROM audit_log;" | column -t
  echo; echo "Latest audit entry in the backup:"
  mariadb -N "$SCRATCH_DB" -e "SELECT MAX(created_at) FROM audit_log;"
  mariadb -e "DROP DATABASE \`$SCRATCH_DB\`;"
  echo; echo "Scratch database removed. If those numbers look right, this backup is good."
  exit 0
fi

echo "This REPLACES the live database '$DB_NAME' with the backup. The app will be stopped while it happens."
read -r -p "Type RESTORE to continue: " answer
[ "$answer" = "RESTORE" ] || die "Cancelled. Nothing was changed."
SAFETY="$BACKUP_DIR/before-restore-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
echo "Saving the current database to $SAFETY (NOT encrypted: delete it once you are happy, and treat it as secret)..."
systemctl stop payslip-sender || true
mysqldump --single-transaction --no-tablespaces "$DB_NAME" | gzip > "$SAFETY"
mariadb -e "DROP DATABASE \`$DB_NAME\`; CREATE DATABASE \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
load | mariadb "$DB_NAME"
echo "Restored. Bringing the app up to date with the database and starting it..."
sudo -u payslip -H bash -c "cd /opt/payslip-sender && .venv/bin/python -m alembic upgrade head"
systemctl start payslip-sender
echo "Done. Check the site, then delete the safety file when you no longer need it:  shred -u $SAFETY"
