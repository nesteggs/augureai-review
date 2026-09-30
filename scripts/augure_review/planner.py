"""Deterministic coverage planning.

Every changed file, hunk, or hunk part receives exactly one owning chunk. Paths,
test and documentation names, and cross-references group related changes; they
never decide whether a change receives coverage.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ReviewFailure
from .gitdiff import FileChange

TEST_DIRECTORIES = {"test", "tests", "__tests__", "spec", "specs", "testdata", "fixtures", "e2e"}
TEST_NAME = re.compile(
    r"(^test_|_test\.|\.test\.|\.spec\.|_spec\.|Tests?\.[A-Za-z]+$|^tests?\.[A-Za-z]+$)"
)
DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".adoc", ".txt"}
GENERIC_STEMS = {
    "index", "main", "mod", "lib", "init", "__init__", "utils", "util", "types", "test",
    "tests", "readme", "config", "setup", "common", "helpers", "constants", "package",
    "styles", "style", "app", "base", "core", "error", "errors", "model", "models",
}

DEFAULT_LAYER_PATTERNS = {
    "data": ["*migrations/*", "*migrate/*", "*.sql", "*/db/*", "*schema*"],
    "services-and-workers": ["*worker*", "*jobs/*", "*services/*", "*queue*", "*scheduler*"],
    "api": ["*/api/*", "api/*", "*routes*", "*handlers/*", "*controllers/*", "*openapi*"],
    "web": ["web/*", "*/web/*", "frontend/*", "*.tsx", "*.jsx", "*.vue", "*.svelte", "*.css", "*.scss", "*.html"],
    "mobile": ["ios/*", "android/*", "mobile/*", "*/mobile/*", "*.swift", "*.dart"],
    "shared-contracts": ["shared/*", "*/shared/*", "common/*", "packages/*", "*.proto", "*/types/*"],
    "infrastructure": [
        ".github/*", "*Dockerfile*", "*docker-compose*", "*compose.y*ml", "*.tf", "helm/*", "*/helm/*",
        "k8s/*", "*/k8s/*", "deploy/*", "*/deploy/*",
    ],
}


# Per-unit prompt framing: the assigned-unit listing line and the untrusted
# delimiters around the unit's diff, excluding the path itself.
UNIT_FRAME_BYTES = 320


def unit_frame(change: FileChange) -> int:
    return UNIT_FRAME_BYTES + 2 * len(change.path.encode()) + len((change.old_path or "").encode())


@dataclass
class Unit:
    id: str
    path: str
    description: str
    hunks: list[int]
    part: tuple[int, int] | None
    text: str
    cost: int
    role: str

    def summary(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "description": self.description,
            "hunks": self.hunks,
            "part": list(self.part) if self.part else None,
            "bytes": self.cost,
            "role": self.role,
        }


@dataclass
class Chunk:
    id: str
    units: list[Unit] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    @property
    def cost(self) -> int:
        return sum(unit.cost for unit in self.units)

    @property
    def paths(self) -> list[str]:
        return sorted({unit.path for unit in self.units})

    def summary(self) -> dict:
        return {
            "id": self.id,
            "labels": self.labels,
            "bytes": self.cost,
            "paths": self.paths,
            "units": [unit.summary() for unit in self.units],
        }


@dataclass
class Plan:
    chunks: list[Chunk]
    relationships: list[dict]
    diff_budget: int

    @property
    def units(self) -> dict[str, Unit]:
        return {unit.id: unit for chunk in self.chunks for unit in chunk.units}

    def owner(self) -> dict[str, str]:
        return {unit.id: chunk.id for chunk in self.chunks for unit in chunk.units}

    def chunks_for_path(self, path: str) -> list[str]:
        return [chunk.id for chunk in self.chunks if path in chunk.paths]

    def summary(self) -> dict:
        return {
            "diff_budget_bytes": self.diff_budget,
            "chunks": [chunk.summary() for chunk in self.chunks],
            "relationships": self.relationships,
        }


def _size(text: str) -> int:
    return len(text.encode())


def is_test(path: str) -> bool:
    parts = path.split("/")
    return bool(TEST_DIRECTORIES.intersection(parts[:-1])) or bool(TEST_NAME.search(parts[-1]))


def is_doc(path: str) -> bool:
    return posixpath.splitext(path)[1].lower() in DOC_EXTENSIONS


def stem(path: str) -> str:
    name = posixpath.basename(path).split(".", 1)[0]
    name = re.sub(r"^test_", "", name)
    name = re.sub(r"(_test|_spec|Tests?)$", "", name)
    return name.lower()


def role(path: str) -> str:
    if is_test(path):
        return "test"
    if is_doc(path):
        return "doc"
    return "source"


def _anchor(path: str, sources: dict[str, list[str]]) -> str:
    """Attach tests and documentation to the changed source they describe."""
    if role(path) == "source":
        return path
    candidates = sources.get(stem(path), [])
    if not candidates:
        return path
    top = path.split("/", 1)[0]
    return min(candidates, key=lambda source: (source.split("/", 1)[0] != top, len(source), source))


def _area(directory: str) -> str:
    return "/".join(directory.split("/")[:2])


def _truncate_long_lines(lines: list[str], limit: int) -> list[str]:
    result = []
    for line in lines:
        if _size(line) > limit:
            kept = line.encode()[:limit].decode(errors="ignore")
            line = f"{kept}\n[line truncated at {limit} bytes; read the full line from the head commit]\n"
        result.append(line)
    return result


def file_units(change: FileChange, max_bytes: int, context_bytes: int) -> list[tuple[str, list[int], tuple[int, int] | None, str]]:
    """Split one file's patch into units that fit the per-chunk diff budget."""
    limit = max_bytes - context_bytes
    header = change.header
    whole = change.patch
    if _size(whole) <= limit or not change.hunks:
        return [("whole file", [hunk.index for hunk in change.hunks], None, whole)]

    pieces: list[tuple[str, list[int], tuple[int, int] | None, str]] = []
    pending: list = []

    def flush() -> None:
        if pending:
            indexes = [hunk.index for hunk in pending]
            label = f"hunk {indexes[0]}" if len(indexes) == 1 else f"hunks {indexes[0]}-{indexes[-1]}"
            pieces.append((label, indexes, None, header + "".join(hunk.text for hunk in pending)))
            pending.clear()

    for hunk in change.hunks:
        if _size(header) + _size(hunk.text) > limit:
            flush()
            body = _truncate_long_lines(hunk.lines[1:], max(256, limit // 4))
            note_room = 160 + _size(hunk.lines[0])
            parts: list[list[str]] = [[]]
            used = _size(header) + note_room
            for line in body:
                if parts[-1] and used + _size(line) > limit:
                    parts.append([])
                    used = _size(header) + note_room
                parts[-1].append(line)
                used += _size(line)
            for number, lines in enumerate(parts, start=1):
                note = f"[hunk {hunk.index} part {number} of {len(parts)}; neighbouring parts are reviewed separately]\n"
                text = header + hunk.lines[0] + note + "".join(lines)
                pieces.append((f"hunk {hunk.index} part {number}/{len(parts)}", [hunk.index], (number, len(parts)), text))
        elif pending and _size(header) + sum(_size(h.text) for h in pending) + _size(hunk.text) > limit:
            flush()
            pending.append(hunk)
        else:
            pending.append(hunk)
    flush()
    return pieces


def load_layer_map(path: Path | None) -> dict[str, list[str]]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewFailure("configuration", f"layer map is not valid JSON: {error}") from error
    valid = isinstance(data, dict) and all(
        isinstance(key, str) and isinstance(value, list) and all(isinstance(item, str) for item in value)
        for key, value in data.items()
    )
    if not valid:
        raise ReviewFailure("configuration", "layer map must map label names to lists of glob patterns")
    return data


def labels_for(paths: list[str], layer_map: dict[str, list[str]]) -> list[str]:
    labels = set()
    for path in paths:
        matched = {label for label, patterns in layer_map.items() if any(fnmatch.fnmatch(path, p) for p in patterns)}
        if not matched:
            matched = {
                label
                for label, patterns in DEFAULT_LAYER_PATTERNS.items()
                if any(fnmatch.fnmatch(path, p) for p in patterns)
            }
        labels.update(matched)
    return sorted(labels)


def _changed_text(chunk: Chunk) -> str:
    return "\n".join(
        line[1:]
        for unit in chunk.units
        for line in unit.text.split("\n")
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ).lower()


def relationships(chunks: list[Chunk]) -> list[dict]:
    reasons: dict[tuple[str, str], list[str]] = {}

    def relate(first: str, second: str, reason: str) -> None:
        if first == second:
            return
        key = tuple(sorted((first, second)))
        entry = reasons.setdefault(key, [])
        if reason not in entry and len(entry) < 5:
            entry.append(reason)

    by_path: dict[str, list[str]] = {}
    by_directory: dict[str, list[str]] = {}
    for chunk in chunks:
        for path in chunk.paths:
            by_path.setdefault(path, []).append(chunk.id)
            by_directory.setdefault(posixpath.dirname(path), []).append(chunk.id)
    for path, owners in by_path.items():
        for other in owners[1:]:
            relate(owners[0], other, f"{path} is split across chunks")
    for directory, owners in by_directory.items():
        unique = sorted(set(owners))
        for other in unique[1:]:
            relate(unique[0], other, f"shared directory {directory or '.'}")

    texts = {chunk.id: _changed_text(chunk) for chunk in chunks}
    for chunk in chunks:
        stems = sorted(
            {stem(path) for path in chunk.paths if role(path) == "source"}
            - GENERIC_STEMS
        )
        stems = [value for value in stems if len(value) >= 4]
        if not stems:
            continue
        pattern = re.compile(r"\b(" + "|".join(re.escape(value) for value in stems) + r")\b")
        for other in chunks:
            if other.id == chunk.id:
                continue
            match = pattern.search(texts[other.id])
            if match:
                relate(chunk.id, other.id, f"{other.id} references {match.group(1)} changed in {chunk.id}")

    return [{"chunks": list(key), "reasons": value} for key, value in sorted(reasons.items())]


def build_plan(
    changes: list[FileChange],
    diff_budget: int,
    file_context_bytes: dict[str, int],
    layer_map: dict[str, list[str]],
) -> Plan:
    if diff_budget < 4_000:
        raise ReviewFailure("budget", "the chunk budget leaves too little room for diff content")

    sources: dict[str, list[str]] = {}
    for change in changes:
        if role(change.path) == "source":
            sources.setdefault(stem(change.path), []).append(change.path)

    ordered = []
    for sequence, change in enumerate(changes):
        anchor = _anchor(change.path, sources)
        path_role = role(change.path)
        rank = {"source": 0, "test": 1, "doc": 2}[path_role]
        key = (posixpath.dirname(anchor), anchor, rank, change.path, sequence)
        context = min(file_context_bytes.get(change.path, 0), diff_budget // 4) + unit_frame(change)
        for piece_index, (description, hunks, part, text) in enumerate(file_units(change, diff_budget, context)):
            ordered.append((key + (piece_index,), change.path, description, hunks, part, text, context, path_role))
    ordered.sort(key=lambda item: item[0])

    chunks: list[Chunk] = []
    current_area = None
    for number, (key, path, description, hunks, part, text, context, path_role) in enumerate(ordered, start=1):
        unit = Unit(
            id=f"U{number:03d}",
            path=path,
            description=description,
            hunks=hunks,
            part=part,
            text=text,
            cost=_size(text) + context,
            role=path_role,
        )
        area = _area(key[0])
        if chunks and chunks[-1].units:
            current = chunks[-1]
            over_budget = current.cost + unit.cost > diff_budget
            new_area = area != current_area and current.cost >= diff_budget // 2
            if over_budget or new_area:
                chunks.append(Chunk(id=""))
        else:
            chunks.append(Chunk(id=""))
        chunks[-1].units.append(unit)
        current_area = area

    for number, chunk in enumerate(chunks, start=1):
        chunk.id = f"C{number:02d}"
        chunk.labels = labels_for(chunk.paths, layer_map)

    plan = Plan(chunks=chunks, relationships=relationships(chunks), diff_budget=diff_budget)
    verify_plan(changes, plan)
    return plan


def verify_plan(changes: list[FileChange], plan: Plan) -> None:
    """Prove that every changed file and hunk has exactly one owner."""
    seen: set[str] = set()
    hunk_parts: dict[str, dict[int, set[int]]] = {}
    covered_files: set[str] = set()
    for chunk in plan.chunks:
        if chunk.cost > plan.diff_budget:
            raise ReviewFailure("budget", f"{chunk.id} exceeds the diff budget ({chunk.cost} > {plan.diff_budget} bytes)")
        for unit in chunk.units:
            if unit.id in seen:
                raise ReviewFailure("coverage", f"{unit.id} is assigned to more than one chunk")
            seen.add(unit.id)
            covered_files.add(unit.path)
            for hunk in unit.hunks:
                parts = hunk_parts.setdefault(unit.path, {}).setdefault(hunk, set())
                parts.add(unit.part[0] if unit.part else 0)

    for change in changes:
        if change.path not in covered_files:
            raise ReviewFailure("coverage", f"{change.path} has no owning chunk")
        owned = hunk_parts.get(change.path, {})
        for hunk in change.hunks:
            parts = owned.get(hunk.index)
            if not parts:
                raise ReviewFailure("coverage", f"{change.path} hunk {hunk.index} has no owning chunk")
            if parts != {0}:
                expected = next(
                    unit.part[1]
                    for unit in plan.units.values()
                    if unit.path == change.path and hunk.index in unit.hunks and unit.part
                )
                if parts != set(range(1, expected + 1)):
                    raise ReviewFailure("coverage", f"{change.path} hunk {hunk.index} has missing parts")
