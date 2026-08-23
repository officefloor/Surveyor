"""Validation layer: does change-impact predict where the pain lands?

Aggregates the raw scan tables to per-file features, then tests change-impact
against a repo-mined bug ground-truth AND against churn/frequency baselines, so a
result means "impact beats/augments size & churn", not "big files have bugs".

Writes report.md + files.csv + coupling.csv + commits.html to an output directory.
Pure Python.
"""
from __future__ import annotations

import csv
import os
import sqlite3
from dataclasses import dataclass, field

from . import plot, stats


@dataclass
class FileRow:
    file_id: int
    path: str
    lang: str
    is_test: int
    impact_mut: float = 0.0    # mutation of existing code — the defect predictor
    impact_comp: float = 0.0   # composite (incl. one-time new code) — architectural cost
    churn_before: float = 0.0
    commits_before: set = field(default_factory=set)
    authors: set = field(default_factory=set)
    max_cc: int = 0
    fix_after: int = 0     # bug-fix commits touching the file in the outcome window
    fix_all: int = 0

    @property
    def n_commits(self) -> float:
        return float(len(self.commits_before))


def _load(db: sqlite3.Connection, split_ts: int | None, exclude_tests: bool):
    files: dict[int, FileRow] = {}
    paths = dict(db.execute("SELECT id, canonical_path FROM file_ids").fetchall())
    # commit author lookup for ownership
    authors = dict(db.execute("SELECT sha, email FROM commits").fetchall())

    q = ("SELECT sha, file_id, lang, add_lines, del_lines, mutation_cost, "
         "godclass_cost, is_test, ts, is_fix FROM file_changes")
    for sha, fid, lang, add, dele, mut, god, is_test, ts, is_fix in db.execute(q):
        fr = files.get(fid)
        if fr is None:
            fr = files[fid] = FileRow(fid, paths.get(fid, "?"), lang or "", is_test)
        fr.is_test = fr.is_test or is_test
        if lang:
            fr.lang = lang
        before = split_ts is None or ts <= split_ts
        if before:
            fr.impact_mut += (mut or 0)
            fr.impact_comp += (mut or 0) + (god or 0)
            fr.churn_before += (add or 0) + (dele or 0)
            fr.commits_before.add(sha)
            if authors.get(sha):
                fr.authors.add(authors[sha])
        if is_fix:
            fr.fix_all += 1
            if split_ts is not None and ts > split_ts:
                fr.fix_after += 1
    # max cc per file from units
    for fid, mcc in db.execute("SELECT file_id, MAX(cc) FROM unit_changes GROUP BY file_id"):
        if fid in files:
            files[fid].max_cc = mcc or 0

    rows = list(files.values())
    # validation universe: real source files, optionally excluding tests
    universe = [r for r in rows if r.lang and (not exclude_tests or not r.is_test)]
    return rows, universe


def _coupling(db: sqlite3.Connection, min_support: int, top: int):
    """Top temporally-coupled file pairs (co-change support/confidence)."""
    by_sha: dict[str, set] = {}
    counts: dict[int, int] = {}
    for sha, fid in db.execute("SELECT sha, file_id FROM file_changes"):
        by_sha.setdefault(sha, set()).add(fid)
        counts[fid] = counts.get(fid, 0) + 1
    pair: dict[tuple, int] = {}
    for fids in by_sha.values():
        if len(fids) > 30:   # skip sweeping commits that couple everything
            continue
        fl = sorted(fids)
        for i in range(len(fl)):
            for j in range(i + 1, len(fl)):
                pair[(fl[i], fl[j])] = pair.get((fl[i], fl[j]), 0) + 1
    paths = dict(db.execute("SELECT id, canonical_path FROM file_ids").fetchall())
    out = []
    for (a, b), s in pair.items():
        if s < min_support:
            continue
        conf = s / min(counts[a], counts[b])
        out.append((s, conf, paths.get(a, "?"), paths.get(b, "?")))
    out.sort(reverse=True)
    return out[:top]


def _fmt(v: float) -> str:
    return "n/a" if v != v else f"{v:.3f}"   # v!=v => NaN


