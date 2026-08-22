# Surveyor

Language-agnostic **change-impact** + pain-signal harness. Offline. No AI.

Point it at any git repository. It walks the commit history, computes the
change-impact score per commit / file / function, mines a bug ground-truth from
the repo itself, and tests whether change-impact **predicts** where the pain lands
— beyond trivial size/churn baselines.

Full design rationale: `docs/CHANGE_IMPACT_HARNESS_DESIGN.md`.

## Why

The PetClinic-Evolve experiment
(<https://github.com/officefloor/spring-petclinic-rest-long-degradation-test>)
validated the change-impact score by correlating it with an AI agent's own effort,
on two hand-built codebases. This tool is the **external-validity** stage: does the
same score predict
independently-recorded pain (bug fixes, reverts) in real third-party repos it has
never seen? The score needs only per-function complexity, line ranges and
container membership, so it is **not** Java-specific — plugins handle each
language.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                        # installs the `surveyor` command (only lizard is required)
# or, without installing the package:  pip install -r requirements.txt
```

Requires `git` on PATH. Everything runs offline once installed. After `pip install
-e .` you can run `surveyor ...` directly; otherwise use `python -m surveyor ...`.

## Use

```bash
# 1. Measure — walk history into a SQLite db. Resumable; leave it running.
python -m surveyor scan /path/to/repo --db repo.db [--since 2019-01-01] [--max-commits N]

# 2. Validate — test change-impact vs the mined bug ground-truth.
python -m surveyor analyze repo.db --out report/                 # concurrent association
python -m surveyor analyze repo.db --out report/ --split-at 2023-01-01   # leakage-free prediction
```

`scan` is **incremental and resumable**: re-running skips commits already in the
db, and a parse cache keyed by git blob OID means a file version is parsed once no
matter how many commits carry it. Kill it any time; re-run with the same `--db` to
continue. Ideal for an unattended multi-hour run.

`analyze` writes `report.md`, `files.csv`, and `coupling.csv`. Use `--split-at`
for the honest predictive test: predictors are measured **before** the date, bug
outcomes **after**, so there is no leakage. Without it you get a concurrent
association check (clearly caveated in the report).

## What the report tells you

- **Spearman + AUC** of change-impact vs bug-fix count, next to churn, commit
  frequency, and the hotspot (cc×freq) baselines.
- **Partial Spearman(impact, fixes | churn)** — the honest number. Change-impact
  earns its keep only if this stays clearly positive, i.e. it adds signal *beyond*
  raw churn.
- **Precision@k** — of the top-k most-impactful files, how many are bug sites,
  vs churn-ranked and vs base rate.
- **Top painful files** and **temporally-coupled file pairs** (hidden dependencies).

## Languages

The default plugin wraps **lizard**, covering Java, C#, JS/TS, Python, C/C++, Go,
Kotlin, Swift, Ruby, PHP, Rust and more. "Container" (the cohesion scope for the
surrounding-complexity weight) is the enclosing class where lizard qualifies names
(`Class::method`), else the file. Add a plugin under `surveyor/plugins/` and
`register()` it for an extension only where lizard is weak (e.g. TSX).

## Status

Implemented: git access + resumable scan, lizard plugin, change-impact,
Tier-1 signals (churn, frequency, ownership, temporal coupling), bug-message
classification (fix/revert/issue-ref), SQLite store + parse cache, and the
validation report (Spearman/partial/AUC/precision@k). Pure Python; only `lizard`
is required.

Deliberately light for round one (refine against real repos later):

- **Function renames** use exact-name matching plus a single greedy token-Jaccard
  pass; cross-file moves are not chased.
- **Container** defaults to class-or-file; finer per-language rules can come later.
- **SZZ** blame-based inducing-commit linkage (`repo.blame_lines` exists) is not
  yet wired into scan/analyze — that is the next milestone, turning "high-impact
  files have bugs" into "high-impact commits *induce* the bugs".
- Stats are pure-Python; a `numpy/scipy/matplotlib` pass (regression LR test,
  plots) is optional and not required to run.

## Layout

```
surveyor/
  config.py      languages, ignore globs, bug/revert keywords, thresholds
  repo.py        git plumbing: log / -U0 diff parse / cat-file --batch / blame
  plugins/       LanguagePlugin interface + lizard default
  impact.py      change-impact per file (cost, WMC_other, light rename match)
  bugs.py        commit-message classification (fix / revert / issue-ref)
  store.py       SQLite: parse cache, per-commit/-file/-unit rows, resume cursor
                 (Tier-1 signals — churn, frequency, ownership, coupling — are
                  derived in analyze from these raw rows, no separate module)
  stats.py       pure-Python Spearman / partial / AUC / precision@k
  analyze.py     validation report (report.md, files.csv, coupling.csv)
  __main__.py    CLI: scan / analyze
```
