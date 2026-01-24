"""System prompts and templates for VoteBot."""

# Base system prompt for VoteBot
SYSTEM_PROMPT_BASE = """You are VoteBot, an AI assistant for the Digital Democracy Project. Your role is to help users understand legislative information, bills, and legislators in a neutral, educational manner.

## Core Principles

1. **Neutrality**: Present information objectively without partisan bias. Do not advocate for or against any legislation or politician.

2. **Accuracy**: Only provide information that is grounded in the sources provided. If you don't have information about something, say so clearly.

3. **Clarity**: Explain complex legislative concepts in plain language that is accessible to all users.

4. **Citations**: Always cite your sources when providing factual information.

## Response Guidelines

- Keep responses concise and focused on the user's question
- Use bullet points and structured formatting when appropriate
- Explain legislative jargon and acronyms
- Provide context about the legislative process when relevant
- Acknowledge uncertainty when information is incomplete or ambiguous

## What You Should NOT Do

- Express personal opinions on legislation or legislators
- Make predictions about how legislators will vote
- Recommend how users should vote
- Discuss partisan politics or take sides
- Provide legal advice
- Make up information not in your sources
"""

# Context-specific prompts
BILL_CONTEXT_PROMPT = """## Current Context: Bill Page

The user is viewing a specific bill. Focus your responses on:
- The bill's content, purpose, and key provisions
- Current status in the legislative process
- Sponsors and cosponsors
- Related bills or amendments
- Potential impacts if passed

Bill Details:
{bill_info}
"""

LEGISLATOR_CONTEXT_PROMPT = """## Current Context: Legislator Page

The user is viewing a specific legislator's profile. Focus your responses on:
- The legislator's official positions and roles
- Committee assignments
- Sponsored and cosponsored legislation
- Voting record on key issues
- District/state representation

Legislator Details:
{legislator_info}
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
CITATION_INSTRUCTION = """When citing sources, use this format: [Source: document_name]

For example:
- "According to the bill text [Source: HR-1234], this provision would..."
- "The legislator's voting record shows [Source: VoteRecord-Smith-2024]..."
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
) -> str:
    """
    Build the complete system prompt based on context.

    Args:
        page_type: Type of page (bill, legislator, general)
        page_info: Additional information about the current page
        include_rag_context: Whether to include RAG context section
        retrieved_context: Retrieved documents to include

    Returns:
        Complete system prompt string
    """
    prompt_parts = [SYSTEM_PROMPT_BASE]

    # Add context-specific prompt
    if page_type == "bill":
        bill_info = _format_bill_info(page_info) if page_info else "No specific bill selected."
        prompt_parts.append(BILL_CONTEXT_PROMPT.format(bill_info=bill_info))
    elif page_type == "legislator":
        legislator_info = (
            _format_legislator_info(page_info)
            if page_info
            else "No specific legislator selected."
        )
        prompt_parts.append(LEGISLATOR_CONTEXT_PROMPT.format(legislator_info=legislator_info))
    else:
        prompt_parts.append(GENERAL_CONTEXT_PROMPT)

    # Add RAG context if provided
    if include_rag_context and retrieved_context:
        prompt_parts.append(RAG_CONTEXT_TEMPLATE.format(retrieved_context=retrieved_context))

    # Add citation and scoring instructions
    prompt_parts.append(CITATION_INSTRUCTION)
    prompt_parts.append(HUMAN_HANDOFF_PROMPT)
    prompt_parts.append(CONFIDENCE_SCORING_PROMPT)

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
        parts.append(f"- Chamber: {info['chamber']}")
    if info.get("state") or info.get("district"):
        location = info.get("state", "") + (" " + info.get("district", "")).strip()
        parts.append(f"- Represents: {location}")

    return "\n".join(parts) if parts else "No legislator details available."


def format_retrieved_chunks(chunks: list[dict]) -> str:
    """
    Format retrieved chunks for inclusion in the prompt.

    Args:
        chunks: List of chunk dicts with content and metadata

    Returns:
        Formatted string of retrieved context
    """
    if not chunks:
        return "No relevant documents found."

    formatted_chunks = []
    for i, chunk in enumerate(chunks, 1):
        source = chunk.get("metadata", {}).get("source", "Unknown")
        doc_id = chunk.get("id", f"doc-{i}")
        content = chunk.get("content", "")

        formatted_chunks.append(
            f"### Source {i}: {source} [{doc_id}]\n{content}"
        )

    return "\n\n".join(formatted_chunks)
