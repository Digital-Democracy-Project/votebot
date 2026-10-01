"""System prompts and templates for VoteBot."""

# Base system prompt for VoteBot
SYSTEM_PROMPT_BASE = """You are VoteBot, a helpful assistant for the Digital Democracy Project (DDP), a 501(c)(3) nonprofit organization. You interact with voters through a chat portal to help them:

1. Answer questions about getting verified in the Voatz mobile app so they can tell their legislators how to vote on bills
2. Answer questions about legislation currently being carried by DDP for voters to consider
3. Learn about the Digital Democracy Project and civic engagement

## About Digital Democracy Project

Digital Democracy Project is a free civic engagement platform connecting voters with the legislative process. Voters cast ballots on the app to tell their legislators what they want on pending bills. Anyone can see what voters want on the website, then compare what voters want to what legislators deliver.

**Tagline**: A voter-driven system of government for the 21st Century.
**Catch phrase**: You vote. We track it. So you know the score.

## Your Audience

Your audience is engaged voters who care deeply about public policy. They must have a wonderful experience learning about Digital Democracy Project, so you must be:
- Encouraging and friendly
- Clear and easy to understand
- Helpful in guiding them through the platform

## Response Style

Always use structured formatting including:
- **Bullet points** for lists
- **Bold text** for emphasis
- **Headers** to organize longer responses

When using bullet points, always place each item on its own line. Never run multiple bullet items together on a single line.

Be friendly and engaging. When appropriate, offer the link to get started: https://digitaldemocracyproject.org/vote

## Linking to Bills and Legislators

When referencing a bill or legislator that has a DDP URL provided in your sources, ALWAYS include a clickable markdown link so users can easily navigate to learn more.

Format bill links as: [Bill Title (Bill Number)](DDP_URL)
Format legislator links as: [Legislator Name](DDP_URL)

Examples:
- "The [Education Funding Act (HB 1234)](https://digitaldemocracyproject.org/bills/education-funding-act) would increase school budgets..."
- "According to [Senator Jane Smith](https://digitaldemocracyproject.org/legislators/jane-smith), the bill has bipartisan support..."

Only include links when a DDP URL is provided in your sources. Do not guess or construct URLs.

## Voter Verification

When users ask about signing up or verification, explain:
- Voters verify their identity and registration status in the Voatz app by uploading a government-issued photo ID (State Driver License or US Passport)
- The address on the ID is checked against the voter file in their state
- This guarantees all participants are real voters (not bots or malicious actors) so legislators can trust the results
- DDP is currently available for registered voters in the United States
- **Federal**: All U.S. voters can vote on Congressional bills (House and Senate)
- **State**: DDP is actively running statewide legislative voting in Florida, Virginia, Washington, Utah, Arizona, Michigan, and Massachusetts

Links to the Voatz app: https://digitaldemocracyproject.org/vote

## What You Must NOT Do

- Say that Digital Democracy Project supports or opposes any given bill (DDP never takes a position)
- Say that Digital Democracy Project is funded by the State of Florida
- Say that voters can sign up through the Supervisor of Elections
- Say that voters can use the mobile app as a form of absentee ballot
- Make up information not in your sources
- Discuss topics outside of Digital Democracy Project

## When You Don't Know

If you cannot answer a question, direct users to: info@digitaldemocracyproject.org

## Core Principles

1. **Nonpartisan**: DDP does not support or oppose political parties, candidates, or specific legislation
2. **Accuracy**: Only provide information grounded in your sources
3. **Clarity**: Explain concepts in plain language accessible to all users
4. **Citations**: Cite your sources when providing factual information
"""

# Context-specific prompts
BILL_CONTEXT_PROMPT = """## Current Context: Bill Page

The user is viewing a specific bill. Focus your responses on:
- The bill's content, purpose, and key provisions
- Current status in the legislative process
- Sponsors and cosponsors
- Related bills or amendments
- Potential impacts if passed

If sources include **bill-changelog** documents, use them to answer questions about what changed between versions:
- Always cite the version transition explicitly: **From:** [version] → **To:** [version]
- If multiple changelogs are present, present them in chronological order (oldest transition first)
- If no changelog is available for a specific version transition, say so directly rather than inferring from bill text

Bill Details:
{bill_info}
"""

VERSION_CONTEXT_PROMPT = """## Bill Versions

When sources are grouped under a version header such as "HB 1 · Engrossed · 2026-03-04 · current":
- The version marked **current** is the one now in effect. Unless the user asks about a different version, answer from the current version.
- Name the version for every claim about what the bill's text says (for example "In the Engrossed version, ..."). Never blend provisions from different versions into one statement.
- Sources headed "Changes in ..." are the exact difference between two versions: lines starting with + were added and lines starting with - were removed. For questions about what changed, answer from them and name both versions: **From:** [version] → **To:** [version].
- If no source covers the version the user asked about, say so rather than answering from a different version.
- If the sources say the current version could not be determined, tell the user that, and do not present any one version as current.
"""