def analyze(db_path: str, out_dir: str, *, split_ts: int | None = None,
            exclude_tests: bool = True, log=print) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    db = sqlite3.connect(db_path)
    rows, uni = _load(db, split_ts, exclude_tests)

    # outcome: post-split fixes if a split was given, else all fixes (concurrent)
    def outcome(r: FileRow) -> int:
        return r.fix_after if split_ts is not None else r.fix_all

    impact = [r.impact_mut for r in uni]        # headline predictor (defect-relevant)
    impact_comp = [r.impact_comp for r in uni]  # composite, shown for contrast
    churn = [r.churn_before for r in uni]
    ncomm = [r.n_commits for r in uni]
    hotspot = [r.max_cc * len(r.commits_before) for r in uni]
    fixes = [float(outcome(r)) for r in uni]
    labels = [1 if outcome(r) > 0 else 0 for r in uni]
    prevalence = (sum(labels) / len(labels)) if labels else float("nan")

    corr = {
        "impact": stats.spearman(impact, fixes),
        "impact_comp": stats.spearman(impact_comp, fixes),
        "churn": stats.spearman(churn, fixes),
        "commits": stats.spearman(ncomm, fixes),
        "hotspot": stats.spearman(hotspot, fixes),
    }
    partial_impact = stats.partial_spearman(impact, fixes, churn)
    partial_comp = stats.partial_spearman(impact_comp, fixes, churn)
    aucs = {
        "impact": stats.auc(impact, labels),
        "impact_comp": stats.auc(impact_comp, labels),
        "churn": stats.auc(churn, labels),
        "hotspot": stats.auc(hotspot, labels),
    }
    ks = [k for k in (10, 25, 50) if k <= len(uni)] or [max(1, len(uni) // 10)]
    patk = {k: (stats.precision_at_k(impact, labels, k),
                stats.precision_at_k(churn, labels, k)) for k in ks}

    # ---- write files.csv --------------------------------------------------
    uni_sorted = sorted(uni, key=lambda r: r.impact_mut, reverse=True)
    with open(os.path.join(out_dir, "files.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "lang", "impact_mut", "impact_composite", "churn", "commits",
                    "authors", "max_cc", "hotspot", "fix_all", "fix_after", "is_test"])
        for r in uni_sorted:
            w.writerow([r.path, r.lang, int(r.impact_mut), int(r.impact_comp),
                        int(r.churn_before), len(r.commits_before), len(r.authors), r.max_cc,
                        r.max_cc * len(r.commits_before), r.fix_all, r.fix_after, r.is_test])

    coupling = _coupling(db, min_support=3, top=25)
    with open(os.path.join(out_dir, "coupling.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["support", "confidence", "file_a", "file_b"])
        for s, conf, a, b in coupling:
            w.writerow([s, f"{conf:.2f}", a, b])

    # ---- write report.md --------------------------------------------------
    lines = []
    P = lines.append
    P(f"# Surveyor report\n")
    P(f"- files analysed (source, {'excl.' if exclude_tests else 'incl.'} tests): "
      f"**{len(uni)}**")
    P(f"- files with a bug-fix touch: **{sum(labels)}** "
      f"(prevalence {_fmt(prevalence)})")
    P(f"- outcome window: {'post-split fixes' if split_ts else 'ALL fixes (concurrent — see caveat)'}\n")
    if split_ts is None:
        P("> **Caveat:** no `--split-at` given, so predictors and bug outcomes are "
          "measured over the *same* period. This shows association, not prediction. "
          "Pass a split date for a leakage-free predictive test.\n")

    P("## Does change-impact predict bug-fix locations?\n")
    P("**Headline predictor: change-impact (mutation)** — the cost of *disturbing existing*"
      " code. The composite variant (which also counts one-time new code) is shown for"
      " contrast: new code inflates it but rarely gets fixed, so it is noise for defects.\n")
    P("Spearman correlation of each file-level signal with bug-fix count:\n")
    P("| signal | Spearman vs fixes | AUC (buggy vs not) |")
    P("|---|---|---|")
    P(f"| **change-impact (mutation)** | {_fmt(corr['impact'])} | {_fmt(aucs['impact'])} |")
    P(f"| change-impact (composite) | {_fmt(corr['impact_comp'])} | {_fmt(aucs['impact_comp'])} |")
    P(f"| churn (add+del) | {_fmt(corr['churn'])} | {_fmt(aucs['churn'])} |")
    P(f"| commit frequency | {_fmt(corr['commits'])} | n/a |")
    P(f"| hotspot (cc×freq) | {_fmt(corr['hotspot'])} | {_fmt(aucs['hotspot'])} |")
    P("")
    P(f"**Partial** Spearman(change-impact mutation, fixes | churn) = **{_fmt(partial_impact)}** "
      f"(composite: {_fmt(partial_comp)}) — the mutation signal *after* removing churn. This is "
      f"the honest number: it must stay clearly positive for change-impact to add value beyond "
      f"raw churn.\n")

    P("### Precision@k (top-k most-impactful files that are bug sites)\n")
    P("| k | precision@k (impact) | precision@k (churn) | lift vs base |")
    P("|---|---|---|---|")
    for k in ks:
        pi, pc = patk[k]
        lift = pi / prevalence if prevalence and prevalence == prevalence else float("nan")
        P(f"| {k} | {_fmt(pi)} | {_fmt(pc)} | {_fmt(lift)} |")
    P("")

    P("## Top 20 painful files (by change-impact mutation)\n")
    P("| impact_mut | churn | commits | max_cc | fixes | file |")
    P("|---|---|---|---|---|---|")
    for r in uni_sorted[:20]:
        P(f"| {int(r.impact_mut)} | {int(r.churn_before)} | {len(r.commits_before)} "
          f"| {r.max_cc} | {outcome(r)} | `{r.path}` |")
    P("")

    if coupling:
        P("## Top temporally-coupled file pairs (hidden dependencies)\n")
        P("| support | confidence | file A | file B |")
        P("|---|---|---|---|")
        for s, conf, a, b in coupling[:15]:
            P(f"| {s} | {conf:.2f} | `{a}` | `{b}` |")
        P("")

    # ---- per-commit metric charts (commits.html) ----
    METRICS = [
        ("impact_mutation", "change-impact (mutation of existing code)"),
        ("impact_composite", "change-impact (composite)"),
        ("impact_godclass", "change-impact (god-class / new code)"),
        ("files_changed", "files changed"),
        ("mut_fns", "functions modified"),
        ("new_fns", "functions added"),
        ("renames", "renames"),
    ]
    crows = []
    q = ("SELECT sha, subject, is_fix, is_revert, impact_composite, impact_mutation, "
         "impact_godclass, files_changed, mut_fns, new_fns, renames "
         "FROM commits ORDER BY rowid")
    for i, row in enumerate(db.execute(q)):
        d = {"idx": i, "sha": row[0], "subject": row[1] or "",
             "bug": bool(row[2] or row[3])}   # red = is_fix OR is_revert
        for (key, _label), val in zip(METRICS, row[4:]):
            d[key] = val or 0
        crows.append(d)
    if crows:
        meta = db.execute("SELECT value FROM meta WHERE key='repo_path'").fetchone()
        title = os.path.basename((meta[0] if meta else "repo").rstrip("/")) or "repo"
        plot.write_commit_charts(crows, METRICS, os.path.join(out_dir, "commits.html"), title)
        P("## Per-commit metric charts\n")
        P(f"See **commits.html** — one scatter per per-commit metric, {len(crows):,} commits "
          "on X (oldest → newest), value on Y, with **red = bug-fix commit** (fix keyword or "
          "revert) and **blue = ordinary change**. Lets you eyeball where fixes land relative "
          "to high-impact changes.\n")

    report = "\n".join(lines)
    with open(os.path.join(out_dir, "report.md"), "w") as fh:
        fh.write(report)
    db.close()
    log(f"wrote {out_dir}/report.md, files.csv, coupling.csv, commits.html")
    return {"corr": corr, "partial_impact": partial_impact, "partial_comp": partial_comp,
            "auc": aucs, "precision_at_k": patk, "prevalence": prevalence,
            "n_files": len(uni), "n_buggy": sum(labels)}
