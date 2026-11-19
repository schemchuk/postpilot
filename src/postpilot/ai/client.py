import os
from typing import Protocol

import anthropic
from anthropic.types import OutputConfigParam


class AIError(RuntimeError):
    """The model request did not produce a usable response."""


class TextModel(Protocol):
    def complete(
        self, system: str, user: str, schema: dict[str, object] | None = None
    ) -> str: ...


class ClaudeClient:
    def __init__(self, api_key: str, model: str | None = None) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = (
            model
            if model is not None
            else os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
        )

    def complete(
        self, system: str, user: str, schema: dict[str, object] | None = None
    ) -> str:
        options: OutputConfigParam | anthropic.Omit = anthropic.omit
        if schema is not None:
            options = {"format": {"type": "json_schema", "schema": schema}}
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config=options,
            )
        except anthropic.APIError as exc:
            raise AIError(f"Claude request failed ({type(exc).__name__}).") from None
        if response.stop_reason != "end_turn":
            raise AIError(f"Claude response stopped with {response.stop_reason}.")
        text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if not text:
            raise AIError("Claude returned no text.")
        return text
