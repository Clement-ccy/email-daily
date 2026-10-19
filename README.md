# email-daily

Daily email digest pipeline: pull last 24h of inbox email via IMAP, summarize via LLM, push to QQ DM through Hermes cron.

## Architecture

```
cron job (no_agent=true, deliver="qqbot")
  └─ wrapper script: $HERMES_HOME/scripts/email-daily-digest.sh
      └─ real script: scripts/email-daily-digest.sh  (this repo)
          ├─ resolves Python portably (EMAIL_DAILY_PY override → probe candidates)
          ├─ sources $HERMES_HOME/.env  (IMAP_USER / IMAP_PASS / OPENAI_API_KEY)
          └─ exec `python -m email_daily --since-hours 24 --max-messages 20`
                ├─ imaplib fetch  (default: ALL SINCE <cutoff>, not UNSEEN)
                ├─ urllib POST → LLM_BASE_URL/v1/chat/completions  (Bearer token)
                └─ stdout = QQ message body  (fallback to raw list on LLM failure)
```

The wrapper script exists because Hermes cron only resolves `script` targets inside `$HERMES_HOME/scripts/` — see [Hermes cron docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron). Edits belong here in `scripts/`, not in the wrapper.

## Portability (macOS / Linux / Windows)

Everything is resolved at runtime; no machine-specific paths are baked in.

| Concern | How it's handled |
|---------|------------------|
| Repo location | Wrapper probes `EMAIL_DAILY_ROOT` → `$HOME/Repo/...` → `%USERPROFILE%/Repo/...` → `/c/Repo/...` |
| `$HERMES_HOME` | Taken from env; on Windows falls back to `%LOCALAPPDATA%/hermes` |
| Python interpreter | `EMAIL_DAILY_PY` override, else probe `python3`/`python`/`py` **by executing** each (`command -v` can hit the Microsoft Store stub), else Hermes-managed `python.exe` |
| `PYTHONPATH` separator | `;` on MINGW/MSYS, `:` elsewhere |
| MSYS paths | `cygpath -m` converts `/c/Repo/...` → `C:/Repo/...` for native `python.exe` |
| Line endings | `.gitattributes` forces `eol=lf` for shell scripts |

Windows cron note: the job's `workdir` must be a native path (`C:\Repo\SCY-OS\email-daily`), not the macOS `/Users/...` path.

## Files

| Path | Role |
|------|------|
| `src/email_daily/core.py` | IMAP fetch + LLM summarize + fallback |
| `src/email_daily/__main__.py` | CLI entry (`python -m email_daily --since-hours N ...`) |
| `scripts/email-daily-digest.sh` | Cron entry: sources .env, execs the CLI |
| `config/config.example.json` | Schema reference (committed) |
| `config.local.json` | Local config overrides (gitignored) |
| `docs/legacy-email_digest-2026-09-16.py` | Pre-repo version snapshot for archaeology |

## Configuration

Set these in `~/.hermes/.env` (NOT here — .env files are gitignored at the Hermes level):

```bash
IMAP_HOST=imap.qq.com
IMAP_PORT=993
IMAP_USER=your-qq-email
IMAP_PASS=your-qq-authorization-code   # 设置 → 账户 → IMAP/SMTP 授权码, not login password
OPENAI_API_KEY=sk-...                  # sub2api gateway token
LLM_BASE_URL=https://sub.shengchuiye.com/v1
LLM_MODEL=MiniMax-M3
```

`LLM_BASE_URL` defaults to the Sub2API gateway running on the SCY VPS. Adjust the model name as upstream Sub2API changes its catalog.

## Cron registration

Existing job (`defae651cee1`, name `email-daily-digest-qq`) is already wired:

```yaml
schedule:        0 9 * * *           # 09:00 CST daily
script:          email-daily-digest.sh (wrapper under $HERMES_HOME/scripts/)
workdir:         C:\Repo\SCY-OS\email-daily   # native path per host
deliver:         qqbot                # default DM thread
no_agent:        true                 # script stdout → QQ DM
failure_deliver: qqbot                # error alert also to QQ DM
```

## Development

```bash
# Dry run (no LLM if key unset; uses fallback)
PYTHONPATH=src python -m email_daily --dry-run --since-hours 1

# Windows git-bash: PYTHONPATH needs ';' and a native path
PYTHONPATH='C:/Repo/SCY-OS/email-daily/src' python -m email_daily --dry-run --since-hours 1

# Real run with manual inspection (portable entry: resolves repo + python itself)
bash "$HERMES_HOME/scripts/email-daily-digest.sh" --dry-run
bash scripts/email-daily-digest.sh
```

No third-party Python deps — stdlib only (`imaplib`, `urllib`, `email`).

## Scope

Default scope is `ALL SINCE <cutoff>` (read + unread) — digest summarises **everything** received in the window. Pass `--unseen-only` to restrict to UNSEEN for the legacy narrow behaviour.

## Failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `⚠️ IMAP_USER/IMAP_PASS 未配置` | .env not loaded or empty | Check `$HERMES_HOME/.env` (`%LOCALAPPDATA%\hermes\.env` on Windows) |
| `⚠️ IMAP 抓取失败` | Network / wrong host / wrong pass | Verify IMAP_HOST/PORT and 授权码 |
| `No module named email_daily` | `PYTHONPATH` used `:` on Windows, or an MSYS `/c/...` path | Use `;` separator + native `C:/...` path (script does this automatically) |
| `Python not found` / Store stub | `command -v python` hit the Microsoft Store alias | Set `EMAIL_DAILY_PY` to a real interpreter |
| `LLM call failed: HTTP 403` | Token rejected by Sub2API | Refresh OPENAI_API_KEY in VPS Sub2API dashboard |
| `delivery error: QQBot send failed` | gateway / channel disconnected | `hermes gateway status` + QQ bot reconnect |
