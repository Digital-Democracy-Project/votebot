"""Chat endpoint implementation."""

import time
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from votebot.api.middleware.auth import api_key_auth
from votebot.api.schemas.chat import (
    ChatRequest,
    ChatResponse,
    Citation,
    ResponseMetadata,
    StreamChunk,
    WebCitation,
)
from votebot.config import Settings, get_settings
from votebot.core.agent import VoteBotAgent

router = APIRouter(tags=["chat"])
logger = structlog.get_logger()


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Process a chat message",
    description="Process a user message and return an AI-generated response with citations.",
)
async def chat(
    request: ChatRequest,
    raw_request: Request,
    api_key: Annotated[str, Depends(api_key_auth)],
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    """
    Process a chat message and return a response.

    This endpoint handles:
    - Context-aware responses based on page_context
    - Human suppression when human_active=true
    - RAG-based retrieval and response generation
    - Citation extraction and confidence scoring
    """
    start_time = time.perf_counter()

    logger.info(
        "Processing chat request",
        session_id=request.session_id,
        page_type=request.page_context.type,
        human_active=request.human_active,
        message_length=len(request.message),
    )

    # Handle human suppression
    if request.human_active:
        logger.info(
            "Human active - suppressing LLM response",
            session_id=request.session_id,
        )
        return ChatResponse(
            response=None,
            citations=[],
            confidence=0.0,
            requires_human=False,
            suppressed=True,
            metadata=None,
        )

    # Extract client IP and User-Agent for query logging
    client_ip = raw_request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        raw_request.client.host if raw_request.client else None
    )
    user_agent = raw_request.headers.get("user-agent")

    # Extract analytics fields from client_metadata and navigation_context
    visitor_id = None
    entry_referrer = None
    page_url = None
    if request.client_metadata:
        visitor_id = request.client_metadata.client_id
        entry_referrer = request.client_metadata.entry_referrer
        page_url = request.client_metadata.page_url

    scroll_depth = None
    time_on_page = None
    if request.navigation_context:
        scroll_depth = request.navigation_context.scroll_depth
        time_on_page = request.navigation_context.time_on_page

    try:
        # Initialize the agent
        agent = VoteBotAgent(settings=settings)

        # Process the message
        result = await agent.process_message(
            message=request.message,
            session_id=request.session_id,
            page_context=request.page_context,
            navigation_context=request.navigation_context,
            conversation_history=request.conversation_history,
            channel="rest",
            human_active=request.human_active,
            client_ip=client_ip,
            user_agent=user_agent,
            visitor_id=visitor_id,
            entry_referrer=entry_referrer,
            page_url=page_url,
            scroll_depth=scroll_depth,
            time_on_page=time_on_page,
        )

        # Calculate latency
        latency_ms = int((time.perf_counter() - start_time) * 1000)

        # Convert web citations if present
        web_citations = []
        if result.web_citations:
            for wc in result.web_citations:
                web_citations.append(WebCitation(
                    url=wc.url,
                    title=wc.title,
                    snippet=wc.snippet,
                ))

        # Build response
        response = ChatResponse(
            response=result.response,
            citations=result.citations,
            confidence=result.confidence,
            requires_human=result.requires_human,
            suppressed=False,
            web_search_used=result.web_search_used,
            web_citations=web_citations,
            bill_votes_tool_used=result.bill_votes_tool_used,
            metadata=ResponseMetadata(
                model=settings.openai_model,
                tokens_used=result.tokens_used,
                retrieval_count=result.retrieval_count,
                latency_ms=latency_ms,
                cached=result.cached,
            ),
        )

        logger.info(
            "Chat response generated",
            session_id=request.session_id,
            latency_ms=latency_ms,
            confidence=result.confidence,
            citation_count=len(result.citations),
            bill_votes_tool_used=result.bill_votes_tool_used,
        )

        return response

    except Exception as e:
        logger.exception(
            "Error processing chat request",
            session_id=request.session_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing chat request: {str(e)}",
        )


@router.post(
    "/chat/stream",
    summary="Process a chat message with streaming response",
    description="Process a user message and stream the AI-generated response.",
)
async def chat_stream(
    request: ChatRequest,
    raw_request: Request,
    api_key: Annotated[str, Depends(api_key_auth)],
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """
    Process a chat message and stream the response.

    Returns a streaming response for real-time display of the AI response.
    Useful for achieving first-token latency targets.
    """
    logger.info(
        "Processing streaming chat request",
        session_id=request.session_id,
        page_type=request.page_context.type,
    )

    # Handle human suppression
    if request.human_active:
        async def empty_stream():
            chunk = StreamChunk(chunk="", done=True)
            yield f"data: {chunk.model_dump_json()}\n\n"

        return StreamingResponse(
            empty_stream(),
            media_type="text/event-stream",
        )

    # Extract client IP and User-Agent for query logging
    client_ip = raw_request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        raw_request.client.host if raw_request.client else None
    )
    user_agent = raw_request.headers.get("user-agent")

    # Extract analytics fields
    visitor_id_stream = None
    entry_referrer_stream = None
    page_url_stream = None
    if request.client_metadata:
        visitor_id_stream = request.client_metadata.client_id
        entry_referrer_stream = request.client_metadata.entry_referrer
        page_url_stream = request.client_metadata.page_url

    scroll_depth_stream = None
    time_on_page_stream = None
    if request.navigation_context:
        scroll_depth_stream = request.navigation_context.scroll_depth
        time_on_page_stream = request.navigation_context.time_on_page

    async def generate_stream():
        """Generate streaming response chunks."""
        try:
            agent = VoteBotAgent(settings=settings)

            async for chunk_data in agent.process_message_stream(
                message=request.message,
                session_id=request.session_id,
                page_context=request.page_context,
                navigation_context=request.navigation_context,
                conversation_history=request.conversation_history,
                client_ip=client_ip,
                user_agent=user_agent,
                visitor_id=visitor_id_stream,
                entry_referrer=entry_referrer_stream,
                page_url=page_url_stream,
                scroll_depth=scroll_depth_stream,
                time_on_page=time_on_page_stream,
            ):
                chunk = StreamChunk(
                    chunk=chunk_data.text,
                    done=chunk_data.done,
                    citations=chunk_data.citations if chunk_data.done else None,
                    metadata=chunk_data.metadata if chunk_data.done else None,
                )
                yield f"data: {chunk.model_dump_json()}\n\n"

        except Exception as e:
            logger.exception(
                "Error in streaming response",
                session_id=request.session_id,
                error=str(e),
            )
            error_chunk = StreamChunk(chunk=f"Error: {str(e)}", done=True)
            yield f"data: {error_chunk.model_dump_json()}\n\n"

    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post(
    "/chat/feedback",
    summary="Submit feedback for a chat response",
    description="Submit user feedback (thumbs up/down) for a chat response.",
)
async def chat_feedback(
    session_id: str,
    message_id: str,
    feedback: str,
    api_key: Annotated[str, Depends(api_key_auth)],
) -> dict:
    """
    Submit feedback for a chat response.

    Used for tracking response quality and improving the system.
    """
    logger.info(
        "Feedback received",
        session_id=session_id,
        message_id=message_id,
        feedback=feedback,
    )

    # TODO: Store feedback in database for analysis
    return {
        "status": "received",
        "session_id": session_id,
        "message_id": message_id,
    }
