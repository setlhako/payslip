#!/usr/bin/env bash
# Runs one of the app's commands as the app user ("payslip"), in the right folder, with the app's Python. Use it with sudo:
#     sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.production_check
#     sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.production_check --url https://payslips.example.com
#     sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.create_admin
#     sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.reset_admin_password
#     sudo /opt/payslip-sender/deploy/app-run.sh app.scripts.check_live_config
# (The app user owns the secrets file, so commands that read the settings must run as that user, from the app folder.)
set -euo pipefail

APP_DIR=/opt/payslip-sender
[ "$(id -u)" -eq 0 ] || { echo "Run this with sudo." >&2; exit 1; }
[ $# -ge 1 ] || { echo "Usage: sudo $0 app.scripts.<command> [options]" >&2; exit 1; }
module="$1"; shift
[[ "$module" =~ ^app\.[A-Za-z0-9_.]+$ ]] || { echo "That is not an app module name: $module" >&2; exit 1; }
cd "$APP_DIR"
exec sudo -u payslip -H "$APP_DIR/.venv/bin/python" -m "$module" "$@"
