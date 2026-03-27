# Technical Design: Streaming Chat Widget with Slack Human Handoff

> **Status:** Implemented. The WebSocket handler (`api/routes/websocket.py`) and chat widget (`chat-widget/src/`) described in this plan are live in production.
>
> **Subsequent changes (March 2026):** The WebSocket handler and chat widget were extended with user analytics and behavioral logging (see [user-analytics-logging.md](user-analytics-logging.md), commit `5d1870d`). Key additions to the architecture described below:
> - **`visitor_id`** (localStorage) sent in every `user_message` payload for cross-session tracking
> - **Conversation boundary detection** in the WebSocket handler — tracks `conversation_id`, `session_message_index`, `conversation_message_index`
> - **Three event types** emitted by the handler: `message_received`, `query_processed`, `conversation_ended`
> - **Engagement data** (`entry_referrer`, `page_url`, `scroll_depth`, `time_on_page`) sent from the widget
> - **Intent classification** and **grounding status** computed per query
>
> These changes extend the WebSocket handler and widget payload but do not modify the Slack handoff flow or session management described in this document. The Jigsaw opinion elicitation system ([PLAN-jigsaw-overview.md](PLAN-jigsaw-overview.md)) will add further WebSocket message types (`member_auth`) and session-level opinion tracking in future stages.

## Executive Summary

This document describes a new architecture for VoteBot that enables:
1. **True streaming responses** via a custom chat widget
2. **Human agent handoff** through Slack instead of Brevo
3. **Real-time bidirectional communication** between users and human agents

This replaces the current Brevo Conversations integration which does not support streaming responses.

---

## Table of Contents

