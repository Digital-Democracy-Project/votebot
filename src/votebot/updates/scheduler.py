"""Scheduler for content updates and OpenStates bill sync."""

import asyncio
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable

import structlog
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from votebot.config import Settings, get_settings
from votebot.ingestion.pipeline import IngestionPipeline, IngestionResult
from votebot.updates.change_detection import ChangeDetector
from votebot.utils.legislative_calendar import StateLegislativeCalendar

logger = structlog.get_logger()

# Default config path
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "sync_schedule.yaml"


class UpdateScheduler:
    """
    Scheduler for periodic content updates.

    Handles:
    - Daily OpenStates bill sync (based on legislative calendar)
    - Hourly polling for content changes
    - Manual update triggers
    - Graceful shutdown
    """

    def __init__(
        self,
        settings: Settings | None = None,
        config_path: Path | None = None,
    ):
        """
        Initialize the update scheduler.

        Args:
            settings: Application settings
            config_path: Path to sync_schedule.yaml config file
        """
        self.settings = settings or get_settings()
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.scheduler = AsyncIOScheduler()
        self.pipeline = IngestionPipeline(self.settings)
        self.change_detector = ChangeDetector(self.settings)
        self.calendar = StateLegislativeCalendar()
        self._is_running = False
        self._update_callbacks: list[Callable] = []
        self._sync_config = self._load_sync_config()

    def _load_sync_config(self) -> dict[str, Any]:
        """Load sync schedule configuration from YAML file."""
        if not self.config_path.exists():
            logger.warning(f"Sync config not found at {self.config_path}, using defaults")
            return {
                "sync_time_utc": "04:00",
                "US": {
                    "enabled": True,
                    "frequency": "daily",
                    "congress_number": 119,
                },
            }

        try:
            with open(self.config_path) as f:
                config = yaml.safe_load(f)
                logger.info(f"Loaded sync config from {self.config_path}")
                return config or {}
        except Exception as e:
            logger.error(f"Failed to load sync config: {e}")
            return {}

    def start(self) -> None:
        """Start the scheduler."""
        if self._is_running:
            logger.warning("Scheduler already running")
            return

        # Parse sync time from config
        sync_time_str = self._sync_config.get("sync_time_utc", "04:00")
        hour, minute = map(int, sync_time_str.split(":"))

        # Add daily OpenStates sync job
        self.scheduler.add_job(
            self._run_openstates_sync,
            trigger=CronTrigger(hour=hour, minute=minute),
            id="daily_openstates_sync",
            name="Daily OpenStates Bill Sync",
            replace_existing=True,
        )

        # Add hourly content update job
        self.scheduler.add_job(
            self._run_updates,
            trigger=IntervalTrigger(hours=1),
            id="hourly_update",
            name="Hourly Content Update",
            replace_existing=True,
        )

        # Add legislator sync job (daily or weekly based on config)
        # Prefer new legislator_sync config, fall back to legacy legislator_bills
        leg_config = self._sync_config.get("legislator_sync", {})
        if not leg_config:
            leg_config = self._sync_config.get("legislator_bills", {})

        if leg_config.get("enabled", False):
            leg_sync_time = leg_config.get("sync_time_utc", "06:00")
            leg_hour, leg_minute = map(int, leg_sync_time.split(":"))
            frequency = leg_config.get("frequency", "weekly")

            # Map day name to cron day_of_week (0=Monday, 6=Sunday)
            day_map = {
                "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6
            }

            if frequency == "daily":
                self.scheduler.add_job(
                    self._run_legislator_bills_sync,
                    trigger=CronTrigger(hour=leg_hour, minute=leg_minute),
                    id="daily_legislator_sync",
                    name="Daily Legislator Sync",
                    replace_existing=True,
                )
                logger.info(
                    "Legislator sync scheduled (daily)",
                    sync_time=leg_sync_time,
                )
            else:
                sync_day = leg_config.get("sync_day", "sunday")
                day_of_week = day_map.get(sync_day.lower(), 6)
                self.scheduler.add_job(
                    self._run_legislator_bills_sync,
                    trigger=CronTrigger(
                        day_of_week=day_of_week,
                        hour=leg_hour,
                        minute=leg_minute,
                    ),
                    id="weekly_legislator_sync",
                    name="Weekly Legislator Sync",
                    replace_existing=True,
                )
                logger.info(
                    "Legislator sync scheduled (weekly)",
                    sync_time=leg_sync_time,
                    sync_day=sync_day,
                )

        # Add organization sync job (monthly based on config)
        org_config = self._sync_config.get("organization_sync", {})
        if org_config.get("enabled", False):
            org_sync_time = org_config.get("sync_time_utc", "08:00")
            org_hour, org_minute = map(int, org_sync_time.split(":"))
            org_day_of_month = org_config.get("day_of_month", 1)

            self.scheduler.add_job(
                self._run_organization_sync,
                trigger=CronTrigger(
                    day=org_day_of_month,
                    hour=org_hour,
                    minute=org_minute,
                ),
                id="monthly_organization_sync",
                name="Monthly Organization Sync",
                replace_existing=True,
            )
            logger.info(
                "Organization sync scheduled (monthly)",
                sync_time=org_sync_time,
                day_of_month=org_day_of_month,
            )

        self.scheduler.start()
        self._is_running = True

        logger.info(
            "Update scheduler started",
            openstates_sync_time=sync_time_str,
        )

    def stop(self) -> None:
        """Stop the scheduler."""
        if not self._is_running:
            return

        self.scheduler.shutdown(wait=True)
        self._is_running = False

        logger.info("Update scheduler stopped")

    def add_callback(self, callback: Callable) -> None:
        """
        Add a callback to be called after updates complete.

        Args:
            callback: Async function to call with update results
        """
        self._update_callbacks.append(callback)

    async def trigger_update(
        self,
        sources: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, IngestionResult]:
        """
        Manually trigger an update.

        Args:
            sources: Specific sources to update (None for all)
            force: Force update even if no changes detected

        Returns:
            Dict mapping source names to results
        """
        logger.info(
            "Manual update triggered",
            sources=sources,
            force=force,
        )

        return await self._run_updates(sources=sources, force=force)

    async def _run_updates(
        self,
        sources: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, IngestionResult]:
        """
        Run the update process.

        Args:
            sources: Specific sources to update
            force: Force update even without changes

        Returns:
            Dict mapping source names to results
        """
        start_time = datetime.utcnow()
        results: dict[str, IngestionResult] = {}

        # Default to all sources if none specified
        if sources is None:
            sources = ["congress", "openstates"]

        logger.info(
            "Starting scheduled update",
            sources=sources,
            timestamp=start_time.isoformat(),
        )

        for source in sources:
            try:
                # Check for changes (unless forced)
                if not force:
                    has_changes = await self.change_detector.check_source(source)
                    if not has_changes:
                        logger.info(f"No changes detected for {source}, skipping")
                        continue

                # Run ingestion
                result = await self._update_source(source)
                results[source] = result

                # Record successful update
                if result.errors:
                    logger.warning(
                        f"Update completed with errors for {source}",
                        errors=result.errors,
                    )
                else:
                    await self.change_detector.mark_updated(source)

            except Exception as e:
                logger.exception(f"Failed to update {source}", error=str(e))
                results[source] = IngestionResult(
                    documents_processed=0,
                    chunks_created=0,
                    chunks_upserted=0,
                    errors=[str(e)],
                )

        # Calculate duration
        duration = (datetime.utcnow() - start_time).total_seconds()

        logger.info(
            "Scheduled update completed",
            duration_seconds=duration,
            sources_updated=list(results.keys()),
        )

        # Call callbacks
        for callback in self._update_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(results)
                else:
                    callback(results)
            except Exception as e:
                logger.error("Update callback failed", error=str(e))

        return results

    async def _update_source(self, source: str) -> IngestionResult:
        """
        Update a specific source.

        Args:
            source: Source name to update

        Returns:
            IngestionResult with update stats
        """
        source_configs = {
            "congress": {
                "congress": 119,  # Current Congress
                "limit": 50,
            },
            "openstates": {
                "limit": 50,
            },
        }

        config = source_configs.get(source, {})
        return await self.pipeline.ingest_from_source(source, config)

    async def _run_openstates_sync(self) -> dict[str, Any]:
        """
        Run the daily OpenStates bill sync.

        Only syncs bills from current sessions in jurisdictions
        that are currently in session or scheduled for sync.

        Returns:
            Dict with sync results
        """
        from votebot.ingestion.sources.webflow import WebflowSource
        from votebot.updates.bill_sync import BillSyncService

        start_time = datetime.utcnow()
        logger.info("Starting daily OpenStates sync")

        try:
            # Initialize services
            sync_service = BillSyncService(self.settings)
            webflow = WebflowSource(self.settings)

            # Fetch all bills from Webflow
            bills = []
            async for doc in webflow.fetch(include_pdfs=False):
                # We need the raw bill data, not processed docs
                pass

            # Actually fetch raw items from Webflow
            import httpx

            async with httpx.AsyncClient(timeout=60.0) as client:
                headers = {
                    "Authorization": f"Bearer {self.settings.webflow_api_key.get_secret_value()}",
                    "accept": "application/json",
                }

                offset = 0
                while True:
                    response = await client.get(
                        f"https://api.webflow.com/v2/collections/{self.settings.webflow_bills_collection_id}/items",
                        headers=headers,
                        params={"limit": 100, "offset": offset},
                    )

                    if response.status_code != 200:
                        break

                    data = response.json()
                    items = data.get("items", [])

                    if not items:
                        break

                    bills.extend(items)
                    offset += 100

                    if len(items) < 100:
                        break

            logger.info(f"Fetched {len(bills)} bills from Webflow CMS")

            # Sync current session bills only
            result = await sync_service.sync_current_session_bills(bills)

            duration = (datetime.utcnow() - start_time).total_seconds()

            logger.info(
                "Daily OpenStates sync completed",
                duration_seconds=duration,
                total_bills=result.total_bills,
                successful=result.successful,
                failed=result.failed,
                chunks_created=result.chunks_created,
            )

            # Call callbacks
            for callback in self._update_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback({"openstates_sync": result})
                    else:
                        callback({"openstates_sync": result})
                except Exception as e:
                    logger.error("Sync callback failed", error=str(e))

            return {
                "success": True,
                "duration_seconds": duration,
                "total_bills": result.total_bills,
                "successful": result.successful,
                "failed": result.failed,
                "chunks_created": result.chunks_created,
                "errors": result.errors[:10] if result.errors else [],
            }

        except Exception as e:
            logger.exception("OpenStates sync failed", error=str(e))
            return {
                "success": False,
                "error": str(e),
            }

    async def trigger_openstates_sync(self, force_all: bool = False) -> dict[str, Any]:
        """
        Manually trigger an OpenStates sync.

        Args:
            force_all: If True, sync all bills regardless of session

        Returns:
            Dict with sync results
        """
        logger.info("Manual OpenStates sync triggered", force_all=force_all)

        if force_all:
            # Use backload logic for all bills
            from votebot.ingestion.sources.webflow import WebflowSource
            from votebot.updates.bill_sync import BillSyncService

            sync_service = BillSyncService(self.settings)

            # Fetch all bills
            import httpx

            bills = []
            async with httpx.AsyncClient(timeout=60.0) as client:
                headers = {
                    "Authorization": f"Bearer {self.settings.webflow_api_key.get_secret_value()}",
                    "accept": "application/json",
                }

                offset = 0
                while True:
                    response = await client.get(
                        f"https://api.webflow.com/v2/collections/{self.settings.webflow_bills_collection_id}/items",
                        headers=headers,
                        params={"limit": 100, "offset": offset},
                    )

                    if response.status_code != 200:
                        break

                    data = response.json()
                    items = data.get("items", [])

                    if not items:
                        break

                    bills.extend(items)
                    offset += 100

                    if len(items) < 100:
                        break

            result = await sync_service.backload_all_bills(bills)

            return {
                "success": True,
                "mode": "backload_all",
                "total_bills": result.total_bills,
                "successful": result.successful,
                "failed": result.failed,
                "chunks_created": result.chunks_created,
                "errors": result.errors[:10] if result.errors else [],
            }

        return await self._run_openstates_sync()

    async def _run_legislator_bills_sync(self) -> dict[str, Any]:
        """
        Run the legislator sync (bills and optionally votes).

        Fetches sponsored bills and voting records for each legislator
        from OpenStates and creates documents for RAG.

        Returns:
            Dict with sync results
        """
        from votebot.ingestion.sources.webflow import WebflowSource
        from votebot.ingestion.metadata import MetadataExtractor
        from votebot.updates.legislator_sync import LegislatorSyncService

        start_time = datetime.utcnow()

        try:
            # Get config - prefer new legislator_sync, fall back to legacy legislator_bills
            leg_config = self._sync_config.get("legislator_sync", {})
            if not leg_config:
                leg_config = self._sync_config.get("legislator_bills", {})

            delay_ms = leg_config.get("delay_between_legislators_ms", 500)
            max_per_run = leg_config.get("max_legislators_per_run", 200)

            # Vote sync settings
            sync_votes = leg_config.get("sync_votes", False)
            max_vote_bills = leg_config.get("max_vote_bills_per_legislator", 200)
            vote_session = leg_config.get("vote_session")  # None = current session

            sync_type = "bills + votes" if sync_votes else "bills only"
            logger.info(
                "Starting legislator sync",
                sync_type=sync_type,
                max_per_run=max_per_run,
            )

            # Initialize services
            sync_service = LegislatorSyncService(self.settings)
            webflow = WebflowSource(self.settings, MetadataExtractor())

            # Fetch legislators from Webflow
            legislators = []
            async for doc in webflow.fetch_legislators(limit=0):
                extra = doc.metadata.extra
                legislator = {
                    "openstates_id": doc.metadata.legislator_id,
                    "name": doc.metadata.title,
                    "slug": extra.get("slug", ""),
                    "jurisdiction": doc.metadata.jurisdiction or "us",
                    "party": extra.get("party", ""),
                    "chamber": extra.get("chamber", ""),
                }
                if legislator["openstates_id"]:
                    legislators.append(legislator)

            logger.info(f"Fetched {len(legislators)} legislators from Webflow")

            # Limit per run to avoid timeouts
            if max_per_run > 0 and len(legislators) > max_per_run:
                # Rotate which legislators get synced by using date-based offset
                day_of_year = datetime.utcnow().timetuple().tm_yday
                offset = (day_of_year * max_per_run) % len(legislators)
                legislators = legislators[offset:offset + max_per_run]
                if len(legislators) < max_per_run:
                    # Wrap around
                    legislators.extend(legislators[:max_per_run - len(legislators)])
                logger.info(f"Processing batch of {len(legislators)} legislators (offset {offset})")

            # Override rate limit for paced sync
            sync_service.rate_limit.delay_between_requests_ms = delay_ms

            # Run sync with votes if enabled
            result = await sync_service.sync_all_legislators(
                legislators,
                include_votes=sync_votes,
                vote_session=vote_session,
                max_vote_bills=max_vote_bills,
            )

            duration = (datetime.utcnow() - start_time).total_seconds()

            logger.info(
                "Legislator sync completed",
                sync_type=sync_type,
                duration_seconds=duration,
                total_legislators=result.total_legislators,
                successful=result.successful,
                failed=result.failed,
                total_bills=result.total_bills_found,
                total_votes=result.total_votes_found,
                chunks_created=result.chunks_created,
            )

            # Call callbacks
            for callback in self._update_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback({"legislator_sync": result})
                    else:
                        callback({"legislator_sync": result})
                except Exception as e:
                    logger.error("Sync callback failed", error=str(e))

            return {
                "success": True,
                "sync_type": sync_type,
                "duration_seconds": duration,
                "total_legislators": result.total_legislators,
                "successful": result.successful,
                "failed": result.failed,
                "total_bills": result.total_bills_found,
                "total_votes": result.total_votes_found,
                "chunks_created": result.chunks_created,
                "errors": result.errors[:10] if result.errors else [],
            }

        except Exception as e:
            logger.exception("Legislator sync failed", error=str(e))
            return {
                "success": False,
                "error": str(e),
            }

    async def _run_organization_sync(self) -> dict[str, Any]:
        """
        Run the monthly organization sync.

        Fetches all organizations from Webflow CMS and re-ingests them
        into the vector store.

        Returns:
            Dict with sync results
        """
        from votebot.sync.handlers.organization import OrganizationHandler
        from votebot.sync.types import SyncOptions

        start_time = datetime.utcnow()
        logger.info("Starting monthly organization sync")

        try:
            handler = OrganizationHandler(self.settings)
            options = SyncOptions(include_openstates=False)
            result = await handler.sync_batch(options)

            duration = (datetime.utcnow() - start_time).total_seconds()

            logger.info(
                "Monthly organization sync completed",
                duration_seconds=duration,
                processed=result.items_processed,
                successful=result.items_successful,
                failed=result.items_failed,
                chunks_created=result.chunks_created,
            )

            # Call callbacks
            for callback in self._update_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback({"organization_sync": result})
                    else:
                        callback({"organization_sync": result})
                except Exception as e:
                    logger.error("Sync callback failed", error=str(e))

            return {
                "success": result.success,
                "duration_seconds": duration,
                "processed": result.items_processed,
                "successful": result.items_successful,
                "failed": result.items_failed,
                "chunks_created": result.chunks_created,
                "errors": result.errors[:10] if result.errors else [],
            }

        except Exception as e:
            logger.exception("Organization sync failed", error=str(e))
            return {
                "success": False,
                "error": str(e),
            }

    async def trigger_organization_sync(self) -> dict[str, Any]:
        """Manually trigger an organization sync."""
        logger.info("Manual organization sync triggered")
        return await self._run_organization_sync()

    async def trigger_legislator_bills_sync(
        self,
        limit: int = 0,
        jurisdiction: str | None = None,
        include_votes: bool | None = None,
        vote_session: str | None = None,
        max_vote_bills: int | None = None,
    ) -> dict[str, Any]:
        """
        Manually trigger a legislator sync (bills and optionally votes).

        Args:
            limit: Maximum legislators to process (0 = use config default)
            jurisdiction: Filter by jurisdiction code (e.g., 'fl', 'us')
            include_votes: Whether to sync votes (None = use config default)
            vote_session: Session filter for votes (None = use config default)
            max_vote_bills: Max bills to check for votes per legislator

        Returns:
            Dict with sync results
        """
        from votebot.ingestion.sources.webflow import WebflowSource
        from votebot.ingestion.metadata import MetadataExtractor
        from votebot.updates.legislator_sync import LegislatorSyncService

        # Get config
        leg_config = self._sync_config.get("legislator_sync", {})
        if not leg_config:
            leg_config = self._sync_config.get("legislator_bills", {})

        delay_ms = leg_config.get("delay_between_legislators_ms", 500)

        # Use config defaults if not specified
        if include_votes is None:
            include_votes = leg_config.get("sync_votes", False)
        if vote_session is None:
            vote_session = leg_config.get("vote_session")
        if max_vote_bills is None:
            max_vote_bills = leg_config.get("max_vote_bills_per_legislator", 200)

        sync_type = "bills + votes" if include_votes else "bills only"

        logger.info(
            "Manual legislator sync triggered",
            limit=limit,
            jurisdiction=jurisdiction,
            sync_type=sync_type,
        )

        start_time = datetime.utcnow()

        try:
            # Initialize services
            sync_service = LegislatorSyncService(self.settings)
            webflow = WebflowSource(self.settings, MetadataExtractor())

            # Fetch legislators from Webflow
            legislators = []
            count = 0
            async for doc in webflow.fetch_legislators(limit=0):
                extra = doc.metadata.extra
                legislator = {
                    "openstates_id": doc.metadata.legislator_id,
                    "name": doc.metadata.title,
                    "slug": extra.get("slug", ""),
                    "jurisdiction": doc.metadata.jurisdiction or "us",
                    "party": extra.get("party", ""),
                    "chamber": extra.get("chamber", ""),
                }

                if not legislator["openstates_id"]:
                    continue

                # Filter by jurisdiction if specified
                if jurisdiction:
                    if legislator["jurisdiction"].lower() != jurisdiction.lower():
                        continue

                legislators.append(legislator)
                count += 1

                if limit > 0 and count >= limit:
                    break

            logger.info(f"Processing {len(legislators)} legislators")

            # Override rate limit for paced sync
            sync_service.rate_limit.delay_between_requests_ms = delay_ms

            # Run sync with votes if enabled
            result = await sync_service.sync_all_legislators(
                legislators,
                include_votes=include_votes,
                vote_session=vote_session,
                max_vote_bills=max_vote_bills,
            )

            duration = (datetime.utcnow() - start_time).total_seconds()

            return {
                "success": True,
                "mode": "manual",
                "sync_type": sync_type,
                "duration_seconds": duration,
                "total_legislators": result.total_legislators,
                "successful": result.successful,
                "failed": result.failed,
                "total_bills": result.total_bills_found,
                "total_votes": result.total_votes_found,
                "chunks_created": result.chunks_created,
                "errors": result.errors[:10] if result.errors else [],
            }

        except Exception as e:
            logger.exception("Manual legislator sync failed", error=str(e))
            return {
                "success": False,
                "error": str(e),
            }

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._is_running

    def get_jobs(self) -> list[dict]:
        """Get information about scheduled jobs."""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            })
        return jobs


class UpdateSchedulerFactory:
    """Factory for creating update scheduler instances."""

    _instance: UpdateScheduler | None = None

    @classmethod
    def get_instance(cls, settings: Settings | None = None) -> UpdateScheduler:
        """Get or create a singleton scheduler instance."""
        if cls._instance is None:
            cls._instance = UpdateScheduler(settings)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance."""
        if cls._instance and cls._instance.is_running:
            cls._instance.stop()
        cls._instance = None
