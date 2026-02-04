"""OpenAI LLM integration service using the Responses API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

import structlog
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from votebot.config import Settings, get_settings

if TYPE_CHECKING:
    from votebot.services.bill_votes import BillVotesService

logger = structlog.get_logger()


@dataclass
class WebSearchCitation:
    """A citation from web search results."""

    url: str
    title: str
    snippet: str | None = None


@dataclass
class BillVotesToolResult:
    """Result from the bill votes tool."""

    jurisdiction: str
    session: str
    bill_identifier: str
    found: bool
    votes_summary: str | None = None
    cached: bool = False


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
    bill_votes_tool_used: bool = False
    bill_votes_result: BillVotesToolResult | None = None


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

    def _build_tools(
        self,
        enable_web_search: bool = False,
        enable_bill_votes: bool = False,
    ) -> list[dict] | None:
        """Build the tools array for the Responses API."""
        tools = []

        if self.settings.web_search_enabled and enable_web_search:
            web_search_tool = {
                "type": "web_search_preview",
                "search_context_size": self.settings.web_search_context_size,
            }
            tools.append(web_search_tool)

        if self.settings.bill_votes_tool_enabled and enable_bill_votes:
            bill_votes_tool = {
                "type": "function",
                "name": "get_bill_votes",
                "description": (
                    "Retrieve voting records for a specific bill from OpenStates. "
                    "Use this when asked about how legislators voted on a bill, vote counts, "
                    "or who supported/opposed a bill. Only use if the vote information is not "
                    "already in the provided context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "jurisdiction": {
                            "type": "string",
                            "description": (
                                "Two-letter state code (e.g., 'fl' for Florida, 'ca' for California, "
                                "'tx' for Texas) or 'us' for federal legislation."
                            ),
                        },
                        "session": {
                            "type": "string",
                            "description": (
                                "Legislative session identifier. For states, this is usually the year "
                                "(e.g., '2024', '2025'). For federal, use Congress number (e.g., '118', '119')."
                            ),
                        },
                        "bill_identifier": {
                            "type": "string",
                            "description": (
                                "The bill number without spaces (e.g., 'HB1234', 'SB567', 'HR2'). "
                                "Include the bill type prefix (HB, SB, HR, S, etc.)."
                            ),
                        },
                    },
                    "required": ["jurisdiction", "session", "bill_identifier"],
                },
            }
            tools.append(bill_votes_tool)

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

    def _extract_function_calls(self, response: Any) -> list[dict]:
        """Extract function calls from the response."""
        function_calls = []
        try:
            if hasattr(response, 'output') and response.output:
                for item in response.output:
                    if hasattr(item, 'type') and item.type == 'function_call':
                        function_calls.append({
                            "id": getattr(item, 'id', ''),
                            "call_id": getattr(item, 'call_id', ''),
                            "name": getattr(item, 'name', ''),
                            "arguments": getattr(item, 'arguments', '{}'),
                        })
        except Exception as e:
            logger.debug(f"Error extracting function calls: {e}")
        return function_calls

    async def _execute_bill_votes_tool(
        self,
        arguments: dict,
        bill_votes_service: BillVotesService,
    ) -> tuple[str, BillVotesToolResult]:
        """Execute the bill votes tool and return the result."""
        jurisdiction = arguments.get("jurisdiction", "").lower()
        session = arguments.get("session", "")
        bill_identifier = arguments.get("bill_identifier", "")

        logger.info(
            "Executing bill votes tool",
            jurisdiction=jurisdiction,
            session=session,
            bill_identifier=bill_identifier,
        )

        try:
            result = await bill_votes_service.get_bill_votes(
                jurisdiction=jurisdiction,
                session=session,
                bill_identifier=bill_identifier,
            )

            if result and result.votes:
                # Format the votes for the LLM
                votes_text = bill_votes_service.format_votes_document(result)
                tool_result = BillVotesToolResult(
                    jurisdiction=jurisdiction,
                    session=session,
                    bill_identifier=bill_identifier,
                    found=True,
                    votes_summary=votes_text,
                    cached=result.cached,
                )
                return votes_text, tool_result
            else:
                no_votes_msg = (
                    f"No voting records found for {bill_identifier} in {jurisdiction.upper()} "
                    f"session {session}. The bill may not have been voted on yet, or the "
                    "bill identifier may be incorrect."
                )
                tool_result = BillVotesToolResult(
                    jurisdiction=jurisdiction,
                    session=session,
                    bill_identifier=bill_identifier,
                    found=False,
                )
                return no_votes_msg, tool_result

        except Exception as e:
            logger.error("Bill votes tool execution failed", error=str(e))
            error_msg = f"Error retrieving vote data: {str(e)}"
            tool_result = BillVotesToolResult(
                jurisdiction=jurisdiction,
                session=session,
                bill_identifier=bill_identifier,
                found=False,
            )
            return error_msg, tool_result

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
        enable_bill_votes: bool = False,
        bill_votes_service: BillVotesService | None = None,
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
            enable_bill_votes: Whether to enable bill votes lookup tool
            bill_votes_service: Service instance for bill votes lookup
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
        tools = self._build_tools(enable_web_search, enable_bill_votes)

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
            bill_votes_enabled=enable_bill_votes,
            has_previous_response=previous_response_id is not None,
        )

        # Track tool usage
        bill_votes_tool_used = False
        bill_votes_result: BillVotesToolResult | None = None
        total_tokens = 0

        # Make the API call (with function calling loop)
        max_iterations = 3  # Prevent infinite loops
        for iteration in range(max_iterations):
            response = await self.client.responses.create(**params)

            # Accumulate token usage
            if hasattr(response, 'usage') and response.usage:
                total_tokens += getattr(response.usage, 'total_tokens', 0)

            # Check for function calls
            function_calls = self._extract_function_calls(response)

            if not function_calls:
                # No function calls, we're done
                break

            # Process function calls
            function_results = []
            for fc in function_calls:
                if fc["name"] == "get_bill_votes" and bill_votes_service:
                    try:
                        args = json.loads(fc["arguments"])
                    except json.JSONDecodeError:
                        args = {}

                    result_text, tool_result = await self._execute_bill_votes_tool(
                        args, bill_votes_service
                    )
                    bill_votes_tool_used = True
                    bill_votes_result = tool_result

                    function_results.append({
                        "type": "function_call_output",
                        "call_id": fc["call_id"],
                        "output": result_text,
                    })

            if not function_results:
                break

            # Continue the conversation with function results
            # Use previous_response_id to maintain context
            params["previous_response_id"] = getattr(response, 'id', None)
            params["input"] = function_results

            logger.debug(
                "Continuing with function results",
                iteration=iteration + 1,
                function_count=len(function_results),
            )

        # Extract response content
        content = response.output_text if hasattr(response, 'output_text') else ""

        # Check if web search was used and extract citations
        web_search_used = self._check_web_search_used(response)
        web_citations = self._extract_web_citations(response) if web_search_used else []

        # Get response ID for stateful conversations
        response_id = getattr(response, 'id', None)

        logger.debug(
            "OpenAI Responses API response received",
            tokens_used=total_tokens,
            web_search_used=web_search_used,
            bill_votes_tool_used=bill_votes_tool_used,
            citations_count=len(web_citations),
            response_id=response_id,
        )

        return LLMResponse(
            content=content,
            tokens_used=total_tokens,
            model=self.model,
            finish_reason="completed",
            web_search_used=web_search_used,
            web_citations=web_citations,
            response_id=response_id,
            bill_votes_tool_used=bill_votes_tool_used,
            bill_votes_result=bill_votes_result,
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
