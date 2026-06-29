"""
Base agent class — all Shepherd AI pipeline agents inherit from this.

Provides:
  - Timing instrumentation (duration_ms on every AgentResult)
  - Uniform error handling → FAILED AgentResult instead of uncaught exception
  - Structured logging with agent name and message_id
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod

from src.models.agent_models import AgentResult, AgentStatus, AgentTask
from src.utils.logger import get_logger


class BaseAgent(ABC):
    """Abstract base for all pipeline agents."""

    def __init__(self, name: str):
        self.name = name
        self.logger = get_logger(f"agents.{name}")

    def run(self, task: AgentTask) -> AgentResult:
        """
        Execute the agent with timing and error handling.
        Subclasses must implement _execute().
        """
        t0 = time.monotonic()
        try:
            self.logger.info(
                "[%s] start  correlation_id=%s message_id=%s",
                self.name,
                task.correlation_id or "–",
                task.message_id,
            )
            result = self._execute(task)
            result.duration_ms = (time.monotonic() - t0) * 1000
            self.logger.info(
                "[%s] finish status=%s duration=%.0fms",
                self.name,
                result.status.value,
                result.duration_ms,
            )
            return result
        except Exception as exc:
            duration = (time.monotonic() - t0) * 1000
            self.logger.error(
                "[%s] unhandled error: %s",
                self.name,
                exc,
                exc_info=True,
            )
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.FAILED,
                error=str(exc),
                duration_ms=duration,
            )

    @abstractmethod
    def _execute(self, task: AgentTask) -> AgentResult:
        """Agent-specific logic. Must return an AgentResult."""
        ...
