"""Surveyor: language-agnostic change-impact + pain-signal harness.

Offline, no-AI. Reads a project's git history, computes the change-impact score
per commit, mines repository-derived pain signals, extracts a bug ground-truth
from the repo itself, and tests whether change-impact predicts the pain.

See docs/CHANGE_IMPACT_HARNESS_DESIGN.md for the full design.
"""

__version__ = "0.1.0"
