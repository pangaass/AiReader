"""Minimal module-agent interface used by the orchestrator."""

from __future__ import annotations

from abc import ABC, abstractmethod

from paper_visualizer.protocol import TaskEnvelope, TaskResult


class ModuleAgent(ABC):
    name: str

    @abstractmethod
    def run(self, task: TaskEnvelope) -> TaskResult:
        raise NotImplementedError


class VerificationError(RuntimeError):
    """Raised when a module output fails a blocking quality gate."""

