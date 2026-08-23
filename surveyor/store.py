"""SQLite persistence: parse cache (by blob OID), per-commit/-file/-unit results,
file identity across renames, and a resume cursor. Stdlib only.

Philosophy mirrors the Java harness: store RAW rows, derive aggregates in analyze.
"""
from __future__ import annotations

import json
import sqlite3

from .plugins.base import Unit

SCHEMA = """
CREATE TABLE IF NOT EXISTS blob_cache (oid TEXT PRIMARY KEY, units TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY, parent TEXT, author TEXT, email TEXT, ts INTEGER,
    is_merge INTEGER, is_fix INTEGER, is_revert INTEGER, has_issue_ref INTEGER,
    has_keyword INTEGER, issue_refs TEXT, files_changed INTEGER, subject TEXT,
    impact_mutation INTEGER, impact_godclass INTEGER, impact_composite INTEGER,
    mut_fns INTEGER, new_fns INTEGER, renames INTEGER
);

CREATE TABLE IF NOT EXISTS file_ids (id INTEGER PRIMARY KEY, canonical_path TEXT);
CREATE TABLE IF NOT EXISTS file_alias (path TEXT PRIMARY KEY, file_id INTEGER);

CREATE TABLE IF NOT EXISTS file_changes (
    sha TEXT, file_id INTEGER, path TEXT, lang TEXT, status TEXT,
    add_lines INTEGER, del_lines INTEGER,
    mutation_cost INTEGER, godclass_cost INTEGER, mut_fns INTEGER, new_fns INTEGER,
    is_test INTEGER, ts INTEGER, is_fix INTEGER
);
CREATE INDEX IF NOT EXISTS ix_fc_file ON file_changes(file_id);
CREATE INDEX IF NOT EXISTS ix_fc_sha  ON file_changes(sha);

CREATE TABLE IF NOT EXISTS unit_changes (
    sha TEXT, file_id INTEGER, unit TEXT, container TEXT,
    cc INTEGER, dlines INTEGER, wmc_other INTEGER, cost INTEGER, kind TEXT
);
CREATE INDEX IF NOT EXISTS ix_uc_file ON unit_changes(file_id);

CREATE TABLE IF NOT EXISTS bug_links (
    fix_sha TEXT, inducing_sha TEXT, file_id INTEGER, unit TEXT
);
CREATE INDEX IF NOT EXISTS ix_bl_ind ON bug_links(inducing_sha);
CREATE INDEX IF NOT EXISTS ix_bl_fix ON bug_links(fix_sha);

CREATE TABLE IF NOT EXISTS progress (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self._next_fid = self._max_fid() + 1

    def _max_fid(self) -> int:
        row = self.db.execute("SELECT MAX(id) FROM file_ids").fetchone()
        return row[0] if row and row[0] is not None else 0

    # ---- blob parse cache -------------------------------------------------
    def cached_units(self, oid: str) -> list[Unit] | None:
        row = self.db.execute("SELECT units FROM blob_cache WHERE oid=?", (oid,)).fetchone()
        if row is None:
            return None
        return [Unit(*u) for u in json.loads(row[0])]

    def cache_units(self, oid: str, units: list[Unit]) -> None:
        payload = json.dumps([[u.name, u.container, u.start_line, u.end_line, u.cc, u.nloc]
                              for u in units])
        self.db.execute("INSERT OR REPLACE INTO blob_cache(oid, units) VALUES(?,?)",
                        (oid, payload))

    # ---- file identity across renames ------------------------------------
    def resolve_file(self, path: str, rename_from: str | None = None) -> int:
        cur = self.db.execute
        if rename_from:
            row = cur("SELECT file_id FROM file_alias WHERE path=?", (rename_from,)).fetchone()
            if row:
                fid = row[0]
                cur("INSERT OR REPLACE INTO file_alias(path, file_id) VALUES(?,?)", (path, fid))
                cur("UPDATE file_ids SET canonical_path=? WHERE id=?", (path, fid))
                return fid
        row = cur("SELECT file_id FROM file_alias WHERE path=?", (path,)).fetchone()
        if row:
            return row[0]
        fid = self._next_fid
        self._next_fid += 1
        cur("INSERT INTO file_ids(id, canonical_path) VALUES(?,?)", (fid, path))
        cur("INSERT OR REPLACE INTO file_alias(path, file_id) VALUES(?,?)", (path, fid))
        return fid

    # ---- writes -----------------------------------------------------------
    def add_commit(self, row: dict) -> None:
        cols = ("sha", "parent", "author", "email", "ts", "is_merge", "is_fix",
                "is_revert", "has_issue_ref", "has_keyword", "issue_refs",
                "files_changed", "subject", "impact_mutation", "impact_godclass",
                "impact_composite", "mut_fns", "new_fns", "renames")
        self.db.execute(
            f"INSERT OR REPLACE INTO commits({','.join(cols)}) "
            f"VALUES({','.join('?' * len(cols))})",
            tuple(row.get(c) for c in cols),
        )

    def add_file_change(self, row: dict) -> None:
        cols = ("sha", "file_id", "path", "lang", "status", "add_lines", "del_lines",
                "mutation_cost", "godclass_cost", "mut_fns", "new_fns", "is_test",
                "ts", "is_fix")
        self.db.execute(
            f"INSERT INTO file_changes({','.join(cols)}) "
            f"VALUES({','.join('?' * len(cols))})",
            tuple(row.get(c) for c in cols),
        )

    def add_unit_change(self, row: dict) -> None:
        cols = ("sha", "file_id", "unit", "container", "cc", "dlines",
                "wmc_other", "cost", "kind")
        self.db.execute(
            f"INSERT INTO unit_changes({','.join(cols)}) "
            f"VALUES({','.join('?' * len(cols))})",
            tuple(row.get(c) for c in cols),
        )

    def add_bug_link(self, fix_sha: str, inducing_sha: str, file_id) -> None:
        self.db.execute(
            "INSERT INTO bug_links(fix_sha, inducing_sha, file_id) VALUES(?,?,?)",
            (fix_sha, inducing_sha, file_id))

    # ---- resume cursor ----------------------------------------------------
    def get_progress(self, key: str = "last_commit") -> str | None:
        row = self.db.execute("SELECT value FROM progress WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_progress(self, value: str, key: str = "last_commit") -> None:
        self.db.execute("INSERT OR REPLACE INTO progress(key, value) VALUES(?,?)", (key, value))

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()
