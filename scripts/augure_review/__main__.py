"""Command-line entry point: python3 -m augure_review."""

from __future__ import annotations

import sys
import traceback

from .errors import ReviewFailure
from .pipeline import Pipeline
from .settings import load_settings
from .state import RunState, annotate_error, redact_tree


def main() -> int:
    try:
        settings = load_settings()
    except ReviewFailure as failure:
        annotate_error(failure.category, failure.message)
        return 1

    state = RunState(settings.state_dir)
    pipeline = Pipeline(settings, state)
    failure = None
    try:
        pipeline.run()
    except ReviewFailure as error:
        failure = error
    except Exception as error:  # noqa: BLE001 - report every crash with a category
        state.path("traceback.txt").write_text(traceback.format_exc())
        failure = ReviewFailure("internal", f"{type(error).__name__}: {error}")
    finally:
        try:
            pipeline.finish(failure)
        finally:
            redact_tree(settings.state_dir)

    if failure:
        annotate_error(failure.category, failure.message)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
