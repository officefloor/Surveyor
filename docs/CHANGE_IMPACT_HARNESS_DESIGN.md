# Surveyor — a language-agnostic change-impact + pain-signal harness

> Working name: **Surveyor**. A standalone, **offline**, **no-AI** harness that
> reads a project's git history, computes the **change-impact** score per commit,
> mines other repository-derived pain signals, extracts a bug ground-truth from the
> repo itself, and tests whether change-impact **predicts** where the pain lands.
>
> Built to run unattended for hours on a laptop with no internet: pure CPU over a
> local clone, fully resumable, results in a local SQLite + markdown/CSV report.

## 1. Purpose

The PetClinic-Evolve experiment validated the change-impact score by *concurrent
correlation with the AI agent's own consumption*, on two hand-built codebases. The
open question (see the blog series and `RUN_WITH_A_DIFFERENT_MODEL.md`) is
**external, predictive validity**:

> Computed on real projects it has never seen, does a high change-impact score
> predict independently-recorded pain — bug fixes, reverts, files that never settle?

The change-impact formula is **not Java-specific** (it needs only per-function
complexity, line ranges, and container membership), so Surveyor supports many
languages via **plugins** and works on any git repo.

Two jobs, cleanly separated:

1. **Measure** — compute change-impact + a battery of repo-derived signals per
   commit / file / function across history.
2. **Validate** — extract a bug ground-truth from the repo and test whether
   change-impact (and each other signal) predicts it, *beyond* trivial baselines
   like size and churn.

## 2. The measure, generalized

Recap of the score (from `metrics.impact_stats`):

```
cost(unit)     = max(WMC_other, 1) · CC · max(1, Δlines)
change_impact  = files_changed · Σ_units cost(unit)
```

- `unit` = a changed function/method in a commit.
- `CC` = that unit's cyclomatic complexity (post-change).
- `Δlines` = lines the diff changed inside the unit.
- `WMC_other` = Σ CC of the **other** units in the same **container** (the context
  you must hold to change it safely), measured on the **pre-change (before)** container
  by default. Floor of 1. A unit added to a brand-new container therefore has no prior
  siblings → `WMC_other` = 1 (importing/greenfield code costs only ~`CC·Δlines`), while
  a method accreted onto an existing (god) class is still charged for the siblings that
  were already there. `--wmc-context after` measures it on the resulting file instead
  (the pre-2026-08 definition; reproduces the older scores).
- `files_changed` = distinct source files the commit touched (a spread penalty).
- A within-commit **rename** (body token-set Jaccard ≥ 0.6) is scored as a
  mutation, not a free addition.

**Language generalization of "container".** The plugin defines it:

