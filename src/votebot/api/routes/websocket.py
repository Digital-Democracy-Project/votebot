"""WebSocket endpoint for streaming chat."""

import asyncio
import json
import time
import uuid
from typing import Dict

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from votebot.api.schemas.chat import PageContext
from votebot.config import get_settings
from votebot.core.agent import VoteBotAgent

logger = structlog.get_logger()
router = APIRouter()

# Simple in-memory session store for POC
# In production, use Redis
sessions: Dict[str, dict] = {}


class ConnectionManager:
    """Manages WebSocket connections."""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, session_id: str):
        """Accept and register a new connection."""
        await websocket.accept()
        self.active_connections[session_id] = websocket

        # Initialize session if new
        if session_id not in sessions:
            sessions[session_id] = {
                "created_at": time.time(),
                "messages": [],
                "handoff_active": False,
            }

        logger.info("WebSocket connected", session_id=session_id)

    def disconnect(self, session_id: str):
        """Remove connection but preserve session."""
        if session_id in self.active_connections:
            del self.active_connections[session_id]
        logger.info("WebSocket disconnected", session_id=session_id)

    async def send_json(self, session_id: str, data: dict):
        """Send JSON message to a specific session."""
        if websocket := self.active_connections.get(session_id):
            try:
                await websocket.send_json(data)
            except Exception as e:
                logger.error("Failed to send message", session_id=session_id, error=str(e))

    def get_session(self, session_id: str) -> dict | None:
        """Get session data."""
        return sessions.get(session_id)

    def add_message(self, session_id: str, role: str, content: str):
        """Add message to session history."""
        if session_id in sessions:
            sessions[session_id]["messages"].append({
                "role": role,
                "content": content,
                "timestamp": time.time()
            })
            # Keep last 20 messages
            sessions[session_id]["messages"] = sessions[session_id]["messages"][-20:]


manager = ConnectionManager()


@router.websocket("/ws/chat")
async def websocket_chat_endpoint(
    websocket: WebSocket,
    session_id: str = Query(default=None),
):
    """
    WebSocket endpoint for streaming chat.

    Protocol:
    - Client sends: {"type": "user_message", "payload": {"message": "...", "page_context": {...}}}
    - Server sends: {"type": "stream_start"}
    - Server sends: {"type": "stream_chunk", "payload": {"text": "..."}}
    - Server sends: {"type": "stream_end", "payload": {"citations": [...], "confidence": 0.85}}
    """
    # Generate session ID if not provided
    if not session_id:
        session_id = str(uuid.uuid4())[:12]

    await manager.connect(websocket, session_id)

    # Send session info
    await manager.send_json(session_id, {
        "type": "session_info",
        "payload": {
            "session_id": session_id,
            "restored": len(sessions.get(session_id, {}).get("messages", [])) > 0
        }
    })

    # Send any existing messages (for reconnection)
    session = manager.get_session(session_id)
    if session and session["messages"]:
        await manager.send_json(session_id, {
            "type": "session_restored",
            "payload": {
                "messages": session["messages"]
            }
        })

    try:
        while True:
            # Receive message from client
            data = await websocket.receive_json()
            await handle_client_message(session_id, data)
    except WebSocketDisconnect:
        manager.disconnect(session_id)
    except Exception as e:
        logger.exception("WebSocket error", session_id=session_id, error=str(e))
        manager.disconnect(session_id)


async def handle_client_message(session_id: str, data: dict):
    """Handle incoming client message."""
    message_type = data.get("type")
    payload = data.get("payload", {})

    if message_type == "user_message":
        await handle_user_message(session_id, payload)
    elif message_type == "ping":
        await manager.send_json(session_id, {"type": "pong"})
    else:
        logger.warning("Unknown message type", type=message_type, session_id=session_id)


async def handle_user_message(session_id: str, payload: dict):
    """Process user message and stream response."""
    message = payload.get("message", "").strip()
    page_context_data = payload.get("page_context", {"type": "general"})

    if not message:
        await manager.send_json(session_id, {
            "type": "error",
            "payload": {"code": "empty_message", "message": "Message cannot be empty"}
        })
        return

    # Store user message
    manager.add_message(session_id, "user", message)

    # Get conversation history for context
    session = manager.get_session(session_id)
    conversation_history = session.get("messages", [])[:-1] if session else []  # Exclude current message

    # Build page context
    page_context = PageContext(
        type=page_context_data.get("type", "general"),
        id=page_context_data.get("id"),
        jurisdiction=page_context_data.get("jurisdiction"),
        title=page_context_data.get("title"),
        url=page_context_data.get("url"),
    )

    logger.info(
        "Processing WebSocket message",
        session_id=session_id,
        message_preview=message[:50],
        page_type=page_context.type,
    )

    # Send stream start
    await manager.send_json(session_id, {"type": "stream_start"})

    try:
        # Create agent and stream response
        agent = VoteBotAgent()
        full_response = ""
        citations = []
        confidence = 0.0
        requires_human = False
        retrieval_count = 0

        async for chunk in agent.process_message_stream(
            message=message,
            session_id=session_id,
            page_context=page_context,
            conversation_history=conversation_history,
        ):
            if chunk.done:
                # Final chunk with metadata
                citations = [
                    {
                        "source": c.source,
                        "document_id": c.document_id,
                        "excerpt": c.excerpt,
                        "url": c.url,
                        "relevance_score": c.relevance_score,
                    }
                    for c in (chunk.citations or [])
                ]

                # Get retrieval count from metadata
                if chunk.metadata:
                    retrieval_count = getattr(chunk.metadata, "retrieval_count", 0)

                # Calculate confidence (same logic as agent._calculate_confidence)
                confidence = calculate_confidence(
                    response=full_response,
                    retrieval_count=retrieval_count,
                    citations=citations,
                )

                # Check for human handoff triggers in message
                requires_human = check_human_handoff(message, full_response, confidence)

                await manager.send_json(session_id, {
                    "type": "stream_end",
                    "payload": {
                        "citations": citations,
                        "confidence": confidence,
                        "requires_human": requires_human,
                    }
                })
            else:
                # Stream chunk
                full_response += chunk.text
                await manager.send_json(session_id, {
                    "type": "stream_chunk",
                    "payload": {"text": chunk.text}
                })

        # Store assistant message
        manager.add_message(session_id, "assistant", full_response)

        logger.info(
            "WebSocket message processed",
            session_id=session_id,
            response_length=len(full_response),
            confidence=confidence,
            requires_human=requires_human,
        )

    except Exception as e:
        logger.exception("Error processing message", session_id=session_id)
        await manager.send_json(session_id, {
            "type": "error",
            "payload": {
                "code": "processing_error",
                "message": "Sorry, I encountered an error processing your message. Please try again."
            }
        })


def calculate_confidence(
    response: str,
    retrieval_count: int,
    citations: list[dict],
) -> float:
    """
    Calculate confidence score for the response.

    Replicates logic from VoteBotAgent._calculate_confidence.
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
        relevance_scores = [c.get("relevance_score") or 0 for c in citations]
        if relevance_scores:
            avg_relevance = sum(relevance_scores) / len(relevance_scores)
            confidence += avg_relevance * 0.1

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


def check_human_handoff(message: str, response: str, confidence: float) -> bool:
    """Check if conversation should be handed off to human."""
    message_lower = message.lower()

    # Explicit human request
    human_phrases = [
        "speak to a human",
        "talk to a person",
        "real person",
        "human agent",
        "customer service",
        "representative",
        "talk to someone",
        "speak with someone",
    ]
    for phrase in human_phrases:
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

    return False
