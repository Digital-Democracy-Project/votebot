#!/usr/bin/env python3
"""
One-time backload script for syncing all CMS bills from OpenStates.

This script fetches all bills from Webflow CMS and syncs their status,
votes, and actions from OpenStates API. Run this once to populate
historical data, then use daily sync for ongoing updates.

Usage:
    python scripts/backload_openstates.py [--limit N] [--jurisdiction XX] [--dry-run]

Options:
    --limit N          Only process first N bills (for testing)
    --jurisdiction XX  Only process bills from jurisdiction XX (e.g., FL, WA)
    --dry-run          Show what would be synced without making changes
    --resume-from ID   Resume from a specific Webflow bill ID
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx
import structlog

from votebot.config import get_settings
from votebot.updates.bill_sync import BillSyncService
from votebot.utils.logging import setup_logging

# Clear settings cache
get_settings.cache_clear()

logger = structlog.get_logger()


async def fetch_all_bills(settings, limit: int = 0, jurisdiction: str = None) -> list:
    """Fetch all bills from Webflow CMS."""
    bills = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = {
            "Authorization": f"Bearer {settings.webflow_votebot_api_key.get_secret_value()}",
            "accept": "application/json",
        }

        offset = 0
        while True:
            response = await client.get(
                f"https://api.webflow.com/v2/collections/{settings.webflow_bills_collection_id}/items",
                headers=headers,
                params={"limit": 100, "offset": offset},
            )

            if response.status_code != 200:
                logger.error(f"Webflow API error: {response.status_code}")
                break

            data = response.json()
            items = data.get("items", [])

            if not items:
                break

            # Filter by jurisdiction if specified
            if jurisdiction:
                items = [
                    item for item in items
                    if BillSyncService.JURISDICTION_MAP.get(
                        item.get("fieldData", {}).get("jurisdiction", "")
                    ) == jurisdiction.lower()
                ]

            bills.extend(items)
            offset += 100

            logger.info(f"Fetched {len(bills)} bills so far...")

            if len(items) < 100:
                break

            # Check limit
            if limit > 0 and len(bills) >= limit:
                bills = bills[:limit]
                break

    return bills


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Backload all CMS bills from OpenStates",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process first N bills (0 = unlimited)",
    )
    parser.add_argument(
        "--jurisdiction",
        type=str,
        help="Only process bills from jurisdiction (e.g., FL, WA, US)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without making changes",
    )
    parser.add_argument(
        "--resume-from",
        type=str,
        help="Resume from a specific Webflow bill ID",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level)

    print("=" * 70)
    print("OPENSTATES BACKLOAD")
    print("=" * 70)
    print(f"Started: {datetime.now().isoformat()}")
    print(f"Limit: {args.limit if args.limit > 0 else 'unlimited'}")
    print(f"Jurisdiction: {args.jurisdiction or 'all'}")
    print(f"Dry run: {args.dry_run}")
    print()

    settings = get_settings()

    # Check API key
    if not settings.openstates_api_key.get_secret_value():
        print("ERROR: OPENSTATES_API_KEY not configured in .env")
        sys.exit(1)

    # Fetch all bills from Webflow
    print("Fetching bills from Webflow CMS...")
    bills = await fetch_all_bills(settings, args.limit, args.jurisdiction)
    print(f"Total bills to process: {len(bills)}")

    # Filter bills with OpenStates URLs
    bills_with_urls = [
        bill for bill in bills
        if bill.get("fieldData", {}).get("open-states-url-2")
    ]
    print(f"Bills with OpenStates URLs: {len(bills_with_urls)}")

    if not bills_with_urls:
        print("No bills to sync!")
        return

    # Resume from specific ID if requested
    if args.resume_from:
        start_idx = next(
            (i for i, b in enumerate(bills_with_urls) if b.get("id") == args.resume_from),
            0,
        )
        if start_idx > 0:
            bills_with_urls = bills_with_urls[start_idx:]
            print(f"Resuming from index {start_idx}, {len(bills_with_urls)} bills remaining")

    if args.dry_run:
        print("\n--- DRY RUN MODE ---")
        print("Would sync the following bills:")
        for i, bill in enumerate(bills_with_urls[:20]):
            fields = bill.get("fieldData", {})
            title = fields.get("name", "Unknown")
            url = fields.get("open-states-url-2", "")
            print(f"  {i+1}. {title[:60]}...")
            print(f"      URL: {url}")

        if len(bills_with_urls) > 20:
            print(f"  ... and {len(bills_with_urls) - 20} more")

        print("\nRun without --dry-run to perform actual sync.")
        return

    # Initialize sync service
    print("\nInitializing sync service...")
    sync_service = BillSyncService(settings)

    # Run backload
    print("\nStarting backload...")
    print("-" * 70)

    result = await sync_service.backload_all_bills(bills_with_urls)

    # Print results
    print()
    print("=" * 70)
    print("BACKLOAD COMPLETE")
    print("=" * 70)
    print(f"Total bills processed: {result.total_bills}")
    print(f"Successful: {result.successful}")
    print(f"Failed: {result.failed}")
    print(f"Chunks created: {result.chunks_created}")
    print(f"Finished: {datetime.now().isoformat()}")

    if result.errors:
        print(f"\nErrors ({len(result.errors)} total):")
        for error in result.errors[:20]:
            print(f"  - {error[:100]}...")
        if len(result.errors) > 20:
            print(f"  ... and {len(result.errors) - 20} more errors")

    # Return exit code based on success rate
    success_rate = result.successful / result.total_bills if result.total_bills > 0 else 0
    if success_rate < 0.5:
        print("\nWARNING: Less than 50% success rate!")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
