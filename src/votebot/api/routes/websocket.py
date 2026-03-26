"""WebSocket endpoint for streaming chat with Slack human handoff support."""

import asyncio
import json
import time
import uuid
from collections import Counter
from typing import Dict, Optional

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from votebot.api.schemas.chat import PageContext
from votebot.config import get_settings
from votebot.core.agent import VoteBotAgent
from votebot.services.redis_store import get_redis_store
from votebot.services.slack import get_slack_service, SlackService

logger = structlog.get_logger()
router = APIRouter()

# Conversation boundary: inactivity timeout in seconds
_CONVERSATION_INACTIVITY_TIMEOUT = 600  # 10 minutes

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

        # Start Redis pub/sub subscriber for cross-worker agent event delivery
        redis_store = get_redis_store()
        if redis_store.is_available:
            await redis_store.subscribe_agent_events(self._handle_redis_event)

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
                # Analytics: conversation tracking
                "visitor_id": None,
                "conversation_counter": 0,
                "conversation_message_counter": 0,
                "session_message_counter": 0,
                "last_message_time": None,
                "last_page_context_type": None,
                "last_page_context_id": None,
                "conversation_has_response": False,
                "conversation_start_time": None,
                "conversation_intents": [],
                "conversation_had_handoff": False,
                "conversation_had_fallback": False,
                "conversation_had_retrieval_miss": False,
                "entry_referrer": None,
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

            # Map thread to session for routing (local + Redis)
            thread_to_session[thread_ts] = session_id
            await get_redis_store().set_thread_mapping(thread_ts, session_id)

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
        Routes via Redis pub/sub so the worker owning the WebSocket delivers.
        """
        logger.info(
            "Handling agent message callback",
            thread_ts=thread_ts,
            agent_name=agent_name,
            message_preview=message[:50] if message else "",
        )

        # Look up session: local cache first, then Redis
        session_id = thread_to_session.get(thread_ts)
        if not session_id:
            session_id = await get_redis_store().get_session_for_thread(thread_ts)
        if not session_id:
            logger.warning("No session found for thread", thread_ts=thread_ts)
            return

        redis_store = get_redis_store()
        if redis_store.is_available:
            # Publish via Redis — the worker owning the WebSocket will deliver
            await redis_store.publish_agent_event("agent_message", session_id, {
                "text": message,
                "agent_name": agent_name,
            })
        else:
            # No Redis — deliver directly (single-worker mode)
            await self._deliver_agent_message(session_id, message, agent_name)

    async def _deliver_agent_message(self, session_id: str, message: str, agent_name: str):
        """Deliver an agent message to the user via WebSocket (local worker)."""
        # Store message in session
        self.add_message(session_id, "agent", message)

        # Send agent_joined on first message if not already sent
        session = self.get_session(session_id)
        if session and not session.get("agent_joined_sent"):
            await self.send_json(session_id, {
                "type": "agent_joined",
                "payload": {
                    "agent_name": agent_name,
                }
            })
            self.update_session(session_id, agent_joined_sent=True)

        # Send to user via WebSocket
        await self.send_json(session_id, {
            "type": "agent_message",
            "payload": {
                "text": message,
                "agent_name": agent_name,
            }
        })

        logger.info(
            "Agent message delivered to user",
            session_id=session_id,
            agent=agent_name,
        )

    async def _handle_handoff_resolved(self, thread_ts: str):
        """
        Handle handoff resolution (checkmark reaction in Slack).

        Called by SlackService when an agent reacts with checkmark.
        Routes via Redis pub/sub so the worker owning the WebSocket delivers.
        """
        # Look up session: local cache first, then Redis
        session_id = thread_to_session.get(thread_ts)
        if not session_id:
            session_id = await get_redis_store().get_session_for_thread(thread_ts)
        if not session_id:
            logger.warning("No session found for thread", thread_ts=thread_ts)
            return

        redis_store = get_redis_store()
        if redis_store.is_available:
            # Publish via Redis — the worker owning the WebSocket will deliver
            await redis_store.publish_agent_event("agent_left", session_id, {
                "thread_ts": thread_ts,
            })
        else:
            # No Redis — deliver directly (single-worker mode)
            await self._deliver_handoff_resolved(session_id, thread_ts)

        # Clean up mappings (local + Redis) regardless of which worker
        thread_to_session.pop(thread_ts, None)
        await redis_store.remove_thread_mapping(thread_ts)

        # Send resolved message to Slack
        if self._slack_service:
            await self._slack_service.send_handoff_resolved_message(thread_ts)

        logger.info("Handoff resolved", session_id=session_id)

    async def _deliver_handoff_resolved(self, session_id: str, thread_ts: str):
        """Deliver handoff resolved event to user via WebSocket (local worker)."""
        self.update_session(
            session_id,
            handoff_active=False,
            agent_joined_sent=False,
        )
        await self.send_json(session_id, {
            "type": "agent_left",
            "payload": {}
        })

    async def _handle_redis_event(self, event_data: dict):
        """Handle an agent event received via Redis pub/sub.

        Only delivers if THIS worker owns the WebSocket for the session.
        """
        session_id = event_data.get("session_id")
        event_type = event_data.get("event_type")
        payload = event_data.get("payload", {})

        if not session_id or not event_type:
            return

        # Only deliver if this worker owns the WebSocket connection
        if session_id not in self.active_connections:
            return

        if event_type == "agent_message":
            await self._deliver_agent_message(
                session_id,
                payload.get("text", ""),
                payload.get("agent_name", "Agent"),
            )

        elif event_type == "agent_left":
            thread_ts = payload.get("thread_ts")
            await self._deliver_handoff_resolved(session_id, thread_ts)
            # Clean up local mapping
            if thread_ts:
                thread_to_session.pop(thread_ts, None)


manager = ConnectionManager()


def _get_conversation_id(session_id: str, counter: int) -> str:
    """Build conversation_id from session_id and counter."""
    return f"{session_id}:{counter}"


def _check_conversation_boundary(session: dict, page_context_data: dict) -> bool:
    """Check if a new conversation should start.

    Boundary evaluation happens BEFORE message_received is emitted,
    so each message is logged against its final assigned conversation_id.

    Returns True if a new conversation should be started.
    """
    # Rule 1: Explicit reset handled elsewhere (new session)

    # Rule 2: Inactivity timeout
    last_time = session.get("last_message_time")
    if last_time and (time.time() - last_time) > _CONVERSATION_INACTIVITY_TIMEOUT:
        return True

    # Rule 3: Page type changed
    new_type = page_context_data.get("type", "general")
    old_type = session.get("last_page_context_type")
    if old_type and old_type != new_type:
        return True

    # Rule 4: Page ID changed within same type (only if previous conversation had a response)
    if old_type and old_type == new_type and new_type != "general":
        new_id = page_context_data.get("slug") or page_context_data.get("webflow_id") or page_context_data.get("id")
        old_id = session.get("last_page_context_id")
        if old_id and new_id and old_id != new_id and session.get("conversation_has_response"):
            return True

    return False


async def _emit_conversation_ended(session_id: str, session: dict) -> None:
    """Emit a conversation_ended event for the current conversation."""
    try:
        from votebot.services.query_logger import get_query_logger

        query_logger = get_query_logger()
        if query_logger is None:
            return

        # Determine terminal state
        terminal_state = "navigated"  # Default for boundary detection

        # Determine dominant intent
        intents = session.get("conversation_intents", [])
        dominant = None
        if intents:
            counts = Counter(intents)
            dominant = counts.most_common(1)[0][0]

        start_time = session.get("conversation_start_time")
        duration = int(time.time() - start_time) if start_time else 0

        conv_id = _get_conversation_id(session_id, session.get("conversation_counter", 0))

        asyncio.create_task(
            query_logger.log_event(
                event_type="conversation_ended",
                visitor_id=session.get("visitor_id"),
                session_id=session_id,
                conversation_id=conv_id,
                turn_count=session.get("conversation_message_counter", 0),
                duration_seconds=duration,
                handoff_occurred=session.get("conversation_had_handoff", False),
                fallback_occurred=session.get("conversation_had_fallback", False),
                retrieval_miss_occurred=session.get("conversation_had_retrieval_miss", False),
                terminal_state=terminal_state,
                primary_intents_seen=list(set(intents)),
                dominant_primary_intent=dominant,
            )
        )
    except Exception:
        logger.warning("Failed to emit conversation_ended", exc_info=True)


def _start_new_conversation(session: dict) -> None:
    """Reset conversation-level tracking fields."""
    session["conversation_counter"] = session.get("conversation_counter", 0) + 1
    session["conversation_message_counter"] = 0
    session["conversation_has_response"] = False
    session["conversation_start_time"] = time.time()
    session["conversation_intents"] = []
    session["conversation_had_handoff"] = False
    session["conversation_had_fallback"] = False
    session["conversation_had_retrieval_miss"] = False


async def _emit_conversation_ended_with_state(
    session_id: str, session: dict, terminal_state: str
) -> None:
    """Emit conversation_ended with a specific terminal_state."""
    try:
        from votebot.services.query_logger import get_query_logger

        query_logger = get_query_logger()
        if query_logger is None:
            return

        intents = session.get("conversation_intents", [])
        dominant = None
        if intents:
            counts = Counter(intents)
            dominant = counts.most_common(1)[0][0]

        start_time = session.get("conversation_start_time")
        duration = int(time.time() - start_time) if start_time else 0
        conv_id = _get_conversation_id(session_id, session.get("conversation_counter", 0))

        asyncio.create_task(
            query_logger.log_event(
                event_type="conversation_ended",
                visitor_id=session.get("visitor_id"),
                session_id=session_id,
                conversation_id=conv_id,
                turn_count=session.get("conversation_message_counter", 0),
                duration_seconds=duration,
                handoff_occurred=session.get("conversation_had_handoff", False),
                fallback_occurred=session.get("conversation_had_fallback", False),
                retrieval_miss_occurred=session.get("conversation_had_retrieval_miss", False),
                terminal_state=terminal_state,
                primary_intents_seen=list(set(intents)),
                dominant_primary_intent=dominant,
            )
        )
    except Exception:
        logger.warning("Failed to emit conversation_ended", exc_info=True)


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

    # Extract client IP and User-Agent for query logging
    client_ip = websocket.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        websocket.client.host if websocket.client else None
    )
    user_agent = websocket.headers.get("user-agent")

    try:
        while True:
            # Receive message from client
            data = await websocket.receive_json()
            await handle_client_message(session_id, data, client_ip=client_ip, user_agent=user_agent)
    except WebSocketDisconnect:
        # Emit conversation_ended on disconnect
        session = manager.get_session(session_id)
        if session and session.get("conversation_message_counter", 0) > 0:
            session_copy = dict(session)
            # Determine if this was mid-turn (abandoned) or natural end
            if session_copy.get("conversation_has_response"):
                terminal = "inactive_end"
            else:
                terminal = "abandoned"
            await _emit_conversation_ended_with_state(session_id, session_copy, terminal)
        manager.disconnect(session_id)
    except Exception as e:
        logger.exception("WebSocket error", session_id=session_id, error=str(e))
        session = manager.get_session(session_id)
        if session and session.get("conversation_message_counter", 0) > 0:
            await _emit_conversation_ended_with_state(session_id, dict(session), "abandoned")
        manager.disconnect(session_id)


async def handle_client_message(
    session_id: str,
    data: dict,
    client_ip: str | None = None,
    user_agent: str | None = None,
):
    """Handle incoming client message."""
    message_type = data.get("type")
    payload = data.get("payload", {})

    if message_type == "user_message":
        await handle_user_message(session_id, payload, client_ip=client_ip, user_agent=user_agent)
    elif message_type == "context_update":
        await handle_context_update(session_id, payload)
    elif message_type == "confirm_handoff":
        await handle_confirm_handoff(session_id)
    elif message_type == "ping":
        await manager.send_json(session_id, {"type": "pong"})
    else:
        logger.warning("Unknown message type", type=message_type, session_id=session_id)


async def handle_confirm_handoff(session_id: str):
    """Handle user confirmation to initiate human handoff."""
    session = manager.get_session(session_id)
    if not session:
        return

    # Get the pending handoff context
    pending_message = session.get("pending_handoff_message", "")
    page_context = session.get("page_context", {"type": "general"})

    # Initiate the handoff
    await manager.initiate_handoff(
        session_id=session_id,
        message=pending_message,
        page_context=page_context,
    )

    # Clear pending handoff
    manager.update_session(session_id, pending_handoff_message=None)

    logger.info("Handoff confirmed by user", session_id=session_id)


async def handle_context_update(session_id: str, payload: dict):
    """Handle page context change — inject a system note so the LLM knows the user navigated."""
    page_context_data = payload.get("page_context", {"type": "general"})

    # Update stored page context
    manager.update_session(session_id, page_context=page_context_data)

    # Build a note for the LLM based on the new context
    ctx_type = page_context_data.get("type", "general")
    title = page_context_data.get("title", "")
    if ctx_type == "general":
        note = "The user has navigated to a general page. They may now ask about any topic."
    elif title:
        note = f"The user has navigated to a new {ctx_type} page: {title}. Answer subsequent questions about this {ctx_type}, not previous ones."
    else:
        note = f"The user has navigated to a new {ctx_type} page. Answer subsequent questions about this {ctx_type}, not previous ones."

    # Add as a system message in conversation history
    manager.add_message(session_id, "system", note)

    logger.info(
        "Page context updated",
        session_id=session_id,
        new_type=ctx_type,
        new_title=title,
    )


async def handle_user_message(
    session_id: str,
    payload: dict,
    client_ip: str | None = None,
    user_agent: str | None = None,
):
    """Process user message and stream response (or relay to agent)."""
    message = payload.get("message", "").strip()
    page_context_data = payload.get("page_context", {"type": "general"})

    if not message:
        await manager.send_json(session_id, {
            "type": "error",
            "payload": {"code": "empty_message", "message": "Message cannot be empty"}
        })
        return

    # Extract analytics fields from payload
    visitor_id = payload.get("visitor_id")
    entry_referrer = payload.get("entry_referrer")
    page_url = payload.get("page_url")
    scroll_depth = payload.get("scroll_depth")
    time_on_page = payload.get("time_on_page")

    # Store user message
    manager.add_message(session_id, "user", message)

    # Update page context in session
    manager.update_session(session_id, page_context=page_context_data)

    # --- Analytics: conversation boundary detection ---
    session = manager.get_session(session_id)
    if session:
        # Store visitor_id (first message sets it, subsequent messages preserve it)
        if visitor_id:
            session["visitor_id"] = visitor_id
        # Store entry_referrer on first message only
        if entry_referrer and session.get("entry_referrer") is None:
            session["entry_referrer"] = entry_referrer

        # Boundary evaluation happens BEFORE message_received
        if session.get("conversation_start_time") is None:
            # First message in session — start first conversation
            _start_new_conversation(session)
        elif _check_conversation_boundary(session, page_context_data):
            # Emit conversation_ended for previous conversation
            await _emit_conversation_ended(session_id, session)
            _start_new_conversation(session)

        # Update counters
        session["session_message_counter"] = session.get("session_message_counter", 0) + 1
        session["conversation_message_counter"] = session.get("conversation_message_counter", 0) + 1
        session["last_message_time"] = time.time()
        session["last_page_context_type"] = page_context_data.get("type", "general")
        session["last_page_context_id"] = (
            page_context_data.get("slug")
            or page_context_data.get("webflow_id")
            or page_context_data.get("id")
        )

        conversation_id = _get_conversation_id(session_id, session.get("conversation_counter", 0))
        session_message_index = session["session_message_counter"]
        conversation_message_index = session["conversation_message_counter"]

        # Emit message_received event
        try:
            from votebot.services.query_logger import get_query_logger

            query_logger = get_query_logger()
            if query_logger:
                asyncio.create_task(
                    query_logger.log_event(
                        event_type="message_received",
                        visitor_id=session.get("visitor_id"),
                        session_id=session_id,
                        conversation_id=conversation_id,
                        session_message_index=session_message_index,
                        conversation_message_index=conversation_message_index,
                        message=message,
                        page_context={
                            "type": page_context_data.get("type", "general"),
                            "id": page_context_data.get("id"),
                            "title": page_context_data.get("title"),
                            "jurisdiction": page_context_data.get("jurisdiction"),
                            "webflow_id": page_context_data.get("webflow_id"),
                            "slug": page_context_data.get("slug"),
                        },
                        entry_referrer=session.get("entry_referrer") if session_message_index == 1 else None,
                        page_url=page_url,
                        client_ip=client_ip,
                        user_agent=user_agent,
                    )
                )
        except Exception:
            logger.warning("Failed to emit message_received", exc_info=True)
    else:
        conversation_id = None
        session_message_index = None
        conversation_message_index = None

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
    # Session can come from:
    # - "session-code" (Webflow CMS field name)
    # - "session" (returned by /content/resolve endpoint)
    # Do NOT use session-year, which is just the calendar year
    page_context = PageContext(
        type=page_context_data.get("type", "general"),
        id=page_context_data.get("id"),
        jurisdiction=page_context_data.get("jurisdiction"),
        session=page_context_data.get("session") or page_context_data.get("session-code"),
        title=page_context_data.get("title"),
        url=page_context_data.get("url"),
        slug=page_context_data.get("slug"),
        webflow_id=page_context_data.get("webflow_id"),
    )

    logger.info(
        "Processing WebSocket message",
        session_id=session_id,
        message_preview=message[:50],
        page_type=page_context.type,
        webflow_id=page_context.webflow_id,
        slug=page_context.slug,
        bill_id=page_context.id,
        bill_session=page_context.session,
        jurisdiction=page_context.jurisdiction,
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
            client_ip=client_ip,
            user_agent=user_agent,
            visitor_id=session.get("visitor_id") if session else None,
            conversation_id=conversation_id,
            session_message_index=session_message_index,
            conversation_message_index=conversation_message_index,
            entry_referrer=session.get("entry_referrer") if session and session_message_index == 1 else None,
            page_url=page_url,
            scroll_depth=scroll_depth,
            time_on_page=time_on_page,
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

                # Store pending handoff for user confirmation
                if requires_human:
                    manager.update_session(
                        session_id,
                        pending_handoff_message=message,
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

        # Update conversation tracking after response
        if session:
            session["conversation_has_response"] = True

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
