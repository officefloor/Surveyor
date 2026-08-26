"""Validation layer: does change-impact predict where the pain lands?

Aggregates the raw scan tables to per-file features, then tests change-impact
against a repo-mined bug ground-truth AND against churn/frequency baselines, so a
result means "impact beats/augments size & churn", not "big files have bugs".

Writes report.md + files.csv + coupling.csv + commits.html to an output directory.
Pure Python.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
from dataclasses import dataclass, field

from . import plot, stats
from .repo import GitRepo


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
    entropy: float = 0.0          # Hassan change-entropy accrued from pre-split commits
    size: float = float("nan")    # file size (bytes) at the split snapshot
    wmc: float = float("nan")     # total complexity (sum CC) at the split snapshot
    fix_after: int = 0     # bug-fix commits touching the file in the outcome window
    fix_all: int = 0

    @property
    def n_commits(self) -> float:
        return float(len(self.commits_before))


def _accrue_entropy(per_sha: dict, files: dict) -> None:
    """Change entropy (Hassan): each file accrues the entropy of every commit it was
    part of, where a commit's entropy is over how its churn spreads across the files
    it touched. Mutates each FileRow.entropy in place."""
    for dist in per_sha.values():
        tot = sum(dist.values())
        if tot <= 0 or len(dist) < 2:
            continue
        H = 0.0
        for c in dist.values():
            if c > 0:
                p = c / tot
                H -= p * math.log(p, 2)
        for fid in dist:
            if fid in files:
                files[fid].entropy += H


def _load(db: sqlite3.Connection, split_ts: int | None, exclude_tests: bool):
    files: dict[int, FileRow] = {}
    paths = dict(db.execute("SELECT id, canonical_path FROM file_ids").fetchall())
    # commit author lookup for ownership
    authors = dict(db.execute("SELECT sha, email FROM commits").fetchall())

    per_sha: dict[str, dict[int, float]] = {}   # pre-split churn distribution, for entropy
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
            ch = (add or 0) + (dele or 0)
            fr.churn_before += ch
            fr.commits_before.add(sha)
            if authors.get(sha):
                fr.authors.add(authors[sha])
            if ch > 0:
                d = per_sha.setdefault(sha, {})
                d[fid] = d.get(fid, 0.0) + ch
        if is_fix:
            fr.fix_all += 1
            if split_ts is not None and ts > split_ts:
                fr.fix_after += 1
    # max cc per file from units
    for fid, mcc in db.execute("SELECT file_id, MAX(cc) FROM unit_changes GROUP BY file_id"):
        if fid in files:
            files[fid].max_cc = mcc or 0

    _accrue_entropy(per_sha, files)

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


def _blob_wmc(db, oid: str, cache: dict) -> int | None:
    """Total complexity (sum CC) of a blob, from the parse cache. None if not parsed."""
    if oid in cache:
        return cache[oid]
    row = db.execute("SELECT units FROM blob_cache WHERE oid=?", (oid,)).fetchone()
    val = None
    if row:
        try:
            val = sum(u[4] for u in json.loads(row[0]))   # cc is unit field index 4
        except (ValueError, IndexError, TypeError):
            val = None
    cache[oid] = val
    return val


def _fill_size_wmc(db, split_ts: int | None, rows: list) -> None:
    """Attach file size (bytes) and total complexity (sum CC) at the split snapshot,
    read from the repo tree + blob parse cache. Best-effort: leaves NaN when the repo
    is unavailable or a blob was never parsed (e.g. a file untouched in the window)."""
    meta = db.execute("SELECT value FROM meta WHERE key='repo_path'").fetchone()
    if not meta or not meta[0]:
        return
    if split_ts is not None:
        r = db.execute("SELECT sha FROM commits WHERE ts<=? ORDER BY ts DESC, rowid DESC "
                       "LIMIT 1", (split_ts,)).fetchone()
        rev = r[0] if r else "HEAD"
    else:
        rev = "HEAD"
    try:
        repo = GitRepo(meta[0])
        tree = repo.tree_entries(rev)
        repo.close()
    except Exception:
        return
    if not tree:
        return
    alias = dict(db.execute("SELECT path, file_id FROM file_alias"))
    size_by_fid: dict[int, int] = {}
    wmc_by_fid: dict[int, int] = {}
    cache: dict = {}
    for path, (oid, size) in tree.items():
        fid = alias.get(path)
        if fid is None:
            continue
        size_by_fid[fid] = size
        w = _blob_wmc(db, oid, cache)
        if w is not None:
            wmc_by_fid[fid] = w
    for r in rows:
        if r.file_id in size_by_fid:
            r.size = float(size_by_fid[r.file_id])
        if r.file_id in wmc_by_fid:
            r.wmc = float(wmc_by_fid[r.file_id])


def _predictor_stats(uni: list, split_ts: int | None) -> dict:
    """All file-level statistics: Spearman / partial / AUC / precision@k of change-impact
    vs each baseline. Returns a bundle consumed by the report renderer and stats.json."""
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
    # head-to-head vs the nearest structural rival: does impact survive removing
    # the hotspot baseline (max_cc x change-frequency), not just churn?
    partial_impact_hot = stats.partial_spearman(impact, fixes, hotspot)

    # ---- extra baselines: file size, total complexity, change entropy, ownership,
    # prior bug-fixes. Each asks whether impact survives removing that rival alone. ----
    size = [r.size for r in uni]                       # bytes at split (NaN if unknown)
    wmc = [r.wmc for r in uni]                         # sum CC at split (NaN if unknown)
    entropy = [r.entropy for r in uni]                 # Hassan change entropy
    ndev = [float(len(r.authors)) for r in uni]        # distinct developers, pre-split
    pfix = [float(r.fix_all - r.fix_after) for r in uni]   # prior bug-fixes (pre-split)

    def _fin2(a, b):
        A, B = [], []
        for x, y in zip(a, b):
            if x == x and y == y:      # drop NaN pairs
                A.append(x); B.append(y)
        return A, B

    def _fin3(a, b, c):
        A, B, C = [], [], []
        for x, y, z in zip(a, b, c):
            if x == x and y == y and z == z:
                A.append(x); B.append(y); C.append(z)
        return A, B, C

    def _sp(pred):
        a, b = _fin2(pred, fixes)
        return stats.spearman(a, b) if len(a) > 2 else float("nan")

    def _pt(pred):    # partial(change-impact, fixes | pred) on the finite subset
        a, b, c = _fin3(impact, fixes, pred)
        return stats.partial_spearman(a, b, c) if len(a) > 2 else float("nan")

    def _ac(pred):
        a, b = _fin2(pred, [float(x) for x in labels])
        return stats.auc(a, [int(v) for v in b]) if a else float("nan")

    xbase = {"size": size, "wmc": wmc, "entropy": entropy, "ndev": ndev, "prior_fixes": pfix}
    sp_extra = {k: _sp(v) for k, v in xbase.items()}
    auc_extra = {k: _ac(v) for k, v in xbase.items()}
    partials = {"churn": partial_impact, "hotspot": partial_impact_hot,
                "size": _pt(size), "wmc": _pt(wmc), "entropy": _pt(entropy),
                "ndev": _pt(ndev), "prior_fixes": _pt(pfix)}

    # multivariate: does impact survive removing a whole SET of rivals at once? The
    # single-control partials above each remove one; this removes them together, the real
    # "not just a mix of known signals" test. prior_fixes is excluded (partly circular).
    def _mv(zcols):
        X, Y, Z = [], [], [[] for _ in zcols]
        for i in range(len(uni)):
            vals = [impact[i], fixes[i]] + [z[i] for z in zcols]
            if all(v == v for v in vals):          # keep rows finite across the whole set
                X.append(impact[i]); Y.append(fixes[i])
                for j, z in enumerate(zcols):
                    Z[j].append(z[i])
        return stats.partial_spearman_multi(X, Y, Z) if len(X) > len(zcols) + 3 else float("nan")

    partials["multi_core"] = _mv([churn, hotspot, size, wmc])                  # strong rivals
    partials["multi_all"] = _mv([churn, hotspot, size, wmc, entropy, ndev])    # + weak process
    aucs = {
        "impact": stats.auc(impact, labels),
        "impact_comp": stats.auc(impact_comp, labels),
        "churn": stats.auc(churn, labels),
        "hotspot": stats.auc(hotspot, labels),
    }
    ks = [k for k in (10, 25, 50) if k <= len(uni)] or [max(1, len(uni) // 10)]
    patk = {k: (stats.precision_at_k(impact, labels, k),
                stats.precision_at_k(churn, labels, k)) for k in ks}
    return {"corr": corr, "aucs": aucs, "partial_impact": partial_impact,
            "partial_comp": partial_comp, "partial_impact_hot": partial_impact_hot,
            "partials": partials, "sp_extra": sp_extra, "auc_extra": auc_extra,
            "patk": patk, "ks": ks, "prevalence": prevalence, "n_buggy": sum(labels)}


def _write_files_csv(out_dir: str, uni_sorted: list) -> None:
    with open(os.path.join(out_dir, "files.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "lang", "impact_mut", "impact_composite", "churn", "commits",
                    "authors", "max_cc", "hotspot", "fix_all", "fix_after", "is_test"])
        for r in uni_sorted:
            w.writerow([r.path, r.lang, int(r.impact_mut), int(r.impact_comp),
                        int(r.churn_before), len(r.commits_before), len(r.authors), r.max_cc,
                        r.max_cc * len(r.commits_before), r.fix_all, r.fix_after, r.is_test])


def _write_coupling_csv(out_dir: str, coupling: list) -> None:
    with open(os.path.join(out_dir, "coupling.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["support", "confidence", "file_a", "file_b"])
        for s, conf, a, b in coupling:
            w.writerow([s, f"{conf:.2f}", a, b])


def _szz(db: sqlite3.Connection) -> dict | None:
    """SZZ: do high-impact commits induce later bug-fixes? Returns per-commit ranking
    stats (AUC + honest partials vs commit size/spread/complexity) or None if the scan
    holds no bug-link ground-truth."""
    induced = {r[0] for r in db.execute("SELECT DISTINCT inducing_sha FROM bug_links")}
    if not induced:
        return None
    churn_by_sha = dict(db.execute(
        "SELECT sha, SUM(add_lines + del_lines) FROM file_changes GROUP BY sha"))
    # commit-level complexity-volume controls, independent of the impact weighting
    cc_by_sha, nu_by_sha = {}, {}
    for sha, scc, ncnt in db.execute(
            "SELECT sha, SUM(cc), COUNT(*) FROM unit_changes GROUP BY sha"):
        cc_by_sha[sha] = scc or 0
        nu_by_sha[sha] = ncnt or 0
    # per-commit change entropy (how the commit's churn spreads across its files)
    ent_by_sha, _tmp = {}, {}
    for sha, fid, a, d in db.execute(
            "SELECT sha, file_id, add_lines, del_lines FROM file_changes"):
        ch = (a or 0) + (d or 0)
        if ch > 0:
            dd = _tmp.setdefault(sha, {})
            dd[fid] = dd.get(fid, 0) + ch
    for sha, dd in _tmp.items():
        tot = sum(dd.values())
        H = 0.0
        if tot > 0 and len(dd) > 1:
            for c in dd.values():
                p = c / tot
                H -= p * math.log(p, 2)
        ent_by_sha[sha] = H

    c_imp, c_comp, c_churn, c_files, c_cc, c_nu, c_ent, c_lab = ([] for _ in range(8))
    for sha, im, ic, fchg in db.execute(
            "SELECT sha, impact_mutation, impact_composite, files_changed "
            "FROM commits WHERE is_merge=0"):
        c_imp.append(im or 0)
        c_comp.append(ic or 0)
        c_churn.append(churn_by_sha.get(sha, 0) or 0)
        c_files.append(fchg or 0)
        c_cc.append(cc_by_sha.get(sha, 0))
        c_nu.append(nu_by_sha.get(sha, 0))
        c_ent.append(ent_by_sha.get(sha, 0.0))
        c_lab.append(1 if sha in induced else 0)
    n_linked = db.execute("SELECT COUNT(DISTINCT fix_sha) FROM bug_links").fetchone()[0]
    auc_mut, auc_comp, auc_ch = (stats.auc(c_imp, c_lab), stats.auc(c_comp, c_lab),
                                 stats.auc(c_churn, c_lab))
    # Incremental / "beyond size" test: does impact rank inducers after removing
    # commit churn? (AUCs alone are size-inflated — a bigger commit has more lines
    # a later fix can blame, so churn alone already ranks inducers well.)
    lab_f = [float(x) for x in c_lab]
    pind_mut = stats.partial_spearman(c_imp, lab_f, c_churn)
    pind_comp = stats.partial_spearman(c_comp, lab_f, c_churn)

    # multivariate: does composite rank inducers beyond commit size (lines), spread
    # (files touched), and complexity volume (ΣCC, #units) removed all at once?
    def _mvz(zcols):
        return (stats.partial_spearman_multi(c_comp, lab_f, zcols)
                if len(c_comp) > len(zcols) + 3 else float("nan"))
    szz_multi_core = _mvz([c_churn, c_files, c_cc, c_nu])
    szz_multi_all = _mvz([c_churn, c_files, c_cc, c_nu, c_ent])
    order = sorted(range(len(c_imp)), key=lambda i: c_imp[i])
    nq = len(order)
    quart = []
    for k in range(4):
        seg = order[k * nq // 4:(k + 1) * nq // 4]
        quart.append(sum(c_lab[i] for i in seg) / len(seg) if seg else float("nan"))
    base = (sum(c_lab) / len(c_lab)) if c_lab else float("nan")
    return {"auc_mut": auc_mut, "auc_comp": auc_comp, "auc_churn": auc_ch,
            "partial_mut": pind_mut, "partial_comp": pind_comp,
            "multi_core": szz_multi_core, "multi_all": szz_multi_all,
            "n_inducing": len(induced), "n_linked_fixes": n_linked,
            "n_commits": len(c_lab), "base_rate": base, "quartiles": quart}


def _render_report(uni: list, uni_sorted: list, S: dict, coupling: list,
                   szz: dict | None, split_ts: int | None, exclude_tests: bool) -> list:
    """Build report.md body lines (everything but the per-commit charts section)."""
    corr, aucs = S["corr"], S["aucs"]
    sp_extra, auc_extra = S["sp_extra"], S["auc_extra"]
    partial_impact, partial_comp = S["partial_impact"], S["partial_comp"]
    partial_impact_hot, partials = S["partial_impact_hot"], S["partials"]
    ks, patk, prevalence, n_buggy = S["ks"], S["patk"], S["prevalence"], S["n_buggy"]

    def outcome(r: FileRow) -> int:
        return r.fix_after if split_ts is not None else r.fix_all

    lines = []
    P = lines.append
    P(f"# Surveyor report\n")
    P(f"- files analysed (source, {'excl.' if exclude_tests else 'incl.'} tests): "
      f"**{len(uni)}**")
    P(f"- files with a bug-fix touch: **{n_buggy}** "
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
    P(f"| file size (bytes) | {_fmt(sp_extra['size'])} | {_fmt(auc_extra['size'])} |")
    P(f"| total complexity (ΣCC) | {_fmt(sp_extra['wmc'])} | {_fmt(auc_extra['wmc'])} |")
    P(f"| change entropy (Hassan) | {_fmt(sp_extra['entropy'])} | {_fmt(auc_extra['entropy'])} |")
    P(f"| prior bug-fixes | {_fmt(sp_extra['prior_fixes'])} | {_fmt(auc_extra['prior_fixes'])} |")
    P(f"| developers touching file | {_fmt(sp_extra['ndev'])} | {_fmt(auc_extra['ndev'])} |")
    P("")
    P(f"**Partial** Spearman(change-impact mutation, fixes | churn) = **{_fmt(partial_impact)}** "
      f"(composite: {_fmt(partial_comp)}) — the mutation signal *after* removing churn. This is "
      f"the honest number: it must stay clearly positive for change-impact to add value beyond "
      f"raw churn.\n")
    P(f"**Partial** Spearman(change-impact mutation, fixes | hotspot) = **{_fmt(partial_impact_hot)}** "
      f"— the mutation signal *after* removing the **hotspot** baseline (complexity×change-frequency), "
      f"the nearest structural rival. Positive means change-impact adds signal beyond hotspot too, "
      f"not only beyond churn.\n")

    P("### Change-impact beyond each baseline\n")
    P("Partial Spearman of change-impact (mutation) with fixes, controlling for each rival "
      "signal *one at a time*. Impact must stay positive after removing each:\n")
    P("| control X | partial(change-impact, fixes \\| X) |")
    P("|---|---|")
    for key, label in (("churn", "churn (add+del)"), ("hotspot", "hotspot (cc×freq)"),
                       ("size", "file size (bytes)"), ("wmc", "total complexity (ΣCC)"),
                       ("entropy", "change entropy (Hassan)"), ("prior_fixes", "prior bug-fixes"),
                       ("ndev", "developers touching file")):
        P(f"| {label} | {_fmt(partials[key])} |")
    P("")
    P("> **Note:** these are single-control partials — each removes one rival. `prior bug-fixes` "
      "is a strong but partly circular baseline (past fixes vs future fixes under the same label).\n")

    P("### Multivariate — change-impact beyond the whole rival set at once\n")
    P("Partial Spearman controlling for *all* controls together (rank-transform + OLS "
      "residualisation), the strongest \"not just a mix of known signals\" test. Correlated "
      "controls make it shrink; staying positive is the bar:\n")
    P(f"- vs **churn + hotspot + size + complexity**: **{_fmt(partials['multi_core'])}**")
    P(f"- vs **+ entropy + developers**: **{_fmt(partials['multi_all'])}**")
    P("")

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

    if szz:
        _render_szz(P, szz)
    return lines


def _render_szz(P, szz: dict) -> None:
    """Append the SZZ report section from a computed _szz() bundle."""
    n_linked = szz["n_linked_fixes"]
    induced_n, n_commits, base = szz["n_inducing"], szz["n_commits"], szz["base_rate"]
    auc_comp, auc_mut, auc_ch = szz["auc_comp"], szz["auc_mut"], szz["auc_churn"]
    pind_comp, pind_mut = szz["partial_comp"], szz["partial_mut"]
    szz_multi_core, szz_multi_all = szz["multi_core"], szz["multi_all"]
    quart = szz["quartiles"]
    P("## SZZ: do high-impact commits induce later bug-fixes?\n")
    P(f"- fix commits linked to an inducer: **{n_linked:,}**")
    P(f"- distinct inducing commits: **{induced_n:,}** of {n_commits:,} non-merge "
      f"commits (base induce-rate {_fmt(base)})\n")
    P("How well each per-commit signal ranks *inducing* commits — AUC (size-inflated), "
      "and the honest **partial vs churn** (does impact rank inducers *beyond* commit "
      "size?):\n")
    P("| predictor | AUC (inducing vs not) | partial vs churn |")
    P("|---|---|---|")
    P(f"| **change-impact (composite)** | {_fmt(auc_comp)} | **{_fmt(pind_comp)}** |")
    P(f"| change-impact (mutation) | {_fmt(auc_mut)} | {_fmt(pind_mut)} |")
    P(f"| commit churn | {_fmt(auc_ch)} | — |")
    P("")
    P(f"**Partial(change-impact composite, inducing | churn) = {_fmt(pind_comp)}** — "
      "positive means composite impact ranks bug-inducing commits *beyond* what raw "
      "commit size explains. This is the honest number; the AUCs above are inflated "
      "because a bigger commit simply has more lines a later fix can blame.\n")
    P(f"**Multivariate** partial(composite, inducing | churn + files + ΣCC + #units) = "
      f"**{_fmt(szz_multi_core)}**; adding commit entropy = {_fmt(szz_multi_all)}. This "
      "removes commit size (lines), spread (files touched), and complexity volume "
      "*together* — the strong test that composite is more than 'a big, sprawling, "
      "complex commit'. Correlated controls make it shrink; staying positive is the bar.\n")
    P("Induce-rate by change-impact (mutation) quartile — expect it to rise Q1→Q4:\n")
    P("| Q1 (low) | Q2 | Q3 | Q4 (high) |")
    P("|---|---|---|---|")
    P(f"| {_fmt(quart[0])} | {_fmt(quart[1])} | {_fmt(quart[2])} | {_fmt(quart[3])} |")
    P("")
    P("> **Caveat:** recency censoring — recent commits have had less time to be blamed "
      "by a later fix, so they under-count as inducers. Read the quartile *trend*, not the "
      "absolute rates.\n")


def _write_commit_charts(db: sqlite3.Connection, out_dir: str) -> list:
    """Write commits.html (one scatter per per-commit metric) and return the report
    section lines pointing at it (empty if there are no commits)."""
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
    if not crows:
        return []
    meta = db.execute("SELECT value FROM meta WHERE key='repo_path'").fetchone()
    title = os.path.basename((meta[0] if meta else "repo").rstrip("/")) or "repo"
    plot.write_commit_charts(crows, METRICS, os.path.join(out_dir, "commits.html"), title)
    return [
        "## Per-commit metric charts\n",
        f"See **commits.html** — one scatter per per-commit metric, {len(crows):,} commits "
        "on X (oldest → newest), value on Y, with **red = bug-fix commit** (fix keyword or "
        "revert) and **blue = ordinary change**. Lets you eyeball where fixes land relative "
        "to high-impact changes.\n",
    ]


def _write_stats_json(db: sqlite3.Connection, out_dir: str, S: dict,
                      split_ts: int | None, n_files: int, szz: dict | None) -> None:
    """Machine-readable stats for a cross-repo summary (analyze-all --summary-only)."""
    meta_rp = db.execute("SELECT value FROM meta WHERE key='repo_path'").fetchone()
    name = os.path.basename((meta_rp[0] if meta_rp else out_dir).rstrip("/")) or "repo"
    with open(os.path.join(out_dir, "stats.json"), "w") as fh:
        json.dump({"name": name, "split_ts": split_ts, "corr": S["corr"],
                   "partial_impact": S["partial_impact"], "partial_comp": S["partial_comp"],
                   "partial_impact_hot": S["partial_impact_hot"], "partials": S["partials"],
                   "sp_extra": S["sp_extra"], "auc_extra": S["auc_extra"],
                   "auc": S["aucs"], "prevalence": S["prevalence"], "n_files": n_files,
                   "n_buggy": S["n_buggy"], "szz": szz}, fh)


def analyze(db_path: str, out_dir: str, *, split_ts: int | None = None,
            exclude_tests: bool = True, log=print) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    db = sqlite3.connect(db_path)
    rows, uni = _load(db, split_ts, exclude_tests)
    _fill_size_wmc(db, split_ts, uni)

    S = _predictor_stats(uni, split_ts)
    uni_sorted = sorted(uni, key=lambda r: r.impact_mut, reverse=True)
    _write_files_csv(out_dir, uni_sorted)

    coupling = _coupling(db, min_support=3, top=25)
    _write_coupling_csv(out_dir, coupling)

    szz = _szz(db)
    lines = _render_report(uni, uni_sorted, S, coupling, szz, split_ts, exclude_tests)
    lines += _write_commit_charts(db, out_dir)
    with open(os.path.join(out_dir, "report.md"), "w") as fh:
        fh.write("\n".join(lines))

    _write_stats_json(db, out_dir, S, split_ts, len(uni), szz)
    db.close()
    log(f"wrote {out_dir}/report.md, files.csv, coupling.csv, commits.html, stats.json")
    return {"corr": S["corr"], "partial_impact": S["partial_impact"],
            "partial_comp": S["partial_comp"], "partial_impact_hot": S["partial_impact_hot"],
            "partials": S["partials"], "sp_extra": S["sp_extra"], "auc_extra": S["auc_extra"],
            "auc": S["aucs"], "precision_at_k": S["patk"], "prevalence": S["prevalence"],
            "n_files": len(uni), "n_buggy": S["n_buggy"], "szz": szz}
