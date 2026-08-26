"""Walk a repo's history and persist change-impact + churn per commit/file/unit.

Incremental and resumable: already-processed commits are skipped, and the parse
cache (by blob OID) means a re-run is near-instant over seen file versions.
"""
from __future__ import annotations

from . import bugs
from .config import Config
from .impact import compute_file_impact
from .plugins.base import get_plugin
from .repo import GitRepo
from .store import Store


def _units(store: Store, cfg: Config, repo: GitRepo, rev: str, path: str):
    """Return (source_lines, units) for <rev>:<path>, or (None, None) if absent."""
    got = repo.blob(rev, path)
    if got is None:
        return None, None
    oid, data = got
    units = store.cached_units(oid)
    if units is None:
        plugin = get_plugin(cfg.ext(path))
        units = plugin.parse(data, path) if plugin else []
        store.cache_units(oid, units)
    src = data.decode("utf-8", errors="replace").split("\n")
    return src, units


def _process_commit(repo, store, cfg, c, want_units, want_szz, wmc_context="before"):
    flags = bugs.classify(c)
    diffs = repo.diff(c.parent, c.sha)

    touched_src = [d for d in diffs
                   if not d.is_binary and cfg.is_source(d.path) and d.status in ("A", "M", "R")]
    files_changed = len(touched_src)

    # SZZ: for a bug-fix, blame the lines it changed to find the inducing commit(s).
    # Skip sprawling fixes (huge refactors/reverts) to bound blame cost and noise.
    do_szz = want_szz and flags.is_fix and 0 < files_changed <= cfg.szz_max_files

    sum_mut = sum_god = mut_fns = new_fns = renames = 0

    # churn/coupling cover ALL non-ignored files (docs, config too); impact only source.
    for d in diffs:
        if d.is_binary or cfg.is_ignored(d.path):
            continue
        fid = store.resolve_file(d.path, d.old_path if d.status == "R" else None)
        if do_szz and d.removed and cfg.is_source(d.path):
            for isha in repo.blame_ranges(c.parent, d.old_path or d.path, d.removed):
                if isha != c.sha:
                    store.add_bug_link(c.sha, isha, fid)
        fi = None
        is_src = cfg.is_source(d.path) and d.status in ("A", "M", "R")
        if is_src and (d.add_total + d.del_total) <= cfg.max_diff_lines:
            before_src, before_units = ([], []) if d.status == "A" \
                else _units(store, cfg, repo, c.parent, d.old_path or d.path)
            after_src, after_units = _units(store, cfg, repo, c.sha, d.new_path or d.path)
            if after_units is not None:
                fi = compute_file_impact(
                    before_units or [], after_units,
                    before_src or [], after_src,
                    d.added, d.removed, cfg.rename_jaccard, wmc_context,
                )
        if fi is not None:
            sum_mut += fi.mutation_cost
            sum_god += fi.godclass_cost
            mut_fns += fi.mut_fns
            new_fns += fi.new_fns
            renames += fi.renames
            if want_units:
                for u in fi.units:
                    store.add_unit_change(dict(
                        sha=c.sha, file_id=fid, unit=u.name, container=u.container,
                        cc=u.cc, dlines=u.dlines, wmc_other=u.wmc_other,
                        cost=u.cost, kind=u.kind))
        store.add_file_change(dict(
            sha=c.sha, file_id=fid, path=d.path, lang=cfg.language(d.path) or "",
            status=d.status, add_lines=d.add_total, del_lines=d.del_total,
            mutation_cost=fi.mutation_cost if fi else 0,
            godclass_cost=fi.godclass_cost if fi else 0,
            mut_fns=fi.mut_fns if fi else 0, new_fns=fi.new_fns if fi else 0,
            is_test=int(cfg.is_test(d.path)), ts=c.ts, is_fix=int(flags.is_fix)))

    composite = (sum_mut + sum_god) * files_changed
    store.add_commit(dict(
        sha=c.sha, parent=c.parent, author=c.author, email=c.email, ts=c.ts,
        is_merge=int(c.is_merge), is_fix=int(flags.is_fix), is_revert=int(flags.is_revert),
        has_issue_ref=int(flags.has_issue_ref), has_keyword=int(flags.has_keyword),
        issue_refs=",".join(flags.issue_refs), files_changed=files_changed,
        subject=c.subject, impact_mutation=sum_mut, impact_godclass=sum_god,
        impact_composite=composite, mut_fns=mut_fns, new_fns=new_fns, renames=renames))


def scan(repo_path: str, db_path: str, cfg: Config, *, since=None, until="HEAD",
         max_count=None, want_units=True, want_szz=True, log=print,
         progress_path=None, wmc_context="before") -> int:
    repo = GitRepo(repo_path)
    store = Store(db_path)
    store.set_meta("repo_path", repo_path)
    store.set_meta("wmc_context", wmc_context)
    commits = repo.commits(since=since, until=until, max_count=max_count)
    total = len(commits)

    def _emit(done_n: int) -> None:
        # tiny "<done> <total>\n" file a parallel driver can poll for a progress bar.
        if not progress_path:
            return
        try:
            with open(progress_path, "w") as fh:
                fh.write(f"{done_n} {total}\n")
        except OSError:
            pass

    done = {r[0] for r in store.db.execute("SELECT sha FROM commits").fetchall()}
    step = max(1, total // 200)   # ~200 progress updates across the history
    processed = 0
    _emit(0)
    try:
        for i, c in enumerate(commits):
            if progress_path and (i % step == 0 or i == total - 1):
                _emit(i + 1)   # advances even over already-done commits on a resume
            if c.sha in done:
                continue
            _process_commit(repo, store, cfg, c, want_units, want_szz, wmc_context)
            store.set_progress(c.sha)
            processed += 1
            if processed % 50 == 0:
                store.commit()
                log(f"  ... {processed} commits ({i + 1}/{total})")
        store.commit()
        _emit(total)
    finally:
        repo.close()
        store.close()
    log(f"scanned {processed} new commits ({len(commits)} on mainline, "
        f"{len(done)} already done)")
    return processed
