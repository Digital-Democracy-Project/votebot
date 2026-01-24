#!/usr/bin/env python3
"""Development data seeding script for VoteBot."""

import asyncio
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from votebot.config import get_settings
from votebot.ingestion.metadata import DocumentMetadata, MetadataExtractor
from votebot.ingestion.pipeline import DocumentSource, IngestionPipeline
from votebot.utils.logging import setup_logging


# Sample bills for development testing
SAMPLE_BILLS = [
    {
        "id": "HR-1234-118",
        "title": "Sample Clean Energy Act of 2024",
        "content": """
# Sample Clean Energy Act of 2024

## Summary
This bill establishes new requirements for clean energy production and investment
in renewable energy infrastructure across the United States.

## Key Provisions

### Section 1: Clean Energy Standards
- Requires 50% of electricity to come from renewable sources by 2030
- Establishes tax incentives for solar and wind energy production
- Creates grant programs for state and local clean energy initiatives

### Section 2: Infrastructure Investment
- Authorizes $10 billion for electric vehicle charging infrastructure
- Provides funding for grid modernization projects
- Supports workforce development in clean energy sectors

### Section 3: Environmental Protection
- Strengthens emissions standards for power plants
- Expands protected federal lands
- Increases penalties for environmental violations

## Sponsors
- Primary Sponsor: Rep. Jane Smith (D-CA)
- Cosponsors: 45 Representatives

## Status
- Introduced: January 15, 2024
- Passed House: March 20, 2024
- Currently in Senate Committee on Environment and Public Works
        """,
        "jurisdiction": "US",
        "type": "bill",
    },
    {
        "id": "SB-5678-CA",
        "title": "California Housing Affordability Act",
        "content": """
# California Housing Affordability Act (SB 5678)

## Summary
This bill addresses housing affordability in California by streamlining
development processes and providing funding for affordable housing projects.

## Key Provisions

### Article 1: Zoning Reform
- Allows increased density near transit stations
- Reduces parking requirements for new developments
- Streamlines environmental review for affordable housing

### Article 2: Funding Mechanisms
- Creates a $2 billion housing trust fund
- Provides tax credits for affordable housing developers
- Establishes down payment assistance programs

### Article 3: Tenant Protections
- Caps annual rent increases at 5%
- Requires just cause for eviction
- Expands legal aid for tenants

## Sponsors
- Author: Senator John Doe (D-San Francisco)
- Principal Coauthors: 12 Senators

## Status
- Introduced: February 1, 2024
- Passed Senate: April 15, 2024
- In Assembly Housing Committee
        """,
        "jurisdiction": "CA",
        "type": "bill",
    },
]

# Sample legislators
SAMPLE_LEGISLATORS = [
    {
        "id": "bioguide-S000123",
        "name": "Rep. Jane Smith",
        "content": """
# Representative Jane Smith

## Biography
Jane Smith has served as the U.S. Representative for California's 12th
congressional district since 2018. She is a member of the Democratic Party.

## Committee Assignments
- House Committee on Energy and Commerce (Chair, Subcommittee on Environment)
- House Committee on Science, Space, and Technology

## Policy Focus Areas
- Climate change and clean energy
- Healthcare access
- Education funding

## Key Sponsored Legislation
- Clean Energy Act of 2024 (HR-1234)
- Renewable Energy Tax Credit Extension Act
- Electric Vehicle Infrastructure Act

## Voting Record
- Environment: 95% League of Conservation Voters score
- Labor: 90% AFL-CIO score
- Business: 45% Chamber of Commerce score

## Contact Information
- Washington Office: 123 Cannon HOB, Washington, DC 20515
- District Office: 456 Main Street, San Francisco, CA 94102
        """,
        "party": "D",
        "state": "CA",
        "chamber": "House",
        "type": "legislator",
    },
]

# Sample educational content
SAMPLE_EDUCATIONAL = [
    {
        "id": "edu-how-bill-becomes-law",
        "title": "How a Bill Becomes a Law",
        "content": """
# How a Bill Becomes a Law

## Introduction
Understanding the legislative process is essential for civic engagement.
This guide explains the journey of a bill from introduction to becoming law.

## Step 1: Bill Introduction
- A member of Congress introduces the bill
- The bill is assigned a number (H.R. for House, S. for Senate)
- The bill is referred to a committee

## Step 2: Committee Review
- The committee studies the bill
- Hearings may be held to gather testimony
- The committee votes on whether to report the bill

## Step 3: Floor Debate
- The bill is debated by the full chamber
- Amendments may be proposed and voted on
- A final vote is taken

## Step 4: The Other Chamber
- The process repeats in the other chamber
- If the chambers pass different versions, a conference committee reconciles them

## Step 5: Presidential Action
- The President signs the bill into law, or
- The President vetoes the bill, or
- The bill becomes law without signature after 10 days

## Key Terms
- **Sponsor**: The member who introduces the bill
- **Cosponsor**: Members who support the bill
- **Amendment**: A change proposed to the bill
- **Veto**: Presidential rejection of a bill
        """,
        "type": "educational",
    },
]


async def seed_data() -> None:
    """Seed the database with sample data."""
    print("Starting data seeding...")

    settings = get_settings()
    pipeline = IngestionPipeline(settings)
    metadata_extractor = MetadataExtractor()

    documents = []

    # Add sample bills
    print("\nPreparing sample bills...")
    for bill in SAMPLE_BILLS:
        metadata = DocumentMetadata(
            document_id=f"bill-sample-{bill['id']}",
            document_type="bill",
            source="sample",
            title=bill["title"],
            jurisdiction=bill["jurisdiction"],
            bill_id=bill["id"],
        )
        documents.append(DocumentSource(content=bill["content"], metadata=metadata))
        print(f"  - {bill['title']}")

    # Add sample legislators
    print("\nPreparing sample legislators...")
    for leg in SAMPLE_LEGISLATORS:
        metadata = DocumentMetadata(
            document_id=f"legislator-sample-{leg['id']}",
            document_type="legislator",
            source="sample",
            title=leg["name"],
            jurisdiction=leg["state"],
            legislator_id=leg["id"],
            extra={
                "party": leg["party"],
                "chamber": leg["chamber"],
            },
        )
        documents.append(DocumentSource(content=leg["content"], metadata=metadata))
        print(f"  - {leg['name']}")

    # Add educational content
    print("\nPreparing educational content...")
    for edu in SAMPLE_EDUCATIONAL:
        metadata = DocumentMetadata(
            document_id=f"edu-sample-{edu['id']}",
            document_type="educational",
            source="sample",
            title=edu["title"],
        )
        documents.append(DocumentSource(content=edu["content"], metadata=metadata))
        print(f"  - {edu['title']}")

    # Ingest all documents
    print(f"\nIngesting {len(documents)} documents...")
    result = await pipeline.ingest_batch(documents)

    print("\n--- Results ---")
    print(f"Documents processed: {result.documents_processed}")
    print(f"Chunks created: {result.chunks_created}")
    print(f"Chunks upserted: {result.chunks_upserted}")

    if result.errors:
        print(f"\nErrors ({len(result.errors)}):")
        for error in result.errors:
            print(f"  - {error}")
    else:
        print("\nSeeding completed successfully!")


async def main() -> None:
    """Main entry point."""
    setup_logging("INFO")

    try:
        await seed_data()
    except Exception as e:
        print(f"\nSeeding failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
