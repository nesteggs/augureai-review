"""Deterministic review construction, validation, and receipt verification."""

from __future__ import annotations

from .errors import ReviewFailure
from .gitdiff import FileChange
from .prompts import clip
from .schemas import BLOCKING

EMOJI = {"mountain": "⛰", "boulder": "🧗", "pebble": "⚪", "sand": "⏳", "dust": "🌫"}
BODY_LIMIT = 60_000
COMMENT_BODY_LIMIT = 8_000
MISSING_INTENT_MARKER = "<!-- augure-review:missing-intent -->"
EXPECTED_STATE = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}


def review_marker(head_sha: str) -> str:
    return f"<!-- augure-review head={head_sha} -->"


def inline_location(finding: dict, changes_by_path: dict[str, FileChange]) -> dict | None:
    change = changes_by_path.get(finding["path"])
    if change is None or finding["line"] is None:
        return None
    lines = change.left_lines if finding["side"] == "LEFT" else change.right_lines
    if finding["line"] not in lines:
        return None
    return {"path": finding["path"], "line": finding["line"], "side": finding["side"]}


def location_label(finding: dict) -> str:
    if not finding["path"]:
        return "pull request"
    if finding["line"] is None:
        return f"`{finding['path']}`"
    suffix = " (base)" if finding["side"] == "LEFT" else ""
    return f"`{finding['path']}:{finding['line']}`{suffix}"


def format_finding(candidate: dict) -> str:
    finding = candidate["finding"]
    lines = [
        f"{EMOJI[finding['severity']]} **{finding['severity']}** — {finding['title']}",
        "",
        f"**Failure:** {finding['failure_scenario']}",
        "",
        f"**Evidence:** {finding['evidence']}",
        "",
        f"**Fix:** {finding['fix']}",
    ]
    if candidate.get("verification") == "unverified":
        lines += ["", "_Blocking finding not independently re-verified._"]
    return clip("\n".join(lines), COMMENT_BODY_LIMIT)


def decide_event(candidates: list[dict], coverage_complete: bool) -> str:
    if any(candidate["finding"]["severity"] in BLOCKING for candidate in candidates):
        return "REQUEST_CHANGES"
    return "APPROVE" if coverage_complete else "COMMENT"


def build_review(
    candidates: list[dict],
    coverage: dict,
    changes_by_path: dict[str, FileChange],
    head_sha: str,
    footer: str,
) -> dict:
    event = decide_event(candidates, coverage["complete"])
    comments = []
    detailed = []
    for candidate in candidates:
        location = inline_location(candidate["finding"], changes_by_path)
        if location:
            comments.append({**location, "body": format_finding(candidate)})
        else:
            detailed.append(candidate)

    blocking = [c for c in candidates if c["finding"]["severity"] in BLOCKING]
    notes = [c for c in candidates if c["finding"]["severity"] not in BLOCKING]
    sections = []
    if event == "APPROVE":
        sections.append("LGTM 👍")
    elif event == "REQUEST_CHANGES":
        sections.append(f"Changes requested: {len(blocking)} blocking finding{'s' if len(blocking) != 1 else ''}.")
    else:
        sections.append("Review incomplete. Coverage gaps prevent approval; see Coverage below.")

    if blocking:
        sections.append(
            "### Blocking findings\n\n"
            + "\n".join(
                f"- {EMOJI[c['finding']['severity']]} {location_label(c['finding'])} — {c['finding']['title']}"
                for c in blocking
            )
        )
    if notes:
        sections.append(
            "### Non-blocking notes\n\n"
            + "\n".join(
                f"- {EMOJI[c['finding']['severity']]} {c['finding']['severity']} · {location_label(c['finding'])} — "
                f"{c['finding']['title']}"
                for c in notes
            )
        )
    if detailed:
        sections.append(
            "### Findings outside the diff\n\n"
            + "\n\n---\n\n".join(f"{location_label(c['finding'])}\n\n{format_finding(c)}" for c in detailed)
        )

    coverage_lines = [
        f"Reviewed {coverage['units_reviewed']} of {coverage['units_total']} coverage units across "
        f"{coverage['files_total']} files in {coverage['chunks_total']} chunks; "
        f"{coverage['integration_passes']} integration passes."
    ]
    coverage_lines += [f"- {gap['kind']}: {gap['detail']}" for gap in coverage["gaps"]]
    sections.append("### Coverage\n\n" + "\n".join(coverage_lines))
    if coverage.get("open_questions"):
        sections.append(
            "### Open cross-chunk questions\n\n" + "\n".join(f"- {question}" for question in coverage["open_questions"])
        )
    sections.append(f"<sub>{footer}</sub>")

    marker = review_marker(head_sha)
    body = clip("\n\n".join(sections), BODY_LIMIT - len(marker) - 2) + "\n\n" + marker
    return {"commit_id": head_sha, "body": body, "event": event, "comments": comments}


def missing_intent_review(head_sha: str, reason: str) -> dict:
    body = (
        "Augure did not review this pull request because its description has no clear intent and goals. "
        "Please describe what the change is meant to accomplish and how to judge it, then request another "
        f"review.\n\n{clip(reason, 2_000)}\n\n{MISSING_INTENT_MARKER}"
    )
    return {"commit_id": head_sha, "body": body, "event": "COMMENT", "comments": []}


def validate_payload(payload: dict, changes_by_path: dict[str, FileChange], head_sha: str) -> None:
    if payload.get("commit_id") != head_sha:
        raise ReviewFailure("publication", "review payload does not target the expected head commit")
    if payload.get("event") not in EXPECTED_STATE:
        raise ReviewFailure("publication", "review payload has an invalid event")
    if not isinstance(payload.get("body"), str) or len(payload["body"]) > 65_536:
        raise ReviewFailure("publication", "review body is missing or too long")
    for comment in payload.get("comments", []):
        if set(comment) != {"path", "line", "side", "body"}:
            raise ReviewFailure("publication", "inline comment has unexpected fields")
        if inline_location(comment, changes_by_path) is None:
            raise ReviewFailure(
                "publication", f"inline comment is outside the diff: {comment['path']}:{comment['line']}"
            )


def verify_receipt(payload: dict, response: dict, fetched: dict) -> dict:
    """Confirm the publication response and an independent read agree."""
    review_id = response.get("id")
    if not isinstance(review_id, int) or isinstance(review_id, bool):
        raise ReviewFailure("publication", "publication response has no review ID")
    expected_state = EXPECTED_STATE[payload["event"]]
    for source, value in (("response", response), ("fetched review", fetched)):
        if value.get("id") != review_id:
            raise ReviewFailure("publication", f"{source} has a different review ID")
        if value.get("state") != expected_state:
            raise ReviewFailure("publication", f"{source} state is {value.get('state')}, expected {expected_state}")
        if value.get("commit_id") != payload["commit_id"]:
            raise ReviewFailure("publication", f"{source} targets a different commit")
    return {
        "id": review_id,
        "url": response.get("html_url", ""),
        "state": expected_state,
        "commit_id": payload["commit_id"],
    }
