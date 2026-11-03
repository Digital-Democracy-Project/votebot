"""VOTEBOT-74: the knowledge-base documents must not carry a hand-typed list of the states DDP runs voting in.

VOTEBOT-73 made the system prompt read the live states from the broker. The same list was still typed in two
documents under "RAG training docs" (ddp-sync can ingest them by hand): a list that goes stale and can be retrieved
as if it were current. These tests keep every document in that folder free of it.
"""

import re
from pathlib import Path

import pytest

DOCS_DIR = Path(__file__).resolve().parents[2] / "RAG training docs"
DOCS = sorted(DOCS_DIR.glob("*.txt"))

# The sentence that introduced the typed list, and the shape of the list itself: four or more bulleted state names
# in a row ("* Florida * Virginia * Washington ..."), however the file separates its lines (these files use \r).
INTRO = "actively running statewide legislative voting for"
BULLETED_STATES = re.compile(
    r"(?:[*•-]\s*(?:Florida|Virginia|Washington|Utah|Arizona|Michigan|Massachusetts|North Carolina|Alabama)\s*){4,}"
)


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", "replace")


def test_the_documents_folder_is_found_and_not_empty():
    assert DOCS, f"no .txt documents under {DOCS_DIR}"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_no_document_introduces_a_typed_list_of_live_states(path):
    assert INTRO not in _text(path)


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_no_document_lists_four_or_more_states_as_bullets(path):
    assert not BULLETED_STATES.search(_text(path))


def test_the_replacement_sentence_names_no_state_and_points_to_the_live_source():
    instructions = _text(DOCS_DIR / "VoteBot Agent Instructions.txt")
    assert "never list states from memory" in instructions
