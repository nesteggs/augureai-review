"""Review settings loaded from the action environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ReviewFailure

SECRET_VARIABLES = (
    "AUGURE_TOKEN",
    "AUGURE_API_KEY",
    "OPENAI_API_KEY",
    "REVIEW_PROVIDER_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
)


@dataclass(frozen=True)
class Settings:
    action_root: Path
    provider: str
    model: str
    prompt_file: Path
    repository: str
    change_number: int
    base_ref: str
    expected_head_sha: str
    state_dir: Path
    repo_root: Path
    cli_version: str
    chunk_budget_bytes: int
    session_timeout_seconds: int
    session_max_tool_calls: int
    max_attempts: int
    parallel_sessions: int
    max_integration_passes: int
    layer_map_file: Path | None
    resume_dir: Path | None

    @property
    def provider_adapter(self) -> Path:
        return self.action_root / "providers" / f"{self.provider}.sh"


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, "")
    if value == "" and default is not None:
        return default
    if value == "":
        raise ReviewFailure("configuration", f"{name} is required")
    return value


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _env(name, str(default))
    if not re.fullmatch(r"[0-9]+", raw):
        raise ReviewFailure("configuration", f"{name} must be a non-negative integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ReviewFailure("configuration", f"{name} must be between {minimum} and {maximum}")
    return value


def _optional_path(name: str) -> Path | None:
    raw = os.environ.get(name, "")
    return Path(raw) if raw else None


def load_settings() -> Settings:
    layer_map_file = _optional_path("AUGURE_REVIEW_LAYER_MAP_FILE")
    if layer_map_file is not None and not layer_map_file.is_file():
        raise ReviewFailure("configuration", "layer map file does not exist")
    resume_dir = _optional_path("AUGURE_REVIEW_RESUME_DIR")
    if resume_dir is not None and not resume_dir.is_dir():
        raise ReviewFailure("configuration", "resume directory does not exist")

    return Settings(
        action_root=Path(_env("AUGURE_REVIEW_ACTION_ROOT")),
        provider=_env("AUGURE_REVIEW_PROVIDER"),
        model=_env("AUGURE_REVIEW_MODEL"),
        prompt_file=Path(_env("AUGURE_REVIEW_PROMPT_FILE")),
        repository=_env("AUGURE_REVIEW_REPOSITORY"),
        change_number=int(_env("AUGURE_REVIEW_CHANGE_NUMBER")),
        base_ref=_env("AUGURE_REVIEW_BASE_REF"),
        expected_head_sha=_env("AUGURE_REVIEW_EXPECTED_HEAD_SHA").lower(),
        state_dir=Path(_env("AUGURE_REVIEW_STATE_DIR")),
        repo_root=Path(os.getcwd()),
        cli_version=_env("AUGURE_REVIEW_CLI_VERSION"),
        chunk_budget_bytes=_int_env("AUGURE_REVIEW_CHUNK_BUDGET_BYTES", 120_000, 16_000, 4_000_000),
        session_timeout_seconds=60
        * _int_env("AUGURE_REVIEW_SESSION_TIMEOUT_MINUTES", 12, 1, 120),
        session_max_tool_calls=_int_env("AUGURE_REVIEW_SESSION_MAX_TOOL_CALLS", 12, 1, 200),
        max_attempts=_int_env("AUGURE_REVIEW_MAX_ATTEMPTS", 2, 1, 5),
        parallel_sessions=_int_env("AUGURE_REVIEW_PARALLEL_SESSIONS", 2, 1, 16),
        max_integration_passes=_int_env("AUGURE_REVIEW_MAX_INTEGRATION_PASSES", 6, 0, 50),
        layer_map_file=layer_map_file,
        resume_dir=resume_dir,
    )
