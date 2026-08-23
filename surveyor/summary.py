"""analyze-all: run analyze on every <repo>.db under a directory and emit a
cross-repo summary (summary.md + summary.csv).

The headline row per repo is the leakage-free predictive test by default: predictors
measured before a per-repo split date, bug outcomes after. Pass concurrent=True for
the association-only view.
"""
from __future__ import annotations

import csv
import glob
import json
import math
import os
import sqlite3
from datetime import datetime, timezone

from .analyze import analyze


def _date_to_ts(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _percentile_ts(db_path: str, frac: float) -> int | None:
    c = sqlite3.connect(db_path)
    ts = [r[0] for r in c.execute("SELECT ts FROM commits WHERE ts>0 ORDER BY ts")]
    c.close()
    if len(ts) < 20:
        return None
    return ts[min(len(ts) - 1, int(len(ts) * frac))]


def _concentration(db_path: str):
    c = sqlite3.connect(db_path)
    try:
        maxcc = c.execute("SELECT MAX(cc) FROM unit_changes").fetchone()[0] or 0
        maxwmc = c.execute("SELECT MAX(wmc_other) FROM unit_changes").fetchone()[0] or 0
        ncom = c.execute("SELECT COUNT(*) FROM commits").fetchone()[0] or 0
    finally:
        c.close()
    return maxcc, maxwmc, ncom


def _f(v, p=3) -> str:
    try:
        return "n/a" if v is None or v != v else f"{float(v):.{p}f}"
    except (TypeError, ValueError):
        return "n/a"


def analyze_all(scan_dir: str, *, split_frac: float = 0.75, split_at: str | None = None,
                concurrent: bool = False, exclude_tests: bool = True, log=print) -> list[dict]:
    dbs = sorted(glob.glob(os.path.join(scan_dir, "*.db")))
    if not dbs:
        log(f"no *.db files in {scan_dir}")
        return []
    mode = "concurrent" if concurrent else (f"split@{split_at}" if split_at
                                            else f"split@{split_frac:.0%}")
    rows = []
    for i, db in enumerate(dbs, 1):
        name = os.path.basename(db)[:-3]
        split_ts = None
        if not concurrent:
            split_ts = _date_to_ts(split_at) if split_at else _percentile_ts(db, split_frac)
        out = os.path.join(scan_dir, f"{name}-report")
        try:
            r = analyze(db, out, split_ts=split_ts, exclude_tests=exclude_tests,
                        log=lambda *a: None)
        except Exception as e:  # one bad db shouldn't sink the batch
            log(f"[{i}/{len(dbs)}] {name}: FAILED ({e})")
            continue
        maxcc, maxwmc, ncom = _concentration(db)
        r.update(name=name, maxcc=maxcc, maxwmc=maxwmc, ncommits=ncom,
                 split_ts=split_ts)
        rows.append(r)
        log(f"[{i}/{len(dbs)}] {name}: part_mut={_f(r['partial_impact'])} "
            f"(n={r['n_files']}, prev={_f(r['prevalence'],2)})")

    rows.sort(key=lambda r: (r["partial_impact"] if r["partial_impact"] == r["partial_impact"]
                             else -9), reverse=True)
    _write(scan_dir, rows, mode)
    return rows


def summary_only(scan_dir: str, log=print) -> list[dict]:
    """Build the cross-repo summary from existing <repo>-report/stats.json files
    (written by `analyze`), without re-running analyze. Used by analyze-parallel.sh."""
    rows = []
    for sj in sorted(glob.glob(os.path.join(scan_dir, "*-report", "stats.json"))):
        with open(sj) as fh:
            d = json.load(fh)
        name = d.get("name") or os.path.basename(os.path.dirname(sj))[:-7]
        db = os.path.join(scan_dir, f"{name}.db")
        maxcc, maxwmc, ncom = _concentration(db) if os.path.exists(db) else (0, 0, 0)
        d.update(name=name, maxcc=maxcc, maxwmc=maxwmc, ncommits=ncom)
        rows.append(d)
    if not rows:
        log(f"no <repo>-report/stats.json found under {scan_dir} — run analyze first")
        return []
    mode = "split" if any(r.get("split_ts") for r in rows) else "concurrent"
    rows.sort(key=lambda r: (r["partial_impact"] if r["partial_impact"] == r["partial_impact"]
                             else -9), reverse=True)
    _write(scan_dir, rows, mode)
    log(f"summary over {len(rows)} repos → {scan_dir}/summary.md")
    return rows


def _write(scan_dir: str, rows: list[dict], mode: str) -> None:
    def szz(r, k):
        s = r.get("szz")
        return (s or {}).get(k)

    # ---- summary.csv ----
    with open(os.path.join(scan_dir, "summary.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["repo", "commits", "files", "prevalence", "partial_mut", "partial_comp",
                    "sp_mut", "sp_churn", "auc_mut", "auc_churn",
                    "szz_auc_mut", "szz_auc_comp", "szz_auc_churn", "n_inducing",
                    "max_cc", "max_wmc"])
        for r in rows:
            w.writerow([r["name"], r["ncommits"], r["n_files"], _f(r["prevalence"], 3),
                        _f(r["partial_impact"]), _f(r["partial_comp"]),
                        _f(r["corr"]["impact"]), _f(r["corr"]["churn"]),
                        _f(r["auc"]["impact"]), _f(r["auc"]["churn"]),
                        _f(szz(r, "auc_mut")), _f(szz(r, "auc_comp")), _f(szz(r, "auc_churn")),
                        szz(r, "n_inducing") or 0, r["maxcc"], r["maxwmc"]])

    # ---- summary.md ----
    pm = [r["partial_impact"] for r in rows if r["partial_impact"] == r["partial_impact"]]
    pos = sum(1 for v in pm if v > 0)
    med = sorted(pm)[len(pm) // 2] if pm else float("nan")
    L = []
    L.append("# Surveyor cross-repo summary\n")
    L.append(f"- repos: **{len(rows)}** &middot; outcome mode: **{mode}**")
    if pm:
        L.append(f"- `partial(impact_mutation | churn)` **> 0 in {pos}/{len(pm)} repos** "
                 f"&middot; median **{_f(med)}** &middot; range {_f(min(pm))}…{_f(max(pm))}")
    L.append("\n**Headline:** does change-impact (mutation) predict bug-fix locations "
             "beyond churn? `partial_mut` is the honest number; `sp_*` are raw Spearman, "
             "`auc_*` rank buggy files. SZZ columns ask the per-commit question: do "
             "high-impact commits *induce* later fixes?\n")
    L.append("| repo | commits | files | prev | partial_mut | partial_comp | sp_mut | sp_churn "
             "| auc_mut | auc_churn | szz_mut | szz_comp | szz_churn | max_cc | max_wmc |")
    L.append("|" + "---|" * 15)
    for r in rows:
        L.append("| {name} | {nc} | {nf} | {pv} | **{pm}** | {pc} | {sm} | {sch} | {am} | {ach} "
                 "| {zm} | {zc} | {zch} | {cc} | {wmc} |".format(
                     name=r["name"], nc=r["ncommits"], nf=r["n_files"],
                     pv=_f(r["prevalence"], 2), pm=_f(r["partial_impact"]),
                     pc=_f(r["partial_comp"]), sm=_f(r["corr"]["impact"]),
                     sch=_f(r["corr"]["churn"]), am=_f(r["auc"]["impact"]),
                     ach=_f(r["auc"]["churn"]), zm=_f(szz(r, "auc_mut")),
                     zc=_f(szz(r, "auc_comp")), zch=_f(szz(r, "auc_churn")),
                     cc=r["maxcc"], wmc=r["maxwmc"]))
    L.append("\nPer-repo reports are in `<repo>-report/` (report.md, files.csv, coupling.csv, "
             "commits.html). This summary: summary.md + summary.csv.\n")
    with open(os.path.join(scan_dir, "summary.md"), "w") as fh:
        fh.write("\n".join(L))
