# VoteBot 2.0

High-performance, context-aware chat API for the Digital Democracy Project.

## Overview

VoteBot 2.0 is a RAG-powered chatbot API that provides intelligent, context-aware responses about legislation, legislators, and civic engagement. It's designed to be UI-agnostic and can be integrated with various chat interfaces.

## Features

- **Context-Aware Responses**: Understands the page context (bill, legislator, general) to provide relevant answers
- **RAG-Powered**: Uses Pinecone vector database for semantic search and retrieval
- **Human Handoff**: Supports seamless handoff to human agents when needed
- **Multi-Source Data**: Ingests data from Congress.gov, OpenStates, and custom sources
- **Real-Time Updates**: Hourly polling for content changes
- **High Performance**: Designed for 1000+ concurrent conversations

## Tech Stack

- **Framework**: FastAPI (Python 3.11+)
- **Vector Database**: Pinecone
- **LLM**: OpenAI GPT-4
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
| `PINECONE_ENVIRONMENT` | Pinecone environment | Yes |
| `PINECONE_INDEX_NAME` | Pinecone index name | Yes |
| `CONGRESS_API_KEY` | Congress.gov API key | For ingestion |
| `OPENSTATES_API_KEY` | OpenStates API key | For ingestion |
| `REDIS_URL` | Redis connection URL | Optional |
| `API_KEY` | API key for authentication | Yes |

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

## Data Ingestion

### Manual Ingestion

```bash
# Ingest from Congress.gov
python scripts/ingest.py congress --congress 118 --limit 100

# Ingest from OpenStates
python scripts/ingest.py openstates --jurisdiction ca --limit 100

# Ingest PDFs
python scripts/ingest.py pdf /path/to/pdfs --recursive
```

### Seed Development Data

```bash
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
│   │   ├── schemas/         # Request/response models
│   │   └── middleware/      # Auth, logging
│   ├── core/                # Business logic
│   │   ├── agent.py         # Conversational agent
│   │   ├── retrieval.py     # RAG retrieval
│   │   └── prompts.py       # System prompts
│   ├── services/            # External integrations
│   │   ├── llm.py           # OpenAI client
│   │   ├── embeddings.py    # Embedding generation
│   │   └── vector_store.py  # Pinecone operations
│   ├── ingestion/           # Data ingestion
│   │   ├── pipeline.py      # Main orchestrator
│   │   ├── sources/         # Data source connectors
│   │   └── chunking.py      # Text chunking
│   └── updates/             # Real-time updates
│       ├── scheduler.py     # Hourly polling
│       └── change_detection.py
└── tests/
    ├── unit/
    ├── integration/
    └── load/
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
