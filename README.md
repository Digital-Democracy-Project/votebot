# VoteBot 2.0

High-performance, context-aware chat API for the Digital Democracy Project.

## Overview

VoteBot 2.0 is a RAG-powered chatbot API that provides intelligent, context-aware responses about legislation, legislators, and civic engagement. It's designed to be UI-agnostic and can be integrated with various chat interfaces.

## Features

- **Context-Aware Responses**: Understands the page context (bill, legislator, organization, general) to provide relevant answers
- **RAG-Powered**: Uses Pinecone vector database for semantic search and retrieval
- **Multi-Phase Retrieval**: For bill queries, prioritizes legislative text over CMS summaries, with dedicated phases for organization positions and vote records
- **Organization-Aware Retrieval**: Scoped retrieval on org pages via `webflow_id`/`slug` filters (mirrors bill/legislator pattern), plus query-based detection for org queries on non-org pages. Fetches all related chunks for complete bill position data
- **Legislator Slug Resolution**: Automatically resolves legislator slugs from Webflow pages to OpenStates person IDs via Webflow CMS lookup, enabling correct Pinecone filtering even when only the URL slug is available
- **Webflow CMS Runtime Lookup**: Bidirectional Webflow CMS pre-fetch — fetches authoritative org positions for bill→org queries (99.1%) and bill positions for org→bill queries (100%), bypassing Pinecone similarity thresholds
- **Webflow CMS Verification on Disputes**: When users challenge information, fetches authoritative details from Webflow CMS for the current page entity (bill facts, legislator party/chamber/district, org type/website) and injects as high-priority context
- **Bill Info Tool**: Real-time OpenStates lookups for full bill details (status, sponsors, votes) on bills not in the RAG system
  - Automatic jurisdiction detection from message text ("Virginia HB 2724" → VA)
  - Session year fallback (tries current year, then previous 2 years)
  - Party affiliation enrichment for vote records
- **Web Search Fallback**: Automatically searches the web (via OpenAI web search + Tavily) when RAG confidence is low
- **Production Query Monitoring**: All production queries and responses are logged to date-partitioned JSONL files for offline evaluation against ground truth
- **Human Handoff**: Supports seamless handoff to human agents when needed via Slack
- **Multi-Source Data**: Ingests data from Congress.gov, OpenStates, Webflow CMS, and custom sources
- **Automated Sync Scheduling**: Daily bill version checks (detects newer amended PDFs/HTML, re-ingests text and updates Webflow CMS gov-url), weekly legislator sync, monthly org sync — with Redis leader election for multi-worker safety
- **High Performance**: Designed for 1000+ concurrent conversations

## Tech Stack

- **Framework**: FastAPI (Python 3.11+)
- **Vector Database**: Pinecone
- **LLM**: OpenAI GPT-4.1 (via Responses API with web search)
- **Embeddings**: OpenAI text-embedding-3-large
- **Caching / Cross-Worker State**: Redis (thread-to-session mapping, pub/sub for multi-worker handoff, active jurisdictions tracking, bill version cache)
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
| `WEBFLOW_VOTEBOT_API_KEY` | Webflow CMS API key (read-only, used at query time) | For sync |
| `WEBFLOW_SCHEDULER_API_KEY` | Webflow CMS API key with CMS:write scope (used by scheduler for gov-url updates) | For scheduler |
| `WEBFLOW_BILLS_COLLECTION_ID` | Webflow bills collection | For sync |
| `WEBFLOW_LEGISLATORS_COLLECTION_ID` | Webflow legislators collection | For sync |
| `WEBFLOW_ORGANIZATIONS_COLLECTION_ID` | Webflow organizations collection | For sync |
| `CONGRESS_API_KEY` | Congress.gov API key | For federal bills |
| `OPENSTATES_API_KEY` | OpenStates API key | For state bills |
| `TAVILY_API_KEY` | Tavily API key for web search fallback | For web search |
| `REDIS_URL` | Redis connection URL (cross-worker handoff state) | For multi-worker |
| `SLACK_BOT_TOKEN` | Slack Bot Token (xoxb-...) | For handoff |
| `SLACK_APP_TOKEN` | Slack App Token (xapp-...) | For handoff |
| `SLACK_SUPPORT_CHANNEL` | Slack channel for support | For handoff |
| `QUERY_LOG_ENABLED` | Enable production query logging (default: true) | No |
| `QUERY_LOG_DIR` | Directory for JSONL query logs (default: logs/queries) | No |
| `SCHEDULER_ENABLED` | Enable automated sync scheduler (default: false) | For production |
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

