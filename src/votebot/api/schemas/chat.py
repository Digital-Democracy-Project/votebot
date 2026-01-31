"""Chat API request and response schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PageContext(BaseModel):
    """Context about the page where the chat is initiated."""

    type: Literal["bill", "legislator", "organization", "general"] = Field(
        ...,
        description="Type of page context",
    )
    id: str | None = Field(
        None,
        description="Identifier for the bill, legislator, or organization",
    )
    jurisdiction: str | None = Field(
        None,
        description="Jurisdiction code (e.g., 'US', 'CA', 'NY')",
    )
    title: str | None = Field(
        None,
        description="Title of the bill or name of the legislator",
    )
    url: str | None = Field(
        None,
        description="URL of the current page",
    )
    slug: str | None = Field(
        None,
        description="URL slug for the content item",
    )


class NavigationContext(BaseModel):
    """Context about user navigation patterns."""

    previous_pages: list[str] = Field(
        default_factory=list,
        description="List of previously visited page types",
    )
    time_on_page: int | None = Field(
        None,
        description="Time spent on current page in seconds",
    )
    scroll_depth: float | None = Field(
        None,
        ge=0,
        le=1,
        description="How far user has scrolled (0-1)",
    )


class ClientMetadata(BaseModel):
    """Metadata about the client making the request."""

    client_id: str | None = Field(
        None,
        description="Client application identifier",
    )
    client_version: str | None = Field(
        None,
        description="Client application version",
    )
    user_agent: str | None = Field(
        None,
        description="User agent string",
    )
    platform: str | None = Field(
        None,
        description="Platform identifier (e.g., 'brevo', 'web', 'mobile')",
    )


class ChatRequest(BaseModel):
    """Request schema for the chat endpoint."""

    message: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="User's message",
    )
    session_id: str = Field(
        ...,
        description="Unique session/conversation identifier",
    )
    human_active: bool = Field(
        default=False,
        description="Whether a human agent is currently handling this conversation",
    )
    page_context: PageContext = Field(
        ...,
        description="Context about the current page",
    )
    navigation_context: NavigationContext | None = Field(
        None,
        description="Optional navigation context for intent disambiguation",
    )
    client_metadata: ClientMetadata | None = Field(
        None,
        description="Optional client metadata for analytics",
    )
    conversation_history: list[dict] | None = Field(
        None,
        description="Optional previous messages in the conversation",
    )


class Citation(BaseModel):
    """Citation reference for a source used in the response."""

    source: str = Field(
        ...,
        description="Name or type of the source (e.g., 'Congress.gov', 'OpenStates')",
    )
    document_id: str = Field(
        ...,
        description="Unique identifier for the source document",
    )
    excerpt: str = Field(
        ...,
        description="Relevant excerpt from the source",
    )
    url: str | None = Field(
        None,
        description="URL to the source document",
    )
    relevance_score: float | None = Field(
        None,
        ge=0,
        le=1,
        description="How relevant this citation is to the response",
    )


class WebCitation(BaseModel):
    """Citation from web search results."""

    url: str = Field(..., description="URL of the web source")
    title: str = Field(..., description="Title of the web page")
    snippet: str | None = Field(None, description="Relevant snippet from the page")


class ResponseMetadata(BaseModel):
    """Metadata about the response generation."""

    model: str = Field(..., description="LLM model used for generation")
    tokens_used: int = Field(..., description="Total tokens consumed")
    retrieval_count: int = Field(
        ...,
        description="Number of documents retrieved",
    )
    latency_ms: int = Field(
        ...,
        description="Response generation time in milliseconds",
    )
    cached: bool = Field(
        default=False,
        description="Whether the response was served from cache",
    )


class ChatResponse(BaseModel):
    """Response schema for the chat endpoint."""

    response: str | None = Field(
        None,
        description="Bot's response message (null if human_active or suppressed)",
    )
    citations: list[Citation] = Field(
        default_factory=list,
        description="List of citations supporting the response",
    )
    confidence: float = Field(
        default=0.0,
        ge=0,
        le=1,
        description="Confidence score for the response (0-1)",
    )
    requires_human: bool = Field(
        default=False,
        description="Whether the query should be escalated to a human",
    )
    suppressed: bool = Field(
        default=False,
        description="Whether the response was suppressed (human_active=true)",
    )
    web_search_used: bool = Field(
        default=False,
        description="Whether web search was used to augment the response",
    )
    web_citations: list[WebCitation] = Field(
        default_factory=list,
        description="Citations from web search results",
    )
    metadata: ResponseMetadata | None = Field(
        None,
        description="Optional metadata about response generation",
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="Response timestamp",
    )


class StreamChunk(BaseModel):
    """Schema for streaming response chunks."""

    chunk: str = Field(..., description="Text chunk of the response")
    done: bool = Field(default=False, description="Whether this is the final chunk")
    citations: list[Citation] | None = Field(
        None,
        description="Citations (only sent with final chunk)",
    )
    metadata: ResponseMetadata | None = Field(
        None,
        description="Metadata (only sent with final chunk)",
    )
