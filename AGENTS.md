# AGENTS.md — guidance for AI agents working in this repo

## What this repo does

Daily email digest pipeline. IMAP pull → LLM summarize → stdout → cron delivers to QQ DM. Standalone git repo at `~/Repo/SCY-OS/email-daily`.

## Hard rules

1. **Do NOT add third-party Python deps.** The whole module is stdlib-only by design (`imaplib`, `urllib`, `email`). If you find yourself wanting `requests` or `beautifulsoup`, you're solving a problem this codebase explicitly avoids.
2. **Do NOT move code out of `src/email_daily/`.** The cron entrypoint imports via `PYTHONPATH=src`. Layout change = breakage.
3. **Do NOT edit `$HERMES_HOME/scripts/email-daily-digest.sh`.** It is a thin wrapper that `exec`s `scripts/email-daily-digest.sh`. The wrapper exists ONLY because Hermes cron only resolves script targets inside `$HERMES_HOME/scripts/`. Real edits belong in `scripts/email-daily-digest.sh` here.
4. **Do NOT change `deliver` format in the cron job without checking** `cronjob_manage` guidance. `deliver="qqbot"` (no hex_id) routes to the default DM thread registered in `~/.hermes/channel_directory.json`.
5. **Do NOT silently change `ALL SINCE` to `UNSEEN SINCE`** — default is ALL. If a future requirement changes this, update the `--unseen-only` flag in `core.py` AND the default in `__main__.py` together.

## Testing locally

```bash
PYTHONPATH=src python -m email_daily --dry-run --since-hours 1
bash scripts/email-daily-digest.sh    # full run, hits real IMAP + LLM
```

Windows (git-bash): `PYTHONPATH` uses `;` and native paths —
`PYTHONPATH='C:/Repo/SCY-OS/email-daily/src' python -m email_daily --dry-run`.

`--dry-run` prints what would be pushed without polluting the QQ DM.

## Portability rule

This repo is deployed on macOS and Windows. **Never hardcode an absolute path,
an interpreter location, or a `$HOME`-specific repo path.** The wrapper probes
candidate repo roots and `scripts/email-daily-digest.sh` probes interpreters by
actually executing them; keep both mechanisms intact when editing.

## Commit style

This repo uses Conventional Commits per the SCY convention (`feat(scope):`, `fix(scope):`, `chore:`). Examples from the timeline:

- `feat(core): initial IMAP fetch + LLM digest module`
- `chore(repo): migrate from ~/.hermes/scripts to standalone git repo`
- `fix(core): fall back to raw list on LLM 403`

## Failure debugging checklist

When the cron job fails, check in this order:

1. `cron/jobs.json` (read-only) → `last_status`, `last_error`, `last_delivery_error` for `defae651cee1`
2. `$HERMES_HOME/.env` (Windows: `%LOCALAPPDATA%\hermes\.env`) → IMAP_USER / IMAP_PASS / OPENAI_API_KEY still valid
3. `bash scripts/email-daily-digest.sh` → reproduce the failure manually with stderr visible
4. Sub2API dashboard → token not revoked
5. `hermes gateway status` → qqbot platform connected

## Contacts / channels

- QQ bot DM: `~/.hermes/channel_directory.json` → `platforms.qqbot[0].id`
- IMAP: `imap.qq.com:993` (SSL)
- Sub2API: `https://sub.shengchuiye.com/v1`
