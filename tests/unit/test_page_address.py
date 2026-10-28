"""VOTEBOT-63: the query log and the Slack handoff keep only origin and path of a page address.

An address can carry a sign-in token or personal data (query string, fragment) or credentials before the host.
Old widget builds keep sending the full address, so the backend reduces it itself before it is written or posted.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from votebot.services.query_logger import QueryLogger
from votebot.services.slack import SlackService
from votebot.utils.page_address import page_address

FULL = "https://user:secret@example.test:8443/explore/FL/2026/HB%20219?token=abc123&utm_source=x#access_token=zzz"
SAFE = "https://example.test:8443/explore/FL/2026/HB%20219"


class TestPageAddress:
    def test_query_fragment_and_credentials_are_dropped(self):
        assert page_address(FULL) == SAFE

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("https://example.test/", "https://example.test/"),
            ("https://example.test", "https://example.test"),
            ("http://example.test/a/b?x=1", "http://example.test/a/b"),
            ("https://example.test/a#frag", "https://example.test/a"),
            ("  https://example.test/a?x=1  ", "https://example.test/a"),
            ("https://example.test/a@b?x=1", "https://example.test/a@b"),  # an "@" in the path is not credentials
            ("https://[::1]:8000/a?x=1", "https://[::1]:8000/a"),
            (
                "https://votebot.digitaldemocracyproject.org/?ddp_url=https://digitaldemocracyproject.org/bills/x",
                "https://votebot.digitaldemocracyproject.org/",
            ),
        ],
    )
    def test_the_rest_is_kept(self, value, expected):
        assert page_address(value) == expected

    @pytest.mark.parametrize(
        "value", [None, "", "   ", 5, "not a url?token=abc", "javascript:alert(1)//?token=abc", "ftp://example.test/a", "https://"]
    )
    def test_anything_that_is_not_an_http_address_is_dropped_not_kept(self, value):
        assert page_address(value) is None


def _entries(directory):
    (log_file,) = list(directory.glob("*.jsonl"))
    return [json.loads(line) for line in log_file.read_text().splitlines()]


class TestQueryLog:
    async def test_an_event_logs_the_page_url_and_the_context_url_without_query_fragment_or_credentials(self, tmp_path):
        await QueryLogger(str(tmp_path)).log_event(
            event_type="message_received",
            session_id="s1",
            page_url=FULL,
            page_context={"type": "bill", "id": "HB 219", "url": FULL},
            client_ip="203.0.113.9",
        )
        (entry,) = _entries(tmp_path)
        assert entry["page_url"] == SAFE
        assert entry["page_context"] == {"type": "bill", "id": "HB 219", "url": SAFE}
        assert "secret" not in json.dumps(entry) and "abc123" not in json.dumps(entry) and "zzz" not in json.dumps(entry)

    async def test_the_legacy_query_entry_is_reduced_too(self, tmp_path):
        await QueryLogger(str(tmp_path)).log_query(
            session_id="s1",
            message="hi",
            response="hello",
            confidence=0.9,
            citations=[],
            page_context={"type": "general", "url": FULL},
            channel="rest",
            duration_ms=5,
        )
        (entry,) = _entries(tmp_path)
        assert entry["page_context"]["url"] == SAFE

    async def test_an_address_that_is_not_http_is_not_logged(self, tmp_path):
        await QueryLogger(str(tmp_path)).log_event(
            event_type="message_received",
            session_id="s1",
            page_url="not a url?token=abc",
            page_context={"type": "general", "url": "javascript:alert(1)"},
        )
        (entry,) = _entries(tmp_path)
        assert "page_url" not in entry  # None values are not written
        assert entry["page_context"]["url"] is None  # a nested None is written as null: nothing of the value is kept

    async def test_a_context_without_a_url_is_logged_as_it_was(self, tmp_path):
        await QueryLogger(str(tmp_path)).log_event(
            event_type="message_received", session_id="s1", page_context={"type": "general", "id": None}
        )
        (entry,) = _entries(tmp_path)
        assert entry["page_context"] == {"type": "general", "id": None}


class TestSlackHandoff:
    async def _handoff_blocks(self, page_context):
        service = SlackService()
        service._web_client = MagicMock()
        service._web_client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        service._get_support_channel_id = AsyncMock(return_value="C1")
        await service.create_handoff_thread("s1", page_context, "I want a person", [])
        return json.dumps(service._web_client.chat_postMessage.call_args.kwargs["blocks"])

    async def test_the_page_link_has_no_query_fragment_or_credentials(self):
        blocks = await self._handoff_blocks({"type": "bill", "title": "A Bill", "id": "HB 219", "url": FULL})
        assert f"<{SAFE}|A Bill>" in blocks
        assert "secret" not in blocks and "abc123" not in blocks and "zzz" not in blocks

    async def test_a_url_that_is_not_an_address_gives_a_page_line_with_no_link(self):
        blocks = await self._handoff_blocks({"type": "bill", "title": "A Bill", "id": "HB 219", "url": "javascript:alert(1)"})
        assert "*Page:* A Bill (HB 219)" in blocks and "javascript" not in blocks

    async def test_a_context_with_no_url_is_as_before(self):
        blocks = await self._handoff_blocks({"type": "bill", "title": "A Bill", "id": "HB 219"})
        assert "*Page:* A Bill (HB 219)" in blocks
