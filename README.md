# VoteBot 2.0

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An open-source, high-performance, context-aware chat API for the Digital Democracy Project.

## Overview

VoteBot 2.0 is a RAG-powered chatbot API that provides intelligent, context-aware responses about legislation, legislators, and civic engagement. It's designed to be UI-agnostic and can be integrated with various chat interfaces.

## Features

- **Context-Aware Responses**: Understands the page context (bill, legislator, organization, general) to provide relevant answers
- **RAG-Powered**: Uses Pinecone vector database for semantic search and retrieval
- **Multi-Phase Retrieval**: For bill queries, prioritizes legislative text over CMS summaries, with dedicated phases for organization positions, vote records, and version changelogs. Phase 5 fires on "what changed / what was added / difference" queries and surfaces `bill-changelog` documents (LLM-generated diffs produced by DDP-Sync on each version transition)
- **Organization-Aware Retrieval**: Scoped retrieval on org pages via `webflow_id`/`slug` filters (mirrors bill/legislator pattern), plus query-based detection for org queries on non-org pages. Fetches all related chunks for complete bill position data
- **Legislator Slug Resolution**: Automatically resolves legislator slugs from Webflow pages to OpenStates person IDs via Webflow CMS lookup, enabling correct Pinecone filtering even when only the URL slug is available
- **Webflow CMS Runtime Lookup**: Bidirectional Webflow CMS pre-fetch — fetches authoritative org positions for bill→org queries (99.1%) and bill positions for org→bill queries (100%), bypassing Pinecone similarity thresholds
- **Legislators live from api-v3 (canonical-id index)**: legislators are not embedded, so a legislator page, or a message about a legislator, is answered from api-v3's `/people` (party, chamber, district, contact, links). A legislator page should pass the OpenStates person id as `id` (`ocd-person/...`); without one, its `title` (the name) and `jurisdiction` are used. On a bill page a name is looked up only when it is probably a person's (two capitalised words such as "Ashley Moody", or any name after a cue like "senator"; acronyms, state names, titles and question words are ignored, so "Summarize this bill" makes no call); on other pages the message must also say "senator", "representative", "legislator" and the like. All the live lookups for one message (legislators and the broker) share one 8-second budget. Several matches are listed so the bot asks which one (with the total when api-v3 has more than it returned); if api-v3 is slow or failing the model is told the records could not be retrieved and must not state a legislator's role or contact details from memory. Votes still come from the live bill lookup.
- **Positions from the broker (canonical-id index)**: organization positions on a bill, a bill-page organization's bills, and organization details on a dispute come from ddp-broker-py's public reads (`/api/bill-organization-positions/current/`, `/api/organizations/{id}/positions/`, `/api/organizations/{id}/`), not Webflow. Needs `DDP_BROKER_API_ROOT`; a broker problem costs the enrichment, never the answer
- **Webflow CMS Verification on Disputes**: When users challenge information, fetches authoritative details from Webflow CMS for the current page entity (bill facts, legislator party/chamber/district, org type/website) and injects as high-priority context
- **Bill Info Tool**: Real-time OpenStates lookups for full bill details (status, sponsors, votes) on bills not in the RAG system
  - Automatic jurisdiction detection from message text ("Virginia HB 2724" → VA)
  - Session year fallback (tries current year, then previous 2 years)
  - Party affiliation enrichment for vote records
