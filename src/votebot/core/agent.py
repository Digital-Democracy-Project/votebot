"""Single conversational agent for VoteBot."""

import re
from dataclasses import dataclass, field
from typing import AsyncIterator

import structlog

from votebot.api.schemas.chat import (
    Citation,
    NavigationContext,
    PageContext,
    ResponseMetadata,
)
from votebot.config import Settings, get_settings
from votebot.core.prompts import build_system_prompt, format_retrieved_chunks
from votebot.core.retrieval import RetrievalService
from votebot.services.llm import LLMService, WebSearchCitation

logger = structlog.get_logger()


@dataclass
class AgentResult:
    """Result from the agent's message processing."""

    response: str
    citations: list[Citation]
    confidence: float
    requires_human: bool
    tokens_used: int
    retrieval_count: int
    cached: bool = False
    web_search_used: bool = False
    web_citations: list[WebSearchCitation] | None = None
    response_id: str | None = None  # For stateful conversations


@dataclass
class StreamChunkData:
    """Data for a streaming response chunk."""

    text: str
    done: bool = False
    citations: list[Citation] | None = None
    metadata: ResponseMetadata | None = None


class VoteBotAgent:
    """
    Single conversational agent that handles all VoteBot interactions.

    This agent:
    - Determines intent from message and page context
    - Retrieves relevant information using RAG
    - Generates grounded, neutral responses
    - Extracts citations from retrieved sources
    - Determines if human handoff is needed
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the agent.

        Args:
            settings: Application settings. Uses default if not provided.
        """
        self.settings = settings or get_settings()
        self.llm = LLMService(self.settings)
        self.retrieval = RetrievalService(self.settings)

    async def process_message(
        self,
        message: str,
        session_id: str,
        page_context: PageContext,
        navigation_context: NavigationContext | None = None,
        conversation_history: list[dict] | None = None,
    ) -> AgentResult:
        """
        Process a user message and generate a response.

        Args:
            message: The user's message
            session_id: Session identifier
            page_context: Context about the current page
            navigation_context: Optional navigation context
            conversation_history: Optional previous messages

        Returns:
            AgentResult with the response and metadata
        """
        logger.info(
            "Processing message",
            session_id=session_id,
            page_type=page_context.type,
            message_preview=message[:100],
        )

        # Step 1: Retrieve relevant context
        retrieval_result = await self.retrieval.retrieve(
            query=message,
            page_context=page_context,
        )

        # Step 2: Format retrieved context
        retrieved_context = format_retrieved_chunks(
            [
                {
                    "id": chunk.id,
                    "content": chunk.content,
                    "metadata": chunk.metadata,
                }
                for chunk in retrieval_result.chunks
            ]
        )

        # Step 3: Build page info for prompt
        page_info = self._extract_page_info(page_context)

        # Step 4: Build system prompt
        system_prompt = build_system_prompt(
            page_type=page_context.type,
            page_info=page_info,
            include_rag_context=True,
            retrieved_context=retrieved_context,
        )

        # Step 5: Build messages
        messages = self._build_messages(message, conversation_history)

        # Step 6: Calculate pre-LLM confidence based on retrieval quality
        rag_confidence = self._calculate_rag_confidence(retrieval_result)

        # Step 7: Generate response (with web search fallback if RAG confidence is low)
        llm_response = await self.llm.complete_with_fallback(
            messages=messages,
            system_prompt=system_prompt,
            rag_confidence=rag_confidence,
        )

        # Step 8: Extract citations from RAG
        citations = self._extract_citations(
            response=llm_response.content,
            retrieved_chunks=retrieval_result.chunks,
        )

        # Step 9: Calculate final confidence
        confidence = self._calculate_confidence(
            response=llm_response.content,
            retrieval_count=retrieval_result.total_retrieved,
            citations=citations,
            web_search_used=llm_response.web_search_used,
        )

        # Step 10: Check for human handoff
        requires_human = self._check_human_handoff(
            message=message,
            response=llm_response.content,
            confidence=confidence,
        )

        logger.info(
            "Message processed",
            session_id=session_id,
            tokens_used=llm_response.tokens_used,
            confidence=confidence,
            requires_human=requires_human,
            web_search_used=llm_response.web_search_used,
        )

        return AgentResult(
            response=llm_response.content,
            citations=citations,
            confidence=confidence,
            requires_human=requires_human,
            tokens_used=llm_response.tokens_used,
            retrieval_count=retrieval_result.total_retrieved,
            cached=False,
            web_search_used=llm_response.web_search_used,
            web_citations=llm_response.web_citations if llm_response.web_search_used else None,
            response_id=llm_response.response_id,
        )

    async def process_message_stream(
        self,
        message: str,
        session_id: str,
        page_context: PageContext,
        navigation_context: NavigationContext | None = None,
        conversation_history: list[dict] | None = None,
    ) -> AsyncIterator[StreamChunkData]:
        """
        Process a message and stream the response.

        Args:
            message: The user's message
            session_id: Session identifier
            page_context: Context about the current page
            navigation_context: Optional navigation context
            conversation_history: Optional previous messages

        Yields:
            StreamChunkData objects with text fragments
        """
        logger.info(
            "Processing message (streaming)",
            session_id=session_id,
            page_type=page_context.type,
        )

        # Step 1: Retrieve relevant context
        retrieval_result = await self.retrieval.retrieve(
            query=message,
            page_context=page_context,
        )

        # Step 2: Format retrieved context
        retrieved_context = format_retrieved_chunks(
            [
                {
                    "id": chunk.id,
                    "content": chunk.content,
                    "metadata": chunk.metadata,
                }
                for chunk in retrieval_result.chunks
            ]
        )

        # Step 3: Build page info and system prompt
        page_info = self._extract_page_info(page_context)
        system_prompt = build_system_prompt(
            page_type=page_context.type,
            page_info=page_info,
            include_rag_context=True,
            retrieved_context=retrieved_context,
        )

        # Step 4: Build messages
        messages = self._build_messages(message, conversation_history)

        # Step 5: Stream response
        full_response = ""
        async for chunk in self.llm.stream(
            messages=messages,
            system_prompt=system_prompt,
        ):
            full_response += chunk.text

            if chunk.done:
                # Extract citations from full response
                citations = self._extract_citations(
                    response=full_response,
                    retrieved_chunks=retrieval_result.chunks,
                )

                confidence = self._calculate_confidence(
                    response=full_response,
                    retrieval_count=retrieval_result.total_retrieved,
                    citations=citations,
                )

                yield StreamChunkData(
                    text=chunk.text,
                    done=True,
                    citations=citations,
                    metadata=ResponseMetadata(
                        model=self.settings.openai_model,
                        tokens_used=0,  # Not available in streaming
                        retrieval_count=retrieval_result.total_retrieved,
                        latency_ms=0,  # Calculated by caller
                        cached=False,
                    ),
                )
            else:
                yield StreamChunkData(text=chunk.text, done=False)

    def _build_messages(
        self,
        message: str,
        conversation_history: list[dict] | None,
    ) -> list[dict]:
        """Build the message list for the LLM."""
        messages = []

        # Add conversation history
        if conversation_history:
            for msg in conversation_history[-10:]:  # Keep last 10 messages
                messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                })

        # Add current message
        messages.append({"role": "user", "content": message})

        return messages

    def _extract_page_info(self, page_context: PageContext) -> dict:
        """Extract page info dict from PageContext."""
        return {
            "id": page_context.id,
            "jurisdiction": page_context.jurisdiction,
            "title": page_context.title,
            "url": page_context.url,
        }

    def _extract_citations(
        self,
        response: str,
        retrieved_chunks: list,
    ) -> list[Citation]:
        """
        Extract citations from the response and match to retrieved chunks.

        Args:
            response: The LLM response
            retrieved_chunks: Chunks that were retrieved

        Returns:
            List of Citation objects
        """
        citations = []

        # Look for citation patterns like [Source: doc-id]
        citation_pattern = r"\[Source:\s*([^\]]+)\]"
        matches = re.findall(citation_pattern, response)

        # Match citations to retrieved chunks
        for match in matches:
            match_lower = match.lower().strip()
            for chunk in retrieved_chunks:
                chunk_id_lower = chunk.id.lower()
                source = chunk.metadata.get("source", "Unknown")

                if match_lower in chunk_id_lower or chunk_id_lower in match_lower:
                    citations.append(
                        Citation(
                            source=source,
                            document_id=chunk.id,
                            excerpt=chunk.content[:200],
                            url=chunk.metadata.get("url"),
                            relevance_score=chunk.score,
                        )
                    )
                    break

        # Also add top retrieved chunks as implicit citations
        if not citations and retrieved_chunks:
            for chunk in retrieved_chunks[:3]:
                citations.append(
                    Citation(
                        source=chunk.metadata.get("source", "Knowledge Base"),
                        document_id=chunk.id,
                        excerpt=chunk.content[:200],
                        url=chunk.metadata.get("url"),
                        relevance_score=chunk.score,
                    )
                )

        # Deduplicate
        seen_ids = set()
        unique_citations = []
        for citation in citations:
            if citation.document_id not in seen_ids:
                seen_ids.add(citation.document_id)
                unique_citations.append(citation)

        return unique_citations

    def _calculate_rag_confidence(self, retrieval_result) -> float:
        """
        Calculate confidence score based on RAG retrieval quality.

        This is used to determine whether to enable web search fallback.

        Args:
            retrieval_result: The retrieval result from the vector store

        Returns:
            Confidence score from 0.0 to 1.0
        """
        # Use actual chunk count (after threshold filtering) not raw retrieval count
        usable_chunks = len(retrieval_result.chunks) if retrieval_result.chunks else 0

        if usable_chunks == 0:
            return 0.0

        # Base confidence from having results
        confidence = 0.3

        # Boost based on number of relevant chunks (using actual usable count)
        if usable_chunks >= 3:
            confidence += 0.2
        elif usable_chunks >= 1:
            confidence += 0.1

        # Boost based on relevance scores of top chunks
        if retrieval_result.chunks:
            top_scores = [c.score for c in retrieval_result.chunks[:3] if c.score]
            if top_scores:
                avg_score = sum(top_scores) / len(top_scores)
                # Scores above 0.7 are highly relevant
                if avg_score > 0.7:
                    confidence += 0.4
                elif avg_score > 0.5:
                    confidence += 0.2
                elif avg_score > 0.3:
                    confidence += 0.1

        return min(1.0, confidence)

    def _calculate_confidence(
        self,
        response: str,
        retrieval_count: int,
        citations: list[Citation],
        web_search_used: bool = False,
    ) -> float:
        """
        Calculate confidence score for the response.

        Args:
            response: The LLM response
            retrieval_count: Number of documents retrieved
            citations: Extracted citations
            web_search_used: Whether web search was used

        Returns:
            Confidence score from 0.0 to 1.0
        """
        confidence = 0.5  # Base confidence

        # Boost for having retrieved documents
        if retrieval_count > 0:
            confidence += 0.2

        # Boost for having citations
        if citations:
            confidence += min(len(citations) * 0.05, 0.2)

        # Boost for citation relevance scores
        if citations:
            avg_relevance = sum(c.relevance_score or 0 for c in citations) / len(citations)
            confidence += avg_relevance * 0.1

        # Boost for web search being used (indicates comprehensive answer)
        if web_search_used:
            confidence += 0.1

        # Penalty for uncertainty phrases
        uncertainty_phrases = [
            "i'm not sure",
            "i don't know",
            "i cannot find",
            "no information",
            "unclear",
        ]
        response_lower = response.lower()
        for phrase in uncertainty_phrases:
            if phrase in response_lower:
                confidence -= 0.15
                break

        return max(0.0, min(1.0, confidence))

    def _check_human_handoff(
        self,
        message: str,
        response: str,
        confidence: float,
    ) -> bool:
        """
        Determine if the conversation should be handed off to a human.

        Args:
            message: The user's message
            response: The generated response
            confidence: Calculated confidence score

        Returns:
            True if human handoff is needed
        """
        message_lower = message.lower()

        # Explicit human request
        human_request_phrases = [
            "speak to a human",
            "talk to a person",
            "real person",
            "human agent",
            "customer service",
            "representative",
        ]
        for phrase in human_request_phrases:
            if phrase in message_lower:
                return True

        # Frustration indicators
        frustration_phrases = [
            "this is useless",
            "doesn't work",
            "stupid bot",
            "not helpful",
            "waste of time",
        ]
        for phrase in frustration_phrases:
            if phrase in message_lower:
                return True

        # Low confidence
        if confidence < 0.3:
            return True

        # Legal advice request
        legal_phrases = ["legal advice", "sue", "lawsuit", "attorney", "lawyer"]
        for phrase in legal_phrases:
            if phrase in message_lower:
                return True

        return False
