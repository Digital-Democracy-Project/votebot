"""Pytest configuration and shared fixtures."""

import os
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

# Set test environment before imports
os.environ["ENVIRONMENT"] = "development"
os.environ["DEBUG"] = "true"
os.environ["OPENAI_API_KEY"] = "test-openai-key"
os.environ["PINECONE_API_KEY"] = "test-pinecone-key"
os.environ["API_KEY"] = "test-api-key"

from votebot.api.schemas.chat import ChatRequest, PageContext
from votebot.config import Settings, get_settings
from votebot.main import app


@pytest.fixture
def settings() -> Settings:
    """Get test settings."""
    return Settings(
        environment="development",
        debug=True,
        api_key="test-api-key",
        openai_api_key="test-openai-key",
        pinecone_api_key="test-pinecone-key",
        pinecone_index_name="votebot-test",
    )


@pytest.fixture
def client() -> TestClient:
    """Create a test client for the FastAPI app."""
    return TestClient(app)


@pytest.fixture
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    """Create an async test client."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.fixture
def api_headers() -> dict:
    """Get headers with API key for authenticated requests."""
    return {"X-API-Key": "test-api-key"}


@pytest.fixture
def sample_chat_request() -> ChatRequest:
    """Create a sample chat request."""
    return ChatRequest(
        message="What does this bill do?",
        session_id="test-session-123",
        human_active=False,
        page_context=PageContext(
            type="bill",
            id="HR-1234",
            jurisdiction="US",
        ),
    )


@pytest.fixture
def sample_bill_context() -> PageContext:
    """Create a sample bill page context."""
    return PageContext(
        type="bill",
        id="HR-1234",
        jurisdiction="US",
        title="Sample Bill Act",
    )


@pytest.fixture
def sample_legislator_context() -> PageContext:
    """Create a sample legislator page context."""
    return PageContext(
        type="legislator",
        id="bioguide-S000123",
        jurisdiction="CA",
        title="Rep. Jane Smith",
    )


@pytest.fixture
def sample_general_context() -> PageContext:
    """Create a sample general page context."""
    return PageContext(
        type="general",
    )


@pytest.fixture
def mock_openai():
    """Mock OpenAI client."""
    with patch("votebot.services.llm.AsyncOpenAI") as mock:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(
                message=MagicMock(content="This is a test response."),
                finish_reason="stop",
            )
        ]
        mock_response.usage = MagicMock(total_tokens=100)
        mock_response.model = "gpt-4-turbo-preview"

        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock.return_value = mock_client

        yield mock_client


@pytest.fixture
def mock_pinecone():
    """Mock Pinecone client."""
    with patch("votebot.services.vector_store.Pinecone") as mock:
        mock_pc = MagicMock()
        mock_index = MagicMock()

        # Mock query response
        mock_index.query.return_value = MagicMock(
            matches=[
                MagicMock(
                    id="doc-1",
                    score=0.9,
                    metadata={"content": "Test content 1", "source": "test"},
                ),
                MagicMock(
                    id="doc-2",
                    score=0.8,
                    metadata={"content": "Test content 2", "source": "test"},
                ),
            ]
        )

        mock_index.upsert.return_value = None
        mock_index.describe_index_stats.return_value = MagicMock(total_vector_count=100)

        mock_pc.Index.return_value = mock_index
        mock_pc.list_indexes.return_value = [MagicMock(name="votebot-test")]
        mock.return_value = mock_pc

        yield mock_pc


@pytest.fixture
def mock_embeddings():
    """Mock embedding service."""
    with patch("votebot.services.embeddings.AsyncOpenAI") as mock:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.data = [
            MagicMock(embedding=[0.1] * 3072)
        ]
        mock_response.usage = MagicMock(total_tokens=10)
        mock_response.model = "text-embedding-3-large"

        mock_client.embeddings.create = AsyncMock(return_value=mock_response)
        mock.return_value = mock_client

        yield mock_client


@pytest.fixture
def sample_chunks():
    """Create sample document chunks."""
    return [
        {
            "id": "doc-1",
            "content": "This bill establishes new requirements for clean energy.",
            "metadata": {"source": "Congress.gov", "bill_id": "HR-1234"},
        },
        {
            "id": "doc-2",
            "content": "The bill was introduced on January 15, 2024.",
            "metadata": {"source": "Congress.gov", "bill_id": "HR-1234"},
        },
    ]
