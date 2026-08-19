"""Enumerations shared across the AHSEA state layer.

These enums are intentionally centralized so that every model, service, and
orchestration component references the exact same vocabulary. Do not
duplicate these definitions elsewhere.
"""

from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    """Lifecycle status of a unit of work in the task DAG."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


class AgentStatus(str, Enum):
    """Lifecycle status of an agent instance."""

    IDLE = "idle"
    PLANNING = "planning"
    WORKING = "working"
    WAITING = "waiting"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentType(str, Enum):
    """Role of an agent within the hierarchy."""

    CTO = "cto"
    MANAGER = "manager"
    WORKER = "worker"
    SYSTEM_AGENT = "system_agent"


class SystemAgentKind(str, Enum):
    """Specialization for agents of type SYSTEM_AGENT."""

    INTEGRATION = "integration"
    QA = "qa"
    ERROR_ANALYZER = "error_analyzer"
    DEPLOYMENT = "deployment"
    SELF_HEALING = "self_healing"


class ArtifactType(str, Enum):
    """Kind of artifact produced by an agent."""

    SOURCE_FILE = "source_file"
    TEST_FILE = "test_file"
    CONFIG_FILE = "config_file"
    DOCUMENTATION = "documentation"
    DIAGRAM = "diagram"
    REPORT = "report"
    OTHER = "other"


class ContractType(str, Enum):
    """Kind of contract enforced between subsystems."""

    API = "api"
    DATABASE = "database"
    ENVIRONMENT = "environment"


class DeploymentStage(str, Enum):
    """Stage of the deployment pipeline."""

    NOT_STARTED = "not_started"
    PREPARING = "preparing"
    BUILDING = "building"
    AWAITING_APPROVAL = "awaiting_approval"
    DEPLOYING = "deploying"
    VERIFYING = "verifying"
    DEPLOYED = "deployed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    #: Deployment was not attempted because a required tool (currently:
    #: the `docker` CLI) is not installed on this host. Distinct from
    #: FAILED -- nothing was attempted, so nothing needs a repair task or
    #: self-healing retry. Hardware/host-constrained environments
    #: (see the project's HARDWARE CONSTRAINTS notes) are expected to hit
    #: this on plain dev machines without Docker.
    SKIPPED = "skipped"


class ErrorSeverity(str, Enum):
    """Severity classification for a recorded error."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EventLevel(str, Enum):
    """Verbosity/level classification for events."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class RequirementPriority(str, Enum):
    """Priority classification for a requirement."""

    MUST_HAVE = "must_have"
    SHOULD_HAVE = "should_have"
    NICE_TO_HAVE = "nice_to_have"


class RequirementStatus(str, Enum):
    """Lifecycle status of a requirement."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    SATISFIED = "satisfied"
    REJECTED = "rejected"


class TestOutcome(str, Enum):
    """Outcome of an individual test run."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class TaskComplexity(str, Enum):
    """Relative implementation complexity of a task, set at planning time."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
