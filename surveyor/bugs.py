"""Bug ground-truth extraction from the repo itself (no network).

Precision-ranked: reverts (highest) > issue-closing refs > fix keywords (lowest,
reported separately by analyze). SZZ inducing-commit linkage lives in scan/analyze
using repo.blame_lines; this module only classifies commit messages.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config
from .repo import Commit


@dataclass
class BugFlags:
    is_fix: bool          # any fix signal (revert OR issue-ref OR keyword)
    is_revert: bool
    has_issue_ref: bool
    has_keyword: bool
    issue_refs: list[str]


def classify(commit: Commit) -> BugFlags:
    msg = commit.message
    is_revert = bool(config.REVERT_RE.search(msg))
    refs = ["".join(m) for m in config.ISSUE_REF_RE.findall(msg)]
    has_ref = bool(refs)
    has_kw = bool(config.FIX_KEYWORDS.search(msg))
    return BugFlags(
        is_fix=is_revert or has_ref and has_kw or has_kw,
        is_revert=is_revert,
        has_issue_ref=has_ref,
        has_keyword=has_kw,
        issue_refs=refs,
    )
