"""Cache for federal legislator OpenStates person IDs.

OpenStates doesn't return person IDs in federal vote records (only voter_name),
but we can pre-fetch all US Congress members and cache their IDs for lookup.

The cache maps voter_name strings (as they appear in vote records) to person IDs.
For example: "Alsobrooks (D-MD)" -> "ocd-person/abc123..."
"""

import json
import re
from pathlib import Path

import httpx
import structlog

from votebot.config import Settings, get_settings

logger = structlog.get_logger()

# Cache file location
CACHE_DIR = Path(__file__).parent.parent.parent.parent / "data" / "cache"
CACHE_FILE = CACHE_DIR / "federal_legislators.json"


class FederalLegislatorCache:
    """
    Cache for federal legislator OpenStates person IDs.

    The cache maps multiple name formats to person IDs:
    - "Alsobrooks (D-MD)" - Senate vote format
    - "Alsobrooks" - Last name only
    - "Angela Alsobrooks" - Full name
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._cache: dict[str, dict] = {}
        self._name_to_id: dict[str, str] = {}  # Quick lookup by formatted name
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Load cache from disk if not already loaded."""
        if self._loaded:
            return

        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                    self._cache = data.get("legislators", {})
                    self._build_name_index()
                    logger.info(
                        "Loaded federal legislator cache",
                        count=len(self._cache),
                        name_variants=len(self._name_to_id),
                    )
            except Exception as e:
                logger.warning(f"Failed to load cache: {e}")
                self._cache = {}

        self._loaded = True

    def _build_name_index(self) -> None:
        """Build quick lookup index from name variants to person IDs."""
        self._name_to_id = {}

        for person_id, info in self._cache.items():
            name = info.get("name", "")
            party = info.get("party", "")
            state = info.get("state", "")

            party_abbrev = {"Democratic": "D", "Republican": "R", "Independent": "I"}.get(
                party, party[0] if party else ""
            )

            # Generate all name variants
            name_variants = self._generate_name_variants(name, party_abbrev, state)
            for variant in name_variants:
                # Normalize for lookup
                normalized = variant.lower().strip()
                if normalized and normalized not in self._name_to_id:
                    self._name_to_id[normalized] = person_id

    def _generate_name_variants(
        self, full_name: str, party_abbrev: str, state: str
    ) -> list[str]:
        """
        Generate all name variants for a legislator.

        For "Angela Alsobrooks" with party "D" and state "MD":
        - "Alsobrooks (D-MD)" - Senate vote format
        - "Alsobrooks" - Last name only
        - "Angela Alsobrooks" - Full name
        """
        variants = []
        if not full_name:
            return variants

        # Full name
        variants.append(full_name)

        # Extract last name (handle suffixes like Jr., III)
        name_parts = full_name.split()
        suffixes = {"jr.", "jr", "sr.", "sr", "ii", "iii", "iv", "v"}

        # Find last name (last word that's not a suffix)
        last_name = ""
        for part in reversed(name_parts):
            if part.lower().rstrip(".") not in suffixes:
                last_name = part
                break

        if last_name:
            # Last name only
            variants.append(last_name)

            # Last name with party-state (Senate vote format)
            if party_abbrev and state:
                variants.append(f"{last_name} ({party_abbrev}-{state})")

            # Last name with just party
            if party_abbrev:
                variants.append(f"{last_name} ({party_abbrev})")

        return variants

    def lookup(self, voter_name: str) -> str | None:
        """
        Look up a person ID by voter name.

        Args:
            voter_name: Name as it appears in vote record, e.g., "Alsobrooks (D-MD)"

        Returns:
            OpenStates person ID if found, None otherwise
        """
        self._ensure_loaded()

        normalized = voter_name.lower().strip()
        return self._name_to_id.get(normalized)

    def lookup_with_info(self, voter_name: str) -> dict | None:
        """
        Look up full legislator info by voter name.

        Returns dict with: person_id, name, party, state, chamber
        """
        self._ensure_loaded()

        person_id = self.lookup(voter_name)
        if person_id and person_id in self._cache:
            info = self._cache[person_id].copy()
            info["person_id"] = person_id
            return info

        return None

    def get_all(self) -> dict[str, dict]:
        """Get all cached legislators."""
        self._ensure_loaded()
        return self._cache.copy()

    def get_by_person_id(self, person_id: str) -> dict | None:
        """
        Look up legislator info by OpenStates person ID.

        Args:
            person_id: OpenStates person ID, e.g., "ocd-person/abc123..."

        Returns:
            Dict with name, party, state, chamber if found, None otherwise
        """
        self._ensure_loaded()
        return self._cache.get(person_id)

    async def refresh(self) -> dict:
        """
        Refresh the cache by fetching all US Congress members from OpenStates.

        Returns:
            Stats about the refresh operation
        """
        logger.info("Refreshing federal legislator cache from OpenStates")

        api_key = self.settings.openstates_api_key.get_secret_value()
        if not api_key:
            return {"success": False, "error": "OpenStates API key not configured"}

        legislators: dict[str, dict] = {}
        stats = {"senate": 0, "house": 0, "errors": []}

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Fetch Senate members
            senate_result = await self._fetch_chamber_members(
                client, api_key, "upper", stats
            )
            legislators.update(senate_result)
            stats["senate"] = len(senate_result)

            # Fetch House members
            house_result = await self._fetch_chamber_members(
                client, api_key, "lower", stats
            )
            legislators.update(house_result)
            stats["house"] = len(house_result)

        # Save to cache file
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        cache_data = {
            "legislators": legislators,
            "refreshed_at": __import__("datetime").datetime.now().isoformat(),
            "count": len(legislators),
        }

        with open(CACHE_FILE, "w") as f:
            json.dump(cache_data, f, indent=2)

        # Update in-memory cache
        self._cache = legislators
        self._build_name_index()
        self._loaded = True

        logger.info(
            "Federal legislator cache refreshed",
            total=len(legislators),
            senate=stats["senate"],
            house=stats["house"],
            name_variants=len(self._name_to_id),
        )

        return {
            "success": True,
            "total": len(legislators),
            "senate": stats["senate"],
            "house": stats["house"],
            "name_variants": len(self._name_to_id),
            "errors": stats["errors"][:5] if stats["errors"] else [],
        }

    async def _fetch_chamber_members(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        chamber: str,  # "upper" (Senate) or "lower" (House)
        stats: dict,
    ) -> dict[str, dict]:
        """Fetch all members of a chamber from OpenStates."""
        legislators = {}
        page = 1
        per_page = 50

        chamber_name = "Senate" if chamber == "upper" else "House"
        logger.info(f"Fetching US {chamber_name} members")

        while True:
            try:
                response = await client.get(
                    "https://v3.openstates.org/people",
                    params={
                        "jurisdiction": "us",
                        "org_classification": chamber,
                        "page": page,
                        "per_page": per_page,
                    },
                    headers={"X-API-KEY": api_key},
                )
                response.raise_for_status()
                data = response.json()

                results = data.get("results", [])
                if not results:
                    break

                for person in results:
                    person_id = person.get("id", "")
                    if not person_id:
                        continue

                    # Extract state from current role
                    state = ""
                    current_role = person.get("current_role", {})
                    if current_role:
                        # District format: "ocd-division/.../state:md/..."
                        district = current_role.get("district", "")
                        division = current_role.get("division_id", "")

                        # Try to extract state from division_id
                        state_match = re.search(r"/state:([a-z]{2})", division)
                        if state_match:
                            state = state_match.group(1).upper()

                    legislators[person_id] = {
                        "name": person.get("name", ""),
                        "party": person.get("party", ""),
                        "state": state,
                        "chamber": chamber_name.lower(),
                        "image": person.get("image", ""),
                    }

                # Check if there are more pages
                pagination = data.get("pagination", {})
                total_pages = pagination.get("max_page", 1)

                if page >= total_pages:
                    break

                page += 1

                # Rate limiting
                await __import__("asyncio").sleep(0.5)

            except httpx.HTTPStatusError as e:
                error_msg = f"HTTP error fetching {chamber_name} page {page}: {e}"
                logger.error(error_msg)
                stats["errors"].append(error_msg)
                break
            except Exception as e:
                error_msg = f"Error fetching {chamber_name} page {page}: {e}"
                logger.error(error_msg)
                stats["errors"].append(error_msg)
                break

        return legislators


# Singleton instance
_cache_instance: FederalLegislatorCache | None = None


def get_federal_cache() -> FederalLegislatorCache:
    """Get the singleton federal legislator cache instance."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = FederalLegislatorCache()
    return _cache_instance


async def main():
    """CLI entry point for refreshing the federal legislator cache."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Refresh the federal legislator cache from OpenStates"
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Show current cache contents instead of refreshing",
    )

    args = parser.parse_args()

    cache = get_federal_cache()

    if args.show:
        legislators = cache.get_all()
        print(f"\n=== Federal Legislator Cache ({len(legislators)} legislators) ===")
        for person_id, info in list(legislators.items())[:20]:
            print(f"  {info['name']} ({info['party']}-{info['state']}) [{info['chamber']}]")
            print(f"    ID: {person_id}")
        if len(legislators) > 20:
            print(f"  ... and {len(legislators) - 20} more")
    else:
        results = await cache.refresh()
        print("\n=== Refresh Results ===")
        for key, value in results.items():
            print(f"{key}: {value}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
