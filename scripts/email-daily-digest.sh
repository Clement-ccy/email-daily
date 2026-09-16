#!/usr/bin/env bash
# ~/Repo/SCY-OS/email-daily/scripts/email-daily-digest.sh
# Cron pre-run script for the daily email digest job.
# Sourced env must include IMAP_USER / IMAP_PASS / (optional) OPENAI_API_KEY.
# stdout becomes the QQ message body; empty stdout = nothing pushed.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${EMAIL_DAILY_PY:-/opt/miniconda3/bin/python3}"
HERMES_ENV="${HERMES_HOME:-$HOME/.hermes}/.env"

if [[ ! -x "$PY" ]]; then
  printf 'email-daily-digest: Python not found: %s\n' "$PY" >&2
  exit 1
fi
if [[ ! -f "$HERMES_ENV" ]]; then
  printf 'email-daily-digest: hermes env not found: %s\n' "$HERMES_ENV" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$HERMES_ENV"
set +a

export PYTHONPATH="$ROOT/src:${PYTHONPATH:-}"
exec "$PY" -m email_daily --since-hours 24 --max-messages 20
