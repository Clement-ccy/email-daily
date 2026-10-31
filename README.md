# email-daily

Daily email digest pipeline: pull last 24h of inbox email via IMAP, summarize via LLM, push to QQ DM through Hermes cron.

## Architecture

```
cron job (no_agent=true, deliver="qqbot")
  └─ wrapper script: $HERMES_HOME/scripts/email-daily-digest.sh
      └─ real script: scripts/email-daily-digest.sh  (this repo)
          ├─ resolves Python portably (EMAIL_DAILY_PY override → probe candidates)
          ├─ sources $HERMES_HOME/.env  (IMAP_USER / IMAP_PASS / LLM_API_KEY)
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
LLM_API_KEY=...                        # wins over the legacy OPENAI_API_KEY
LLM_BASE_URL=http://127.0.0.1:3425/v1  # magpie router on the Hermes host
LLM_MODEL=group/deepseek-v4-1-flash   # fastest; reasoning models work too
LLM_MAX_TOKENS=8192                    # optional; generous budget for reasoning
```

`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` are read from the environment and
override the code defaults, so switching upstream is a config edit. The default
in `core.py` is the Sub2API gateway (`https://sub.shengchuiye.com/v1`), but as of
2026-10-22 that key's group answers `Model "X" is not supported by any configured
account in this group` for every id, so the digest points at the local magpie
router instead. A model id existing in `/v1/models` does **not** prove the key may
use it — verify with a real completion before trusting a name.

The output budget is deliberately generous (`max_tokens` 8192, override with
`LLM_MAX_TOKENS`) and the read timeout is 180 s, because modern models think
before answering and spend most of the budget on `reasoning_content`. With the
old 800-token cap a reasoning model answered `content: null`; at 8192 the same
model (`group/minimax-m3.1-flash`) returns a proper digest in ~38 s, while a
non-reasoning model (`group/deepseek-v4-1-flash`) does it in ~8 s. An empty
`content` is treated as a failed summary, never as a crash.

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
| `LLM call failed: HTTP 403` | Token rejected by the gateway | Refresh `LLM_API_KEY` |
| `HTTP 404 … not supported by any configured account in this group` | The key's gateway group has no account for that model (a `/v1/models` listing proves nothing) | Point `LLM_BASE_URL`/`LLM_MODEL` at a gateway that actually answers, e.g. the local magpie router |
| Digest arrives as a bare title list, no summary | LLM call failed and the raw-list fallback took over (exit 0, so it looks healthy) | Probe `POST $LLM_BASE_URL/chat/completions` with 4 max_tokens before trusting the run |
| `delivery error: QQBot send failed` | gateway / channel disconnected | `hermes gateway status` + QQ bot reconnect |
