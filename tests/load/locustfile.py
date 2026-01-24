"""Load testing configuration using Locust.

Run with: locust -f tests/load/locustfile.py --host=http://localhost:8000
"""

import random
import uuid

from locust import HttpUser, between, task


class VoteBotUser(HttpUser):
    """Simulated user for load testing VoteBot API."""

    wait_time = between(1, 3)  # Wait 1-3 seconds between requests

    def on_start(self):
        """Set up headers on user start."""
        self.headers = {
            "X-API-Key": "test-api-key",
            "Content-Type": "application/json",
        }
        self.session_id = str(uuid.uuid4())

    @task(10)
    def chat_bill_context(self):
        """Chat with bill context (most common)."""
        messages = [
            "What does this bill do?",
            "Who sponsored this bill?",
            "What is the current status of this bill?",
            "What are the key provisions?",
            "When was this bill introduced?",
        ]

        self.client.post(
            "/votebot/v1/chat",
            headers=self.headers,
            json={
                "message": random.choice(messages),
                "session_id": self.session_id,
                "human_active": False,
                "page_context": {
                    "type": "bill",
                    "id": f"HR-{random.randint(1, 5000)}",
                    "jurisdiction": "US",
                },
            },
        )

    @task(5)
    def chat_legislator_context(self):
        """Chat with legislator context."""
        messages = [
            "What committees is this legislator on?",
            "What bills has this legislator sponsored?",
            "What is their voting record?",
            "How long have they been in office?",
        ]

        self.client.post(
            "/votebot/v1/chat",
            headers=self.headers,
            json={
                "message": random.choice(messages),
                "session_id": self.session_id,
                "human_active": False,
                "page_context": {
                    "type": "legislator",
                    "id": f"bioguide-{random.randint(1000, 9999)}",
                    "jurisdiction": random.choice(["US", "CA", "NY", "TX"]),
                },
            },
        )

    @task(3)
    def chat_general_context(self):
        """Chat with general context."""
        messages = [
            "How does a bill become a law?",
            "What is the legislative process?",
            "How can I find my representative?",
            "What is Digital Democracy Project?",
        ]

        self.client.post(
            "/votebot/v1/chat",
            headers=self.headers,
            json={
                "message": random.choice(messages),
                "session_id": self.session_id,
                "human_active": False,
                "page_context": {
                    "type": "general",
                },
            },
        )

    @task(1)
    def health_check(self):
        """Check health endpoint."""
        self.client.get(
            "/votebot/v1/health",
            headers=self.headers,
        )

    @task(1)
    def liveness_check(self):
        """Check liveness endpoint (no auth)."""
        self.client.get("/votebot/v1/health/live")


class HumanHandoffUser(HttpUser):
    """Simulated user testing human handoff scenarios."""

    wait_time = between(2, 5)

    def on_start(self):
        """Set up headers on user start."""
        self.headers = {
            "X-API-Key": "test-api-key",
            "Content-Type": "application/json",
        }
        self.session_id = str(uuid.uuid4())

    @task
    def human_active_request(self):
        """Test human_active suppression."""
        self.client.post(
            "/votebot/v1/chat",
            headers=self.headers,
            json={
                "message": "Hello",
                "session_id": self.session_id,
                "human_active": True,
                "page_context": {
                    "type": "general",
                },
            },
        )


class StressTestUser(HttpUser):
    """User for stress testing with minimal wait time."""

    wait_time = between(0.1, 0.5)

    def on_start(self):
        """Set up headers on user start."""
        self.headers = {
            "X-API-Key": "test-api-key",
            "Content-Type": "application/json",
        }

    @task
    def rapid_chat(self):
        """Send rapid chat requests."""
        self.client.post(
            "/votebot/v1/chat",
            headers=self.headers,
            json={
                "message": "Quick question",
                "session_id": str(uuid.uuid4()),
                "human_active": False,
                "page_context": {"type": "general"},
            },
        )
