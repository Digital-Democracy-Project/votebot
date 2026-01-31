#!/usr/bin/env python3
"""
Legislator bills sync script for VoteBot.

This script:
1. Fetches legislators from Webflow CMS
2. Fetches their sponsored bills from OpenStates
3. Creates `legislator-bills` documents with DDP links
4. Ingests to Pinecone vector store

This enables queries like "What bills has Rick Scott sponsored?"

Usage:
    python scripts/sync_legislator_bills.py [options]

Options:
    --limit N          Maximum legislators to process (default: 0 = unlimited)
    --jurisdiction J   Filter by jurisdiction code (e.g., 'fl', 'us')
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
from votebot.ingestion.sources.webflow import WebflowSource
from votebot.updates.legislator_sync import LegislatorSyncService
from votebot.utils.logging import setup_logging

import structlog

logger = structlog.get_logger()


async def fetch_webflow_legislators(
    webflow: WebflowSource,
    limit: int = 0,
    jurisdiction: str | None = None,
) -> list[dict]:
    """
    Fetch legislators from Webflow CMS and extract relevant fields.

    Args:
        webflow: WebflowSource instance
        limit: Maximum number to fetch (0 = unlimited)
        jurisdiction: Filter by jurisdiction code

    Returns:
        List of legislator dicts ready for sync
    """
    legislators = []
    count = 0

    async for doc in webflow.fetch_legislators(limit=0):  # Fetch all, filter later
        # Extract fields needed for sync
        extra = doc.metadata.extra
        legislator = {
            "openstates_id": doc.metadata.legislator_id,
            "name": doc.metadata.title,
            "slug": extra.get("slug", ""),
            "jurisdiction": doc.metadata.jurisdiction or "us",
            "party": extra.get("party", ""),
            "chamber": extra.get("chamber", ""),
        }

        # Skip if no OpenStates ID
        if not legislator["openstates_id"]:
            logger.debug(f"Skipping {legislator['name']}: no OpenStates ID")
            continue

        # Filter by jurisdiction if specified
        if jurisdiction:
            if legislator["jurisdiction"].lower() != jurisdiction.lower():
                continue

        legislators.append(legislator)
        count += 1

        # Apply limit
        if limit > 0 and count >= limit:
            break

    return legislators


async def sync_legislator_bills(
    limit: int = 0,
    jurisdiction: str | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Main sync function that orchestrates the legislator bills ingestion.

    Args:
        limit: Maximum legislators to process (0 = unlimited)
        jurisdiction: Filter by jurisdiction code
        dry_run: If True, don't actually ingest

    Returns:
        Summary dict with stats
    """
    settings = get_settings()
    metadata_extractor = MetadataExtractor()

    webflow = WebflowSource(settings, metadata_extractor)
    sync_service = LegislatorSyncService(settings)

    print("=" * 70)
    print("LEGISLATOR BILLS SYNC")
    print("=" * 70)

    # Step 1: Fetch legislators from Webflow
    print("\n[1/2] Fetching legislators from Webflow CMS...")
    legislators = await fetch_webflow_legislators(
        webflow,
        limit=limit,
        jurisdiction=jurisdiction,
    )
    print(f"      Fetched {len(legislators)} legislators with OpenStates IDs")

    if not legislators:
        print("      No legislators found. Aborting.")
        return {
            "webflow_count": 0,
            "successful": 0,
            "failed": 0,
            "total_bills": 0,
        }

    # Show sample
    print("\n      Sample legislators:")
    for leg in legislators[:5]:
        print(f"        - {leg['name']} ({leg['jurisdiction'].upper()})")
    if len(legislators) > 5:
        print(f"        ... and {len(legislators) - 5} more")

    # Step 2: Sync sponsored bills
    if dry_run:
        print("\n[2/2] DRY RUN - Would fetch sponsored bills for:")
        for leg in legislators[:10]:
            print(f"      - {leg['name']} ({leg['openstates_id'][:20]}...)")
        if len(legislators) > 10:
            print(f"      ... and {len(legislators) - 10} more")

        result = {
            "webflow_count": len(legislators),
            "successful": 0,
            "failed": 0,
            "total_bills": 0,
            "dry_run": True,
        }
    else:
        print(f"\n[2/2] Fetching sponsored bills from OpenStates...")
        print(f"      (This may take a while due to rate limiting)")

        batch_result = await sync_service.sync_all_legislators(legislators)

        print(f"\n      Results:")
        print(f"        Successful: {batch_result.successful}")
        print(f"        Failed: {batch_result.failed}")
        print(f"        Total bills found: {batch_result.total_bills_found}")
        print(f"        Chunks created: {batch_result.chunks_created}")

        if batch_result.errors:
            print(f"\n      Errors ({len(batch_result.errors)}):")
            for error in batch_result.errors[:5]:
                print(f"        - {error}")
            if len(batch_result.errors) > 5:
                print(f"        ... and {len(batch_result.errors) - 5} more")

        result = {
            "webflow_count": len(legislators),
            "successful": batch_result.successful,
            "failed": batch_result.failed,
            "total_bills": batch_result.total_bills_found,
            "chunks_created": batch_result.chunks_created,
        }

    print("\n" + "=" * 70)
    print("SYNC COMPLETE")
    print("=" * 70)

    return result


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync legislator sponsored bills from OpenStates to VoteBot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum legislators to process (default: 0 = unlimited)",
    )
    parser.add_argument(
        "--jurisdiction",
        "-j",
        type=str,
        default=None,
        help="Filter by jurisdiction code (e.g., 'fl', 'us')",
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
        result = await sync_legislator_bills(
            limit=args.limit,
            jurisdiction=args.jurisdiction,
            dry_run=args.dry_run,
        )

        print(f"\nSummary:")
        print(f"  Legislators processed: {result['webflow_count']}")
        print(f"  Successful syncs: {result.get('successful', 'N/A (dry run)')}")
        print(f"  Failed syncs: {result.get('failed', 'N/A (dry run)')}")
        print(f"  Total bills found: {result.get('total_bills', 'N/A (dry run)')}")
        if result.get("chunks_created"):
            print(f"  Chunks created: {result['chunks_created']}")

    except Exception as e:
        print(f"\nSync failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
