#!/usr/bin/env python3
"""
One-shot script to flush bill-history vectors from Pinecone.

Companion to Fix F2 in PLAN-quick-action-buttons. Run AFTER the ddp-sync
producer change (Fix F1) is deployed and verified — running before would
leave a window where the next sync repopulates the chunks.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/flush_bill_history.py
    PYTHONPATH=src .venv/bin/python scripts/flush_bill_history.py --yes  # skip prompt

Idempotent: safe to re-run if a partial delete leaves stragglers. The underlying
Pinecone metadata-filter delete is a set operation, so a second run completes
the cleanup.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from votebot.config import get_settings
from votebot.services.vector_store import VectorStoreService


BILL_HISTORY_FILTER = {"document_type": "bill-history"}
BILL_HISTORY_ID_PREFIX = "bill-history-"
RECORD_PATH = Path("logs/eval/flush_bill_history.json")

# Eventual-consistency settings: Pinecone delete may take a few seconds to
# propagate. Re-poll the count a few times before declaring partial.
POST_DELETE_POLL_ATTEMPTS = 5
POST_DELETE_POLL_INTERVAL_SEC = 3


def count_bill_history(vector_store: VectorStoreService) -> int:
    """Count bill-history vectors via paginated index.list() with prefix.

    Avoids the top_k=10000 cap that a query-based count would hit if the
    corpus ever grows past that threshold. Uses the same paginated API
    that `build_legislator_votes.py` uses for bill-votes.
    """
    total = 0
    for ids in vector_store.index.list(
        namespace=vector_store.namespace,
        prefix=BILL_HISTORY_ID_PREFIX,
    ):
        total += len(ids)
    return total


async def main(skip_confirm: bool) -> int:
    settings = get_settings()
    vector_store = VectorStoreService(settings)

    print(f"Pinecone index: {vector_store.index_name}")
    print(f"Namespace: {vector_store.namespace}")
    print()

    # Pre-flight count
    print("Counting bill-history vectors...")
    pre_count = count_bill_history(vector_store)
    print(f"  Found {pre_count} bill-history vectors")

    if pre_count == 0:
        print("\nNothing to delete. Pinecone is already clean.")
        record = {
            "timestamp": datetime.utcnow().isoformat(),
            "pre_count": 0,
            "post_count": 0,
            "deleted": 0,
            "status": "no-op",
        }
        RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
        RECORD_PATH.write_text(json.dumps(record, indent=2))
        print(f"  Record written to {RECORD_PATH}")
        return 0

    # Confirmation prompt unless --yes
    if not skip_confirm:
        print()
        response = input(f"Delete {pre_count} bill-history vectors? [yes/no]: ").strip().lower()
        if response not in ("yes", "y"):
            print("Aborted.")
            return 1

    # Execute delete (vector_store.delete already wraps tenacity retry)
    print(f"\nDeleting bill-history vectors via filter...")
    await vector_store.delete(filter=BILL_HISTORY_FILTER)
    print("  Delete call returned successfully.")

    # Post-delete verification with eventual-consistency retry.
    # Pinecone's metadata-filter delete may take a few seconds to propagate;
    # re-poll the count a few times before declaring partial.
    print("\nRe-counting to verify (polling for eventual consistency)...")
    post_count = pre_count
    for attempt in range(1, POST_DELETE_POLL_ATTEMPTS + 1):
        post_count = count_bill_history(vector_store)
        print(f"  Attempt {attempt}/{POST_DELETE_POLL_ATTEMPTS}: {post_count} remaining")
        if post_count == 0:
            break
        if attempt < POST_DELETE_POLL_ATTEMPTS:
            time.sleep(POST_DELETE_POLL_INTERVAL_SEC)

    deleted = pre_count - post_count
    print(f"\n  Pre-count: {pre_count}")
    print(f"  Post-count: {post_count}")
    print(f"  Deleted: {deleted}")

    # Persist a record for evaluate_production.py and audit trail
    record = {
        "timestamp": datetime.utcnow().isoformat(),
        "pre_count": pre_count,
        "post_count": post_count,
        "deleted": deleted,
        "status": "complete" if post_count == 0 else "partial",
    }
    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECORD_PATH.write_text(json.dumps(record, indent=2))
    print(f"\nRecord written to {RECORD_PATH}")

    if post_count > 0:
        print(
            f"\nWARNING: {post_count} bill-history vectors remain after delete. "
            "Re-running the script with the same filter is idempotent and should "
            "drive the count to zero. If a second run still leaves vectors, "
            "escalate — likely a Pinecone API issue requiring support contact."
        )
        return 2

    print("\nDone. Bill-history is fully removed from Pinecone.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Assume yes — skip the interactive confirmation prompt (for scripted runs).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(skip_confirm=args.yes)))