| language kind | container | WMC_other = |
|---|---|---|
| OO with classes (Java, C#, TS/JS classes, Python classes, Kotlin, Swift) | the enclosing class | Σ CC of the class's other methods |
| module / free functions (C, Go, Python module funcs, JS module funcs) | the file/module | Σ CC of the file's other top-level functions |

That keeps "surrounding complexity you must comprehend" meaningful everywhere, and
makes the score defined for any parseable file.

## 3. Architecture

```
                    ┌───────────────────────────────────────────────┐
   local git repo ─▶│ repo.py   (git plumbing: log, cat-file, blame)│
                    └───────────────┬───────────────────────────────┘
                                    │ commits, diffs, blobs (by OID)
              ┌─────────────────────┼───────────────────────────────┐
              ▼                     ▼                                ▼
     ┌────────────────┐   ┌──────────────────┐            ┌──────────────────┐
     │ plugins/*      │   │ signals.py       │            │ bugs.py          │
     │ parse blob →   │   │ parser-free      │            │ bug-fix + revert │
     │ Units (CC,     │   │ file/commit      │            │ detection, issue │
     │ lines, cont.)  │   │ signals (Tier 1) │            │ refs, SZZ blame  │
     └───────┬────────┘   └────────┬─────────┘            └────────┬─────────┘
             ▼                     │                               │
     ┌────────────────┐            │                               │
     │ impact.py      │            │                               │
     │ change-impact  │            │                               │
     │ (Tier 2)       │            │                               │
     └───────┬────────┘            │                               │
             └──────────┬──────────┴───────────────┬───────────────┘
                        ▼                           ▼
                 ┌──────────────┐           ┌──────────────────┐
                 │ store.py     │◀─ resume ─│ run.py (CLI)     │
                 │ SQLite cache │           │ incremental walk │
                 │ + results    │           └──────────────────┘
                 └──────┬───────┘
                        ▼
                 ┌──────────────┐
                 │ analyze.py   │  validation: impact vs bug ground-truth
                 │ report + CSV │  (AUC, precision@k, partial corr, regression)
                 └──────────────┘
```

Data flows **oldest→newest commit**; every expensive result is memoised in SQLite
keyed by content hash, so a killed run resumes from its cursor.

## 4. Module map

| module | responsibility |
|---|---|
| `repo.py` | Git access via subprocess only (no network). Enumerate commits (topo, `--first-parent`), parent lookup, `--numstat`/`--name-status -M` diffs, blob streaming via `git cat-file --batch`, `git blame -w -C` for SZZ. |
| `units.py` | The `Unit` dataclass + diff-hunk→unit mapping (line-range intersection), rename matching (Jaccard), container grouping. Language-independent. |
| `plugins/base.py` | `LanguagePlugin` interface + extension registry. |
| `plugins/lizard_plugin.py` | Default multi-language parser (CC, NLOC, line ranges, class-qualified names) via **lizard**. Covers Java, C#, JS/TS, Python, C/C++, Go, Kotlin, Swift, Ruby, PHP, Rust, … |
| `plugins/treesitter_*.py` | Optional higher-fidelity plugins (e.g. TSX/JSX, better container detection) via bundled tree-sitter grammars. |
| `impact.py` | Change-impact per commit from before/after Unit sets + diff (Tier 2). A lift-and-generalize of `harness/metrics.py:impact_stats`. |
| `signals.py` | Tier-1 parser-free file/commit signals (churn, frequency, coupling, ownership, commit size, keyword flags). |
| `bugs.py` | Bug-fix + revert identification, issue-ref extraction, optional issue-export ingestion, SZZ inducing-commit detection. |
| `store.py` | SQLite: blob-OID parse cache, per-commit/-file/-function results, per-file identity across renames, resumable progress cursor. |
| `analyze.py` | The validation layer: join impact/signals with the bug ground-truth; ranking (AUC, precision@k), correlation (Spearman, partial), incremental-value regression, temporal precedence; write report. |
| `run.py` | CLI driver: incremental, checkpointed walk; `analyze` subcommand. |
| `config.py` | Per-project config: path ignore globs, test-path heuristics, bug/revert keyword lists, language overrides, thresholds. |

## 5. Language plugin interface

```python
# plugins/base.py
from dataclasses import dataclass

@dataclass(frozen=True)
class Unit:
    name: str          # qualified, e.g. "OwnerController.addOwner"
    container: str      # cohesion scope: class name, or "<file>:path" for free funcs
    start_line: int     # 1-based, inclusive
    end_line: int
    cc: int             # cyclomatic complexity
    nloc: int
    token_hash: int     # hash of the normalised token multiset, for rename Jaccard
    tokens: frozenset   # normalised token set (or a minhash) for Jaccard scoring

class LanguagePlugin:
    name: str
    extensions: tuple[str, ...]           # e.g. (".java",)
    def parse(self, blob: bytes, path: str) -> list[Unit]: ...

# registry: extension -> plugin instance (config can override / disable)
```

`lizard_plugin` is the default for every extension lizard knows; a specific-language
plugin only needs writing where lizard is weak. **Key optimisation:** `parse()` is
called on a *blob*, so results are cached by the blob's git OID — an unchanged file
version is parsed once no matter how many commits carry it.

## 6. Git access (all offline)

- **Commit walk:** `git log --first-parent --reverse --format=...` (oldest→newest,
  merges collapsed to their first parent so a merge doesn't double-count).
- **Per-commit diff:** `git diff-tree -r -M --numstat --name-status <parent> <commit>`
  → per file: status (A/M/D/R), rename old→new, added/removed line counts.
- **Hunks (for line→unit mapping):** `git diff -U0 -M <parent> <commit> -- <file>`
  parsed into added/removed line ranges.
- **Blob content:** batch via `git cat-file --batch` keyed by the `<commit>:<path>`
  or blob OID from `git ls-tree` — stream, don't check out.
- **SZZ blame:** `git blame -w -C -L <line>,<line> <fix^> -- <file>` on each line a
  fix touched → the commit that last wrote it (approximate inducer).

No working-tree checkout is ever needed; everything reads objects. That makes it
safe to run against a bare or read-only clone and trivially parallelisable.

## 7. Change-impact computation (per commit)

For each non-merge commit `C` with first parent `P`:

1. From `name-status`, take modified/renamed source files with a registered plugin.
   Count `files_changed` over all touched source files (spread term).
2. For each such file: `before = parse(blob@P)`, `after = parse(blob@C)` (cache hits
   most of the time). Parse `-U0` hunks.
3. Map removed-line ranges onto `before` units, added-line ranges onto `after` units
   (line-range intersection). A unit touched by either side is **changed**;
   `Δlines` = touched lines within it.
4. Classify each changed unit: **new** (only in `after`, no rename match), **modified**
   (present in both), **renamed** (best Jaccard ≥ 0.6 to a removed `before` unit).
5. `WMC_other(u)` = Σ CC of the other units sharing `u.container`, in the **before**
   file by default (floor 1) — a new unit in a new container has none, so `WMC_other`=1;
   a new unit in a pre-existing container is charged for the siblings already there; a
   modified unit subtracts its own prior CC. `--wmc-context after` uses the resulting
   file instead. `cost(u) = max(WMC_other,1)·CC·max(1,Δlines)`.
6. `impact_mutation` = Σ cost over modified/renamed; `impact_godclass` = Σ cost over
   new; `impact_composite = (mutation+godclass) · files_changed`. Persist all three
   plus raw counts, per commit **and** attributed per file/per unit (for the
   file-level validation join).

This is the exact formula the Java harness uses; only the parsing and the container
rule are generalised.

## 8. Persistence & resumability (SQLite)

Stdlib `sqlite3`, one file per analysed repo. Tables:

- `blob_units(blob_oid PK, json)` — parse cache. The big time-saver.
- `commit(sha PK, parent, author, ts, files_changed, msg, is_merge, is_fix, is_revert, issue_refs)`.
- `commit_impact(sha PK, impact_mutation, impact_godclass, impact_composite, mut_fns, new_fns, renames)`.
- `file_change(sha, file_id, path, dlines_add, dlines_del, impact, is_test)` — per-file attribution.
- `file_id(id PK, canonical_path)` + `file_alias(path, file_id)` — stitches identity
  across renames (from `-M` records) so per-file aggregation survives moves.
- `unit_change(sha, file_id, unit_name, cc, dlines, impact, kind)`.
- `bug_link(fix_sha, inducing_sha, file_id, unit_name)` — SZZ output.
- `progress(key PK, last_commit)` — the resume cursor.

`run.py` commits progress after each source commit; re-invoking with `--resume`
continues from `progress.last_commit`. Parse cache means a resumed or re-run pass is
near-instant over already-seen blobs.

## 9. Tier-1 signals — parser-free, any repo, immediately

These need only diffs/metadata, so they run on **every** repo (even languages with no
plugin) and serve as **baselines and cross-validators** for change-impact.

| signal | definition | what it flags |
|---|---|---|
| **churn** | added+removed lines per file over window | instability (classic defect predictor) |
| **change frequency** | commits touching a file | hotspot activity |
| **temporal coupling** | files that repeatedly change together in one commit (co-change support/confidence) | hidden dependencies, architectural erosion |
| **ownership / authors** | distinct authors per file; top-author share; minor-contributor count | low-ownership + fragmented files are defect-prone |
| **commit size** | files & lines per commit; outlier large commits | risky changes |
| **fix-density** | bug-fix commits ÷ total commits per file | firefighting |
| **revert rate** | reverted commits per file | acute failure |
| **keyword flags** | messages / code with `hack`,`fixme`,`todo`,`temp`,`wip`,`revert`,`quick fix` | self-reported pain |
| **recency/age** | time since last change; recently-churned files | freshly-destabilised code |
| **test co-change ratio** | share of a prod file's changes accompanied by a test-file change | untested change risk |

The **hotspot = complexity × change-frequency** metric (Tornhill) is a Tier-1.5
signal (needs one CC read per file version, which the plugin already caches). It is
the single best-known validated pain predictor and a must-have **comparison
baseline** for change-impact.

## 10. Bug ground-truth from the repo alone

No network needed once the repo is cloned. Precision-ranked sources:

1. **Reverts** (highest precision): `Revert "..."` messages and `git revert` trailers.
2. **Issue-closing refs**: `fixes #123`, `closes GH-123`, `JIRA-456` in messages.
3. **Fix keywords**: `fix|bug|defect|hotfix|regression|broke|crash|npe|leak` (a
   configurable regex; report keyword-based results separately as lower precision).
4. **Optional issue export**: if the user pre-downloads issues (before going offline)
   as JSON/CSV, `bugs.py` ingests it and joins by issue id for typed, dated,
   labelled ground-truth. Purely additive.

**SZZ (the strong test).** For each fix commit, `git blame -w -C` the lines it
*modified* (not pure additions) at the fix's parent → the commits that introduced
those lines = **bug-inducing commits**. Then the sharp question becomes:

> Did the bug-inducing commits carry **high change-impact** when they landed?

That is causal-flavoured evidence, not just spatial overlap. Refinements: ignore
whitespace/format-only and comment/blank lines; skip fixes that only add lines
(no inducer to find); cap blame to a sane window.

## 11. The validation layer (the scientific core)

**Hypothesis:** change-impact predicts future bug-fix locations, and adds signal
**beyond** size/churn baselines.

Guard against the obvious confound up front: big, busy files have more bugs
*trivially*. So the test is not "does impact correlate with bugs" (it will) but
"does impact **beat and augment** LOC + churn + commit-count".

Unit of analysis: **file** (robust; also run at **function** where history allows).
Use a **temporal split** to avoid leakage: measure impact/ signals over `[start, T]`,
count bugs over `(T, end]`.

Analyses, per repo and pooled across a corpus:

1. **Ranking.** Rank files by cumulative/peak change-impact. Compute **precision@k**,
   **recall@k**, **AUC** against "was a bug-fix (or SZZ-induced) site". Compare
   against churn-ranked, hotspot-ranked, and random. Impact earns its keep only if it
   beats churn/hotspot, or complements them.
2. **Correlation.** Spearman(impact, bug_count); then **partial** Spearman
   controlling for LOC and churn — the honest number.
3. **Incremental value.** Negative-binomial / logistic regression
   `bugs ~ log_LOC + churn + commits (+ change_impact)`; likelihood-ratio test on
   adding `change_impact`. A significant positive coefficient = impact carries
   information the baselines don't.
4. **Temporal precedence.** Lead-lag: does an impact **spike** on a file precede a
   fix on that file within N commits/days? Cross-correlation per file, aggregated.
5. **SZZ direction.** Distribution of change-impact for inducing vs non-inducing
   commits (Mann-Whitney); do fixes trace back to high-impact changes?

Outputs (`analyze.py`): a per-repo `report.md` (headline AUC/lift table, the
regression LR result, top-20 painful files with impact vs bug counts), CSVs for
re-analysis, and matplotlib PNGs (impact-vs-bugs scatter, precision@k curve,
lead-lag). All offline. Pool across repos for the external-validity claim.

## 12. Other pain measures worth deriving

Grouped by role. **(GT)** = usable as ground-truth/pain outcome; **(B)** = baseline
predictor to beat/augment; **(N)** = novel, enabled by the impact machinery.

**Classic, validated (use as baselines and cross-checks)**

- **Hotspot = CC × change-frequency** (B) — the canonical pain predictor; primary rival.
- **Temporal/change coupling** (B, GT-ish) — co-change graph; rising coupling between
  files that "shouldn't" know each other = erosion. Also a pain *outcome*.
- **Code churn** (B) and **relative churn** (churn ÷ LOC) — strong defect predictors.
- **Ownership / minor contributors / truck factor** (B) — low ownership → more defects.
- **Fix density & revert rate** (GT) — direct firefighting signal.
- **Complexity/erosion trend per file** (B) — CC or WMC climbing over time; your
  existing erosion metric, applied per file across history.

**Novel measures your history + impact engine enable**

- **Impact concentration (Gini) over time** (N) — is change-impact concentrating into
  fewer files (a growing god-file at repo scale)? A rising Gini is systemic erosion —
  the PetClinic thesis measured on any real repo.
- **Settle time / impact half-life** (N) — after a high-impact change to a unit, how
  many commits/days until it's touched again? Short settle = the change thrashed
  rather than resolved (generalises `reedit_rate` temporally).
- **Fix-attracts-fix clustering** (N) — do bug-fixes recur on the same file in bursts?
  Repeated-fix files are the acute pain; test whether high prior impact predicts
  entering a fix-cluster.
- **Realized vs structural blast radius** (N) — does a commit's structural
  `files_changed`/impact predict the set of files that later **co-change or get fixed**
  with it? Turns blast radius from a static count into a validated forecast.
- **Impact-to-fix ratio** (N) — share of a file's total change-impact spent on
  bug-fixes vs features; high = expensive just to keep alive.
- **Diff scattering / entropy** (N) — how scattered a change is across a file (many
  small hunks vs one block); scattered edits signal poor cohesion / cross-cutting
  concerns. Language-agnostic (hunk geometry only).

## 13. Confounds & gotchas (design them in)

- **Generated/vendored code dominates churn.** Config ignore-globs
  (`node_modules`, `dist`, `vendor`, `*.min.js`, `migrations`, generated dirs) or it
  drowns the signal. Ship sensible defaults per ecosystem.
- **Merges.** `--first-parent` (or skip merges) to avoid double counting.
- **Renames/moves.** Use `-M` rename records to stitch a stable `file_id`; don't rely
  on `--follow` (slow, per-file). Per-file aggregation must survive moves or history
  looks artificially short.
- **Squash-merge repos.** File history is coarse and bug-linkage weaker; detect and
  warn (many single-parent commits touching many files, PR-style messages).
- **SZZ is approximate.** `blame` points at last modifier, not true inducer; mitigate
  with `-w -C`, ignore comment/blank/format-only lines, and report it as approximate.
- **Bug-keyword precision varies by project.** Make the regex configurable; always
  report reverts and issue-refs separately from keyword-only matches.
- **Language container semantics differ.** The plugin owns the container rule; default
  to file-scope for free functions so WMC_other stays defined.
- **Size/churn confound.** Never report impact-vs-bugs without the LOC/churn controls
  from §11. This is the difference between a real result and re-discovering that big
  files have bugs.

## 14. CLI & config

```bash
# Measure (resumable; leave it running):
surveyor scan  /path/to/repo --db repo.db --since 2019-01-01 \
    --languages java,cs,ts,js,py --ignore 'node_modules/**,dist/**' --resume

# Validate against the mined bug ground-truth:
surveyor analyze repo.db --split-at 2024-01-01 --unit file --out report/

# Optional richer ground-truth if pre-downloaded before going offline:
surveyor analyze repo.db --issues issues.json --out report/
```

`config.yaml` (per project): ignore globs, test-path patterns, bug/revert keyword
regexes, language plugin overrides, CC threshold, Jaccard threshold, analysis window.

## 15. Dependencies (all pip-installable, all offline once installed)

- **Core:** Python 3.10+, `git` on PATH, **lizard** (multi-language CC + line ranges).
- **Stats/report:** `numpy`, `scipy` (Spearman/partial/Mann-Whitney/NB regression via
  `statsmodels`), `matplotlib`. `pandas` optional.
- **Optional fidelity:** `tree_sitter` + prebuilt grammars for languages where lizard
  is weak (TSX/JSX). Bundle the compiled grammars so it stays offline.
- **Optional duplication:** `jscpd` (multi-language clones) if you want a duplication-
  trend signal; degrade gracefully if absent.

SQLite is stdlib. No service, no network, no API keys.

## 16. Build order (each milestone is independently useful offline)

1. **M1 — Tier-1 only.** `repo.py` + `signals.py` + `store.py` + a churn/frequency/
   hotspot/coupling report. Runs on *any* repo day one, no plugins. Immediate value.
2. **M2 — bug ground-truth.** `bugs.py` message/revert/issue-ref detection + fix-
   density. Now you can rank files by pain.
3. **M3 — change-impact.** `plugins/lizard_plugin.py` + `units.py` + `impact.py`.
   The headline measure, multi-language via lizard.
4. **M4 — validation.** `analyze.py` §11 (AUC/precision@k, partial corr, regression,
   lead-lag). The scientific payoff.
5. **M5 — SZZ.** blame-based inducer linkage + the direction test.
6. **M6 — extras.** Gini/settle-time/scattering novel measures; tree-sitter plugins;
   corpus pooling across many repos.

Do M1–M2 before you lose internet (pip installs, pre-clone a corpus, optionally
pre-export issues). M3+ is pure offline CPU.

## 17. Where this sits in the research program

This is the **external-validity** stage: it turns "change-impact correlates with our
AI's cost in our harness" into "change-impact predicts independently-recorded pain in
real code it never saw". It costs no tokens, needs no network, and — crucially —
answers the standing critique that the score merely re-describes the additive-vs-
mutative difference. If it predicts bugs beyond size/churn on third-party repos, the
measure stands on its own. The health-check tool falls out for free.
