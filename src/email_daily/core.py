"""IMAP fetch + LLM digest for the daily QQ digest cron job.

Self-contained, no third-party deps (Python stdlib only). Invoked by
`scripts/email-daily-digest.sh` in this repo (via the thin cron wrapper in
`$HERMES_HOME/scripts/`); environment is sourced from `$HERMES_HOME/.env` so
secrets stay there. Paths are resolved at runtime — nothing is hardcoded to a
single machine, so the repo runs unchanged on macOS, Linux and Windows.

Public entry point:
    build_digest(since_hours=24, max_messages=20, unseen_only=False,
                 llm_model="MiniMax-M3.1-Flash",
                 base_url="https://sub.shengchuiye.com/v1")
        -> (text_to_send: str, message_count: int)

LLM is intentionally optional: when the API is unreachable or the call fails,
the digest falls back to a flat title/sender list so the cron job still has
something to push to QQ — silent failure is worse than a plain list.

Default scope is `ALL SINCE <cutoff>` (read + unread) per user preference; pass
`unseen_only=True` to restrict to UNSEEN.
"""
from __future__ import annotations

import email
import email.message
import email.policy
import imaplib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from typing import Any  # noqa: F401  — used in type annotations below

CST = timezone(timedelta(hours=8), "Asia/Shanghai")
IMAP_HOST_DEFAULT = "imap.qq.com"
IMAP_PORT_DEFAULT = 993
LLM_BASE_URL_DEFAULT = "https://sub.shengchuiye.com/v1"
# Only a name the gateway actually serves will answer; the retired
# "MiniMax-M3" returned 404 and silently downgraded every digest to the
# raw-list fallback. LLM_MODEL in the environment overrides this default.
LLM_MODEL_DEFAULT = "MiniMax-M3.1-Flash"
# Reasoning models think before answering, so the read timeout has to cover
# deliberation as well as the (short) digest itself.
LLM_TIMEOUT_SECONDS = 180
# Generous output budget: modern reasoning models spend most of it on
# reasoning_content before emitting the digest. Override with LLM_MAX_TOKENS.
LLM_MAX_TOKENS_DEFAULT = 8192


def _max_tokens() -> int:
    try:
        return int(os.environ.get("LLM_MAX_TOKENS") or LLM_MAX_TOKENS_DEFAULT)
    except ValueError:
        return LLM_MAX_TOKENS_DEFAULT


@dataclass
class MailItem:
    msg_id: str
    sender: str
    subject: str
    received_cst: str
    body_excerpt: str  # first ~600 chars of plain-text body


def _decode(s: str | bytes | None) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        try:
            return s.decode("utf-8", errors="replace")
        except Exception:
            return s.decode("latin-1", errors="replace")
    return str(make_header(decode_header(s)))


def _extract_body(msg: email.message.Message) -> str:
    """Prefer text/plain; fall back to a stripped text/html; cap to 600 chars."""
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                plain_chunks.append(text)
            elif ctype == "text/html":
                html_chunks.append(text)
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = payload.decode("utf-8", errors="replace")
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            plain_chunks.append(text)
        elif ctype == "text/html":
            html_chunks.append(text)

    body = "\n".join(plain_chunks).strip()
    if not body and html_chunks:
        body = re.sub(r"<[^>]+>", " ", "\n".join(html_chunks))
        body = re.sub(r"\s+", " ", body).strip()
    return body[:600]


