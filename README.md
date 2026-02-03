# VoteBot 2.0

High-performance, context-aware chat API for the Digital Democracy Project.

## Overview

VoteBot 2.0 is a RAG-powered chatbot API that provides intelligent, context-aware responses about legislation, legislators, and civic engagement. It's designed to be UI-agnostic and can be integrated with various chat interfaces.

## Features

- **Context-Aware Responses**: Understands the page context (bill, legislator, general) to provide relevant answers
- **RAG-Powered**: Uses Pinecone vector database for semantic search and retrieval
- **Bill Text Prioritization**: For bill queries, prioritizes actual legislative text over CMS summaries
- **Web Search Fallback**: Automatically searches the web (via Tavily API) when RAG confidence is low
- **Human Handoff**: Supports seamless handoff to human agents when needed
- **Multi-Source Data**: Ingests data from Congress.gov, OpenStates, and custom sources
- **Real-Time Updates**: Hourly polling for content changes
- **High Performance**: Designed for 1000+ concurrent conversations

## Tech Stack

- **Framework**: FastAPI (Python 3.11+)
- **Vector Database**: Pinecone
- **LLM**: OpenAI GPT-4.1 (via Responses API with web search)
- **Embeddings**: OpenAI text-embedding-3-large
- **Caching**: Redis
- **Database**: PostgreSQL (optional)

## Quick Start

### Prerequisites

- Python 3.11+
- Docker and Docker Compose (optional)
- API keys for OpenAI, Pinecone, Congress.gov, and OpenStates

### Installation

1. Clone the repository:
```bash
cd votebot
```

2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -e ".[dev]"
```

4. Set up environment variables:
```bash
cp .env.example .env
# Edit .env with your API keys
```

5. Run the development server:
```bash
python -m votebot.main
```

Or using Docker:
```bash
docker-compose -f infrastructure/docker/docker-compose.yml up
```

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `OPENAI_API_KEY` | OpenAI API key | Yes |
| `PINECONE_API_KEY` | Pinecone API key | Yes |
| `PINECONE_ENVIRONMENT` | Pinecone environment (default: us-east-1) | Yes |
| `PINECONE_INDEX_NAME` | Pinecone index name (default: votebot-large) | Yes |
| `PINECONE_NAMESPACE` | Pinecone namespace (default: default) | No |
| `API_KEY` | API key for authentication | Yes |
| `WEBFLOW_API_KEY` | Webflow CMS API key | For sync |
| `WEBFLOW_BILLS_COLLECTION_ID` | Webflow bills collection | For sync |
| `WEBFLOW_LEGISLATORS_COLLECTION_ID` | Webflow legislators collection | For sync |
| `WEBFLOW_ORGANIZATIONS_COLLECTION_ID` | Webflow organizations collection | For sync |
| `CONGRESS_API_KEY` | Congress.gov API key | For federal bills |
| `OPENSTATES_API_KEY` | OpenStates API key | For state bills |
| `TAVILY_API_KEY` | Tavily API key for web search fallback | For web search |
| `REDIS_URL` | Redis connection URL | Optional |
| `SLACK_BOT_TOKEN` | Slack Bot Token (xoxb-...) | For handoff |
| `SLACK_APP_TOKEN` | Slack App Token (xapp-...) | For handoff |
| `SLACK_SUPPORT_CHANNEL` | Slack channel for support | For handoff |
| `SIMILARITY_THRESHOLD` | RAG similarity threshold (default: 0.1) | No |

## API Endpoints

### Chat

```
POST /votebot/v1/chat
```

Process a chat message and return a response.

**Request Body:**
```json
{
  "message": "What does this bill do?",
  "session_id": "abc123",
  "human_active": false,
  "page_context": {
    "type": "bill",
    "id": "HR-1234",
    "jurisdiction": "US"
  }
}
```

**Response:**
```json
{
  "response": "This bill establishes...",
  "citations": [
    {
      "source": "Congress.gov",
      "document_id": "bill-HR-1234",
      "excerpt": "..."
    }
  ],
  "confidence": 0.85,
  "requires_human": false
}
```

### Health Checks

```
GET /votebot/v1/health       # Basic health check
GET /votebot/v1/health/ready # Readiness check (verifies dependencies)
GET /votebot/v1/health/live  # Liveness check
```

### WebSocket

```
WS /ws/chat?session_id={session_id}
```

Real-time streaming chat with human handoff support.

### Content Resolution

```
GET /votebot/v1/content/resolve?url={ddp_url}
```

Resolve a DDP URL to content metadata for the chat widget.

**Example:**
```bash
curl "https://api.digitaldemocracyproject.org/votebot/v1/content/resolve?url=https://digitaldemocracyproject.org/bills/one-big-beautiful-bill-act-hr1-2025"
```

**Response:**
```json
{
  "type": "bill",
  "id": "HR 1",
  "title": "One Big Beautiful Bill Act (HR1)",
  "jurisdiction": "US",
  "description": "The One Big Beautiful Bill Act aims to reform...",
  "status": "",
  "url": "https://digitaldemocracyproject.org/bills/one-big-beautiful-bill-act-hr1-2025",
  "slug": "one-big-beautiful-bill-act-hr1-2025"
}
```

### Unified Sync API

```
POST /votebot/v1/sync/unified
```

Sync content to the vector store. Supports bills, legislators, organizations, webpages, and training documents.

**Request Body (single item):**
```json
{
  "content_type": "bill",
  "mode": "single",
  "slug": "fl-hb-123-2025",
  "include_pdfs": true,
  "include_openstates": true
}
```

**Request Body (batch):**
```json
{
  "content_type": "bill",
  "mode": "batch",
  "jurisdiction": "FL",
  "limit": 100,
  "include_pdfs": true
}
```

**Response:**
```json
{
  "success": true,
  "content_type": "bill",
  "mode": "batch",
  "status": "accepted",
  "task_id": "abc-123-def",
  "items_processed": 0,
  "chunks_created": 0
}
```

For batch operations, the sync runs in the background. Check status with:

```
GET /votebot/v1/sync/unified/status/{task_id}
```

**Sync all content types:**
```
POST /votebot/v1/sync/unified/all
```

## Chat Widget

An embeddable JavaScript widget is available in the `chat-widget/` directory. See [chat-widget/README.md](chat-widget/README.md) for embedding instructions.

### Hosted Version

VoteBot is hosted at **https://votebot.digitaldemocracyproject.org/**

You can pass a DDP URL to provide page context:
```
https://votebot.digitaldemocracyproject.org/?ddp_url=https://digitaldemocracyproject.org/bills/one-big-beautiful-bill-act-hr1-2025
```

### Embedding on Your Site

```html
<script>
    window.DDPChatConfig = {
        wsUrl: 'wss://api.digitaldemocracyproject.org/ws/chat',
        pageContext: { type: 'bill', id: 'HR 1', title: 'My Bill' }
    };
