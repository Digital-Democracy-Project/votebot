#!/usr/bin/env python3
"""
Organization sync script for VoteBot.

This script:
1. Fetches member organizations from Webflow CMS
2. Resolves bill references to bill names
3. Ingests to Pinecone vector store with proper metadata

Usage:
    python scripts/sync_organizations.py [options]

Options:
    --limit N          Maximum organizations to process (default: 0 = unlimited)
    --dry-run          Print what would be done without ingesting
    --log-level LEVEL  Logging level (default: INFO)
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from votebot.config import get_settings

# Clear settings cache to ensure fresh env vars are loaded
get_settings.cache_clear()

from votebot.ingestion.metadata import MetadataExtractor
from votebot.ingestion.pipeline import DocumentSource, IngestionPipeline
from votebot.ingestion.sources.webflow import WebflowSource
from votebot.utils.logging import setup_logging

import structlog

logger = structlog.get_logger()


async def fetch_webflow_organizations(
    webflow: WebflowSource,
    limit: int = 0,
) -> list[DocumentSource]:
    """
    Fetch all organizations from Webflow CMS.

    Args:
        webflow: WebflowSource instance
        limit: Maximum number to fetch (0 = unlimited)

    Returns:
        List of DocumentSource objects from Webflow
    """
    organizations = []
    async for doc in webflow.fetch_organizations(limit=limit):
        organizations.append(doc)
    return organizations


async def sync_organizations(
    limit: int = 0,
    dry_run: bool = False,
) -> dict:
    """
    Main sync function that orchestrates the organization ingestion.

    Args:
        limit: Maximum organizations to process (0 = unlimited)
        dry_run: If True, don't actually ingest

    Returns:
        Summary dict with stats
    """
    settings = get_settings()
    metadata_extractor = MetadataExtractor()

    webflow = WebflowSource(settings, metadata_extractor)

    print("=" * 70)
    print("ORGANIZATION SYNC")
    print("=" * 70)

    # Step 1: Fetch from Webflow
    print("\n[1/2] Fetching organizations from Webflow CMS...")
    organizations = await fetch_webflow_organizations(webflow, limit=limit)
    print(f"      Fetched {len(organizations)} organizations from Webflow")

    if not organizations:
        print("      No organizations found in Webflow. Aborting.")
        return {"webflow_count": 0, "ingested_count": 0}

    # Show sample of bill positions
    orgs_with_support = sum(
        1 for doc in organizations
        if doc.metadata.extra.get("bills_support_count", 0) > 0
    )
    orgs_with_oppose = sum(
        1 for doc in organizations
        if doc.metadata.extra.get("bills_oppose_count", 0) > 0
    )
    print(f"      Organizations with bill support positions: {orgs_with_support}")
    print(f"      Organizations with bill oppose positions: {orgs_with_oppose}")

    # Step 2: Ingest to vector store
    if dry_run:
        print("\n[2/2] DRY RUN - Would ingest the following organizations:")
        for doc in organizations[:10]:
            print(f"      - {doc.metadata.title}")
            extra = doc.metadata.extra
            if extra.get("organization_type"):
                print(f"        Type: {extra['organization_type']}")
            support_count = extra.get("bills_support_count", 0)
            oppose_count = extra.get("bills_oppose_count", 0)
            if support_count or oppose_count:
                print(f"        Bill positions: {support_count} support, {oppose_count} oppose")
        if len(organizations) > 10:
            print(f"      ... and {len(organizations) - 10} more")
        result = {
            "webflow_count": len(organizations),
            "ingested_count": 0,
        }
    else:
        print(f"\n[2/2] Ingesting {len(organizations)} organizations to vector store...")
        pipeline = IngestionPipeline(settings)
        ingest_result = await pipeline.ingest_batch(organizations)

        print(f"      Documents processed: {ingest_result.documents_processed}")
        print(f"      Chunks created: {ingest_result.chunks_created}")
        print(f"      Chunks upserted: {ingest_result.chunks_upserted}")
        if ingest_result.errors:
            print(f"      Errors: {len(ingest_result.errors)}")
            for error in ingest_result.errors[:5]:
                print(f"        - {error}")

        result = {
            "webflow_count": len(organizations),
            "ingested_count": ingest_result.documents_processed,
            "chunks_created": ingest_result.chunks_created,
            "chunks_upserted": ingest_result.chunks_upserted,
            "errors": len(ingest_result.errors),
        }

    print("\n" + "=" * 70)
    print("SYNC COMPLETE")
    print("=" * 70)

    return result


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync organizations from Webflow to VoteBot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum organizations to process (default: 0 = unlimited)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without ingesting",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level)

    try:
        result = await sync_organizations(
            limit=args.limit,
            dry_run=args.dry_run,
        )

        print(f"\nSummary:")
        print(f"  Webflow organizations: {result['webflow_count']}")
        print(f"  Ingested to vector store: {result.get('ingested_count', 'N/A (dry run)')}")
        if result.get("chunks_created"):
            print(f"  Chunks created: {result['chunks_created']}")
            print(f"  Chunks upserted: {result['chunks_upserted']}")

    except Exception as e:
        print(f"\nSync failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
