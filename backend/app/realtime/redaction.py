"""Redaction for anything that goes out over `/ws/projects/{project_id}`.

Every `RealtimeEvent.payload` is run through `sanitize_payload` before it
is ever constructed (see `RealtimeEmitter.emit`). This is the single
choke point enforcing the Phase 19 requirement that the socket never
exposes secrets, private environment values, hidden system prompts, or
other sensitive LLM data -- callers building a payload dict don't each
need to remember to redact it themselves.
"""

from __future__ import annotations

import re
from typing import Any

#: Payload dict keys that are dropped outright, regardless of their value.
#: Broader than the deployment validator's credential-*shaped* patterns --
#: this also blocks anything that would leak an env snapshot, prompt
#: text, or raw LLM request/response content.
_BLOCKED_KEY_PATTERN = re.compile(
    r"(SECRET|PASSWORD|PASSWD|PWD|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY|"
    r"ACCESS[_-]?KEY|CREDENTIAL|AUTH[_-]?KEY|CLIENT[_-]?SECRET|"
    r"SYSTEM[_-]?PROMPT|ENV(IRON(MENT)?)?[_-]?(VARS?|VALUES?)?|"
    r"^PROMPT$|RAW[_-]?(REQUEST|RESPONSE)|COOKIE|SESSION[_-]?ID)",
    re.IGNORECASE,
)

#: Values are truncated past this length -- a payload is a short status
#: blurb for a live feed, never a place for full file contents or logs.
_MAX_STRING_LEN = 800
_MAX_LIST_ITEMS = 50
_MAX_DEPTH = 4


def _is_blocked_key(key: str) -> bool:
    # Deferred import: `app.deployment.validator` lives under the
    # `app.deployment` package, whose `__init__` eagerly imports
    # `DeploymentManager` -> ... -> `IntegrationAgent` -> this module.
    # Importing it at module scope here would deadlock that cycle; by the
    # time `sanitize_payload` actually runs, every module is fully loaded.
    from app.deployment.validator import SECRET_NAME_PATTERN

    return bool(_BLOCKED_KEY_PATTERN.search(key)) or bool(SECRET_NAME_PATTERN.search(key))


def _sanitize_value(value: Any, depth: int) -> Any:
    from app.deployment.validator import redact_secrets

    if depth > _MAX_DEPTH:
        return "<omitted: nested too deep>"

    if isinstance(value, str):
        text = redact_secrets(value)
        if len(text) > _MAX_STRING_LEN:
            text = text[:_MAX_STRING_LEN] + "...<truncated>"
        return text

    if isinstance(value, dict):
        return sanitize_payload(value, _depth=depth + 1)

    if isinstance(value, (list, tuple)):
        items = [_sanitize_value(item, depth + 1) for item in list(value)[:_MAX_LIST_ITEMS]]
        if len(value) > _MAX_LIST_ITEMS:
            items.append(f"...<{len(value) - _MAX_LIST_ITEMS} more item(s) omitted>")
        return items

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    # Anything else (custom objects, enums, etc.) -- stringify defensively
    # rather than risk serializing something we haven't accounted for.
    return _sanitize_value(str(value), depth)


def sanitize_payload(payload: dict[str, Any], *, _depth: int = 0) -> dict[str, Any]:
    """Return a redacted, size-bounded, JSON-safe copy of `payload`.

    - Keys that look like secrets/credentials/env dumps/system prompts are
      dropped entirely (not just redacted), since the presence of such a
      key is itself often sensitive.
    - Every string value is scrubbed with the same secret-shape patterns
      used for deployment logs (`app.deployment.validator.redact_secrets`)
      and truncated.
    - Nesting/list length are bounded so one bad payload can't blow up a
      client's message handling.
    """

    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or _is_blocked_key(key):
            continue
        clean[key] = _sanitize_value(value, _depth)
    return clean
