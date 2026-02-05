#!/usr/bin/env python3
"""
Wipe Pinecone index and rebuild all data from scratch.

Usage:
    python scripts/rebuild_pinecone.py [--skip-wipe] [--content-types bill,legislator,...]
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import structlog
from votebot.config import get_settings
from votebot.services.vector_store import VectorStoreService

logger = structlog.get_logger()


async def wipe_index():
    """Delete all documents from Pinecone index."""
    settings = get_settings()
    vs = VectorStoreService(settings)

    # Get current stats
    stats = vs.index.describe_index_stats()
    total_vectors = stats.total_vector_count

    print(f"Current index has {total_vectors} vectors")

    if total_vectors == 0:
        print("Index is already empty")
        return

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


async def run_sync(content_types: list[str]):
    """Run batch sync for specified content types."""
    import httpx

    settings = get_settings()
    base_url = f"http://127.0.0.1:{settings.port}"

    async with httpx.AsyncClient(timeout=600.0) as client:
        for content_type in content_types:
            print(f"\n{'='*60}")
            print(f"Syncing {content_type}...")
            print('='*60)

            try:
                response = await client.post(
                    f"{base_url}/votebot/v1/sync/unified",
                    json={
                        "content_type": content_type,
                        "mode": "batch",
                    }
                )
                response.raise_for_status()
                result = response.json()
                print(f"Result: {result}")
            except Exception as e:
                print(f"Error syncing {content_type}: {e}")


async def build_legislator_votes():
    """Build the legislator-votes reverse index."""
    print(f"\n{'='*60}")
    print("Building legislator-votes reverse index...")
    print('='*60)

    from votebot.sync.build_legislator_votes import LegislatorVotesBuilder

    builder = LegislatorVotesBuilder()
    result = await builder.build()

    print(f"Result: {result}")


async def main():
    parser = argparse.ArgumentParser(description="Rebuild Pinecone index")
    parser.add_argument("--skip-wipe", action="store_true", help="Skip wiping the index")
    parser.add_argument(
        "--content-types",
        default="bill,legislator,organization",
        help="Comma-separated list of content types to sync"
    )
    parser.add_argument("--skip-sync", action="store_true", help="Skip the sync step")
    parser.add_argument("--skip-votes", action="store_true", help="Skip building legislator-votes")

    args = parser.parse_args()

    # Wipe
    if not args.skip_wipe:
        wiped = await wipe_index()
        if wiped is False:
            return

    # Sync
    if not args.skip_sync:
        content_types = [ct.strip() for ct in args.content_types.split(",")]
        print(f"\nWill sync: {content_types}")
        print("Note: VoteBot API must be running on localhost")

        confirm = input("Proceed with sync? (yes/no): ")
        if confirm.lower() != "yes":
            print("Aborted")
            return

        await run_sync(content_types)

    # Build legislator-votes
    if not args.skip_votes:
        confirm = input("\nBuild legislator-votes reverse index? (yes/no): ")
        if confirm.lower() == "yes":
            await build_legislator_votes()

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
