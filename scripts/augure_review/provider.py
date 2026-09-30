"""Code-host adapter invocation.

Adapters are executables under providers/ with the subcommands validate, head,
context, publish PAYLOAD_FILE, and review REVIEW_ID. They receive the provider
token; review sessions never do.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .errors import ReviewFailure


class Provider:
    def __init__(self, adapter: Path, repo_root: Path):
        self.adapter = adapter
        self.repo_root = repo_root

    def _run(self, category: str, *args: str) -> str:
        result = subprocess.run(
            ["bash", str(self.adapter), *args],
            cwd=self.repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1:] or ["no error output"]
            raise ReviewFailure(category, f"provider {args[0]} failed: {detail[0]}")
        return result.stdout

    def _json(self, category: str, *args: str) -> dict:
        output = self._run(category, *args)
        try:
            value = json.loads(output)
        except json.JSONDecodeError as error:
            raise ReviewFailure(category, f"provider {args[0]} returned invalid JSON") from error
        if not isinstance(value, dict):
            raise ReviewFailure(category, f"provider {args[0]} returned a non-object")
        return value

    def validate(self) -> None:
        self._run("configuration", "validate")

    def head(self) -> str:
        return self._run("provider", "head").strip().lower()

    def context(self) -> dict:
        return self._json("provider", "context")

    def publish(self, payload_file: Path) -> dict:
        return self._json("publication", "publish", str(payload_file))

    def review(self, review_id: int) -> dict:
        return self._json("publication", "review", str(review_id))
