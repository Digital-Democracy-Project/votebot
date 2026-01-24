"""Tests for API endpoints."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock

from votebot.main import app


class TestHealthEndpoints:
    """Tests for health check endpoints."""

    def test_health_check(self, client, api_headers):
        """Test basic health check endpoint."""
        response = client.get("/votebot/v1/health", headers=api_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data
        assert "environment" in data

    def test_liveness_check(self, client):
        """Test liveness check endpoint (no auth required)."""
        response = client.get("/votebot/v1/health/live")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"


class TestAuthMiddleware:
    """Tests for API key authentication."""

    def test_missing_api_key(self, client):
        """Test that missing API key returns 401."""
        response = client.post(
            "/votebot/v1/chat",
            json={
                "message": "test",
                "session_id": "123",
                "page_context": {"type": "general"},
            },
        )

        assert response.status_code == 401
        assert "Missing API key" in response.json()["detail"]

    def test_invalid_api_key(self, client):
        """Test that invalid API key returns 401."""
        response = client.post(
            "/votebot/v1/chat",
            headers={"X-API-Key": "wrong-key"},
            json={
                "message": "test",
                "session_id": "123",
                "page_context": {"type": "general"},
            },
        )

        assert response.status_code == 401
        assert "Invalid API key" in response.json()["detail"]


class TestChatEndpoint:
    """Tests for chat endpoint."""

    def test_chat_human_active_suppression(self, client, api_headers):
        """Test that human_active=true returns suppressed response."""
        response = client.post(
            "/votebot/v1/chat",
            headers=api_headers,
            json={
                "message": "Hello",
                "session_id": "test-123",
                "human_active": True,
                "page_context": {"type": "general"},
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["response"] is None
        assert data["suppressed"] is True
        assert data["confidence"] == 0.0

    @patch("votebot.api.routes.chat.VoteBotAgent")
    def test_chat_success(self, mock_agent_class, client, api_headers):
        """Test successful chat response."""
        # Setup mock
        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.response = "This is the response."
        mock_result.citations = []
        mock_result.confidence = 0.9
        mock_result.requires_human = False
        mock_result.tokens_used = 100
        mock_result.retrieval_count = 5
        mock_result.cached = False

        mock_agent.process_message = AsyncMock(return_value=mock_result)
        mock_agent_class.return_value = mock_agent

        response = client.post(
            "/votebot/v1/chat",
            headers=api_headers,
            json={
                "message": "What does this bill do?",
                "session_id": "test-123",
                "human_active": False,
                "page_context": {
                    "type": "bill",
                    "id": "HR-1234",
                    "jurisdiction": "US",
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["response"] == "This is the response."
        assert data["confidence"] == 0.9
        assert data["suppressed"] is False

    def test_chat_validation_error(self, client, api_headers):
        """Test chat with invalid request."""
        response = client.post(
            "/votebot/v1/chat",
            headers=api_headers,
            json={
                "message": "",  # Empty message
                "session_id": "test-123",
                "page_context": {"type": "general"},
            },
        )

        assert response.status_code == 422  # Validation error

    def test_chat_invalid_page_type(self, client, api_headers):
        """Test chat with invalid page type."""
        response = client.post(
            "/votebot/v1/chat",
            headers=api_headers,
            json={
                "message": "Hello",
                "session_id": "test-123",
                "page_context": {"type": "invalid"},
            },
        )

        assert response.status_code == 422


class TestChatFeedbackEndpoint:
    """Tests for chat feedback endpoint."""

    def test_submit_feedback(self, client, api_headers):
        """Test submitting feedback."""
        response = client.post(
            "/votebot/v1/chat/feedback",
            headers=api_headers,
            params={
                "session_id": "test-123",
                "message_id": "msg-456",
                "feedback": "positive",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "received"
        assert data["session_id"] == "test-123"