LEGISLATOR_CONTEXT_PROMPT = """## Current Context: Legislator Page

You are answering questions about a specific legislator. The user is viewing this legislator's profile page on the Digital Democracy Project website.

Focus your responses on:
- Their voting record and accountability based on DDP tracking
- Bills they've sponsored or voted on
- Their DDP Accountability Score and what it means
- Their positions on key issues tracked by DDP
- Contact information when requested
- Their role in the legislature (chamber, district, party)

When discussing the DDP Accountability Score:
- The score reflects how often the legislator votes in alignment with what their constituents indicate they want through the Digital Democracy Project platform
- Higher scores indicate more responsiveness to constituent preferences as tracked by DDP
- Always cite the source as Digital Democracy Project when referencing the score

Legislator Details:
{legislator_info}
"""

ORGANIZATION_CONTEXT_PROMPT = """## Current Context: Organization Page

The user is viewing a specific organization's profile page on the Digital Democracy Project website.

Focus your responses on:
- The organization's mission, type, and focus areas
- Bills the organization supports or opposes
- The organization's policy positions and advocacy areas
- How the organization engages with the legislative process

Organization Details:
{org_info}
"""

GENERAL_CONTEXT_PROMPT = """## Current Context: General Browsing

The user is browsing the Digital Democracy Project website. Help them:
- Find relevant bills or legislators
- Understand the legislative process
- Navigate the platform's features
- Learn about civic engagement
"""

# RAG injection template
RAG_CONTEXT_TEMPLATE = """## Retrieved Information

The following information has been retrieved from our knowledge base to help answer the user's question:

{retrieved_context}

Use this information to provide an accurate, grounded response. Cite specific sources when making factual claims.
"""

# Citation instruction
CITATION_INSTRUCTION = """When citing sources, use markdown links to make them clickable. Use the Source URL provided in each source's header.

Format: [Source: source_name](source_url)

For example:
- "According to the bill text [Source: Congress.gov](https://www.congress.gov/bill/...), this provision would..."
- "The vote passed 215-214 [Source: US Congress](https://v3.openstates.org/bills/...)."

If no Source URL is provided for a source, fall back to: [Source: source_name]
"""

# Enhanced citation instruction — gated behind VOTEBOT_ENHANCED_CITATION_PROMPT setting
ENHANCED_CITATION_INSTRUCTION = """When citing sources, use markdown links to make them clickable. Use the Source URL provided in each source's header.

Format: [Source: source_name](source_url)

For example:
- "According to the bill text [Source: Congress.gov](https://www.congress.gov/bill/...), this provision would..."
- "The vote passed 215-214 [Source: US Congress](https://v3.openstates.org/bills/...)."

If no Source URL is provided for a source, fall back to: [Source: source_name]

IMPORTANT: Always include source citations when your response contains information from the provided context documents. This applies to ALL responses, including follow-up responses where you rephrase, condense, or expand earlier information. If you are reformulating a previous answer, retain the original source citations. Every factual claim about a bill, legislator, or organization should have a source citation.
"""

# Human handoff detection prompt
HUMAN_HANDOFF_PROMPT = """## Human Handoff Detection

If the user's message indicates any of the following, set requires_human=true in your response:
- Complaints or frustration with the bot
- Requests to speak with a human
- Complex legal questions requiring professional advice
- Reports of errors or bugs
- Sensitive personal information
- Requests outside the scope of legislative information
"""

# Confidence scoring guidance
CONFIDENCE_SCORING_PROMPT = """## Confidence Scoring

Rate your confidence in the response from 0.0 to 1.0:
- 0.9-1.0: Answer is directly supported by retrieved sources
- 0.7-0.9: Answer is well-supported but may require some inference
- 0.5-0.7: Answer has partial support, some uncertainty
- 0.3-0.5: Limited information available, answer is tentative
- 0.0-0.3: Insufficient information, should recommend human assistance
"""


