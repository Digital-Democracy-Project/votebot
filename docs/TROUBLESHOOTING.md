# VoteBot Troubleshooting Guide

This document captures common issues, diagnostic procedures, and solutions for VoteBot's RAG and data sync systems.

## Table of Contents

- [Legislator Vote Lookups Not Working](#legislator-vote-lookups-not-working)
- [Model Contradicts Itself About Votes](#model-contradicts-itself-about-votes)
- [Poor Search Ranking for Full Name Queries](#poor-search-ranking-for-full-name-queries)
- [Corrupted Legislator-Votes Documents](#corrupted-legislator-votes-documents)
- [Missing Data in Search Results](#missing-data-in-search-results)
- [Federal Legislator Cache Issues](#federal-legislator-cache-issues)
- [Pinecone Index Diagnostics](#pinecone-index-diagnostics)
- [Full Index Rebuild Procedure](#full-index-rebuild-procedure)

---

## Legislator Vote Lookups Not Working

### Symptom
VoteBot responds with "X is not listed as a voting member" or cannot find voting records for a legislator who should have votes in the system.

### Example
```
User: "How did Ashley Moody vote on HR1?"
Bot: "Ashley Moody is not listed as a voting member of the U.S. Congress..."
```

### Diagnostic Steps

#### 1. Check if the legislator exists in the index

```python
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def check_legislator(name):
    settings = get_settings()
    vs = VectorStoreService(settings)

    results = await vs.query(f'{name} legislator profile', top_k=5)
    for r in results:
        print(f'{r.score:.3f} | {r.metadata.get("document_id", "")}')
        print(f'  Content: {r.content[:200]}...')

asyncio.run(check_legislator("Ashley Moody"))
```

#### 2. Check if legislator-votes document exists

```python
async def check_votes_doc(person_uuid):
    settings = get_settings()
    vs = VectorStoreService(settings)

    doc_id = f'legislator-votes-{person_uuid}-chunk-0'
    result = vs.index.fetch(ids=[doc_id], namespace=vs.namespace)

    if doc_id in result.vectors:
        meta = result.vectors[doc_id].metadata
        print(f'Title: {meta.get("title")}')
        print(f'Total votes: {meta.get("total_votes")}')
        print(f'Yes votes: {meta.get("yes_votes")}')
        print(f'No votes: {meta.get("no_votes")}')
    else:
        print(f'Document not found: {doc_id}')

# Ashley Moody's OpenStates person UUID
asyncio.run(check_votes_doc("cb582ab6-6a5a-4578-9e44-620c9a6a1f4c"))
```

#### 3. Check if bill-votes contain the legislator's person ID

```python
async def check_bill_votes(person_uuid, bill_webflow_id):
    settings = get_settings()
    vs = VectorStoreService(settings)

    results = await vs.query(
        f'{bill_webflow_id} votes',
        top_k=20,
        filter={'document_type': 'bill-votes'}
    )

    for r in results:
        if bill_webflow_id in r.metadata.get('document_id', ''):
            if person_uuid in r.content:
                print(f'Person ID found in {r.metadata.get("document_id")}')
                idx = r.content.find(person_uuid)
                print(f'Context: {r.content[max(0,idx-30):idx+80]}')
            else:
                print(f'Person ID NOT in chunk {r.metadata.get("chunk_index")}')

asyncio.run(check_bill_votes("cb582ab6-6a5a-4578-9e44-620c9a6a1f4c", "682f4c9a5a8d551cb4777414"))
```

#### 4. Test search ranking

```python
async def test_search(query):
    settings = get_settings()
    vs = VectorStoreService(settings)

    results = await vs.query(query, top_k=10)

    for i, r in enumerate(results):
        doc_type = r.metadata.get('document_type', 'N/A')
        doc_id = r.metadata.get('document_id', '')
        print(f'{i+1}. {r.score:.3f} | {doc_type} | {doc_id[:60]}...')

asyncio.run(test_search("Ashley Moody vote HR1"))
```

### Common Causes

1. **Legislator-votes document doesn't exist**: The legislator's votes weren't extracted during the build process
2. **Corrupted duplicate documents**: Malformed documents ranking higher than valid ones (see next section)
3. **Missing person ID in bill-votes**: The bill sync didn't include the legislator's OpenStates person ID
4. **Federal legislator cache outdated**: The cache doesn't include newly appointed legislators
5. **Last-name-only in document**: Document contains only last name (e.g., "Moody") but user queries with full name ("Ashley Moody") - see [Poor Search Ranking](#poor-search-ranking-for-full-name-queries)

### Solutions

1. **Rebuild legislator-votes index**:
   ```bash
   python -m votebot.sync.build_legislator_votes
   ```

2. **Refresh federal legislator cache** (for federal legislators):
   ```bash
   python -m votebot.sync.federal_legislator_cache
   ```

3. **Re-sync bills with OpenStates data**:
   ```bash
   python -m votebot.updates.bill_sync batch --jurisdiction us --include-openstates
   ```

---

## Model Contradicts Itself About Votes

### Symptom
VoteBot gives contradictory answers about a legislator's vote within the same conversation:
- First response: "Ashley Moody voted No on HR1"
- Second response: "Ashley Moody is not listed as a member of Congress"
- Or: "All Republicans voted Yes" followed by claiming a Republican voted No

### Example
```
User: "How did Ashley Moody vote on this?"
Bot: "Ashley Moody voted No on this bill."

User: "Are you sure?"
Bot: "Ashley Moody is the Attorney General of Florida and does not serve in Congress..."
```

### Root Causes

1. **Duplicate votes in RAG data**: When a bill has multiple vote events (procedural votes, final passage), the same legislator may appear in both "Voted Yes" and "Voted No" sections for different motions. This confuses the model.

2. **Model hallucination**: When users challenge information, the model may fall back to outdated training data instead of trusting RAG results.

3. **Verification not triggered**: Phrases like "are you sure" or "be sure" may not trigger the verification flow.

### Solution: Vote Verification Feature

VoteBot includes automatic vote verification that fetches directly from OpenStates when users challenge information. This is triggered by phrases like:

- **Dispute phrases**: "that's wrong", "no way", "that can't be", "impossible"
- **Verification requests**: "be sure", "double check", "verify", "confirm"
- **Search commands**: "do a web search", "check openstates", "look it up"

When triggered, the agent:
1. Extracts the legislator name from the conversation (handles lowercase input, "X voted Y" patterns, etc.)
2. Gets the `session-code` from Webflow page context (e.g., "119" for 119th Congress)
3. Calls `BillVotesService.lookup_legislator_vote()` directly
4. **Prioritizes final passage votes** over procedural votes (motion to commit, cloture, etc.)
5. Returns authoritative data from OpenStates API that overrides RAG results

### Diagnostic Steps

#### 1. Check if verification was triggered

Look in the logs for these key messages:
```
"Checking dispute/verification trigger" message=... is_dispute=True/False
"Dispute detected, attempting vote verification"
"Vote verification successful" context_length=...
"Vote verification returned empty" (if name extraction or API lookup failed)
"Verifying legislator vote from OpenStates" legislator=... bill=... session=...
"Could not extract legislator name for vote verification" (if name not found in message or history)
```

If `is_dispute=False` when you expected verification, the trigger phrase isn't being matched.

#### 2. Test verification manually

```python
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.bill_votes import BillVotesService

async def verify_vote():
    settings = get_settings()
    service = BillVotesService(settings)

    result = await service.lookup_legislator_vote(
        legislator_name="Moody",
        jurisdiction="US",
        session="119",  # 119th Congress - use session-code from Webflow
        bill_identifier="HR1",
    )

    if result:
        print(f"Legislator: {result['legislator']}")
        print(f"Vote: {result['vote']}")  # Should be YES for final passage
        print(f"Motion: {result['motion']}")  # Should be final passage, not procedural
        print(f"Date: {result['date']}")
        # Check if multiple votes were found
        if result.get('total_votes_on_bill'):
            print(f"Total votes on bill: {result['total_votes_on_bill']}")
            print(f"Note: {result.get('note')}")
    else:
        print("Legislator not found in vote records")

asyncio.run(verify_vote())
```

#### 3. Check verification trigger phrases

The current trigger phrases are in `agent.py:_is_dispute_or_correction()`. If a phrase isn't triggering verification, it may need to be added to the list.

### If Verification Isn't Working

1. **Check OpenStates API key**: Ensure `OPENSTATES_API_KEY` is set and valid
2. **Check bill identifier format**: The bill must be in OpenStates (e.g., "HR1" for federal, "HB123" for state)
3. **Check legislator name extraction**: Names must be capitalized and not common words
4. **Check session-code from Webflow**: The frontend should pass `session-code` in the page context. This field in Webflow contains the OpenStates-friendly session identifier (e.g., "119" for 119th Congress, "2025" for state sessions). Do NOT use `session-year` which is just the calendar year.
5. **Check logs for session value**: Look for `bill_session=` in the WebSocket logs to verify the session is being passed

### Webflow Page Context Fields

The frontend should pass these fields from Webflow:

| Webflow Field | Maps To | Description |
|---------------|---------|-------------|
| `session-code` | `page_context.session` | OpenStates-friendly session (e.g., "119", "2025") |
| `session-year` | (not used) | Calendar year only - don't use for OpenStates |
| `jurisdiction` | `page_context.jurisdiction` | State code or "US" for federal |
| `slug` | `page_context.id` | Bill identifier (e.g., "HR1") |

### Known Issues (Fixed)

#### Federal Bills Using Year Instead of Congress Number

**Bug**: The verification code was using the current year (e.g., "2026") as the session for federal bills, but OpenStates API expects the Congress number (e.g., "119").

**Example**: Query to `https://v3.openstates.org/bills/us/2026/HR1` would fail because the correct URL is `https://v3.openstates.org/bills/us/119/HR1`.

**Fix**:
1. The WebSocket handler now extracts `session-code` from Webflow and maps it to `page_context.session`
2. If `session-code` is not provided, the agent calculates the Congress number from the year as a fallback:
   - 119th Congress: 2025-2027
   - 120th Congress: 2027-2029

#### Name Extraction Failing for Lowercase Input

**Bug**: When users typed "how did ashley moody vote?" (lowercase), the name extraction couldn't find "Ashley Moody" because it only looked for capitalized names.

**Fix**: Added multiple extraction methods:
1. Pattern match for "X voted Y"
2. Pattern match for "Name (Party-State)"
3. Pattern match for "did X vote"
4. Fallback to capitalized word extraction

#### Verification Returning Procedural Vote Instead of Final Passage

**Bug**: When a legislator cast multiple votes on a bill (e.g., NO on "motion to commit" and YES on final passage), the verification returned the first match (procedural NO) instead of the more important final passage vote (YES).

**Example**: Ashley Moody voted NO on the "Motion to Commit HR 1 to Committee" but YES on final passage. The verification incorrectly reported her as voting NO.

**Fix**: The `lookup_legislator_vote` method now:
1. Collects ALL votes by the legislator on the bill
2. Scores each vote to prioritize final passage keywords over procedural keywords
3. Returns the highest-priority vote with a note indicating multiple votes exist

Final passage keywords (high priority): "final passage", "passage of the bill", "on passage", "third reading", "conference report"

Procedural keywords (low priority): "motion to commit", "motion to recommit", "cloture", "motion to table"

### Prevention

The duplicate votes issue can be mitigated by improving the `build_legislator_votes.py` to:
1. Only include final passage votes (not procedural)
2. Or clearly label each vote with its motion type

---

## Poor Search Ranking for Full Name Queries

### Symptom
The legislator-votes document exists and contains the correct data, but queries using the legislator's full name (e.g., "Ashley Moody vote HR1") don't return the document in top results. Queries using only last name (e.g., "Moody voting record") work fine.

### Example
```
Query: "Ashley Moody vote HR1"    -> Document NOT in top 15
Query: "Moody voting record HR1"  -> Document at position 1
```

### Root Cause
Vote records from OpenStates only include last names with party/state (e.g., "Moody (R-FL)"). When legislator-votes documents are built, they inherit this last-name-only format. The document title becomes "Moody (Republican-FL) - Voting Record" instead of "Ashley Moody (Republican-FL) - Voting Record".

Since semantic search relies on text similarity, queries with the full name "Ashley Moody" don't match well against documents that only contain "Moody".

### Diagnostic Steps

#### 1. Check if document has full name

```python
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def check_document_name(person_uuid):
    settings = get_settings()
    vs = VectorStoreService(settings)

    doc_id = f'legislator-votes-{person_uuid}-chunk-0'
    result = vs.index.fetch(ids=[doc_id], namespace=vs.namespace)

    if doc_id in result.vectors:
        meta = result.vectors[doc_id].metadata
        title = meta.get('title', '')
        content = meta.get('content', '')[:200]
        print(f'Title: {title}')
        print(f'Content start: {content}')

        # Check if it has only last name
        if title.startswith('Moody') and 'Ashley' not in title:
            print('\n⚠️  Document has last-name-only - needs rebuild with name enrichment')
    else:
        print('Document not found')

# Ashley Moody's UUID
asyncio.run(check_document_name("cb582ab6-6a5a-4578-9e44-620c9a6a1f4c"))
```

#### 2. Compare search rankings for full name vs last name

```python
async def compare_queries(full_name, last_name, person_uuid):
    settings = get_settings()
    vs = VectorStoreService(settings)

    doc_prefix = f'legislator-votes-{person_uuid}'

    for query in [f'{full_name} vote HR1', f'{last_name} voting record HR1']:
        results = await vs.query(query, top_k=15)
        position = None
        for i, r in enumerate(results):
            if doc_prefix in r.metadata.get('document_id', ''):
                position = i + 1
                break
        print(f'Query "{query}": Position {position if position else "NOT FOUND"}')

asyncio.run(compare_queries("Ashley Moody", "Moody", "cb582ab6-6a5a-4578-9e44-620c9a6a1f4c"))
```

### Solution

The `build_legislator_votes.py` module enriches legislator names from the federal legislator cache. Rebuild the index to apply name enrichment:

```bash
# Rebuild legislator-votes documents with full names
python -m votebot.sync.build_legislator_votes
```

The rebuild will:
1. Look up each legislator's person ID in the federal legislator cache
2. Replace last-name-only entries with full names (e.g., "Moody" → "Ashley Moody")
3. Include full names in document titles and content

After rebuild, verify the fix:
```python
# Should now show full name in title
asyncio.run(check_document_name("cb582ab6-6a5a-4578-9e44-620c9a6a1f4c"))
# Expected: "Ashley Moody (Republican-FL) - Voting Record"
```

### Prevention

The name enrichment feature is built into `build_legislator_votes.py`:
- Uses `federal_legislator_cache.get_by_person_id()` to look up full names
- Automatically enriches federal legislators during document creation
- Reports `name_enrichments` count in build results

Ensure the federal legislator cache is up-to-date before building:
```bash
python -m votebot.sync.federal_legislator_cache
python -m votebot.sync.build_legislator_votes
```

---

## Corrupted Legislator-Votes Documents

### Symptom
Valid legislator-votes documents exist but aren't appearing in search results because corrupted duplicates with similar content are ranking higher.

### Root Cause
When bill-votes content is split across Pinecone chunks, the regex parser can match partial person IDs at chunk boundaries, creating malformed entries like:
- `ocd-person/cb582ab6-6a` (truncated at chunk boundary)
- `44-620c9a6a1f4c` (partial UUID fragment)
- `PA), [ocd-person/cb582ab6...` (garbage prefix from prior entry)

These malformed IDs create fake `legislator-votes` documents that have similar content to valid documents, causing them to rank highly in semantic search.

### Diagnostic Steps

#### 1. Check for corrupted document IDs

```python
import asyncio
import re
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def find_corrupted():
    settings = get_settings()
    vs = VectorStoreService(settings)

    # Valid patterns
    valid_uuid = re.compile(
        r'^legislator-votes-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}(-chunk-\d+)?$'
    )
    valid_name = re.compile(r'^legislator-votes-[a-z\-]+(-chunk-\d+)?$')

    all_ids = []
    for ids in vs.index.list(namespace=vs.namespace, prefix='legislator-votes-'):
        all_ids.extend(ids)

    corrupted = [
        id for id in all_ids
        if not valid_uuid.match(id) and not valid_name.match(id)
    ]

    print(f'Total: {len(all_ids)}, Corrupted: {len(corrupted)}')
    for id in corrupted[:20]:
        print(f'  {repr(id)}')

asyncio.run(find_corrupted())
```

#### 2. Check search results for a specific legislator

Compare search results to see if corrupted documents are outranking valid ones:

```python
async def check_ranking(legislator_name, correct_uuid):
    settings = get_settings()
    vs = VectorStoreService(settings)

    results = await vs.query(f'{legislator_name} voting record', top_k=10)

    correct_doc = f'legislator-votes-{correct_uuid}'
    found_position = None

    for i, r in enumerate(results):
        doc_id = r.metadata.get('document_id', '')
        is_correct = correct_doc in doc_id
        marker = ' *** CORRECT ***' if is_correct else ''
        if is_correct:
            found_position = i + 1
        print(f'{i+1}. {r.score:.3f} | {doc_id[:60]}...{marker}')

    if found_position:
        print(f'\nCorrect document at position {found_position}')
    else:
        print('\nCorrect document NOT in top 10!')

asyncio.run(check_ranking("Moody", "cb582ab6-6a5a-4578-9e44-620c9a6a1f4c"))
```

### Solution

#### 1. Run the cleanup command

The `build_legislator_votes.py` module includes a cleanup function:

```bash
# Dry run to see what would be deleted
python -m votebot.sync.build_legislator_votes --cleanup --dry-run

# Actually delete corrupted documents
python -m votebot.sync.build_legislator_votes --cleanup
```

#### 2. Rebuild from scratch

For a complete fix, delete all legislator-votes and rebuild:

```python
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def delete_all_legislator_votes():
    settings = get_settings()
    vs = VectorStoreService(settings)

    all_ids = []
    for ids in vs.index.list(namespace=vs.namespace, prefix='legislator-votes-'):
        all_ids.extend(ids)

    print(f'Deleting {len(all_ids)} documents...')

    batch_size = 100
    for i in range(0, len(all_ids), batch_size):
        batch = all_ids[i:i+batch_size]
        vs.index.delete(ids=batch, namespace=vs.namespace)

    print('Done')

asyncio.run(delete_all_legislator_votes())
```

Then rebuild:
```bash
python -m votebot.sync.build_legislator_votes
```

### Prevention

The fix in `build_legislator_votes.py` includes:
1. **UUID validation**: `is_valid_person_id()` method validates the format `ocd-person/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`
2. **Skip malformed entries**: Parser skips person IDs that don't match the valid format
3. **Defense in depth**: Double-checking in both parsing and extraction functions

---

## Missing Data in Search Results

### Symptom
Content that should be in the index isn't appearing in search results.

### Diagnostic Steps

#### 1. Verify content exists in Pinecone

```python
async def search_by_id_prefix(prefix):
    settings = get_settings()
    vs = VectorStoreService(settings)

    all_ids = []
    for ids in vs.index.list(namespace=vs.namespace, prefix=prefix):
        all_ids.extend(ids)

    print(f'Found {len(all_ids)} documents with prefix "{prefix}"')
    for id in all_ids[:10]:
        print(f'  {id}')

asyncio.run(search_by_id_prefix("bill-votes-682f"))  # HR1 example
```

#### 2. Check document metadata

```python
async def check_metadata(doc_id):
    settings = get_settings()
    vs = VectorStoreService(settings)

    result = vs.index.fetch(ids=[doc_id], namespace=vs.namespace)

    if doc_id in result.vectors:
        meta = result.vectors[doc_id].metadata
        for k, v in sorted(meta.items()):
            if k != 'content':
                print(f'{k}: {v}')
    else:
        print(f'Document not found: {doc_id}')

asyncio.run(check_metadata("bill-votes-682f4c9a5a8d551cb4777414-chunk-0"))
```

#### 3. Check overall index stats

```python
async def index_stats():
    settings = get_settings()
    vs = VectorStoreService(settings)

    stats = vs.index.describe_index_stats()
    print(f'Total vectors: {stats.total_vector_count}')

    for prefix in ['bill-', 'legislator-', 'organization-', 'web-', 'training-']:
        count = 0
        for ids in vs.index.list(namespace=vs.namespace, prefix=prefix):
            count += len(ids)
        print(f'{prefix}: {count}')

asyncio.run(index_stats())
```

### Common Causes

1. **Sync never ran**: Content wasn't synced to Pinecone
2. **Metadata filtering**: Query uses filters that exclude the document
3. **Embedding mismatch**: Content doesn't semantically match the query
4. **Document chunked**: Content is in a different chunk than expected

---

## Federal Legislator Cache Issues

### Symptom
Federal legislators' person IDs aren't being matched in bill-votes documents.

### Background
OpenStates doesn't include person IDs in federal vote records - only voter names like "Moody (R-FL)". The federal legislator cache maps these names to person IDs.

### Diagnostic Steps

#### 1. Check cache contents

```bash
python -m votebot.sync.federal_legislator_cache --show
```

#### 2. Test name lookup

```python
from src.votebot.sync.federal_legislator_cache import get_federal_cache

cache = get_federal_cache()

test_names = [
    'Moody (R-FL)',
    'Scott (R-FL)',
    'Pelosi (D-CA)',
]

for name in test_names:
    result = cache.lookup(name)
    print(f'{name!r:25} -> {result}')
```

#### 3. Check cache file

```bash
cat data/cache/federal_legislators.json | python -c "
import json, sys
data = json.load(sys.stdin)
print(f'Total legislators: {len(data.get(\"legislators\", {}))}')
print(f'Last refreshed: {data.get(\"refreshed_at\", \"unknown\")}')
"
```

### Solution

Refresh the cache from OpenStates:

```bash
python -m votebot.sync.federal_legislator_cache
```

This fetches all 538 members of Congress and builds name variant mappings.

---

## Pinecone Index Diagnostics

### Quick Health Check

```python
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def health_check():
    settings = get_settings()
    vs = VectorStoreService(settings)

    # Overall stats
    stats = vs.index.describe_index_stats()
    print(f'Index: {settings.pinecone_index_name}')
    print(f'Namespace: {vs.namespace}')
    print(f'Total vectors: {stats.total_vector_count}')

    # Count by document type
    prefixes = {
        'bill-webflow-': 'Bill (Webflow)',
        'bill-pdf-': 'Bill (PDF)',
        'bill-history-': 'Bill (History)',
        'bill-votes-': 'Bill (Votes)',
        'legislator-ocd-person/': 'Legislator (Profile)',
        'legislator-bills-': 'Legislator (Bills)',
        'legislator-votes-': 'Legislator (Votes)',
        'organization-': 'Organization',
        'web-': 'Webpage',
        'training-': 'Training',
    }

    print('\n=== Document Counts ===')
    for prefix, label in prefixes.items():
        count = 0
        for ids in vs.index.list(namespace=vs.namespace, prefix=prefix):
            count += len(ids)
        if count > 0:
            print(f'{label}: {count}')

asyncio.run(health_check())
```

### Test Search Quality

```python
async def test_queries():
    settings = get_settings()
    vs = VectorStoreService(settings)

    test_cases = [
        ("HR1 One Big Beautiful Bill", "bill"),
        ("Rick Scott voting record", "legislator-votes"),
        ("ACLU bill positions", "organization"),
        ("What is Digital Democracy Project", "webpage"),
    ]

    for query, expected_type in test_cases:
        results = await vs.query(query, top_k=1)
        if results:
            r = results[0]
            doc_type = r.metadata.get('document_type', 'N/A')
            match = '✓' if expected_type in r.metadata.get('document_id', '') else '✗'
            print(f'{match} "{query[:40]}..." -> {doc_type} ({r.score:.3f})')
        else:
            print(f'✗ "{query[:40]}..." -> No results')

asyncio.run(test_queries())
```

---

## Full Index Rebuild Procedure

When all else fails, a complete rebuild ensures data consistency.

### Step 1: Backup current state (optional)

```python
async def export_stats():
    # Save current document counts for comparison
    # ... (run health check and save results)
    pass
```

### Step 2: Run full rebuild

```bash
# Non-interactive full rebuild
python scripts/rebuild_pinecone.py --yes

# Or step-by-step:
# 1. Wipe index
python -c "
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def wipe():
    settings = get_settings()
    vs = VectorStoreService(settings)
    await vs.delete(delete_all=True)
    print('Index wiped')

asyncio.run(wipe())
"

# 2. Sync all content types
python scripts/sync.py all

# 3. Build legislator-votes reverse index
python -m votebot.sync.build_legislator_votes
```

### Step 3: Verify rebuild

```bash
# Check document counts
python -c "..." # (health check script above)

# Test key queries
python -c "..." # (test queries script above)
```

### Expected Document Counts (approximate)

| Document Type | Expected Count |
|---------------|----------------|
| Bill (all types) | 20,000+ |
| Legislator (Profile) | 500-600 |
| Legislator (Bills) | 500-600 |
| Legislator (Votes) | 800-1200 |
| Organization | 1,500+ |
| Webpage | 10-20 |
| Training | 2-5 |
| **Total** | **24,000-26,000** |

---

## Getting Help

If these troubleshooting steps don't resolve your issue:

1. Check the application logs for errors
2. Review recent changes to sync code or data sources
3. Test with a minimal reproducible example
4. File an issue at https://github.com/VotingRightsBrigade/votebot/issues
