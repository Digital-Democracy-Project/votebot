"""Single conversational agent for VoteBot."""

import asyncio
import re
import time
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
from votebot.services.bill_votes import BillVotesService
from votebot.services.llm import BillVotesToolResult, LLMService, WebSearchCitation
from votebot.services.web_search import WebSearchService, WebSearchResult
from votebot.services.webflow_lookup import (
    WebflowLookupService,
    format_org_positions_context,
    format_org_bill_positions_context,
    format_bill_verification_context,
    format_legislator_verification_context,
    format_org_verification_context,
)

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
    bill_votes_tool_used: bool = False
    bill_votes_result: BillVotesToolResult | None = None


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
        self.web_search = WebSearchService(self.settings)
        self.bill_votes = BillVotesService(self.settings)
        self.webflow_lookup = WebflowLookupService(self.settings)

    def _log_query(
        self,
        *,
        session_id: str,
        message: str,
        result: AgentResult,
        page_context: PageContext,
        channel: str,
        start_time: float,
        human_active: bool = False,
    ) -> None:
        """Fire-and-forget log of a completed query to JSONL."""
        from votebot.services.query_logger import get_query_logger

        query_logger = get_query_logger()
        if query_logger is None:
            return

        duration_ms = int((time.perf_counter() - start_time) * 1000)

        citations_dicts = [
            {
                "source": c.source,
                "document_id": c.document_id,
                "url": c.url,
                "relevance_score": c.relevance_score,
            }
            for c in result.citations
        ]

        page_context_dict = {
            "type": page_context.type,
            "id": page_context.id,
            "title": page_context.title,
            "jurisdiction": page_context.jurisdiction,
            "webflow_id": getattr(page_context, "webflow_id", None),
            "slug": getattr(page_context, "slug", None),
        }

        asyncio.create_task(
            query_logger.log_query(
                session_id=session_id,
                message=message,
                response=result.response,
                confidence=result.confidence,
                citations=citations_dicts,
                page_context=page_context_dict,
                channel=channel,
                duration_ms=duration_ms,
                human_active=human_active,
            )
        )

    async def process_message(
        self,
        message: str,
        session_id: str,
        page_context: PageContext,
        navigation_context: NavigationContext | None = None,
        conversation_history: list[dict] | None = None,
        channel: str = "rest",
        human_active: bool = False,
    ) -> AgentResult:
        """
        Process a user message and generate a response.

        Args:
            message: The user's message
            session_id: Session identifier
            page_context: Context about the current page
            navigation_context: Optional navigation context
            conversation_history: Optional previous messages
            channel: Source channel ("rest" or "websocket")
            human_active: Whether a human agent is active (for logging)

        Returns:
            AgentResult with the response and metadata
        """
        _start_time = time.perf_counter()

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

        # Step 3: If user is disputing/verifying vote info, fetch directly from OpenStates
        vote_verification_context = ""
        is_dispute = self._is_dispute_or_correction(message)
        logger.info(
            "Checking dispute/verification trigger (non-streaming)",
            message=message[:50],
            is_dispute=is_dispute,
            page_type=page_context.type if page_context else None,
        )
        if is_dispute:
            logger.info("Dispute detected, attempting vote verification (non-streaming)")
            vote_verification_context = await self._verify_legislator_vote(
                message=message,
                page_context=page_context,
                conversation_history=conversation_history,
            )
            if vote_verification_context:
                logger.info(
                    "Vote verification successful (non-streaming)",
                    context_length=len(vote_verification_context),
                )
            else:
                logger.warning(
                    "Vote verification returned empty (non-streaming)",
                    message=message[:50],
                )

        # Step 3b: Pre-fetch org positions from Webflow CMS
        org_positions_context = ""
        if (page_context and page_context.type == "bill"
                and self._is_org_position_query(message)):
            org_positions_context = await self._prefetch_bill_org_positions(page_context)

        # Step 3c: Pre-fetch bill positions from Webflow CMS for org pages
        org_bill_positions_context = ""
        if (page_context and page_context.type == "organization"
                and self._is_bill_position_query(message)):
            org_bill_positions_context = await self._prefetch_org_bill_positions(page_context)

        # Step 3d: Webflow CMS verification for all page types on disputes
        webflow_verification_context = ""
        if is_dispute and page_context and page_context.type in ("bill", "legislator", "organization"):
            webflow_verification_context = await self._verify_from_webflow(page_context)

        # Step 4: Build page info for prompt
        page_info = self._extract_page_info(page_context)

        # Step 5: Build system prompt with verification context if available
        # Context order (most authoritative first):
        # 1. webflow_verification_context (CMS facts)
        # 2. org_bill_positions_context
        # 3. org_positions_context
        # 4. vote_verification_context (OpenStates votes)
        # 5. retrieved_context (RAG results)
        full_context = retrieved_context
        if vote_verification_context:
            # Put verification context first - it's the authoritative source
            full_context = f"{vote_verification_context}\n\n{full_context}"
        if org_positions_context:
            # Org positions from CMS are authoritative — prepend before RAG
            full_context = f"{org_positions_context}\n\n{full_context}"
        if org_bill_positions_context:
            # Org bill positions from CMS are authoritative — prepend before RAG
            full_context = f"{org_bill_positions_context}\n\n{full_context}"
        if webflow_verification_context:
            # Webflow CMS data is authoritative — prepend before everything
            full_context = f"{webflow_verification_context}\n\n{full_context}"

        system_prompt = build_system_prompt(
            page_type=page_context.type,
            page_info=page_info,
            include_rag_context=True,
            retrieved_context=full_context,
        )

        # Step 7: Build messages
        messages = self._build_messages(message, conversation_history)

        # Step 8: Calculate pre-LLM confidence based on retrieval quality
        rag_confidence = self._calculate_rag_confidence(retrieval_result)

        # Step 9: Determine if web search should be enabled
        enable_web_search = self._should_use_web_search(rag_confidence, page_context.type, message)

        # Step 9b: Determine if bill info tool should be enabled
        enable_bill_votes = self._should_use_bill_votes_tool(rag_confidence, message)

        # Step 9c: Enable web search as fallback when bill info tool is enabled
        # This allows hybrid lookup: OpenStates first, then web search if not found
        if enable_bill_votes and not enable_web_search:
            enable_web_search = True
            logger.info("Enabling web search as fallback for bill info tool")

        # Step 10: Generate response (with tools if enabled)
        llm_response = await self.llm.complete(
            messages=messages,
            system_prompt=system_prompt,
            enable_web_search=enable_web_search,
            enable_bill_votes=enable_bill_votes,
            bill_votes_service=self.bill_votes if enable_bill_votes else None,
        )

        # Step 10b: If OpenAI web search was enabled but didn't return citations,
        # fall back to Tavily for supplementary search
        if enable_web_search and not llm_response.web_citations:
            logger.info("OpenAI web search returned no citations, trying Tavily fallback")
            tavily_results = await self._perform_web_search(
                query=message,
                page_context=page_context,
            )
            if tavily_results:
                # Add Tavily results to context and regenerate
                web_context = self.web_search.format_results_for_context(tavily_results)
                enhanced_prompt = f"{system_prompt}\n\n{web_context}"
                llm_response = await self.llm.complete(
                    messages=messages,
                    system_prompt=enhanced_prompt,
                )
                llm_response.web_search_used = True
                llm_response.web_citations = [
                    WebSearchCitation(
                        url=r.url,
                        title=r.title,
                        snippet=r.snippet,
                    )
                    for r in tavily_results
                ]
                logger.info(
                    "Tavily fallback web search added to context",
                    results_count=len(tavily_results),
                )

        # Step 11: Extract citations from RAG
        citations = self._extract_citations(
            response=llm_response.content,
            retrieved_chunks=retrieval_result.chunks,
        )

        # Step 12: Calculate final confidence
        confidence = self._calculate_confidence(
            response=llm_response.content,
            retrieval_count=retrieval_result.total_retrieved,
            citations=citations,
            web_search_used=llm_response.web_search_used,
        )

        # Step 13: Check for human handoff
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
            bill_votes_tool_used=llm_response.bill_votes_tool_used,
        )

        result = AgentResult(
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
            bill_votes_tool_used=llm_response.bill_votes_tool_used,
            bill_votes_result=llm_response.bill_votes_result,
        )

        # Fire-and-forget query logging
        self._log_query(
            session_id=session_id,
            message=message,
            result=result,
            page_context=page_context,
            channel=channel,
            start_time=_start_time,
            human_active=human_active,
        )

        return result

    async def process_message_stream(
        self,
        message: str,
        session_id: str,
        page_context: PageContext,
        navigation_context: NavigationContext | None = None,
        conversation_history: list[dict] | None = None,
        channel: str = "websocket",
        human_active: bool = False,
    ) -> AsyncIterator[StreamChunkData]:
        """
        Process a message and stream the response.

        Args:
            message: The user's message
            session_id: Session identifier
            page_context: Context about the current page
            navigation_context: Optional navigation context
            conversation_history: Optional previous messages
            channel: Source channel ("rest" or "websocket")
            human_active: Whether a human agent is active (for logging)

        Yields:
            StreamChunkData objects with text fragments
        """
        _start_time = time.perf_counter()

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

        # Step 2b: Pre-fetch bill info if query mentions a specific bill
        # (This is done before streaming since tool calls can't interrupt streams)
        rag_confidence = self._calculate_rag_confidence(retrieval_result)
        bill_info_context = ""
        if self._should_use_bill_votes_tool(rag_confidence, message):
            bill_info_context = await self._prefetch_bill_info(message, page_context, conversation_history)
            if bill_info_context:
                logger.info("Pre-fetched bill info for streaming", has_info=bool(bill_info_context))

        # Step 2c: Pre-fetch legislator info if query mentions a person on a bill page
        legislator_info_context = ""
        if page_context and page_context.type == "bill":
            legislator_info_context = await self._prefetch_legislator_info(message)
            if legislator_info_context:
                logger.info("Pre-fetched legislator info for streaming")

        # Step 2d: If user is disputing/verifying vote info, fetch directly from OpenStates
        vote_verification_context = ""
        is_dispute = self._is_dispute_or_correction(message)
        logger.info(
            "Checking dispute/verification trigger",
            message=message[:50],
            is_dispute=is_dispute,
            page_type=page_context.type if page_context else None,
        )
        if is_dispute:
            logger.info("Dispute detected, attempting vote verification")
            vote_verification_context = await self._verify_legislator_vote(
                message=message,
                page_context=page_context,
                conversation_history=conversation_history,
            )
            if vote_verification_context:
                logger.info(
                    "Vote verification successful",
                    context_length=len(vote_verification_context),
                )
            else:
                logger.warning(
                    "Vote verification returned empty - could not find legislator or vote",
                    message=message[:50],
                )

        # Step 2e: Pre-fetch org positions from Webflow CMS
        org_positions_context = ""
        if (page_context and page_context.type == "bill"
                and self._is_org_position_query(message)):
            org_positions_context = await self._prefetch_bill_org_positions(page_context)

        # Step 2f: Pre-fetch bill positions from Webflow CMS for org pages
        org_bill_positions_context = ""
        if (page_context and page_context.type == "organization"
                and self._is_bill_position_query(message)):
            org_bill_positions_context = await self._prefetch_org_bill_positions(page_context)

        # Step 2g: Webflow CMS verification for all page types on disputes
        webflow_verification_context = ""
        if is_dispute and page_context and page_context.type in ("bill", "legislator", "organization"):
            webflow_verification_context = await self._verify_from_webflow(page_context)

        # Step 3: Build page info and system prompt
        page_info = self._extract_page_info(page_context)

        # Combine RAG context with bill info, legislator info, and verification
        # Context order (most authoritative first):
        # 1. webflow_verification_context (CMS facts)
        # 2. org_bill_positions_context
        # 3. org_positions_context
        # 4. vote_verification_context (OpenStates votes)
        # 5. legislator_info_context / bill_info_context
        # 6. retrieved_context (RAG results)
        full_context = retrieved_context
        if bill_info_context:
            full_context = f"{retrieved_context}\n\n{bill_info_context}"
        if legislator_info_context:
            full_context = f"{full_context}\n\n{legislator_info_context}"
        if vote_verification_context:
            # Put verification context first - it's the authoritative source
            full_context = f"{vote_verification_context}\n\n{full_context}"
        if org_positions_context:
            # Org positions from CMS are authoritative — prepend before everything
            full_context = f"{org_positions_context}\n\n{full_context}"
        if org_bill_positions_context:
            # Org bill positions from CMS are authoritative — prepend before everything
            full_context = f"{org_bill_positions_context}\n\n{full_context}"
        if webflow_verification_context:
            # Webflow CMS data is authoritative — prepend before everything
            full_context = f"{webflow_verification_context}\n\n{full_context}"

        system_prompt = build_system_prompt(
            page_type=page_context.type,
            page_info=page_info,
            include_rag_context=True,
            retrieved_context=full_context,
        )

        # Step 4: Determine if web search should be enabled
        enable_web_search = self._should_use_web_search(rag_confidence, page_context.type, message)

        # Step 5: Build messages
        messages = self._build_messages(message, conversation_history)

        # Step 6: Stream response (with OpenAI web search if enabled)
        full_response = ""
        async for chunk in self.llm.stream(
            messages=messages,
            system_prompt=system_prompt,
            enable_web_search=enable_web_search,
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

                # Log the completed streaming interaction
                stream_result = AgentResult(
                    response=full_response,
                    citations=citations,
                    confidence=confidence,
                    requires_human=False,
                    tokens_used=0,
                    retrieval_count=retrieval_result.total_retrieved,
                )
                self._log_query(
                    session_id=session_id,
                    message=message,
                    result=stream_result,
                    page_context=page_context,
                    channel=channel,
                    start_time=_start_time,
                    human_active=human_active,
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
            "name": page_context.title,
            "title": page_context.title,
            "jurisdiction": page_context.jurisdiction,
            "session": getattr(page_context, "session", None),
            "url": page_context.url,
        }

    def _extract_citations(
        self,
        response: str,
        retrieved_chunks: list,
    ) -> list[Citation]:
        """
        Extract citations from the response and match to retrieved chunks.

        Only includes citations that the LLM explicitly referenced in the response.
        This prevents showing irrelevant sources for simple conversational queries.

        Args:
            response: The LLM response
            retrieved_chunks: Chunks that were retrieved

        Returns:
            List of Citation objects
        """
        citations = []

        # Look for citation patterns:
        # 1. Markdown link format: [Source: name](url)
        # 2. Plain format: [Source: name]
        markdown_pattern = r"\[Source:\s*([^\]]+)\]\(([^)]+)\)"
        plain_pattern = r"\[Source:\s*([^\]]+)\](?!\()"

        # Extract markdown citations (with URLs)
        markdown_matches = re.findall(markdown_pattern, response)
        # Extract plain citations (without URLs)
        plain_matches = re.findall(plain_pattern, response)

        # Combine all source names for matching
        all_source_names = [m[0] for m in markdown_matches] + plain_matches

        # Match citations to retrieved chunks by source name or doc_id
        for source_name in all_source_names:
            source_lower = source_name.lower().strip()
            for chunk in retrieved_chunks:
                chunk_id_lower = chunk.id.lower()
                chunk_source = chunk.metadata.get("source", "").lower()

                # Match by source name OR document ID
                if (source_lower in chunk_source or chunk_source in source_lower or
                    source_lower in chunk_id_lower or chunk_id_lower in source_lower):
                    citations.append(
                        Citation(
                            source=chunk.metadata.get("source", "Unknown"),
                            document_id=chunk.id,
                            excerpt=chunk.content[:200],
                            url=chunk.metadata.get("url"),
                            relevance_score=chunk.score,
                        )
                    )
                    break

        # Only show citations that were explicitly referenced by the LLM
        # Do NOT add implicit citations from retrieved chunks as they may be
        # irrelevant to the actual response (especially for conversational queries)

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

    def _should_use_bill_votes_tool(
        self,
        rag_confidence: float,
        message: str,
    ) -> bool:
        """
        Determine if the bill votes lookup tool should be enabled.

        The tool is enabled when:
        1. Bill votes tool is enabled in settings
        2. The message appears to be asking about votes/voting
        3. RAG confidence is below threshold (vote info may not be in context)

        Args:
            rag_confidence: Confidence score from RAG retrieval
            message: The user's message

        Returns:
            True if bill votes tool should be enabled
        """
        if not self.settings.bill_votes_tool_enabled:
            return False

        message_lower = message.lower()

        # Check if the query is about votes
        vote_keywords = [
            "vote", "voted", "voting", "votes",
            "vote count", "vote tally",
            "who supported", "who opposed",
            "who voted yes", "who voted no",
            "pass", "passed", "fail", "failed",
            "yea", "nay", "abstain",
            "roll call", "floor vote",
            "how did", "did it pass",
        ]
        is_vote_query = any(keyword in message_lower for keyword in vote_keywords)

        # Check if the query mentions a specific bill identifier (HB, SB, HR, etc.)
        bill_pattern = r'\b(hb|sb|hr|s|hj|sj|hcr|scr|hjr|sjr)\s*\d+'
        has_bill_identifier = bool(re.search(bill_pattern, message_lower))

        # Check for general bill inquiry keywords
        bill_inquiry_keywords = [
            "tell me about", "what is", "what does", "explain",
            "summary", "sponsor", "status", "action",
        ]
        is_bill_inquiry = has_bill_identifier and any(kw in message_lower for kw in bill_inquiry_keywords)

        # Enable tool if:
        # 1. It's a vote query (always enable for vote questions)
        # 2. Query mentions a specific bill identifier (HB 2724, etc.) - these often aren't in RAG
        # 3. RAG confidence is very low AND it's a bill inquiry
        threshold = self.settings.bill_votes_rag_confidence_threshold
        very_low_threshold = 0.5  # Higher threshold for specific bill lookups

        should_enable = (
            is_vote_query or  # Always for vote queries
            has_bill_identifier or  # Always when a specific bill number is mentioned
            (rag_confidence < very_low_threshold and is_bill_inquiry)  # Low confidence + bill inquiry
        )

        if should_enable:
            logger.info(
                "Bill info tool enabled",
                rag_confidence=rag_confidence,
                threshold=threshold,
                is_vote_query=is_vote_query,
                has_bill_identifier=has_bill_identifier,
                is_bill_inquiry=is_bill_inquiry,
            )
        else:
            logger.debug(
                "Bill info tool not enabled",
                rag_confidence=rag_confidence,
                has_bill_identifier=has_bill_identifier,
                message_preview=message[:50],
            )

        return should_enable

    async def _resolve_bill_from_title(
        self,
        message: str,
    ) -> tuple[str | None, str | None]:
        """
        Resolve a bill identifier from a title/name by searching Pinecone.

        When the user mentions a bill by its common name (e.g., "one big
        beautiful bill act") instead of its number ("HR 1"), search Pinecone
        for matching bill documents and extract the identifier from metadata.

        Args:
            message: The user's message containing a bill title

        Returns:
            Tuple of (bill_identifier, jurisdiction) — either may be None
        """
        message_lower = message.lower()

        # Must contain bill-related terms to warrant a Pinecone search
        bill_terms = ["bill", "act", "resolution", "legislation", "law"]
        has_bill_term = any(term in message_lower for term in bill_terms)

        if not has_bill_term:
            return None, None

        try:
            results = await self.bill_votes.vector_store.query(
                query=message,
                top_k=1,
                filter={"document_type": "bill"},
            )

            if results and results[0].score > 0.7:
                metadata = results[0].metadata
                bill_prefix = metadata.get("bill_prefix", "")
                bill_number = metadata.get("bill_number", "")
                jurisdiction = metadata.get("jurisdiction", "")

                if bill_prefix and bill_number:
                    bill_identifier = f"{bill_prefix}{bill_number}"
                    logger.info(
                        "Resolved bill from title via Pinecone",
                        query=message[:50],
                        bill_identifier=bill_identifier,
                        jurisdiction=jurisdiction,
                        score=results[0].score,
                    )
                    return bill_identifier, jurisdiction

        except Exception as e:
            logger.warning("Error resolving bill from title", error=str(e))

        return None, None

    async def _prefetch_bill_info(
        self,
        message: str,
        page_context: PageContext,
        conversation_history: list[dict] | None = None,
    ) -> str:
        """
        Pre-fetch bill info for streaming responses.

        Extracts bill identifier from message and fetches from OpenStates
        before streaming begins. Uses three resolution methods:
        1. Regex extraction from current message (e.g., "HR 1")
        2. Pinecone title search (e.g., "one big beautiful bill act")
        3. Conversation history search (e.g., "check how he voted on it")

        Args:
            message: The user's message
            page_context: Context about the current page
            conversation_history: Optional previous messages for fallback

        Returns:
            Formatted bill info string to add to context, or empty string
        """
        bill_identifier = None
        jurisdiction = None

        # Method 1: Extract bill identifier from message via regex
        message_lower = message.lower()
        bill_pattern = r'\b(hb|sb|hr|s|hj|sj|hcr|scr|hjr|sjr)\s*(\d+)'
        match = re.search(bill_pattern, message_lower)

        if match:
            bill_type = match.group(1).upper()
            bill_number = match.group(2)
            bill_identifier = f"{bill_type}{bill_number}"

        # Method 2: Resolve bill from title via Pinecone search
        if not bill_identifier:
            bill_identifier, jurisdiction = await self._resolve_bill_from_title(message)

        # Method 3: Search conversation history for bill identifiers
        if not bill_identifier and conversation_history:
            for msg in reversed(conversation_history[-6:]):
                content = msg.get("content", "")
                extracted_id, extracted_jurisdiction = self._extract_bill_from_text(content)
                if extracted_id:
                    bill_identifier = extracted_id
                    if extracted_jurisdiction:
                        jurisdiction = extracted_jurisdiction
                    logger.info(
                        "Found bill identifier in conversation history for pre-fetch",
                        bill_identifier=bill_identifier,
                    )
                    break

        # Method 4: Fall back to page_context.id when on a bill page
        # This handles "how did X vote on this?" where "this" refers to the current bill
        if not bill_identifier and page_context and page_context.type == "bill" and page_context.id:
            bill_identifier = page_context.id
            logger.info(
                "Using page_context.id as bill identifier for pre-fetch",
                bill_identifier=bill_identifier,
            )

        if not bill_identifier:
            return ""

        # Extract jurisdiction from message or page context (if not already resolved)
        if not jurisdiction:
            jurisdiction = self._extract_jurisdiction_from_message(message)
        if not jurisdiction:
            jurisdiction = page_context.jurisdiction or "US"

        # Determine session (default to current year, service will fallback)
        session = getattr(page_context, "session", None)
        if not session:
            from datetime import datetime
            year = datetime.now().year
            # For federal bills (US jurisdiction), use Congress number instead of year
            # Congress numbers: 119th = 2025-2027, 118th = 2023-2024, etc.
            if jurisdiction and jurisdiction.upper() == "US":
                congress_number = (year - 2025) // 2 + 119
                session = str(congress_number)
            else:
                session = str(year)

        logger.info(
            "Pre-fetching bill info for streaming",
            jurisdiction=jurisdiction,
            session=session,
            bill_identifier=bill_identifier,
        )

        try:
            result = await self.bill_votes.get_bill_info(
                jurisdiction=jurisdiction,
                session=session,
                bill_identifier=bill_identifier,
            )

            if result and result.found:
                formatted = self.bill_votes.format_bill_info_document(result)

                # Look up specific legislator's vote to prevent LLM from drowning
                # in hundreds of voter names and guessing incorrectly.
                legislator_vote_context = ""
                legislator_name = None

                if page_context and page_context.type == "legislator" and page_context.title:
                    # Legislator pages: name is in page context
                    legislator_name = page_context.title
                else:
                    # Other page types: extract name from message text
                    legislator_name = self._extract_legislator_name(message)
                    # Fall back to conversation history for pronoun resolution ("she", "he")
                    if not legislator_name and conversation_history:
                        for msg in reversed(conversation_history[-6:]):
                            if msg.get("role") == "user":
                                legislator_name = self._extract_legislator_name(msg.get("content", ""))
                                if legislator_name:
                                    break

                if legislator_name and result.votes:
                    try:
                        vote_result = self.bill_votes.find_legislator_in_votes(
                            legislator_name=legislator_name,
                            votes=result.votes,
                            bill_identifier=result.bill_identifier,
                        )
                        if vote_result:
                            legislator_vote_context = (
                                f"## SPECIFIC VOTE LOOKUP RESULT\n\n"
                                f"**{vote_result['legislator']}** voted **{vote_result['vote'].upper()}** "
                                f"on **{vote_result['bill']}**\n"
                                f"Motion: {vote_result.get('motion', 'N/A')[:100]}\n"
                                f"Date: {vote_result.get('date', 'N/A')}\n"
                                f"Chamber: {vote_result.get('chamber', 'N/A')}\n"
                                f"Result: {vote_result.get('result', 'N/A').upper()}\n\n"
                                f"*This is the authoritative vote record for this legislator. "
                                f"Use this as the definitive answer.*\n\n"
                            )
                            logger.info(
                                "Found specific legislator vote during bill pre-fetch",
                                legislator=legislator_name,
                                vote=vote_result['vote'],
                                bill=bill_identifier,
                            )
                        else:
                            legislator_vote_context = (
                                f"## SPECIFIC VOTE LOOKUP RESULT\n\n"
                                f"**{legislator_name}** was NOT found in the vote records "
                                f"for **{result.bill_identifier}** in OpenStates. They may not have voted "
                                f"on this bill, or the bill may not have had a recorded vote yet.\n\n"
                            )
                            logger.info(
                                "Legislator not found in bill votes during pre-fetch",
                                legislator=legislator_name,
                                bill=bill_identifier,
                            )
                    except Exception as e:
                        logger.warning("Error looking up specific legislator vote", error=str(e))

                return f"{legislator_vote_context}## Bill Information from OpenStates API\n\n{formatted}"
            else:
                logger.info(
                    "Bill not found in OpenStates",
                    jurisdiction=jurisdiction,
                    bill_identifier=bill_identifier,
                )
                return ""

        except Exception as e:
            logger.error("Error pre-fetching bill info", error=str(e))
            return ""

    def _is_org_position_query(self, message: str) -> bool:
        """
        Detect if the message is asking about organization positions on a bill.

        Uses the same keyword list as retrieval.py Phase 4a.

        Args:
            message: The user's message

        Returns:
            True if the query is about org positions and the feature is enabled
        """
        if not self.settings.webflow_org_lookup_enabled:
            return False

        message_lower = message.lower()
        org_keywords = [
            "organization", "organizations", "org ", "orgs ",
            "who supports", "who support", "who opposes", "who oppose",
            "which groups", "which organizations",
            "support", "oppose", "backed", "backs", "endorses", "against",
        ]
        return any(kw in message_lower for kw in org_keywords)

    def _is_bill_position_query(self, message: str) -> bool:
        """
        Detect if the message is asking about an organization's bill positions.

        Used when page_context.type == "organization" to trigger Webflow CMS
        pre-fetch of the org's supported/opposed bills.

        Args:
            message: The user's message

        Returns:
            True if the query is about bill positions and the feature is enabled
        """
        if not self.settings.webflow_org_lookup_enabled:
            return False

        message_lower = message.lower()
        bill_keywords = [
            "bill", "bills", "legislation", "legislative",
            "what bills", "which bills",
            "positions on", "stance on", "stances on",
            "support", "supports", "oppose", "opposes",
            "backed", "backs", "endorsed", "endorses",
            "against", "in favor",
        ]
        return any(kw in message_lower for kw in bill_keywords)

    async def _prefetch_org_bill_positions(self, page_context: PageContext) -> str:
        """
        Pre-fetch bill positions from Webflow CMS for an organization.

        Mirrors _prefetch_bill_org_positions but in the reverse direction:
        given an org page, fetch the bills it supports/opposes.

        Args:
            page_context: Organization page context with webflow_id and/or slug

        Returns:
            Formatted bill positions string, or empty string on failure
        """
        webflow_id = getattr(page_context, "webflow_id", None)
        slug = getattr(page_context, "slug", None)

        if not webflow_id and not slug:
            logger.debug("No webflow_id or slug in page_context for org bill lookup")
            return ""

        logger.info(
            "Pre-fetching org bill positions from Webflow CMS",
            webflow_id=webflow_id,
            slug=slug,
        )

        try:
            result = await self.webflow_lookup.get_org_bill_positions(
                webflow_id=webflow_id,
                slug=slug,
            )
            if result.found:
                formatted = format_org_bill_positions_context(result)
                if formatted:
                    logger.info(
                        "Webflow org bill positions fetched successfully",
                        supported=len(result.supported_bills),
                        opposed=len(result.opposed_bills),
                    )
                return formatted
            return ""
        except Exception as e:
            logger.error("Error pre-fetching org bill positions from Webflow", error=str(e))
            return ""

    async def _prefetch_bill_org_positions(self, page_context: PageContext) -> str:
        """
        Pre-fetch organization positions from Webflow CMS for a bill.

        Follows the pre-fetch pattern used by _prefetch_bill_info and
        _verify_legislator_vote — fetches authoritative data before
        LLM generation begins.

        Args:
            page_context: Bill page context with webflow_id and/or slug

        Returns:
            Formatted org positions string, or empty string on failure
        """
        webflow_id = getattr(page_context, "webflow_id", None)
        slug = getattr(page_context, "slug", None)

        if not webflow_id and not slug:
            logger.debug("No webflow_id or slug in page_context for org lookup")
            return ""

        logger.info(
            "Pre-fetching bill org positions from Webflow CMS",
            webflow_id=webflow_id,
            slug=slug,
        )

        try:
            result = await self.webflow_lookup.get_bill_org_positions(
                webflow_id=webflow_id,
                slug=slug,
            )
            if result.found:
                formatted = format_org_positions_context(result)
                if formatted:
                    logger.info(
                        "Webflow org positions fetched successfully",
                        supporting=len(result.supporting_orgs),
                        opposing=len(result.opposing_orgs),
                    )
                return formatted
            return ""
        except Exception as e:
            logger.error("Error pre-fetching org positions from Webflow", error=str(e))
            return ""

    async def _verify_legislator_vote(
        self,
        message: str,
        page_context: PageContext | None = None,
        conversation_history: list[dict] | None = None,
    ) -> str:
        """
        Verify a legislator's vote by fetching directly from OpenStates.

        This is triggered when a user disputes or challenges vote information.
        It bypasses RAG and goes directly to the authoritative source.

        Works from any page type or with no page context by extracting bill
        identifiers and jurisdiction from message text or conversation history.

        Args:
            message: The user's message
            page_context: Context about the current page (optional)
            conversation_history: Previous messages to extract context

        Returns:
            Formatted verification result, or empty string if not applicable
        """
        # Extract legislator name from message or conversation
        legislator_name = self._extract_legislator_name(message)

        # If no name in message and we're on a legislator page, use page context title
        if not legislator_name and page_context and page_context.type == "legislator" and page_context.title:
            legislator_name = page_context.title
            logger.debug("Using legislator name from page context", name=legislator_name)

        if not legislator_name and conversation_history:
            # Look in recent conversation for legislator names
            # Prioritize user questions (they contain "did X vote" patterns)
            # then look at assistant responses (they contain "X voted Y" patterns)
            user_messages = []
            assistant_messages = []

            for msg in conversation_history[-6:]:
                content = msg.get("content", "")
                role = msg.get("role", "")
                if role == "user":
                    user_messages.append(content)
                elif role in ("assistant", "agent"):
                    assistant_messages.append(content)

            # First try user messages (most likely to have the name they asked about)
            for content in reversed(user_messages):
                legislator_name = self._extract_legislator_name(content)
                if legislator_name:
                    logger.debug(
                        "Found legislator name in user message",
                        name=legislator_name,
                    )
                    break

            # Then try assistant messages (contains vote results)
            if not legislator_name:
                for content in reversed(assistant_messages):
                    legislator_name = self._extract_legislator_name(content)
                    if legislator_name:
                        logger.debug(
                            "Found legislator name in assistant message",
                            name=legislator_name,
                        )
                        break

        if not legislator_name:
            logger.info("Could not extract legislator name for vote verification")
            return ""

        # Get bill info from page context first, then fall back to message extraction
        # IMPORTANT: Only use page_context.id as bill_identifier when on a BILL page.
        # On legislator pages, page_context.id is the legislator's OpenStates ID,
        # not a bill identifier — using it would cause the lookup to fail silently.
        bill_identifier = None
        jurisdiction = page_context.jurisdiction if page_context else None
        if page_context and page_context.type == "bill":
            bill_identifier = page_context.id

        # If no bill identifier from page context, extract from message text
        if not bill_identifier:
            bill_identifier, msg_jurisdiction = self._extract_bill_from_text(message)
            if msg_jurisdiction and not jurisdiction:
                jurisdiction = msg_jurisdiction

        # If still no bill identifier, search conversation history
        if not bill_identifier and conversation_history:
            for msg in reversed(conversation_history[-6:]):
                content = msg.get("content", "")
                bill_identifier, msg_jurisdiction = self._extract_bill_from_text(content)
                if bill_identifier:
                    if msg_jurisdiction and not jurisdiction:
                        jurisdiction = msg_jurisdiction
                    logger.debug(
                        "Found bill identifier in conversation history",
                        bill_identifier=bill_identifier,
                    )
                    break

        # If still no bill identifier, try resolving from title via Pinecone
        # This handles cases like "he didn't vote yes on the big beautiful bill"
        if not bill_identifier:
            resolved_id, resolved_jurisdiction = await self._resolve_bill_from_title(message)
            if resolved_id:
                bill_identifier = resolved_id
                if resolved_jurisdiction and not jurisdiction:
                    jurisdiction = resolved_jurisdiction
            # Also try conversation history messages for title resolution
            if not bill_identifier and conversation_history:
                for msg in reversed(conversation_history[-6:]):
                    content = msg.get("content", "")
                    resolved_id, resolved_jurisdiction = await self._resolve_bill_from_title(content)
                    if resolved_id:
                        bill_identifier = resolved_id
                        if resolved_jurisdiction and not jurisdiction:
                            jurisdiction = resolved_jurisdiction
                        break

        if not bill_identifier:
            logger.info("Could not extract bill identifier for vote verification")
            return ""

        # Default jurisdiction if still unknown
        if not jurisdiction:
            jurisdiction = "US"

        # Determine session
        session = getattr(page_context, "session", None)
        if not session:
            from datetime import datetime
            year = datetime.now().year
            # For federal bills (US jurisdiction), use Congress number instead of year
            # Congress numbers: 119th = 2025-2027, 118th = 2023-2024, etc.
            if jurisdiction and jurisdiction.upper() == "US":
                congress_number = (year - 2025) // 2 + 119
                session = str(congress_number)
            else:
                session = str(year)

        logger.info(
            "Verifying legislator vote from OpenStates",
            legislator=legislator_name,
            bill=bill_identifier,
            jurisdiction=jurisdiction,
            session=session,
        )

        try:
            # Use the lookup_legislator_vote method for direct verification
            result = await self.bill_votes.lookup_legislator_vote(
                legislator_name=legislator_name,
                jurisdiction=jurisdiction,
                session=session,
                bill_identifier=bill_identifier,
            )

            if result:
                # Found the vote - format as authoritative answer
                parts = [
                    "## Vote Verification (Direct from OpenStates API)",
                    "",
                    f"**Legislator:** {result['legislator']}",
                    f"**Bill:** {result['bill']}",
                    f"**Vote:** {result['vote'].upper()}",
                    f"**Motion:** {result['motion'][:100]}..." if len(result.get('motion', '')) > 100 else f"**Motion:** {result.get('motion', 'N/A')}",
                    f"**Date:** {result['date']}",
                    f"**Chamber:** {result['chamber'].title()}",
                    f"**Result:** {result['result'].upper()}",
                ]

                # Add note about multiple votes if present
                if result.get("note"):
                    parts.append("")
                    parts.append(f"*Note: {result['note']}*")

                parts.append("")
                parts.append("*This information is fetched directly from OpenStates and should be considered authoritative.*")

                return "\n".join(parts)
            else:
                # Legislator not found in vote records
                return f"## Vote Verification\n\n**{legislator_name}** was not found in the vote records for **{bill_identifier}** in OpenStates. This could mean they did not vote on this bill, or the name spelling differs from official records."

        except Exception as e:
            logger.error("Error verifying legislator vote", error=str(e))
            return ""

    def _extract_legislator_name(self, text: str) -> str | None:
        """
        Extract a potential legislator name from text.

        Args:
            text: Text to search for names

        Returns:
            Extracted name or None
        """
        # Try multiple extraction methods in order of specificity

        # Method 1: Look for "X voted Y" pattern (most reliable in vote context)
        import re
        vote_pattern = re.search(
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+voted\s+(yes|no|yea|nay)",
            text,
            re.IGNORECASE,
        )
        if vote_pattern:
            return vote_pattern.group(1)

        # Method 2: Look for "Name (Party-State)" pattern like "Moody (R-FL)"
        party_state_pattern = re.search(
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*\([RDI]-[A-Z]{2}\)",
            text,
        )
        if party_state_pattern:
            return party_state_pattern.group(1)

        # Method 3: Look for "did/how did Name vote" pattern
        question_pattern = re.search(
            r"(?:how\s+)?did\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\s+vote",
            text,
            re.IGNORECASE,
        )
        if question_pattern:
            name = question_pattern.group(1)
            # Filter out common words
            if name.lower() not in {"she", "he", "they", "the", "this", "that"}:
                # Title case the name
                return " ".join(w.capitalize() for w in name.split())

        # Method 4: Capitalized name patterns (First Last or just Last)
        common_words = {
            "how", "did", "what", "about", "the", "this", "vote", "on", "and",
            "senator", "rep", "representative", "congressman", "congresswoman",
            "she", "he", "they", "is", "a", "us", "u.s.", "that", "way", "no",
            "there", "can", "be", "sure", "verify", "check", "wrong", "right",
            "actually", "really", "tell", "me", "all", "who", "why", "when",
            "does", "bill", "act", "hr", "hb", "sb", "one", "big", "beautiful",
            "yes", "yea", "nay", "not", "voted", "according", "official",
            "apologies", "thank", "you", "result", "result:", "your", "let",
            "sources", "source", "source:", "digital", "democracy", "project",
            "as", "at", "in", "if", "it", "or", "but", "for", "with", "from",
            "has", "have", "had", "was", "were", "are", "been", "being",
            "however", "therefore", "furthermore", "additionally", "currently",
            "also", "based", "please", "note", "here", "possible", "reasons",
        }

        words = text.split()
        name_parts = []

        for i, word in enumerate(words):
            # Clean the word
            clean_word = word.strip(".,!?\"'():*#[]")

            # Check if it looks like a name (capitalized, not common)
            if (len(clean_word) > 1 and
                clean_word[0].isupper() and
                clean_word.lower() not in common_words and
                clean_word.isalpha()):
                name_parts.append(clean_word)
            elif name_parts:
                # If we had name parts and hit a non-name, stop
                break

        if name_parts:
            return " ".join(name_parts)

        return None

    async def _prefetch_legislator_info(self, message: str) -> str:
        """
        Pre-fetch legislator info from OpenStates when a name is mentioned.

        This helps override outdated LLM training data with current info
        (e.g., Ashley Moody is now a Senator, not FL Attorney General).

        Args:
            message: The user's message

        Returns:
            Formatted legislator info string, or empty string
        """
        import httpx

        # Extract potential name from message
        common_words = {
            "how", "did", "what", "about", "the", "this", "vote", "on", "and",
            "senator", "rep", "representative", "congressman", "congresswoman",
            "she", "he", "they", "is", "a", "us", "u.s."
        }

        # Get capitalized words that might be names
        words = message.split()
        name_parts = []
        for w in words:
            # Keep capitalized words that aren't common
            if len(w) > 1 and w[0].isupper() and w.lower() not in common_words:
                name_parts.append(w)

        if not name_parts:
            return ""

        # Construct search name (e.g., "Ashley Moody")
        search_name = " ".join(name_parts)

        logger.info("Looking up legislator info", name=search_name)

        try:
            api_key = self.settings.openstates_api_key.get_secret_value()
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    "https://v3.openstates.org/people",
                    headers={"x-api-key": api_key},
                    params={"name": search_name, "per_page": 3},
                )

                if response.status_code != 200:
                    return ""

                data = response.json()
                results = data.get("results", [])

                if not results:
                    return ""

                # Format the first matching result
                person = results[0]
                name = person.get("name", search_name)
                party = person.get("party", "")
                current_role = person.get("current_role", {})
                title = current_role.get("title", "")
                district = current_role.get("district", "")
                org_class = current_role.get("org_classification", "")

                # Build context string
                parts = [f"## Current Legislator Information (from OpenStates)"]
                parts.append(f"**{name}** ({party})")
                if title and district:
                    parts.append(f"**Current Role:** {title} - {district}")
                elif title:
                    parts.append(f"**Current Role:** {title}")
                if org_class:
                    chamber = "Senate" if org_class == "upper" else "House" if org_class == "lower" else org_class
                    parts.append(f"**Chamber:** {chamber}")

                parts.append("")
                parts.append("*Note: This is current information from OpenStates. If the RAG data conflicts with this, trust this current information.*")

                logger.info(
                    "Found legislator info",
                    name=name,
                    title=title,
                    district=district,
                )

                return "\n".join(parts)

        except Exception as e:
            logger.warning("Error fetching legislator info", error=str(e))
            return ""

    def _extract_jurisdiction_from_message(self, message: str) -> str | None:
        """Extract state/jurisdiction from message text."""
        message_lower = message.lower()

        # State name to code mapping
        state_names = {
            "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
            "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
            "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
            "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
            "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
            "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
            "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
            "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
            "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
            "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
            "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
            "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
            "wisconsin": "WI", "wyoming": "WY",
        }

        # Check for state names
        for state_name, state_code in state_names.items():
            if state_name in message_lower:
                return state_code

        # Check for explicit state codes (e.g., "VA HB 2724")
        state_code_pattern = r'\b([A-Z]{2})\s+(hb|sb|hr|s|hj|sj)'
        match = re.search(state_code_pattern, message, re.IGNORECASE)
        if match:
            potential_code = match.group(1).upper()
            if potential_code in state_names.values():
                return potential_code

        # Check for federal bill indicators
        federal_keywords = ["federal", "congress", "us ", "u.s.", "united states"]
        if any(kw in message_lower for kw in federal_keywords):
            return "US"

        return None

    def _extract_bill_from_text(self, text: str) -> tuple[str | None, str | None]:
        """
        Extract bill identifier and jurisdiction from text.

        Uses the same regex patterns as retrieval.py _extract_bill_from_query()
        for consistency.

        Args:
            text: Text to search for bill identifiers

        Returns:
            Tuple of (bill_identifier, jurisdiction) — either may be None
        """
        if not text:
            return None, None

        text_lower = text.lower()

        # Extract jurisdiction using the same state mappings as retrieval.py
        jurisdiction = self._extract_jurisdiction_from_message(text)

        # Bill identifier patterns (same as retrieval.py)
        pattern = r'\b(H\.?J\.?R\.?|S\.?J\.?R\.?|H\.?C\.?R\.?|S\.?C\.?R\.?|H\.?B\.?|S\.?B\.?|H\.?R\.?|S\.?|H\.?J\.?|S\.?J\.?)\s*(\d+)\b'
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            prefix = match.group(1).replace(".", "").upper()
            number = match.group(2)
            return f"{prefix}{number}", jurisdiction

        # Full name patterns: "House Bill 363", "Senate Bill 123"
        pattern2 = r'\b(house|senate)\s+(?:bill|resolution|joint\s+resolution)\s*(\d+)\b'
        match = re.search(pattern2, text, re.IGNORECASE)
        if match:
            chamber = match.group(1).lower()
            number = match.group(2)
            prefix = "HB" if chamber == "house" else "SB"
            return f"{prefix}{number}", jurisdiction

        return None, jurisdiction

    async def _verify_from_webflow(self, page_context: PageContext) -> str:
        """
        Fetch authoritative details from Webflow CMS for verification.

        Dispatches based on page_context.type to the appropriate detail-lookup
        method. Used when a user disputes or challenges information.

        Args:
            page_context: Page context with type, webflow_id, and/or slug

        Returns:
            Formatted verification context string, or empty string on failure
        """
        webflow_id = getattr(page_context, "webflow_id", None)
        slug = getattr(page_context, "slug", None)

        if not webflow_id and not slug:
            logger.debug("No webflow_id or slug in page_context for Webflow verification")
            return ""

        try:
            if page_context.type == "bill":
                result = await self.webflow_lookup.get_bill_details(
                    webflow_id=webflow_id, slug=slug,
                )
                formatted = format_bill_verification_context(result)

            elif page_context.type == "legislator":
                result = await self.webflow_lookup.get_legislator_details(
                    webflow_id=webflow_id, slug=slug,
                )
                formatted = format_legislator_verification_context(result)

            elif page_context.type == "organization":
                result = await self.webflow_lookup.get_org_details(
                    webflow_id=webflow_id, slug=slug,
                )
                formatted = format_org_verification_context(result)

            else:
                return ""

            if formatted:
                logger.info(
                    "Webflow CMS verification fetched",
                    page_type=page_context.type,
                    webflow_id=webflow_id,
                    slug=slug,
                )
            return formatted

        except Exception as e:
            logger.error(
                "Error fetching Webflow CMS verification",
                page_type=page_context.type,
                error=str(e),
            )
            return ""

    def _should_use_web_search(
        self,
        rag_confidence: float,
        page_context_type: str | None,
        message: str | None = None,
    ) -> bool:
        """
        Determine if web search should be used based on RAG confidence and query content.

        Args:
            rag_confidence: Confidence score from RAG retrieval
            page_context_type: Type of page context
            message: The user's message (for detecting current events queries)

        Returns:
            True if web search should be triggered
        """
        if not self.settings.web_search_enabled:
            return False

        # Use higher threshold for legislator/organization queries
        if page_context_type == "legislator":
            threshold = self.settings.web_search_legislator_confidence_threshold
        elif page_context_type == "organization":
            threshold = self.settings.web_search_organization_confidence_threshold
        else:
            threshold = self.settings.web_search_confidence_threshold

        # Check if RAG confidence is below threshold
        confidence_trigger = rag_confidence < threshold

        # Check if query is about current/recent events (force web search)
        current_events_trigger = False
        dispute_trigger = False
        if message:
            current_events_trigger = self._is_current_events_query(message)
            dispute_trigger = self._is_dispute_or_correction(message)

        should_search = confidence_trigger or current_events_trigger or dispute_trigger

        if should_search:
            logger.info(
                "Web search triggered",
                rag_confidence=rag_confidence,
                threshold=threshold,
                confidence_trigger=confidence_trigger,
                current_events_trigger=current_events_trigger,
                dispute_trigger=dispute_trigger,
                page_context_type=page_context_type,
            )

        return should_search

    def _is_current_events_query(self, message: str) -> bool:
        """
        Detect if the query is asking about current/recent events.

        Args:
            message: The user's message

        Returns:
            True if the query appears to be about current events
        """
        message_lower = message.lower()

        # Time-related keywords that suggest current events
        current_time_keywords = [
            "2026", "2025",  # Recent years
            "this year", "this month", "this week", "today",
            "recently", "latest", "current", "now",
            "just passed", "just introduced", "newly",
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ]

        # Action keywords that suggest recent activity
        recent_action_keywords = [
            "what happened", "what's happening", "what is happening",
            "any news", "any updates", "recent news",
            "passed congress", "signed into law", "introduced",
            "being debated", "under consideration",
        ]

        for keyword in current_time_keywords:
            if keyword in message_lower:
                return True

        for phrase in recent_action_keywords:
            if phrase in message_lower:
                return True

        return False

    def _is_dispute_or_correction(self, message: str) -> bool:
        """
        Detect if the user is disputing or correcting previous information.

        When users say things like "that's wrong" or "she is a senator",
        they're indicating our information is outdated. Trigger web search
        to get current information.

        Args:
            message: The user's message

        Returns:
            True if the user appears to be correcting information
        """
        message_lower = message.lower()

        # Explicit dispute phrases
        dispute_phrases = [
            "that's wrong", "that is wrong", "this is wrong",
            "that's incorrect", "that is incorrect", "this is incorrect",
            "that's not true", "that is not true", "this is not true",
            "that's not right", "that is not right", "you're wrong",
            "actually,", "actually she", "actually he",
            "no,", "wrong.", "incorrect.",
            # Strong disagreement
            "no way", "there's no way", "there is no way",
            "that can't be", "that cannot be", "can't be right",
            "doesn't sound right", "doesn't seem right",
            "i don't believe", "i don't think that's",
            "bull", "nonsense", "impossible",
        ]

        # Verification request phrases (user asking to double-check)
        verification_phrases = [
            "be sure", "make sure", "to be sure",
            "double check", "double-check", "doublecheck",
            "verify", "confirm", "check again",
            "try again", "look again",
            "search for", "look up", "look it up",
            "do a web search", "search the web", "web search",
            "check your sources", "check the source",
            "are you sure", "you sure about that",
            "can you verify", "can you confirm",
            "check openstates", "check open states",
            "check congress", "official record",
        ]

        # Correction phrases (user stating what they believe is true)
        correction_phrases = [
            "she is a", "he is a", "they are a",
            "she's a", "he's a", "they're a",
            "is now a", "is currently a", "is the",
            "was appointed", "was elected", "became",
        ]

        for phrase in dispute_phrases:
            if phrase in message_lower:
                logger.info("Dispute detected, triggering verification", phrase=phrase)
                return True

        for phrase in verification_phrases:
            if phrase in message_lower:
                logger.info("Verification request detected", phrase=phrase)
                return True

        for phrase in correction_phrases:
            if phrase in message_lower:
                logger.info("Correction detected, triggering verification", phrase=phrase)
                return True

        return False

    async def _perform_web_search(
        self,
        query: str,
        page_context: PageContext,
    ) -> list[WebSearchResult]:
        """
        Perform web search based on the query and page context.

        Args:
            query: The user's query
            page_context: Context about the current page

        Returns:
            List of web search results
        """
        try:
            # Use specialized search based on page type
            if page_context.type == "bill":
                # Include bill info in search query
                search_query = query
                if page_context.title:
                    search_query = f"{page_context.title} {query}"
                return await self.web_search.search_legislation(search_query)

            elif page_context.type == "legislator":
                search_query = query
                if page_context.title:
                    search_query = f"{page_context.title} {query}"
                return await self.web_search.search_legislator(search_query)

            else:
                # General search
                return await self.web_search.search(query)

        except Exception as e:
            logger.error("Web search failed", error=str(e))
            return []
