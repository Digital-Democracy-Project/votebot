"""Helpers for work started in the background of one request."""

import asyncio


async def cancel_and_wait(tasks: list[asyncio.Future]) -> None:
    """Cancel what is still running and wait until every task has ended, so none outlives the request that
    started it and no failure goes unretrieved. Cancelling stops the wait, not a call already running on a thread."""
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
