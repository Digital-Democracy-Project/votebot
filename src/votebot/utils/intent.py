"""Intent classification for user queries.

Provides two-level taxonomy (primary_intent + sub_intent) for analytics.
Both levels use lightweight keyword/regex heuristics, not ML classifiers.

IMPORTANT: Do not add new intent values casually — taxonomy creep degrades
analytics consistency. New values require a deliberate decision.
"""

import re
import sys
from dataclasses import dataclass
from datetime import datetime

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):
        """Backport of StrEnum for Python < 3.11."""
        pass


# ---------------------------------------------------------------------------
# Primary intent — entity-level classification
# ---------------------------------------------------------------------------

class PrimaryIntent(StrEnum):
    BILL = "bill"
    LEGISLATOR = "legislator"
    ORGANIZATION = "organization"
    GENERAL = "general"
    OUT_OF_SCOPE = "out_of_scope"


# ---------------------------------------------------------------------------
# Sub intent — action-level classification within each primary
# ---------------------------------------------------------------------------

class SubIntent(StrEnum):
    # bill
    SUMMARY = "summary"
    SUPPORT_OPPOSITION = "support_opposition"
    VOTE_HISTORY = "vote_history"
    STATUS = "status"
    EXPLANATION = "explanation"
    COMPARISON = "comparison"
    CHANGELOG = "changelog"
    # legislator
    VOTING_RECORD = "voting_record"
    CONTACT = "contact"
    BIO = "bio"
    DDP_SCORE = "ddp_score"
    SPONSORED_BILLS = "sponsored_bills"
    # organization
    POSITIONS = "positions"
    INFO = "info"
    BILL_ALIGNMENT = "bill_alignment"
    # general
    NAVIGATION = "navigation"
    HOW_TO_VOTE = "how_to_vote"
    ABOUT_DDP = "about_ddp"
    ISSUE_AREA = "issue_area"
    # cross-primary — text refinement requests
    TEXT_EDITING = "text_editing"
    # out_of_scope
    GREETING = "greeting"
    OFF_TOPIC = "off_topic"
    META = "meta"
    # bill — civic engagement
    CIVIC_ACTION = "civic_action"
    # fallback
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Controlled vocabulary for retrieval source document types
# ---------------------------------------------------------------------------

VALID_RETRIEVAL_SOURCES = frozenset({
    "bill",
    "bill-text",
    "bill-history",
    "bill-votes",
    "bill-changelog",
    "bill-text-history",
    "bill-version-diff",
    "legislator",
    "legislator-votes",
    "organization",
    "training",
})


# ---------------------------------------------------------------------------
# Classification patterns
# ---------------------------------------------------------------------------

_BILL_PATTERN = re.compile(
    r"\b(HB|SB|HR|S|HJ|SJ|HCR|SCR|HJR|SJR)\s*\d+", re.IGNORECASE
)

_ORG_KEYWORDS = [
    "organization", "organizations", "org ", "who supports", "who opposes",
    "which groups", "support", "oppose", "backed", "endorses",
]

_LEGISLATOR_KEYWORDS = [
    "senator", "representative", "legislator", "congress",
    "voted", "vote", "sponsor", "cosponsor",
]

_OUT_OF_SCOPE_KEYWORDS = [
    "weather", "recipe", "joke", "hello", "hi ", "hey ",
    "thanks", "thank you", "bye", "goodbye",
]

# Canonical changelog keyword list — single source of truth.
# Used for SubIntent.CHANGELOG classification AND imported by retrieval.py
# for Phase 5 trigger detection. "amendment"/"amended" are intentionally
# excluded here (they remain in the "status" sub-intent for analytics) but
# retrieval.py extends this list with those terms for broader recall.
CHANGELOG_KEYWORDS: list[str] = [
    "what changed", "what's changed", "what has changed",
    "how has", "how it changed",
    "what was added", "what was removed", "what was modified",
    "compare version", "different version", "difference", "differences",
    "between versions", "updated since", "revision",
    "new version", "previous version", "old version", "what's new in",
]

# ---------------------------------------------------------------------------
# Version requests (VOTEBOT-10) — "as introduced", "the engrossed version", a date
# ---------------------------------------------------------------------------

