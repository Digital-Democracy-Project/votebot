# Bill Document Enrichment Plan

## Overview
Enrich bill documents in VoteBot by:
1. Adding organization positions (support/oppose) for bidirectional relationship
2. Ensuring all relevant content fields are extracted
3. Adding metadata fields for citations and external links

## Current State Analysis

### What's Currently Extracted from Bills:

| Field | In Content | In Metadata | Notes |
|-------|------------|-------------|-------|
| `name` | ✓ | ✓ (title) | Bill title |
| `description` | ✓ (full) | ✓ (500 chars) | **Truncated in metadata** |
| `status` | ✓ | ✓ | Current status |
| `bill-session` | ✓ | ✓ | Legislative session |
| `bill-prefix` | ✓ | ✓ | Bill letter (HB, SB) |
| `bill-number` | ✓ | ✓ | Bill number |
| `support` | ✓ | ✗ | Arguments in support (HTML) |
| `oppose` | ✓ | ✗ | Arguments in opposition (HTML) |
| `post-body` | ✓ | ✗ | Main bill details (HTML) |
| `gov-url` | ✗ | ✓ (url) | PDF link |
| `jurisdiction` | ✗ | ✓ | State code |

### What's MISSING:

#### Organization Position References (Critical):

| Field | Type | Description |
|-------|------|-------------|
| `member-organizations` | MultiReference | Organizations that SUPPORT the bill |
| `organizations-oppose` | MultiReference | Organizations that OPPOSE the bill |

#### Metadata Fields for Citations & Links:

| Field | Type | Description |
|-------|------|-------------|
| `slug` | PlainText | DDP URL slug (for citation links) |
| `gov-url` | Link | Government bill text URL |
| `open-plural-url` | Link | Open Plural discussion URL |
| `kialo-url` | Link | Kialo debate URL |
| `session-code` | PlainText | Session code (e.g., "2024") |

### Bidirectional Relationship Problem:
```
Organizations → Bills: ✓ COMPLETE
  - fetch_organizations() resolves bills-support/bills-oppose
  - Bill names included in organization content

Bills → Organizations: ✗ INCOMPLETE
  - member-organizations field NOT extracted
  - organizations-oppose field NOT extracted
  - Cannot answer: "What organizations support/oppose this bill?"
```

---

## Implementation Plan

### Phase 1: Organization Mapping Cache

**File: `src/votebot/ingestion/sources/webflow.py`**

Add `_organization_cache` and `_build_organization_mapping()`:

```python
# In __init__:
self._organization_cache: dict[str, dict] = {}  # org_id -> {name, type}

async def _build_organization_mapping(
    self,
    client: httpx.AsyncClient,
    headers: dict,
) -> None:
    """
    Build mapping from organization IDs to organization names.

    Similar to _build_bill_mapping() but for organizations.
    """
```

### Phase 2: Enhance Bill Processing

**File: `src/votebot/ingestion/sources/webflow.py`**

Modify `_process_bill_item()` to:
1. Call `_build_organization_mapping()` before processing
2. Resolve `member-organizations` → supporting org names
3. Resolve `organizations-oppose` → opposing org names
4. Add organization positions to content
5. Add all metadata fields (citations, external links)

```python
async def _process_bill_item(
    self,
    item: dict,
    include_pdfs: bool = True,
) -> AsyncIterator[DocumentSource]:
    """Process bill with organization positions and full metadata."""
    fields = item.get("fieldData", {})

    # Resolve organization references (NEW)
    supporting_orgs = self._resolve_organization_references(
        fields.get("member-organizations", [])
    )
    opposing_orgs = self._resolve_organization_references(
        fields.get("organizations-oppose", [])
    )

    # Pass to content extraction
    cms_content = self._extract_bill_content(
        fields,
        supporting_orgs=supporting_orgs,
        opposing_orgs=opposing_orgs,
    )

    # Enhanced metadata with all fields
    metadata = DocumentMetadata(
        document_id=f"bill-webflow-{item_id}",
        document_type="bill",
        source="webflow-cms",
        title=name,
        jurisdiction=self._get_jurisdiction(fields),
        bill_id=self._get_bill_id(fields),
        url=fields.get("gov-url"),
        extra={
            "webflow_id": item_id,
            # Bill identification
            "slug": fields.get("slug", ""),  # DDP citation URL
            "session_code": fields.get("session-code", ""),
            "bill_prefix": fields.get("bill-prefix", ""),
            "bill_number": fields.get("bill-number", ""),
            "status": fields.get("status", ""),
            # External links
            "gov_url": fields.get("gov-url", ""),
            "open_plural_url": fields.get("open-plural-url", ""),
            "kialo_url": fields.get("kialo-url", ""),
            # Organization positions (NEW)
            "supporting_orgs_count": len(supporting_orgs),
            "opposing_orgs_count": len(opposing_orgs),
        }
    )
```

### Phase 3: Enhance Bill Content Structure

