"""Fresh Augure sessions with orchestrator-enforced time and tool budgets."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .settings import SECRET_VARIABLES, Settings

TOOL_ITEM_TYPES = {"command_execution", "mcp_tool_call", "web_search", "file_change"}
# The CLI needs its own credential; nothing else secret reaches the session.
SESSION_SECRET_EXCLUSIONS = tuple(name for name in SECRET_VARIABLES if name != "AUGURE_TOKEN")


@dataclass
class SessionOutcome:
    name: str
    exit_code: int | None
    duration_seconds: float
    tool_calls: int
    termination: str | None
    usage: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    result_text: str = ""

    def summary(self) -> dict:
        return {
            "name": self.name,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 1),
            "tool_calls": self.tool_calls,
            "termination": self.termination,
            "usage": self.usage,
            "errors": self.errors,
            "result_bytes": len(self.result_text.encode()),
        }


def _toml_string(value: str) -> str:
    return json.dumps(value)


def session_command(settings: Settings, instructions: Path, schema: Path, result: Path) -> list[str]:
    return [
        "augure",
        "--enable",
        "use_legacy_landlock",
        "--ask-for-approval",
        "never",
        "exec",
        "--model",
        settings.model,
        "-c",
        f"model_instructions_file={_toml_string(str(instructions))}",
        "--ephemeral",
        "--json",
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(result),
        "-",
    ]


def _add_usage(total: dict, usage: dict) -> None:
    for key, value in usage.items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


def _terminate(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_session(
    settings: Settings,
    name: str,
    directory: Path,
    instructions: str,
    prompt: str,
    schema: Path,
    log,
) -> SessionOutcome:
    directory.mkdir(parents=True, exist_ok=True)
    instructions_path = directory / "instructions.md"
    prompt_path = directory / "prompt.md"
    result_path = directory / "result.json"
    instructions_path.write_text(instructions)
    prompt_path.write_text(prompt)
    result_path.unlink(missing_ok=True)

    environment = {key: value for key, value in os.environ.items() if key not in SESSION_SECRET_EXCLUSIONS}
    command = session_command(settings, instructions_path, schema, result_path)
    (directory / "command.json").write_text(json.dumps(command, indent=2) + "\n")

    outcome = SessionOutcome(name=name, exit_code=None, duration_seconds=0.0, tool_calls=0, termination=None)
    started = time.monotonic()
    with open(directory / "events.jsonl", "w") as events, open(directory / "stderr.log", "w") as stderr:
        process = subprocess.Popen(
            command,
            cwd=settings.repo_root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        lock = threading.Lock()

        def feed() -> None:
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except BrokenPipeError:
                pass

        def watch() -> None:
            for line in process.stdout:
                events.write(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                item = event.get("item") if isinstance(event.get("item"), dict) else {}
                with lock:
                    if kind == "item.started" and item.get("type") in TOOL_ITEM_TYPES:
                        outcome.tool_calls += 1
                        log(f"{name}: tool call {outcome.tool_calls}: {str(item.get('command', item.get('type')))[:160]}")
                        if outcome.tool_calls > settings.session_max_tool_calls and outcome.termination is None:
                            outcome.termination = "tool-budget"
                            _terminate(process)
                    elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
                        _add_usage(outcome.usage, event["usage"])
                    elif kind in ("turn.failed", "error"):
                        detail = event.get("error", event.get("message", ""))
                        outcome.errors = (outcome.errors + [json.dumps(detail)[:2000]])[-10:]

        threads = [threading.Thread(target=feed, daemon=True), threading.Thread(target=watch, daemon=True)]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=settings.session_timeout_seconds)
        except subprocess.TimeoutExpired:
            with lock:
                if outcome.termination is None:
                    outcome.termination = "timeout"
            _terminate(process)
        process.wait()
        for thread in threads:
            thread.join(timeout=10)
        process.stdout.close()

    outcome.exit_code = process.returncode
    outcome.duration_seconds = time.monotonic() - started
    if result_path.is_file():
        outcome.result_text = result_path.read_text(errors="replace")
    (directory / "outcome.json").write_text(json.dumps(outcome.summary(), indent=2) + "\n")
    return outcome
