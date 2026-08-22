"""Surveyor CLI.

  surveyor scan    <repo> [--db path] [--since ...] [--max-commits N]
  surveyor analyze <repo> [--db path] [--out dir] [--split-at YYYY-MM-DD]

By convention, beside the checkout: the db is <repo>.db and the report is
<repo>-report/ (e.g. scanning ~/scan/spring-petclinic writes
~/scan/spring-petclinic.db, and analyze writes ~/scan/spring-petclinic-report/).
So you only ever pass the repo path; --db / --out override either default.
"""
from __future__ import annotations

import argparse
import os
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


def _base(target: str) -> str:
    """Location-convention base path: the abs repo path, or a .db stripped of .db."""
    base = os.path.abspath(target).rstrip(os.sep)
    return base[:-3] if base.endswith(".db") else base


def _default_db(repo_path: str) -> str:
    """<repo>.db beside the checkout: ~/scan/spring-petclinic -> ~/scan/spring-petclinic.db"""
    return _base(repo_path) + ".db"


def _default_out(target: str) -> str:
    """<repo>-report/ beside the checkout: ~/scan/spring-petclinic -> ~/scan/spring-petclinic-report"""
    return _base(target) + "-report"


def _resolve_db(target: str, override: str | None) -> str:
    """Resolve the db to use. --db wins; else if `target` is itself a .db file use
    it directly; otherwise derive <target>.db by the location convention."""
    if override:
        return override
    if target.endswith(".db") or (os.path.isfile(target) and not os.path.isdir(target)):
        return target
    return _default_db(target)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="surveyor")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="walk a repo's history into a SQLite db")
    s.add_argument("repo")
    s.add_argument("--db", help="SQLite output path (default: <repo>.db beside the checkout)")
    s.add_argument("--config", help="optional YAML config (ignore globs, keywords, ...)")
    s.add_argument("--ignore", action="append", metavar="GLOB", default=[],
                   help="extra ignore glob, repeatable, appended to the defaults "
                        "(and any --config ignores). E.g. --ignore '**/vendors/**' "
                        "--ignore 'src/main/webapp/**'")
    s.add_argument("--since", help="git --since date filter, e.g. 2019-01-01")
    s.add_argument("--until", default="HEAD")
    s.add_argument("--max-commits", type=int)
    s.add_argument("--no-units", action="store_true",
                   help="skip per-function rows (faster; loses function-level detail)")

    a = sub.add_parser("analyze", help="validate change-impact vs the mined bug ground-truth")
    a.add_argument("target", help="the scanned repo/checkout (its <repo>.db is read by "
                   "convention), or a .db file directly")
    a.add_argument("--db", help="db path override (default: <repo>.db beside the checkout)")
    a.add_argument("--out", help="report output dir (default: <repo>-report/ beside the checkout)")
    a.add_argument("--split-at", help="YYYY-MM-DD; measure predictors before, bugs after "
                   "(leakage-free predictive test). Omit for a concurrent association check.")
    a.add_argument("--include-tests", action="store_true",
                   help="keep test files in the validation universe (default: excluded)")

    args = ap.parse_args(argv)

    if args.cmd == "scan":
        cfg = Config.load(args.config)
        cfg.ignore += args.ignore   # CLI globs append on top of defaults + config
        db = args.db or _default_db(args.repo)
        scan(args.repo, db, cfg, since=args.since, until=args.until,
             max_count=args.max_commits, want_units=not args.no_units)
        print(f"db: {db}")
        print(f"Next: surveyor analyze {args.repo}")
        return 0

    if args.cmd == "analyze":
        db = _resolve_db(args.target, args.db)
        if not os.path.exists(db):
            print(f"error: db not found: {db}\n"
                  f"       run `surveyor scan {args.target}` first, or pass --db.",
                  file=sys.stderr)
            return 2
        out = args.out or _default_out(args.target)
        print(f"db:  {db}")
        print(f"out: {out}")
        analyze(db, out, split_ts=_to_ts(args.split_at),
                exclude_tests=not args.include_tests)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
