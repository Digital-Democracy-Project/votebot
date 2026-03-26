# Plan: Large PDF Handling & Sync Auto-Resume (v2)

## Problem Statement

Worker PID 61904 was OOM-killed on Feb 27 at 06:55:05 UTC during a batch bill sync (task `e3ebabae-5394-4bc7-9b9a-64676a2bc443`). It processed **130 of 993 bills** (13.1%) before dying. The trigger was **HR 7148** (Consolidated Appropriations Act 2026) — a 1,540-page, 2.3MB PDF that generated **816 chunks**. The worker died 6 seconds after completing the bill following HR 7148, with no exception or shutdown log — the signature of a kernel OOM kill.

Secondary issue: when the worker died, the sync task was left in "running" status in Redis with no recovery mechanism. The 863 remaining bills require manual intervention to resume.

## Root Cause Analysis

### Memory pressure chain for HR 7148:

```
1. _extract_text(): pdfplumber opens 1,540 pages
   - page.flush_cache() releases per-page layout objects ✓
   - BUT: all extracted text accumulated in text_parts[] list
   - ~5-10MB of raw text held in memory after extraction

2. chunk_text(): splits into 816 chunks
   - 816 Chunk objects with content + metadata in memory
   - Original text string still in memory (not freed until function exits)

3. upsert_documents(): generates ALL 816 embeddings BEFORE any upsert
   - embed_documents() calls OpenAI for all 816 texts
   - 816 embeddings × 3072 floats × 4 bytes = ~10MB of float arrays
   - Plus the 816 Document objects with content + metadata copies

4. Total memory for single bill: ~50-100MB
   - text_parts list + joined string + 816 chunks + 816 embeddings + metadata
   - This doesn't get GC'd until the bill's processing function returns

5. gc.collect() only runs every 10 bills in batch sync
   - Memory from bills 1-9 may not be freed before bill 10
   - Python's allocator doesn't always return memory to the OS even after gc
```

### Why the worker died AFTER HR 7148 (not during):

The 816-chunk bill pushed memory near the limit. Python's memory allocator doesn't immediately return freed memory to the OS — it keeps freed blocks in its internal free lists. When the next bill (HB 429, small) allocated memory, it pushed the process over the limit and the kernel OOM killer terminated it.

---

## Fix 1: Large PDF Memory Management

### 1A. Add configurable PDF page limit (safety net, default 1000)

Large omnibus bills contain important budget priority information. The primary defense is better memory management (1B-1D). The page limit is a safety net for truly extreme PDFs (>1000 pages) only.

**File: `src/votebot/config.py`** — add setting:

```python
# After chunk_overlap on line 72
pdf_max_pages: int = 1000
```

**File: `src/votebot/ingestion/sources/pdf.py`** — add `max_pages` parameter to `_extract_text()`:

Current code (line 122-160):
```python
async def _extract_text(self, file_path: str) -> tuple[str, dict | None]:
    # ...
    with pdfplumber.open(file_path) as pdf:
        # ...
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
            page.flush_cache()
```

New code:
```python
async def _extract_text(self, file_path: str, max_pages: int = 0) -> tuple[str, dict | None]:
    """
    Extract text from a PDF file.

    Args:
        file_path: Path to the PDF file
        max_pages: Maximum pages to process (0 = unlimited)

    Returns:
        Tuple of (extracted_text, pdf_metadata)
    """
    try:
        import pdfplumber

        text_parts = []
        metadata = None

        with pdfplumber.open(file_path) as pdf:
            metadata = pdf.metadata
            total_pages = len(pdf.pages)

            if total_pages > 200:
                logger.info(
                    "Processing large PDF",
                    file=file_path,
                    pages=total_pages,
                )

            # Apply page limit if set
            if max_pages > 0 and total_pages > max_pages:
                logger.warning(
                    "PDF exceeds page limit, truncating",
                    file=file_path,
                    total_pages=total_pages,
                    max_pages=max_pages,
                    truncated_pages=total_pages - max_pages,
                )
                pages_to_process = pdf.pages[:max_pages]
            else:
                pages_to_process = pdf.pages

            for page in pages_to_process:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
                page.flush_cache()

        if text_parts:
            return "\n\n".join(text_parts), metadata

    except ImportError:
        logger.debug("pdfplumber not available, trying PyPDF2")
    except Exception as e:
        logger.warning(f"pdfplumber extraction failed: {e}")

    # Fallback to PyPDF2 (unchanged)
    # ...
```

