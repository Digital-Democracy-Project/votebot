#!/usr/bin/env python3
"""
Refresh OpenStates legislator cache with full data.

This script fetches complete legislator data from OpenStates API and saves
it to a JSON cache file for use with sync_legislators.py --use-cache.

Run this script when OpenStates rate limits have reset (typically daily).

Usage:
    python scripts/refresh_openstates_cache.py [options]

Options:
    --state STATE      Fetch legislators for specific state (e.g., fl, wa)
    --all-states       Fetch legislators for all states in Webflow
    --rate-limit N     Seconds between API calls (default: 2.0)
    --output FILE      Output file (default: scripts/legislators_openstates.json)
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx
import structlog

from votebot.config import get_settings

get_settings.cache_clear()

logger = structlog.get_logger()

# States with legislators in Webflow
WEBFLOW_STATES = ["fl", "wa", "va", "mi", "ma", "ut", "az", "al"]


async def fetch_legislators_for_state(
    client: httpx.AsyncClient,
    api_key: str,
    state: str,
    rate_limit: float = 2.0,
) -> list[dict]:
    """
    Fetch all legislators for a state from OpenStates API.

    Args:
        client: httpx AsyncClient
        api_key: OpenStates API key
        state: State abbreviation (e.g., 'fl')
        rate_limit: Seconds between API calls

    Returns:
        List of legislator dicts with full data
    """
    legislators = []
    page = 1
    per_page = 50

    headers = {"X-API-Key": api_key}
    base_url = "https://v3.openstates.org"

    logger.info(f"Fetching legislators for {state.upper()}...")

    while True:
        try:
            params = {
                "jurisdiction": state,
                "per_page": per_page,
                "page": page,
            }

            response = await client.get(
                f"{base_url}/people",
                headers=headers,
                params=params,
            )

            if response.status_code == 429:
                logger.warning(f"Rate limited, waiting 60s...")
                await asyncio.sleep(60)
                continue

            response.raise_for_status()
            data = response.json()

            results = data.get("results", [])
            if not results:
                break

            # Fetch full details for each legislator
            for person in results:
                person_id = person.get("id")
                if person_id:
                    await asyncio.sleep(rate_limit)

                    try:
                        detail_response = await client.get(
                            f"{base_url}/people/{person_id}",
                            headers=headers,
                        )

                        if detail_response.status_code == 429:
                            logger.warning(f"Rate limited on {person_id}, waiting 60s...")
                            await asyncio.sleep(60)
                            detail_response = await client.get(
                                f"{base_url}/people/{person_id}",
                                headers=headers,
                            )

                        if detail_response.status_code == 200:
                            full_person = detail_response.json()
                            full_person["_jurisdiction"] = state.upper()
                            legislators.append(full_person)
                            logger.debug(f"Fetched: {full_person.get('name')}")
                        else:
                            # Use basic data
                            person["_jurisdiction"] = state.upper()
                            legislators.append(person)

                    except Exception as e:
                        logger.warning(f"Failed to fetch details for {person_id}: {e}")
                        person["_jurisdiction"] = state.upper()
                        legislators.append(person)

            logger.info(f"  {state.upper()}: Fetched page {page}, {len(results)} legislators")

            # Check if there are more pages
            pagination = data.get("pagination", {})
            if page >= pagination.get("max_page", 1):
                break

            page += 1
            await asyncio.sleep(rate_limit)

        except Exception as e:
            logger.error(f"Error fetching {state} page {page}: {e}")
            break

    logger.info(f"  {state.upper()}: Total {len(legislators)} legislators")
    return legislators


async def refresh_cache(
    states: list[str],
    output_file: Path,
    rate_limit: float = 2.0,
) -> dict:
    """
    Refresh the OpenStates cache for specified states.

    Args:
        states: List of state abbreviations
        output_file: Path to output JSON file
        rate_limit: Seconds between API calls

    Returns:
        Summary dict with stats
    """
    settings = get_settings()
    api_key = settings.openstates_api_key.get_secret_value()

    if not api_key:
        logger.error("No OpenStates API key configured")
        return {"error": "No API key"}

    all_legislators = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        for state in states:
            legislators = await fetch_legislators_for_state(
                client, api_key, state, rate_limit
            )
            all_legislators.extend(legislators)

            # Longer pause between states
            await asyncio.sleep(rate_limit * 2)

    # Save to cache file
    logger.info(f"Saving {len(all_legislators)} legislators to {output_file}")
    with open(output_file, "w") as f:
        json.dump(all_legislators, f, indent=2)

    return {
        "total_legislators": len(all_legislators),
        "states": states,
        "output_file": str(output_file),
    }


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Refresh OpenStates legislator cache",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--state",
        help="Fetch legislators for specific state (e.g., fl, wa)",
    )
    parser.add_argument(
        "--all-states",
        action="store_true",
        help="Fetch legislators for all Webflow states",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=2.0,
        help="Seconds between API calls (default: 2.0)",
    )
    parser.add_argument(
        "--output",
        default="scripts/legislators_openstates.json",
        help="Output file path",
    )

    args = parser.parse_args()

    if args.state:
        states = [args.state.lower()]
    elif args.all_states:
        states = WEBFLOW_STATES
    else:
        print("Specify --state STATE or --all-states")
        sys.exit(1)

    output_file = Path(args.output)

    print("=" * 70)
    print("OPENSTATES CACHE REFRESH")
    print("=" * 70)
    print(f"\nStates: {', '.join(s.upper() for s in states)}")
    print(f"Rate limit: {args.rate_limit}s between requests")
    print(f"Output: {output_file}")
    print()

    try:
        result = await refresh_cache(states, output_file, args.rate_limit)

        print("\n" + "=" * 70)
        print("REFRESH COMPLETE")
        print("=" * 70)
        print(f"\nTotal legislators cached: {result.get('total_legislators', 0)}")
        print(f"Output file: {result.get('output_file')}")

    except Exception as e:
        print(f"\nRefresh failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