# How people name a bill version, mapped to api-v3's `version_stage` labels
# (``version_ordering.note_stage``: introduced, amendment, chamber_passage, final_passage,
# enacted). Specific phrases only: a bare "introduced" or "amended" is usually a status question
# ("when was it introduced?"), not a request for that version's text.
VERSION_STAGE_KEYWORDS: dict[str, list[str]] = {
    "introduced": ["as introduced", "introduced version", "original version", "as filed", "version as filed"],
    "amendment": ["amended version", "substitute version", "committee substitute"],
    "chamber_passage": [
        "engrossed", "as passed the house", "as passed the senate",
        "house-passed version", "senate-passed version",
    ],
    "final_passage": ["enrolled", "sent to the governor"],
    "enacted": ["as enacted", "enacted version", "signed into law", "chaptered"],
}

_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_WRITTEN_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class VersionRequest:
    """Which bill versions a query asks about. Empty means "no particular version"."""

    stages: tuple[str, ...] = ()  # api-v3 `version_stage` labels
    dates: tuple[str, ...] = ()  # ISO dates, matched against a version's `version_date`

    def __bool__(self) -> bool:
        return bool(self.stages or self.dates)


def detect_version_request(query: str) -> VersionRequest:
    """Stages and dates a query names; anything else leaves retrieval on the current version."""
    lowered = query.lower()
    stages = tuple(
        stage
        for stage, phrases in VERSION_STAGE_KEYWORDS.items()
        if any(phrase in lowered for phrase in phrases)
    )
    dates = list(_ISO_DATE.findall(query))
    for month, day, year in _WRITTEN_DATE.findall(query):
        try:
            dates.append(datetime.strptime(f"{month[:3].title()} {int(day)} {year}", "%b %d %Y").date().isoformat())
        except ValueError:
            continue  # "Feb 31": not a date, so not a request
    return VersionRequest(stages=stages, dates=tuple(dict.fromkeys(dates)))


# Sub-intent keyword maps per primary intent
_BILL_SUB_KEYWORDS: dict[str, list[str]] = {
    "changelog": CHANGELOG_KEYWORDS,
    "text_editing": [
        "concise", "trim", "shorten", "rewrite",
        "make it shorter", "make it longer", "make it more concise",
        "fewer words", "more words", "a few more words", "one more word",
        "slightly more", "slightly less", "add back",
        "more detail", "less detail", "expand", "paragraph",
    ],
    "vote_history": [
        "vote", "voted", "voting", "yea", "nay", "roll call", "tally",
        "passed the house", "passed the senate",
    ],
    "support_opposition": [
        "support", "oppose", "position", "stance", "for or against",
        "who supports", "who opposes", "backed", "endorses",
        "pros and cons", "pros", "cons", "benefits", "drawbacks",
        "arguments for", "arguments against", "advantages", "disadvantages",
    ],
    "status": [
        "status", "passed", "failed", "committee", "signed", "vetoed",
        "introduced", "referred", "latest action", "latest status",
        "what happened", "current status", "where is this bill",
        "sponsor", "sponsoring", "cosponsor", "cosponsoring",
        "amended", "amendment",
    ],
    "explanation": [
        "explain", "what does", "what is", "mean", "means",
        "rephrase", "simpler", "plain language",
        "help me understand", "break down", "what are these",
        "referenced in", "specific sections",
        "check your sources", "recheck", "bill text",
        "acronym",
    ],
    "comparison": ["compare", "difference", "vs", "versus", "similar"],
    "summary": [
        "summary", "summarize", "overview", "about", "what is this bill",
        "tell me about", "make it",
    ],
    "civic_action": [
        "email", "letter", "contact", "write to", "call my",
        "tell my representative", "tell my senator",
    ],
}

_LEGISLATOR_SUB_KEYWORDS: dict[str, list[str]] = {
    "voting_record": ["vote", "voted", "voting", "record", "roll call"],
    "contact": ["contact", "email", "phone", "office", "address", "reach"],
    "bio": ["bio", "background", "who is", "about"],
    "ddp_score": ["score", "ddp score", "rating"],
    "sponsored_bills": ["sponsor", "authored", "introduced", "bills"],
}

_ORG_SUB_KEYWORDS: dict[str, list[str]] = {
    "positions": ["position", "stance", "support", "oppose", "for or against"],
    "bill_alignment": ["align", "bills", "legislation", "legislative"],
    "info": ["about", "what is", "who is", "info", "information", "describe"],
}