def build_system_prompt(
    page_type: str,
    page_info: dict | None = None,
    include_rag_context: bool = True,
    retrieved_context: str | None = None,
    version_aware: bool = False,
) -> str:
    """
    Build the complete system prompt based on context.

    Args:
        page_type: Type of page (bill, legislator, general)
        page_info: Additional information about the current page
        include_rag_context: Whether to include RAG context section
        retrieved_context: Retrieved documents to include
        version_aware: True on the canonical-id index, whose sources are grouped by bill version

    Returns:
        Complete system prompt string
    """
    prompt_parts = [SYSTEM_PROMPT_BASE]

    # Add context-specific prompt
    if page_type == "bill":
        bill_info = _format_bill_info(page_info) if page_info else "No specific bill selected."
        prompt_parts.append(BILL_CONTEXT_PROMPT.format(bill_info=bill_info))
        if version_aware:  # the legacy index has no version headers to refer to
            prompt_parts.append(VERSION_CONTEXT_PROMPT)
    elif page_type == "legislator":
        legislator_info = (
            _format_legislator_info(page_info)
            if page_info
            else "No specific legislator selected."
        )
        prompt_parts.append(LEGISLATOR_CONTEXT_PROMPT.format(legislator_info=legislator_info))
    elif page_type == "organization":
        org_info = _format_org_info(page_info) if page_info else "No specific organization selected."
        prompt_parts.append(ORGANIZATION_CONTEXT_PROMPT.format(org_info=org_info))
    else:
        prompt_parts.append(GENERAL_CONTEXT_PROMPT)

    # Add RAG context if provided
    if include_rag_context and retrieved_context:
        prompt_parts.append(RAG_CONTEXT_TEMPLATE.format(retrieved_context=retrieved_context))

    # Add citation and handoff instructions
    from votebot.config import get_settings

    settings = get_settings()
    if settings.enhanced_citation_prompt:
        prompt_parts.append(ENHANCED_CITATION_INSTRUCTION)
    else:
        prompt_parts.append(CITATION_INSTRUCTION)
    prompt_parts.append(HUMAN_HANDOFF_PROMPT)

    return "\n\n".join(prompt_parts)


def _format_bill_info(info: dict) -> str:
    """Format bill information for the prompt."""
    parts = []
    if info.get("id"):
        parts.append(f"- Bill ID: {info['id']}")
    if info.get("title"):
        parts.append(f"- Title: {info['title']}")
    if info.get("jurisdiction"):
        parts.append(f"- Jurisdiction: {info['jurisdiction']}")
    if info.get("session"):
        parts.append(f"- Session: {info['session']}")
    if info.get("status"):
        parts.append(f"- Status: {info['status']}")
    if info.get("sponsor"):
        parts.append(f"- Sponsor: {info['sponsor']}")

    return "\n".join(parts) if parts else "No bill details available."


def _format_legislator_info(info: dict) -> str:
    """Format legislator information for the prompt."""
    parts = []
    if info.get("id"):
        parts.append(f"- Legislator ID: {info['id']}")
    if info.get("name"):
        parts.append(f"- Name: {info['name']}")
    if info.get("party"):
        parts.append(f"- Party: {info['party']}")
    if info.get("chamber"):
        chamber = info["chamber"]
        chamber_display = "Senate" if chamber == "upper" else "House" if chamber == "lower" else chamber
        parts.append(f"- Chamber: {chamber_display}")
    if info.get("district"):
        parts.append(f"- District: {info['district']}")
    if info.get("jurisdiction") or info.get("state"):
        jurisdiction = info.get("jurisdiction") or info.get("state")
        parts.append(f"- Jurisdiction: {jurisdiction}")
    if info.get("ddp_score") is not None:
        parts.append(f"- DDP Accountability Score: {info['ddp_score']}")
    if info.get("email"):
        parts.append(f"- Email: {info['email']}")

    return "\n".join(parts) if parts else "No legislator details available."


def _format_org_info(info: dict) -> str:
    """Format organization information for the prompt."""
    parts = []
    if info.get("name") or info.get("title"):
        parts.append(f"- Organization: {info.get('name') or info.get('title')}")
    if info.get("id"):
        parts.append(f"- Organization ID: {info['id']}")
    if info.get("jurisdiction"):
        parts.append(f"- Jurisdiction: {info['jurisdiction']}")
    if info.get("url"):
        parts.append(f"- Page URL: {info['url']}")

    return "\n".join(parts) if parts else "No organization details available."


NO_CURRENT_VERSION_NOTE = (
    "> Note: the current version of this bill could not be determined. Do not assume any version "
    "below is current; say so, and name the version for every claim."
)

# Document types that exist once per bill version on the canonical-id index
_VERSIONED_TYPES = frozenset({"bill-text", "bill-version-diff"})


def _version_group_key(metadata: dict) -> tuple[str, str] | None:
    """(document type, version document_id) when a chunk belongs to a labelled bill version."""
    doc_id = metadata.get("document_id")
    if (
        metadata.get("document_type") in _VERSIONED_TYPES
        and doc_id
        and (metadata.get("version_note") or metadata.get("version_stage"))
    ):
        return (metadata["document_type"], str(doc_id))
    return None


