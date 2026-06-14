import logging

from openai import AsyncOpenAI

import config
from agents.base_agent import AgentResult, BaseAgent

logger = logging.getLogger(__name__)


class SummarizerAgent(BaseAgent):
    name = "summarizer"
    version = "1.0.0"

    _SYSTEM = (
        "You are a concise financial news analyst. "
        "When given a block of text, extract the three most important points "
        "relevant to investors. Respond with exactly 3 bullet points, each on "
        "its own line starting with '• '. Be factual and terse — no fluff."
    )

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            base_url=config.LMSTUDIO_BASE_URL,
            api_key="lm-studio",
        )

    async def run(self, input: dict) -> AgentResult:
        text: str = input.get("text", "").strip()
        if not text:
            return AgentResult(
                agent=self.name,
                version=self.version,
                output="No text provided.",
                confidence=0.0,
                metadata={"error": "empty input"},
            )

        try:
            response = await self._client.chat.completions.create(
                model=config.LLM_MODEL,
                max_tokens=512,
                messages=[
                    {"role": "user", "content": f"{self._SYSTEM}\n\n{text}"},
                ],
            )
            summary = (response.choices[0].message.content or "").strip()
            return AgentResult(
                agent=self.name,
                version=self.version,
                output=summary,
                confidence=0.9,
                metadata={"input_length": len(text)},
            )
        except Exception as exc:
            logger.error("SummarizerAgent failed: %s", exc)
            return self._error_result(exc)