- **Web Search Fallback**: Automatically searches the web (via OpenAI web search + Tavily) when RAG confidence is low
- **User Analytics & Behavioral Logging**: Event-based logging system with three event types (`message_received`, `query_processed`, `conversation_ended`), three-level identity model (visitor, session, conversation), two-level intent classification, grounding status tracking, fallback detection, and conversation boundary analysis. All events logged to date-partitioned JSONL files for offline evaluation and analytics.
- **Quick-Action Buttons**: Three preset buttons on bill pages — "Summary", "Pros & cons", "Status & votes" (visible labels; full descriptions on `aria-label` for screen readers). Buttons stay visible across the chat session and are disabled in-flight to prevent double-fires. Summary + pros/cons responses are cached in Redis (slug-keyed with amendment-triggered invalidation via pub/sub from DDP-Sync on the legacy index; on the canonical-id index keyed by `ocd_bill_id` and stamped with the current version, so a new version is a miss without any event; 7-day safety TTL). Status/votes button is never cached — always hits live OpenStates. Gated on `VOTEBOT_QUICK_ACTION_BUTTONS` (default `false`); enabled in production 2026-04-29. See [plans/PLAN-quick-action-buttons.md](plans/PLAN-quick-action-buttons.md).
- **Opinion Elicitation (exploratory)**: A guided opinion-capture feature — multi-position opinion vectors, Polis-based clustering, Memberstack accounts, Catalist voter verification — has been discussed as a future direction. Not implemented in VoteBot; design docs now live in the ddp-infra repo.
- **Human Handoff**: Supports seamless handoff to human agents when needed via Slack
- **Multi-Source Data**: RAG index is built from Congress.gov, OpenStates, Webflow CMS, and custom sources — ingestion itself is handled by [DDP-Sync](https://github.com/Digital-Democracy-Project/ddp-sync), a standalone service
- **Data Sync**: Content ingestion (bills, legislators, orgs), sync handlers, CLI tooling, and scheduled jobs all live in [DDP-Sync](https://github.com/Digital-Democracy-Project/ddp-sync) — VoteBot itself is chat/RAG-only and contains no ingestion code
- **High Performance**: Designed for 1000+ concurrent conversations

## Tech Stack

- **Framework**: FastAPI (Python 3.11+)
- **Vector Database**: Pinecone
- **LLM**: OpenAI GPT-4.1 (via Responses API with web search)
- **Embeddings**: OpenAI text-embedding-3-large
- **Caching / Cross-Worker State**: Redis (thread-to-session mapping, pub/sub for multi-worker handoff, active jurisdictions tracking, bill version cache, quick-action button cache)
- **Database**: PostgreSQL — config field (`DATABASE_URL`) is reserved but not currently used by any application code; no Postgres driver is in `pyproject.toml`

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
| `PINECONE_INDEX_NAME` | Pinecone index name (default: `votebot-large`). **Also decides how bills are filtered in retrieval:** `votebot-large` is keyed by `webflow_id`; exactly `ddp-knowledge-base` (the name in `CANONICAL_PINECONE_INDEX_NAME`, overridable with `CANONICAL_PINECONE_INDEX_NAME`) is keyed by `ocd_bill_id`. The value is stripped and lowercased, and empty means unset. Any other name keeps `webflow_id` retrieval and logs a startup warning. The startup log shows the index and the chosen key. Switching the index, or rolling back, is this one setting. | Yes |
| `PINECONE_NAMESPACE` | Pinecone namespace (default: default) | No |
| `API_KEY` | API key for authentication | Yes |
| `WEBFLOW_VOTEBOT_API_KEY` | Webflow CMS API key (read-only, used at query time by `/content/resolve` and runtime CMS lookups) | Yes |
| `WEBFLOW_SCHEDULER_API_KEY` | Webflow CMS API key with CMS:write scope (not currently read by any VoteBot code path — used by DDP-Sync for gov-url updates) | No |
| `WEBFLOW_BILLS_COLLECTION_ID` | Webflow bills collection (used by `/content/resolve` and runtime CMS lookups) | Yes |
| `WEBFLOW_LEGISLATORS_COLLECTION_ID` | Webflow legislators collection (used by `/content/resolve` and runtime CMS lookups) | Yes |
| `WEBFLOW_ORGANIZATIONS_COLLECTION_ID` | Webflow organizations collection (used by `/content/resolve` and runtime CMS lookups) | Yes |
| `VOTEBOT_DETERMINISTIC_CITATIONS` | When the model writes no `[Source: ...]`, cite the page's own retrieved chunks (those carrying the bill's / organization's id) that the answer shares distinctive words with (VOTEBOT-21; `core/citations.py`). A general page, a greeting, a refusal or an answer that says it does not know gets none. `false` returns only the citations the model wrote | No (default `true`) |
| `DDP_SITE_BASE_URL` | Base URL of our own site (ddp-next), e.g. `https://digitaldemocracyproject.org`. On the canonical-id index, bill sources and citations link to `{base}/explore/{JURISDICTION}/{SESSION}/{IDENTIFIER}` instead of the legislature's URL (that page links to the legislature). No default host: unset leaves links unchanged | No |
| `DDP_BROKER_API_ROOT` | Base URL of ddp-broker-py. `/content/resolve` uses its public bill endpoints to turn a ddp-next bill URL into an OpenStates bill id. No code default; unset means ddp-next URLs return 503 (Webflow URLs are unaffected) | For ddp-next bill URLs |
| `CONGRESS_API_KEY` | Congress.gov API key | For federal bills |
| `OPENSTATES_API_KEY` | OpenStates API key | For state bills |
| `USE_DDP_OPENSTATES_REPLICA` | Routes OpenStates calls to DDP-API's own proxy instead of the public API (default `false`) | No |
| `DDP_OPENSTATES_API_ROOT` | Base URL of DDP-API's OpenStates proxy — required if the replica flag above is on; no hardcoded fallback, fails loudly at request time if unset | Only with replica flag |
| `DDP_OPENSTATES_AUTH_HEADER` | How the credential below is sent when the replica flag is on: `bearer` (default, for ddp-api's proxy) or `x-api-key` (api-v3 itself accepts only `X-API-Key` and answers 403 to a Bearer token). Exactly one shape is ever sent | Only with replica flag |
| `DDP_OPENSTATES_BEARER_TOKEN` | Bearer token DDP-API's proxy expects (`Authorization: Bearer ...`) — a different auth shape than the public API's `x-api-key`, and the two must never both be sent on the same request | Only with replica flag |
| `TAVILY_API_KEY` | Tavily API key for web search fallback | For web search |
| `REDIS_URL` | Redis connection URL (cross-worker handoff state) | For multi-worker |
| `SLACK_BOT_TOKEN` | Slack Bot Token (xoxb-...) | For handoff |
| `SLACK_APP_TOKEN` | Slack App Token (xapp-...) | For handoff |
| `SLACK_SUPPORT_CHANNEL` | Slack channel for support | For handoff |
| `QUERY_LOG_ENABLED` | Enable production query logging (default: true) | No |
| `QUERY_LOG_DIR` | Directory for JSONL query logs (default: logs/queries) | No |
| `SCHEDULER_ENABLED` | *Deprecated* — sync scheduling has moved to [DDP-Sync](https://github.com/Digital-Democracy-Project/ddp-sync) | No |
| `SIMILARITY_THRESHOLD` | RAG similarity threshold (default: 0.1) | No |
| `VOTEBOT_QUICK_ACTION_BUTTONS` | Enable quick-action buttons on bill pages + Redis response cache (default: false) | No |

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
    "session": "119"
  }
}
```

> **Note**: `page_context.session` should contain the OpenStates-friendly session identifier (e.g., "119" for 119th Congress, "2025" for state legislative sessions). This is used for vote verification lookups. Webflow CMS calls the equivalent field `session-code`; the WebSocket protocol (used by the chat widget) accepts either `session` or `session-code` in the raw payload, but the REST `PageContext` schema only recognizes `session`.

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

Two kinds of bill URL are understood:

- **ddp-next** (`/explore/{jurisdiction}/{session}/{gov_id}` — the new site's bill page, gov_id URL-encoded, e.g. `HB%20219` — or `/bills/{broker_id}` or `/bills/{jurisdiction}/{session}/{gov_id}`): resolved through ddp-broker-py (`DDP_BROKER_API_ROOT`) to the bill's OpenStates id. ddp-broker-py has no bill-detail endpoint, so this uses its public `/api/bills/{id}/scorecard/` (broker id to jurisdiction, session and gov_id) and `/api/bills/resolve/` (those three to the bare OpenStates UUID). The result is the page context the widget sends back with each message; retrieval filters on its `ocd_bill_id` when the index is the canonical-id one (see `PINECONE_INDEX_NAME`). Errors: 404 unknown bill (the broker said so), 502 broker problem, a non-JSON or non-object broker body, or a broker answer without a valid bare UUID, 503 `DDP_BROKER_API_ROOT` unset. A bill that exists in OpenStates but has no row in the broker is a 404 here, since `/api/bills/resolve/` only knows the broker's bills.

ddp-next **legislator** (`/legislators/{numeric id}`) and **organization** URLs are not resolved yet and still take the Webflow path: the broker has no public endpoint that returns a legislator's OpenStates person id (its list and scorecard endpoints do not carry it), and ddp-next has no organization page. Revisit when the broker exposes the id. **Known open bug (VOTEBOT-31):** on the new deployment there is no Webflow collection (`WEBFLOW_*` unset by design), so such a URL ends in a 500 ("Webflow collection not configured for organization") instead of a clear 4xx; the widget builds an organization page context by hand (`{"type": "organization", "id": "<broker pk>"}`), which works.
- **Webflow** (`/bills/{slug}`): looked up in the Webflow CMS as before, returning `webflow_id`. Kept until Webflow is retired, and it is what a rollback to `votebot-large` relies on.

**Example (Webflow):**
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
  "session": "119",
  "description": "The One Big Beautiful Bill Act aims to reform...",
  "status": "",
  "url": "https://digitaldemocracyproject.org/bills/one-big-beautiful-bill-act-hr1-2025",
  "slug": "one-big-beautiful-bill-act-hr1-2025",
  "webflow_id": "6512abc123..."
}
```

**Example (ddp-next) and response:**
```bash
curl "https://api.digitaldemocracyproject.org/votebot/v1/content/resolve?url=https://digitaldemocracyproject.org/bills/fl/2026/HB%20123"
```
```json
{
  "type": "bill",
  "id": "HB 123",
  "ocd_bill_id": "a3f7c0d1-1111-4222-8333-444455556666",
  "gov_id": "HB 123",
  "jurisdiction": "FL",
  "session": "2026",
  "url": "https://digitaldemocracyproject.org/bills/fl/2026/HB%20123",
  "ddp_url": "https://digitaldemocracyproject.org/bills/fl/2026/HB%20123"
}
```
`title` is included when the URL used a broker id (the scorecard carries it). `source_url` is not returned: neither broker endpoint has it, and the citation URL comes from the vectors' own metadata.

### Feature Flags

```
GET /votebot/v1/features
```

Returns the set of feature flags relevant to the chat widget. Public (unauthenticated) — only exposes booleans, no secrets. Used by the embeddable widget to discover whether to render quick-action buttons on bill pages.

**Response:**
```json
{
  "quick_action_buttons_enabled": false
}
```

### Button Cache Admin

```
DELETE /votebot/v1/cache/button/{slug}
```

Force-clear all cached button responses (`summary`, `pros_cons`) for a bill slug. Authenticated via Bearer token. Idempotent — returns the count of cache entries deleted (0–2). Useful when a manual content edit bypasses DDP-Sync's amendment detection. Normal cache invalidation is automatic via Redis pub/sub on bill version changes.

**Response:**
```json
{
  "slug": "hr-1234-2025",
  "deleted": 2
}
```

### Unified Sync API

> **Sync/ingestion has moved entirely to [DDP-Sync](https://github.com/Digital-Democracy-Project/ddp-sync).** VoteBot no longer implements a sync API, ingestion pipeline, or scheduler — it is a chat/RAG-only service (see `src/votebot/main.py`). DDP-API proxies `/sync/*` and `/trigger/*` to DDP-Sync, which runs as a standalone service on port 8001. See the DDP-Sync repo for its current sync API, request/response shapes, and CLI.

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

For running VoteBot for the new site (the old Webflow copy frozen on its own branch, the new copy on the new-infrastructure server, nginx, origins, bot monitoring, cutover and rollback), see [docs/RUNBOOK-civic-host-ddp-next.md](docs/RUNBOOK-civic-host-ddp-next.md).

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
| `WEBFLOW_ORG_LOOKUP_ENABLED` | Enable the runtime organization-position lookups (Webflow CMS on the legacy index, ddp-broker-py on the canonical-id index): the switch to turn them off without redeploying | `true` |
| `PDF_MAX_PAGES` | Maximum PDF pages to process per bill (0 = unlimited) | `1000` |

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
python -m votebot.utils.federal_legislator_cache

# Show cached legislators
python -m votebot.utils.federal_legislator_cache --show
```

> **Note**: Bill syncing (which injects OpenStates person IDs into vote content) and building the `legislator-votes` reverse index from `bill-votes` documents are now handled by [DDP-Sync](https://github.com/Digital-Democracy-Project/ddp-sync) — this functionality no longer lives in VoteBot. `federal_legislator_cache` is the one piece VoteBot still owns locally, since the chat agent depends on it at query time (`src/votebot/core/retrieval.py`).

### Document Types

| Document Type | Description | ID Format |
|--------------|-------------|-----------|
| `bill` | CMS summary (description, support/oppose, org positions) | `bill-webflow-{webflow_id}` |
| `bill-text` | Current legislative text (PDF/HTML) — overwritten on each version | `bill-pdf-{webflow_id}` |
| `bill-text-history` | Permanent per-version copy of bill text — never overwritten | `bill-text-history-{webflow_id}-{version_date}` |
| `bill-changelog` | LLM-generated diff between consecutive versions (gpt-4o-mini). Legacy index only | `bill-changelog-{webflow_id}-{version_date}` |
| `bill-version-diff` | **Not embedded** on the canonical-id index (decided 2026-10-05). "What changed" is answered from api-v3's stored diff, read live; see "What changed" below | none (read live) |
| `bill-votes` | Per-bill vote records with all legislators | `bill-votes-{webflow_id}` |
| `legislator-votes` | Per-legislator voting history | `legislator-votes-{person_uuid}` |

#### Version-aware answers (canonical-id index only)

On the `ddp-knowledge-base` index every version of a bill is embedded, so retrieval has to say which one it is reading:

- **Current by default.** For a bill page VoteBot asks api-v3 (`/bills/ocd-bill/{uuid}?include=versions`, cached 120 s; a failed lookup is remembered 45 s and the call times out after 3 s) which version is current and filters `bill-text` to that version's `document_id`. "Current" is looked up per request, never stored on vectors. It is the latest classifiable version (stage not `unknown`); if that version has no archived text yet there is no current version, rather than an older one being called current.
- **Named versions.** A question naming a stage ("as introduced", "the engrossed version", "the text as enrolled", "as enacted"; bare "engrossed"/"enrolled"/"chaptered" count only with "version", "text", "draft" or "as", since "is it enrolled yet?" is a status question) or a date together with the word "version" or "draft" ("the version from March 4, 2026") is answered from those versions instead; stages are api-v3's `introduced / amendment / chamber_passage / final_passage / enacted`.
- **What changed.** Diffs are not in the index: a changelog question reads api-v3's stored `diff_from_previous_version` live (the current version's, or the named version's), labelled with both versions and cut to 12,000 characters per version with a note when cut. Every classifiable version has a stored diff (api-v3, since OPEN-118) except a bill's first version and any whose predecessor has no saved text, and for those the answer says it cannot show the change. Diffs together are capped at 12,000 characters, and what is omitted is said. If api-v3 cannot be reached the model is told so and must say it could not retrieve the change, rather than describe it from memory; a version with no stored diff is told apart from an outage. A diff is only used when the version before it (api-v3's own lineage: classifiable versions in order) has archived text, so it is never labelled with the wrong comparison. Needs `USE_DDP_OPENSTATES_REPLICA`.
- **Display.** Chunks are grouped by version under a header like `HB 1 · Engrossed · 2026-03-04 · current`, and the prompt requires every claim to name its version.
- **Needs `USE_DDP_OPENSTATES_REPLICA=true`.** The version fields exist only on DDP's api-v3. Without it, or if api-v3 cannot be reached, no version filter is applied: the bill's chunks come back from every version, each labelled with its own, and the context says no version is current so the answer must not pick one silently. A version that is asked for (or the current one) but has no text in the index returns nothing rather than other versions.

### OpenStates Person ID Coverage

- **State bills**: ~100% coverage (person IDs from OpenStates API)
- **Federal bills**: ~68% coverage (matched via federal legislator cache)

## Data Ingestion

All content ingestion now happens in [DDP-Sync](https://github.com/Digital-Democracy-Project/ddp-sync), a standalone service — VoteBot contains no sync/ingestion code (removed in favor of DDP-Sync; see `src/votebot/main.py`). VoteBot only reads from Pinecone at query time, plus the runtime CMS/OpenStates lookups described below. The primary data sources (ingested by DDP-Sync) are:
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

> **Note**: The Pinecone rebuild/sync scripts formerly documented here (`scripts/rebuild_pinecone.py`, `scripts/sync.py`, `scripts/seed_data.py`, etc.) depended on the sync/ingestion code that has since moved to DDP-Sync — they are no longer functional against this repo. See [DDP-Sync](https://github.com/Digital-Democracy-Project/ddp-sync) for current ingestion, full-rebuild, and CLI tooling.

### Sync Scheduling

> **Sync scheduling has moved to [DDP-Sync](https://github.com/Digital-Democracy-Project/ddp-sync).** DDP-Sync runs as a standalone service on port 8001, handling all scheduled and on-demand sync operations. VoteBot no longer runs a scheduler — it is a chat-only service.

**Schedule (managed by DDP-Sync):**

| Content | Frequency | Time (UTC) |
|---------|-----------|------------|
| Bill versions | Daily | 04:00 |
| Legislators | Weekly | 06:00 Sunday |
| Organizations | Monthly | 08:00 1st |
| Voatz → Brevo user sync | Every 30 min | — |
| Webflow CMS batch jobs | Weekly | 03:00 Monday |

See [DDP-Sync README](https://github.com/Digital-Democracy-Project/ddp-sync) for full schedule, trigger endpoints, and configuration.

## Development

### Local dev copy on the Mac Studio

A copy of VoteBot runs on the Mac Studio against the **dev** stack, so a change can be tested without a production deploy: VoteBot on `127.0.0.1:8010`, the dev broker (`ddpbroker-web-1`, `:8080`), the local api-v3 (`:8002`), its own Redis (`votebot-dev-redis`, `127.0.0.1:6380`) and the shared Pinecone index `ddp-knowledge-base`, read-only. Ports are in the [ddp-infra port registry](https://github.com/Digital-Democracy-Project/ddp-infra#port-registry).

- **Config:** a git-ignored `.env` (mode 600). The OpenAI and Pinecone keys are the dev broker's (`ddp-broker-py/.env`) and the api-v3 key is `LOCAL_OPENSTATES_API_KEY` in `ddp-sync/.env`; set `DDP_BROKER_API_ROOT=http://localhost:8080`, `USE_DDP_OPENSTATES_REPLICA=true`, `DDP_OPENSTATES_API_ROOT=http://localhost:8002`, `DDP_OPENSTATES_AUTH_HEADER=x-api-key`, `REDIS_URL=redis://localhost:6380/0`, `DDP_SITE_BASE_URL=http://localhost:3000`. **No Slack tokens**: one Slack connection per app token, and production holds it. Tests cost real (small) OpenAI usage.
- **Start:** `infrastructure/start-votebot-dev.sh` (creates the Redis container if missing, waits for Docker, the broker and api-v3, then runs `uvicorn`). As a system LaunchDaemon (`com.ddp.votebot-dev`, like ddp-sync and the others) it starts at boot; the plist is `infrastructure/launchd/com.ddp.votebot-dev.plist` and an admin installs it with `sudo bash infrastructure/install-votebot-dev-daemon.sh` (`--check` first, no sudo, changes nothing; `--uninstall` removes it); the service account has no sudo. It is installed (VOTEBOT-32, 2026-10-09): launchd restarts a killed process within seconds. The Mac Studio is a server and is rarely rebooted, so a reboot has not been tried; if the daemon does not return after one, `logs/dev-server.log` names what the start script was waiting for. Without the daemon, run the start script by hand.
- **Check:** `curl localhost:8010/votebot/v1/health/ready`, then `python scripts/smoke_ws.py --url ws://localhost:8010/ws/chat --cases scripts/smoke_cases.json --retrieval`.
- **Use FL 2026E HB 5601E** (it has real organization positions); FL HB 7089 is not in the dev broker. Known open bug (VOTEBOT-30): naming that bill by number in a message ("Who voted in HB 5601E?") looks up `HB5601`, without the trailing letter, so the live vote lookup fails; ask without the number on the bill's page. The REST endpoint skips the bill pre-fetch the chat widget uses, so test the streaming path over the websocket.

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
├── src/votebot/              # Chat/RAG only — sync & ingestion live in DDP-Sync
│   ├── main.py              # FastAPI application
│   ├── config.py            # Configuration
│   ├── api/                  # API layer
│   │   ├── routes/          # Endpoint handlers
│   │   │   ├── chat.py      # POST /chat, /chat/stream endpoints
│   │   │   ├── websocket.py # WebSocket /ws/chat
│   │   │   ├── health.py    # Health checks
│   │   │   ├── content.py   # Content resolution
│   │   │   ├── features.py  # Feature-flag discovery
│   │   │   └── cache_admin.py  # Button cache admin
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
│   │   ├── button_cache.py  # Redis-backed quick-action button cache
│   │   ├── webflow_lookup.py # Runtime Webflow CMS lookup (bill→org + org→bill + verification + gov-url write)
│   │   ├── redis_store.py   # Redis client for cross-worker state (thread mapping + pub/sub + active jurisdictions + bill version cache)
│   │   ├── query_logger.py  # Event & query logger (JSONL, date-partitioned, 3 event types)
│   │   └── slack.py         # Slack human handoff
│   └── utils/               # Utility modules
│       ├── legislative_calendar.py  # Session date lookup (live OpenStates + hardcoded fallback)
│       ├── federal_legislator_cache.py  # Federal legislator ID cache (only sync-adjacent file VoteBot still owns)
│       └── intent.py         # Two-level intent classification (primary + sub) with controlled enums
├── scripts/
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

## WebSocket Smoke Test

`scripts/smoke_ws.py` opens `/ws/chat` the way the widget does and checks the streamed answer, citations and confidence for a list of bills. It needs no Webflow credentials and works against either index, so it is the check for the canonical-index cutover and for a rollback to `votebot-large`.

```bash
# The five-jurisdiction cutover check (FL, WA, US, VA, MI). The cases pick an embedded bill themselves
# (`discover`), so nothing needs filling in; discovery and --retrieval read the index and need the target's .env keys.
python scripts/smoke_ws.py --url wss://<votebot-host>/ws/chat --cases scripts/smoke_cases.json --retrieval

# Without --retrieval and discovery (cases that name their bill): citations-only isolation, no keys needed
python scripts/smoke_ws.py --url wss://<votebot-host>/ws/chat --cases my_cases.json

# Against the legacy index (skips the citation isolation and live-votes-tool checks, which are for the canonical index)
python scripts/smoke_ws.py --index legacy --cases my_legacy_cases.json
```

Per question it checks the frame order, a non-empty answer, `confidence >= --min-confidence`, and optional per-question assertions: `expect_any` / `expect_none` / `expect_regex` on the answer (use text the question does not contain, or the check passes on any reply), `min_citations`, and `expect_votes_tool`. **Votes are not embedded in the new index**, so a vote question must be answered by the live OpenStates tool: the `stream_end` frame carries `bill_votes_tool_used`, and `expect_votes_tool: true` fails the question if retrieval answered instead (asserted only with `--index canonical`; on the legacy index either path may answer). Bill isolation on the canonical index fails if a citation carries another bill's id, and a question that requires citations must have at least one that carries *this* bill's id, so zero evidence cannot pass.

With `--retrieval` it also checks that every chunk retrieved for the bill carries that bill's id, that the `--expect-types` document types are present (default `bill-text`), that `bill-text` chunks carry the `document_id` api-v3 calls current, and that the index holds **no** documents of the `--forbid-types` (default `bill-votes,bill-version-diff`, neither of which is embedded). That check first runs a positive control (a `bill-text` query through the same path must find something), so a wrong index, namespace or filter cannot pass it by returning nothing. `--retrieval` reads the index from the machine running the script, so it only proves the target if both use the same `PINECONE_INDEX_NAME`/`NAMESPACE` (the script says so when it runs). Without it the report says isolation was judged from citations only. A case may give only a `ddp_url` plus `--resolve-base` to resolve its page context through `/content/resolve`. The exit status is non-zero if any case fails; `--timeout` bounds each frame and `--turn-timeout` a whole question.

"What changed from the previous version" is not a smoke question: diffs are not in the index, so it cannot be answered from retrieval.

The protocol itself is covered offline by `tests/unit/test_websocket_protocol.py` (handshake, streaming frames including `bill_votes_tool_used`, `context_update`, `ping`, `empty_message`, page-context hand-off to the agent), and the script's own logic by `tests/unit/test_smoke_ws.py`, which runs it against a local server with a faked agent.

## User Analytics & Production Monitoring

VoteBot uses an event-based logging system that captures user behavior, query outcomes, and conversation metrics for offline analytics and quality evaluation.

### Event Model

Three event types, all written to the same date-partitioned JSONL files:

| Event | When Emitted | Purpose |
|-------|-------------|---------|
| `message_received` | Server receives a user message, before processing | Tracks message arrival, visitor identity, page context |
| `query_processed` | Agent finishes processing and responds | Full behavioral/outcome record: intent, retrieval, grounding, fallback, confidence |
| `conversation_ended` | Conversation boundary detected or session disconnects | Summary: turn count, duration, handoff/fallback/retrieval-miss flags, terminal state |

### Identity Model

| Level | ID | Lifetime | Purpose |
|---|---|---|---|
| **Visitor** | `visitor_id` | localStorage (permanent, best-effort) | Cross-session device tracking |
| **Session** | `session_id` | sessionStorage (per-tab, 30-min timeout) | Visit-level intent |
| **Conversation** | `conversation_id` | Server-side (resets on boundary) | Multi-turn behavior |

`visitor_id` tracks browser instances, not people. It is generated client-side and sent in the WebSocket payload.

### Key Fields on `query_processed`

| Field | Description |
|-------|-------------|
| `timestamp` | ISO 8601 UTC timestamp |
| `visitor_id` | Persistent device identifier (localStorage) |
| `session_id` | Chat session identifier |
| `conversation_id` | `{session_id}:{n}` — resets on conversation boundary |
| `session_message_index` | Message position within session |
| `conversation_message_index` | Message position within conversation (resets per conversation) |
| `message` / `response` | User query and LLM response text |
| `primary_intent` / `sub_intent` | Two-level intent classification (central enum, keyword heuristics) |
| `confidence` | Response confidence score (0-1) |
| `retrieval_count` | Number of RAG chunks retrieved |
| `retrieval_sources` | Document types retrieved (normalized to controlled vocabulary) |
| `has_citations` / `citations_count` | Whether citations were surfaced and how many |
| `grounding_status` | `grounded`, `partial`, or `ungrounded` |
| `external_augmentation` | `none` or `web` |
| `web_search_used` | Whether OpenAI web search was invoked |
| `fallback_used` / `fallback_reason` | Whether RAG was insufficient and why |
| `handoff_triggered` | Whether human handoff was triggered |
| `error` / `error_type` | Whether processing failed and error category |
| `device_type` | `desktop`, `mobile`, or `tablet` (derived from User-Agent) |
| `entry_referrer` | Referring domain (first message per session only, null by design thereafter) |
| `page_url` / `scroll_depth` / `time_on_page` | Page engagement data from widget |

### Conversation Boundaries

A new conversation starts when:
1. Explicit session reset
2. Inactivity > 10 minutes
3. Page type changes (bill → legislator, etc.)
4. Page ID changes within same type (only if previous conversation had a response)

### Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `QUERY_LOG_ENABLED` | Enable/disable query logging | `true` |
| `QUERY_LOG_DIR` | Directory for JSONL files | `logs/queries` |

### Offline Evaluation & Analytics

The evaluation script reads production logs, validates against Webflow CMS ground truth, and generates analytics reports:

```bash
# Evaluate today's queries
PYTHONPATH=src python scripts/evaluate_production.py

# Evaluate a specific date
PYTHONPATH=src python scripts/evaluate_production.py --date 2026-02-08

# Evaluate last 7 days, filtered by jurisdiction
PYTHONPATH=src python scripts/evaluate_production.py --days 7 --jurisdiction FL --verbose

# Filter to a specific visitor
PYTHONPATH=src python scripts/evaluate_production.py --days 30 --visitor v_a1b2c3d4e5f6

# Filter by event type
PYTHONPATH=src python scripts/evaluate_production.py --days 7 --event-type conversation_ended

# Re-classify sub_intent using current intent.py (useful after classifier changes)
PYTHONPATH=src python scripts/evaluate_production.py --days 7 --reclassify-intents

# Recompute has_citations using current detection patterns (useful after citation logger fixes)
PYTHONPATH=src python scripts/evaluate_production.py --days 7 --recompute-citations
```

The report includes:
- **Ground truth validation**: Pass rate per entity type (bill, organization, legislator)
- **Success tiers**: System success, citation-grounded success, heuristic answer success
- **Intent distribution**: Breakdown by `primary_intent` and `sub_intent`
- **Fallback analysis**: Fallback rate by reason, distinct from web search rate
- **Grounding distribution**: % grounded vs partial vs ungrounded, cross-tabulated with external augmentation
- **Conversation metrics**: Avg turns, drop-off rate, duration, terminal state distribution (from `conversation_ended` events)
- **Handoff rates**: At query, conversation, and session levels
- **Visitor metrics**: Unique visitors, queries per visitor
- **Device distribution**: Desktop vs mobile vs tablet
- **Confidence and citation analysis**: Low-confidence flagging, citation rates

## Opinion Elicitation (Exploratory)

A guided opinion-elicitation system — capturing voter opinions on legislation through natural conversation, using multi-position opinion vectors, Polis for clustering, Memberstack for accounts, and Catalist for voter verification — has been discussed as a future direction for VoteBot. There is no implementation in this repo (no code, routes, or dependencies for Jigsaw/Polis/Memberstack/Catalist exist under `src/`). The design docs and staged rollout plan now live in the ddp-infra repo alongside the rest of the fleet's `PLAN-*.md` files.

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
- Missing line breaks in responses (three bugs: SDK block-boundary whitespace loss → `_join_response_blocks()`; intermittent model bullet formatting → system prompt; widget markdown parser paragraph/list bugs → `chat-widget/src/ui.js`)
- Production query monitoring (JSONL logging, offline evaluation)
- Wrong-state lookups, false "dispute" detection, and links that do not open on the new index (VOTEBOT-23 to 30), with the log lines that tell them apart
- Batch sync progress reporting and checkpoint/resume after worker crash
- Large PDF memory management (incremental embed+upsert, gc per bill, page limit)
- DDP-Sync issues (Redis health check, trigger endpoints)

## Contributing

1. Create a feature branch
2. Make your changes
3. Run tests and linting
4. Submit a pull request

## License

This project is open source and available under the [MIT License](LICENSE).
