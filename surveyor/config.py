"""Configuration + language/keyword defaults.

Everything here is data the analysis is shaped by, so a run can snapshot it and
stay reproducible. All values are overridable via a YAML config file.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

# Extension -> language label. lizard picks its reader by filename, so this table
# only decides "is this a source file Surveyor should try to parse", plus the
# label used in reports. Add rows as new plugins arrive.
LANG_BY_EXT: dict[str, str] = {
    ".java": "java",
    ".cs": "csharp",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".py": "python",
    ".go": "go",
    ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".rs": "rust",
    ".scala": "scala",
    ".m": "objectivec", ".mm": "objectivec",
    ".lua": "lua",
    ".ttcn": "ttcn",
}

DEFAULT_IGNORE = [
    "**/node_modules/**", "**/dist/**", "**/build/**", "**/target/**",
    "**/out/**", "**/bin/**", "**/obj/**", "**/third_party/**",
    # vendored front-end / dependency trees (note the plural 'vendors' — a real repo
    # used it and its jQuery/Bootstrap blobs swamped the ranking with 0-fix code).
    "**/vendor/**", "**/vendors/**", "**/bower_components/**", "**/webjars/**",
    "**/.venv/**", "**/venv/**", "**/__pycache__/**", "**/.git/**",
    "**/*.min.js", "**/*.min.css", "**/*.bundle.js", "**/*.generated.*",
    "**/generated/**", "**/gen/**",
]

# Test-path heuristics (a prod file changing WITHOUT an accompanying test change is
# a risk signal; also lets us exclude tests from some aggregates).
DEFAULT_TEST_PATTERNS = [
    "**/test/**", "**/tests/**", "**/__tests__/**", "**/spec/**",
    "**/*Test.*", "**/*Tests.*", "**/*_test.*", "**/test_*.*",
    "**/*.test.*", "**/*.spec.*",
]

# Bug ground-truth keyword sets (precision-ranked; reverts + issue-refs are higher
# precision than plain keywords, and reported separately by analyze).
FIX_KEYWORDS = re.compile(
    r"\b(fix(e[ds]|ing)?|bug|bugs|defect|hotfix|regressi(on|ons)|crash(e[ds])?|"
    r"broke(n)?|npe|nullpointer|leak(s|ed|ing)?|segfault|deadlock|race\s+condition|"
    r"corrupt(ion|ed)?|wrong|incorrect|fault)\b",
    re.IGNORECASE,
)
REVERT_RE = re.compile(r"^\s*Revert\b|\brevert(s|ed|ing)?\b", re.IGNORECASE)
ISSUE_REF_RE = re.compile(r"(?:#(\d+))|(?:\b[A-Z][A-Z0-9]+-\d+\b)")

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's empty tree object


@dataclass
class Config:
    ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    test_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_TEST_PATTERNS))
    lang_by_ext: dict[str, str] = field(default_factory=lambda: dict(LANG_BY_EXT))
    cc_threshold: int = 10          # "eroded" function threshold (SlopCodeBench)
    szz_max_files: int = 60         # skip SZZ-blaming fix commits that touch more files (huge refactors)
    rename_jaccard: float = 0.6     # body token-set similarity to call a rename
    max_diff_lines: int = 200_000   # skip pathological mega-diffs (generated dumps)

    def ext(self, path: str) -> str:
        i = path.rfind(".")
        return path[i:].lower() if i >= 0 else ""

    def is_source(self, path: str) -> bool:
        return not self.is_ignored(path) and self.ext(path) in self.lang_by_ext

    def language(self, path: str) -> str | None:
        return self.lang_by_ext.get(self.ext(path))

    def is_ignored(self, path: str) -> bool:
        # Match each glob against the path, and also with a leading "**/" stripped, so a
        # pattern like "**/vendor/**" ignores a TOP-LEVEL vendor/ too. fnmatch's "*" spans
        # "/", but "**/vendor/**" still requires a parent segment before "vendor", which
        # let a repo-root vendor/ or node_modules/ slip through.
        for pat in self.ignore:
            if fnmatch.fnmatch(path, pat):
                return True
            if pat.startswith("**/") and fnmatch.fnmatch(path, pat[3:]):
                return True
        return False

    def is_test(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pat) for pat in self.test_patterns)

    @classmethod
    def load(cls, path: str | None) -> "Config":
        cfg = cls()
        if not path:
            return cfg
        import yaml  # optional; only needed when a config file is passed
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
        for key in ("cc_threshold", "rename_jaccard", "max_diff_lines", "szz_max_files"):
            if key in data:
                setattr(cfg, key, data[key])
        # ignore / test_patterns APPEND to the built-in defaults (a config adds to
        # them, never silently drops them). To start from scratch, set the matching
        # `*_replace` key instead.
        cfg.ignore = list(data["ignore_replace"]) if "ignore_replace" in data \
            else cfg.ignore + list(data.get("ignore", []))
        cfg.test_patterns = list(data["test_patterns_replace"]) if "test_patterns_replace" in data \
            else cfg.test_patterns + list(data.get("test_patterns", []))
        if "lang_by_ext" in data:
            cfg.lang_by_ext.update(data["lang_by_ext"])
        return cfg