Propagate through `process_file()` and `process_url()`:

```python
# pdf.py line 88 — process_file()
async def process_file(self, file_path: str, max_pages: int = 0) -> DocumentSource | None:
    # ...
    text, pdf_metadata = await self._extract_text(file_path, max_pages=max_pages)
    # ... rest unchanged

# pdf.py line 186 — process_url()
async def process_url(self, url: str, save_path: str | None = None, max_pages: int = 0) -> DocumentSource | None:
    # ... download logic unchanged ...
    # Line 238 — change:
    doc = await self.process_file(file_path, max_pages=max_pages)
    # ... rest unchanged
```

**File: `src/votebot/ingestion/sources/webflow.py`** — pass setting through to PDF calls:

In `_process_bill_pdf()`, pass the setting from `self.settings`:

```python
# webflow.py — _process_bill_pdf(), at the process_url call:
doc = await self.pdf_source.process_url(
    pdf_url,
    max_pages=self.settings.pdf_max_pages,
)
```

### 1B. Incremental embedding + upsert for large documents

**File: `src/votebot/services/vector_store.py`**

Currently (lines 94-106) all embeddings are generated before any upsert:

```python
# CURRENT — generates ALL embeddings upfront
texts_to_embed = []
docs_needing_embeddings = []
for doc in documents:
    if doc.embedding is None:
        texts_to_embed.append(doc.content)
        docs_needing_embeddings.append(doc)

if texts_to_embed:
    embeddings = await self.embedding_service.embed_documents(texts_to_embed)
    for doc, embedding in zip(docs_needing_embeddings, embeddings):
        doc.embedding = embedding
```

Replace `upsert_documents()` (lines 79-150) with batched embed+upsert:

```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
)
async def upsert_documents(
    self,
    documents: list[Document],
    batch_size: int = 100,
) -> int:
    """
    Add or update documents in the vector store.

    Processes in batches: embeds and upserts batch_size documents at a time
    to cap peak memory usage for large documents (800+ chunks).

    Args:
        documents: List of documents to upsert
        batch_size: Number of documents per embed+upsert cycle

    Returns:
        Number of documents upserted
    """
    total_upserted = 0

    for i in range(0, len(documents), batch_size):
        batch_docs = documents[i : i + batch_size]

        # Generate embeddings for this batch only
        texts_to_embed = []
        docs_needing_embeddings = []
        for doc in batch_docs:
            if doc.embedding is None:
                texts_to_embed.append(doc.content)
                docs_needing_embeddings.append(doc)

        if texts_to_embed:
            embeddings = await self.embedding_service.embed_documents(texts_to_embed)
            for doc, embedding in zip(docs_needing_embeddings, embeddings):
                doc.embedding = embedding

        # Build and upsert vectors for this batch
        batch = []
        for doc in batch_docs:
            batch.append(
                {
                    "id": doc.id,
                    "values": doc.embedding,
                    "metadata": {
                        "content": doc.content[:40000],
                        **doc.metadata,
                    },
                }
            )

        logger.debug(
            "Upserting batch to Pinecone",
            batch_size=len(batch),
            batch_index=i // batch_size,
        )
        self.index.upsert(vectors=batch, namespace=self.namespace)
        total_upserted += len(batch)

    logger.info(
        "Documents upserted to vector store",
        count=total_upserted,
        index=self.index_name,
    )

    return total_upserted
```

This caps peak memory at ~100 embeddings (~1.2MB) instead of 816+ (~10MB). The OpenAI embedding API already batches at 100 internally, so there's no throughput penalty.

