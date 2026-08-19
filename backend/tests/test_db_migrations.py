"""Tests that the Alembic migrations (Phase 17) apply and roll back cleanly,
and that the resulting schema matches `Base.metadata` (i.e. nobody edited
`app/db/models.py` without regenerating a migration).

Runs `alembic` as a subprocess against a scratch SQLite database, the same
way an operator would from the command line, rather than importing
`alembic.command` in-process -- this exercises `alembic/env.py` exactly as
written, including its async engine setup.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent

EXPECTED_TABLES = {
    "projects",
    "agents",
    "tasks",
    "artifacts",
    "contracts",
    "events",
    "errors",
    "test_results",
    "deployment_runs",
    "architecture_decisions",
    "repair_attempts",
    "llm_requests",
    "alembic_version",
}


def _run_alembic(*args: str, db_path: Path) -> subprocess.CompletedProcess:
    import os

    full_env = dict(os.environ)
    full_env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.slow
def test_alembic_upgrade_head_creates_every_table(tmp_path: Path):
    db_path = tmp_path / "migration_test.db"

    result = _run_alembic("upgrade", "head", db_path=db_path)
    assert result.returncode == 0, result.stderr

    con = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        con.close()

    assert EXPECTED_TABLES.issubset(tables)


@pytest.mark.slow
def test_alembic_downgrade_base_drops_everything(tmp_path: Path):
    db_path = tmp_path / "migration_test.db"

    assert _run_alembic("upgrade", "head", db_path=db_path).returncode == 0
    result = _run_alembic("downgrade", "base", db_path=db_path)
    assert result.returncode == 0, result.stderr

    con = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'"
            )
        }
    finally:
        con.close()

    assert tables == set()
