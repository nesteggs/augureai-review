"""Frozen commit range and changed-file inventory."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ReviewFailure

# Diff drivers and textconv filters come from repository-controlled
# configuration, so they are disabled for every diff the orchestrator reads.
DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "--find-renames")
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    index: int
    header: str
    lines: list[str]
    right_lines: set[int] = field(default_factory=set)
    left_lines: set[int] = field(default_factory=set)

    @property
    def text(self) -> str:
        return "".join(self.lines)


@dataclass
class FileChange:
    path: str
    old_path: str | None
    status: str
    additions: int | None
    deletions: int | None
    binary: bool
    header: str
    hunks: list[Hunk]

    @property
    def patch(self) -> str:
        return self.header + "".join(hunk.text for hunk in self.hunks)

    @property
    def right_lines(self) -> set[int]:
        return set().union(*(hunk.right_lines for hunk in self.hunks)) if self.hunks else set()

    @property
    def left_lines(self) -> set[int]:
        return set().union(*(hunk.left_lines for hunk in self.hunks)) if self.hunks else set()

    def summary(self) -> dict:
        return {
            "path": self.path,
            "old_path": self.old_path,
            "status": self.status,
            "additions": self.additions,
            "deletions": self.deletions,
            "binary": self.binary,
            "hunks": len(self.hunks),
            "patch_bytes": len(self.patch.encode()),
        }


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise ReviewFailure("git", f"git {args[0]} failed: {message}")
    return result.stdout


def resolve_commit(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()


def merge_base(repo: Path, base: str, head: str) -> str:
    return git(repo, "merge-base", base, head).decode().strip()


def _split_z(raw: bytes) -> list[str]:
    parts = raw.decode(errors="replace").split("\0")
    return parts[:-1] if parts and parts[-1] == "" else parts


def _name_status(repo: Path, base: str, head: str) -> list[tuple[str, str | None, str]]:
    fields = _split_z(git(repo, "diff", *DIFF_FLAGS, "--name-status", "-z", base, head))
    entries = []
    index = 0
    while index < len(fields):
        status = fields[index]
        if status[:1] in ("R", "C"):
            entries.append((status[:1], fields[index + 1], fields[index + 2]))
            index += 3
        else:
            entries.append((status[:1], None, fields[index + 1]))
            index += 2
    return entries


def _numstat(repo: Path, base: str, head: str) -> list[tuple[int | None, int | None]]:
    fields = _split_z(git(repo, "diff", *DIFF_FLAGS, "--numstat", "-z", base, head))
    counts = []
    index = 0
    while index < len(fields):
        added, deleted, path = fields[index].split("\t", 2)
        # Renames and copies put an empty path here, then old and new paths.
        index += 3 if path == "" else 1
        counts.append((None if added == "-" else int(added), None if deleted == "-" else int(deleted)))
    return counts


def _lines(text: str) -> list[str]:
    # str.splitlines also splits on carriage returns and form feeds, which are
    # ordinary content inside a diff line.
    lines = text.split("\n")
    last = lines.pop()
    return [line + "\n" for line in lines] + ([last] if last else [])


def _split_patch(patch: str) -> list[str]:
    sections: list[str] = []
    for line in _lines(patch):
        if line.startswith("diff --git "):
            sections.append(line)
        elif sections:
            sections[-1] += line
    return sections


def _pair_sections(names: list[tuple[str, str | None, str]], sections: list[str]) -> list[str]:
    """Join the deletion and addition patches git prints for one type change."""
    paired = []
    index = 0
    for status, _, _ in names:
        if index >= len(sections):
            break
        section = sections[index]
        index += 1
        if status == "T" and index < len(sections) and sections[index].split("\n", 1)[0] == section.split("\n", 1)[0]:
            section += sections[index]
            index += 1
        paired.append(section)
    return paired + sections[index:]


def parse_hunks(section: str) -> tuple[str, list[Hunk]]:
    header_lines: list[str] = []
    hunks: list[Hunk] = []
    old_line = new_line = 0
    in_header = True
    for line in _lines(section):
        match = HUNK_HEADER.match(line)
        if match:
            in_header = False
            old_line, new_line = int(match.group(1)), int(match.group(3))
            hunks.append(Hunk(index=len(hunks) + 1, header=line.rstrip("\n"), lines=[line]))
            continue
        # A type change has a second file header after the first patch's hunks.
        if in_header or line.startswith("diff --git "):
            in_header = True
            header_lines.append(line)
            continue
        hunk = hunks[-1]
        hunk.lines.append(line)
        marker = line[:1]
        if marker == " ":
            hunk.right_lines.add(new_line)
            old_line += 1
            new_line += 1
        elif marker == "-":
            hunk.left_lines.add(old_line)
            old_line += 1
        elif marker == "+":
            hunk.right_lines.add(new_line)
            new_line += 1
    return "".join(header_lines), hunks


def inventory(repo: Path, base: str, head: str) -> list[FileChange]:
    names = _name_status(repo, base, head)
    counts = _numstat(repo, base, head)
    patch = git(repo, "diff", *DIFF_FLAGS, "--unified=3", base, head).decode(errors="replace")
    sections = _pair_sections(names, _split_patch(patch))
    if not len(names) == len(counts) == len(sections):
        raise ReviewFailure(
            "git",
            f"diff inventory is inconsistent: {len(names)} names, {len(counts)} counts, {len(sections)} patches",
        )

    changes = []
    for (status, old_path, path), (additions, deletions), section in zip(names, counts, sections):
        header, hunks = parse_hunks(section)
        changes.append(
            FileChange(
                path=path,
                old_path=old_path,
                status=status,
                additions=additions,
                deletions=deletions,
                binary=additions is None,
                header=header,
                hunks=hunks,
            )
        )
    return changes