_GENERAL_SUB_KEYWORDS: dict[str, list[str]] = {
    "text_editing": [
        "concise", "trim", "shorten", "rewrite",
        "make it shorter", "make it longer", "make it more concise",
        "fewer words", "more words", "a few more words", "one more word",
        "slightly more", "slightly less", "add back",
        "more detail", "less detail", "expand", "paragraph",
    ],
    "navigation": ["where", "find", "navigate", "page", "link", "go to"],
    "how_to_vote": ["vote", "register", "ballot", "how do i vote", "cast"],
    "about_ddp": ["ddp", "digital democracy", "votebot", "this site", "this platform"],
    "issue_area": [
        "issue", "topic", "policy", "immigration", "healthcare",
        "education", "environment",
    ],
}

_OUT_OF_SCOPE_SUB_KEYWORDS: dict[str, list[str]] = {
    "greeting": ["hello", "hi ", "hey ", "good morning", "good afternoon"],
    "off_topic": ["weather", "recipe", "joke", "sports", "movie"],
    "meta": [
        "thanks", "thank you", "bye", "goodbye", "ok", "great",
        "check your sources", "recheck",
    ],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_primary_intent(page_type: str, message: str) -> str:
    """Classify the primary intent of a query.

    Args:
        page_type: The page context type ("bill", "legislator", "organization", "general").
        message: The user's message text.

    Returns:
        One of the PrimaryIntent values.
    """
    # Page context is the strongest signal
    if page_type == "bill":
        return PrimaryIntent.BILL
    if page_type == "organization":
        return PrimaryIntent.ORGANIZATION
    if page_type == "legislator":
        return PrimaryIntent.LEGISLATOR

    message_lower = message.lower()

    # Fall back to message content analysis
    if _BILL_PATTERN.search(message_lower):
        return PrimaryIntent.BILL

    if any(kw in message_lower for kw in _ORG_KEYWORDS):
        return PrimaryIntent.ORGANIZATION

    if any(kw in message_lower for kw in _LEGISLATOR_KEYWORDS):
        return PrimaryIntent.LEGISLATOR

    if any(kw in message_lower for kw in _OUT_OF_SCOPE_KEYWORDS):
        return PrimaryIntent.OUT_OF_SCOPE

    return PrimaryIntent.GENERAL


def classify_sub_intent(primary_intent: str, message: str) -> str:
    """Classify the sub-intent within a primary intent category.

    Args:
        primary_intent: The primary intent (from classify_primary_intent).
        message: The user's message text.

    Returns:
        One of the SubIntent values, or "unknown" if no match.
    """
    message_lower = message.lower()

    keyword_map: dict[str, list[str]]
    if primary_intent == PrimaryIntent.BILL:
        keyword_map = _BILL_SUB_KEYWORDS
    elif primary_intent == PrimaryIntent.LEGISLATOR:
        keyword_map = _LEGISLATOR_SUB_KEYWORDS
    elif primary_intent == PrimaryIntent.ORGANIZATION:
        keyword_map = _ORG_SUB_KEYWORDS
    elif primary_intent == PrimaryIntent.GENERAL:
        keyword_map = _GENERAL_SUB_KEYWORDS
    elif primary_intent == PrimaryIntent.OUT_OF_SCOPE:
        keyword_map = _OUT_OF_SCOPE_SUB_KEYWORDS
    else:
        return SubIntent.UNKNOWN

    for sub_intent, keywords in keyword_map.items():
        if any(kw in message_lower for kw in keywords):
            return sub_intent

    return SubIntent.UNKNOWN


def normalize_retrieval_sources(raw_sources: set[str]) -> list[str]:
    """Normalize retrieval source document types to the controlled vocabulary.

    Unknown values are mapped to "unknown" and logged as warnings.

    Args:
        raw_sources: Set of document_type values from retrieval chunks.

    Returns:
        Sorted list of normalized source types.
    """
    import structlog
    logger = structlog.get_logger()

    normalized = set()
    for src in raw_sources:
        if src in VALID_RETRIEVAL_SOURCES:
            normalized.add(src)
        else:
            logger.warning("Unknown retrieval source document_type", document_type=src)
            normalized.add("unknown")

    return sorted(normalized)