def fetch_mail(since_hours: int, max_messages: int,
               host: str, port: int, user: str, password: str,
               unseen_only: bool = False) -> list[MailItem]:
    """Connect, search ALL (or UNSEEN) since the cutoff, return parsed list.

    Default scope is `ALL SINCE <cutoff>` — covers read + unread so digest
    summarises everything received in the window. unread_only=True switches
    to UNSEEN for the legacy narrow behaviour.

    Important: server-side `SINCE` is unreliable on some IMAP servers (e.g.
    QQ Mail ignores it entirely), so this function applies the time filter
    CLIENT-SIDE using INTERNALDATE (the server-stamped arrival time in UTC).
    We still pass SINCE to the server as a hint to limit transfer volume.
    """
    cutoff_cst = datetime.now(CST) - timedelta(hours=since_hours)
    cutoff_utc = cutoff_cst.astimezone(timezone.utc)
    # Pass SINCE one day earlier than the actual cutoff — the server uses
    # day-granularity for SINCE, so anything within ±24h of the cutoff
    # could be missed if we passed exactly the day. The client-side filter
    # is the authoritative one.
    since_hint = (cutoff_utc - timedelta(days=1)).strftime("%d-%b-%Y")
    cutoff_str = cutoff_cst.strftime("%d-%b-%Y")
    out: list[MailItem] = []

    with imaplib.IMAP4_SSL(host, port, timeout=30) as imap:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)
        flag = "UNSEEN" if unseen_only else "ALL"
        typ, data = imap.search(None, f'({flag} SINCE {since_hint})')
        if typ != "OK" or not data or not data[0]:
            return out
        ids = data[0].split()
        # Walk newest-first; stop once we've seen enough older messages.
        # Cap the candidate pool so a 2000-message inbox doesn't burn time.
        # Worst case: 24h of mail ≤ 200 on a busy day; 2000 is generous headroom.
        candidate_pool = 2000
        ids = ids[-candidate_pool:]

        # Bulk-fetch INTERNALDATE for all candidates in one round-trip
        # (cheaper than per-message FETCH). Then apply the cutoff filter
        # in memory. INTERNALDATE is monotonically increasing with mailbox
        # order, so walking newest-first lets us stop at the cutoff.
        id_range = b",".join(ids).decode() if isinstance(ids[0], bytes) else ",".join(ids)
        typ, internal_all = imap.fetch(id_range, "(INTERNALDATE)")
        if typ != "OK":
            return out
        # Response shape: alternating ([b'1 (INTERNALDATE "...")'], b')').
        # We iterate and parse per-message, then drop ones older than cutoff.
        from email.utils import parsedate_to_datetime as _ptd
        # Parse `1 (INTERNALDATE "...") 2 (INTERNALDATE "...") ...` style.
        # imaplib returns each item; for bulk fetch, the entire string is one item.
        raw_blob = b""
        for item in internal_all:
            if isinstance(item, bytes):
                raw_blob += item + b"\n"
            elif isinstance(item, tuple) and len(item) >= 1 and isinstance(item[0], bytes):
                raw_blob += item[0] + b"\n"
        # Parse pairs of (msg_id, internaldate_str)
        pairs = re.findall(rb'(\d+)\s+\(INTERNALDATE\s+"([^"]+)"', raw_blob)
        # Parse to UTC datetimes; drop unparseable rows.
        # NOTE: imap.search returns ids in mailbox-order (oldest first), NOT
        # newest-first, so we cannot early-break on the first old row.
        # Sort by arrival time descending and then take the cutoff window.
        from email.utils import parsedate_to_datetime as _ptd
        annotated: list[tuple[datetime, bytes]] = []
        for mid_str, idate_str in pairs:
            try:
                arrival_utc = _ptd(idate_str.decode()).astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
            mid_b = mid_str.encode() if isinstance(mid_str, str) else mid_str
            annotated.append((arrival_utc, mid_b))
        annotated.sort(key=lambda x: x[0], reverse=True)  # newest first
        filtered_ids: list[bytes] = [mid for arrival, mid in annotated
                                     if arrival >= cutoff_utc][:max_messages]

        # Now fetch the full RFC822 only for messages that survived the filter.
        for num in filtered_ids:
            typ, msg_data = imap.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if isinstance(raw, int):  # bytes expected; some servers return tuple index
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            sender = _decode(msg.get("From"))
            subject = _decode(msg.get("Subject")) or "(无主题)"
            date_hdr = msg.get("Date")
            try:
                dt = parsedate_to_datetime(str(date_hdr)).astimezone(CST)
            except (TypeError, ValueError):
                dt = datetime.now(CST)
            received = dt.strftime("%m-%d %H:%M")
            body = _extract_body(msg)
            out.append(MailItem(
                msg_id=num.decode(),
                sender=sender,
                subject=subject,
                received_cst=received,
                body_excerpt=body,
            ))
    return out


def _format_for_llm(items: list[MailItem]) -> str:
    rows = []
    for i, m in enumerate(items, 1):
        rows.append(
            f"[{i}] 时间: {m.received_cst}\n"
            f"    发件人: {m.sender}\n"
            f"    主题: {m.subject}\n"
            f"    正文摘要: {m.body_excerpt or '(空)'}"
        )
    return "\n\n".join(rows)


