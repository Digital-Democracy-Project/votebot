"""WebSocket endpoint for streaming chat with Slack human handoff support."""

import asyncio
import json
import time
import uuid
from typing import Dict, Optional

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from votebot.api.schemas.chat import PageContext
from votebot.config import get_settings
from votebot.core.agent import VoteBotAgent
from votebot.services.slack import get_slack_service, SlackService

logger = structlog.get_logger()
router = APIRouter()

# Session store - uses Redis in production via RedisSessionStore
# Falls back to in-memory dict for development
sessions: Dict[str, dict] = {}

# Mapping of Slack thread_ts to session_id for routing agent messages
thread_to_session: Dict[str, str] = {}


class ConnectionManager:
    """Manages WebSocket connections and sessions."""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self._slack_service: Optional[SlackService] = None
        self._slack_started = False

    async def _ensure_slack_started(self):
        """Ensure Slack service is started (once)."""
        if self._slack_started:
            return

        self._slack_service = get_slack_service()
        if self._slack_service.is_configured:
            await self._slack_service.start(
                on_agent_message=self._handle_agent_message,
                on_handoff_resolved=self._handle_handoff_resolved,
            )
            self._slack_started = True
            logger.info("Slack service started for handoff support")

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
                "slack_thread_ts": None,
                "page_context": {"type": "general"},
            }

        # Start Slack service on first connection
        await self._ensure_slack_started()

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

    def update_session(self, session_id: str, **kwargs):
        """Update session data."""
        if session_id in sessions:
            sessions[session_id].update(kwargs)

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

    async def initiate_handoff(self, session_id: str, message: str, page_context: dict):
        """
        Initiate human handoff by creating a Slack thread.

        Args:
            session_id: Chat session ID
            message: User's latest message
            page_context: Current page context
        """
        if not self._slack_service or not self._slack_service.is_configured:
            logger.warning("Slack not configured, cannot initiate handoff")
            return

        session = self.get_session(session_id)
        if not session:
            return

        # Already in handoff
        if session.get("handoff_active"):
            return

        conversation_history = session.get("messages", [])

        # Create Slack thread
        thread_ts = await self._slack_service.create_handoff_thread(
            session_id=session_id,
            page_context=page_context,
            latest_message=message,
            conversation_history=conversation_history,
        )

        if thread_ts:
            # Update session
            self.update_session(
                session_id,
                handoff_active=True,
                slack_thread_ts=thread_ts,
            )

            # Map thread to session for routing
            thread_to_session[thread_ts] = session_id

            logger.info(
                "Handoff initiated",
                session_id=session_id,
                thread_ts=thread_ts,
            )

    async def relay_to_slack(self, session_id: str, message: str):
        """Relay user message to Slack thread during handoff."""
        if not self._slack_service:
            return

        session = self.get_session(session_id)
        if not session or not session.get("handoff_active"):
            return

        thread_ts = session.get("slack_thread_ts")
        if thread_ts:
            await self._slack_service.relay_user_message(thread_ts, message)

    async def _handle_agent_message(self, thread_ts: str, agent_name: str, message: str):
        """
        Handle incoming agent message from Slack.

        Called by SlackService when an agent replies in a thread.
        """
        session_id = thread_to_session.get(thread_ts)
        if not session_id:
            logger.warning("No session found for thread", thread_ts=thread_ts)
            return

        # Store message in session
        self.add_message(session_id, "agent", message)

        # Send to user via WebSocket
        await self.send_json(session_id, {
            "type": "agent_message",
            "payload": {
                "text": message,
                "agent_name": agent_name,
            }
        })

        # Also send agent_joined on first message if not already sent
        session = self.get_session(session_id)
        if session and not session.get("agent_joined_sent"):
            await self.send_json(session_id, {
                "type": "agent_joined",
                "payload": {
                    "agent_name": agent_name,
                }
            })
            self.update_session(session_id, agent_joined_sent=True)

        logger.info(
            "Agent message relayed to user",
            session_id=session_id,
            agent=agent_name,
        )

    async def _handle_handoff_resolved(self, thread_ts: str):
        """
        Handle handoff resolution (checkmark reaction in Slack).

        Called by SlackService when an agent reacts with checkmark.
        """
        session_id = thread_to_session.get(thread_ts)
        if not session_id:
            logger.warning("No session found for thread", thread_ts=thread_ts)
            return

        # Update session
        self.update_session(
            session_id,
            handoff_active=False,
            agent_joined_sent=False,
        )

        # Remove thread mapping
        if thread_ts in thread_to_session:
            del thread_to_session[thread_ts]

        # Notify user
        await self.send_json(session_id, {
            "type": "agent_left",
            "payload": {}
        })

        # Send resolved message to Slack
        if self._slack_service:
            await self._slack_service.send_handoff_resolved_message(thread_ts)

        logger.info("Handoff resolved", session_id=session_id)


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
    - Server sends: {"type": "stream_end", "payload": {"citations": [...], "confidence": 0.85, "requires_human": false}}

    Human Handoff Events:
    - Server sends: {"type": "agent_joined", "payload": {"agent_name": "..."}}
    - Server sends: {"type": "agent_message", "payload": {"text": "...", "agent_name": "..."}}
    - Server sends: {"type": "agent_left", "payload": {}}
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
    """Process user message and stream response (or relay to agent)."""
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

    # Update page context in session
    manager.update_session(session_id, page_context=page_context_data)

    # Check if handoff is active - relay to Slack instead of bot
    session = manager.get_session(session_id)
    if session and session.get("handoff_active"):
        await manager.relay_to_slack(session_id, message)
        logger.info(
            "Message relayed to agent",
            session_id=session_id,
            message_preview=message[:50],
        )
        return

    # Normal bot processing
    # Get conversation history, excluding current message
    raw_history = session.get("messages", [])[:-1] if session else []

    # Convert 'agent' roles to 'assistant' for OpenAI compatibility
    # (human agent messages during handoff are stored with role 'agent')
    conversation_history = []
    for msg in raw_history:
        if msg.get("role") == "agent":
            conversation_history.append({**msg, "role": "assistant"})
        else:
            conversation_history.append(msg)

    # Build page context
    page_context = PageContext(
        type=page_context_data.get("type", "general"),
        id=page_context_data.get("id"),
        jurisdiction=page_context_data.get("jurisdiction"),
        title=page_context_data.get("title"),
        url=page_context_data.get("url"),
        slug=page_context_data.get("slug"),
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

                # Calculate confidence
                confidence = calculate_confidence(
                    response=full_response,
                    retrieval_count=retrieval_count,
                    citations=citations,
                )

                # Check for human handoff triggers
                requires_human = check_human_handoff(message, full_response, confidence)

                await manager.send_json(session_id, {
                    "type": "stream_end",
                    "payload": {
                        "citations": citations,
                        "confidence": confidence,
                        "requires_human": requires_human,
                    }
                })

                # Initiate handoff if needed
                if requires_human:
                    await manager.initiate_handoff(
                        session_id=session_id,
                        message=message,
                        page_context=page_context_data,
                    )
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
