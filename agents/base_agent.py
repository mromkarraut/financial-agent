from abc import ABC, abstractmethod
from typing import Any, TypedDict


class AgentResult(TypedDict):
    agent: str
    version: str
    output: str
    confidence: float   # 0.0–1.0; 0 signals a hard failure
    metadata: dict[str, Any]


class BaseAgent(ABC):
    name: str = "base"
    version: str = "1.0.0"

    @abstractmethod
    async def run(self, input: dict) -> AgentResult:
        """Execute the agent with the given input dict and return an AgentResult."""

    def _error_result(self, error: Exception) -> AgentResult:
        return AgentResult(
            agent=self.name,
            version=self.version,
            output="",
            confidence=0.0,
            metadata={"error": str(error), "error_type": type(error).__name__},
        )
