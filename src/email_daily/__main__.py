from __future__ import annotations

import argparse
import os
import sys

from .core import build_digest, IMAP_HOST_DEFAULT, LLM_BASE_URL_DEFAULT, LLM_MODEL_DEFAULT


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="email-daily", description="Daily email digest")
    p.add_argument("--since-hours", type=int, default=24, help="Look back window (default 24)")
    p.add_argument("--max-messages", type=int, default=20, help="Max emails to fetch")
    p.add_argument("--unseen-only", action="store_true",
                   help="[legacy] restrict to UNSEEN (default is ALL in last window)")
    p.add_argument("--imap-host", default=IMAP_HOST_DEFAULT)
    # LLM_MODEL in $HERMES_HOME/.env wins, so a gateway-side rename is a config
    # edit instead of a code change. --model still overrides everything.
    p.add_argument("--model", default=os.environ.get("LLM_MODEL") or LLM_MODEL_DEFAULT)
    p.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL") or LLM_BASE_URL_DEFAULT)
    p.add_argument("--dry-run", action="store_true", help="Skip actual send; print to stdout only")
    args = p.parse_args(argv)

    text, count = build_digest(
        since_hours=args.since_hours,
        max_messages=args.max_messages,
        unseen_only=args.unseen_only,
        llm_model=args.model,
        base_url=args.base_url,
    )
    if args.dry_run:
        print(f"[dry-run] would push: count={count}")
        print("---")
    print(text)
    if count == 0 and "无新邮件" not in text and "⚠️" in text:
        return 2  # config / network failure → cron failure_deliver
    return 0


if __name__ == "__main__":
    sys.exit(main())
