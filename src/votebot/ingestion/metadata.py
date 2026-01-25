"""Metadata extraction for documents."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class DocumentMetadata:
    """Metadata for a document."""

    document_id: str
    document_type: str
    source: str
    title: str | None = None
    jurisdiction: str | None = None
    bill_id: str | None = None
    legislator_id: str | None = None
    effective_date: datetime | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage (Pinecone-compatible)."""
        result = {
            "document_id": self.document_id,
            "document_type": self.document_type,
            "source": self.source,
        }

        if self.title:
            result["title"] = self.title
        if self.jurisdiction:
            result["jurisdiction"] = self.jurisdiction
        if self.bill_id:
            result["bill_id"] = self.bill_id
        if self.legislator_id:
            result["legislator_id"] = self.legislator_id
        if self.effective_date:
            result["effective_date"] = self.effective_date.isoformat()
        if self.url:
            result["url"] = self.url

        result["created_at"] = self.created_at.isoformat()
        result["updated_at"] = self.updated_at.isoformat()

        # Add extra fields, filtering out None values and converting lists to strings
        # (Pinecone metadata doesn't support None or nested structures)
        for key, value in self.extra.items():
            if value is None:
                continue
            if isinstance(value, list):
                result[key] = ", ".join(str(v) for v in value)
            elif isinstance(value, dict):
                continue  # Skip nested dicts
            else:
                result[key] = value

        return result


