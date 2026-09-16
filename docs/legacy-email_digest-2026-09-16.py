"""IMAP email fetch + LLM digest for the daily QQ digest cron job.

Self-contained, no third-party deps (Python stdlib only). Designed to run from
~/.hermes/scripts/email-daily-digest.sh on a Mac; environment is sourced from
~/.hermes/.env so secrets stay there.

Public entry point:
    build_digest(since_hours=24, max_messages=20, llm_model="MiniMax-M3",
                 base_url="https://sub.shengchuiye.com/v1")
        -> (text_to_send: str, message_count: int)

LLM is intentionally optional: when the API is unreachable or the call fails,
the digest falls back to a flat title/sender list so the cron job still has
something to push to QQ — silent failure is worse than a plain list.
"""
from __future__ import annotations

import email
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

CST = timezone(timedelta(hours=8), "Asia/Shanghai")
IMAP_HOST_DEFAULT = "imap.qq.com"
IMAP_PORT_DEFAULT = 993
LLM_BASE_URL_DEFAULT = "https://sub.shengchuiye.com/v1"
LLM_MODEL_DEFAULT = "MiniMax-M3"
LLM_TIMEOUT_SECONDS = 30


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


def _extract_body(msg: email.message.EmailMessage) -> str:
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
               host: str, port: int, user: str, password: str) -> list[MailItem]:
    """Connect, search UNSEEN since the cutoff, return parsed list (newest last)."""
    cutoff = datetime.now(CST) - timedelta(hours=since_hours)
    cutoff_str = cutoff.strftime("%d-%b-%Y")
    out: list[MailItem] = []

    with imaplib.IMAP4_SSL(host, port, timeout=30) as imap:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)
        # IMAP SINCE matches arrival date at day granularity; combined with
        # UNSEEN for inbox-only digest. If we want all-recent including read,
        # swap UNSEEN for ALL — kept UNSEEN here because re-reading past 24h
        # digest would duplicate everything the user already saw.
        typ, data = imap.search(None, f'(UNSEEN SINCE {cutoff_str})')
        if typ != "OK" or not data or not data[0]:
            return out
        ids = data[0].split()
        # newest first by default
        ids = ids[-max_messages:]

        for num in ids:
            typ, msg_data = imap.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            sender = _decode(msg.get("From"))
            subject = _decode(msg.get("Subject")) or "(无主题)"
            date_hdr = msg.get("Date")
            try:
                dt = parsedate_to_datetime(date_hdr).astimezone(CST)
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
        "max_tokens": 800,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"[email-digest] LLM call failed: {exc}", file=sys.stderr)
        return None
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        print(f"[email-digest] LLM response malformed: {data!r}", file=sys.stderr)
        return None


def _fallback_summary(items: list[MailItem], today_label: str) -> str:
    """Flat title/sender list — used when LLM call fails so cron still ships something."""
    lines = [f"📬 {today_label} 收件箱日报（共 {len(items)} 封，LLM 暂不可用，附原始列表）"]
    for i, m in enumerate(items, 1):
        lines.append(f"{i}. {m.received_cst} · {m.sender} · {m.subject}")
    return "\n".join(lines)


def build_digest(since_hours: int = 24,
                 max_messages: int = 20,
                 llm_model: str = LLM_MODEL_DEFAULT,
                 base_url: str = LLM_BASE_URL_DEFAULT) -> tuple[str, int]:
    """Pull + summarize + return (text_to_send, message_count). Always returns text."""
    host = os.environ.get("IMAP_HOST", IMAP_HOST_DEFAULT)
    port = int(os.environ.get("IMAP_PORT", str(IMAP_PORT_DEFAULT)))
    user = os.environ.get("IMAP_USER", "").strip()
    password = os.environ.get("IMAP_PASS", "")
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY", "")

    today_label = datetime.now(CST).strftime("%Y-%m-%d")

    if not user or not password:
        msg = f"⚠️ Email digest: IMAP_USER/IMAP_PASS 未配置 (today={today_label})"
        print(msg, file=sys.stderr)
        return msg, 0

    try:
        items = fetch_mail(since_hours, max_messages, host, port, user, password)
    except (imaplib.IMAP4.error, OSError) as exc:
        msg = f"⚠️ Email digest: IMAP 抓取失败 — {exc}"
        print(msg, file=sys.stderr)
        return msg, 0

    if not items:
        return f"📭 {today_label} 收件箱无新邮件（过去 {since_hours}h）", 0

    if not api_key:
        return _fallback_summary(items, today_label), len(items)

    summary = _llm_summarize(items, api_key, base_url, llm_model, today_label)
    if not summary:
        return _fallback_summary(items, today_label), len(items)

    header = f"📬 {today_label} 收件箱日报（共 {len(items)} 封）"
    return f"{header}\n\n{summary}", len(items)


def main() -> int:
    text, count = build_digest()
    # cron delivery sends stdout as the QQ message body; keep last line short
    sys.stdout.write(text)
    sys.stdout.write("\n")
    sys.stdout.flush()
    if count == 0 and "无新邮件" not in text and "⚠️" in text:
        return 2  # surface config / network failures to cron failure_deliver
    return 0


if __name__ == "__main__":
    sys.exit(main())
