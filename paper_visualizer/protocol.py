"""Shared task-envelope types for module agents."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


Status = Literal["passed", "needs_review", "retryable_error", "fatal_error"]


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    sha256: str
    schema_version: str


@dataclass
class TaskEnvelope:
    run_id: str
    paper_id: str
    module: str
    task_id: str
    input_artifacts: list[ArtifactRef] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    attempt: int = 1
    deadline_seconds: int = 600

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskResult:
    status: Status
    output_artifacts: list[ArtifactRef] = field(default_factory=list)
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    review_items: list[dict[str, Any]] = field(default_factory=list)
    retry_hint: str | None = None
    agent_role: str = ""
    reviewed_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ensure_within(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    resolved.relative_to(root.expanduser().resolve())
    return resolved

