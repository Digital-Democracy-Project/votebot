"""Intent classification for user queries.

Provides two-level taxonomy (primary_intent + sub_intent) for analytics.
Both levels use lightweight keyword/regex heuristics, not ML classifiers.

IMPORTANT: Do not add new intent values casually — taxonomy creep degrades
analytics consistency. New values require a deliberate decision.
"""

import re
from enum import StrEnum


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
    # out_of_scope
    GREETING = "greeting"
    OFF_TOPIC = "off_topic"
    META = "meta"
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

# Sub-intent keyword maps per primary intent
_BILL_SUB_KEYWORDS: dict[str, list[str]] = {
    "vote_history": ["vote", "voted", "voting", "yea", "nay", "roll call", "tally"],
    "support_opposition": [
        "support", "oppose", "position", "stance", "for or against",
        "who supports", "who opposes", "backed", "endorses",
    ],
    "status": ["status", "passed", "failed", "committee", "signed", "vetoed", "introduced", "referred"],
    "explanation": ["explain", "what does", "what is", "mean", "means", "rephrase", "simpler", "plain language"],
    "comparison": ["compare", "difference", "vs", "versus", "similar"],
    "summary": ["summary", "summarize", "overview", "about", "what is this bill"],
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
    "navigation": ["where", "find", "navigate", "page", "link", "go to"],
    "how_to_vote": ["vote", "register", "ballot", "how do i vote", "cast"],
    "about_ddp": ["ddp", "digital democracy", "votebot", "this site", "this platform"],
    "issue_area": ["issue", "topic", "policy", "immigration", "healthcare", "education", "environment"],
}

_OUT_OF_SCOPE_SUB_KEYWORDS: dict[str, list[str]] = {
    "greeting": ["hello", "hi ", "hey ", "good morning", "good afternoon"],
    "off_topic": ["weather", "recipe", "joke", "sports", "movie"],
    "meta": ["thanks", "thank you", "bye", "goodbye", "ok", "great"],
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