### 1C. gc.collect every bill in batch sync

**File: `src/votebot/sync/handlers/bill.py`**

Current code (lines 404-414):
```python
# Reclaim PDF text, pdfplumber objects, embedding vectors
del bill_docs
if (item_idx + 1) % 10 == 0:
    gc.collect()
    logger.debug(
        "Batch sync progress",
        bills_processed=item_idx + 1,
        total_bills=len(raw_items),
        docs_collected=total_docs_collected,
        items_skipped=items_skipped,
    )
```

New code:
```python
# Reclaim PDF text, pdfplumber objects, embedding vectors
del bill_docs
gc.collect()

if (item_idx + 1) % 10 == 0:
    logger.debug(
        "Batch sync progress",
        bills_processed=item_idx + 1,
        total_bills=len(raw_items),
        docs_collected=total_docs_collected,
        items_skipped=items_skipped,
    )
```

This matches the version sync's behavior (gc every bill) and ensures memory from the 816-chunk monster is reclaimed before the next bill starts.

### 1D. Explicit memory cleanup in pipeline for large documents

**File: `src/votebot/ingestion/pipeline.py`**

After the upsert in `ingest_document()` (around line 163), add explicit cleanup:

Current code (lines 149-177):
```python
# Upsert to vector store
try:
    upserted = await self.vector_store.upsert_documents(documents)
except Exception as e:
    # ... error handling ...

logger.info(
    "Document ingested",
    document_id=metadata.document_id,
    chunks=len(chunks),
)

return IngestionResult(
    documents_processed=1,
    chunks_created=len(chunks),
    chunks_upserted=upserted,
    errors=errors,
    skipped=skipped,
)
```

New code:
```python
# Upsert to vector store
try:
    upserted = await self.vector_store.upsert_documents(documents)
except Exception as e:
    # ... error handling unchanged ...

chunk_count = len(chunks)

logger.info(
    "Document ingested",
    document_id=metadata.document_id,
    chunks=chunk_count,
)

# Explicit cleanup for large documents to prevent OOM
del documents
del chunks
if chunk_count > 100:
    import gc
    gc.collect()
    logger.info(
        "Forced gc.collect after large document",
        document_id=metadata.document_id,
        chunks=chunk_count,
    )

return IngestionResult(
    documents_processed=1,
    chunks_created=chunk_count,
    chunks_upserted=upserted,
    errors=errors,
    skipped=skipped,
)
```

---

## Fix 2: Sync Auto-Resume on Worker Death

### Problem

When a worker is OOM-killed during a batch sync:
1. The task stays in "running" status in Redis forever (zombie)
2. The 863 remaining bills are not processed
3. Recovery requires manual `resume_task_id` intervention

### 2A. Add `last_heartbeat` to sync task state

**File: `src/votebot/api/routes/sync_unified.py`**

Add heartbeat timestamp to the initial task state (line 323-335):

Current code:
```python
_background_tasks[task_id] = {
    "status": "accepted",
    "content_type": content_type.value,
    "mode": "batch",
    "started_at": time.time(),
    "options": {
        "include_pdfs": options.include_pdfs,
        "include_openstates": options.include_openstates,
        "jurisdiction": options.jurisdiction,
        "limit": options.limit,
        "dry_run": options.dry_run,
    },
}
```

New code:
```python
from datetime import datetime, timezone

_background_tasks[task_id] = {
    "status": "accepted",
    "content_type": content_type.value,
    "mode": "batch",
    "started_at": time.time(),
    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
    "retry_count": 0,
    "options": {
        "include_pdfs": options.include_pdfs,
        "include_openstates": options.include_openstates,
        "include_sponsored_bills": options.include_sponsored_bills,
        "jurisdiction": options.jurisdiction,
        "limit": options.limit,
        "dry_run": options.dry_run,
    },
}
```

Update heartbeat in the progress callback inside `_run_batch_sync_background()` (line 160-180):

