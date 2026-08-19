"""Metadata-only tracing; prompt and tool argument content are never stored."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db.config import DatabaseSettings, get_database_settings
from app.db.models import ObservabilityEventORM
from app.db.session import session_scope


class TraceContext(BaseModel):
    project_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    request_id: str | None = None


class ObservabilityService:
    """Records lifecycle events and provides aggregated metrics APIs."""

    def __init__(self, settings: DatabaseSettings | None = None) -> None:
        self.settings = settings or get_database_settings()

    async def record(self, event_type: str, context: TraceContext | None = None, *, task_type: str | None = None, model: str | None = None, duration_seconds: float | None = None, success: bool | None = None, attributes: dict[str, str | int | float | bool] | None = None) -> str:
        context = context or TraceContext()
        row = ObservabilityEventORM(event_id=f"obs_{uuid.uuid4().hex}", project_id=context.project_id, agent_id=context.agent_id, task_id=context.task_id, request_id=context.request_id, event_type=event_type, task_type=task_type, model=model, duration_seconds=duration_seconds, success=success, attributes=attributes or {})
        async with session_scope(self.settings) as session:
            session.add(row)
        return row.event_id

    async def metrics(self, project_id: str | None = None) -> dict:
        async with session_scope(self.settings) as session:
            stmt = select(ObservabilityEventORM)
            if project_id:
                stmt = stmt.where(ObservabilityEventORM.project_id == project_id)
            rows = list((await session.execute(stmt)).scalars())
        by_type: dict[str, dict] = {}
        for row in rows:
            bucket = by_type.setdefault(row.event_type, {"count": 0, "success": 0, "failed": 0, "total_duration_seconds": 0.0})
            bucket["count"] += 1
            bucket["success"] += int(row.success is True)
            bucket["failed"] += int(row.success is False)
            bucket["total_duration_seconds"] += row.duration_seconds or 0.0
        llm = [row for row in rows if row.event_type == "llm_call"]
        return {"project_id": project_id, "generated_at": datetime.utcnow().isoformat(), "event_count": len(rows), "by_event_type": by_type, "llm_calls": [{"task_type": row.task_type, "model": row.model, "duration_seconds": row.duration_seconds, "success": row.success, "project_id": row.project_id, "agent_id": row.agent_id, "task_id": row.task_id, "request_id": row.request_id} for row in llm]}

    async def agent_scorecards(
        self, project_id: str | None = None, *, recent_days: int = 7
    ) -> list[dict]:
        """Phase 23: per-agent performance over time.

        Groups every recorded `llm_call` event (the only event type that
        currently carries `agent_id`) by `agent_id` and reports success
        rate, average duration, the models/task types that agent used, and
        a simple trend signal comparing the last `recent_days` of activity
        against everything before that -- enough to answer "is this agent
        getting better or worse over the life of the project" without
        requiring a separate time-series store.

        Agents with no `agent_id` (e.g. system-level LLM calls not
        attributed to a specific agent) are excluded; there is nothing to
        scorecard for them.
        """
        async with session_scope(self.settings) as session:
            stmt = select(ObservabilityEventORM).where(
                ObservabilityEventORM.event_type == "llm_call",
                ObservabilityEventORM.agent_id.is_not(None),
            )
            if project_id:
                stmt = stmt.where(ObservabilityEventORM.project_id == project_id)
            rows = list((await session.execute(stmt)).scalars())

        by_agent: dict[str, list[ObservabilityEventORM]] = {}
        for row in rows:
            by_agent.setdefault(row.agent_id, []).append(row)

        now = datetime.utcnow()
        cutoff = now - timedelta(days=recent_days)
        cards: list[dict] = []
        for agent_id, agent_rows in by_agent.items():
            agent_rows.sort(key=lambda r: r.created_at)
            count = len(agent_rows)
            success_count = sum(1 for r in agent_rows if r.success is True)
            failure_count = sum(1 for r in agent_rows if r.success is False)
            durations = [r.duration_seconds for r in agent_rows if r.duration_seconds is not None]
            recent = [r for r in agent_rows if r.created_at >= cutoff]
            prior = [r for r in agent_rows if r.created_at < cutoff]
            trend = self._trend(prior, recent)
            cards.append(
                {
                    "agent_id": agent_id,
                    "event_count": count,
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "success_rate": success_count / count if count else None,
                    "avg_duration_seconds": sum(durations) / len(durations) if durations else None,
                    "models_used": sorted({r.model for r in agent_rows if r.model}),
                    "task_types": sorted({r.task_type for r in agent_rows if r.task_type}),
                    "first_seen": agent_rows[0].created_at.isoformat(),
                    "last_seen": agent_rows[-1].created_at.isoformat(),
                    "trend": trend,
                }
            )
        cards.sort(key=lambda c: c["event_count"], reverse=True)
        return cards

    async def task_type_model_scorecards(self, project_id: str | None = None) -> list[dict]:
        """Phase 23: success rate and duration broken down by (task_type,
        model) -- the raw data a future model-routing feedback loop (e.g.
        "route DEBUGGING tasks away from a model with a low success rate")
        would consume. This method only reports the numbers; it does not
        act on them -- that routing decision is a larger design change
        left for a follow-up phase (see REPORT.md).
        """
        async with session_scope(self.settings) as session:
            stmt = select(ObservabilityEventORM).where(
                ObservabilityEventORM.event_type == "llm_call",
                ObservabilityEventORM.task_type.is_not(None),
                ObservabilityEventORM.model.is_not(None),
            )
            if project_id:
                stmt = stmt.where(ObservabilityEventORM.project_id == project_id)
            rows = list((await session.execute(stmt)).scalars())

        buckets: dict[tuple[str, str], list[ObservabilityEventORM]] = {}
        for row in rows:
            buckets.setdefault((row.task_type, row.model), []).append(row)

        cards: list[dict] = []
        for (task_type, model), bucket_rows in buckets.items():
            count = len(bucket_rows)
            success_count = sum(1 for r in bucket_rows if r.success is True)
            durations = [r.duration_seconds for r in bucket_rows if r.duration_seconds is not None]
            cards.append(
                {
                    "task_type": task_type,
                    "model": model,
                    "event_count": count,
                    "success_rate": success_count / count if count else None,
                    "avg_duration_seconds": sum(durations) / len(durations) if durations else None,
                }
            )
        cards.sort(key=lambda c: (c["task_type"], c["model"]))
        return cards

    @staticmethod
    def _trend(
        prior: list[ObservabilityEventORM], recent: list[ObservabilityEventORM]
    ) -> str:
        """Compare recent vs. prior success rate. Needs at least 3 events
        in each window to say anything more specific than
        `insufficient_data` -- small samples swing wildly and would be
        misleading labeled as a real trend.
        """
        if len(prior) < 3 or len(recent) < 3:
            return "insufficient_data"
        prior_rate = sum(1 for r in prior if r.success is True) / len(prior)
        recent_rate = sum(1 for r in recent if r.success is True) / len(recent)
        delta = recent_rate - prior_rate
        if delta >= 0.1:
            return "improving"
        if delta <= -0.1:
            return "declining"
        return "stable"
