"""Change-impact computation for one file within one commit.

cost(unit)    = max(WMC_other, 1) * CC * max(1, dlines)
  WMC_other   = sum CC of the OTHER units sharing the unit's container
mutation_cost = sum cost over modified/renamed existing units
godclass_cost = sum cost over new units (new files + new methods)

The commit-level composite multiplies (mutation+godclass) by files_changed; that
spread term is applied by the caller, which knows the whole commit.

Rename handling is intentionally LIGHT for round one: match after-vs-before units
by exact name first, then a single greedy token-set Jaccard pass over the leftovers.
Cross-file moves are not chased. Refine against real exceptions later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .plugins.base import Unit

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class UnitImpact:
    name: str
    container: str
    cc: int
    dlines: int
    wmc_other: int
    cost: int
    kind: str          # "mutation" | "godclass" (new) | "rename"


@dataclass
class FileImpact:
    mutation_cost: int = 0
    godclass_cost: int = 0
    mut_fns: int = 0
    new_fns: int = 0
    renames: int = 0
    units: list[UnitImpact] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.units is None:
            self.units = []


def _line_set(ranges: list[tuple[int, int]]) -> set[int]:
    out: set[int] = set()
    for start, count in ranges:
        out.update(range(start, start + count))
    return out


def _overlap(u: Unit, lines: set[int]) -> int:
    if not lines:
        return 0
    return sum(1 for ln in range(u.start_line, u.end_line + 1) if ln in lines)


def _tokens(src_lines: list[str], u: Unit) -> set[str]:
    body = "\n".join(src_lines[max(0, u.start_line - 1): u.end_line])
    return set(_TOKEN.findall(body))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _wmc_other(u: Unit, by_container: dict[str, int]) -> int:
    total = by_container.get(u.container, 0)
    return max(total - u.cc, 0)


def compute_file_impact(
    before: list[Unit], after: list[Unit],
    before_src: list[str], after_src: list[str],
    added: list[tuple[int, int]], removed: list[tuple[int, int]],
    rename_jaccard: float,
) -> FileImpact:
    added_lines = _line_set(added)
    removed_lines = _line_set(removed)

    # WMC context is the surrounding complexity in the resulting (after) file.
    by_container: dict[str, int] = {}
    for u in after:
        by_container[u.container] = by_container.get(u.container, 0) + u.cc

    before_by_name = {u.name: u for u in before}
    after_names = {u.name for u in after}

    # Which after-units the change actually touched, and by how much.
    touched_after = [(u, _overlap(u, added_lines)) for u in after]
    touched_after = [(u, n) for u, n in touched_after if n > 0]

    matched_before: set[str] = set()
    fi = FileImpact()

    for u, add_n in touched_after:
        prior = before_by_name.get(u.name)
        kind = "mutation"
        del_n = 0
        if prior is not None:
            matched_before.add(prior.name)
            del_n = _overlap(prior, removed_lines)
        else:
            # light rename match: best Jaccard among removed before-units (those
            # gone from `after`), touched by this diff and not already claimed.
            cand = _best_rename(u, before, after_names, matched_before,
                                removed_lines, before_src, after_src, rename_jaccard)
            if cand is not None:
                matched_before.add(cand.name)
                del_n = _overlap(cand, removed_lines)
                kind = "rename"
            else:
                kind = "godclass"  # genuinely new unit

        dlines = max(1, add_n + del_n)
        wmc = _wmc_other(u, by_container)
        cost = max(wmc, 1) * u.cc * dlines
        fi.units.append(UnitImpact(u.name, u.container, u.cc, dlines, wmc, cost, kind))
        if kind == "godclass":
            fi.godclass_cost += cost
            fi.new_fns += 1
        else:
            fi.mutation_cost += cost
            fi.mut_fns += 1
            if kind == "rename":
                fi.renames += 1

    return fi


def _best_rename(u: Unit, before: list[Unit], after_names: set[str],
                 matched: set[str], removed_lines: set[int],
                 before_src, after_src, threshold: float) -> Unit | None:
    u_tokens = _tokens(after_src, u)
    best, best_j = None, threshold
    for b in before:
        if b.name in matched or b.name in after_names:
            continue  # already claimed, or still exists (not a rename source)
        if _overlap(b, removed_lines) == 0:
            continue
        j = _jaccard(u_tokens, _tokens(before_src, b))
        if j >= best_j:
            best, best_j = b, j
    return best