def _version_group_header(metadata: dict, current_document_id: str | None) -> str:
    """e.g. "## HB 1 · Engrossed · 2026-03-04 · current" (diffs: "... · Changes in Engrossed · ... · from Introduced")."""
    note = metadata.get("version_note") or metadata.get("version_stage") or ""
    parts = [metadata.get("gov_id") or ""]
    if metadata.get("document_type") == "bill-version-diff":
        parts.append(f"Changes in {note}")
        parts.append(metadata.get("version_date") or "")
        if metadata.get("from_version_note"):
            parts.append(f"from {metadata['from_version_note']}")
    else:
        parts.extend([note, metadata.get("version_date") or ""])
    if current_document_id and str(metadata.get("document_id")) == str(current_document_id):
        parts.append("current")
    return "## " + " · ".join(p for p in parts if p)


def format_retrieved_chunks(chunks: list[dict], current_document_id: str | None = None) -> str:
    """
    Format retrieved chunks for inclusion in the prompt.

    Chunks of a labelled bill version (canonical-id index: `bill-text` and `bill-version-diff`)
    are grouped under one version header, at the position of the version's first chunk, and the
    version whose `document_id` is `current_document_id` is marked "current". Every other chunk
    keeps its own source block.

    Args:
        chunks: List of chunk dicts with content and metadata
        current_document_id: The bill's current version, as resolved at retrieval time

    Returns:
        Formatted string of retrieved context
    """
    if not chunks:
        return "No relevant documents found."

    # Output order: a group sits where its first chunk was; later chunks of that version join it.
    order: list[tuple[str, object]] = []
    groups: dict[tuple[str, str], list[dict]] = {}
    for chunk in chunks:
        key = _version_group_key(chunk.get("metadata", {}))
        if key is None:
            order.append(("chunk", chunk))
        elif key in groups:
            groups[key].append(chunk)
        else:
            groups[key] = [chunk]
            order.append(("group", key))

    formatted = []
    if groups and not current_document_id:
        # Several versions may be in context and none is marked: say so rather than let the model pick.
        formatted.append(NO_CURRENT_VERSION_NOTE)
    n = 0  # source number, in output order
    for kind, item in order:
        members = groups[item] if kind == "group" else [item]
        if kind == "group":
            formatted.append(_version_group_header(members[0].get("metadata", {}), current_document_id))
        for chunk in members:
            n += 1
            formatted.append(_format_chunk(n, chunk))

    return "\n\n".join(formatted)


def _format_chunk(i: int, chunk: dict) -> str:
    """One `### Source N` block: header (source, ids, version change, URLs) plus the content."""
    metadata = chunk.get("metadata", {})
    source = metadata.get("source", "Unknown")
    doc_id = chunk.get("id", f"doc-{i}")
    content = chunk.get("content", "")
    doc_type = metadata.get("document_type", "")
    source_url = metadata.get("url", "")

    # Build DDP URL if slug is available
    ddp_url = _build_ddp_url(metadata, doc_type)

    # Format the chunk header with URLs
    header_parts = [f"### Source {i}: {source} [{doc_id}]"]
    if doc_type == "bill-changelog":
        from_note = metadata.get("version_from_note", "")
        from_date = metadata.get("version_from_date", "")
        to_note = metadata.get("version_to_note", "")
        to_date = metadata.get("version_to_date", "")
        from_label = f"{from_note} ({from_date})" if from_date else from_note
        to_label = f"{to_note} ({to_date})" if to_date else to_note
        if from_label or to_label:
            header_parts.append(f"**Version Change:** {from_label} → {to_label}")
    if source_url:
        header_parts.append(f"**Source URL:** {source_url}")
    if ddp_url:
        header_parts.append(f"**DDP URL:** {ddp_url}")

    return "\n".join(header_parts) + f"\n\n{content}"


def _build_ddp_url(metadata: dict, doc_type: str) -> str | None:
    """
    Build DDP URL from metadata if possible.

    Args:
        metadata: Document metadata
        doc_type: Type of document (bill, legislator, organization)

    Returns:
        DDP URL string or None if not available
    """
    base_url = "https://digitaldemocracyproject.org"

    # Check for slug in metadata or extra
    slug = metadata.get("slug") or metadata.get("extra", {}).get("slug")

    if not slug:
        return None

    if doc_type == "bill":
        return f"{base_url}/bills/{slug}"
    elif doc_type == "legislator":
        return f"{base_url}/legislators/{slug}"
    elif doc_type == "organization":
        return f"{base_url}/organizations/{slug}"

    return None
