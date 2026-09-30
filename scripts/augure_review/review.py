"""Result validation, integration selection, aggregation, and verification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .gitdiff import FileChange
from .planner import Chunk, Plan
from .prompts import clip
from .schemas import BLOCKING, SEVERITIES

EVIDENCE_HUNK_LIMIT = 8_000
FINDING_FIELD_LIMIT = 3_000
PRIOR_BLOCKING_MARKERS = re.compile(r"(⛰|🧗|\bmountain\b|\bboulder\b|REQUEST_CHANGES)", re.IGNORECASE)


def severity_rank(severity: str) -> int:
    return SEVERITIES.index(severity)


def normalize_path(path: str) -> str:
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def normalize_finding(finding: dict) -> dict:
    finding = dict(finding)
    for key in ("title", "failure_scenario", "evidence", "fix"):
        finding[key] = clip(finding[key], FINDING_FIELD_LIMIT)
    finding["path"] = normalize_path(finding["path"])
    if finding["line"] is None or finding["line"] < 1:
        finding["line"] = None
        finding["side"] = None
    elif finding["side"] is None:
        finding["side"] = "RIGHT"
    return finding


def check_findings(findings: list[dict], known_paths: set[str]) -> list[str]:
    """Reject findings that cite files absent from both the head tree and the diff."""
    problems = []
    for index, finding in enumerate(findings):
        path = normalize_path(finding["path"])
        if path and path not in known_paths:
            problems.append(f"finding {index} cites a path that is neither at the head commit nor in the diff: {path}")
    return problems


def check_chunk_result(chunk: Chunk, result: dict, known_paths: set[str]) -> list[str]:
    assigned = {unit.id for unit in chunk.units}
    reviewed = set(result["units_reviewed"])
    incomplete = {entry["unit"] for entry in result["incomplete"]}
    problems = []
    unknown = sorted((reviewed | incomplete) - assigned)
    if unknown:
        problems.append(f"reports units that are not assigned to {chunk.id}: {', '.join(unknown)}")
    missing = sorted(assigned - reviewed - incomplete)
    if missing:
        problems.append(f"neither reviewed nor reported incomplete: {', '.join(missing)}")
    if result["status"] == "complete" and (incomplete or reviewed != assigned):
        problems.append("status is complete but not every assigned unit was reviewed")
    problems += check_findings(result["findings"], known_paths)
    return problems


def check_integration_result(result: dict, known_paths: set[str]) -> list[str]:
    return check_findings(result["findings"], known_paths)


class _UnionFind:
    def __init__(self, items: list[str]):
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, first: str, second: str) -> None:
        self.parent[self.find(first)] = self.find(second)


@dataclass
class IntegrationBatch:
    id: str
    chunk_ids: list[str]
    relationships: list[dict]
    referenced_paths: list[str] = field(default_factory=list)


def reported_relationships(plan: Plan, results: dict[str, dict]) -> list[dict]:
    """Relate chunks through the paths their boundary records mention."""
    relationships = []
    for chunk_id, result in results.items():
        for kind in ("interfaces_changed", "assumptions", "questions"):
            for entry in result[kind]:
                for path in entry["paths"]:
                    for owner in plan.chunks_for_path(normalize_path(path)):
                        if owner != chunk_id:
                            relationships.append(
                                {
                                    "chunks": sorted([chunk_id, owner]),
                                    "reasons": [f"{chunk_id} {kind.replace('_', ' ')}: {entry['description'][:160]}"],
                                }
                            )
    return relationships


def boundary_paths(result: dict) -> list[str]:
    return [
        normalize_path(path)
        for kind in ("interfaces_changed", "assumptions", "questions")
        for entry in result[kind]
        for path in entry["paths"]
    ]


def integration_batches(
    plan: Plan,
    results: dict[str, dict],
    digest_sizes: dict[str, int],
    digest_room: int,
) -> list[IntegrationBatch]:
    """Group reviewed chunks for bounded cross-chunk review.

    Related chunks share a batch. Chunks without detected relationships are
    still reviewed together so that a multi-chunk pull request always receives
    an integration pass.
    """
    reviewed = [chunk.id for chunk in plan.chunks if chunk.id in results]
    if len(reviewed) < 2:
        return []

    relationships = [
        item
        for item in plan.relationships + reported_relationships(plan, results)
        if all(chunk_id in results for chunk_id in item["chunks"])
    ]
    union = _UnionFind(reviewed)
    related = set()
    for item in relationships:
        first, second = item["chunks"]
        union.union(first, second)
        related.update(item["chunks"])

    groups: dict[str, list[str]] = {}
    for chunk_id in reviewed:
        key = union.find(chunk_id) if chunk_id in related else "unrelated"
        groups.setdefault(key, []).append(chunk_id)
    unrelated = groups.pop("unrelated", [])
    ordered = sorted(groups.values(), key=lambda members: members[0])
    if len(unrelated) == 1 and ordered:
        ordered[0].append(unrelated[0])
    elif unrelated:
        ordered.append(unrelated)

    batches: list[IntegrationBatch] = []
    for members in ordered:
        current: list[str] = []
        used = 0
        for chunk_id in members:
            size = digest_sizes[chunk_id]
            if current and used + size > digest_room:
                batches.append(IntegrationBatch(id="", chunk_ids=current, relationships=[]))
                current, used = [], 0
            current.append(chunk_id)
            used += size
        batches.append(IntegrationBatch(id="", chunk_ids=current, relationships=[]))

    for number, batch in enumerate(batches, start=1):
        batch.id = f"I{number:02d}"
        members = set(batch.chunk_ids)
        batch.relationships = [item for item in relationships if set(item["chunks"]) <= members]
        paths = []
        for item in batch.relationships:
            for reason in item["reasons"]:
                match = re.match(r"(.+) is split across chunks$", reason)
                if match:
                    paths.append(match.group(1))
        for chunk_id in batch.chunk_ids:
            paths += boundary_paths(results[chunk_id])
        batch.referenced_paths = sorted(dict.fromkeys(paths))
    return batches


def _similar(first: str, second: str) -> bool:
    words_a = set(re.findall(r"[a-z0-9]+", first.lower()))
    words_b = set(re.findall(r"[a-z0-9]+", second.lower()))
    if not words_a or not words_b:
        return False
    return len(words_a & words_b) / len(words_a | words_b) >= 0.5


def aggregate(sources: list[tuple[str, list[dict]]]) -> list[dict]:
    """Deduplicate findings from all sessions, keeping the highest severity."""
    candidates: list[dict] = []
    for source, findings in sources:
        for raw in findings:
            finding = normalize_finding(raw)
            duplicate = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate["finding"]["path"] == finding["path"]
                    and candidate["finding"]["line"] == finding["line"]
                    and candidate["finding"]["side"] == finding["side"]
                    and (
                        _similar(candidate["finding"]["title"], finding["title"])
                        or candidate["finding"]["severity"] == finding["severity"]
                    )
                ),
                None,
            )
            if duplicate is None:
                candidates.append({"finding": finding, "sources": [source], "verification": "not-required"})
                continue
            duplicate["sources"].append(source)
            if severity_rank(finding["severity"]) < severity_rank(duplicate["finding"]["severity"]):
                duplicate["finding"] = finding

    candidates.sort(
        key=lambda c: (severity_rank(c["finding"]["severity"]), c["finding"]["path"], c["finding"]["line"] or 0)
    )
    for number, candidate in enumerate(candidates, start=1):
        candidate["id"] = f"F{number:03d}"
        if candidate["finding"]["severity"] in BLOCKING:
            candidate["verification"] = "pending"
    return candidates


def evidence_hunk(finding: dict, changes_by_path: dict[str, FileChange]) -> str | None:
    change = changes_by_path.get(finding["path"])
    if change is None or finding["line"] is None:
        return None
    for hunk in change.hunks:
        lines = hunk.left_lines if finding["side"] == "LEFT" else hunk.right_lines
        if finding["line"] in lines:
            return clip(change.header + hunk.text, EVIDENCE_HUNK_LIMIT)
    return None


def prior_blocking_reviews(reviews: list[dict], limit: int, count: int = 3) -> list[str]:
    blocking = [
        review
        for review in reviews
        if review.get("body") and PRIOR_BLOCKING_MARKERS.search(review["body"] + " " + str(review.get("state", "")))
    ]
    blocking.sort(key=lambda review: str(review.get("submitted_at", "")), reverse=True)
    return [
        clip(
            f"Review {review.get('id')} by {review.get('user')} ({review.get('state')}, commit {review.get('commit_id')}):\n"
            f"{review['body']}",
            limit,
        )
        for review in blocking[:count]
    ]


def apply_verdicts(candidates: list[dict], batch: list[dict], result: dict) -> list[str]:
    """Apply one verification session's verdicts to its batch of candidates."""
    by_id = {candidate["id"]: candidate for candidate in batch}
    problems = []
    seen = set()
    for verdict in result["verdicts"]:
        candidate = by_id.get(verdict["finding_id"])
        if candidate is None:
            problems.append(f"verdict for unknown finding {verdict['finding_id']}")
            continue
        seen.add(candidate["id"])
        candidate["verification"] = verdict["verdict"]
        candidate["verification_evidence"] = verdict["evidence"]
        if verdict["verdict"] == "downgraded":
            candidate["finding"]["severity"] = verdict["severity"]
    for candidate in batch:
        if candidate["id"] not in seen:
            candidate["verification"] = "unverified"
            problems.append(f"no verdict for {candidate['id']}")
    return problems


def final_findings(candidates: list[dict]) -> list[dict]:
    return [candidate for candidate in candidates if candidate["verification"] != "rejected"]


def merge_carried(candidates: list[dict], findings: list[dict], source: str) -> None:
    """Add verified prior findings that are not already candidates."""
    for raw in findings:
        finding = normalize_finding(raw)
        duplicate = any(
            candidate["finding"]["path"] == finding["path"]
            and (candidate["finding"]["line"] == finding["line"] or _similar(candidate["finding"]["title"], finding["title"]))
            for candidate in candidates
        )
        if not duplicate:
            candidates.append(
                {
                    "id": f"F{len(candidates) + 1:03d}",
                    "finding": finding,
                    "sources": [source],
                    "verification": "confirmed",
                }
            )