> **Proxy note**: In production, requests go through the DDP-API proxy on port 5000, which strips `/v1` from the path. External callers (e.g., Postman) use `POST https://api.digitaldemocracyproject.org/votebot/sync/unified`. The DDP-API re-authenticates to VoteBot internally. See [DDP-API](https://github.com/VotingRightsBrigade/DDP-API) for the full endpoint map.

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

> **Note**: Batch bill sync with `include_openstates` (default: true) automatically chains `BillVersionSyncService` after the OpenStates history sync. This checks for newer bill text versions (PDF/HTML), re-ingests updated text into Pinecone, and updates Webflow CMS fields (`gov-url`, `status`, `status-date`). The same version sync also runs independently on the daily scheduler (04:00 UTC).

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

Via the DDP-API proxy:
```
GET https://api.digitaldemocracyproject.org/votebot/sync/unified/status/{task_id}
```

**Sync all content types:**
```
POST /votebot/v1/sync/unified/all
```

## Chat Widget

An embeddable JavaScript widget is available in the `chat-widget/` directory. See [chat-widget/README.md](chat-widget/README.md) for embedding instructions.

### Features

- **Context-Aware**: Automatically detects page context from DDP URLs or manual configuration
- **Cross-Page Session Persistence**: Chat session, conversation history, and popup state persist across full-page navigations via `sessionStorage` (scoped to browser tab, 30-minute timeout)
- **Smart Context Handling**: When the user navigates to a different entity, a fresh session starts with a new welcome message to avoid confusing the LLM with stale context
- **Streaming Responses**: Real-time token streaming with partial auto-scroll
- **Partial Auto-Scroll**: Force-scrolls to show typing indicator and start of response, then stops — user scrolls down at their own pace; "scroll to bottom" button appears when content is below
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
4. Thread-to-session mapping is stored in both local memory and Redis (`votebot:threads` hash)
5. Human agents reply in the Slack thread
6. Agent messages are published via Redis pub/sub (`votebot:agent_events` channel) for cross-worker delivery
7. The worker that owns the user's WebSocket receives the event and delivers it
8. Agent reacts with ✅ to resolve and return to VoteBot

**Multi-worker support**: VoteBot runs with 2 uvicorn workers. Redis ensures that Slack events received by one worker are delivered to the user's WebSocket on another worker. See [Troubleshooting](docs/TROUBLESHOOTING.md#human-handoff-messages-dropped-in-multi-worker-deployment) for details.

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
| `WEBFLOW_ORG_LOOKUP_ENABLED` | Enable runtime Webflow CMS lookup for org positions | `true` |

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

5. **Vote Verification**: When users challenge or dispute vote information, VoteBot automatically fetches directly from OpenStates API to verify. This works from **any page type** (bill, legislator, organization, or no page context). Triggered by phrases like "are you sure", "double check", "that's wrong", or "verify". The verification:
   - Works from any page type — extracts bill identifier from message text or conversation history when not on a bill page
   - Gets session via: `/content/resolve` → extracts `session-code` from Webflow → widget passes to WebSocket
   - Falls back to calculating Congress number from year if session not provided
   - Searches for legislator by **last name** (e.g., "moody" matches "Moody (R-FL)")
   - Prioritizes **final passage votes** over procedural votes (motion to commit, cloture, etc.)
   - Returns authoritative data that overrides RAG results

6. **Webflow CMS Verification on Disputes**: When users challenge information, VoteBot also fetches authoritative details from Webflow CMS for the current page entity:
   - **Bill pages**: name, identifier, status, description, jurisdiction
   - **Legislator pages**: name, party, chamber, district, DDP score
   - **Organization pages**: name, type, website, description
   - Injected as highest-priority context before all other sources

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

| Relationship | Direction | Content | Runtime Lookup |
|--------------|-----------|---------|----------------|
| Bill ↔ Organization | Bill → Org | "Organizations Supporting/Opposing This Bill" | Webflow CMS (99.1%) |
| Bill ↔ Organization | Org → Bill | "Bills Supported/Opposed" with DDP links | Webflow CMS (100%) |
| Bill → Legislator | Vote Records | `[ocd-person/uuid]Name (Party-State)` format | OpenStates API |
| Legislator → Bill | Voting Record | `legislator-votes-{person_uuid}` documents | — |
| Legislator Page → ID | Slug → OpenStates ID | Resolves slug to `legislator_id` for Pinecone filtering | Webflow CMS |
| Dispute Verification | Any → CMS | Bill/legislator/org details on disputes | Webflow CMS |
| Vote Verification | Any → OpenStates | Legislator vote lookup (any page type) | OpenStates API |

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

### Sync Scheduling

VoteBot uses an automated sync scheduler to keep content fresh. Enable it by setting `SCHEDULER_ENABLED=true` in the environment. In multi-worker deployments, only one worker runs the scheduler (Redis-based leader election).

**Schedule:**

| Content | Frequency | Time (UTC) | Details |
|---------|-----------|------------|---------|
| **Bill versions** | Daily | 04:00 | Checks OpenStates for newer amended versions (Engrossed, Enrolled, etc.), re-ingests bill text (PDF or HTML) into Pinecone, updates Webflow CMS `gov-url` |
| **Legislators** | Weekly | 06:00 Sunday | Sponsored bills + voting records, date-based rotation (200/run) |
| **Organizations** | Monthly | 08:00 1st | Full re-ingest of org profiles from Webflow CMS |
| **Content poll** | Hourly | — | Congress/OpenStates change detection |

**Bill version checking** (`BillVersionSyncService` in `src/votebot/updates/bill_version_sync.py`):

For each current-session bill, the daily job:
1. Fetches the bill from OpenStates API (includes `versions` array with dated entries and PDF/HTML links)
2. Compares the latest version against a Redis cache per bill (`votebot:bill_version:{webflow_id}`, 90-day TTL)
3. If a newer version is detected (new date, different note like "Engrossed", or changed URL):
   - Downloads bill text (PDF via `WebflowSource._process_bill_pdf()` or HTML via `_process_bill_html()`, based on OpenStates `media_type`)
   - Re-ingests into Pinecone as `bill-text` documents (idempotent upsert overwrites old chunks)
   - Updates Webflow CMS `gov-url` field via `WebflowLookupService.update_bill_gov_url()` (PATCH API)
4. If unchanged: updates `last_checked` timestamp only

Configuration in `config/sync_schedule.yaml`:
- `bill_version_check.max_updates_per_run`: Limits re-ingestions per run (default: 50). First run populates the Redis cache for all bills, cycling through remaining bills on subsequent runs.
- `bill_version_check.skip_webflow_update`: Disables CMS writes for testing (default: false)

> **Prerequisite**: Set `WEBFLOW_SCHEDULER_API_KEY` to a Webflow API token with `CMS:write` scope. This key is used only by the scheduler for `gov-url` updates. The main `WEBFLOW_VOTEBOT_API_KEY` remains read-only for query-time CMS lookups. If `WEBFLOW_SCHEDULER_API_KEY` is not set, the scheduler falls back to `WEBFLOW_VOTEBOT_API_KEY` (which will fail on writes unless it also has write scope).

**Session detection** uses a two-tier approach:

1. **Live data (preferred)**: Before each bill version check run, jurisdiction info is fetched from the OpenStates API (via `OpenStatesSource.fetch_jurisdiction()`) and warms `StateLegislativeCalendar` with real session start/end dates. Each state is fetched at most once per sync run.

2. **Hardcoded fallback**: If the OpenStates API is unavailable or a state has no live data, the calendar falls back to hardcoded heuristics (start patterns + duration in weeks for all 50 states + DC).

**Auto-tracking jurisdictions**: When a bill from a new state is synced, the system automatically detects the jurisdiction via `resolve_jurisdiction_code()` — which checks the `JURISDICTION_MAP` first, then falls back to parsing the bill's OpenStates URL (e.g., `https://openstates.org/ca/bills/...` → `CA`). Discovered jurisdictions are registered in Redis (`votebot:active_jurisdictions` set) for cross-worker visibility. No manual config changes are needed to add new states — just add bills from the new state to Webflow CMS and sync.

**Multi-worker safety**: The scheduler uses a Redis-based leader lock (`votebot:scheduler:leader`, 5-min TTL, refreshed every 2 min). Only the leader worker runs scheduled jobs. If the leader dies, another worker can acquire the lock. If Redis is unavailable, the scheduler starts anyway (single-worker fallback).

Configuration is in `config/sync_schedule.yaml`. The `StateLegislativeCalendar` class is at `src/votebot/utils/legislative_calendar.py`.

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
│   │   ├── webflow_lookup.py # Runtime Webflow CMS lookup (bill→org + org→bill + verification + gov-url write)
│   │   ├── redis_store.py   # Redis client for cross-worker state (thread mapping + pub/sub + active jurisdictions + bill version cache)
│   │   ├── query_logger.py  # Production query logger (JSONL, date-partitioned)
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
│   ├── utils/               # Utility modules
│   │   └── legislative_calendar.py  # Session date lookup (live OpenStates + hardcoded fallback)
│   └── updates/             # Real-time updates
│       ├── scheduler.py     # Scheduled polling
│       ├── bill_sync.py     # OpenStates bill sync (status, votes, actions)
│       ├── bill_version_sync.py  # Daily bill version check (PDF/HTML re-ingestion + Webflow gov-url update)
│       └── change_detection.py
├── scripts/
│   ├── sync.py              # Unified sync CLI
│   ├── seed_data.py         # Development data seeding
│   ├── test_bill_votes_tool.py  # Bill votes tool tests
│   ├── rag_test_common.py       # Shared test infra (TestResult, VoteBotTestClient, reporting)
│   ├── rag_ground_truth.py      # Ground truth fetcher (Webflow CMS + OpenStates)
│   ├── test_rag_comprehensive.py # Orchestrated RAG test suite (delegates to modules)
│   ├── test_rag_quality.py      # Quality tests (static YAML + dynamic ground truth)
│   ├── test_rag_bills.py        # Bill-focused RAG tests
│   ├── test_rag_legislators.py  # Legislator-focused RAG tests (with page_context)
│   ├── test_rag_organizations.py # Organization-focused RAG tests
│   └── evaluate_production.py    # Offline evaluation of production query logs
├── tests/
│   ├── unit/
│   ├── integration/
│   └── load/
└── chat-widget/             # Embeddable chat widget
    ├── src/                 # Widget source files
    ├── dist/                # Built widget (ddp-chat.min.js)
    └── test.html            # Local testing page
```

## RAG Test Suite

VoteBot includes an orchestrated RAG test suite that validates response quality across all content types.

### Architecture

```
test_rag_comprehensive.py  (orchestrator — CLI, ground truth, delegates, unified report)
  ├── rag_test_common.py       (shared TestResult, TestReport, VoteBotTestClient, validation)
  ├── test_rag_bills.py        (bill-focused tests, single + multi-turn)
  ├── test_rag_legislators.py  (legislator tests with page_context)
  ├── test_rag_organizations.py (organization-focused tests)
  ├── rag_ground_truth.py      (ground truth fetcher from Webflow CMS + OpenStates)
  └── test_rag_quality.py      (static YAML tests + dynamic ground truth validation)
```

Each focused script is independently runnable via `__main__` and exports a unified `run_tests()` interface.

### Running the Full Suite

```bash
# Run all categories (bills, legislators, organizations, DDP, out-of-system votes)
PYTHONPATH=src python scripts/test_rag_comprehensive.py

# Run specific categories
PYTHONPATH=src python scripts/test_rag_comprehensive.py --category bills --category legislators

# Multi-turn conversations
PYTHONPATH=src python scripts/test_rag_comprehensive.py --mode both --limit 5

# With ground truth enrichment from OpenStates
PYTHONPATH=src python scripts/test_rag_comprehensive.py --with-openstates --limit 10

# Save JSON report
PYTHONPATH=src python scripts/test_rag_comprehensive.py --output test_report.json

# Dry run to see test plan
PYTHONPATH=src python scripts/test_rag_comprehensive.py --dry-run
```

### Running Individual Modules

```bash
# Bill tests (standalone)
PYTHONPATH=src python scripts/test_rag_bills.py --limit 5 --mode single

# Legislator tests (preserves page_context)
PYTHONPATH=src python scripts/test_rag_legislators.py --sample-size 5 --mode both

# Organization tests
PYTHONPATH=src python scripts/test_rag_organizations.py --limit 5

# Quality tests (static YAML + dynamic ground truth)
PYTHONPATH=src python scripts/test_rag_quality.py --dynamic --limit 10
```

### CLI Options (Orchestrator)

| Option | Description |
|--------|-------------|
| `--category CAT` | Category to test (repeatable): bills, legislators, organizations, ddp, out_of_system_votes |
| `--mode MODE` | single, multi, or both (default: single) |
| `--limit N` | Max entities per category (default: 10) |
| `--jurisdiction CODE` | Filter by state code (e.g., FL, VA) |
| `--with-openstates` | Enrich ground truth with OpenStates data |
| `--api-url URL` | VoteBot API URL (default: http://localhost:8000) |
| `--output FILE` | JSON report output path |
| `--verbose` | Per-test detailed output |
| `--dry-run` | Show test plan without executing |

### Test Categories

| Category | Ground Truth | Description |
|----------|-------------|-------------|
| `bills` | Webflow CMS | Bill queries with optional ground truth validation |
| `legislators` | Webflow CMS | Legislator queries with page_context (critical for scoped retrieval) |
| `organizations` | Webflow CMS | Organization profile and position queries |
| `ddp` | None | DDP general knowledge (confidence/citation metrics only) |
| `out_of_system_votes` | None | Bills NOT in CMS (tests dynamic OpenStates lookup) |

### Benchmark Results (100-Document Sample, February 2026)

| Category | Passed/Total | Rate |
|----------|-------------|------|
| Bills | 310/312 | **99.4%** |
| Legislators | 290/300 | 97% |
| Organizations | 290/292 | **99.3%** |
| **Overall** | **890/904** | **98.5%** |

**Webflow CMS Runtime Lookup** (`WebflowLookupService`) fetches authoritative position data directly from CMS in both directions, bypassing Pinecone similarity thresholds:
- **Bill→Org positions**: 111/112 (99.1%) — up from 82.1% after Phase 4a-i, and 58.9% before
- **Org→Bill positions**: 99/99 (100%) — up from ~96% with Pinecone-only retrieval

Top jurisdictions (bills): MI 100%, WA 100%, VA 100%, FL 100%, US 100%, MA 100%, AZ 98%, UT 96%. All jurisdictions now ≥96%.

See [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#failure-analysis-100-document-sample) for detailed failure analysis.

## Production Query Monitoring

VoteBot logs all production queries and LLM responses to date-partitioned JSONL files for offline quality evaluation and performance monitoring.

### How It Works

1. **Automatic capture**: Every call to `process_message()` and `process_message_stream()` is logged via fire-and-forget (`asyncio.create_task`) — zero impact on response latency
2. **Date-partitioned JSONL**: Logs are written to `logs/queries/YYYY-MM-DD.jsonl`, one JSON object per line
3. **Multi-worker safe**: Uses `aiofiles` with append mode for atomic writes across uvicorn workers

### Log Entry Fields

| Field | Description |
|-------|-------------|
| `timestamp` | ISO 8601 UTC timestamp |
| `session_id` | Chat session identifier |
| `client_ip` | Client IP address (from X-Forwarded-For or direct connection) |
| `user_agent` | Client User-Agent header |
| `message` | User's query text |
| `response` | LLM response text |
| `confidence` | Response confidence score (0-1) |
| `citations` | List of citation objects |
| `page_context` | Page context (type, id, title, jurisdiction, webflow_id, slug) |
| `channel` | Source: "rest" or "websocket" |
| `duration_ms` | End-to-end response time in milliseconds |
| `human_active` | Whether a human agent was active |

### Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `QUERY_LOG_ENABLED` | Enable/disable query logging | `true` |
| `QUERY_LOG_DIR` | Directory for JSONL files | `logs/queries` |

### Offline Evaluation

The evaluation script reads production logs and validates responses against Webflow CMS ground truth:

```bash
# Evaluate today's queries
PYTHONPATH=src python scripts/evaluate_production.py

# Evaluate a specific date
PYTHONPATH=src python scripts/evaluate_production.py --date 2026-02-08

# Evaluate last 7 days, filtered by jurisdiction
PYTHONPATH=src python scripts/evaluate_production.py --days 7 --jurisdiction FL --verbose

# Custom log directory
PYTHONPATH=src python scripts/evaluate_production.py --log-dir /var/log/votebot/queries
```

The evaluation report includes:
- **Pass rate per entity type** (bill, organization, legislator) validated against ground truth
- **Confidence distribution** with low-confidence query flagging (< 0.5)
- **Citation rate** and average citation count
- **Latency metrics** (average and P95)
- **Breakdown by jurisdiction, entity type, and query category**

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

- Human handoff messages dropped in multi-worker deployment (Redis cross-worker state)
- Wrong legislator returned on Webflow pages (slug→ID resolution)
- Legislator vote lookups not working
- Corrupted legislator-votes documents (chunk boundary parsing issues)
- Organization retrieval issues (bill→org, org→bill, org type detection)
- Bill identifier extraction (HJR, SJR, HCR, SCR patterns)
- Organization chunk data quality (aggressive chunking)
- Webflow CMS verification on disputes (bill, legislator, organization pages)
- Missing data in search results
- Federal legislator cache issues
- Pinecone index diagnostics
- RAG test suite diagnostics and benchmarks
- Full index rebuild procedures
- Chat widget truncated on mobile (send button cut off due to layout viewport expansion on content-rich host pages — fixed with `screen.width` mobile detection)
- Production query monitoring (JSONL logging, offline evaluation)

## Contributing

1. Create a feature branch
2. Make your changes
3. Run tests and linting
4. Submit a pull request

## License

MIT License - see LICENSE file for details.
