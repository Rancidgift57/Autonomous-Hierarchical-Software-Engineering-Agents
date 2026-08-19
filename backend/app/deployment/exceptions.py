"""Exceptions for the Deployment System (Phase 15)."""

from __future__ import annotations


class DeploymentError(Exception):
    """Base exception for all deployment-pipeline errors."""


class QAGateNotPassedError(DeploymentError):
    """Raised when `run_pipeline` is invoked without a passing QA gate."""


class ValidationFailedError(DeploymentError):
    """Raised when a generated deployment artifact fails blocking validation."""


class ApprovalRequiredError(DeploymentError):
    """Raised when `deploy()` is called without a recorded human approval.

    This is the structural enforcement of "never deploy to production
    without explicit approval" -- there is no code path in
    `DeploymentManager.deploy` that can reach an actual `docker compose up`
    call while `state.deployment.approved_by` is unset.
    """


class DeploymentPipelineFailedError(DeploymentError):
    """Raised when an automated pipeline stage (build/start/health/smoke)
    fails and the pipeline cannot proceed."""
