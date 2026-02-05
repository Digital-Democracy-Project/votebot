# VoteBot 2.0

High-performance, context-aware chat API for the Digital Democracy Project.

## Overview

VoteBot 2.0 is a RAG-powered chatbot API that provides intelligent, context-aware responses about legislation, legislators, and civic engagement. It's designed to be UI-agnostic and can be integrated with various chat interfaces.

## Features

- **Context-Aware Responses**: Understands the page context (bill, legislator, general) to provide relevant answers
- **RAG-Powered**: Uses Pinecone vector database for semantic search and retrieval
- **Bill Text Prioritization**: For bill queries, prioritizes actual legislative text over CMS summaries
- **Bill Info Tool**: Real-time OpenStates lookups for full bill details (status, sponsors, votes) on bills not in the RAG system
  - Automatic jurisdiction detection from message text ("Virginia HB 2724" → VA)
  - Session year fallback (tries current year, then previous 2 years)
  - Party affiliation enrichment for vote records
- **Web Search Fallback**: Automatically searches the web (via OpenAI web search + Tavily) when RAG confidence is low
- **Human Handoff**: Supports seamless handoff to human agents when needed via Slack
- **Multi-Source Data**: Ingests data from Congress.gov, OpenStates, Webflow CMS, and custom sources
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
    "jurisdiction": "US",
    "session-code": "119"
  }
}
```

> **Note**: The `session-code` field should contain the OpenStates-friendly session identifier from Webflow (e.g., "119" for 119th Congress, "2025" for state legislative sessions). This is used for vote verification lookups.

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
  "requires_human": false,
  "web_search_used": false,
  "bill_votes_tool_used": false
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

### Features

- **Context-Aware**: Automatically detects page context from DDP URLs or manual configuration
- **Streaming Responses**: Real-time token streaming with smooth auto-scroll
- **Smart Auto-Scroll**: Auto-scrolling pauses when user scrolls up to read; "scroll to bottom" button appears to resume
- **Auto-Open Modes**:
  - **Explicit mode** (`?ddp_url=...`): Widget auto-opens when URL parameter is provided
  - **Discovery mode**: Widget stays closed, auto-detects page context when opened
- **Bill Info Pre-fetching**: Fetches bill details from OpenStates before streaming for bills not in RAG

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
        pageContext: {
            type: 'bill',
            id: 'HR 1',
            title: 'My Bill',
            jurisdiction: 'US',
            'session-code': '119'  // OpenStates-friendly session from Webflow
        },
        autoOpen: false,  // Set to true to auto-open on page load
        autoDetect: true  // Auto-detect DDP page context
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

## Bill Info Tool

VoteBot includes a real-time bill information lookup tool that enables fetching full bill details (status, sponsors, actions, votes) for bills not in the RAG system. This uses OpenAI's function calling with the Responses API, and also works with streaming responses through pre-fetching.

### How It Works

1. **Hybrid Lookup Strategy**: Bills in the system are retrieved via RAG; bills NOT in the system are fetched live from OpenStates
2. **Automatic Bill Detection**: When a user mentions a bill identifier (e.g., "Virginia HB 2724"), the tool is automatically triggered
3. **Jurisdiction Extraction**: State names in the message are automatically mapped to state codes (e.g., "Virginia" → "VA")
4. **Session Year Fallback**: If a bill isn't found in the current year, the tool automatically tries previous years (2026 → 2025 → 2024)
5. **Party Affiliation Lookup**: Vote records are enriched with legislator party information from the OpenStates people endpoint
6. **Streaming Support**: For streaming responses (chat widget), bill info is pre-fetched before the stream starts

### Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `BILL_VOTES_TOOL_ENABLED` | Enable the bill info tool | `true` |
| `BILL_VOTES_RAG_CONFIDENCE_THRESHOLD` | RAG confidence below which tool is enabled | `0.4` |

### Function Schema

```json
{
  "name": "get_bill_info",
  "description": "Get full bill information including status, sponsors, actions, and votes from OpenStates",
  "parameters": {
    "jurisdiction": "Two-letter state code (e.g., 'va', 'fl') or 'us' for federal",
    "session": "Legislative session (e.g., '2025', '2024')",
    "bill_identifier": "Bill identifier (e.g., 'HB 2724', 'SB 648')"
  }
}
```

### Example Usage

**Example 1: Bill not in Pinecone**

When a user asks "Tell me about Virginia HB 2724", VoteBot:
1. Extracts jurisdiction "VA" from "Virginia" in the message
2. Detects bill identifier "HB 2724"
3. Fetches from OpenStates: tries 2026, then 2025 (where the bill is found)
4. Returns full bill info including title, sponsors, status, actions, and votes

**Example 2: Vote breakdown by party**

When a user asks "How did Democrats vote on this bill?", VoteBot:
1. Uses the bill info already fetched (or fetches it)
2. Returns party breakdown: "Democratic: 35 Yes, 2 No" with individual legislator names
3. Party info is looked up from OpenStates people endpoint and cached per jurisdiction

### Data Returned

The bill info tool returns:
- **Bill metadata**: Title, description, session, jurisdiction
- **Sponsors**: Primary sponsor and co-sponsors
- **Status**: Latest action description
- **Actions**: Recent legislative actions with dates
- **Votes**: All recorded votes with:
  - Vote totals (Yes/No/Other)
  - Party breakdown (Democratic/Republican counts)
  - Individual legislator names grouped by party and vote

## Legislator Voting Records

VoteBot maintains a reverse index of legislator voting records, enabling queries like "How did Ashley Moody vote on HR 1?" to find answers directly in the legislator's voting record document.

### Architecture

1. **Bill-Votes Documents**: When bills are synced with `include_openstates=True`, vote records are extracted and stored with inline OpenStates person IDs in the format `[ocd-person/uuid]Name (Party-State)`

2. **Federal Legislator Cache**: A local cache of US Congress members' OpenStates person IDs, used to match federal legislators (who don't have person IDs in vote records) to their stable IDs

3. **Legislator-Votes Documents**: A reverse index built from bill-votes documents, creating per-legislator voting record documents keyed by OpenStates person ID

4. **Name Enrichment**: During legislator-votes document creation, last-name-only entries (e.g., "Moody") are enriched with full names (e.g., "Ashley Moody") from the federal legislator cache. This improves search ranking for full-name queries.

5. **Vote Verification**: When users challenge or dispute vote information, VoteBot automatically fetches directly from OpenStates API to verify. This is triggered by phrases like "are you sure", "double check", "that's wrong", or "verify". The verification:
   - Gets session via: `/content/resolve` → extracts `session-code` from Webflow → widget passes to WebSocket
   - Falls back to calculating Congress number from year if session not provided
   - Searches for legislator by **last name** (e.g., "moody" matches "Moody (R-FL)")
   - Prioritizes **final passage votes** over procedural votes (motion to commit, cloture, etc.)
   - Returns authoritative data that overrides RAG results

### CLI Commands

```bash
# Refresh the federal legislator cache (538 members of Congress)
python -m votebot.sync.federal_legislator_cache