</script>
<script src="https://api.digitaldemocracyproject.org/widget/ddp-chat.min.js" async></script>
```

## Slack Human Handoff

VoteBot supports seamless handoff to human agents via Slack when users request human assistance.

### Setup

1. **Create a Slack App** in your workspace at https://api.slack.com/apps

2. **Enable Socket Mode** in the app settings

3. **Add Bot Token Scopes:**
   - `channels:history` - Read public channel messages
   - `channels:read` - List public channels
   - `chat:write` - Send messages
   - `groups:read` - List private channels
   - `groups:history` - Read private channel messages
   - `reactions:read` - Read message reactions
   - `users:read` - Get user info

4. **Add App Token Scope:**
   - `connections:write` - Connect via Socket Mode

5. **Subscribe to Bot Events:**
   - `message.channels` - Messages in public channels
   - `message.groups` - Messages in private channels
   - `reaction_added` - Emoji reactions

6. **Install the app** to your workspace

7. **Create a support channel** (e.g., `#votebot-support`) and invite the bot

8. **Add environment variables:**
   ```bash
   SLACK_BOT_TOKEN=xoxb-your-bot-token
   SLACK_APP_TOKEN=xapp-your-app-token
   SLACK_SUPPORT_CHANNEL=#votebot-support
   ```

### How It Works

1. User sends message like "I want to talk to a human"
2. VoteBot detects handoff request (`requires_human: true`)
3. A thread is created in the support channel with conversation context
4. Human agents reply in the Slack thread
5. Agent messages are relayed to the user in real-time
6. Agent reacts with ✅ to resolve and return to VoteBot

### Thread Format

```
🆘 Human Assistance Requested
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Session: abc12345
Page: Bill - Education Funding Act (FL-HB-1234)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Latest Message:
> I want to talk to someone about this bill

Recent Conversation:
👤 User: What does this bill do?
🤖 Bot: This bill addresses education funding...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💡 Reply in thread to respond | ✅ to resolve
```

## Data Ingestion

VoteBot uses a unified sync service for all content types. The primary data sources are:
- **Webflow CMS** - Bills, legislators, and organizations managed in Webflow
- **OpenStates** - Legislative history, votes, actions, and sponsored bills
- **Congress.gov** - Federal bill text and amendments
- **PDFs** - Bill text from legislative websites and Google Drive

### Unified Sync CLI

