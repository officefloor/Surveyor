"""Git access layer — subprocess only, no network, no working-tree checkout.

Reads objects directly (log / diff / cat-file --batch / blame), so it is safe
against a bare or read-only clone and never mutates the repo.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from .config import EMPTY_TREE

US = "\x01"  # field separator inside git --format


@dataclass
class Commit:
    sha: str
    parents: list[str]
    author: str
    email: str
    ts: int          # author time, unix seconds
    subject: str
    body: str = ""

    @property
    def message(self) -> str:
        return (self.subject + "\n" + self.body).strip()

    @property
    def parent(self) -> str:
        """First parent, or the empty tree for a root commit."""
        return self.parents[0] if self.parents else EMPTY_TREE

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int


@dataclass
class FileDiff:
    status: str = "M"           # A / M / D / R
    old_path: str | None = None
    new_path: str | None = None
    is_binary: bool = False
    added: list[tuple[int, int]] = field(default_factory=list)    # (start, count) in NEW file
    removed: list[tuple[int, int]] = field(default_factory=list)  # (start, count) in OLD file
    add_total: int = 0
    del_total: int = 0

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


class GitRepo:
    def __init__(self, path: str):
        self.path = path
        self._batch: subprocess.Popen | None = None

    # ---- plumbing ---------------------------------------------------------
    def _run(self, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", self.path, *args],
            check=True, capture_output=True,
        )
        return out.stdout.decode("utf-8", errors="replace")

    def commits(self, since: str | None = None, until: str = "HEAD",
                max_count: int | None = None) -> list[Commit]:
        """Mainline commits oldest-first (--first-parent, merges kept and diffed
        against their first parent so a merge represents the branch it brought in)."""
        # NUL-terminated records (-z) so multi-line commit bodies don't break parsing.
        fmt = US.join(["%H", "%P", "%an", "%ae", "%at", "%s", "%b"])
        args = ["log", "--first-parent", "--reverse", "-z", f"--format={fmt}"]
        if max_count:
            args += [f"--max-count={max_count}"]
        if since:
            args += [f"--since={since}"]
        args += [until]
        out = self._run(*args)
        commits: list[Commit] = []
        for rec in out.split("\0"):
            if not rec.strip():
                continue
            sha, parents, an, ae, at, subj, body = (rec.split(US) + [""] * 7)[:7]
            commits.append(Commit(
                sha=sha,
                parents=parents.split() if parents else [],
                author=an, email=ae, ts=int(at or 0), subject=subj, body=body,
            ))
        return commits

    def diff(self, old: str, new: str) -> list[FileDiff]:
        """Parse a -U0 rename-aware diff into per-file line ranges + totals."""
        text = self._run(
            "diff", "-U0", "-M", "--no-color", "--no-ext-diff",
            "--find-renames", old, new,
        )
        return parse_diff(text)

    # ---- blob streaming via a persistent cat-file --batch -----------------
    def _ensure_batch(self) -> subprocess.Popen:
        if self._batch is None or self._batch.poll() is not None:
            self._batch = subprocess.Popen(
                ["git", "-C", self.path, "cat-file", "--batch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
        return self._batch

    def blob(self, rev: str, path: str) -> tuple[str, bytes] | None:
        """(<blob oid>, contents) of <rev>:<path>, or None if absent.

        The OID lets callers cache the parse of identical file versions once.
        """
        proc = self._ensure_batch()
        assert proc.stdin and proc.stdout
        proc.stdin.write(f"{rev}:{path}\n".encode())
        proc.stdin.flush()
        header = proc.stdout.readline().decode("utf-8", errors="replace").strip()
        if not header or header.endswith(("missing", "ambiguous")):
            return None
        parts = header.split()
        oid = parts[0]
        try:
            size = int(parts[-1])
        except ValueError:
            return None
        data = proc.stdout.read(size)
        proc.stdout.read(1)  # trailing newline
        return oid, data

    def blame_ranges(self, rev: str, path: str, ranges: list[tuple[int, int]]) -> set[str]:
        """Distinct SHAs that last touched the given line ranges of <rev>:<path>, in
        ONE blame call (SZZ inducer detection). -w -C sees through whitespace/moves.
        Returns an empty set on error."""
        largs: list[str] = []
        for start, count in ranges:
            if count > 0:
                largs += ["-L", f"{start},{start + count - 1}"]
        if not largs:
            return set()
        try:
            out = self._run("blame", "-w", "-C", "--line-porcelain",
                            *largs, rev, "--", path)
        except subprocess.CalledProcessError:
            return set()
        shas = set()
        for line in out.splitlines():
            head = line.split(" ", 1)[0]
            if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
                shas.add(head)
        return shas

    def blame_lines(self, rev: str, path: str, start: int, count: int) -> list[str]:
        """SHAs that last touched lines [start, start+count) at <rev> (SZZ input).

        Uses -w -C to see through whitespace/moves. Best-effort: returns [] on error.
        """
        if count <= 0:
            return []
        try:
            out = self._run("blame", "-w", "-C", "--line-porcelain",
                            "-L", f"{start},{start + count - 1}", rev, "--", path)
        except subprocess.CalledProcessError:
            return []
        shas = []
        for line in out.splitlines():
            head = line.split(" ", 1)[0]
            if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
                shas.append(head)
        return shas

    def tree_entries(self, rev: str) -> dict[str, tuple[str, int]]:
        """{path: (blob_oid, size_bytes)} for every file at <rev>, in one ls-tree call.
        The OID lets a caller reuse the blob parse cache; size is a file-size baseline.
        Returns {} on error."""
        try:
            out = self._run("ls-tree", "-r", "--long", rev)
        except subprocess.CalledProcessError:
            return {}
        entries: dict[str, tuple[str, int]] = {}
        for line in out.splitlines():
            if "\t" not in line:
                continue
            meta, path = line.split("\t", 1)
            parts = meta.split()
            # <mode> blob <oid> <size>\t<path>
            if len(parts) >= 4 and parts[1] == "blob":
                try:
                    entries[path] = (parts[2], int(parts[3]))
                except ValueError:
                    continue
        return entries

    def close(self) -> None:
        if self._batch and self._batch.poll() is None:
            try:
                self._batch.stdin.close()  # type: ignore[union-attr]
                self._batch.terminate()
            except Exception:
                pass


def _parse_hunk_header(line: str) -> Hunk | None:
    # @@ -old_start[,old_count] +new_start[,new_count] @@ ...
    try:
        body = line[3:line.index(" @@", 3)]
        old_part, new_part = body.split(" +")
        old_part = old_part.lstrip("-")

        def rng(s: str) -> tuple[int, int]:
            if "," in s:
                a, b = s.split(",")
                return int(a), int(b)
            return int(s), 1

        os_, oc = rng(old_part)
        ns_, nc = rng(new_part)
        return Hunk(os_, oc, ns_, nc)
    except (ValueError, IndexError):
        return None


def parse_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            cur = FileDiff()
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("new file"):
            cur.status = "A"
        elif line.startswith("deleted file"):
            cur.status = "D"
        elif line.startswith("rename from "):
            cur.status = "R"
            cur.old_path = line[len("rename from "):]
        elif line.startswith("rename to "):
            cur.new_path = line[len("rename to "):]
        elif line.startswith("copy to "):
            cur.new_path = line[len("copy to "):]
        elif line.startswith("Binary files"):
            cur.is_binary = True
        elif line.startswith("--- "):
            p = line[4:]
            if p != "/dev/null":
                cur.old_path = p[2:] if p.startswith(("a/", "b/")) else p
        elif line.startswith("+++ "):
            p = line[4:]
            if p != "/dev/null":
                cur.new_path = p[2:] if p.startswith(("a/", "b/")) else p
        elif line.startswith("@@"):
            h = _parse_hunk_header(line)
            if h:
                if h.old_count > 0:
                    cur.removed.append((h.old_start, h.old_count))
                    cur.del_total += h.old_count
                if h.new_count > 0:
                    cur.added.append((h.new_start, h.new_count))
                    cur.add_total += h.new_count
    return files
