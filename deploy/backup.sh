#!/usr/bin/env bash
# =====================================================================================================================
# Nightly ENCRYPTED database backup. Started by payslip-backup.timer (as the "payslip" user). Run by hand:
#     sudo systemctl start payslip-backup        (then:  sudo journalctl -u payslip-backup -n 20 --no-pager)
#
# What it does: dumps the database, compresses it, and encrypts it with age using your PUBLIC key(s) in
# /etc/payslip-sender/backup-recipients.txt. The matching PRIVATE key is NOT on the server, so a stolen backup (or a stolen server)
# cannot be read without it. It never writes an unencrypted dump to disk: the dump goes straight through gzip and age.
# Keeps KEEP_DAYS (default 14) days of files in BACKUP_DIR; optionally copies each file to S3 (S3_URI in backup.conf).
#
# IMPORTANT: the dump does NOT contain ID_ENCRYPTION_KEY (it lives in .env). The employees' stored ID numbers inside the dump
# are encrypted with that key. Keep a copy of the key SOMEWHERE ELSE than your database backups (password manager), or a restored
# database cannot show those ID numbers. And never store the key and the backups in the same place.
# =====================================================================================================================
set -euo pipefail
umask 077

CONF_DIR="${PAYSLIP_CONF_DIR:-/etc/payslip-sender}"
BACKUP_DIR=/var/backups/payslip-sender
KEEP_DAYS=14
S3_URI=""
# shellcheck disable=SC1091
[ -f "$CONF_DIR/backup.conf" ] && . "$CONF_DIR/backup.conf"
DB_NAME="${DB_NAME:-payslip_sender}"
RECIPIENTS="$CONF_DIR/backup-recipients.txt"
CNF="$CONF_DIR/db-backup.cnf"

die() { echo "BACKUP FAILED: $*" >&2; exit 1; }

command -v age >/dev/null || die "'age' is not installed (sudo apt install age)."
command -v mysqldump >/dev/null || die "'mysqldump' is not installed."
[ -r "$CNF" ] || die "Cannot read $CNF (setup-server.sh creates it)."
# At least one real public key must be present, otherwise we would have to leave the dump unencrypted: so we refuse.
grep -Eq '^age1[a-z0-9]{50,}' "$RECIPIENTS" 2>/dev/null || die "No age public key in $RECIPIENTS. Add yours (see DEPLOY.md, 'Backups'). Nothing was backed up."
[[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] && [ "$KEEP_DAYS" -ge 1 ] || die "KEEP_DAYS in backup.conf must be a number of days (1 or more)."
mkdir -p "$BACKUP_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL="$BACKUP_DIR/payslip-db-$STAMP.sql.gz.age"
PARTIAL="$FINAL.partial"
trap 'rm -f "$PARTIAL"' EXIT                       # a half-written file is never left behind as if it were a backup

# --single-transaction: a consistent snapshot without locking the app. --no-tablespaces: needs no special privilege.
# The password comes from the private options file, never from the command line.
mysqldump --defaults-extra-file="$CNF" --single-transaction --no-tablespaces --routines --triggers --default-character-set=utf8mb4 "$DB_NAME" \
  | gzip -9 \
  | age -R "$RECIPIENTS" -o "$PARTIAL"
# "pipefail" above makes the line fail if ANY of the three programs failed.

[ -s "$PARTIAL" ] || die "The backup file is empty."
mv "$PARTIAL" "$FINAL"
chmod 600 "$FINAL"
echo "Backup written: $FINAL ($(du -h "$FINAL" | cut -f1))"

# Optional off-server copy. A failure here is reported but the local backup stays.
if [ -n "$S3_URI" ]; then
  if command -v aws >/dev/null; then
    if aws s3 cp "$FINAL" "${S3_URI%/}/$(basename "$FINAL")" --only-show-errors; then
      echo "Copied to S3."
    else
      echo "WARNING: the copy to S3 failed (the local backup is fine). Check the IAM role/keys and the bucket name." >&2
      S3_FAILED=1
    fi
  else
    echo "WARNING: S3_URI is set but the 'aws' command is not installed (run setup-server.sh --with-aws-cli)." >&2
    S3_FAILED=1
  fi
fi

# Keep KEEP_DAYS days of local backups, never fewer than the newest one.
find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'payslip-db-*.sql.gz.age' -o -name 'payslip-db-*.partial' \) -mtime +"$KEEP_DAYS" -delete
echo "Local backups kept: $(find "$BACKUP_DIR" -maxdepth 1 -name 'payslip-db-*.sql.gz.age' | wc -l) (older than $KEEP_DAYS days are deleted)."
[ -z "${S3_FAILED:-}" ] || exit 2
