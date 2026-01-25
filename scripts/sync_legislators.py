#!/usr/bin/env python3
"""
Legislator sync script for VoteBot.

This script:
1. Fetches legislators from Webflow CMS (DDP-curated with scores)
2. Enriches with OpenStates data (current role, contact, committees)
3. Combines content from both sources
4. Ingests to Pinecone vector store with proper metadata

Usage:
    python scripts/sync_legislators.py [options]

Options:
    --limit N          Maximum legislators to process (default: 0 = unlimited)
    --skip-openstates  Skip OpenStates enrichment (Webflow only)
    --rate-limit N     Seconds between OpenStates API calls (default: 0.5)
    --dry-run          Print what would be done without ingesting
    --log-level LEVEL  Logging level (default: INFO)
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from votebot.config import get_settings

# Clear settings cache to ensure fresh env vars are loaded
get_settings.cache_clear()

from votebot.ingestion.metadata import DocumentMetadata, MetadataExtractor
from votebot.ingestion.pipeline import DocumentSource, IngestionPipeline
from votebot.ingestion.sources.openstates import OpenStatesSource
from votebot.ingestion.sources.webflow import WebflowSource
from votebot.utils.logging import setup_logging

import structlog

logger = structlog.get_logger()


async def fetch_webflow_legislators(
    webflow: WebflowSource,
    limit: int = 0,
) -> list[DocumentSource]:
    """
    Fetch all legislators from Webflow CMS.

    Args:
        webflow: WebflowSource instance
        limit: Maximum number to fetch (0 = unlimited)

    Returns:
        List of DocumentSource objects from Webflow
    """
    legislators = []
    async for doc in webflow.fetch_legislators(limit=limit):
        legislators.append(doc)
    return legislators


async def enrich_with_openstates(
    webflow_docs: list[DocumentSource],
    openstates: OpenStatesSource,
    rate_limit: float = 0.5,
    max_retries: int = 3,
) -> list[DocumentSource]:
    """
    Enrich Webflow legislators with OpenStates data.

    Args:
        webflow_docs: Legislators from Webflow
        openstates: OpenStatesSource instance
        rate_limit: Seconds between API calls
        max_retries: Maximum retries for rate-limited requests

    Returns:
        List of enriched DocumentSource objects
    """
    enriched = []
    enriched_count = 0
    failed_count = 0

    for i, webflow_doc in enumerate(webflow_docs):
        legislator_id = webflow_doc.metadata.legislator_id
        name = webflow_doc.metadata.title

        logger.info(
            f"Enriching legislator {i + 1}/{len(webflow_docs)}: {name}"
        )

        if not legislator_id:
            logger.warning(f"Skipping {name}: no OpenStates ID")
            enriched.append(webflow_doc)
            continue

        try:
            # Fetch from OpenStates
            os_doc = await openstates.fetch_legislator_by_id(
                legislator_id, max_retries=max_retries
            )

            if os_doc:
                # Combine content from both sources
                combined_content = _combine_content(webflow_doc, os_doc)

                # Merge metadata
                combined_metadata = _merge_metadata(
                    webflow_doc.metadata,
                    os_doc.metadata,
                )

                enriched.append(DocumentSource(
                    content=combined_content,
                    metadata=combined_metadata,
                ))
                enriched_count += 1
                logger.debug(f"Enriched {name} with OpenStates data")
            else:
                # Use Webflow data only
                enriched.append(webflow_doc)
                failed_count += 1
                logger.debug(f"No OpenStates data for {name}, using Webflow only")

        except Exception as e:
            logger.warning(f"Failed to enrich {name}: {e}")
            enriched.append(webflow_doc)
            failed_count += 1

        # Rate limiting
        if i < len(webflow_docs) - 1 and rate_limit > 0:
            await asyncio.sleep(rate_limit)

    logger.info(
        f"Enrichment complete: {enriched_count} enriched, {failed_count} Webflow-only"
    )
    return enriched


def enrich_with_cached_openstates(
    webflow_docs: list[DocumentSource],
    cache_file: Path,
) -> list[DocumentSource]:
    """
    Enrich Webflow legislators with cached OpenStates data.

    Uses pre-fetched OpenStates data from a JSON file to avoid API rate limits.

    Args:
        webflow_docs: Legislators from Webflow
        cache_file: Path to cached OpenStates JSON file

    Returns:
        List of enriched DocumentSource objects
    """
    # Load cached OpenStates data
    logger.info(f"Loading cached OpenStates data from {cache_file}")
    with open(cache_file) as f:
        os_data = json.load(f)

    # Build lookup by ID
    os_lookup = {item["id"]: item for item in os_data}
    logger.info(f"Loaded {len(os_lookup)} cached OpenStates legislators")

    enriched = []
    enriched_count = 0
    failed_count = 0

    for i, webflow_doc in enumerate(webflow_docs):
        legislator_id = webflow_doc.metadata.legislator_id
        name = webflow_doc.metadata.title

        if not legislator_id:
            logger.warning(f"Skipping {name}: no OpenStates ID")
            enriched.append(webflow_doc)
            continue

        os_item = os_lookup.get(legislator_id)
        if os_item:
            # Build OpenStates content from cached data
            os_content = _build_openstates_content(os_item)
            os_metadata = _build_openstates_metadata(os_item)

            # Create a pseudo DocumentSource for OpenStates
            os_doc = DocumentSource(content=os_content, metadata=os_metadata)

            # Combine content from both sources
            combined_content = _combine_content(webflow_doc, os_doc)

            # Merge metadata
            combined_metadata = _merge_metadata(
                webflow_doc.metadata,
                os_doc.metadata,
            )

            enriched.append(DocumentSource(
                content=combined_content,
                metadata=combined_metadata,
            ))
            enriched_count += 1
            logger.debug(f"Enriched {name} with cached OpenStates data")
        else:
            # Use Webflow data only
            enriched.append(webflow_doc)
            failed_count += 1
            logger.debug(f"No cached OpenStates data for {name} ({legislator_id})")

    logger.info(
        f"Cache enrichment complete: {enriched_count} enriched, {failed_count} Webflow-only"
    )
    return enriched


def _build_openstates_content(item: dict) -> str:
    """Build content string from cached OpenStates data."""
    parts = []

    name = item.get("name", "")
    if name:
        parts.append(f"# {name}")

    # Current role
    current_role = item.get("current_role", {})
    if current_role:
        title = current_role.get("title", "")
        district = current_role.get("district", "")
        org = current_role.get("org_classification", "")
        if title or district:
            role_str = f"**{title}**" if title else ""
            if district:
                role_str += f", District {district}"
            if org:
                role_str += f" ({org})"
            parts.append(role_str)

    # Party
    party = item.get("party", "")
    if party:
        parts.append(f"**Party:** {party}")

    # Contact info
    email = item.get("email", "")
    if email:
        parts.append(f"\n## Contact Information\n**Email:** {email}")

    # External links
    links = item.get("links", [])
    if links:
        parts.append("\n## External Links")
        for link in links:
            url = link.get("url", "")
            note = link.get("note", "Website")
            if url:
                parts.append(f"- [{note}]({url})")

    # Offices
    offices = item.get("offices", [])
    if offices:
        parts.append("\n## Offices")
        for office in offices:
            office_name = office.get("name", "Office")
            address = office.get("address", "")
            phone = office.get("voice", "")
            if address or phone:
                parts.append(f"**{office_name}**")
                if address:
                    parts.append(f"  Address: {address}")
                if phone:
                    parts.append(f"  Phone: {phone}")

    return "\n".join(parts)


def _build_openstates_metadata(item: dict) -> DocumentMetadata:
    """Build metadata from cached OpenStates data."""
    current_role = item.get("current_role", {})

    return DocumentMetadata(
        document_id=f"legislator-{item.get('id', '')}",
        document_type="legislator",
        source="openstates-cache",
        title=item.get("name", ""),
        jurisdiction=item.get("jurisdiction", {}).get("name", ""),
        legislator_id=item.get("id", ""),
        extra={
            "party": item.get("party", ""),
            "chamber": current_role.get("org_classification", ""),
            "district": current_role.get("district", ""),
            "email": item.get("email", ""),
            "image_url": item.get("image", ""),
            "title": current_role.get("title", ""),
        }
    )


def _combine_content(webflow_doc: DocumentSource, openstates_doc: DocumentSource) -> str:
    """
    Combine content from Webflow and OpenStates sources.

    Webflow content includes:
    - DDP scorecard and voting accountability
    - DDP score

    OpenStates content includes:
    - Current role details
    - Contact information
    - Office locations
    - External links

    Args:
        webflow_doc: Document from Webflow
        openstates_doc: Document from OpenStates

    Returns:
        Combined content string
    """
    parts = []

    # Use Webflow content first (has DDP-specific info)
    if webflow_doc.content:
        parts.append(webflow_doc.content)

    # Add OpenStates-specific sections that might not be in Webflow
    os_content = openstates_doc.content
    if os_content:
        # Extract sections from OpenStates that complement Webflow
        # Look for Contact Information and External Links sections
        sections_to_add = []

        lines = os_content.split("\n")
        current_section = []
        current_header = ""
        in_section = False

        for line in lines:
            if line.startswith("## "):
                # Save previous section if it's one we want
                if current_header and current_section:
                    if any(
                        kw in current_header.lower()
                        for kw in ["contact", "office", "link", "external"]
                    ):
                        sections_to_add.append(
                            f"{current_header}\n" + "\n".join(current_section)
                        )
                current_header = line
                current_section = []
                in_section = True
            elif in_section:
                current_section.append(line)

        # Don't forget the last section
        if current_header and current_section:
            if any(
                kw in current_header.lower()
                for kw in ["contact", "office", "link", "external"]
            ):
                sections_to_add.append(
                    f"{current_header}\n" + "\n".join(current_section)
                )

        # Add complementary sections
        for section in sections_to_add:
            # Only add if not already present in Webflow content
            section_header = section.split("\n")[0].lower()
            if section_header not in webflow_doc.content.lower():
                parts.append(section.strip())

    return "\n\n".join(filter(None, parts))


def _merge_metadata(
    webflow_meta: DocumentMetadata,
    openstates_meta: DocumentMetadata,
) -> DocumentMetadata:
    """
    Merge metadata from both sources, preferring Webflow for DDP-specific fields.

    Args:
        webflow_meta: Metadata from Webflow
        openstates_meta: Metadata from OpenStates

    Returns:
        Merged DocumentMetadata
    """
    # Start with Webflow metadata as base
    merged_extra = dict(webflow_meta.extra)

    # Add OpenStates fields that might be missing
    os_extra = openstates_meta.extra
    for key in ["email", "image_url", "title"]:
        if key not in merged_extra or not merged_extra.get(key):
            if os_extra.get(key):
                merged_extra[key] = os_extra[key]

    # Update chamber if not set
    if not merged_extra.get("chamber") and os_extra.get("chamber"):
        merged_extra["chamber"] = os_extra["chamber"]

    return DocumentMetadata(
        document_id=webflow_meta.document_id,
        document_type="legislator",
        source="webflow+openstates",
        title=webflow_meta.title,
        jurisdiction=webflow_meta.jurisdiction or openstates_meta.jurisdiction,
        legislator_id=webflow_meta.legislator_id,
        extra=merged_extra,
    )


async def sync_legislators(
    limit: int = 0,
    skip_openstates: bool = False,
    use_cache: bool = False,
    rate_limit: float = 0.5,
    dry_run: bool = False,
    max_retries: int = 3,
) -> dict:
    """
    Main sync function that orchestrates the legislator ingestion.

    Args:
        limit: Maximum legislators to process (0 = unlimited)
        skip_openstates: Skip OpenStates enrichment
        use_cache: Use cached OpenStates data instead of API
        rate_limit: Seconds between OpenStates API calls
        dry_run: If True, don't actually ingest
        max_retries: Maximum retries for rate-limited OpenStates requests

    Returns:
        Summary dict with stats
    """
    settings = get_settings()
    metadata_extractor = MetadataExtractor()

    webflow = WebflowSource(settings, metadata_extractor)
    openstates = OpenStatesSource(settings, metadata_extractor)

    print("=" * 70)
    print("LEGISLATOR SYNC")
    print("=" * 70)

    # Step 1: Fetch from Webflow
    print("\n[1/3] Fetching legislators from Webflow CMS...")
    webflow_docs = await fetch_webflow_legislators(webflow, limit=limit)
    print(f"      Fetched {len(webflow_docs)} legislators from Webflow")

    if not webflow_docs:
        print("      No legislators found in Webflow. Aborting.")
        return {"webflow_count": 0, "enriched_count": 0, "ingested_count": 0}

    # Step 2: Enrich with OpenStates (optional)
    if skip_openstates:
        print("\n[2/3] Skipping OpenStates enrichment (--skip-openstates)")
        final_docs = webflow_docs
    elif use_cache:
        cache_file = Path(__file__).parent / "legislators_openstates.json"
        if cache_file.exists():
            print(f"\n[2/3] Enriching with cached OpenStates data...")
            final_docs = enrich_with_cached_openstates(webflow_docs, cache_file)
            print(f"      Enriched {len(final_docs)} legislators from cache")
        else:
            print(f"\n[2/3] Cache file not found: {cache_file}")
            print("      Falling back to API enrichment...")
            final_docs = await enrich_with_openstates(
                webflow_docs, openstates, rate_limit=rate_limit, max_retries=max_retries
            )
            print(f"      Enriched {len(final_docs)} legislators")
    else:
        print(f"\n[2/3] Enriching with OpenStates data (rate limit: {rate_limit}s, max retries: {max_retries})...")
        final_docs = await enrich_with_openstates(
            webflow_docs, openstates, rate_limit=rate_limit, max_retries=max_retries
        )
        print(f"      Enriched {len(final_docs)} legislators")

    # Step 3: Ingest to vector store
    if dry_run:
        print("\n[3/3] DRY RUN - Would ingest the following legislators:")
        for doc in final_docs[:10]:
            print(f"      - {doc.metadata.title} ({doc.metadata.legislator_id})")
            print(f"        Jurisdiction: {doc.metadata.jurisdiction}")
            print(f"        Source: {doc.metadata.source}")
            extra = doc.metadata.extra
            if extra.get("ddp_score"):
                print(f"        DDP Score: {extra['ddp_score']}")
        if len(final_docs) > 10:
            print(f"      ... and {len(final_docs) - 10} more")
        result = {
            "webflow_count": len(webflow_docs),
            "enriched_count": len(final_docs),
            "ingested_count": 0,
        }
    else:
        print(f"\n[3/3] Ingesting {len(final_docs)} legislators to vector store...")
        pipeline = IngestionPipeline(settings)
        ingest_result = await pipeline.ingest_batch(final_docs)

        print(f"      Documents processed: {ingest_result.documents_processed}")
        print(f"      Chunks created: {ingest_result.chunks_created}")
        print(f"      Chunks upserted: {ingest_result.chunks_upserted}")
        if ingest_result.errors:
            print(f"      Errors: {len(ingest_result.errors)}")
            for error in ingest_result.errors[:5]:
                print(f"        - {error}")

        result = {
            "webflow_count": len(webflow_docs),
            "enriched_count": len(final_docs),
            "ingested_count": ingest_result.documents_processed,
            "chunks_created": ingest_result.chunks_created,
            "chunks_upserted": ingest_result.chunks_upserted,
            "errors": len(ingest_result.errors),
        }

    print("\n" + "=" * 70)
    print("SYNC COMPLETE")
    print("=" * 70)

    return result


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync legislators from Webflow and OpenStates to VoteBot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum legislators to process (default: 0 = unlimited)",
    )
    parser.add_argument(
        "--skip-openstates",
        action="store_true",
        help="Skip OpenStates enrichment (Webflow only)",
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        help="Use cached OpenStates data (from legislators_openstates.json) instead of API",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=0.5,
        help="Seconds between OpenStates API calls (default: 0.5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without ingesting",
    )
    parser.add_argument(
        "--overnight",
        action="store_true",
        help="Overnight mode: 10s rate limit, 10 retries, for slow batch enrichment",
    )
    parser.add_argument(
        "--enrich-only",
        action="store_true",
        help="Only enrich existing Webflow data with OpenStates (update in place)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()

    # Overnight mode overrides
    if args.overnight:
        args.rate_limit = 10.0  # 10 seconds between requests
        print("Overnight mode enabled: 10s rate limit, extended retries")

    # Setup logging
    setup_logging(args.log_level)

    try:
        result = await sync_legislators(
            limit=args.limit,
            skip_openstates=args.skip_openstates,
            use_cache=args.use_cache,
            rate_limit=args.rate_limit,
            dry_run=args.dry_run,
            max_retries=10 if args.overnight else 3,
        )

        print(f"\nSummary:")
        print(f"  Webflow legislators: {result['webflow_count']}")
        print(f"  Enriched legislators: {result['enriched_count']}")
        print(f"  Ingested to vector store: {result.get('ingested_count', 'N/A (dry run)')}")

    except Exception as e:
        print(f"\nSync failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
