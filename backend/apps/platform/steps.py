"""Records the per-step entries stored on ``PipelineRun.steps``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from apps.platform.models import PipelineRun


class StepRecorder:
    def __init__(self, run: PipelineRun) -> None:
        self.run = run
        self.items: list[dict[str, Any]] = list(run.steps or [])

    def record(
        self,
        name: str,
        started: float,
        status: str = "ok",
        rows_in: int | None = None,
        rows_out: int | None = None,
        error: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "step_name": name,
            "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "status": status,
            "rows_in": rows_in,
            "rows_out": rows_out,
            "error_text": error,
        }
        if detail:
            entry["detail"] = detail
        self.items.append(entry)
        self.run.steps = self.items
        self.run.save(update_fields=["steps"])
