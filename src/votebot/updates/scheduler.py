"""Hourly polling scheduler for content updates."""

import asyncio
from datetime import datetime
from typing import Callable

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from votebot.config import Settings, get_settings
from votebot.ingestion.pipeline import IngestionPipeline, IngestionResult
from votebot.updates.change_detection import ChangeDetector

logger = structlog.get_logger()


class UpdateScheduler:
    """
    Scheduler for periodic content updates.

    Handles:
    - Hourly polling for content changes
    - Manual update triggers
    - Graceful shutdown
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the update scheduler.

        Args:
            settings: Application settings
        """
        self.settings = settings or get_settings()
        self.scheduler = AsyncIOScheduler()
        self.pipeline = IngestionPipeline(self.settings)
        self.change_detector = ChangeDetector(self.settings)
        self._is_running = False
        self._update_callbacks: list[Callable] = []

    def start(self) -> None:
        """Start the scheduler."""
        if self._is_running:
            logger.warning("Scheduler already running")
            return

        # Add hourly update job
        self.scheduler.add_job(
            self._run_updates,
            trigger=IntervalTrigger(hours=1),
            id="hourly_update",
            name="Hourly Content Update",
            replace_existing=True,
        )

        self.scheduler.start()
        self._is_running = True

        logger.info("Update scheduler started")

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
                "congress": 118,  # Current Congress
                "limit": 50,
            },
            "openstates": {
                "limit": 50,
            },
        }

        config = source_configs.get(source, {})
        return await self.pipeline.ingest_from_source(source, config)

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
