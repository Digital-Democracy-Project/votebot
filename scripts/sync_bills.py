#!/usr/bin/env python3
"""
Bill sync script for VoteBot.

DEPRECATED: This script is deprecated. Use the unified sync tool instead:
    python scripts/sync.py bill --batch
    python scripts/sync.py bill --slug <slug>

This script:
1. Fetches bills from Webflow CMS
2. Builds organization mapping for bill enrichment
3. Resolves organization positions (support/oppose) for each bill
4. Adds enhanced metadata (external links, DDP citation slugs)
5. Ingests to Pinecone vector store with proper metadata

Usage:
    python scripts/sync_bills.py [options]

Options:
    --limit N          Maximum bills to process (default: 0 = unlimited)
    --include-pdfs     Include PDF processing (default: False)
    --dry-run          Print what would be done without ingesting
    --log-level LEVEL  Logging level (default: INFO)
"""
import warnings

warnings.warn(
    "sync_bills.py is deprecated. Use 'python scripts/sync.py bill' instead.",
    DeprecationWarning,
    stacklevel=2,
)

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


async def fetch_webflow_bills(
    webflow: WebflowSource,
    limit: int = 0,
    include_pdfs: bool = False,
) -> list[DocumentSource]:
    """
    Fetch all bills from Webflow CMS with organization enrichment.

    Args:
        webflow: WebflowSource instance
        limit: Maximum number to fetch (0 = unlimited)
        include_pdfs: Whether to process PDFs

    Returns:
        List of DocumentSource objects from Webflow
    """
    bills = []
    async for doc in webflow.fetch(
        collection_id=webflow.bills_collection_id,
        limit=limit,
        include_pdfs=include_pdfs,
    ):
        bills.append(doc)
    return bills


async def sync_bills(
    limit: int = 0,
    include_pdfs: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Main sync function that orchestrates the bill ingestion with enrichment.

    Args:
        limit: Maximum bills to process (0 = unlimited)
        include_pdfs: Whether to process PDFs
        dry_run: If True, don't actually ingest

    Returns:
        Summary dict with stats
    """
    settings = get_settings()
    metadata_extractor = MetadataExtractor()

    webflow = WebflowSource(settings, metadata_extractor)

    print("=" * 70)
    print("BILL SYNC WITH ORGANIZATION ENRICHMENT")
    print("=" * 70)

    # Step 1: Fetch from Webflow (organization mapping is built automatically)
    print("\n[1/2] Fetching bills from Webflow CMS...")
    print("      (Building organization mapping for enrichment...)")
    bills = await fetch_webflow_bills(webflow, limit=limit, include_pdfs=include_pdfs)
    print(f"      Fetched {len(bills)} bill documents from Webflow")

    if not bills:
        print("      No bills found in Webflow. Aborting.")
        return {"webflow_count": 0, "ingested_count": 0}

    # Analyze organization enrichment stats
    bills_with_support = sum(
        1 for doc in bills
        if doc.metadata.extra.get("supporting_orgs_count", 0) > 0
    )
    bills_with_oppose = sum(
        1 for doc in bills
        if doc.metadata.extra.get("opposing_orgs_count", 0) > 0
    )
    bills_with_external_links = sum(
        1 for doc in bills
        if doc.metadata.extra.get("open_plural_url") or doc.metadata.extra.get("kialo_url")
    )
    bills_with_slug = sum(
        1 for doc in bills
        if doc.metadata.extra.get("slug")
    )

    print(f"\n      Enrichment Statistics:")
    print(f"        Bills with supporting organizations: {bills_with_support}")
    print(f"        Bills with opposing organizations: {bills_with_oppose}")
    print(f"        Bills with external discussion links: {bills_with_external_links}")
    print(f"        Bills with DDP slugs (citations): {bills_with_slug}")

    # Step 2: Ingest to vector store
    if dry_run:
        print("\n[2/2] DRY RUN - Would ingest the following bills:")
        for doc in bills[:10]:
            print(f"      - {doc.metadata.title}")
            extra = doc.metadata.extra
            if extra.get("bill_prefix") and extra.get("bill_number"):
                print(f"        Bill: {extra['bill_prefix']} {extra['bill_number']}")
            if extra.get("slug"):
                print(f"        Slug: {extra['slug']}")
            support_count = extra.get("supporting_orgs_count", 0)
            oppose_count = extra.get("opposing_orgs_count", 0)
            if support_count or oppose_count:
                print(f"        Org positions: {support_count} support, {oppose_count} oppose")
        if len(bills) > 10:
            print(f"      ... and {len(bills) - 10} more")
        result = {
            "webflow_count": len(bills),
            "ingested_count": 0,
            "bills_with_support": bills_with_support,
            "bills_with_oppose": bills_with_oppose,
        }
    else:
        print(f"\n[2/2] Ingesting {len(bills)} bills to vector store...")
        pipeline = IngestionPipeline(settings)
        ingest_result = await pipeline.ingest_batch(bills)

        print(f"      Documents processed: {ingest_result.documents_processed}")
        print(f"      Chunks created: {ingest_result.chunks_created}")
        print(f"      Chunks upserted: {ingest_result.chunks_upserted}")
        if ingest_result.errors:
            print(f"      Errors: {len(ingest_result.errors)}")
            for error in ingest_result.errors[:5]:
                print(f"        - {error}")

        result = {
            "webflow_count": len(bills),
            "ingested_count": ingest_result.documents_processed,
            "chunks_created": ingest_result.chunks_created,
            "chunks_upserted": ingest_result.chunks_upserted,
            "errors": len(ingest_result.errors),
            "bills_with_support": bills_with_support,
            "bills_with_oppose": bills_with_oppose,
        }

    print("\n" + "=" * 70)
    print("SYNC COMPLETE")
    print("=" * 70)

    return result


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync bills from Webflow to VoteBot with organization enrichment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum bills to process (default: 0 = unlimited)",
    )
    parser.add_argument(
        "--include-pdfs",
        action="store_true",
        help="Include PDF processing (slower)",
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
        result = await sync_bills(
            limit=args.limit,
            include_pdfs=args.include_pdfs,
            dry_run=args.dry_run,
        )

        print(f"\nSummary:")
        print(f"  Webflow bills: {result['webflow_count']}")
        print(f"  Ingested to vector store: {result.get('ingested_count', 'N/A (dry run)')}")
        if result.get("chunks_created"):
            print(f"  Chunks created: {result['chunks_created']}")
            print(f"  Chunks upserted: {result['chunks_upserted']}")
        print(f"  Bills with org support: {result.get('bills_with_support', 0)}")
        print(f"  Bills with org opposition: {result.get('bills_with_oppose', 0)}")

    except Exception as e:
        print(f"\nSync failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