# Show cached legislators
python -m votebot.sync.federal_legislator_cache --show

# Build legislator-votes documents from bill-votes (reverse index)
python -m votebot.sync.build_legislator_votes

# Dry run to see stats without writing
python -m votebot.sync.build_legislator_votes --dry-run

# Clean up corrupted documents (from chunk boundary parsing issues)
python -m votebot.sync.build_legislator_votes --cleanup --dry-run  # Preview
python -m votebot.sync.build_legislator_votes --cleanup            # Delete corrupted docs
```

### Sync Workflow

For complete legislator voting records:

```bash
# 1. Refresh federal legislator cache (periodic - legislators don't change often)
python -m votebot.sync.federal_legislator_cache

# 2. Sync bills with OpenStates data (injects person IDs into vote content)
python -m votebot.updates.bill_sync batch --jurisdiction us --include-openstates
python -m votebot.updates.bill_sync batch --jurisdiction fl --include-openstates

# 3. Build reverse index for legislator-votes documents
python -m votebot.sync.build_legislator_votes
```

The build output includes a `name_enrichments` count showing how many legislators had their names enriched from the federal cache. For optimal search results, ensure the federal cache is refreshed before building.

### Document Types

| Document Type | Description | ID Format |
|--------------|-------------|-----------|
| `bill-votes` | Per-bill vote records with all legislators | `bill-votes-{webflow_id}` |
| `legislator-votes` | Per-legislator voting history | `legislator-votes-{person_uuid}` |

### OpenStates Person ID Coverage

- **State bills**: ~100% coverage (person IDs from OpenStates API)
- **Federal bills**: ~68% coverage (matched via federal legislator cache)

## Data Ingestion

VoteBot uses a unified sync service for all content types. The primary data sources are:
- **Webflow CMS** - Bills, legislators, and organizations managed in Webflow
- **OpenStates** - Legislative history, votes, actions, and sponsored bills
- **Congress.gov** - Federal bill text and amendments
- **PDFs** - Bill text from legislative websites and Google Drive
- **DDP Website** - Static pages (about, FAQ, etc.) scraped for RAG
- **Training Docs** - Local text files for agent behavior customization

### Data Linkages

VoteBot maintains bidirectional linkages between content types:

| Relationship | Direction | Content |
|--------------|-----------|---------|
| Bill ↔ Organization | Bill → Org | "Organizations Supporting/Opposing This Bill" |
| Bill ↔ Organization | Org → Bill | "Bills Supported/Opposed" with DDP links |
| Bill → Legislator | Vote Records | `[ocd-person/uuid]Name (Party-State)` format |
| Legislator → Bill | Voting Record | `legislator-votes-{person_uuid}` documents |

### Full Rebuild Script

For a complete rebuild of the Pinecone index with all content types:

```bash
# Full rebuild with prompts
python scripts/rebuild_pinecone.py