class MetadataExtractor:
    """
    Extract and normalize metadata from various document types.

    Supports:
    - Bill documents (Congress.gov, OpenStates)
    - Legislator profiles
    - General web content
    - PDF documents
    """

    def __init__(self):
        """Initialize the metadata extractor."""
        # Jurisdiction code mapping
        self.jurisdiction_codes = {
            "federal": "US",
            "united states": "US",
            "congress": "US",
            # Add state mappings as needed
        }

    def extract_bill_metadata(
        self,
        raw_data: dict,
        source: str,
    ) -> DocumentMetadata:
        """
        Extract metadata from bill data.

        Args:
            raw_data: Raw bill data from source API
            source: Source name (e.g., 'congress.gov', 'openstates')

        Returns:
            DocumentMetadata object
        """
        if source == "congress.gov":
            return self._extract_congress_bill_metadata(raw_data)
        elif source == "openstates":
            return self._extract_openstates_bill_metadata(raw_data)
        else:
            return self._extract_generic_bill_metadata(raw_data, source)

    def extract_legislator_metadata(
        self,
        raw_data: dict,
        source: str,
    ) -> DocumentMetadata:
        """
        Extract metadata from legislator data.

        Args:
            raw_data: Raw legislator data from source API
            source: Source name

        Returns:
            DocumentMetadata object with fields:
            - document_id: "legislator-{openstates_id}"
            - document_type: "legislator"
            - source: Source name (openstates, webflow-cms, webflow+openstates)
            - title: Legislator name
            - jurisdiction: State code (e.g., "FL", "WA")
            - legislator_id: OpenStates ID (critical for filtering)
            - extra: Additional fields for enriched context
        """
        legislator_id = raw_data.get("id") or raw_data.get("bioguide_id", "")
        name = raw_data.get("name") or raw_data.get("full_name", "Unknown")
        state = raw_data.get("state", "")

        # Normalize chamber to standard values
        chamber = raw_data.get("chamber", "")
        if chamber:
            chamber_lower = chamber.lower()
            if chamber_lower in ("upper", "senate"):
                chamber = "upper"
            elif chamber_lower in ("lower", "house", "assembly"):
                chamber = "lower"

        # Build extra fields dict
        extra = {
            "party": raw_data.get("party"),
            "chamber": chamber,
            "state": state,
            "district": raw_data.get("district"),
        }

        # Add optional enriched fields if present
        if raw_data.get("ddp_score") is not None:
            extra["ddp_score"] = raw_data.get("ddp_score")

        if raw_data.get("webflow_id"):
            extra["webflow_id"] = raw_data.get("webflow_id")

        if raw_data.get("email"):
            extra["email"] = raw_data.get("email")

        if raw_data.get("image"):
            extra["image_url"] = raw_data.get("image")

        if raw_data.get("current_role"):
            role = raw_data["current_role"]
            if role.get("title"):
                extra["title"] = role["title"]

        return DocumentMetadata(
            document_id=f"legislator-{legislator_id}",
            document_type="legislator",
            source=source,
            title=name,
            jurisdiction=state or "US",
            legislator_id=legislator_id,
            extra=extra,
        )

    def extract_web_content_metadata(
        self,
        url: str,
        title: str | None = None,
        content_type: str = "article",
    ) -> DocumentMetadata:
        """
        Extract metadata from web content.

        Args:
            url: URL of the content
            title: Optional title
            content_type: Type of content

        Returns:
            DocumentMetadata object
        """
        # Generate document ID from URL
        doc_id = self._url_to_id(url)

        return DocumentMetadata(
            document_id=doc_id,
            document_type=content_type,
            source="web",
            title=title,
            url=url,
        )

    def extract_pdf_metadata(
        self,
        file_path: str,
        pdf_metadata: dict | None = None,
    ) -> DocumentMetadata:
        """
        Extract metadata from PDF document.

        Args:
            file_path: Path to the PDF file
            pdf_metadata: Optional PDF metadata dict

        Returns:
            DocumentMetadata object
        """
        import hashlib
        import os

        # Generate ID from file path
        file_name = os.path.basename(file_path)
        doc_id = hashlib.md5(file_path.encode()).hexdigest()[:12]

        title = None
        if pdf_metadata:
            title = pdf_metadata.get("/Title") or pdf_metadata.get("title")

        return DocumentMetadata(
            document_id=f"pdf-{doc_id}",
            document_type="pdf",
            source="pdf",
            title=title or file_name,
            extra={
                "file_path": file_path,
                "file_name": file_name,
            },
        )

    def _extract_congress_bill_metadata(self, raw_data: dict) -> DocumentMetadata:
        """Extract metadata from Congress.gov bill data."""
        bill_number = raw_data.get("number", "")
        bill_type = raw_data.get("type", "")
        congress = raw_data.get("congress", "")

        bill_id = f"{bill_type}{bill_number}-{congress}"

        # Parse effective date
        effective_date = None
        if raw_data.get("introducedDate"):
            try:
                effective_date = datetime.fromisoformat(
                    raw_data["introducedDate"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        return DocumentMetadata(
            document_id=f"bill-congress-{bill_id}",
            document_type="bill",
            source="congress.gov",
            title=raw_data.get("title"),
            jurisdiction="US",
            bill_id=bill_id,
            effective_date=effective_date,
            url=raw_data.get("url"),
            extra={
                "congress": congress,
                "bill_type": bill_type,
                "bill_number": bill_number,
                "sponsor": raw_data.get("sponsor", {}).get("name"),
                "status": raw_data.get("latestAction", {}).get("text"),
            },
        )

    def _extract_openstates_bill_metadata(self, raw_data: dict) -> DocumentMetadata:
        """Extract metadata from OpenStates bill data."""
        bill_id = raw_data.get("identifier", "")
        jurisdiction = raw_data.get("jurisdiction", {}).get("name", "")
        jurisdiction_code = raw_data.get("jurisdiction", {}).get("classification", "")

        # Convert state name to code if needed
        if len(jurisdiction_code) == 2:
            jurisdiction = jurisdiction_code.upper()

        return DocumentMetadata(
            document_id=f"bill-openstates-{bill_id}",
            document_type="bill",
            source="openstates",
            title=raw_data.get("title"),
            jurisdiction=jurisdiction,
            bill_id=bill_id,
            url=raw_data.get("openstates_url"),
            extra={
                "session": raw_data.get("legislative_session", {}).get("identifier"),
                "classification": raw_data.get("classification"),
            },
        )

    def _extract_generic_bill_metadata(
        self,
        raw_data: dict,
        source: str,
    ) -> DocumentMetadata:
        """Extract metadata from generic bill data."""
        bill_id = raw_data.get("id") or raw_data.get("bill_id", "unknown")

        return DocumentMetadata(
            document_id=f"bill-{source}-{bill_id}",
            document_type="bill",
            source=source,
            title=raw_data.get("title"),
            jurisdiction=raw_data.get("jurisdiction"),
            bill_id=bill_id,
        )

    def _url_to_id(self, url: str) -> str:
        """Convert URL to a document ID."""
        import hashlib

        # Create a hash of the URL
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]

        # Extract domain for prefix
        domain = ""
        if "://" in url:
            domain = url.split("://")[1].split("/")[0]
            domain = domain.replace("www.", "").split(".")[0]

        return f"web-{domain}-{url_hash}"

    def normalize_jurisdiction(self, value: str) -> str:
        """Normalize jurisdiction to standard code."""
        value_lower = value.lower().strip()

        # Check mapping
        if value_lower in self.jurisdiction_codes:
            return self.jurisdiction_codes[value_lower]

        # If already a 2-letter code, uppercase it
        if len(value) == 2 and value.isalpha():
            return value.upper()

        return value