def _llm_summarize(items: list[MailItem], api_key: str, base_url: str,
                   model: str, today_label: str) -> str | None:
    """Return LLM digest string, or None on any failure (caller falls back)."""
    sys_prompt = (
        "你是一个收件箱日报助手。把邮件列表压缩成 3-8 条要点，每条一行，"
        "格式「时间 · 发件人 · 主题: 1-2 句结论」。要求：客观、可扫读，"
        "绝不编造邮件内容，遇到订阅/促销类合并成一条「N封订阅/促销邮件」。"
    )
    user_prompt = (
        f"今天是 {today_label}，以下是过去 24 小时收件箱的 {len(items)} 封邮件，"
        "请按上面的格式给出日报：\n\n" + _format_for_llm(items)
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        # Generous on purpose: modern reasoning models spend most of the budget
        # on reasoning_content before they emit the digest, and a tight cap
        # returns content=None (verified with minimax-m3.1-flash at 800).
        "max_tokens": _max_tokens(),
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Cloudflare UA filter (error code 1010) blocks default Python UA.
            # curl UA passes through — sub2api gateway behind CF allows it.
            "User-Agent": "curl/8.4.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"[email-daily] LLM call failed: {exc}", file=sys.stderr)
        return None
    try:
        message = data["choices"][0]["message"]
        # A reasoning model can spend the whole max_tokens budget on
        # reasoning_content and hand back content=None; that is a failed
        # summary, not a crash — the caller falls back to the raw list.
        content = (message.get("content") or "").strip()
        # Strip reasoning traces emitted by reasoning-capable models (M-series).
        # They wrap thoughts in <think>...</think> tags that pollute the digest.
        content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()
        if not content:
            print(f"[email-daily] LLM returned empty content "
                  f"(finish_reason={data['choices'][0].get('finish_reason')!r}, "
                  f"reasoning={'yes' if message.get('reasoning_content') else 'no'})",
                  file=sys.stderr)
            return None
        return content
    except (KeyError, IndexError, TypeError):
        print(f"[email-daily] LLM response malformed: {data!r}", file=sys.stderr)
        return None


def _fallback_summary(items: list[MailItem], today_label: str, since_hours: int) -> str:
    """Flat title/sender list — used when LLM call fails so cron still ships something."""
    lines = [f"📬 {today_label} 收件箱日报（共 {len(items)} 封，LLM 暂不可用，附原始列表，过去 {since_hours}h）"]
    for i, m in enumerate(items, 1):
        lines.append(f"{i}. {m.received_cst} · {m.sender} · {m.subject}")
    return "\n".join(lines)


def build_digest(since_hours: int = 24,
                 max_messages: int = 20,
                 unseen_only: bool = False,
                 llm_model: str = LLM_MODEL_DEFAULT,
                 base_url: str = LLM_BASE_URL_DEFAULT) -> tuple[str, int]:
    """Pull + summarize + return (text_to_send, message_count). Always returns text."""
    host = os.environ.get("IMAP_HOST", IMAP_HOST_DEFAULT)
    port = int(os.environ.get("IMAP_PORT", str(IMAP_PORT_DEFAULT)))
    user = os.environ.get("IMAP_USER", "").strip()
    password = os.environ.get("IMAP_PASS", "")
    # LLM_API_KEY is this pipeline's own knob and wins; OPENAI_API_KEY stays as
    # the legacy fallback so older hosts keep working unchanged.
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

    today_label = datetime.now(CST).strftime("%Y-%m-%d")

    if not user or not password:
        msg = f"⚠️ Email digest: IMAP_USER/IMAP_PASS 未配置 (today={today_label})"
        print(msg, file=sys.stderr)
        return msg, 0

    try:
        items = fetch_mail(since_hours, max_messages, host, port, user, password,
                           unseen_only=unseen_only)
    except (imaplib.IMAP4.error, OSError) as exc:
        msg = f"⚠️ Email digest: IMAP 抓取失败 — {exc}"
        print(msg, file=sys.stderr)
        return msg, 0

    if not items:
        return f"📭 {today_label} 收件箱无新邮件（过去 {since_hours}h）", 0

    if not api_key:
        return _fallback_summary(items, today_label, since_hours), len(items)

    summary = _llm_summarize(items, api_key, base_url, llm_model, today_label)
    if not summary:
        return _fallback_summary(items, today_label, since_hours), len(items)

    header = f"📬 {today_label} 收件箱日报（共 {len(items)} 封）"
    return f"{header}\n\n{summary}", len(items)
