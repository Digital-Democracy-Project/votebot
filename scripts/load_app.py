"""The VoteBot app with the LLM answered by a stub, for `scripts/load_ws.py` (VOTEBOT-34).

Retrieval, embeddings, Pinecone, api-v3 and the button cache are the real ones (a query costs a fraction of a cent);
only the answer generation is faked, so a load test measures VoteBot's own capacity and spends no LLM money.
Run by `load_ws.py`; by hand: `uvicorn load_app:app --app-dir scripts --workers 2` (cwd = repo root, for .env).
"""

import asyncio
import os

# Before votebot.main reads its settings: nothing a test writes may reach the dev button cache (Redis db 5) or the query logs.
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/5")
os.environ.setdefault("QUERY_LOG_ENABLED", "false")
os.environ.setdefault("VOTEBOT_QUICK_ACTION_BUTTONS", "true")

from votebot.main import app  # noqa: E402
from votebot.services import llm  # noqa: E402

STUB_SECONDS = float(os.environ.get("LOAD_STUB_LLM_SECONDS", "0.5"))  # roughly a streamed answer's first-to-last token
ANSWER = ["This is a ", "stubbed answer ", "for load testing."]


async def _stub_stream(self, messages, system_prompt=None, max_tokens=None, temperature=None, enable_web_search=False):
    for part in ANSWER:
        await asyncio.sleep(STUB_SECONDS / len(ANSWER))
        yield llm.StreamChunk(text=part, done=False)
    yield llm.StreamChunk(text="", done=True)


llm.LLMService.stream = _stub_stream

__all__ = ["app"]
