"""Tests for _join_response_blocks — the SDK output_text whitespace fix."""

from unittest.mock import MagicMock

from votebot.services.llm import _join_response_blocks


def _make_response(blocks: list[str]) -> MagicMock:
    """Build a mock Responses API response with the given text blocks."""
    response = MagicMock()
    content_blocks = []
    for text in blocks:
        block = MagicMock()
        block.text = text
        content_blocks.append(block)
    item = MagicMock()
    item.content = content_blocks
    response.output = [item]
    return response


def test_single_block_returned_unchanged():
    r = _make_response(["Hello world."])
    assert _join_response_blocks(r) == "Hello world."


def test_blocks_with_existing_newline_not_doubled():
    """When blocks already carry their own whitespace, no extra newline inserted."""
    r = _make_response(["First paragraph.\n\n", "Second paragraph."])
    assert _join_response_blocks(r) == "First paragraph.\n\nSecond paragraph."


def test_missing_newline_inserted_at_boundary():
    """Core bug fix: period immediately followed by capital letter gets \\n\\n."""
    r = _make_response([
        "...fraud prevention measures in federal child care and nutrition programs.",
        "Potential Impact: The bill could reduce fraud.",
    ])
    result = _join_response_blocks(r)
    assert "programs.\n\nPotential Impact:" in result


def test_missing_newline_before_bold_header():
    """Bold markdown header after punctuation also gets \\n\\n."""
    r = _make_response(["Some sentence.", "**Key Point:** Details here."])
    result = _join_response_blocks(r)
    assert "sentence.\n\n**Key Point:**" in result


def test_blocks_joined_without_extra_whitespace_when_space_present():
    """When second block starts with a space, no extra newline added."""
    r = _make_response(["Hello", " world."])
    assert _join_response_blocks(r) == "Hello world."


def test_empty_response_returns_empty_string():
    r = _make_response([])
    assert _join_response_blocks(r) == ""


def test_falls_back_to_output_text_when_no_output_attr():
    """If response has no .output attribute, falls back to .output_text."""
    r = MagicMock(spec=["output_text"])
    r.output_text = "fallback text"
    assert _join_response_blocks(r) == "fallback text"


def test_multiple_missing_boundaries_all_fixed():
    """Multiple block boundaries without whitespace all get newlines."""
    r = _make_response(["First.", "Second.", "Third."])
    result = _join_response_blocks(r)
    assert result == "First.\n\nSecond.\n\nThird."