1. [Goals and Non-Goals](#goals-and-non-goals)
2. [Architecture Overview](#architecture-overview)
3. [Component Design](#component-design)
4. [API Specifications](#api-specifications)
5. [Data Models](#data-models)
6. [Sequence Diagrams](#sequence-diagrams)
7. [Slack Integration](#slack-integration)
8. [Security Considerations](#security-considerations)
9. [Error Handling](#error-handling)
10. [Infrastructure Requirements](#infrastructure-requirements)
11. [Migration Plan](#migration-plan)
12. [Testing Strategy](#testing-strategy)
13. [Open Questions](#open-questions)

---

## Goals and Non-Goals

### Goals

| Goal | Description |
|------|-------------|
| **Streaming UX** | Users see bot responses as they're generated (first token <1.5s) |
| **Seamless Handoff** | Smooth transition from bot to human with full context |
| **Agent Mobility** | Human agents can respond via Slack mobile/desktop apps |
| **Conversation Continuity** | Full conversation history available to agents |
| **Session Persistence** | Users can refresh page without losing conversation |
| **Multi-agent Support** | Multiple agents can collaborate on complex queries |

### Non-Goals

| Non-Goal | Rationale |
|----------|-----------|
| Voice/Video Support | Out of scope for initial implementation |
| Multi-language Widget | English only for v1 |
| Offline Support | Requires significant additional complexity |
| Chat History Export | Can be added later |
| Brevo Compatibility | Intentionally replacing Brevo for chat |

---

## Architecture Overview

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              User's Browser                                  │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                     Custom Chat Widget (React)                       │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                  │   │
│  │  │   Message   │  │   Input     │  │   Status    │                  │   │
│  │  │   List      │  │   Box       │  │   Indicator │                  │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘                  │   │
│  └───────────────────────────┬─────────────────────────────────────────┘   │
│                              │ WebSocket                                    │
└──────────────────────────────┼──────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                           VoteBot Backend                                     │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │                      WebSocket Gateway                                  │  │
│  │  - Connection management                                                │  │
│  │  - Session tracking                                                     │  │
│  │  - Message routing                                                      │  │
│  └─────────────────┬──────────────────────────────────┬───────────────────┘  │
│                    │                                  │                      │
│                    ▼                                  ▼                      │
│  ┌─────────────────────────────┐    ┌─────────────────────────────────────┐  │
│  │      VoteBot Agent          │    │      Slack Integration Service      │  │
│  │  - RAG retrieval            │    │  - Thread management                │  │
│  │  - LLM streaming            │    │  - Message relay                    │  │
│  │  - Handoff detection        │    │  - Agent notifications              │  │
│  └─────────────────────────────┘    └─────────────────────────────────────┘  │
│                                                       │                      │
└───────────────────────────────────────────────────────┼──────────────────────┘
                                                        │ Socket Mode (WebSocket)
                                                        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              Slack Workspace                                  │
│  ┌─────────────────────────────────────────────────────────────────────────┐ │
│  │  #votebot-support                                                        │ │
│  │  ┌─────────────────────────────────────────────────────────────────┐    │ │
│  │  │ 🆘 Human assistance requested                                    │    │ │
│  │  │ User: session-abc123 | Page: /bills/hb-1234                     │    │ │
│  │  │ ─────────────────────────────────────────────────                │    │ │
│  │  │ │ Visitor: I want to talk to someone about this bill            │    │ │
│  │  │ │ Agent @sarah: Hi! I'm Sarah, how can I help?                  │    │ │
│  │  │ │ Visitor: Can you explain the impact on small businesses?      │    │ │
│  │  │ └── Thread replies...                                            │    │ │
│  │  └─────────────────────────────────────────────────────────────────┘    │ │
│  └─────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Technology Stack

| Component | Technology | Rationale |
|-----------|------------|-----------|
| Chat Widget | React + TypeScript | Modern, component-based, wide adoption |
| WebSocket Client | Native WebSocket API | Built-in, no dependencies |
| Backend Gateway | FastAPI + `websockets` | Already using FastAPI, native async |
| Session Store | Redis | Already configured, fast pub/sub |
| Slack Integration | `slack-bolt` + Socket Mode | Official SDK, real-time, no public endpoint |
| State Management | Redis + PostgreSQL | Sessions in Redis, history in PostgreSQL |

---

## Component Design

### 1. Chat Widget (Frontend)

**Location:** New repository or `packages/chat-widget/`

```
chat-widget/
├── src/
│   ├── components/
│   │   ├── ChatWidget.tsx        # Main container
│   │   ├── MessageList.tsx       # Message display with streaming
│   │   ├── MessageBubble.tsx     # Individual message rendering
│   │   ├── InputBox.tsx          # User input with send button
│   │   ├── TypingIndicator.tsx   # "Bot is typing..." animation
│   │   ├── AgentIndicator.tsx    # "Connected to Sarah" banner
│   │   └── ConnectionStatus.tsx  # Online/offline indicator
│   ├── hooks/
│   │   ├── useWebSocket.ts       # WebSocket connection management
│   │   ├── useChat.ts            # Chat state and actions
│   │   └── useSession.ts         # Session persistence
│   ├── types/
│   │   └── messages.ts           # TypeScript interfaces
│   ├── utils/
│   │   ├── websocket.ts          # WebSocket wrapper with reconnection
│   │   └── storage.ts            # LocalStorage helpers
│   └── index.tsx                 # Widget entry point
├── dist/                         # Built widget for embedding
└── package.json
```

**Key Features:**

```typescript
// types/messages.ts
interface ChatMessage {
  id: string;
  type: 'user' | 'bot' | 'agent' | 'system';
  content: string;
  timestamp: number;
  isStreaming?: boolean;
  agentName?: string;
  agentAvatar?: string;
  citations?: Citation[];
  metadata?: {
    confidence?: number;
    webSearchUsed?: boolean;
  };
}

interface WebSocketMessage {
  type: 'stream_start' | 'stream_chunk' | 'stream_end' |
        'agent_joined' | 'agent_message' | 'agent_left' |
        'error' | 'session_restored';
  payload: any;
}
```

**Embedding:**

```html
<!-- On DDP website -->
<script src="https://cdn.digitaldemocracy.org/chat-widget.js"></script>
<script>
  DDPChat.init({
    endpoint: 'wss://api.digitaldemocracy.org/ws/chat',
    pageContext: {
      type: 'bill',
      id: 'HB-1234',
      jurisdiction: 'FL',
      title: 'Education Funding Act',
      url: window.location.href
    }
  });
</script>
```

### 2. WebSocket Gateway (Backend)

**Location:** `src/votebot/api/websocket/`

```
src/votebot/api/websocket/
├── __init__.py
├── gateway.py          # WebSocket endpoint and connection handling
├── session.py          # Session management
├── handlers.py         # Message type handlers
└── protocol.py         # Message serialization/deserialization
```

**Gateway Implementation:**

```python
# src/votebot/api/websocket/gateway.py
from fastapi import WebSocket, WebSocketDisconnect
from typing import Dict
import structlog

logger = structlog.get_logger()

class ConnectionManager:
    """Manages active WebSocket connections."""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.session_store: SessionStore  # Redis-backed

    async def connect(self, websocket: WebSocket, session_id: str):
        """Accept connection and restore session if exists."""
        await websocket.accept()
        self.active_connections[session_id] = websocket

        # Restore session from Redis
        session = await self.session_store.get(session_id)
        if session:
            await self.send_session_restored(websocket, session)

        logger.info("WebSocket connected", session_id=session_id)

    async def disconnect(self, session_id: str):
        """Handle disconnection, preserve session."""
        if session_id in self.active_connections:
            del self.active_connections[session_id]
        # Session persists in Redis for reconnection
        logger.info("WebSocket disconnected", session_id=session_id)

    async def send_stream_chunk(self, session_id: str, chunk: str):
        """Send streaming chunk to client."""
        if ws := self.active_connections.get(session_id):
            await ws.send_json({
                "type": "stream_chunk",
                "payload": {"text": chunk}
            })

    async def send_agent_message(self, session_id: str, message: dict):
        """Relay agent message from Slack to user."""
        if ws := self.active_connections.get(session_id):
            await ws.send_json({
                "type": "agent_message",
                "payload": message
            })


manager = ConnectionManager()


@router.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket, session_id: str = None):
    """Main WebSocket endpoint for chat."""
    session_id = session_id or generate_session_id()

    await manager.connect(websocket, session_id)

    try:
        while True:
            data = await websocket.receive_json()
            await handle_message(session_id, data, manager)
    except WebSocketDisconnect:
        await manager.disconnect(session_id)
```

**Message Handlers:**

```python
# src/votebot/api/websocket/handlers.py

async def handle_message(session_id: str, data: dict, manager: ConnectionManager):
    """Route incoming messages to appropriate handler."""
    message_type = data.get("type")
    payload = data.get("payload", {})

    handlers = {
        "user_message": handle_user_message,
        "typing_start": handle_typing_start,
        "typing_stop": handle_typing_stop,
        "end_session": handle_end_session,
    }

    handler = handlers.get(message_type)
    if handler:
        await handler(session_id, payload, manager)
    else:
        logger.warning("Unknown message type", type=message_type)


async def handle_user_message(
    session_id: str,
    payload: dict,
    manager: ConnectionManager
):
    """Process user message - either bot response or relay to agent."""
    session = await manager.session_store.get(session_id)
    message = payload.get("message", "")
    page_context = payload.get("page_context", {})

    # Store message in history
    await manager.session_store.add_message(session_id, {
        "role": "user",
        "content": message,
        "timestamp": time.time()
    })

    if session and session.get("handoff_active"):
        # User is talking to human agent - relay to Slack
        await slack_service.relay_to_agent(
            thread_ts=session["slack_thread_ts"],
            message=message,
            session_id=session_id
        )
    else:
        # Bot handles the message with streaming
        await handle_bot_response(session_id, message, page_context, manager)


async def handle_bot_response(
    session_id: str,
    message: str,
    page_context: dict,
    manager: ConnectionManager
):
    """Generate streaming bot response."""
    agent = VoteBotAgent()

    # Send typing indicator
    await manager.send_json(session_id, {"type": "stream_start"})

    full_response = ""
    async for chunk in agent.process_message_stream(
        message=message,
        session_id=session_id,
        page_context=PageContext(**page_context),
    ):
        if chunk.done:
            # Final chunk with metadata
            await manager.send_json(session_id, {
                "type": "stream_end",
                "payload": {
                    "citations": [c.model_dump() for c in chunk.citations],
                    "metadata": chunk.metadata.model_dump() if chunk.metadata else None,
                    "requires_human": chunk.metadata.requires_human if chunk.metadata else False
                }
            })

            # Check for human handoff
            if chunk.metadata and chunk.metadata.requires_human:
                await initiate_handoff(session_id, message, page_context, manager)
        else:
            full_response += chunk.text
            await manager.send_stream_chunk(session_id, chunk.text)

    # Store bot response
    await manager.session_store.add_message(session_id, {
        "role": "assistant",
        "content": full_response,
        "timestamp": time.time()
    })
```

### 3. Session Management

**Location:** `src/votebot/api/websocket/session.py`

```python
# src/votebot/api/websocket/session.py
import json
from dataclasses import dataclass, asdict
from typing import Optional, List
import redis.asyncio as redis

@dataclass
class ChatSession:
    """Represents an active chat session."""
    session_id: str
    created_at: float
    page_context: dict
    messages: List[dict]
    handoff_active: bool = False
    slack_thread_ts: Optional[str] = None
    slack_channel: Optional[str] = None
    assigned_agent: Optional[str] = None


class SessionStore:
    """Redis-backed session storage."""

    SESSION_TTL = 86400  # 24 hours

    def __init__(self, redis_url: str):
        self.redis = redis.from_url(redis_url)

    async def create(self, session_id: str, page_context: dict) -> ChatSession:
        """Create a new session."""
        session = ChatSession(
            session_id=session_id,
            created_at=time.time(),
            page_context=page_context,
            messages=[]
        )
        await self._save(session)
        return session

    async def get(self, session_id: str) -> Optional[ChatSession]:
        """Retrieve session by ID."""
        data = await self.redis.get(f"session:{session_id}")
        if data:
            return ChatSession(**json.loads(data))
        return None

    async def add_message(self, session_id: str, message: dict):
        """Add message to session history."""
        session = await self.get(session_id)
        if session:
            session.messages.append(message)
            # Keep last 50 messages
            session.messages = session.messages[-50:]
            await self._save(session)

    async def set_handoff(
        self,
        session_id: str,
        thread_ts: str,
        channel: str
    ):
        """Mark session as handed off to human."""
        session = await self.get(session_id)
        if session:
            session.handoff_active = True
            session.slack_thread_ts = thread_ts
            session.slack_channel = channel
            await self._save(session)

    async def end_handoff(self, session_id: str):
        """Return session to bot control."""
        session = await self.get(session_id)
        if session:
            session.handoff_active = False
            session.assigned_agent = None
            await self._save(session)

    async def _save(self, session: ChatSession):
        """Persist session to Redis."""
        await self.redis.set(
            f"session:{session.session_id}",
            json.dumps(asdict(session)),
            ex=self.SESSION_TTL
        )
```

### 4. Slack Integration Service

**Location:** `src/votebot/services/slack.py`

```python
# src/votebot/services/slack.py
import structlog
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from typing import Optional, Callable
from dataclasses import dataclass

logger = structlog.get_logger()


@dataclass
class SlackConfig:
    """Slack configuration."""
    bot_token: str          # xoxb-...
    app_token: str          # xapp-...
    support_channel: str    # #votebot-support


class SlackService:
    """
    Service for Slack integration.

    Handles:
    - Creating support threads for handoffs
    - Relaying messages between users and agents
    - Agent assignment and notifications
    """

    def __init__(self, config: SlackConfig):
        self.config = config
        self.app = AsyncApp(token=config.bot_token)
        self.handler = AsyncSocketModeHandler(self.app, config.app_token)

        # Callback for relaying agent messages to users
        self._on_agent_message: Optional[Callable] = None

        # Register event handlers
        self._register_handlers()

    def _register_handlers(self):
        """Register Slack event handlers."""

        @self.app.event("message")
        async def handle_message(event, say):
            """Handle messages in support threads."""
            # Ignore bot messages and non-thread messages
            if event.get("bot_id") or not event.get("thread_ts"):
                return

            thread_ts = event["thread_ts"]
            user_id = event["user"]
            text = event["text"]

            # Look up session by thread
            session = await self._get_session_by_thread(thread_ts)
            if not session:
                return

            # Get agent info
            agent_info = await self._get_user_info(user_id)

            logger.info(
                "Agent message received",
                thread_ts=thread_ts,
                agent=agent_info["name"],
                session_id=session.session_id
            )

            # Relay to user via callback
            if self._on_agent_message:
                await self._on_agent_message(
                    session_id=session.session_id,
                    message={
                        "text": text,
                        "agent_name": agent_info["name"],
                        "agent_avatar": agent_info.get("avatar"),
                        "timestamp": event["ts"]
                    }
                )

        @self.app.event("reaction_added")
        async def handle_reaction(event):
            """Handle reactions for agent actions."""
            # ✅ = Resolved, return to bot
            # 👋 = Agent claiming conversation
            emoji = event["reaction"]
            thread_ts = event.get("item", {}).get("ts")

            if emoji == "white_check_mark" and thread_ts:
                await self._handle_resolution(thread_ts)
            elif emoji == "wave" and thread_ts:
                await self._handle_agent_claim(thread_ts, event["user"])

    async def create_handoff_thread(
        self,
        session_id: str,
        user_message: str,
        page_context: dict,
        conversation_history: list[dict]
    ) -> dict:
        """
        Create a new support thread for human handoff.

        Returns:
            dict with 'channel' and 'thread_ts'
        """
        # Build context blocks
        blocks = self._build_handoff_blocks(
            session_id=session_id,
            user_message=user_message,
            page_context=page_context,
            conversation_history=conversation_history
        )

        # Post to support channel
        result = await self.app.client.chat_postMessage(
            channel=self.config.support_channel,
            text=f"🆘 Human assistance requested - {page_context.get('title', 'General')}",
            blocks=blocks,
            unfurl_links=False
        )

        thread_ts = result["ts"]
        channel = result["channel"]

        logger.info(
            "Handoff thread created",
            session_id=session_id,
            thread_ts=thread_ts,
            channel=channel
        )

        return {"channel": channel, "thread_ts": thread_ts}

    def _build_handoff_blocks(
        self,
        session_id: str,
        user_message: str,
        page_context: dict,
        conversation_history: list[dict]
    ) -> list:
        """Build Slack blocks for handoff message."""
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🆘 Human Assistance Requested",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Session:*\n`{session_id[:12]}...`"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Page Type:*\n{page_context.get('type', 'general').title()}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Page:*\n<{page_context.get('url', '#')}|{page_context.get('title', 'Unknown')}>"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Jurisdiction:*\n{page_context.get('jurisdiction', 'N/A')}"
                    }
                ]
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Latest Message:*"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f">{user_message}"
                }
            }
        ]

        # Add conversation history summary
        if conversation_history:
            history_text = "*Recent Conversation:*\n"
            for msg in conversation_history[-5:]:
                role = "👤 Visitor" if msg["role"] == "user" else "🤖 Bot"
                content = msg["content"][:100] + "..." if len(msg["content"]) > 100 else msg["content"]
                history_text += f"{role}: {content}\n"

            blocks.append({"type": "divider"})
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": history_text
                }
            })

        # Add action hints
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "💡 *Reply in thread* to respond to visitor | React with ✅ to resolve | React with 👋 to claim"
                }
            ]
        })

        return blocks

    async def relay_to_agent(
        self,
        thread_ts: str,
        message: str,
        session_id: str
    ):
        """Relay user message to Slack thread."""
        await self.app.client.chat_postMessage(
            channel=self.config.support_channel,
            thread_ts=thread_ts,
            text=f"*Visitor:* {message}",
            unfurl_links=False
        )

    async def send_system_message(
        self,
        thread_ts: str,
        message: str
    ):
        """Send system message to thread (e.g., 'Visitor disconnected')."""
        await self.app.client.chat_postMessage(
            channel=self.config.support_channel,
            thread_ts=thread_ts,
            text=f"_📢 {message}_",
            unfurl_links=False
        )

    async def _get_user_info(self, user_id: str) -> dict:
        """Get Slack user info for agent display."""
        result = await self.app.client.users_info(user=user_id)
        user = result["user"]
        return {
            "id": user_id,
            "name": user.get("real_name") or user.get("name"),
            "avatar": user.get("profile", {}).get("image_72")
        }

    def on_agent_message(self, callback: Callable):
        """Register callback for agent messages."""
        self._on_agent_message = callback

    async def start(self):
        """Start the Socket Mode handler."""
        await self.handler.start_async()

    async def stop(self):
        """Stop the Socket Mode handler."""
        await self.handler.close_async()
```

---

## API Specifications

### WebSocket Protocol

**Endpoint:** `wss://api.digitaldemocracy.org/ws/chat`

**Query Parameters:**
- `session_id` (optional): Resume existing session

#### Client → Server Messages

```typescript
// User sends a message
{
  "type": "user_message",
  "payload": {
    "message": "What does this bill do?",
    "page_context": {
      "type": "bill",
      "id": "HB-1234",
      "jurisdiction": "FL",
      "title": "Education Funding Act",
      "url": "https://digitaldemocracy.org/bills/hb-1234"
    }
  }
}

// User starts typing (optional, for agent visibility)
{
  "type": "typing_start"
}

// User stops typing
{
  "type": "typing_stop"
}

// User explicitly ends session
{
  "type": "end_session"
}
```

#### Server → Client Messages

```typescript
// Bot starts generating response
{
  "type": "stream_start"
}

// Streaming text chunk
{
  "type": "stream_chunk",
  "payload": {
    "text": "This bill "
  }
}

// Streaming complete
{
  "type": "stream_end",
  "payload": {
    "citations": [...],
    "confidence": 0.85,
    "requires_human": false,
    "web_search_used": false
  }
}

// Human agent joined
{
  "type": "agent_joined",
  "payload": {
    "agent_name": "Sarah",
    "agent_avatar": "https://..."
  }
}

// Message from human agent
{
  "type": "agent_message",
  "payload": {
    "text": "Hi! I'm Sarah, how can I help?",
    "agent_name": "Sarah",
    "agent_avatar": "https://...",
    "timestamp": 1706640000
  }
}

// Agent left / handoff ended
{
  "type": "agent_left",
  "payload": {
    "reason": "resolved"  // or "timeout", "transferred"
  }
}

// Session restored after reconnection
{
  "type": "session_restored",
  "payload": {
    "messages": [...],
    "handoff_active": true,
    "agent_name": "Sarah"
  }
}

// Error
{
  "type": "error",
  "payload": {
    "code": "rate_limited",
    "message": "Please wait before sending another message"
  }
}
```

### REST Endpoints (Supporting)

```
# Health check for WebSocket service
GET /ws/health

# Get session history (for admin/debugging)
GET /api/v1/sessions/{session_id}
Authorization: Bearer <admin-token>

# Force end handoff (admin action)
POST /api/v1/sessions/{session_id}/end-handoff
Authorization: Bearer <admin-token>
```

---

## Data Models

### Database Schema (PostgreSQL)

```sql
-- Chat sessions (long-term storage)
CREATE TABLE chat_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id VARCHAR(64) UNIQUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    ended_at TIMESTAMP WITH TIME ZONE,
    page_context JSONB NOT NULL,
    handoff_occurred BOOLEAN DEFAULT FALSE,
    slack_thread_ts VARCHAR(32),
    resolution_type VARCHAR(20), -- 'bot_resolved', 'agent_resolved', 'abandoned'

    -- Metrics
    message_count INTEGER DEFAULT 0,
    bot_message_count INTEGER DEFAULT 0,
    agent_message_count INTEGER DEFAULT 0,
    first_response_ms INTEGER,

    INDEX idx_sessions_created (created_at),
    INDEX idx_sessions_handoff (handoff_occurred)
);

-- Chat messages (for analytics and history)
CREATE TABLE chat_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id VARCHAR(64) NOT NULL REFERENCES chat_sessions(session_id),
    role VARCHAR(20) NOT NULL, -- 'user', 'bot', 'agent', 'system'
    content TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- Metadata
    agent_slack_id VARCHAR(32),
    agent_name VARCHAR(100),
    confidence FLOAT,
    citations JSONB,

    INDEX idx_messages_session (session_id),
    INDEX idx_messages_created (created_at)
);

-- Agent performance metrics
CREATE TABLE agent_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_slack_id VARCHAR(32) NOT NULL,
    agent_name VARCHAR(100) NOT NULL,
    date DATE NOT NULL,

    -- Daily metrics
    conversations_handled INTEGER DEFAULT 0,
    messages_sent INTEGER DEFAULT 0,
    avg_response_time_ms INTEGER,
    resolutions INTEGER DEFAULT 0,

    UNIQUE (agent_slack_id, date),
    INDEX idx_agent_metrics_date (date)
);
```

### Redis Data Structures

**Implemented (February 2026) — `src/votebot/services/redis_store.py`:**

```
# Thread-to-session mapping (Redis hash — for cross-worker Slack event routing)
HSET votebot:threads {slack_thread_ts} {session_id}
# No TTL — cleaned up on handoff resolution

# Agent event pub/sub (for cross-worker WebSocket delivery)
PUBLISH votebot:agent_events {"event_type": "agent_message", "session_id": "abc123", "payload": {"text": "...", "agent_name": "..."}}
PUBLISH votebot:agent_events {"event_type": "agent_left", "session_id": "abc123", "payload": {"thread_ts": "..."}}
```

**Not yet implemented (from original design):**

```
# Active session (JSON) — currently in-memory per-worker
session:{session_id} -> {
    "session_id": "abc123",
    "created_at": 1706640000,
    "page_context": {...},
    "messages": [...],
    "handoff_active": true,
    "slack_thread_ts": "1706640000.123456",
    "slack_channel": "C123456",
    "assigned_agent": "U789"
}
TTL: 24 hours

# Rate limiting
ratelimit:{session_id} -> message_count
TTL: 60 seconds

# Active connections (for horizontal scaling)
connections:{instance_id} -> Set of session_ids
TTL: 5 minutes (refreshed on heartbeat)
```

---

## Sequence Diagrams

### Normal Bot Conversation (Streaming)

```
User        Widget        WebSocket       VoteBot        Pinecone      OpenAI
 │            │           Gateway          Agent
 │            │              │               │              │            │
 │──message──►│              │               │              │            │
 │            │──user_msg───►│               │              │            │
 │            │              │──process──────►│              │            │
 │            │              │               │──query───────►│            │
 │            │              │               │◄──results─────│            │
 │            │              │               │──stream───────────────────►│
 │            │◄─stream_start│               │              │            │
 │◄──typing───│              │               │              │            │
 │            │              │               │◄─────────chunk 1──────────│
 │            │◄─stream_chunk│◄──chunk 1─────│              │            │
 │◄──"This "──│              │               │              │            │
 │            │              │               │◄─────────chunk 2──────────│
 │            │◄─stream_chunk│◄──chunk 2─────│              │            │
 │◄──"bill "──│              │               │              │            │
 │            │              │               │◄─────────[done]───────────│
 │            │◄──stream_end─│◄──complete────│              │            │
 │◄──full msg─│              │               │              │            │
 │            │              │               │              │            │
```

### Human Handoff Flow

```
User        Widget        WebSocket       VoteBot        Slack         Agent
 │            │           Gateway          Agent         Service
 │            │              │               │              │            │
 │──"talk to  │              │               │              │            │
 │  human"───►│              │               │              │            │
 │            │──user_msg───►│               │              │            │
 │            │              │──process──────►│              │            │
 │            │              │               │──(requires   │            │
 │            │              │               │   human)─────►│            │
 │            │              │               │              │──create────►│
 │            │              │               │              │   thread    │
 │            │              │               │              │◄──thread_ts─│
 │            │◄─agent_joined│◄──handoff─────│◄─────────────│            │
 │◄─"Connecting│             │  complete     │              │            │
 │  to agent"─│              │               │              │            │
 │            │              │               │              │            │
 │            │              │               │              │◄──reply────│
 │            │              │               │              │  (in thread)│
 │            │◄─agent_msg───│◄──────────────│◄──event──────│            │
 │◄─"Hi, I'm  │              │               │              │            │
 │   Sarah"───│              │               │              │            │
 │            │              │               │              │            │
 │──"Can you  │              │               │              │            │
 │  explain?"─►│              │               │              │            │
 │            │──user_msg───►│               │              │            │
 │            │              │──relay────────────────────────►│            │
 │            │              │               │              │──post──────►│
 │            │              │               │              │  to thread  │
 │            │              │               │              │            │
```

### Agent Resolution (Return to Bot)

```
Agent       Slack         WebSocket       Widget        User
  │         Service        Gateway
  │            │              │              │            │
  │──react ✅──►│              │              │            │
  │            │──end_handoff─►│              │            │
  │            │              │──agent_left──►│            │
  │            │              │              │──"Agent    │
  │            │              │              │  resolved"─►│
  │            │              │              │            │
  │            │              │ (session now │            │
  │            │              │  bot-handled)│            │
  │            │              │              │            │
```

---

## Slack Integration

### Slack App Configuration

**Required Scopes (Bot Token):**
- `channels:history` - Read messages in public channels
- `channels:read` - View basic channel info
- `chat:write` - Send messages
- `reactions:read` - View reactions
- `users:read` - View user info for agent details

**Required Scopes (App Token):**
- `connections:write` - Socket Mode connection

**Event Subscriptions (Socket Mode):**
- `message.channels` - Messages in public channels
- `reaction_added` - Reactions added to messages

### Support Channel Setup

1. Create `#votebot-support` channel
2. Invite VoteBot app to channel
3. Pin instructions message:

```
📋 VoteBot Support Channel

When a visitor requests human assistance, a new thread will appear here.

Actions:
• 👋 React to claim the conversation
• Reply in thread to respond to visitor
• ✅ React to resolve and return to bot

Tips:
• Check the conversation history in the initial message
• Visitor messages appear with "Visitor:" prefix
• Your replies are sent to the visitor in real-time
```

### Thread Message Format

**Initial Handoff Message:**
```
🆘 Human Assistance Requested
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Session: abc12345...
Page Type: Bill
Page: Education Funding Act (HB 1234)
Jurisdiction: Florida

━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Latest Message:
> I want to talk to someone about this bill

━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Recent Conversation:
👤 Visitor: What does this bill do?
🤖 Bot: This bill addresses education funding...
👤 Visitor: I want to talk to someone about this bill

━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 Reply in thread to respond | ✅ to resolve | 👋 to claim
```

---

## Security Considerations

### Authentication & Authorization

| Layer | Mechanism |
|-------|-----------|
| WebSocket Connection | Session token in query string or first message |
| Slack Integration | OAuth tokens, Socket Mode (no public endpoint) |
| Admin APIs | Bearer token authentication |
| Inter-service | Internal network, mTLS for production |

### Rate Limiting

```python
# Per-session rate limits
RATE_LIMITS = {
    "messages_per_minute": 10,
    "messages_per_hour": 100,
    "max_message_length": 4000,
    "max_sessions_per_ip": 5,
}

async def check_rate_limit(session_id: str, ip: str) -> bool:
    """Check if request is within rate limits."""
    key = f"ratelimit:{session_id}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 60)
    return count <= RATE_LIMITS["messages_per_minute"]
```

### Data Privacy

- **Session IDs**: Generated randomly, not linkable to user identity
- **Message Storage**: Configurable retention (default 90 days)
- **PII Handling**: No PII collected by default; if user provides, stored encrypted
- **Slack Data**: Only support channel accessible; no access to other channels

### Input Validation

```python
def validate_message(message: str) -> str:
    """Validate and sanitize user message."""
    if not message or not message.strip():
        raise ValueError("Message cannot be empty")

    if len(message) > 4000:
        raise ValueError("Message too long")

    # Remove potential injection attempts
    sanitized = bleach.clean(message, tags=[], strip=True)

    return sanitized.strip()
```

---

## Error Handling

### Client-Side Reconnection

```typescript
// useWebSocket.ts
const useWebSocket = (url: string) => {
  const [status, setStatus] = useState<'connecting' | 'connected' | 'disconnected'>('connecting');
  const reconnectAttempts = useRef(0);
  const maxReconnectAttempts = 5;
  const reconnectDelay = [1000, 2000, 4000, 8000, 16000]; // Exponential backoff

  const connect = useCallback(() => {
    const ws = new WebSocket(url);

    ws.onopen = () => {
      setStatus('connected');
      reconnectAttempts.current = 0;
    };

    ws.onclose = () => {
      setStatus('disconnected');
      if (reconnectAttempts.current < maxReconnectAttempts) {
        const delay = reconnectDelay[reconnectAttempts.current];
        setTimeout(() => {
          reconnectAttempts.current++;
          connect();
        }, delay);
      }
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
    };

    return ws;
  }, [url]);

  // ...
};
```

### Server-Side Error Handling

```python
async def handle_message_safe(session_id: str, data: dict, manager: ConnectionManager):
    """Wrap message handling with error handling."""
    try:
        await handle_message(session_id, data, manager)
    except RateLimitExceeded:
        await manager.send_error(session_id, "rate_limited", "Please wait before sending another message")
    except ValidationError as e:
        await manager.send_error(session_id, "validation_error", str(e))
    except LLMServiceError as e:
        logger.error("LLM service error", error=str(e), session_id=session_id)
        await manager.send_error(session_id, "service_error", "I'm having trouble processing that. Please try again.")
    except Exception as e:
        logger.exception("Unexpected error", session_id=session_id)
        await manager.send_error(session_id, "internal_error", "Something went wrong. Please try again.")
```

### Slack Integration Resilience

```python
class SlackService:
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(SlackApiError)
    )
    async def relay_to_agent(self, thread_ts: str, message: str, session_id: str):
        """Relay with retry logic."""
        await self.app.client.chat_postMessage(
            channel=self.config.support_channel,
            thread_ts=thread_ts,
            text=f"*Visitor:* {message}"
        )
```

---

## Infrastructure Requirements

### Compute Resources

| Component | Resources | Scaling |
|-----------|-----------|---------|
| WebSocket Gateway | 2 vCPU, 4GB RAM | Horizontal (sticky sessions via Redis) |
| Slack Integration | 1 vCPU, 2GB RAM | Single instance (Socket Mode) |
| Redis | 2GB RAM | Managed service (ElastiCache) |
| PostgreSQL | 2 vCPU, 4GB RAM | Managed service (RDS) |

### Deployment Architecture

```
                                   ┌─────────────────┐
                                   │   CloudFront    │
                                   │   (CDN for      │
                                   │    widget JS)   │
                                   └────────┬────────┘
                                            │
┌─────────────────┐                         │
│   Route 53      │                         │
│   DNS           │                         │
└────────┬────────┘                         │
         │                                  │
         ▼                                  ▼
┌─────────────────┐              ┌─────────────────┐
│   ALB           │              │   S3 Bucket     │
│   (WebSocket    │              │   (Widget       │
│    support)     │              │    assets)      │
└────────┬────────┘              └─────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│              ECS Cluster                         │
│  ┌─────────────┐  ┌─────────────┐               │
│  │  WebSocket  │  │  WebSocket  │               │
│  │  Gateway 1  │  │  Gateway 2  │   (...)       │
│  └──────┬──────┘  └──────┬──────┘               │
│         │                │                       │
│         └───────┬────────┘                       │
│                 │                                │
│  ┌──────────────▼──────────────┐                │
│  │      Slack Integration      │                │
│  │      (Single Instance)      │                │
│  └──────────────┬──────────────┘                │
└─────────────────┼───────────────────────────────┘
                  │
         ┌────────┴────────┐
         ▼                 ▼
┌─────────────┐    ┌─────────────┐
│   Redis     │    │  PostgreSQL │
│ (ElastiCache)│    │   (RDS)     │
└─────────────┘    └─────────────┘
```

### Environment Variables

```bash
# WebSocket Gateway
WEBSOCKET_HOST=0.0.0.0
WEBSOCKET_PORT=8001
REDIS_URL=redis://votebot-redis:6379/0
DATABASE_URL=postgresql://...

# Slack Integration
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SUPPORT_CHANNEL=#votebot-support

# Existing VoteBot config
OPENAI_API_KEY=...
PINECONE_API_KEY=...
# ... (other existing env vars)
```

---

## Migration Plan

### Phase 1: Build Foundation (Week 1-2)

- [ ] Set up chat widget repository
- [ ] Implement basic WebSocket gateway
- [ ] Create session management with Redis
- [ ] Build minimal chat widget (no streaming yet)

### Phase 2: Streaming Integration (Week 2-3)

- [ ] Connect widget to VoteBot streaming endpoint
- [ ] Implement stream rendering in widget
- [ ] Add typing indicators
- [ ] Test end-to-end streaming flow

### Phase 3: Slack Integration (Week 3-4)

- [ ] Create Slack app and configure permissions
- [ ] Implement SlackService with Socket Mode
- [ ] Build handoff flow
- [ ] Implement agent → user message relay
- [ ] Add resolution workflow

### Phase 4: Polish & Testing (Week 4-5)

- [ ] Session persistence and reconnection
- [ ] Error handling and edge cases
- [ ] Load testing
- [ ] Security review
- [ ] Documentation

### Phase 5: Deployment & Rollout (Week 5-6)

- [ ] Deploy to staging environment
- [ ] Internal testing with team
- [ ] Beta rollout to subset of users
- [ ] Monitor metrics and fix issues
- [ ] Full production rollout

### Rollback Plan

If issues arise:
1. Widget can be hidden via feature flag
2. Existing Brevo integration remains functional
3. Database migrations are additive (no breaking changes)

---

## Testing Strategy

### Unit Tests

```python
# tests/unit/test_websocket_handlers.py
@pytest.mark.asyncio
async def test_handle_user_message_bot_mode():
    """Test message handling when in bot mode."""
    manager = MockConnectionManager()
    session_store = MockSessionStore()

    # Session without handoff
    await session_store.create("test-session", {"type": "general"})

    await handle_user_message(
        session_id="test-session",
        payload={"message": "Hello", "page_context": {"type": "general"}},
        manager=manager
    )

    # Verify bot response was streamed
    assert manager.sent_messages[0]["type"] == "stream_start"
    assert any(m["type"] == "stream_chunk" for m in manager.sent_messages)
    assert manager.sent_messages[-1]["type"] == "stream_end"


@pytest.mark.asyncio
async def test_handle_user_message_agent_mode():
    """Test message handling when in agent mode."""
    manager = MockConnectionManager()
    session_store = MockSessionStore()
    slack_service = MockSlackService()

    # Session with active handoff
    session = await session_store.create("test-session", {"type": "general"})
    await session_store.set_handoff("test-session", "1234.5678", "C123")

    await handle_user_message(
        session_id="test-session",
        payload={"message": "Hello agent"},
        manager=manager
    )

    # Verify message was relayed to Slack, not processed by bot
    assert slack_service.relay_called
    assert slack_service.last_relay["message"] == "Hello agent"
```

### Integration Tests

```python
# tests/integration/test_websocket_flow.py
@pytest.mark.asyncio
async def test_full_conversation_flow():
    """Test complete conversation from connection to handoff."""
    async with websocket_client("ws://localhost:8001/ws/chat") as ws:
        # Send message
        await ws.send_json({
            "type": "user_message",
            "payload": {
                "message": "What is DDP?",
                "page_context": {"type": "general"}
            }
        })

        # Collect streaming response
        messages = []
        while True:
            msg = await ws.receive_json()
            messages.append(msg)
            if msg["type"] == "stream_end":
                break

        # Verify streaming worked
        assert messages[0]["type"] == "stream_start"
        chunks = [m for m in messages if m["type"] == "stream_chunk"]
        assert len(chunks) > 0

        # Request human
        await ws.send_json({
            "type": "user_message",
            "payload": {
                "message": "I want to talk to a human",
                "page_context": {"type": "general"}
            }
        })

        # Should trigger handoff
        messages = []
        while True:
            msg = await ws.receive_json()
            messages.append(msg)
            if msg["type"] == "agent_joined":
                break

        assert any(m["type"] == "agent_joined" for m in messages)
```

### Load Testing

```python
# tests/load/locustfile_websocket.py
from locust import User, task, between
import websocket
import json

class WebSocketUser(User):
    wait_time = between(1, 3)

    def on_start(self):
        self.ws = websocket.create_connection(
            "wss://api.digitaldemocracy.org/ws/chat"
        )

    def on_stop(self):
        self.ws.close()

    @task
    def send_message(self):
        self.ws.send(json.dumps({
            "type": "user_message",
            "payload": {
                "message": "What is the Digital Democracy Project?",
                "page_context": {"type": "general"}
            }
        }))

        # Consume response
        while True:
            msg = json.loads(self.ws.recv())
            if msg["type"] == "stream_end":
                break
```

---

## Open Questions

### Product Questions

| Question | Options | Recommendation |
|----------|---------|----------------|
| Agent availability hours? | 24/7 vs business hours | Start with business hours, show "Leave message" after hours |
| Max wait time for agent? | 2min, 5min, 10min | 5 minutes, then offer to leave message |
| Can users opt out of bot? | Direct to human option | No - bot should handle 80%+ of queries |
| Conversation history visible to user? | Full history vs current session | Current session only |

### Technical Questions

| Question | Options | Recommendation |
|----------|---------|----------------|
| Widget hosting | CDN vs same domain | CDN (CloudFront) for caching |
| Session ID generation | UUID vs JWT | UUID (simpler, no auth needed) |
| Message persistence | All vs handoff-only | All (valuable for analytics) |
| Multi-instance coordination | Redis pub/sub vs dedicated broker | Redis pub/sub (already using Redis) — **IMPLEMENTED** (Feb 2026): `redis_store.py` provides thread-to-session hash + pub/sub for `agent_events` channel |

### Answered During Implementation

- [ ] Exact Slack channel structure (single channel vs per-jurisdiction)
- [ ] Agent assignment logic (round-robin vs skills-based)
- [ ] Widget z-index and positioning on DDP site
- [ ] Mobile widget behavior (bottom sheet vs full screen)

---

## Appendix: Widget Embedding Example

```html
<!DOCTYPE html>
<html>
<head>
    <title>Digital Democracy Project</title>
</head>
<body>
    <!-- Page content -->

    <!-- Chat Widget -->
    <script>
        window.DDPChatConfig = {
            endpoint: 'wss://api.digitaldemocracy.org/ws/chat',
            position: 'bottom-right',
            primaryColor: '#1a5f7a',
            greeting: 'Hi! I can help you learn about legislation and civic engagement.',
            pageContext: {
                type: 'bill',
                id: 'FL-HB-1234',
                jurisdiction: 'FL',
                title: 'Education Funding Act',
                url: window.location.href
            }
        };
    </script>
    <script
        src="https://cdn.digitaldemocracy.org/chat-widget/v1/widget.js"
        async
    ></script>
</body>
</html>
```

---

## Revision History

| Date | Version | Author | Changes |
|------|---------|--------|---------|
| 2026-01-30 | 1.0 | Claude | Initial draft |

