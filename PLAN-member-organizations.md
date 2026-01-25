# Member Organizations Integration Plan

## Overview
Add member organization support to VoteBot by ingesting data from Webflow CMS, enabling organization-specific queries and bill position lookups.

## Current State Analysis

### Data Source: Webflow Member Organizations Collection
- **Total organizations:** 1,311
- **Collection ID:** `65bd4aca31deb7e14e53d5dc`

### Key Fields:
| Field | Type | Population | Description |
|-------|------|------------|-------------|
| `name` | PlainText | 100% | Organization name |
| `about-organization` | PlainText | 100% | Description |
| `description-4` | PlainText | 100% | Extended description |
| `type-2` | PlainText | 100% | Organization type |
| `policies-2` | PlainText | 98% | Policy positions |
| `funding-2` | PlainText | 97% | Funding sources |
| `affiliates-2` | PlainText | 95% | Affiliate organizations |
| `website` | Link | 83% | Organization website |
| `bills-support` | MultiReference | 66% | Bills they support |
| `bills-oppose` | MultiReference | 32% | Bills they oppose |

### Bidirectional Relationship with Bills:
Bills collection has:
- `member-organizations` (MultiReference) → Organizations that support
- `organizations-oppose` (MultiReference) → Organizations that oppose

This allows lookups in both directions.

## Implementation Plan

### Phase 1: WebflowSource Enhancement

**File: `src/votebot/ingestion/sources/webflow.py`**

Add `fetch_organizations()` method:

```python
async def fetch_organizations(self, limit: int = 0) -> AsyncIterator[DocumentSource]:
    """
    Fetch member organizations from Webflow CMS.

    - Resolves bill references to bill IDs/names
    - Creates content with organization info + bill positions
    """
```

Key implementation details:
1. Fetch all organizations (paginated)
2. Build bill ID → bill name mapping for readable content
3. Extract positions (support/oppose) with bill names
4. Create DocumentMetadata with `document_type="organization"`

### Phase 2: Metadata Schema

**File: `src/votebot/ingestion/metadata.py`**

Add `extract_organization_metadata()`:

```python
DocumentMetadata(
    document_id=f"organization-{webflow_id}",
    document_type="organization",
    source="webflow-cms",
    title=name,
    extra={
        "organization_type": type,
        "webflow_id": webflow_id,
        "website": website,
        "bills_support_count": len(bills_support),
        "bills_oppose_count": len(bills_oppose),
    }
)
```

### Phase 3: Content Structure

Organization documents will contain:

```markdown
# {Organization Name}

**Type:** {organization type}
**Website:** {url}

## About
{about-organization / description}

## Policy Positions
{policies}

## Funding
{funding sources}

## Affiliates
{affiliate organizations}

## Bill Positions

### Bills Supported
- {Bill Name 1} ({Bill ID})
- {Bill Name 2} ({Bill ID})
...

### Bills Opposed
- {Bill Name 1} ({Bill ID})
- {Bill Name 2} ({Bill ID})
...
```

### Phase 4: Sync Script

**File: `scripts/sync_organizations.py`** (new)

```python
async def sync_organizations():
    """
    1. Fetch all organizations from Webflow
    2. Build bill reference mapping
    3. For each organization:
       a. Resolve bill references to names
       b. Create content with positions
       c. Create DocumentSource with metadata
    4. Ingest to vector store
    """
```

### Phase 5: Query Capabilities

After ingestion, VoteBot can answer:

1. **Organization info:** "What is the ACLU?"
2. **Policy positions:** "What policies does the Chamber of Commerce support?"
3. **Bill positions:** "Which organizations oppose HB 123?"
4. **Affiliation queries:** "What organizations are affiliated with [group]?"

### Phase 6: Bill Document Enrichment (Optional)

Consider adding organization positions to bill documents:
- Add `supporting_orgs` and `opposing_orgs` to bill metadata
- Enables filtering: "Show bills opposed by environmental groups"

---

## Data Flow

```
Webflow CMS
(1,311 organizations)
        │
        ├── Organization Info
        │   - name, type, policies
        │   - funding, affiliates
        │
        └── Bill Positions
            - bills-support → [bill_ids]
            - bills-oppose → [bill_ids]
                    │
                    ▼
            Resolve bill IDs → names
            via bills collection
                    │
                    ▼
         Combined Content:
         - Organization profile
         - Policy positions
         - Bill stances with names
                    │
                    ▼
         Chunking Service
         (750 tokens, 150 overlap)
                    │
                    ▼
         Pinecone Vector Store
         metadata: {
           document_type: "organization",
           organization_type: "nonprofit",
           bills_support_count: 5,
           bills_oppose_count: 2
         }
```

---

## Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `src/votebot/ingestion/sources/webflow.py` | Modify | Add `fetch_organizations()` method |
| `src/votebot/ingestion/metadata.py` | Modify | Add `extract_organization_metadata()` |
| `scripts/sync_organizations.py` | Create | Organization ingestion script |
| `scripts/test_rag_organizations.py` | Create | Test script for organization queries |

---

## Verification Plan

1. **Run ingestion:** `python scripts/sync_organizations.py`
   - Verify all 1,311 organizations ingested
   - Check Pinecone for organization documents

2. **Query Pinecone directly:**
   - Filter by `document_type="organization"`
   - Verify bill positions in content

3. **Test queries:**
   - "What is [organization name]?"
   - "Which organizations support [bill]?"
   - "What are [organization]'s policy positions?"

---

## Estimated Effort

- WebflowSource enhancement: ~100 lines
- Metadata extraction: ~30 lines
- Sync script: ~150 lines
- Test script: ~100 lines
- **Total: ~380 lines of code**

---

## Design Decisions

1. **Organizations do NOT have jurisdiction metadata**
   - Organizations can be local, state, or national
   - No strict jurisdiction enforcement

2. **No separate relationship documents**
   - Bill positions included in organization content
   - Simpler architecture, fewer vectors

---

## Follow-up Task: Bill Document Enrichment

After organizations are complete, enrich bill documents with:
- Organization support/oppose positions
- Vote Yes/No statements
- Full bill descriptions
- Any other Webflow CMS fields not currently ingested

This will be planned separately after organization integration.
