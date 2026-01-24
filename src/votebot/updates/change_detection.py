"""Change detection for content updates."""

import hashlib
import json
from datetime import datetime
from typing import Any

import httpx
import structlog

from votebot.config import Settings, get_settings

logger = structlog.get_logger()


class ChangeDetector:
    """
    Detect changes in external data sources.

    Strategies:
    - Content hash comparison
    - Last-modified header checking
    - API-specific change detection
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the change detector.

        Args:
            settings: Application settings
        """
        self.settings = settings or get_settings()
        self._hash_store: dict[str, str] = {}
        self._last_check: dict[str, datetime] = {}
        self._last_update: dict[str, datetime] = {}

    async def check_source(self, source: str) -> bool:
        """
        Check if a source has changes since last update.

        Args:
            source: Source name to check

        Returns:
            True if changes detected
        """
        check_methods = {
            "congress": self._check_congress_changes,
            "openstates": self._check_openstates_changes,
            "webflow": self._check_webflow_changes,
        }

        if source not in check_methods:
            logger.warning(f"Unknown source: {source}, assuming changes exist")
            return True

        try:
            has_changes = await check_methods[source]()
            self._last_check[source] = datetime.utcnow()

            logger.info(
                "Change detection completed",
                source=source,
                has_changes=has_changes,
            )

            return has_changes

        except Exception as e:
            logger.error(
                "Change detection failed",
                source=source,
                error=str(e),
            )
            # Assume changes on error to be safe
            return True

    async def mark_updated(self, source: str) -> None:
        """
        Mark a source as successfully updated.

        Args:
            source: Source name that was updated
        """
        self._last_update[source] = datetime.utcnow()
        logger.debug(f"Marked {source} as updated")

    def get_last_update(self, source: str) -> datetime | None:
        """Get the last update time for a source."""
        return self._last_update.get(source)

    def get_last_check(self, source: str) -> datetime | None:
        """Get the last change check time for a source."""
        return self._last_check.get(source)

    async def _check_congress_changes(self) -> bool:
        """Check Congress.gov for changes."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            api_key = self.settings.congress_api_key.get_secret_value()
            if not api_key:
                return True  # Can't check without API key

            # Check latest bills
            try:
                response = await client.get(
                    "https://api.congress.gov/v3/bill",
                    params={
                        "api_key": api_key,
                        "format": "json",
                        "limit": 10,
                        "sort": "updateDate+desc",
                    },
                )
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                logger.warning(f"Congress API check failed: {e}")
                return True

            # Create hash of latest items
            bills = data.get("bills", [])
            content_hash = self._hash_content(json.dumps(bills, sort_keys=True))

            # Compare with stored hash
            stored_hash = self._hash_store.get("congress")
            if stored_hash != content_hash:
                self._hash_store["congress"] = content_hash
                return True

            return False

    async def _check_openstates_changes(self) -> bool:
        """Check OpenStates for changes."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            api_key = self.settings.openstates_api_key.get_secret_value()
            if not api_key:
                return True

            try:
                response = await client.get(
                    "https://v3.openstates.org/bills",
                    headers={"X-API-Key": api_key},
                    params={
                        "per_page": 10,
                        "sort": "updated_at",
                    },
                )
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                logger.warning(f"OpenStates API check failed: {e}")
                return True

            # Create hash
            bills = data.get("results", [])
            content_hash = self._hash_content(json.dumps(bills, sort_keys=True))

            stored_hash = self._hash_store.get("openstates")
            if stored_hash != content_hash:
                self._hash_store["openstates"] = content_hash
                return True

            return False

    async def _check_webflow_changes(self) -> bool:
        """Check Webflow for changes."""
        # Webflow doesn't have a simple change detection API
        # Could use webhooks in production
        # For now, always return True to check
        return True

    def _hash_content(self, content: str) -> str:
        """Create a hash of content for comparison."""
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    async def check_document_changed(
        self,
        document_id: str,
        content: str,
    ) -> bool:
        """
        Check if a specific document's content has changed.

        Args:
            document_id: Document identifier
            content: Current content to check

        Returns:
            True if content has changed
        """
        content_hash = self._hash_content(content)
        stored_hash = self._hash_store.get(f"doc:{document_id}")

        if stored_hash != content_hash:
            self._hash_store[f"doc:{document_id}"] = content_hash
            return True

        return False

    async def check_url_changed(
        self,
        url: str,
        use_etag: bool = True,
    ) -> bool:
        """
        Check if content at a URL has changed.

        Args:
            url: URL to check
            use_etag: Whether to use ETag/Last-Modified headers

        Returns:
            True if content has changed
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                # First try HEAD request with conditional headers
                stored_etag = self._hash_store.get(f"etag:{url}")
                headers = {}

                if use_etag and stored_etag:
                    headers["If-None-Match"] = stored_etag

                response = await client.head(url, headers=headers)

                # 304 Not Modified means no changes
                if response.status_code == 304:
                    return False

                # Store new ETag if present
                new_etag = response.headers.get("ETag")
                if new_etag:
                    if stored_etag == new_etag:
                        return False
                    self._hash_store[f"etag:{url}"] = new_etag
                    return True

                # Fall back to content hash
                response = await client.get(url)
                response.raise_for_status()
                content_hash = self._hash_content(response.text)

                stored_hash = self._hash_store.get(f"url:{url}")
                if stored_hash != content_hash:
                    self._hash_store[f"url:{url}"] = content_hash
                    return True

                return False

            except Exception as e:
                logger.warning(f"URL change check failed for {url}: {e}")
                return True  # Assume changed on error

    def clear_hashes(self) -> None:
        """Clear all stored hashes."""
        self._hash_store.clear()
        logger.info("Hash store cleared")

    def get_stats(self) -> dict[str, Any]:
        """Get change detection statistics."""
        return {
            "hash_count": len(self._hash_store),
            "last_checks": {k: v.isoformat() for k, v in self._last_check.items()},
            "last_updates": {k: v.isoformat() for k, v in self._last_update.items()},
        }
