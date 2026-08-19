"""Validators for deployment artifacts (Phase 15): Dockerfile, docker-compose,
environment variables, and health-check configuration.

Nothing here executes anything -- it is pure, synchronous, static analysis
of text/structured config, so it's safe to run before any Docker command
ever touches the sandbox. The one cross-cutting rule every validator here
enforces in some form is "never expose secrets": hardcoded credentials in
a Dockerfile or compose file are always a blocking (CRITICAL) finding, and
`redact_secrets` is the single place secret-shaped text gets scrubbed
before it can land in a log line or a `DeploymentEvent`.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from app.deployment.schemas import EnvVarSpec, ValidationIssue, ValidationResult
from app.state.enums import ErrorSeverity

#: Env/ARG/compose key names that indicate a secret value. Intentionally
#: broad (better a false-positive warning than a leaked credential).
SECRET_NAME_PATTERN = re.compile(
    r"(SECRET|PASSWORD|PASSWD|PWD|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY|"
    r"ACCESS[_-]?KEY|CREDENTIAL|AUTH[_-]?KEY|CLIENT[_-]?SECRET)",
    re.IGNORECASE,
)
#: Common literal secret shapes (cloud access keys, GitHub/Stripe/OpenAI-style
#: tokens) that should be flagged even under an innocuous-looking name.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # OpenAI-style secret keys
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
)
_REDACTED = "***REDACTED***"


def redact_secrets(text: str) -> str:
    """Scrub anything that looks like a hardcoded secret out of `text`.

    Used before any command output, config snippet, or diagnostic message
    is written into a deployment log or `DeploymentEvent`, so a value that
    slips past generation-time validation still can't leak through
    observability.
    """

    redacted_lines: list[str] = []
    for line in text.splitlines():
        line_out = line
        match = re.match(r"^(\s*(?:ENV|ARG)?\s*)([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.+)$", line)
        if match and SECRET_NAME_PATTERN.search(match.group(2)):
            line_out = f"{match.group(1)}{match.group(2)}={_REDACTED}"
        else:
            for pattern in _SECRET_VALUE_PATTERNS:
                line_out = pattern.sub(_REDACTED, line_out)
        redacted_lines.append(line_out)
    return "\n".join(redacted_lines)


def _issue(field: str, severity: ErrorSeverity, message: str) -> ValidationIssue:
    return ValidationIssue(field=field, severity=severity, message=message)


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------

_ENV_ARG_LINE = re.compile(r"^\s*(ENV|ARG)\s+([A-Za-z_][A-Za-z0-9_]*)\s*[= ]\s*(.*)$")
_FROM_LINE = re.compile(r"^\s*FROM\s+(\S+)", re.IGNORECASE)


def validate_dockerfile(content: str) -> ValidationResult:
    """Validate Dockerfile contents. Blocking: missing FROM, hardcoded secret
    ENV/ARG values. Non-blocking: `:latest`/untagged base image, no USER,
    no HEALTHCHECK, piping a remote download straight into a shell."""

    issues: list[ValidationIssue] = []
    if not content or not content.strip():
        return ValidationResult(
            passed=False,
            issues=[_issue("dockerfile", ErrorSeverity.CRITICAL, "Dockerfile is empty.")],
        )

    lines = content.splitlines()
    from_lines = [_FROM_LINE.match(line) for line in lines]
    from_matches = [m for m in from_lines if m]

    if not from_matches:
        issues.append(_issue("dockerfile", ErrorSeverity.CRITICAL, "Missing a FROM instruction."))
    else:
        last_base = from_matches[-1].group(1)
        if ":" not in last_base or last_base.endswith(":latest"):
            issues.append(
                _issue(
                    "dockerfile",
                    ErrorSeverity.MEDIUM,
                    f"Base image '{last_base}' has no pinned version tag (uses 'latest' or "
                    "no tag); pin a specific version for reproducible builds.",
                )
            )

    has_user = any(line.strip().upper().startswith("USER ") for line in lines)
    if not has_user:
        issues.append(
            _issue(
                "dockerfile",
                ErrorSeverity.LOW,
                "No USER instruction found; the container will run as root by default.",
            )
        )

    has_healthcheck = any(line.strip().upper().startswith("HEALTHCHECK") for line in lines)
    if not has_healthcheck:
        issues.append(
            _issue(
                "dockerfile",
                ErrorSeverity.LOW,
                "No HEALTHCHECK instruction found; consider adding one.",
            )
        )

    for line in lines:
        match = _ENV_ARG_LINE.match(line)
        if not match:
            continue
        _, var_name, value = match.groups()
        value = value.strip().strip('"').strip("'")
        if (
            SECRET_NAME_PATTERN.search(var_name)
            and value
            and not (value.startswith("${") or value.startswith("$"))
        ):
            issues.append(
                _issue(
                    "dockerfile",
                    ErrorSeverity.CRITICAL,
                    f"Hardcoded secret value detected for '{var_name}' -- pass secrets at "
                    "runtime (env_file / compose secrets), never bake them into the image.",
                )
            )

    if re.search(r"curl[^\n]*\|\s*(sh|bash)", content, re.IGNORECASE) or re.search(
        r"wget[^\n]*\|\s*(sh|bash)", content, re.IGNORECASE
    ):
        issues.append(
            _issue(
                "dockerfile",
                ErrorSeverity.MEDIUM,
                "Piping a remote download straight into a shell is discouraged; "
                "verify and pin what's being executed instead.",
            )
        )

    blocking = any(i.severity in (ErrorSeverity.HIGH, ErrorSeverity.CRITICAL) for i in issues)
    return ValidationResult(passed=not blocking, issues=issues)


# ---------------------------------------------------------------------------
# docker-compose
# ---------------------------------------------------------------------------


def validate_docker_compose(content: str) -> ValidationResult:
    """Validate docker-compose YAML. Blocking: invalid YAML, no `services`,
    hardcoded secrets in a service's `environment`. Non-blocking: missing
    `healthcheck`, host-networked/privileged services, published port
    conflicts."""

    issues: list[ValidationIssue] = []
    if not content or not content.strip():
        return ValidationResult(
            passed=False,
            issues=[_issue("compose", ErrorSeverity.CRITICAL, "docker-compose file is empty.")],
        )

    try:
        doc: Any = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return ValidationResult(
            passed=False,
            issues=[_issue("compose", ErrorSeverity.CRITICAL, f"Invalid YAML: {exc}")],
        )

    if not isinstance(doc, dict) or not doc.get("services"):
        return ValidationResult(
            passed=False,
            issues=[
                _issue(
                    "compose",
                    ErrorSeverity.CRITICAL,
                    "docker-compose file has no top-level 'services' mapping.",
                )
            ],
        )

    services: dict[str, Any] = doc.get("services") or {}
    seen_host_ports: set[str] = set()

    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue

        env = service.get("environment")
        env_items: list[tuple[str, str]] = []
        if isinstance(env, dict):
            env_items = [(k, str(v)) for k, v in env.items()]
        elif isinstance(env, list):
            for entry in env:
                if isinstance(entry, str) and "=" in entry:
                    k, _, v = entry.partition("=")
                    env_items.append((k, v))

        for key, value in env_items:
            if SECRET_NAME_PATTERN.search(key) and value and not value.strip().startswith("${"):
                issues.append(
                    _issue(
                        f"services.{service_name}.environment",
                        ErrorSeverity.CRITICAL,
                        f"Hardcoded secret value detected for '{key}' in service "
                        f"'{service_name}' -- reference an env var (${{{key}}}) or "
                        "'env_file' instead.",
                    )
                )

        if not service.get("healthcheck"):
            issues.append(
                _issue(
                    f"services.{service_name}",
                    ErrorSeverity.LOW,
                    f"Service '{service_name}' has no healthcheck defined.",
                )
            )

        if service.get("privileged"):
            issues.append(
                _issue(
                    f"services.{service_name}",
                    ErrorSeverity.CRITICAL,
                    f"Service '{service_name}' runs with 'privileged: true'.",
                )
            )

        if service.get("network_mode") == "host":
            issues.append(
                _issue(
                    f"services.{service_name}.network_mode",
                    ErrorSeverity.HIGH,
                    "Host networking is not permitted for generated deployments.",
                )
            )

        if service.get("devices") or service.get("cap_add"):
            issues.append(
                _issue(
                    f"services.{service_name}",
                    ErrorSeverity.HIGH,
                    "Device passthrough and added Linux capabilities are not permitted.",
                )
            )

        for volume in service.get("volumes") or []:
            source = str(volume).split(":", 1)[0]
            if source.startswith("/") or source.startswith("~") or "docker.sock" in source:
                issues.append(
                    _issue(
                        f"services.{service_name}.volumes",
                        ErrorSeverity.CRITICAL,
                        "Host-path and Docker-socket mounts are not permitted.",
                    )
                )

        for port_mapping in service.get("ports") or []:
            host_part = str(port_mapping).split(":")[0]
            if host_part in seen_host_ports:
                issues.append(
                    _issue(
                        f"services.{service_name}.ports",
                        ErrorSeverity.MEDIUM,
                        f"Host port {host_part} is published by more than one service.",
                    )
                )
            seen_host_ports.add(host_part)

    blocking = any(i.severity in (ErrorSeverity.HIGH, ErrorSeverity.CRITICAL) for i in issues)
    return ValidationResult(passed=not blocking, issues=issues)


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------


def validate_env_vars(
    specs: list[EnvVarSpec], provided: dict[str, str] | None = None
) -> ValidationResult:
    """Validate a declared list of `EnvVarSpec` against what's actually
    provided. Blocking: a required var is missing with no default.
    Non-blocking: a secret-flagged var's value was passed in plaintext
    here rather than via a reference/secret store."""

    provided = provided or {}
    issues: list[ValidationIssue] = []

    for spec in specs:
        value = provided.get(spec.name, spec.default)
        if spec.required and (value is None or value == ""):
            issues.append(
                _issue(
                    f"env.{spec.name}",
                    ErrorSeverity.CRITICAL,
                    f"Required environment variable '{spec.name}' has no value and no default.",
                )
            )
        if spec.secret and spec.name in provided and provided[spec.name]:
            issues.append(
                _issue(
                    f"env.{spec.name}",
                    ErrorSeverity.MEDIUM,
                    f"'{spec.name}' is marked secret but a literal value was supplied "
                    "directly; prefer a secret store or env_file at deploy time.",
                )
            )
        if not spec.secret and SECRET_NAME_PATTERN.search(spec.name):
            issues.append(
                _issue(
                    f"env.{spec.name}",
                    ErrorSeverity.LOW,
                    f"'{spec.name}' looks like a secret name but isn't marked secret=True.",
                )
            )

    blocking = any(i.severity in (ErrorSeverity.HIGH, ErrorSeverity.CRITICAL) for i in issues)
    return ValidationResult(passed=not blocking, issues=issues)


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------


def validate_health_check_config(max_attempts: int, interval_seconds: float) -> ValidationResult:
    """Sanity-check health-check polling parameters before the pipeline
    spends any real time on them."""

    issues: list[ValidationIssue] = []
    if max_attempts < 1:
        issues.append(
            _issue(
                "health_check.max_attempts",
                ErrorSeverity.CRITICAL,
                "max_attempts must be at least 1.",
            )
        )
    if interval_seconds <= 0:
        issues.append(
            _issue(
                "health_check.interval_seconds",
                ErrorSeverity.CRITICAL,
                "interval_seconds must be positive.",
            )
        )
    if max_attempts * max(interval_seconds, 0) > 600:
        issues.append(
            _issue(
                "health_check",
                ErrorSeverity.LOW,
                "Health check budget exceeds 10 minutes; consider tightening it.",
            )
        )

    blocking = any(i.severity in (ErrorSeverity.HIGH, ErrorSeverity.CRITICAL) for i in issues)
    return ValidationResult(passed=not blocking, issues=issues)
