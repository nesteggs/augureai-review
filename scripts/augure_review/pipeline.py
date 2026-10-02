"""Review orchestration: plan, review chunks, integrate, verify, and publish."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from typing import Callable

from . import gitdiff, planner, prompts, publish, review
from .errors import ReviewFailure
from .provider import Provider
from .schemas import BLOCKING, SchemaError, parse_result, write_schemas
from .session import SessionOutcome, run_session
from .settings import Settings
from .state import RunState, append_summary, set_outputs

AUTH_FAILURE = re.compile(r"\b(401 Unauthorized|403 Forbidden)\b")
# Augure reports exhausted allowances as, for example, "Daily limit reached (100%
# used)" and "Weekly allowance used (100% used)".
QUOTA_FAILURE = re.compile(r"(?i)\b(limit reached|allowance used|usage guard|usage limit)\b|\(100% used\)")
# Room for a retry note appended to a rejected session's prompt.
RETRY_RESERVE = 1_200
# Room for headings, related-chunk lists, and delimiters, plus a retry note.
FRAME_RESERVE = 4_000 + RETRY_RESERVE


def _size(text: str) -> int:
    return len(text.encode())


class Pipeline:
    def __init__(self, settings: Settings, state: RunState, provider: Provider | None = None):
        self.settings = settings
        self.state = state
        self.provider = provider or Provider(settings.provider_adapter, settings.repo_root)
        self.outcomes: list[dict] = []
        self.failures: list[dict] = []
        self.gaps: list[dict] = []
        self.receipt: dict | None = None
        self.event: str | None = None
        self.coverage: dict | None = None
        self._lock = threading.Lock()

    # Sessions -------------------------------------------------------------

    def _digest(self, stage: str, instructions: str, prompt: str, nonce: str) -> str:
        material = {
            "stage": stage,
            "model": self.settings.model,
            "cli_version": self.settings.cli_version,
            "base": self.base_sha,
            "head": self.head_sha,
            "schema": json.loads(self.schemas[stage].read_text()),
            "instructions": instructions,
            "prompt": prompt.replace(nonce, "NONCE"),
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()

    def _resume(self, name: str, digest: str, stage: str, check: Callable[[dict], list[str]]) -> dict | None:
        if self.settings.resume_dir is None:
            return None
        directory = self.settings.resume_dir / "sessions" / name
        try:
            if (directory / "input-digest.txt").read_text().strip() != digest:
                return None
            result = parse_result((directory / "result.json").read_text(), stage)
        except (OSError, SchemaError):
            return None
        if check(result):
            return None
        return result

    @staticmethod
    def _classify(outcome: SessionOutcome) -> tuple[str | None, str]:
        if outcome.termination == "timeout":
            return "cli", f"timed out after {outcome.duration_seconds:.0f} seconds"
        if outcome.termination == "tool-budget":
            return "cli", f"exceeded the tool-call budget ({outcome.tool_calls} calls)"
        if outcome.exit_code != 0:
            detail = f"; {outcome.errors[-1]}" if outcome.errors else ""
            return "cli", f"exited with status {outcome.exit_code}{detail}"
        if not outcome.result_text.strip():
            return "invalid-output", "returned no final message"
        return None, ""

    def run_stage(
        self,
        stage: str,
        name: str,
        instructions: str,
        prompt: str,
        check: Callable[[dict], list[str]],
    ) -> dict | None:
        """Run a fresh session with bounded retries; return a validated result."""
        total = _size(instructions) + _size(prompt) + RETRY_RESERVE
        if total > self.settings.chunk_budget_bytes:
            raise ReviewFailure(
                "budget", f"{name} input is {total} bytes, over the {self.settings.chunk_budget_bytes}-byte budget"
            )
        digest = self._digest(stage, instructions, prompt, self.context.nonce)
        resumed = self._resume(name, digest, stage, check)
        if resumed is not None:
            self.state.log(f"{name}: reused a valid result with matching inputs")
            self._store(name, digest, resumed, resumed=True)
            return resumed

        category, reason, note = "cli", "did not run", ""
        for attempt in range(1, self.settings.max_attempts + 1):
            self.state.log(f"{name}: attempt {attempt} ({total} input bytes)")
            outcome = run_session(
                self.settings,
                name,
                self.state.subdirectory("sessions", name, f"attempt-{attempt}"),
                instructions,
                prompt + note,
                self.schemas[stage],
                self.state.log,
            )
            if outcome.termination == "cancelled":
                self._record(name, attempt, outcome, "cancelled", "the review was cancelled")
                raise ReviewFailure("cancelled", "the review was cancelled")
            category, reason = self._classify(outcome)
            # Account-level failures affect every session, so retrying wastes time and
            # a partial review would misrepresent coverage.
            if any(AUTH_FAILURE.search(error) for error in outcome.errors):
                self._record(name, attempt, outcome, "cli", "authentication failed")
                raise ReviewFailure("cli", f"Augure rejected its credentials: {outcome.errors[-1][:300]}")
            if any(QUOTA_FAILURE.search(error) for error in outcome.errors):
                self._record(name, attempt, outcome, "quota", "usage limit reached")
                raise ReviewFailure("quota", f"Augure usage limit reached: {outcome.errors[-1][:300]}")
            if category is None:
                try:
                    result = parse_result(outcome.result_text, stage)
                    problems = check(result)
                except SchemaError as error:
                    problems = [str(error)]
                if not problems:
                    self._record(name, attempt, outcome, None, "")
                    self._store(name, digest, result, resumed=False)
                    return result
                category, reason = "invalid-output", "; ".join(problems)[:1_000]
            self._record(name, attempt, outcome, category, reason)
            self.state.log(f"{name}: attempt {attempt} failed [{category}] {reason}")
            if outcome.termination in ("tool-budget", "timeout"):
                note = prompts.retry_note(reason, terminated=True)
            elif category == "invalid-output":
                note = prompts.retry_note(reason, terminated=False)
            else:
                note = ""

        with self._lock:
            self.failures.append({"session": name, "category": category, "reason": reason})
        return None

    def _record(self, name: str, attempt: int, outcome: SessionOutcome, category: str | None, reason: str) -> None:
        with self._lock:
            self.outcomes.append({**outcome.summary(), "attempt": attempt, "failure": category, "reason": reason})

    def _store(self, name: str, digest: str, result: dict, resumed: bool) -> None:
        self.state.write_json(f"sessions/{name}/result.json", result)
        self.state.path("sessions", name, "input-digest.txt").write_text(digest + "\n")
        if resumed:
            with self._lock:
                self.outcomes.append({"name": name, "resumed": True})

    def _parallel(self, jobs: list[Callable[[], None]]) -> None:
        pool = ThreadPoolExecutor(max_workers=self.settings.parallel_sessions)
        futures = [pool.submit(job) for job in jobs]
        try:
            done, _ = wait(futures, return_when=FIRST_EXCEPTION)
            for future in futures:
                if future in done and future.exception() is not None:
                    raise future.exception()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def _instructions(self, stage: str) -> str:
        return prompts.instructions(
            stage,
            self.policy,
            self.settings.session_max_tool_calls,
            self.settings.session_timeout_seconds // 60,
        )

    # Stages ---------------------------------------------------------------

    def verify_head(self) -> None:
        current = self.provider.head()
        if current != self.settings.expected_head_sha:
            raise ReviewFailure("stale-head", "pull request head changed; refusing to review or publish stale results")

    def freeze(self) -> None:
        repo = self.settings.repo_root
        self.head_sha = gitdiff.resolve_commit(repo, self.settings.expected_head_sha)
        if self.head_sha != self.settings.expected_head_sha:
            raise ReviewFailure("git", "expected head SHA does not resolve to itself")
        if gitdiff.resolve_commit(repo, "HEAD") != self.head_sha:
            raise ReviewFailure("configuration", "the checked-out commit is not the expected head")
        base_tip = gitdiff.resolve_commit(repo, f"refs/remotes/origin/{self.settings.base_ref}")
        self.base_sha = gitdiff.merge_base(repo, base_tip, self.head_sha)
        tree = gitdiff.git(repo, "ls-tree", "-r", "-z", "--name-only", self.head_sha)
        self.head_paths = set(tree.decode(errors="replace").split("\0")) - {""}

    def load_context(self) -> None:
        pull = self.provider.context()
        self.state.write_json("pr-context.json", pull)
        if str(pull.get("head_sha", "")).lower() != self.head_sha:
            raise ReviewFailure("stale-head", "pull request context does not match the expected head")
        self.pull = pull
        self.context = prompts.ReviewContext(
            repository=self.settings.repository,
            change_number=self.settings.change_number,
            base_ref=self.settings.base_ref,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            title=str(pull.get("title", "")),
            body=str(pull.get("body", "")),
            author=str(pull.get("author", "")),
            intent="",
            goals=[],
            nonce=prompts.ReviewContext.new_nonce(),
        )

    def check_intent(self) -> None:
        result = self.run_stage(
            "intent", "intent", self._instructions("intent"), prompts.intent_prompt(self.context), lambda _: []
        )
        if result is None:
            failure = self.failures[-1]
            raise ReviewFailure(failure["category"], f"intent stage failed: {failure['reason']}")
        if not result["has_clear_intent"]:
            reviews = self.pull.get("reviews", [])
            if not any(publish.MISSING_INTENT_MARKER in str(item.get("body", "")) for item in reviews):
                self.publish(publish.missing_intent_review(self.head_sha, result["reason"]), {})
            raise ReviewFailure("missing-intent", f"pull request has no clear intent or goals: {result['reason']}")
        self.context = dataclasses.replace(self.context, intent=result["intent"], goals=result["goals"])

    def plan_coverage(self) -> None:
        self.changes = gitdiff.inventory(self.settings.repo_root, self.base_sha, self.head_sha)
        self.changes_by_path = {change.path: change for change in self.changes}
        self.known_paths = self.head_paths | set(self.changes_by_path) | {
            change.old_path for change in self.changes if change.old_path
        }
        self.state.write_json("inventory.json", [change.summary() for change in self.changes])
        if not self.changes:
            return

        self.prior_by_path = prompts.render_prior_comments(self.pull.get("review_comments", []))
        context_bytes = {
            path: _size(prompts.untrusted(self.context, f"PRIOR-COMMENTS {path}", text))
            for path, text in self.prior_by_path.items()
        }
        # The file listing depends on the plan, so reserve its maximum size.
        overhead = (
            _size(self._instructions("chunk"))
            + _size(prompts.pull_request_header(self.context))
            + prompts.file_listing_bound(self.changes)
            + FRAME_RESERVE
        )
        diff_budget = self.settings.chunk_budget_bytes - overhead
        if diff_budget < 4_000:
            raise ReviewFailure(
                "budget",
                f"chunk budget of {self.settings.chunk_budget_bytes} bytes leaves {diff_budget} bytes for diffs "
                f"after {overhead} bytes of policy and context",
            )
        layer_map = planner.load_layer_map(self.settings.layer_map_file)
        self.plan = planner.build_plan(
            self.changes,
            diff_budget,
            context_bytes,
            layer_map,
        )
        self.state.write_json("plan.json", self.plan.summary())
        self.state.log(
            f"planned {len(self.plan.units)} coverage units for {len(self.changes)} files in "
            f"{len(self.plan.chunks)} chunks"
        )

    def review_chunks(self) -> None:
        self.chunk_results: dict[str, dict] = {}
        instructions = self._instructions("chunk")

        def job(chunk: planner.Chunk) -> Callable[[], None]:
            def run() -> None:
                prompt = prompts.chunk_prompt(self.context, self.plan, chunk, self.changes_by_path, self.prior_by_path)
                result = self.run_stage(
                    "chunk",
                    f"chunk-{chunk.id}",
                    instructions,
                    prompt,
                    lambda value: review.check_chunk_result(chunk, value, self.known_paths),
                )
                with self._lock:
                    if result is None:
                        self.gaps.append(
                            {"kind": "chunk", "detail": f"{chunk.id} failed; unreviewed: {', '.join(chunk.paths)}"}
                        )
                        return
                    self.chunk_results[chunk.id] = result
                    for entry in result["incomplete"]:
                        unit = self.plan.units[entry["unit"]]
                        self.gaps.append(
                            {"kind": "unit", "detail": f"{unit.id} {unit.path} ({unit.description}): {entry['reason']}"}
                        )

            return run

        self._parallel([job(chunk) for chunk in self.plan.chunks])
        if not self.chunk_results:
            failure = self.failures[0] if self.failures else {"category": "coverage", "reason": "no results"}
            raise ReviewFailure(failure["category"], f"every chunk review failed; first failure: {failure['reason']}")

    def review_integration(self) -> None:
        self.integration_results: dict[str, dict] = {}
        self.open_questions: list[str] = []
        instructions = self._instructions("integration")
        digests = {chunk_id: prompts.boundary_digest(chunk_id, result) for chunk_id, result in self.chunk_results.items()}
        overhead = _size(instructions) + _size(prompts.pull_request_header(self.context)) + FRAME_RESERVE
        room = self.settings.chunk_budget_bytes - overhead
        batches = review.integration_batches(
            self.plan, self.chunk_results, {key: _size(value) for key, value in digests.items()}, room // 2
        )
        self.state.write_json(
            "integration-plan.json",
            [{"id": b.id, "chunks": b.chunk_ids, "relationships": b.relationships, "paths": b.referenced_paths} for b in batches],
        )
        limit = self.settings.max_integration_passes
        for batch in batches[limit:]:
            self.gaps.append({"kind": "integration", "detail": f"{batch.id} ({', '.join(batch.chunk_ids)}) exceeded the pass limit"})

        def job(batch: review.IntegrationBatch) -> Callable[[], None]:
            def run() -> None:
                digest_text = "".join(digests[chunk_id] for chunk_id in batch.chunk_ids)
                remaining = room - _size(digest_text) - 200 * len(batch.referenced_paths)
                diffs, omitted = [], []
                for path in batch.referenced_paths:
                    change = self.changes_by_path.get(path)
                    if change is None:
                        continue
                    wrapped = _size(prompts.untrusted(self.context, f"DIFF {path}", change.patch))
                    if wrapped <= remaining:
                        diffs.append((path, change.patch))
                        remaining -= wrapped
                    else:
                        omitted.append(path)
                prompt = prompts.integration_prompt(
                    self.context, batch.id, batch.chunk_ids, batch.relationships, digest_text, diffs, omitted
                )
                result = self.run_stage(
                    "integration",
                    f"integration-{batch.id}",
                    instructions,
                    prompt,
                    lambda value: review.check_integration_result(value, self.known_paths),
                )
                with self._lock:
                    if result is None:
                        self.gaps.append({"kind": "integration", "detail": f"{batch.id} ({', '.join(batch.chunk_ids)}) failed"})
                        return
                    self.integration_results[batch.id] = result
                    for entry in result["unresolved"]:
                        self.open_questions.append(f"{batch.id}: {entry['description']}")
                    if result["status"] != "complete":
                        self.gaps.append({"kind": "integration", "detail": f"{batch.id} reported incomplete examination"})

            return run

        self._parallel([job(batch) for batch in batches[:limit]])

    def verify(self) -> None:
        sources = [(chunk_id, self.chunk_results[chunk_id]["findings"]) for chunk_id in sorted(self.chunk_results)]
        sources += [(batch_id, self.integration_results[batch_id]["findings"]) for batch_id in sorted(self.integration_results)]
        self.candidates = review.aggregate(sources)
        blockers = [c for c in self.candidates if c["finding"]["severity"] in BLOCKING]
        instructions = self._instructions("verification")
        overhead = _size(instructions) + _size(prompts.pull_request_header(self.context)) + FRAME_RESERVE
        room = self.settings.chunk_budget_bytes - overhead
        prior = review.prior_blocking_reviews(self.pull.get("reviews", []), prompts.PRIOR_REVIEW_LIMIT)
        prior_cost = sum(_size(prompts.untrusted(self.context, "PRIOR-REVIEW 0", text)) for text in prior)
        if prior_cost > room // 2:
            prior = prior[:1]
            prior_cost = sum(_size(prompts.untrusted(self.context, "PRIOR-REVIEW 0", text)) for text in prior)

        evidence = {}
        for candidate in blockers:
            hunk = review.evidence_hunk(candidate["finding"], self.changes_by_path)
            if hunk:
                evidence[candidate["id"]] = hunk

        def cost(candidate: dict) -> int:
            return 2 * _size(json.dumps(candidate["finding"])) + _size(evidence.get(candidate["id"], "")) + 400

        # The first batch also carries the prior reviews to reconcile.
        batches: list[list[dict]] = [[]] if blockers or prior else []
        used = prior_cost
        for candidate in blockers:
            if batches[-1] and used + cost(candidate) > room:
                batches.append([])
                used = 0
            batches[-1].append(candidate)
            used += cost(candidate)

        def job(number: int, batch: list[dict]) -> Callable[[], None]:
            def run() -> None:
                batch_prior = prior if number == 1 else []
                batch_evidence = {c["id"]: evidence[c["id"]] for c in batch if c["id"] in evidence}
                prompt = prompts.verification_prompt(self.context, batch, batch_evidence, batch_prior)
                ids = {c["id"] for c in batch}

                def check(value: dict) -> list[str]:
                    problems = [f"verdict for unknown finding {v['finding_id']}" for v in value["verdicts"] if v["finding_id"] not in ids]
                    missing = ids - {v["finding_id"] for v in value["verdicts"]}
                    if missing:
                        problems.append(f"missing verdicts: {', '.join(sorted(missing))}")
                    return problems + review.check_findings(value["carried_forward"], self.known_paths)

                result = self.run_stage("verification", f"verification-V{number:02d}", instructions, prompt, check)
                with self._lock:
                    if result is None:
                        for candidate in batch:
                            candidate["verification"] = "unverified"
                        self.gaps.append({"kind": "verification", "detail": f"V{number:02d} failed; its blocking findings are unverified"})
                        return
                    review.apply_verdicts(self.candidates, batch, result)
                    self.carried.extend(result["carried_forward"])

            return run

        self.carried: list[dict] = []
        self._parallel([job(number, batch) for number, batch in enumerate(batches, start=1)])
        review.merge_carried(self.candidates, self.carried, "prior-review")
        self.state.write_json("findings.json", self.candidates)

    def build_coverage(self) -> dict:
        units = self.plan.units
        reviewed = sum(len(result["units_reviewed"]) for result in self.chunk_results.values())
        return {
            "complete": not self.gaps,
            "units_total": len(units),
            "units_reviewed": reviewed,
            "files_total": len(self.changes),
            "chunks_total": len(self.plan.chunks),
            "integration_passes": len(self.integration_results),
            "gaps": self.gaps,
            "open_questions": self.open_questions,
        }

    def publish(self, payload: dict, changes_by_path: dict) -> dict:
        publish.validate_payload(payload, changes_by_path, self.head_sha)
        self.verify_head()
        payload_file = self.state.write_json("review-payload.json", payload)
        self.state.log(f"publishing {payload['event']} review with {len(payload['comments'])} inline comments")
        response = self.provider.publish(payload_file)
        self.state.write_json("publication-response.json", response)
        review_id = response.get("id")
        if not isinstance(review_id, int) or isinstance(review_id, bool):
            raise ReviewFailure("publication", "publication response has no review ID")
        fetched = self.provider.review(review_id)
        self.receipt = publish.verify_receipt(payload, response, fetched)
        self.event = payload["event"]
        self.state.write_json("receipt.json", self.receipt)
        self.state.log(f"published review {self.receipt['id']}: {self.receipt['url']}")
        return self.receipt

    # Entry point ----------------------------------------------------------

    def run(self) -> None:
        started = time.time()
        self.provider.validate()
        self.verify_head()
        self.freeze()
        self.schemas = write_schemas(self.state.directory / "schemas")
        self.policy = self.settings.prompt_file.read_text()
        self.load_context()
        self.state.write_json(
            "manifest.json",
            {
                "cli_version": self.settings.cli_version,
                "model": self.settings.model,
                "provider": self.settings.provider,
                "repository": self.settings.repository,
                "change_number": self.settings.change_number,
                "base_ref": self.settings.base_ref,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
                "chunk_budget_bytes": self.settings.chunk_budget_bytes,
                "session_timeout_seconds": self.settings.session_timeout_seconds,
                "session_max_tool_calls": self.settings.session_max_tool_calls,
                "max_attempts": self.settings.max_attempts,
                "parallel_sessions": self.settings.parallel_sessions,
                "max_integration_passes": self.settings.max_integration_passes,
                "started_at": started,
            },
        )
        self.check_intent()
        self.plan_coverage()
        if not self.changes:
            self.state.log("the frozen range contains no committed changes; nothing to review")
            self.event = "none"
            return
        self.review_chunks()
        self.review_integration()
        self.verify()
        self.coverage = self.build_coverage()
        self.state.write_json("coverage.json", self.coverage)

        findings = review.final_findings(self.candidates)
        footer = (
            f"Augure {self.settings.cli_version} · {self.settings.model} · base {self.base_sha[:7]} · "
            f"head {self.head_sha[:7]} · {len([o for o in self.outcomes if not o.get('resumed')])} sessions"
        )
        payload = publish.build_review(findings, self.coverage, self.changes_by_path, self.head_sha, footer)
        self.publish(payload, self.changes_by_path)

        if self.gaps:
            categories = [failure["category"] for failure in self.failures]
            category = categories[0] if categories else "coverage"
            raise ReviewFailure(category, f"review published with {len(self.gaps)} coverage gaps; see coverage.json")

    def finish(self, failure: ReviewFailure | None) -> None:
        usage: dict[str, int] = {}
        for outcome in self.outcomes:
            for key, value in outcome.get("usage", {}).items():
                usage[key] = usage.get(key, 0) + value
        status = {
            "result": "failed" if failure else ("published" if self.receipt else "no-review"),
            "failure_category": failure.category if failure else None,
            "message": failure.message if failure else None,
            "event": self.event,
            "receipt": self.receipt,
            "coverage": self.coverage,
            "session_failures": self.failures,
            "usage": usage,
        }
        self.state.write_json("sessions.json", self.outcomes)
        self.state.write_json("status.json", status)
        set_outputs(
            {
                "review-id": str(self.receipt["id"]) if self.receipt else "",
                "review-url": self.receipt["url"] if self.receipt else "",
                "event": self.event or "",
                "failure-category": failure.category if failure else "",
                "artifacts-path": str(self.state.directory),
            }
        )
        rows = [
            ("Result", status["result"]),
            ("Failure", f"{failure.category}: {failure.message}" if failure else "none"),
            ("Event", self.event or "none"),
            ("Review", self.receipt["url"] if self.receipt else "not published"),
            ("Sessions", str(len(self.outcomes))),
            ("Tokens", ", ".join(f"{key} {value}" for key, value in sorted(usage.items())) or "not reported"),
        ]
        if self.coverage:
            rows.append(("Coverage", f"{self.coverage['units_reviewed']}/{self.coverage['units_total']} units, {len(self.gaps)} gaps"))
        append_summary(
            "### Augure review\n\n| | |\n| --- | --- |\n"
            + "".join(f"| {key} | {str(value).replace('|', '/')} |\n" for key, value in rows)
            + "\n"
        )
