"""Permission levels for the Agent Tool System (Phase 9).

Every tool declares a single `required_permission`. An agent may call a
tool only if its granted permission set contains that permission (or the
blanket `ADMIN` permission, which is a superset of every other level).
Nothing in this module executes anything -- it only defines the
vocabulary that `app.tools.base.BaseTool` and `ToolExecutor` enforce.
"""

from __future__ import annotations

from enum import Enum


class Permission(str, Enum):
    """A single capability an agent can be granted over the tool system."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    GIT = "git"
    DEPLOY = "deploy"
    ADMIN = "admin"


def permission_satisfied(required: Permission, granted: frozenset[Permission]) -> bool:
    """Return True if `granted` authorizes a tool requiring `required`.

    `Permission.ADMIN` is a blanket override: any agent holding it may call
    any tool regardless of the tool's declared requirement.
    """

    return required in granted or Permission.ADMIN in granted


#: Convenience presets. Concrete agent constructors are free to build their
#: own permission sets -- these are just the common cases used by Phase 7/8
#: agents so the "least privilege" intent is visible in one place.
NO_PERMISSIONS: frozenset[Permission] = frozenset()
READ_ONLY: frozenset[Permission] = frozenset({Permission.READ})
# QA needs to execute existing test/lint commands but must never modify the
# workspace. Keeping this separate from WORKER_DEFAULT prevents a QA agent
# from silently acquiring write, Git, or deployment authority.
QA_PIPELINE_DEFAULT: frozenset[Permission] = frozenset(
    {Permission.READ, Permission.EXECUTE}
)
WORKER_DEFAULT: frozenset[Permission] = frozenset(
    {Permission.READ, Permission.WRITE, Permission.EXECUTE, Permission.GIT}
)
#: The Deployment Manager's default grant: it needs to read/write generated
#: deployment artifacts (Dockerfile, compose file, scripts), run build/start/
#: health-check commands, and -- distinct from ordinary `EXECUTE` -- hold the
#: `DEPLOY` permission that actually starting/stopping containers requires.
#: It deliberately does not include `GIT` or `ADMIN`.
DEPLOYMENT_MANAGER_DEFAULT: frozenset[Permission] = frozenset(
    {Permission.READ, Permission.WRITE, Permission.EXECUTE, Permission.DEPLOY}
)
FULL_ACCESS: frozenset[Permission] = frozenset({Permission.ADMIN})