Current code:
```python
async def _progress(
    items_processed, items_successful, items_failed, chunks_created, errors=None,
) -> None:
    nonlocal _redis_write_counter
    live_result["items_processed"] = items_processed
    # ...
    _redis_write_counter += 1
    if _redis_write_counter % 10 == 0:
        await redis_store.set_sync_task(task_id, _background_tasks[task_id])
```

New code:
```python
async def _progress(
    items_processed, items_successful, items_failed, chunks_created, errors=None,
) -> None:
    nonlocal _redis_write_counter
    live_result["items_processed"] = items_processed
    live_result["items_successful"] = items_successful
    live_result["items_failed"] = items_failed
    live_result["chunks_created"] = chunks_created
    live_result["duration_ms"] = int((time.perf_counter() - start_time) * 1000)
    if errors:
        live_result["errors"] = errors

    # Update heartbeat for zombie detection
    _background_tasks[task_id]["last_heartbeat"] = datetime.now(timezone.utc).isoformat()

    # Throttle Redis writes to every 10 progress calls
    _redis_write_counter += 1
    if _redis_write_counter % 10 == 0:
        await redis_store.set_sync_task(task_id, _background_tasks[task_id])
```

### 2B. Zombie task watchdog (polls every 30 minutes)

Instead of only checking at startup, run a recurring watchdog loop on the leader worker that checks for zombie sync tasks every 30 minutes.

**File: `src/votebot/main.py`**

Add a new function `_zombie_sync_watchdog()` and a startup helper. Import needed at top of `lifespan`:

```python
from datetime import datetime, timezone, timedelta
```

Add after the `_try_become_leader` function (around line 87), inside the `lifespan` scope so it has access to `settings`:

