"""Structured validation issues and the exception that carries them.

The harness deliberately collects *every* problem with an input set before
raising, rather than aborting on the first one. When an evaluator swaps in a
new fixture, a single run should tell them everything that is wrong with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    """A single validation finding, tied to the place that produced it."""

    code: str
    message: str
    location: str
    severity: str = ERROR

    def render(self) -> str:
        return "[{}] {} at {}: {}".format(
            self.severity.upper(), self.code, self.location, self.message
        )


class InputValidationError(Exception):
    """Raised when input files are unusable. Reports all errors at once."""

    def __init__(self, issues: Sequence[Issue]) -> None:
        self.issues: List[Issue] = list(issues)
        errors = [i for i in self.issues if i.severity == ERROR]
        header = "Input validation failed with {} error(s):".format(len(errors))
        body = "\n".join("  {}. {}".format(n, i.render()) for n, i in enumerate(errors, 1))
        super().__init__(header + "\n" + body)


class ConfigError(Exception):
    """Raised when config.yaml is missing, malformed, or internally invalid."""