**File: `src/votebot/ingestion/sources/webflow.py`**

Update `_extract_bill_content()` signature and add organization section:

```python
def _extract_bill_content(
    self,
    fields: dict,
    supporting_orgs: list[dict] | None = None,
    opposing_orgs: list[dict] | None = None,
) -> str:
    """Extract text content from bill CMS fields with organization positions."""
    parts = []

    # ... existing content extraction ...

    # NEW: Organization Positions section
    if supporting_orgs or opposing_orgs:
        parts.append("## Organization Positions")

        if supporting_orgs:
            support_lines = ["### Organizations Supporting This Bill"]
            for org in supporting_orgs:
                org_name = org.get("name", "")
                org_type = org.get("type", "")
                if org_name:
                    line = f"- {org_name}"
                    if org_type:
                        line += f" ({org_type})"
                    support_lines.append(line)
            parts.append("\n".join(support_lines))

        if opposing_orgs:
            oppose_lines = ["### Organizations Opposing This Bill"]
            for org in opposing_orgs:
                org_name = org.get("name", "")
                org_type = org.get("type", "")
                if org_name:
                    line = f"- {org_name}"
                    if org_type:
                        line += f" ({org_type})"
                    oppose_lines.append(line)
            parts.append("\n".join(oppose_lines))

    return "\n\n".join(parts)
```

### Phase 4: Organization Reference Resolver

**File: `src/votebot/ingestion/sources/webflow.py`**

Add method to resolve organization references:

```python
def _resolve_organization_references(self, org_refs: list | None) -> list[dict]:
    """
    Resolve organization reference IDs to organization information.

    Args:
        org_refs: List of organization reference IDs from Webflow

    Returns:
        List of dicts with organization name and type
    """
    if not org_refs:
        return []

    resolved = []
    for ref in org_refs:
        if isinstance(ref, str):
            org_info = self._organization_cache.get(ref)
            if org_info:
                resolved.append(org_info)
            else:
                # Unknown org, include ID as fallback
                resolved.append({"name": f"Organization {ref[:8]}...", "type": ""})
        elif isinstance(ref, dict):
            resolved.append(ref)

    return resolved
```

### Phase 5: Update Bill Sync Script

**File: `scripts/sync_bills.py`** (modify existing)

Ensure the sync script:
1. Builds organization mapping before processing bills
2. Logs organization position statistics
3. Shows sample bills with org positions

```python
async def sync_bills():
    """Main sync with organization enrichment."""
    # Build org mapping first
    await webflow._build_organization_mapping(client, headers)

    # Process bills (org positions auto-included)
    async for doc in webflow.fetch_bills():
        ...

    # Log stats
    bills_with_support = sum(1 for b in bills if b.metadata.extra.get("supporting_orgs_count", 0) > 0)
    bills_with_oppose = sum(1 for b in bills if b.metadata.extra.get("opposing_orgs_count", 0) > 0)
    print(f"Bills with supporting organizations: {bills_with_support}")
    print(f"Bills with opposing organizations: {bills_with_oppose}")
```

---

## Data Flow After Enrichment

```
Webflow Bills Collection
        │
        ├── Bill Content Fields
        │   - name (title)
        │   - description (full text)
        │   - support (arguments in favor - HTML)
        │   - oppose (arguments against - HTML)
        │   - post-body (details - HTML)
        │
        ├── Bill Metadata Fields
        │   - slug (DDP citation URL)
        │   - session-code
        │   - bill-prefix, bill-number
        │   - status
        │   - gov-url, open-plural-url, kialo-url
        │
        ├── Organization References (NEW)
        │   - member-organizations → [org_ids]
        │   - organizations-oppose → [org_ids]
        │           │
        │           ▼
        │   Resolve via _organization_cache
        │   org_id → {name, type}
        │
        └── Combined Content:
            - Bill title and identification
            - Full description
            - Arguments for/against
            - Details from post-body
            - Organizations Supporting (NEW)
            - Organizations Opposing (NEW)
            - External resource links
                    │
                    ▼
             Chunking Service
             (750 tokens, 150 overlap)
                    │
                    ▼
             Pinecone Vector Store
             metadata: {
               document_type: "bill",
               slug: "hb-123-education",     (NEW)
               session_code: "2024",         (NEW)
               gov_url: "https://...",       (NEW)
               open_plural_url: "https://...",(NEW)
               kialo_url: "https://...",     (NEW)
               supporting_orgs_count: 5,     (NEW)
               opposing_orgs_count: 3,       (NEW)
               ...
             }
```

---

## Content Structure After Enrichment

