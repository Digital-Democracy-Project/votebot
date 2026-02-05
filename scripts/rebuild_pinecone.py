#!/usr/bin/env python3
"""
Wipe Pinecone index and rebuild all data from scratch.

This script performs a complete rebuild of the Pinecone vector store with all
content types in the correct order to ensure proper data linkages.

Sync Order:
1. Bills (includes org positions, OpenStates votes with person IDs)
2. Legislators (includes OpenStates person IDs)
3. Organizations (includes bill positions)
4. Webpages (DDP website pages)
5. Training (local training documents)
6. Legislator-votes reverse index (aggregates votes per legislator)

Usage:
    python scripts/rebuild_pinecone.py [--skip-wipe] [--skip-votes] [--yes]
    python scripts/rebuild_pinecone.py --content-types bill,legislator
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import structlog
from votebot.config import get_settings
from votebot.services.vector_store import VectorStoreService

logger = structlog.get_logger()

# Default content types in recommended sync order
DEFAULT_CONTENT_TYPES = [
    "bill",         # Must come first - creates bill-votes with person IDs
    "legislator",   # Creates legislator profiles with OpenStates IDs
    "organization", # Creates org profiles with bill positions
    "webpage",      # DDP website pages for general knowledge
    "training",     # Training documents for agent behavior
]


async def wipe_index(auto_confirm: bool = False) -> bool:
    """Delete all documents from Pinecone index."""
    settings = get_settings()
    vs = VectorStoreService(settings)

    # Get current stats
    stats = vs.index.describe_index_stats()
    total_vectors = stats.total_vector_count

    print(f"Current index has {total_vectors} vectors")

    if total_vectors == 0:
        print("Index is already empty")
        return True

    if not auto_confirm:
        confirm = input(f"Are you sure you want to delete ALL {total_vectors} vectors? (yes/no): ")
        if confirm.lower() != "yes":
            print("Aborted")
            return False

    print("Wiping index...")
    await vs.delete(delete_all=True)

    # Verify
    stats = vs.index.describe_index_stats()
    print(f"Index now has {stats.total_vector_count} vectors")
    return True


async def run_unified_sync(content_types: list[str]) -> dict:
    """
    Run batch sync for specified content types using direct service calls.

    Does not require the API to be running.
    """
    from votebot.sync.service import UnifiedSyncService
    from votebot.sync.types import ContentType, SyncMode, SyncOptions

    service = UnifiedSyncService()
    results = {}

    # Map string names to ContentType enum
    content_type_map = {
        "bill": ContentType.BILL,
        "legislator": ContentType.LEGISLATOR,
        "organization": ContentType.ORGANIZATION,
        "webpage": ContentType.WEBPAGE,
        "training": ContentType.TRAINING,
    }

    for ct_name in content_types:
        ct_enum = content_type_map.get(ct_name.lower())
        if not ct_enum:
            print(f"Warning: Unknown content type '{ct_name}', skipping")
            continue

        print(f"\n{'='*60}")
        print(f"SYNCING: {ct_name.upper()}")
        print('='*60)

        start = time.time()
        try:
            result = await service.sync(
                content_type=ct_enum,
                mode=SyncMode.BATCH,
                options=SyncOptions(),
            )
            elapsed = time.time() - start

            print(f"  Items processed: {result.items_processed}")
            print(f"  Items successful: {result.items_successful}")
            print(f"  Chunks created: {result.chunks_created}")
            print(f"  Success: {result.success}")
            print(f"  Duration: {elapsed:.1f}s")

            if result.errors:
                print(f"  Errors ({len(result.errors)}):")
                for err in result.errors[:5]:
                    print(f"    - {err[:100]}")
                if len(result.errors) > 5:
                    print(f"    ... and {len(result.errors) - 5} more")

            results[ct_name] = result

        except Exception as e:
            print(f"  ERROR: {e}")
            logger.exception(f"Failed to sync {ct_name}")

    return results


async def build_legislator_votes() -> dict:
    """Build the legislator-votes reverse index from bill-votes documents."""
    print(f"\n{'='*60}")
    print("BUILDING: LEGISLATOR-VOTES REVERSE INDEX")
    print('='*60)

    from votebot.sync.build_legislator_votes import LegislatorVotesBuilder

    start = time.time()
    builder = LegislatorVotesBuilder()
    result = await builder.build()
    elapsed = time.time() - start

    print(f"  Legislators processed: {result.get('legislators_processed', 0)}")
    print(f"  Documents created: {result.get('documents_created', 0)}")
    print(f"  Chunks created: {result.get('chunks_created', 0)}")
    print(f"  Success: {result.get('success', False)}")
    print(f"  Duration: {elapsed:.1f}s")

    if result.get('errors'):
        print(f"  Errors: {result['errors'][:3]}")

    return result


async def main():
    parser = argparse.ArgumentParser(
        description="Rebuild Pinecone index with all VoteBot content",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Content types (in recommended order):
  bill         - Bills from Webflow + OpenStates (votes, history, PDFs)
  legislator   - Legislators from Webflow + OpenStates
  organization - Organizations from Webflow (with bill positions)
  webpage      - DDP website pages (about, faq, etc.)
  training     - Training documents from local files

Examples:
  python scripts/rebuild_pinecone.py              # Full rebuild with prompts
  python scripts/rebuild_pinecone.py --yes        # Full rebuild, no prompts
  python scripts/rebuild_pinecone.py --skip-wipe  # Rebuild without wiping
  python scripts/rebuild_pinecone.py --content-types bill,legislator
        """
    )
    parser.add_argument(
        "--skip-wipe",
        action="store_true",
        help="Skip wiping the index (add to existing data)"
    )
    parser.add_argument(
        "--content-types",
        default=",".join(DEFAULT_CONTENT_TYPES),
        help=f"Comma-separated list of content types (default: {','.join(DEFAULT_CONTENT_TYPES)})"
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Skip the content sync step"
    )
    parser.add_argument(
        "--skip-votes",
        action="store_true",
        help="Skip building legislator-votes reverse index"
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Auto-confirm all prompts (for non-interactive use)"
    )

    args = parser.parse_args()
    total_start = time.time()

    print("="*60)
    print("VOTEBOT PINECONE REBUILD")
    print("="*60)

    # Parse content types
    content_types = [ct.strip() for ct in args.content_types.split(",") if ct.strip()]

    print(f"\nContent types to sync: {content_types}")
    print(f"Skip wipe: {args.skip_wipe}")
    print(f"Skip votes: {args.skip_votes}")

    if not args.yes:
        confirm = input("\nProceed with rebuild? (yes/no): ")
        if confirm.lower() != "yes":
            print("Aborted")
            return

    # Step 1: Wipe index
    if not args.skip_wipe:
        print("\n" + "="*60)
        print("STEP 1: WIPE INDEX")
        print("="*60)
        wiped = await wipe_index(auto_confirm=args.yes)
        if not wiped:
            return
    else:
        print("\n[Skipping wipe]")

    # Step 2: Sync content types
    if not args.skip_sync:
        print("\n" + "="*60)
        print("STEP 2: SYNC CONTENT")
        print("="*60)
        sync_results = await run_unified_sync(content_types)

        # Summary
        print("\n" + "-"*40)
        print("SYNC SUMMARY:")
        total_chunks = 0
        for ct, result in sync_results.items():
            status = "✓" if result.success else "✗"
            print(f"  {status} {ct}: {result.chunks_created} chunks")
            total_chunks += result.chunks_created
        print(f"  Total chunks: {total_chunks}")
    else:
        print("\n[Skipping sync]")

    # Step 3: Build legislator-votes reverse index
    if not args.skip_votes:
        print("\n" + "="*60)
        print("STEP 3: BUILD REVERSE INDEX")
        print("="*60)
        votes_result = await build_legislator_votes()
    else:
        print("\n[Skipping legislator-votes build]")

    # Final stats
    settings = get_settings()
    vs = VectorStoreService(settings)
    stats = vs.index.describe_index_stats()
    total_time = time.time() - total_start

    print("\n" + "="*60)
    print("REBUILD COMPLETE")
    print("="*60)
    print(f"  Total vectors in index: {stats.total_vector_count}")
    print(f"  Total time: {total_time:.1f}s ({total_time/60:.1f} minutes)")


if __name__ == "__main__":
    asyncio.run(main())
