#!/usr/bin/env bash
# ~/Repo/SCY-OS/email-daily/scripts/email-daily-digest.sh
# Cron pre-run script for the daily email digest job.
# Sourced env must include IMAP_USER / IMAP_PASS / (optional) OPENAI_API_KEY.
# stdout becomes the QQ message body; empty stdout = nothing pushed.
#
# Portable across macOS / Linux / Windows(git-bash). No hardcoded interpreter
# or home paths: ROOT derives from this file's own location, HERMES_HOME is
# honoured, and Python is resolved by probing candidates or $EMAIL_DAILY_PY.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_ENV="${HERMES_HOME:-$HOME/.hermes}/.env"
# Windows host stores its env under %LOCALAPPDATA%\hermes
if [[ ! -f "$HERMES_ENV" && -n "${LOCALAPPDATA:-}" ]]; then
  HERMES_ENV="$LOCALAPPDATA/hermes/.env"
fi

# --- Python resolution -------------------------------------------------------
# On Windows git-bash, `command -v python` may hit a Microsoft Store stub that
# is not a real interpreter, so every candidate is EXECUTED to confirm it runs.
_py_ok() {
  [[ -n "${1:-}" ]] || return 1
  "$1" -c 'import sys, imaplib, email, urllib.request' >/dev/null 2>&1
}

PY=""
if [[ -n "${EMAIL_DAILY_PY:-}" ]]; then
  if _py_ok "$EMAIL_DAILY_PY"; then
    PY="$EMAIL_DAILY_PY"
  else
    printf 'email-daily-digest: EMAIL_DAILY_PY not usable: %s\n' "$EMAIL_DAILY_PY" >&2
    exit 1
  fi
fi

if [[ -z "$PY" ]]; then
  for cand in python3 python py; do
    resolved="$(command -v "$cand" 2>/dev/null || true)"
    if _py_ok "$resolved"; then PY="$resolved"; break; fi
  done
fi

# Windows: Hermes-managed interpreter fallback
if [[ -z "$PY" && -n "${LOCALAPPDATA:-}" ]]; then
  shopt -s nullglob
  for cand in "$LOCALAPPDATA"/hermes/tools/python-*/python.exe; do
    if _py_ok "$cand"; then PY="$cand"; break; fi
  done
  shopt -u nullglob
fi

# Legacy mac location, last resort
if [[ -z "$PY" ]] && _py_ok /opt/miniconda3/bin/python3; then
  PY=/opt/miniconda3/bin/python3
fi

if [[ -z "$PY" ]]; then
  printf 'email-daily-digest: no usable Python found (set EMAIL_DAILY_PY)\n' >&2
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

# Build PYTHONPATH with the platform's own separator: ';' on Windows,
# ':' elsewhere. Using ':' on Windows merges the paths into one bogus entry
# and Python reports "No module named email_daily".
case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*)
    PATH_SEP=";"
    # Native python.exe does not understand MSYS paths like /c/Repo/...;
    # convert ROOT to a Windows path (C:/Repo/...).
    SRC_ROOT="$(cygpath -m "$ROOT" 2>/dev/null || printf '%s' "$ROOT")/src"
    ;;
  *)
    PATH_SEP=":"
    SRC_ROOT="$ROOT/src"
    ;;
esac
if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="${SRC_ROOT}${PATH_SEP}${PYTHONPATH}"
else
  export PYTHONPATH="$SRC_ROOT"
fi
exec "$PY" -m email_daily --since-hours 24 --max-messages 20 "$@"
