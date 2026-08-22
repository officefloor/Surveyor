"""Surveyor CLI.

  surveyor scan    <repo> --db repo.db [--since ...] [--max-commits N]
  surveyor analyze <db>   --out report/ [--split-at YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from .analyze import analyze
from .config import Config
from .scan import scan


def _to_ts(date_str: str | None) -> int | None:
    if not date_str:
        return None
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="surveyor")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="walk a repo's history into a SQLite db")
    s.add_argument("repo")
    s.add_argument("--db", required=True)
    s.add_argument("--config", help="optional YAML config (ignore globs, keywords, ...)")
    s.add_argument("--since", help="git --since date filter, e.g. 2019-01-01")
    s.add_argument("--until", default="HEAD")
    s.add_argument("--max-commits", type=int)
    s.add_argument("--no-units", action="store_true",
                   help="skip per-function rows (faster; loses function-level detail)")

    a = sub.add_parser("analyze", help="validate change-impact vs the mined bug ground-truth")
    a.add_argument("db")
    a.add_argument("--out", required=True)
    a.add_argument("--split-at", help="YYYY-MM-DD; measure predictors before, bugs after "
                   "(leakage-free predictive test). Omit for a concurrent association check.")
    a.add_argument("--include-tests", action="store_true",
                   help="keep test files in the validation universe (default: excluded)")

    args = ap.parse_args(argv)

    if args.cmd == "scan":
        cfg = Config.load(args.config)
        scan(args.repo, args.db, cfg, since=args.since, until=args.until,
             max_count=args.max_commits, want_units=not args.no_units)
        print(f"Next: python -m surveyor analyze {args.db} --out report/")
        return 0

    if args.cmd == "analyze":
        analyze(args.db, args.out, split_ts=_to_ts(args.split_at),
                exclude_tests=not args.include_tests)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