# Non-interactive (auto-confirm all prompts)
python scripts/rebuild_pinecone.py --yes

# Skip wipe (add to existing data)
python scripts/rebuild_pinecone.py --skip-wipe

# Specific content types only
python scripts/rebuild_pinecone.py --content-types bill,legislator
```

**Sync Order** (recommended for proper data linkages):
1. `bill` - Creates bill-votes with OpenStates person IDs and org positions
2. `legislator` - Creates legislator profiles with OpenStates IDs
3. `organization` - Creates org profiles with bill positions
4. `webpage` - DDP website pages (about, faq, vote, tally, score, etc.)
5. `training` - Training documents for agent behavior
6. `legislator-votes` - Reverse index built from bill-votes (post-sync)

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
│   │   ├── bill_votes.py    # Bill votes lookup (OpenStates)
│   │   └── slack.py         # Slack human handoff
│   ├── sync/                # Unified sync service
│   │   ├── service.py       # UnifiedSyncService
│   │   ├── types.py         # ContentType, SyncMode, etc.
│   │   ├── build_legislator_votes.py  # Reverse index builder
│   │   ├── federal_legislator_cache.py  # Federal legislator ID cache
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
│   ├── seed_data.py         # Development data seeding
│   ├── test_bill_votes_tool.py  # Bill votes tool tests
│   └── test_rag_comprehensive.py  # RAG evaluation tests
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

## Troubleshooting

For common issues and diagnostic procedures, see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md). This includes:

- Legislator vote lookups not working
- Corrupted legislator-votes documents (chunk boundary parsing issues)
- Missing data in search results
- Federal legislator cache issues
- Pinecone index diagnostics
- Full index rebuild procedures

## Contributing

1. Create a feature branch
2. Make your changes
3. Run tests and linting
4. Submit a pull request

## License

MIT License - see LICENSE file for details.
