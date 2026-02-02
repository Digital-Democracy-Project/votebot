#!/usr/bin/env python3
"""
Unified sync tool for VoteBot.

This script provides a single entry point for syncing all content types:
- Bills (from Webflow CMS + OpenStates)
- Legislators (from Webflow CMS + OpenStates sponsored bills)
- Organizations (from Webflow CMS)
- Webpages (from any URL)
- Training documents (from local files)

Usage:
    # Single item sync
    python scripts/sync.py bill --webflow-id 6512abc123
    python scripts/sync.py bill --slug fl-hb-123-2025
    python scripts/sync.py legislator --slug rick-scott
    python scripts/sync.py organization --webflow-id 6512xyz789
    python scripts/sync.py webpage --url https://example.com/page
    python scripts/sync.py training --path /path/to/doc.txt

    # Batch sync
    python scripts/sync.py bill --batch
    python scripts/sync.py bill --batch --no-pdfs --limit 100
    python scripts/sync.py legislator --batch --jurisdiction FL
    python scripts/sync.py all --dry-run

    # Common options
    --dry-run          Preview without ingesting
    --limit N          Maximum items (batch mode)
    --log-level        DEBUG, INFO, WARNING, ERROR
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

from votebot.sync import (
    ContentType,
    SyncIdentifier,
    SyncMode,
    SyncOptions,
    UnifiedSyncService,
)
from votebot.utils.logging import setup_logging

import structlog

logger = structlog.get_logger()


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        description="Unified sync tool for VoteBot content ingestion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Global options
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    subparsers = parser.add_subparsers(dest="content_type", help="Content type to sync")

    # Bill subcommand
    bill_parser = subparsers.add_parser("bill", help="Sync bill content")
    _add_common_identifier_args(bill_parser)
    _add_common_sync_args(bill_parser)
    bill_parser.add_argument(
        "--no-pdfs",
        action="store_true",
        help="Skip PDF processing",
    )
    bill_parser.add_argument(
        "--no-openstates",
        action="store_true",
        help="Skip OpenStates legislative history sync",
    )

    # Legislator subcommand
    legislator_parser = subparsers.add_parser("legislator", help="Sync legislator content")
    _add_common_identifier_args(legislator_parser)
    _add_common_sync_args(legislator_parser)
    legislator_parser.add_argument(
        "--no-sponsored-bills",
        action="store_true",
        help="Skip OpenStates sponsored bills sync",
    )
    legislator_parser.add_argument(
        "--jurisdiction",
        type=str,
        help="Filter by jurisdiction (e.g., FL, US) in batch mode",
    )

    # Organization subcommand
    org_parser = subparsers.add_parser("organization", help="Sync organization content")
    _add_common_identifier_args(org_parser)
    _add_common_sync_args(org_parser)

    # Webpage subcommand
    webpage_parser = subparsers.add_parser("webpage", help="Sync webpage content")
    webpage_parser.add_argument(
        "--url",
        type=str,
        help="URL of the webpage to sync",
    )
    webpage_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without ingesting",
    )

    # Training subcommand
    training_parser = subparsers.add_parser("training", help="Sync training documents")
    training_parser.add_argument(
        "--path",
        type=str,
        help="Path to the training document file",
    )
    training_parser.add_argument(
        "--directory",
        type=str,
        help="Path to directory containing training documents (batch mode)",
    )
    training_parser.add_argument(
        "--pattern",
        type=str,
        default="*.txt",
        help="Glob pattern for files in directory (default: *.txt)",
    )
    training_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without ingesting",
    )
    training_parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum files to process (0 = unlimited)",
    )

    # All subcommand (batch sync all content types)
    all_parser = subparsers.add_parser("all", help="Sync all content types (batch mode)")
    all_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without ingesting",
    )
    all_parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum items per content type (0 = unlimited)",
    )
    all_parser.add_argument(
        "--no-pdfs",
        action="store_true",
        help="Skip PDF processing for bills",
    )
    all_parser.add_argument(
        "--no-openstates",
        action="store_true",
        help="Skip OpenStates sync for bills",
    )
    all_parser.add_argument(
        "--no-sponsored-bills",
        action="store_true",
        help="Skip OpenStates sponsored bills sync for legislators",
    )

    return parser


def _add_common_identifier_args(parser: argparse.ArgumentParser) -> None:
    """Add common identifier arguments to a subparser."""
    parser.add_argument(
        "--webflow-id",
        type=str,
        help="Webflow CMS item ID",
    )
    parser.add_argument(
        "--slug",
        type=str,
        help="Item slug in Webflow",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Sync all items (batch mode)",
    )


def _add_common_sync_args(parser: argparse.ArgumentParser) -> None:
    """Add common sync arguments to a subparser."""
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without ingesting",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum items (batch mode, 0 = unlimited)",
    )


def print_result(result) -> None:
    """Print sync result summary."""
    print("\n" + "=" * 60)
    print(f"SYNC RESULT: {result.content_type.value.upper()}")
    print("=" * 60)

    print(f"  Mode: {result.mode.value}")
    print(f"  Success: {'Yes' if result.success else 'No'}")
    print(f"  Items processed: {result.items_processed}")
    print(f"  Items successful: {result.items_successful}")
    print(f"  Items failed: {result.items_failed}")
    print(f"  Chunks created: {result.chunks_created}")
    print(f"  Duration: {result.duration_seconds:.2f}s")

    if result.errors:
        print(f"\n  Errors ({len(result.errors)}):")
        for error in result.errors[:5]:
            print(f"    - {error}")
        if len(result.errors) > 5:
            print(f"    ... and {len(result.errors) - 5} more")

    print("=" * 60)


async def sync_bill(args) -> int:
    """Handle bill sync command."""
    service = UnifiedSyncService()

    if args.batch:
        options = SyncOptions(
            include_pdfs=not args.no_pdfs,
            include_openstates=not args.no_openstates,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        result = await service.sync(ContentType.BILL, SyncMode.BATCH, options=options)
    elif args.webflow_id or args.slug:
        identifier = SyncIdentifier(
            webflow_id=args.webflow_id,
            slug=args.slug,
        )
        options = SyncOptions(
            include_pdfs=not args.no_pdfs,
            include_openstates=not args.no_openstates,
            dry_run=args.dry_run,
        )
        result = await service.sync(ContentType.BILL, SyncMode.SINGLE, identifier, options)
    else:
        print("Error: Either --batch, --webflow-id, or --slug is required")
        return 1

    print_result(result)
    return 0 if result.success else 1


async def sync_legislator(args) -> int:
    """Handle legislator sync command."""
    service = UnifiedSyncService()

    if args.batch:
        options = SyncOptions(
            include_sponsored_bills=not args.no_sponsored_bills,
            jurisdiction=args.jurisdiction,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        result = await service.sync(ContentType.LEGISLATOR, SyncMode.BATCH, options=options)
    elif args.webflow_id or args.slug:
        identifier = SyncIdentifier(
            webflow_id=args.webflow_id,
            slug=args.slug,
        )
        options = SyncOptions(
            include_sponsored_bills=not args.no_sponsored_bills,
            dry_run=args.dry_run,
        )
        result = await service.sync(ContentType.LEGISLATOR, SyncMode.SINGLE, identifier, options)
    else:
        print("Error: Either --batch, --webflow-id, or --slug is required")
        return 1

    print_result(result)
    return 0 if result.success else 1


async def sync_organization(args) -> int:
    """Handle organization sync command."""
    service = UnifiedSyncService()

    if args.batch:
        options = SyncOptions(
            limit=args.limit,
            dry_run=args.dry_run,
        )
        result = await service.sync(ContentType.ORGANIZATION, SyncMode.BATCH, options=options)
    elif args.webflow_id or args.slug:
        identifier = SyncIdentifier(
            webflow_id=args.webflow_id,
            slug=args.slug,
        )
        options = SyncOptions(dry_run=args.dry_run)
        result = await service.sync(ContentType.ORGANIZATION, SyncMode.SINGLE, identifier, options)
    else:
        print("Error: Either --batch, --webflow-id, or --slug is required")
        return 1

    print_result(result)
    return 0 if result.success else 1


async def sync_webpage(args) -> int:
    """Handle webpage sync command."""
    if not args.url:
        print("Error: --url is required for webpage sync")
        return 1

    service = UnifiedSyncService()
    identifier = SyncIdentifier(url=args.url)
    options = SyncOptions(dry_run=args.dry_run)
    result = await service.sync(ContentType.WEBPAGE, SyncMode.SINGLE, identifier, options)

    print_result(result)
    return 0 if result.success else 1


async def sync_training(args) -> int:
    """Handle training document sync command."""
    service = UnifiedSyncService()
    options = SyncOptions(
        limit=args.limit,
        dry_run=args.dry_run,
    )

    if args.directory:
        # Batch sync from directory
        from votebot.sync.handlers.training import TrainingHandler
        handler = TrainingHandler()
        result = await handler.sync_directory(args.directory, options, pattern=args.pattern)
    elif args.path:
        identifier = SyncIdentifier(file_path=args.path)
        result = await service.sync(ContentType.TRAINING, SyncMode.SINGLE, identifier, options)
    else:
        print("Error: Either --path or --directory is required for training document sync")
        return 1

    print_result(result)
    return 0 if result.success else 1


async def sync_all(args) -> int:
    """Handle sync all command."""
    service = UnifiedSyncService()
    options = SyncOptions(
        include_pdfs=not args.no_pdfs,
        include_openstates=not args.no_openstates,
        include_sponsored_bills=not args.no_sponsored_bills,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    print("=" * 60)
    print("UNIFIED SYNC - ALL CONTENT TYPES")
    print("=" * 60)
    print(f"  Dry run: {args.dry_run}")
    print(f"  Limit: {args.limit if args.limit > 0 else 'unlimited'}")
    print(f"  Include PDFs: {not args.no_pdfs}")
    print(f"  Include OpenStates: {not args.no_openstates}")
    print(f"  Include sponsored bills: {not args.no_sponsored_bills}")
    print("=" * 60)

    results = await service.sync_all(options)

    # Print individual results
    for content_type, result in results.items():
        print_result(result)

    # Print summary
    print("\n" + "=" * 60)
    print("OVERALL SUMMARY")
    print("=" * 60)

    total_items = sum(r.items_processed for r in results.values())
    total_successful = sum(r.items_successful for r in results.values())
    total_chunks = sum(r.chunks_created for r in results.values())
    all_success = all(r.success for r in results.values())

    print(f"  Total items processed: {total_items}")
    print(f"  Total items successful: {total_successful}")
    print(f"  Total chunks created: {total_chunks}")
    print(f"  All syncs successful: {'Yes' if all_success else 'No'}")
    print("=" * 60)

    return 0 if all_success else 1


async def main() -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()

    if not args.content_type:
        parser.print_help()
        return 1

    # Setup logging
    setup_logging(args.log_level)

    try:
        if args.content_type == "bill":
            return await sync_bill(args)
        elif args.content_type == "legislator":
            return await sync_legislator(args)
        elif args.content_type == "organization":
            return await sync_organization(args)
        elif args.content_type == "webpage":
            return await sync_webpage(args)
        elif args.content_type == "training":
            return await sync_training(args)
        elif args.content_type == "all":
            return await sync_all(args)
        else:
            print(f"Unknown content type: {args.content_type}")
            return 1

    except KeyboardInterrupt:
        print("\nSync interrupted by user")
        return 130
    except Exception as e:
        print(f"\nSync failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
