"""Run state that is preserved as a workflow artifact."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from .settings import SECRET_VARIABLES

REDACTED = "[REDACTED]"


class RunState:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, *parts: str) -> Path:
        path = self.directory.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def subdirectory(self, *parts: str) -> Path:
        path = self.directory.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, name: str, value) -> Path:
        path = self.path(name)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        return path

    def log(self, message: str) -> None:
        with self._lock:
            print(f"augure-review: {message}", flush=True)
            with open(self.path("orchestrator.log"), "a") as handle:
                handle.write(message + "\n")


def redact_tree(directory: Path) -> None:
    """Remove secret values from every preserved file."""
    secrets = sorted(
        {os.environ[name] for name in SECRET_VARIABLES if len(os.environ.get(name, "")) >= 8},
        key=len,
        reverse=True,
    )
    if not secrets:
        return
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        redacted = data
        for secret in secrets:
            redacted = redacted.replace(secret.encode(), REDACTED.encode())
        if redacted != data:
            path.write_bytes(redacted)


def set_outputs(values: dict[str, str]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def append_summary(markdown: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(markdown)


def annotate_error(category: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        clean = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Augure review failed ({category})::{clean}", file=sys.stderr, flush=True)
    else:
        print(f"augure-review: [{category}] {message}", file=sys.stderr, flush=True)
