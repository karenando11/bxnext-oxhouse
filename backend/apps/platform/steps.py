"""Records the per-step entries stored on ``PipelineRun.steps``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from apps.platform.models import PipelineRun


class StepRecorder:
    def __init__(self, run: PipelineRun) -> None:
        self.run = run
        self.items: list[dict[str, Any]] = []

    def record(
        self,
        name: str,
        started: float,
        status: str = "ok",
        rows_in: int | None = None,
        rows_out: int | None = None,
        error: str = "",
    ) -> None:
        self.items.append(
            {
                "step_name": name,
                "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "status": status,
                "rows_in": rows_in,
                "rows_out": rows_out,
                "error_text": error,
            }
        )
        self.run.steps = self.items
        self.run.save(update_fields=["steps"])
