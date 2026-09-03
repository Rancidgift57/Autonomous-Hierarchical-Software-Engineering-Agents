"""Report shape produced by `app.prompt_testing.checks`."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["info", "warning", "critical"]


class PromptCheckIssue(BaseModel):
    """A single detected symptom of a bad/hallucinating prompt response.

    `severity`:
      * "critical" -- the output is actively wrong/unsafe to trust
        (empty, refusal language, or contradicts ground truth like a
        failing test run). Should block deploy / fail CI.
      * "warning"  -- suspicious but not proven wrong (e.g. touching files
        nobody mentioned). Worth a human's attention, not an automatic
        failure.
      * "info"     -- purely informational.
    """

    code: str
    severity: Severity
    message: str


class PromptCheckReport(BaseModel):
    issues: list[PromptCheckIssue] = Field(default_factory=list)

    @property
    def has_critical(self) -> bool:
        return any(issue.severity == "critical" for issue in self.issues)

    @property
    def has_warning_or_worse(self) -> bool:
        return any(issue.severity in ("warning", "critical") for issue in self.issues)

    def as_error_strings(self) -> list[str]:
        """Render issues as strings suitable for a `WorkerResult.errors`-style list."""

        return [f"[prompt-check:{i.severity}] {i.code}: {i.message}" for i in self.issues]
