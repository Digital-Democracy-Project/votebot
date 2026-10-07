"""Citations for an answer the model wrote without a `[Source: ...]` (VOTEBOT-21).

`VoteBotAgent._extract_citations` only returns a citation when the model writes an explicit
source link, which it does only some of the time (about one answer in three on a bill page had
none, with ten chunks retrieved and used). When it found none, `chunks_used_by` looks at what the
answer actually says and returns the retrieved chunks it was built from.

The caller offers it only the page's own chunks (those carrying the identity retrieval was pinned to);
this module just compares text. "Built from" is a text check, not a relevance score: the answer must share distinctive words with
the chunk. A relevance score alone would cite sources for "thanks!" on a bill page (its chunks are
all about that bill), and the page's own title and number are already in the prompt, so they are
left out of the comparison. No model call, no extra latency.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from votebot.services.vector_store import SearchResult

# A word counts only if it is this long: shorter ones ("bill", "state", "which") are everywhere.
MIN_WORD_LENGTH = 6
# Distinctive words an answer and a chunk must share for the chunk to count as used.
MIN_SHARED_WORDS = 4
# Citations returned at most, strongest first.
MAX_CITATIONS = 3

_WORD = re.compile(rf"[a-z]{{{MIN_WORD_LENGTH},}}")

# Long words that say nothing about which text an answer came from.
_GENERIC = frozenset(
    {
        "about", "above", "according", "across", "additional", "against", "already", "always", "another",
        "because", "before", "being", "between", "could", "current", "different", "during", "either",
        "further", "general", "however", "include", "included", "includes", "including", "information",
        "legislation", "legislative", "provide", "provided", "provides", "provision", "provisions",
        "question", "section", "sections", "should", "specific", "support", "through", "version",
        "versions", "within", "without", "would",
    }
)


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower())) - _GENERIC


def chunks_used_by(
    answer: str,
    chunks: Iterable[SearchResult],
    page_text: str = "",
) -> list[SearchResult]:
    """The retrieved chunks the answer was built from: best match first, at most MAX_CITATIONS.

    Args:
        answer: The model's answer.
        chunks: The chunks retrieved for the question.
        page_text: The page's own title, number and jurisdiction (already in the prompt, so an
            answer that merely repeats them proves nothing about the chunks).
    """
    answer_words = _words(answer) - _words(page_text)
    if len(answer_words) < MIN_SHARED_WORDS:
        return []

    scored = []
    for chunk in chunks:
        shared = len(answer_words & _words(chunk.content))
        if shared >= MIN_SHARED_WORDS:
            scored.append((shared, chunk.score or 0.0, chunk))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    used: list[SearchResult] = []
    seen: set[tuple] = set()
    for _, _, chunk in scored:
        # One citation per source and version: ten chunks of one bill text are one source
        key = (chunk.metadata.get("url") or chunk.metadata.get("source"), chunk.metadata.get("document_id"))
        if key in seen:
            continue
        seen.add(key)
        used.append(chunk)
        if len(used) == MAX_CITATIONS:
            break
    return used
