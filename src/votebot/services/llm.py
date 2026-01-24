"""OpenAI LLM integration service."""

from dataclasses import dataclass
from typing import AsyncIterator

import structlog
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from votebot.config import Settings, get_settings

logger = structlog.get_logger()


@dataclass
class LLMResponse:
    """Response from the LLM."""

    content: str
    tokens_used: int
    model: str
    finish_reason: str | None = None


@dataclass
class StreamChunk:
    """A chunk of streamed LLM response."""

    text: str
    done: bool = False


class LLMService:
    """Service for interacting with OpenAI's LLM API."""

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the LLM service.

        Args:
            settings: Application settings. Uses default if not provided.
        """
        self.settings = settings or get_settings()
        self.client = AsyncOpenAI(
            api_key=self.settings.openai_api_key.get_secret_value()
        )
        self.model = self.settings.openai_model
        self.max_tokens = self.settings.openai_max_tokens
        self.temperature = self.settings.openai_temperature

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def complete(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        functions: list[dict] | None = None,
    ) -> LLMResponse:
        """
        Generate a completion using the LLM.

        Args:
            messages: List of message dicts with 'role' and 'content'
            system_prompt: Optional system prompt to prepend
            max_tokens: Override default max tokens
            temperature: Override default temperature
            functions: Optional function definitions for function calling

        Returns:
            LLMResponse with the generated content
        """
        # Prepare messages
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        # Build request parameters
        params = {
            "model": self.model,
            "messages": full_messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature or self.temperature,
        }

        if functions:
            params["tools"] = [{"type": "function", "function": f} for f in functions]
            params["tool_choice"] = "auto"

        logger.debug(
            "Calling OpenAI API",
            model=self.model,
            message_count=len(full_messages),
        )

        # Make the API call
        response = await self.client.chat.completions.create(**params)

        # Extract response data
        choice = response.choices[0]
        content = choice.message.content or ""

        # Handle function calls
        if choice.message.tool_calls:
            # Return function call info as JSON string for now
            import json

            tool_calls = [
                {
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                }
                for tc in choice.message.tool_calls
            ]
            content = json.dumps(tool_calls)

        tokens_used = response.usage.total_tokens if response.usage else 0

        logger.debug(
            "OpenAI API response received",
            tokens_used=tokens_used,
            finish_reason=choice.finish_reason,
        )

        return LLMResponse(
            content=content,
            tokens_used=tokens_used,
            model=response.model,
            finish_reason=choice.finish_reason,
        )

    async def stream(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """
        Stream a completion from the LLM.

        Args:
            messages: List of message dicts with 'role' and 'content'
            system_prompt: Optional system prompt to prepend
            max_tokens: Override default max tokens
            temperature: Override default temperature

        Yields:
            StreamChunk objects with text fragments
        """
        # Prepare messages
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        logger.debug(
            "Starting OpenAI stream",
            model=self.model,
            message_count=len(full_messages),
        )

        # Make the streaming API call
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            max_tokens=max_tokens or self.max_tokens,
            temperature=temperature or self.temperature,
            stream=True,
        )

        async for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta.content:
                    yield StreamChunk(text=delta.content, done=False)

                # Check for completion
                if chunk.choices[0].finish_reason:
                    yield StreamChunk(text="", done=True)
                    break

    async def health_check(self) -> bool:
        """
        Check if the OpenAI API is accessible.

        Returns:
            True if the API is healthy, raises exception otherwise
        """
        try:
            # Make a minimal API call to verify connectivity
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
            )
            return True
        except Exception as e:
            logger.error("OpenAI health check failed", error=str(e))
            raise


class LLMServiceFactory:
    """Factory for creating LLM service instances."""

    _instance: LLMService | None = None

    @classmethod
    def get_instance(cls, settings: Settings | None = None) -> LLMService:
        """Get or create a singleton LLM service instance."""
        if cls._instance is None:
            cls._instance = LLMService(settings)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance (useful for testing)."""
        cls._instance = None