```markdown
# {Bill Name}

**Bill Number:** {bill-prefix} {bill-number}
**Session:** {session-code}
**Status:** {status}
**Jurisdiction:** {jurisdiction}

## Description
{description - full text, not truncated}

## Arguments in Support
{support - HTML converted to text}

## Arguments in Opposition
{oppose - HTML converted to text}

## Details
{post-body - HTML converted to text}

## Organization Positions

### Organizations Supporting This Bill
- ACLU of Florida (Non-profit civil liberties organization)
- Florida Education Association (Teachers union)
- ...

### Organizations Opposing This Bill
- Florida Chamber of Commerce (Business association)
- ...

## External Resources
- [Government Bill Text]({gov-url})
- [Open Plural Discussion]({open-plural-url})
- [Kialo Debate]({kialo-url})
```

## Metadata Structure After Enrichment

```python
DocumentMetadata(
    document_id="bill-webflow-{webflow_id}",
    document_type="bill",
    source="webflow-cms",
    title="{bill name}",
    jurisdiction="{state code}",
    bill_id="{constructed bill ID}",
    url="{gov-url}",
    extra={
        # Identification
        "webflow_id": "{webflow item ID}",
        "slug": "{ddp-slug}",           # For DDP citation: digitaldemocracy.org/bills/{slug}
        "session_code": "{2024}",
        "bill_prefix": "{HB}",
        "bill_number": "{123}",
        "status": "{Passed}",

        # External links
        "gov_url": "{government PDF URL}",
        "open_plural_url": "{Open Plural URL}",
        "kialo_url": "{Kialo URL}",

        # Organization positions
        "supporting_orgs_count": 5,
        "opposing_orgs_count": 3,
    }
)
```

---

## Files to Modify

| File | Changes |
|------|---------|
| `src/votebot/ingestion/sources/webflow.py` | Add `_organization_cache`, `_build_organization_mapping()`, `_resolve_organization_references()`, modify `_process_bill_item()` for enhanced metadata, modify `_extract_bill_content()` to include org positions and external links |
| `scripts/sync_bills.py` | Add org mapping initialization, logging for org position stats, re-sync all bills with enriched data |

## Summary of New Fields

### Content Fields (ensure full extraction):
- `name` - Bill title ✓ (already extracted)
- `description` - Full description ✓ (already extracted, ensure not truncated)
- `post-body` - Bill details ✓ (already extracted)
- `support` - Arguments in support ✓ (already extracted)
- `oppose` - Arguments in opposition ✓ (already extracted)

### New Metadata Fields:
- `slug` - DDP URL slug for citations (NEW)
- `session-code` - Session code (NEW - was in `bill-session`)
- `gov-url` - Government bill text URL (move to explicit metadata field)
- `open-plural-url` - Open Plural discussion URL (NEW)
- `kialo-url` - Kialo debate URL (NEW)
- `bill-prefix` - Bill letter ✓ (already in metadata)
- `bill-number` - Bill number ✓ (already in metadata)

### New Organization References:
- `member-organizations` - Supporting organizations (NEW)
- `organizations-oppose` - Opposing organizations (NEW)

---

## Verification Plan

1. **Run bill sync with enrichment:**
   ```
   python scripts/sync_bills.py --dry-run --limit 10
   ```
   - Verify organization positions appear in bill content
   - Check metadata has org counts

2. **Query Pinecone directly:**
   ```python
   # Query for bills with organization positions
   results = await vs.query(
       query="bills supported by ACLU environmental organizations",
       filter={"document_type": "bill"}
   )
   ```

3. **Test RAG queries:**
   - "What organizations support HB 123?"
   - "Which groups oppose the education funding bill?"
   - "What bills are supported by the Chamber of Commerce?"

---

## Implementation Notes

1. **Reuse existing patterns:**
   - `_build_organization_mapping()` mirrors `_build_bill_mapping()`
   - `_resolve_organization_references()` mirrors `_resolve_bill_references()`
   - Same caching and pagination approach

2. **Backward compatibility:**
   - `supporting_orgs` and `opposing_orgs` params are optional
   - Existing bill processing still works without org data

3. **Performance considerations:**
   - Organization mapping built once, cached for all bills
   - ~1,311 organizations to map (similar to bill mapping)
   - No additional API calls per bill

4. **Data quality:**
   - Unknown population rate for `member-organizations` and `organizations-oppose`
   - Need to run sync to discover actual coverage
   - Some bills may have 0 org positions (expected)

---

## Implementation Status: COMPLETE ✓

**Completed: 2026-01-25**

### Sync Results:
| Metric | Value |
|--------|-------|
| Bills ingested | 839 |
| Chunks created | 1,417 |
| Bills with supporting orgs | 272 (32%) |
| Bills with opposing orgs | 119 (14%) |
| Bills with DDP slugs | 839 (100%) |
| Bills with external links | 839 (100%) |
| Organization mapping | 1,311 entries |

### Files Modified:
- `src/votebot/ingestion/sources/webflow.py` - Added org cache, mapping, resolution, enhanced metadata
- `scripts/sync_bills.py` - Created bill sync script with enrichment stats
- `scripts/test_rag_bills.py` - Created test script for bill RAG queries

### Bidirectional Relationship Complete:
- ✓ Organizations → Bills (from org sync)
- ✓ Bills → Organizations (from this enrichment)