```python
async def _zombie_sync_watchdog():
    """
    Poll Redis every 30 minutes for zombie sync tasks and auto-resume them.

    A task is a zombie if status == "running" and last_heartbeat is >5 minutes old.
    Runs on the leader worker only.
    """
    POLL_INTERVAL = 1800  # 30 minutes
    STALE_THRESHOLD = timedelta(minutes=5)
    MAX_RETRIES = 3

    while True:
        try:
            await _check_and_resume_stale_syncs(STALE_THRESHOLD, MAX_RETRIES)
        except Exception as e:
            logger.exception("Zombie sync watchdog error", error=str(e))
        await asyncio.sleep(POLL_INTERVAL)

async def _check_and_resume_stale_syncs(
    stale_threshold: timedelta,
    max_retries: int,
):
    """Scan Redis for zombie sync tasks and auto-resume them."""
    from votebot.api.routes.sync_unified import (
        _background_tasks,
        _run_batch_sync_background,
    )
    from votebot.sync import SyncOptions

    redis_store = get_redis_store()
    if not redis_store or not redis_store._client:
        return

    # Scan for sync task keys
    keys = []
    async for key in redis_store._client.scan_iter(match="votebot:sync:task:*"):
        keys.append(key)

    for key in keys:
        task_data = await redis_store._client.get(key)
        if not task_data:
            continue

        import json
        task = json.loads(task_data)

        if task.get("status") != "running":
            continue

        # Check if heartbeat is stale
        heartbeat = task.get("last_heartbeat")
        if not heartbeat:
            continue

        heartbeat_time = datetime.fromisoformat(heartbeat)
        # Ensure heartbeat_time is timezone-aware
        if heartbeat_time.tzinfo is None:
            heartbeat_time = heartbeat_time.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - stale_threshold

        if heartbeat_time > cutoff:
            continue  # Still fresh, another worker might be running it

        # Extract task_id from key
        key_str = key.decode() if isinstance(key, bytes) else key
        old_task_id = key_str.split(":")[-1]

        # --- 2D. Check retry limit ---
        retry_count = task.get("retry_count", 0)
        if retry_count >= max_retries:
            logger.error(
                "Sync task exceeded max retries, marking as permanently failed",
                task_id=old_task_id,
                retry_count=retry_count,
                content_type=task.get("content_type"),
                items_processed=task.get("result", {}).get("items_processed", 0),
            )
            task["status"] = "permanently_failed"
            task["error"] = (
                f"Exceeded max retries ({max_retries}). "
                f"Worker crashed {retry_count} times. "
                f"Last heartbeat: {heartbeat}. "
                f"Manual intervention required."
            )
            await redis_store.set_sync_task(old_task_id, task)
            continue

        # This is a zombie task — mark old task as failed
        content_type_str = task.get("content_type", "bill")
        saved_options = task.get("options", {})

        logger.warning(
            "Found stale sync task from crashed worker, auto-resuming",
            old_task_id=old_task_id,
            content_type=content_type_str,
            retry_count=retry_count,
            items_processed=task.get("result", {}).get("items_processed", 0),
            last_heartbeat=heartbeat,
        )

        task["status"] = "failed"
        task["error"] = f"Worker died (OOM or crash). Auto-resuming as retry {retry_count + 1}/{max_retries}."
        await redis_store.set_sync_task(old_task_id, task)

        # Start a new sync with resume
        from votebot.sync import ContentType

        new_task_id = str(uuid.uuid4())
        options = SyncOptions(
            include_pdfs=saved_options.get("include_pdfs", True),
            include_openstates=saved_options.get("include_openstates", True),
            include_sponsored_bills=saved_options.get("include_sponsored_bills", True),
            jurisdiction=saved_options.get("jurisdiction"),
            limit=saved_options.get("limit", 0),
            dry_run=saved_options.get("dry_run", False),
            resume_task_id=old_task_id,
        )

        # Copy checkpoints from old task
        copied = await redis_store.copy_sync_checkpoints(old_task_id, new_task_id)

        # Register new task in background_tasks dict
        _background_tasks[new_task_id] = {
            "status": "accepted",
            "content_type": content_type_str,
            "mode": "batch",
            "started_at": time.time(),
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "retry_count": retry_count + 1,
            "resumed_from": old_task_id,
            "options": saved_options,
        }
        await redis_store.set_sync_task(new_task_id, _background_tasks[new_task_id])

        # Start the background sync
        content_type_enum = ContentType(content_type_str)
        asyncio.create_task(
            _run_batch_sync_background(new_task_id, content_type_enum, options, settings)
        )

        logger.info(
            "Auto-resumed sync task",
            new_task_id=new_task_id,
            old_task_id=old_task_id,
            retry_count=retry_count + 1,
            checkpoints_copied=copied,
        )
```

Wire the watchdog into the lifespan. In `main.py`, after `await _start_as_leader()` succeeds (both at initial election on line 90 and in `_try_become_leader` on line 85), start the watchdog:

```python
# In _start_as_leader(), add at the end:
async def _start_as_leader():
    """Promote this worker to scheduler leader."""
    from votebot.updates.scheduler import UpdateSchedulerFactory
    state["scheduler"] = UpdateSchedulerFactory.get_instance(settings)
    state["scheduler"].start()
    state["refresh_task"] = asyncio.create_task(_refresh_lock())
    state["watchdog_task"] = asyncio.create_task(_zombie_sync_watchdog())
```

Add `"watchdog_task"` to the shutdown cleanup loop (line 107):
```python
for key in ("bg_task", "refresh_task", "watchdog_task"):
    task = state.get(key)
    if task and not task.done():
        task.cancel()
        # ...
```

And initialize it in the state dict (line 52):
```python
state = {"scheduler": None, "refresh_task": None, "bg_task": None, "watchdog_task": None}
```

### 2C. Persist sync options in Redis for resume

Already handled in 2A — the `options` dict is stored in `_background_tasks[task_id]` on line 323-335 and written through to Redis. The watchdog in 2B reads `task.get("options", {})` to reconstruct `SyncOptions`.

No additional changes needed beyond what's in 2A.

### 2D. Max retry limit with permanent failure

