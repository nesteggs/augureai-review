"""Categorized review failures."""

CATEGORIES = (
    "configuration",
    "git",
    "provider",
    "missing-intent",
    "budget",
    "cli",
    "quota",
    "invalid-output",
    "coverage",
    "stale-head",
    "publication",
    "cancelled",
    "internal",
)


class ReviewFailure(Exception):
    """A failure whose category is reported to the workflow and artifacts."""

    def __init__(self, category: str, message: str):
        if category not in CATEGORIES:
            raise ValueError(f"unknown failure category: {category}")
        super().__init__(message)
        self.category = category
        self.message = message

    def __str__(self) -> str:
        return f"[{self.category}] {self.message}"
