"""OpenAI LLM integration service using the Responses API."""

from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import structlog
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from votebot.config import Settings, get_settings

logger = structlog.get_logger()


@dataclass
class WebSearchCitation:
    """A citation from web search results."""

    url: str
    title: str
    snippet: str | None = None


@dataclass
class LLMResponse:
    """Response from the LLM."""

    content: str
    tokens_used: int
    model: str
    finish_reason: str | None = None
    web_search_used: bool = False
    web_citations: list[WebSearchCitation] = field(default_factory=list)
    response_id: str | None = None  # For stateful conversations


@dataclass
class StreamChunk:
    """A chunk of streamed LLM response."""

    text: str
    done: bool = False


class LLMService:
    """
    Service for interacting with OpenAI's Responses API.

    Uses the Responses API for:
    - Stateful conversations
    - Built-in web search tool
    - Better tool calling with GPT-4.1
    """

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

    def _build_tools(self, enable_web_search: bool = False) -> list[dict] | None:
        """Build the tools array for the Responses API."""
        if not enable_web_search:
            return None

        tools = []

        if self.settings.web_search_enabled and enable_web_search:
            web_search_tool = {
                "type": "web_search_preview",
                "search_context_size": self.settings.web_search_context_size,
            }
            tools.append(web_search_tool)

        return tools if tools else None

    def _extract_web_citations(self, response: Any) -> list[WebSearchCitation]:
        """Extract web citations from the response output."""
        citations = []

        try:
            # The Responses API includes annotations with URL citations
            if hasattr(response, 'output') and response.output:
                for item in response.output:
                    # Check for message content with annotations
                    if hasattr(item, 'content') and item.content:
                        for content_block in item.content:
                            if hasattr(content_block, 'annotations'):
                                for annotation in content_block.annotations:
                                    if hasattr(annotation, 'url'):
                                        citations.append(WebSearchCitation(
                                            url=annotation.url,
                                            title=getattr(annotation, 'title', ''),
                                            snippet=getattr(annotation, 'text', None),
                                        ))
        except Exception as e:
            logger.debug(f"Error extracting web citations: {e}")

        return citations

    def _check_web_search_used(self, response: Any) -> bool:
        """Check if web search was used in the response."""
        try:
            if hasattr(response, 'output') and response.output:
                for item in response.output:
                    if hasattr(item, 'type') and item.type == 'web_search_call':
                        return True
        except Exception as e:
            logger.debug(f"Error checking web search usage: {e}")
        return False

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
        enable_web_search: bool = False,
        previous_response_id: str | None = None,
    ) -> LLMResponse:
        """
        Generate a completion using the Responses API.

        Args:
            messages: List of message dicts with 'role' and 'content'
            system_prompt: Optional system prompt (instructions)
            max_tokens: Override default max tokens
            temperature: Override default temperature
            enable_web_search: Whether to enable web search tool
            previous_response_id: ID of previous response for stateful conversation

        Returns:
            LLMResponse with the generated content
        """
        # Build the input from messages
        # For Responses API, we combine messages into the input
        if len(messages) == 1:
            input_text = messages[0].get("content", "")
        else:
            # Format multi-turn as conversation
            input_parts = []
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "user":
                    input_parts.append(f"User: {content}")
                elif role == "assistant":
                    input_parts.append(f"Assistant: {content}")
            input_text = "\n\n".join(input_parts)

        # Build tools
        tools = self._build_tools(enable_web_search)

        # Build request parameters
        params: dict[str, Any] = {
            "model": self.model,
            "input": input_text,
        }

        if system_prompt:
            params["instructions"] = system_prompt

        if tools:
            params["tools"] = tools

        if previous_response_id:
            params["previous_response_id"] = previous_response_id

        if temperature is not None:
            params["temperature"] = temperature
        elif self.temperature:
            params["temperature"] = self.temperature

        if max_tokens:
            params["max_output_tokens"] = max_tokens

        logger.debug(
            "Calling OpenAI Responses API",
            model=self.model,
            web_search_enabled=enable_web_search,
            has_previous_response=previous_response_id is not None,
        )

        # Make the API call
        response = await self.client.responses.create(**params)

        # Extract response content
        content = response.output_text if hasattr(response, 'output_text') else ""

        # Check if web search was used and extract citations
        web_search_used = self._check_web_search_used(response)
        web_citations = self._extract_web_citations(response) if web_search_used else []

        # Get token usage
        tokens_used = 0
        if hasattr(response, 'usage') and response.usage:
            tokens_used = getattr(response.usage, 'total_tokens', 0)

        # Get response ID for stateful conversations
        response_id = getattr(response, 'id', None)

        logger.debug(
            "OpenAI Responses API response received",
            tokens_used=tokens_used,
            web_search_used=web_search_used,
            citations_count=len(web_citations),
            response_id=response_id,
        )

        return LLMResponse(
            content=content,
            tokens_used=tokens_used,
            model=self.model,
            finish_reason="completed",
            web_search_used=web_search_used,
            web_citations=web_citations,
            response_id=response_id,
        )

    async def complete_with_fallback(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        rag_confidence: float = 1.0,
        previous_response_id: str | None = None,
        page_context_type: str | None = None,
    ) -> LLMResponse:
        """
        Generate a completion, falling back to web search if RAG confidence is low.

        Args:
            messages: List of message dicts with 'role' and 'content'
            system_prompt: Optional system prompt
            max_tokens: Override default max tokens
            temperature: Override default temperature
            rag_confidence: Confidence score from RAG retrieval (0-1)
            previous_response_id: ID of previous response for stateful conversation
            page_context_type: Type of page context ('bill', 'legislator', 'organization', 'general')

        Returns:
            LLMResponse with the generated content
        """
        # Use higher threshold for legislator/organization queries (triggers web search more easily)
        if page_context_type == "legislator":
            threshold = self.settings.web_search_legislator_confidence_threshold
        elif page_context_type == "organization":
            threshold = self.settings.web_search_organization_confidence_threshold
        else:
            threshold = self.settings.web_search_confidence_threshold

        # Determine if we should enable web search
        enable_web_search = (
            self.settings.web_search_on_low_confidence and
            rag_confidence < threshold
        )

        if enable_web_search:
            logger.info(
                "Enabling web search due to low RAG confidence",
                rag_confidence=rag_confidence,
                threshold=threshold,
                page_context_type=page_context_type,
            )

        return await self.complete(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            enable_web_search=enable_web_search,
            previous_response_id=previous_response_id,
        )

    async def stream(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        enable_web_search: bool = False,
    ) -> AsyncIterator[StreamChunk]:
        """
        Stream a completion from the LLM.

        Uses Responses API when web search is enabled, Chat Completions otherwise.

        Args:
            messages: List of message dicts with 'role' and 'content'
            system_prompt: Optional system prompt to prepend
            max_tokens: Override default max tokens
            temperature: Override default temperature
            enable_web_search: Enable OpenAI web search tool

        Yields:
            StreamChunk objects with text fragments
        """
        # Use Responses API for web search streaming
        if enable_web_search:
            async for chunk in self._stream_with_responses_api(
                messages=messages,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            ):
                yield chunk
            return

        # For non-web-search, use Chat Completions API (better streaming support)
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        logger.debug(
            "Starting OpenAI stream (Chat Completions)",
            model=self.model,
            message_count=len(full_messages),
        )

        # Make the streaming API call using Chat Completions
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

    async def _stream_with_responses_api(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """
        Stream using Responses API with web search enabled.

        Args:
            messages: List of message dicts
            system_prompt: Optional system prompt
            max_tokens: Override max tokens
            temperature: Override temperature

        Yields:
            StreamChunk objects
        """
        # Build input from messages (get the last user message)
        user_message = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_message = msg.get("content", "")
                break

        # Build conversation context from history
        conversation_context = ""
        for msg in messages[:-1]:  # All but last message
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                conversation_context += f"User: {content}\n"
            elif role == "assistant":
                conversation_context += f"Assistant: {content}\n"

        # Combine system prompt with conversation context
        instructions = system_prompt or ""
        if conversation_context:
            instructions = f"{instructions}\n\nPrevious conversation:\n{conversation_context}"

        logger.info(
            "Starting OpenAI stream with web search (Responses API)",
            model=self.model,
            web_search=True,
        )

        try:
            # Use Responses API with streaming
            async with self.client.responses.stream(
                model=self.model,
                input=user_message,
                instructions=instructions if instructions else None,
                max_output_tokens=max_tokens or self.max_tokens,
                temperature=temperature or self.temperature,
                tools=[{"type": "web_search_preview"}],
            ) as stream:
                async for event in stream:
                    # Handle different event types from Responses API streaming
                    if hasattr(event, "type"):
                        if event.type == "response.output_text.delta":
                            if hasattr(event, "delta") and event.delta:
                                yield StreamChunk(text=event.delta, done=False)
                        elif event.type == "response.completed":
                            yield StreamChunk(text="", done=True)
                            return

        except Exception as e:
            logger.error("Responses API streaming error, falling back to Chat Completions", error=str(e))
            # Fallback to Chat Completions without web search
            full_messages = []
            if system_prompt:
                full_messages.append({"role": "system", "content": system_prompt})
            full_messages.extend(messages)

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
            response = await self.client.responses.create(
                model=self.model,
                input="ping",
                max_output_tokens=16,  # Minimum allowed by the API
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
