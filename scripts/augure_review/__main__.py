"""Command-line entry point: python3 -m augure_review."""

from __future__ import annotations

import signal
import sys
import traceback

from .errors import ReviewFailure
from .pipeline import Pipeline
from .session import CANCELLED
from .settings import load_settings
from .state import RunState, annotate_error, redact_tree


class Cancelled(BaseException):
    """Raised in the main thread when the workflow cancels the review."""


def _cancel(signum, frame) -> None:
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, signal.SIG_IGN)
    CANCELLED.set()
    raise Cancelled(signal.Signals(signum).name)


def main() -> int:
    try:
        settings = load_settings()
    except ReviewFailure as failure:
        annotate_error(failure.category, failure.message)
        return 1

    state = RunState(settings.state_dir)
    pipeline = Pipeline(settings, state)
    failure = None
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, _cancel)
    try:
        pipeline.run()
    except Cancelled as error:
        failure = ReviewFailure("cancelled", f"the review was cancelled by {error}")
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