Already integrated into 2B above. The flow is:

1. Each new task gets `"retry_count": 0` (set in 2A)
2. When the watchdog resumes a zombie, it increments: `"retry_count": retry_count + 1`
3. Before resuming, the watchdog checks `retry_count >= MAX_RETRIES (3)`:
   - If exceeded: sets `status = "permanently_failed"` with a detailed error message and **does not resume**
   - If not: proceeds with resume

The `_run_batch_sync_background` function must also propagate `retry_count` from the old task to the new task's Redis state. This is handled in the watchdog's `_background_tasks[new_task_id]` initialization which sets `"retry_count": retry_count + 1`.

---

## Files Changed Summary

| File | Changes |
|------|---------|
| `src/votebot/config.py` | Add `pdf_max_pages: int = 1000` setting |
| `src/votebot/ingestion/sources/pdf.py` | Add `max_pages` parameter to `_extract_text()`, `process_file()`, `process_url()` |
| `src/votebot/ingestion/sources/webflow.py` | Pass `settings.pdf_max_pages` to `pdf_source.process_url()` in `_process_bill_pdf()` |
| `src/votebot/services/vector_store.py` | Rewrite `upsert_documents()` to embed+upsert in batches of 100 |
| `src/votebot/sync/handlers/bill.py` | Change `gc.collect()` from every 10 bills to every bill |
| `src/votebot/ingestion/pipeline.py` | Add `del documents/chunks` + `gc.collect()` after large document ingestion (>100 chunks) |
| `src/votebot/api/routes/sync_unified.py` | Add `last_heartbeat`, `retry_count`, `options` to task state; update heartbeat in progress callback |
| `src/votebot/main.py` | Add `_zombie_sync_watchdog()` (30-min poll), `_check_and_resume_stale_syncs()` with retry limit; start watchdog on leader |

## Execution Order

1. **Fix 1B** — Incremental embedding+upsert (biggest memory win, lowest risk)
2. **Fix 1C** — gc.collect every bill (simple, high impact)
3. **Fix 1D** — Explicit cleanup in pipeline for large docs (belt and suspenders)
4. **Fix 1A** — PDF page limit at 1000 (safety net only, after memory fixes)
5. **Fix 2A** — Heartbeat + options in task state (prerequisite for 2B)
6. **Fix 2B+2D** — Zombie watchdog with retry limit (main resilience fix)

## Risk Assessment

- **Fix 1A (page limit at 1000)**: Very low risk. Only omnibus spending bills exceed this. HR 7148 was 1540 pages — with Fix 1B-1D, it should survive. If another 3000-page monster appears, this catches it.
- **Fix 1B (incremental upsert)**: Low risk. Same total vectors, same embeddings, just processed in 100-chunk stages. No behavior change visible externally.
- **Fix 1C-1D (gc)**: Very low risk. More gc is never harmful, just slightly slower (~5ms per call). The version sync already does gc every bill.
- **Fix 2A (heartbeat)**: Very low risk. Adds one field to existing dict, one `datetime.now()` call per progress update.
- **Fix 2B+2D (watchdog + retry limit)**: Medium risk. Must correctly reconstruct sync options and avoid duplicate processing. Safeguards: checkpoint-based skip, 3-retry max, leader-only execution, 5-min stale threshold, 30-min poll interval.

## Testing

1. Run a small batch sync (limit=10) and verify heartbeat updates appear in Redis
2. Kill the worker (`kill -9`) during batch sync, wait 30+ minutes, verify watchdog detects and resumes
3. Faster test: temporarily lower `POLL_INTERVAL` to 60s and `STALE_THRESHOLD` to 1 minute, kill worker, verify resume
4. Verify the resumed sync skips already-checkpointed bills (check "Resume: loaded checkpoints" log)
5. Kill worker 3 times on the same task, verify the 4th attempt logs "permanently failed" and does NOT resume
6. Test with a large PDF (>200 pages) and verify incremental embedding works (check "Upserting batch" log entries)