```bash
# Single item sync
python scripts/sync.py bill --slug fl-hb-123-2025
python scripts/sync.py bill --webflow-id 6512abc123
python scripts/sync.py legislator --slug rick-scott
python scripts/sync.py organization --slug aclu

# Batch sync (all items of a type)
python scripts/sync.py bill --batch
python scripts/sync.py bill --batch --jurisdiction FL --limit 100
python scripts/sync.py legislator --batch --no-sponsored-bills
python scripts/sync.py organization --batch

# Sync all content types
python scripts/sync.py all
python scripts/sync.py all --dry-run --limit 50

# Full refresh (clear and resync)
python scripts/sync.py all --clear-namespace

# Clear namespace only (DESTRUCTIVE)
python scripts/sync.py clear --confirm
```

### Sync Options

| Option | Description |
|--------|-------------|
| `--batch` | Sync all items (vs single item) |
| `--dry-run` | Preview without ingesting |
| `--limit N` | Maximum items to process |
| `--jurisdiction` | Filter by jurisdiction (e.g., FL, US) |
| `--no-pdfs` | Skip PDF processing for bills |
| `--no-openstates` | Skip OpenStates data for bills |
| `--no-sponsored-bills` | Skip sponsored bills for legislators |
| `--clear-namespace` | Delete all data before syncing |
| `--log-level` | DEBUG, INFO, WARNING, ERROR |

### Legacy Scripts

```bash
# Seed development data
python scripts/seed_data.py
```

## Development

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=votebot --cov-report=html

# Run specific test file
pytest tests/unit/test_agent.py
```

### Code Quality

```bash
# Lint code
ruff check src/

# Format code
ruff format src/

# Type check
mypy src/votebot
```

## Architecture

```
votebot/
├── src/votebot/
│   ├── main.py              # FastAPI application
│   ├── config.py            # Configuration
│   ├── api/                  # API layer
│   │   ├── routes/          # Endpoint handlers
│   │   │   ├── chat.py      # POST /chat endpoints
│   │   │   ├── websocket.py # WebSocket /ws/chat
│   │   │   ├── health.py    # Health checks
│   │   │   ├── content.py   # Content resolution
│   │   │   └── sync_unified.py  # Unified sync API
│   │   ├── schemas/         # Request/response models
│   │   └── middleware/      # Auth, logging
│   ├── core/                # Business logic
│   │   ├── agent.py         # Conversational agent
│   │   ├── retrieval.py     # RAG retrieval
│   │   └── prompts.py       # System prompts
│   ├── services/            # External integrations
│   │   ├── llm.py           # OpenAI client
│   │   ├── embeddings.py    # Embedding generation
│   │   ├── vector_store.py  # Pinecone operations
│   │   ├── web_search.py    # Tavily web search
│   │   └── slack.py         # Slack human handoff
│   ├── sync/                # Unified sync service
│   │   ├── service.py       # UnifiedSyncService
│   │   ├── types.py         # ContentType, SyncMode, etc.
│   │   └── handlers/        # Content-specific handlers
│   │       ├── bill.py      # Bill sync (Webflow + OpenStates + PDFs)
│   │       ├── legislator.py    # Legislator sync
│   │       ├── organization.py  # Organization sync
│   │       ├── webpage.py   # Webpage sync
│   │       └── training.py  # Training document sync
│   ├── ingestion/           # Data ingestion pipeline
│   │   ├── pipeline.py      # Main orchestrator
│   │   ├── sources/         # Data source connectors
│   │   │   ├── congress.py  # Congress.gov API
│   │   │   ├── openstates.py    # OpenStates API
│   │   │   ├── webflow.py   # Webflow CMS
│   │   │   └── pdf.py       # PDF extraction
│   │   └── chunking.py      # Text chunking
│   └── updates/             # Real-time updates
│       ├── scheduler.py     # Scheduled polling
│       └── change_detection.py
├── scripts/
│   ├── sync.py              # Unified sync CLI
│   └── seed_data.py         # Development data seeding
├── tests/
│   ├── unit/
│   ├── integration/
│   └── load/
└── chat-widget/             # Embeddable chat widget
    ├── src/                 # Widget source files
    ├── dist/                # Built widget (ddp-chat.min.js)
    └── test.html            # Local testing page
```

## Performance Targets

| Metric | Target |
|--------|--------|
| P50 Latency | < 2.5 seconds |
| P95 Latency | < 5 seconds |
| First Token (streaming) | < 1.5 seconds |
| Availability | 99.9% uptime |
| Concurrency | 1,000+ simultaneous |

## Contributing

1. Create a feature branch
2. Make your changes
3. Run tests and linting
4. Submit a pull request

## License

MIT License - see LICENSE file for details.
