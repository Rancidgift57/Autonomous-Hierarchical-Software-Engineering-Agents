"""Deterministic, LLM-free checks for hallucination / prompt-regression symptoms.

Every function here is pure: given already-parsed, schema-valid agent
output (a `WorkerPlan`, `WorkerImplementationOutput`, `WorkerSelfReview`,
...), decide whether it *looks* grounded in the task it was actually given,
or whether it looks like the kind of thing a model produces when a prompt
edit broke it (empty output, refusal language, echoing the prompt back,
inventing files, or claiming success a real test run disproves).

These are heuristics, not proof -- they're tuned to catch obvious drift
with a low false-positive rate, not to replace human review or an
LLM-as-judge eval. See `backend/tests/test_prompt_regression.py` for the
"golden path produces zero issues" regression test that keeps them honest.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from app.agents.workers.schemas import (
    WorkerContext,
    WorkerImplementationOutput,
    WorkerPlan,
    WorkerSelfReview,
)
from app.prompt_testing.schemas import PromptCheckIssue, PromptCheckReport

# ---------------------------------------------------------------------------
# Degenerate / refusal / echo text
# ---------------------------------------------------------------------------

#: Phrases that should never appear in a worker's structured output. Their
#: presence means the model is talking *about* the task ("as an AI, I
#: cannot...") instead of doing it -- almost always caused by a prompt edit
#: that accidentally makes the request look unsafe, out of scope, or
#: confuses the model about what role it's playing.
_REFUSAL_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bas an ai\b",
        r"\bas a large language model\b",
        r"\bi cannot comply\b",
        r"\bi can.?t help with that\b",
        r"\bi.?m sorry,? but\b",
        r"\bi cannot assist\b",
        r"\bi'?m not able to\b",
        r"\bi don'?t have the ability to\b",
    ]
]

#: If a free-text field is at least this similar to the prompt it was
#: generated from, the model most likely echoed the instructions back
#: instead of answering them -- a classic symptom of an over-long,
#: contradictory, or otherwise broken prompt.
_ECHO_SIMILARITY_THRESHOLD = 0.55
_ECHO_MIN_LENGTH = 40


def _echo_ratio(text: str, prompt: str) -> float:
    return difflib.SequenceMatcher(a=text.lower(), b=prompt.lower()).ratio()


def check_degenerate_text(
    field: str, text: str, *, source_prompt: str | None = None
) -> list[PromptCheckIssue]:
    """Flag empty output, refusal language, or prompt-echo in a free-text field.

    Call this only for fields that are semantically required to carry
    content (e.g. `plan.approach`, `implementation.summary`) -- optional
    fields that legitimately default to "" (e.g. `review.notes`) should be
    skipped by the caller when blank, not run through here.
    """

    issues: list[PromptCheckIssue] = []
    stripped = (text or "").strip()

    if not stripped:
        issues.append(
            PromptCheckIssue(
                code="empty_output",
                severity="critical",
                message=(
                    f"'{field}' came back empty. The prompt is producing "
                    "no usable content for this field."
                ),
            )
        )
        return issues

    for pattern in _REFUSAL_PATTERNS:
        if pattern.search(stripped):
            issues.append(
                PromptCheckIssue(
                    code="refusal_language",
                    severity="critical",
                    message=(
                        f"'{field}' contains refusal/meta language matching "
                        f"{pattern.pattern!r} -- the model is talking about "
                        "the task instead of doing it."
                    ),
                )
            )
            break

    if source_prompt and len(stripped) >= _ECHO_MIN_LENGTH:
        ratio = _echo_ratio(stripped, source_prompt)
        if ratio >= _ECHO_SIMILARITY_THRESHOLD:
            issues.append(
                PromptCheckIssue(
                    code="prompt_echo",
                    severity="warning",
                    message=(
                        f"'{field}' is {ratio:.0%} similar (difflib ratio) to "
                        "its own prompt -- the model may be echoing "
                        "instructions rather than answering them."
                    ),
                )
            )

    return issues


# ---------------------------------------------------------------------------
# Grounding: does the output actually connect to what the worker was told?
# ---------------------------------------------------------------------------


def check_plan_grounding(plan: WorkerPlan) -> list[PromptCheckIssue]:
    issues: list[PromptCheckIssue] = []
    if not plan.steps:
        issues.append(
            PromptCheckIssue(
                code="empty_plan_steps",
                severity="warning",
                message="The plan has an approach but lists zero concrete steps.",
            )
        )
    if not plan.target_files:
        issues.append(
            PromptCheckIssue(
                code="empty_target_files",
                severity="warning",
                message="The plan doesn't name any files it expects to touch.",
            )
        )
    return issues


def check_implementation_grounding(
    plan: WorkerPlan,
    implementation: WorkerImplementationOutput,
    context: WorkerContext,
) -> list[PromptCheckIssue]:
    """Flag an implementation whose files have no connection to the plan or context.

    A little drift is normal -- a worker can reasonably discover and touch
    a sibling file mid-implementation that wasn't named up front. What this
    catches is the *total* case: every single file the implementation
    touches is one neither the plan's `target_files` nor the supplied
    `WorkerContext.relevant_files` ever mentioned. That's the signature of
    a coder model that has stopped following its own plan -- worth a
    human's attention even though `WorkerScope` may still let it through.
    """

    if not implementation.files:
        return []

    declared = set(plan.target_files) | set(context.relevant_files)
    ungrounded = [f.path for f in implementation.files if f.path not in declared]

    if ungrounded and len(ungrounded) == len(implementation.files):
        return [
            PromptCheckIssue(
                code="ungrounded_files",
                severity="warning",
                message=(
                    "None of the implementation's files "
                    f"({', '.join(ungrounded)}) were named in the plan's "
                    "target_files or the supplied context. This can be "
                    "legitimate, but is also a common symptom of a coder "
                    "model going off-plan."
                ),
            )
        ]
    return []


def check_self_review_consistency(
    review: WorkerSelfReview, tests: dict[str, Any]
) -> list[PromptCheckIssue]:
    """Catch the highest-signal hallucination pattern: the model *claiming*
    success while the ground truth (the actual pytest run) disagrees.

    `BaseWorkerAgent.run` already treats a failing test run as non-SUCCESS
    regardless of what the review says, so this issue doesn't change the
    final status -- its job is to *name* the failure mode explicitly. If a
    prompt tweak starts making the review stage over-confident, that would
    otherwise disappear into a generic PARTIAL status with a vague "issues"
    list; this makes it a distinct, greppable diagnostic instead.
    """

    if tests.get("ran") and not tests.get("success") and review.decision == "pass":
        return [
            PromptCheckIssue(
                code="self_review_contradicts_tests",
                severity="critical",
                message=(
                    "Self-review returned decision='pass', but the actual "
                    "test run failed. The review stage is not grounded in "
                    "the real test output -- check whether its prompt still "
                    "includes the test results, and whether a recent prompt "
                    "edit changed how it weighs them."
                ),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Aggregate entry point
# ---------------------------------------------------------------------------


def run_worker_prompt_checks(
    *,
    plan: WorkerPlan,
    implementation: WorkerImplementationOutput,
    review: WorkerSelfReview,
    context: WorkerContext,
    tests: dict[str, Any],
    prompts: dict[str, str] | None = None,
) -> PromptCheckReport:
    """Run every worker-stage check and merge the results into one report.

    `prompts`, if supplied, should map stage name -> the exact prompt text
    sent for that stage (e.g. `{"plan": ..., "implement": ...}`), enabling
    the prompt-echo check. Stages are optional; omitted ones just skip the
    echo check for that field.
    """

    prompts = prompts or {}
    issues: list[PromptCheckIssue] = []

    issues += check_plan_grounding(plan)
    issues += check_implementation_grounding(plan, implementation, context)
    issues += check_self_review_consistency(review, tests)

    issues += check_degenerate_text(
        "plan.approach", plan.approach, source_prompt=prompts.get("plan")
    )
    issues += check_degenerate_text(
        "implementation.summary",
        implementation.summary,
        source_prompt=prompts.get("implement"),
    )
    if review.notes:
        issues += check_degenerate_text(
            "review.notes", review.notes, source_prompt=prompts.get("self_review")
        )

    return PromptCheckReport(issues=issues)
