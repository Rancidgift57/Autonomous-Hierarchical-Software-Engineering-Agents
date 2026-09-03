"""Prompt regression / hallucination testing (new).

Prompts (the `_CHARTER_TEMPLATE` in `app.agents.workers.base`, the ones built
in `app.agents.managers.base`, `app.agents.cto`, `app.qa.agents`, etc.) are
code, but unlike ordinary code a broken prompt usually doesn't raise an
exception -- it just quietly produces worse output: a plan that ignores the
task, a self-review that says "pass" when the tests failed, a coder model
that starts editing files nobody asked about. Pydantic schema validation
(`LLMGateway.generate_json`) already catches *malformed* output. This
package catches *malformed-but-valid* output -- JSON that parses and
satisfies the schema, but doesn't actually answer the prompt.

Two things live here:

* `schemas.PromptCheckIssue` / `PromptCheckReport` -- the report shape.
* `checks.py` -- pure, deterministic functions that look for known
  hallucination symptoms (empty/refusal output, prompt-echo, a plan or
  implementation that isn't grounded in the task/context it was given, a
  self-review that contradicts the actual test run). None of these call an
  LLM, so they run in every CI build, on every commit, for free -- no
  Ollama required.

See `backend/tests/test_prompt_regression.py` for how this is used to (a)
snapshot the literal prompt templates so an edit to them is a visible diff
a reviewer must approve, and (b) drive `BaseWorkerAgent` end-to-end with
scripted "bad" LLM outputs to prove the checks actually catch them.
"""
