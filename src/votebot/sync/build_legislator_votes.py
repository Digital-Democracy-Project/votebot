"""Build legislator-votes documents from bill-votes data.

This creates a reverse index: for each legislator, extract their votes
from all bill-votes documents and create a dedicated legislator-votes
document with their complete voting record.

This enables queries like "how did Ashley Moody vote on HR1?" to find
the answer directly in the legislator's voting record, rather than
searching through bill-level vote lists.
"""

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import DocumentMetadata
from votebot.ingestion.pipeline import IngestionPipeline
from votebot.services.vector_store import VectorStoreService
from votebot.sync.federal_legislator_cache import get_federal_cache

logger = structlog.get_logger()


@dataclass
class ExtractedVote:
    """A vote extracted from a bill-votes document."""
    bill_id: str
    bill_title: str
    vote_date: str
    chamber: str
    motion: str
    result: str
    vote_option: str  # yes, no, not voting, etc.
    openstates_bill_id: str = ""  # ocd-bill/...


@dataclass
class LegislatorVoteRecord:
    """Accumulated votes for a legislator."""
    person_id: str  # OpenStates person ID (ocd-person/...)
    name: str
    party: str
    jurisdiction: str  # e.g., "FL", "US"
    votes: list[ExtractedVote]


class LegislatorVotesBuilder:
    """
    Builds legislator-votes documents from bill-votes data.

    Process:
    1. Fetch all bill-votes documents from Pinecone
    2. Parse each document to extract individual legislator votes
    3. Group votes by legislator (name + party + state)
    4. Create/update legislator-votes documents
    """

    # Regex pattern to validate OpenStates person IDs (ocd-person/UUID format)
    VALID_PERSON_ID_PATTERN = re.compile(
        r'^ocd-person/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
    )

    @classmethod
    def is_valid_person_id(cls, person_id: str) -> bool:
        """
        Validate that a person ID is a properly formatted OpenStates ID.

        Valid format: ocd-person/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
        where x is a lowercase hex digit.

        This prevents malformed IDs from chunk boundary parsing issues.
        """
        if not person_id:
            return False
        return bool(cls.VALID_PERSON_ID_PATTERN.match(person_id))

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.vector_store = VectorStoreService(self.settings)
        self.pipeline = IngestionPipeline(self.settings)

    async def build_all(self, dry_run: bool = False) -> dict:
        """
        Build legislator-votes documents for all legislators found in bill-votes.

        Args:
            dry_run: If True, don't actually write to Pinecone

        Returns:
            Dict with stats about the build process
        """
        logger.info("Starting legislator votes build from bill-votes documents")

        # Step 1: Fetch all bill-votes documents
        bill_votes_docs = await self._fetch_all_bill_votes()
        logger.info(f"Fetched {len(bill_votes_docs)} bill-votes documents")

        if not bill_votes_docs:
            return {"success": False, "error": "No bill-votes documents found"}

        # Step 2: Parse and extract votes by legislator
        legislator_votes = self._extract_votes_by_legislator(bill_votes_docs)
        logger.info(f"Extracted votes for {len(legislator_votes)} legislators")

        if dry_run:
            # Just report stats
            total_votes = sum(len(lv.votes) for lv in legislator_votes.values())
            return {
                "success": True,
                "dry_run": True,
                "legislators_found": len(legislator_votes),
                "total_votes": total_votes,
                "sample_legislators": list(legislator_votes.keys())[:10],
            }

        # Step 3: Create/update legislator-votes documents
        results = await self._create_legislator_votes_documents(legislator_votes)

        return results

    async def _fetch_all_bill_votes(self) -> list[dict]:
        """Fetch all bill-votes documents from Pinecone.

        Groups chunks by document ID and concatenates their content
        to reconstruct the full document for parsing.
        """
        from collections import defaultdict

        # Use Pinecone's list() to get all bill-votes vector IDs
        all_ids = []
        for ids in self.vector_store.index.list(
            namespace=self.vector_store.namespace,
            prefix="bill-votes-"
        ):
            all_ids.extend(ids)

        logger.info(f"Found {len(all_ids)} bill-votes vector IDs")

        # Fetch vectors in batches and group by base document ID
        chunks_by_doc: dict[str, list[tuple[int, str, dict]]] = defaultdict(list)
        batch_size = 100

        for i in range(0, len(all_ids), batch_size):
            batch_ids = all_ids[i:i + batch_size]
            fetch_result = self.vector_store.index.fetch(
                ids=batch_ids,
                namespace=self.vector_store.namespace
            )

            for vec_id, vec_data in fetch_result.vectors.items():
                metadata = vec_data.metadata or {}
                content = metadata.get("content", "")

                # Extract base document ID and chunk index
                # Format: bill-votes-{webflow_id}-chunk-{N}
                if "-chunk-" in vec_id:
                    base_id, chunk_part = vec_id.rsplit("-chunk-", 1)
                    try:
                        chunk_idx = int(chunk_part)
                    except ValueError:
                        chunk_idx = 0
                else:
                    base_id = vec_id
                    chunk_idx = 0

                chunks_by_doc[base_id].append((chunk_idx, content, metadata))

            if (i + batch_size) % 500 == 0:
                logger.info(f"Fetched {min(i + batch_size, len(all_ids))}/{len(all_ids)} bill-votes vectors")

        # Reconstruct full documents by concatenating chunks in order
        all_docs = []
        for base_id, chunks in chunks_by_doc.items():
            # Sort by chunk index
            chunks.sort(key=lambda x: x[0])

            # Concatenate content
            full_content = "\n".join(content for _, content, _ in chunks)

            # Use metadata from first chunk
            first_metadata = chunks[0][2] if chunks else {}

            all_docs.append({
                "id": base_id,
                "content": full_content,
                "metadata": first_metadata,
            })

        logger.info(f"Reconstructed {len(all_docs)} complete bill-votes documents from {len(all_ids)} chunks")

        return all_docs

    def _extract_votes_by_legislator(
        self,
        bill_votes_docs: list[dict]
    ) -> dict[str, LegislatorVoteRecord]:
        """
        Extract votes grouped by legislator using structured vote data from metadata.

        Uses the OpenStates person_id as the unique key for each legislator,
        which is more reliable than name-based matching.

        Args:
            bill_votes_docs: List of bill-votes documents with metadata

        Returns:
            Dict mapping person_id to their vote record
        """
        import json

        legislator_votes: dict[str, LegislatorVoteRecord] = {}
        docs_with_structured_data = 0
        docs_without_structured_data = 0

        for doc in bill_votes_docs:
            metadata = doc.get("metadata", {})

            # Extract bill info from document
            bill_id = metadata.get("bill_id", "")
            bill_title = metadata.get("title", "")
            jurisdiction = metadata.get("jurisdiction", "").upper()
            openstates_bill_id = metadata.get("openstates_bill_id", "")

            # Try to use structured vote data first (preferred)
            structured_votes_json = metadata.get("structured_votes", "")
            if structured_votes_json:
                try:
                    structured_votes = json.loads(structured_votes_json)
                    docs_with_structured_data += 1

                    for vote_data in structured_votes:
                        person_id = vote_data.get("person_id", "")
                        if not person_id:
                            continue

                        name = vote_data.get("name", "Unknown")
                        party = vote_data.get("party", "")
                        option = vote_data.get("option", "").lower()

                        # Create vote record
                        vote = ExtractedVote(
                            bill_id=bill_id,
                            bill_title=bill_title,
                            vote_date="",  # Not stored in structured data
                            chamber="",  # Not stored in structured data
                            motion="",  # Not stored in structured data
                            result="",  # Not stored in structured data
                            vote_option=option,
                            openstates_bill_id=openstates_bill_id,
                        )

                        # Add to legislator record (keyed by person_id)
                        if person_id not in legislator_votes:
                            legislator_votes[person_id] = LegislatorVoteRecord(
                                person_id=person_id,
                                name=name,
                                party=party,
                                jurisdiction=jurisdiction,
                                votes=[],
                            )
                        legislator_votes[person_id].votes.append(vote)

                    continue  # Successfully processed structured data

                except json.JSONDecodeError:
                    logger.warning(
                        "Failed to parse structured_votes JSON",
                        doc_id=doc.get("id", ""),
                    )

            # Parse from text content (works for both new and old documents)
            # New documents may have inline person IDs: [ocd-person/uuid]Name (Party-State)
            docs_without_structured_data += 1
            content = doc.get("content", "")
            votes_extracted = self._parse_bill_votes_content(
                content, bill_id, bill_title, jurisdiction, openstates_bill_id
            )

            # Add to legislator records
            for leg_key, person_id, vote in votes_extracted:
                # Double-check person_id validity (defense in depth)
                # Use person_id as key only if it's valid, otherwise use name-based key
                if person_id and not self.is_valid_person_id(person_id):
                    # Log and skip malformed person IDs (likely chunk boundary artifacts)
                    logger.debug(
                        "Skipping malformed person_id",
                        person_id=person_id[:50] if person_id else "",
                        leg_key=leg_key,
                    )
                    person_id = ""  # Clear invalid ID, fall back to name-based key

                key = person_id if person_id else leg_key
                if key not in legislator_votes:
                    name, party, state = self._parse_legislator_key(leg_key)
                    legislator_votes[key] = LegislatorVoteRecord(
                        person_id=person_id,
                        name=name,
                        party=party,
                        jurisdiction=state or jurisdiction,
                        votes=[],
                    )
                legislator_votes[key].votes.append(vote)

        logger.info(
            "Vote extraction complete",
            docs_with_structured_data=docs_with_structured_data,
            docs_without_structured_data=docs_without_structured_data,
            unique_legislators=len(legislator_votes),
        )

        return legislator_votes

    def _parse_bill_votes_content(
        self,
        content: str,
        bill_id: str,
        bill_title: str,
        jurisdiction: str = "",
        openstates_bill_id: str = "",
    ) -> list[tuple[str, str, ExtractedVote]]:
        """
        Parse a bill-votes document content to extract individual votes.

        The content format is typically:
        ### Senate Vote - 2025-07-01
        **Motion:** On Passage of the Bill
        **Result:** PASS (Yes: 50, No: 50, Other: 0)
        **Voted Yes:** Banks (R-IN), Moody (R-FL), Scott (R-FL), ...
        **Voted No:** Alsobrooks (D-MD), ...

        New format with inline person IDs (for state bills):
        **Voted Yes:** [ocd-person/uuid]Name (Party-State), ...

        Args:
            jurisdiction: Bill's jurisdiction (e.g., "US", "FL") - used to associate
                         state legislators with their state when not in vote list
            openstates_bill_id: OpenStates bill ID for metadata

        Returns:
            List of (legislator_key, person_id, ExtractedVote) tuples
        """
        results = []

        # Split into vote sections
        vote_sections = re.split(r'###\s+', content)

        for section in vote_sections:
            if not section.strip():
                continue

            # Parse section header for chamber and date
            header_match = re.match(
                r'(Senate|House|Upper|Lower)\s+Vote\s*[-–]\s*(\d{4}-\d{2}-\d{2})',
                section,
                re.IGNORECASE
            )

            if not header_match:
                # Try alternate format
                header_match = re.match(
                    r'(\w+)\s+Vote\s*[-–]\s*(\d{4}-\d{2}-\d{2})',
                    section,
                    re.IGNORECASE
                )

            chamber = ""
            vote_date = ""
            if header_match:
                chamber = header_match.group(1).lower()
                if chamber in ("upper", "senate"):
                    chamber = "Senate"
                elif chamber in ("lower", "house"):
                    chamber = "House"
                vote_date = header_match.group(2)

            # Extract motion
            motion_match = re.search(r'\*\*Motion:\*\*\s*(.+?)(?:\n|\*\*)', section)
            motion = motion_match.group(1).strip() if motion_match else ""

            # Extract result
            result_match = re.search(r'\*\*Result:\*\*\s*(PASS|FAIL|pass|fail)', section, re.IGNORECASE)
            result = result_match.group(1).upper() if result_match else ""

            # Extract voted yes
            yes_match = re.search(r'\*\*Voted Yes[^:]*:\*\*\s*(.+?)(?:\n\*\*|\n###|$)', section, re.DOTALL)
            if yes_match:
                yes_text = yes_match.group(1)
                legislators = self._parse_legislator_list(yes_text, jurisdiction)
                for leg_key, person_id in legislators:
                    vote = ExtractedVote(
                        bill_id=bill_id,
                        bill_title=bill_title,
                        vote_date=vote_date,
                        chamber=chamber,
                        motion=motion,
                        result=result,
                        vote_option="yes",
                        openstates_bill_id=openstates_bill_id,
                    )
                    results.append((leg_key, person_id, vote))

            # Extract voted no
            no_match = re.search(r'\*\*Voted No[^:]*:\*\*\s*(.+?)(?:\n\*\*|\n###|$)', section, re.DOTALL)
            if no_match:
                no_text = no_match.group(1)
                legislators = self._parse_legislator_list(no_text, jurisdiction)
                for leg_key, person_id in legislators:
                    vote = ExtractedVote(
                        bill_id=bill_id,
                        bill_title=bill_title,
                        vote_date=vote_date,
                        chamber=chamber,
                        motion=motion,
                        result=result,
                        vote_option="no",
                        openstates_bill_id=openstates_bill_id,
                    )
                    results.append((leg_key, person_id, vote))

            # Extract not voting / other
            other_match = re.search(r'\*\*Not Voting[^:]*:\*\*\s*(.+?)(?:\n\*\*|\n###|$)', section, re.DOTALL)
            if other_match:
                other_text = other_match.group(1)
                legislators = self._parse_legislator_list(other_text, jurisdiction)
                for leg_key, person_id in legislators:
                    vote = ExtractedVote(
                        bill_id=bill_id,
                        bill_title=bill_title,
                        vote_date=vote_date,
                        chamber=chamber,
                        motion=motion,
                        result=result,
                        vote_option="not voting",
                        openstates_bill_id=openstates_bill_id,
                    )
                    results.append((leg_key, person_id, vote))

        return results

    def _parse_legislator_list(
        self, text: str, jurisdiction: str = ""
    ) -> list[tuple[str, str]]:
        """
        Parse a comma-separated list of legislators.

        Handles multiple formats:
        - New format with person IDs: "[ocd-person/uuid]Name (Party-State), ..."
        - US Senate: "Banks (R-IN), Moody (R-FL), Scott (R-FL), ..."
        - US House: "Adams, Bean (FL), Amodei (NV), ..."
        - State legislatures: "Luke E. Torian, Robert S. Bloxom, Jr., ..."

        Args:
            text: The comma-separated list of legislators
            jurisdiction: Bill's jurisdiction (e.g., "US", "FL") - used to associate
                         state legislators with their state when not in vote list

        Returns:
            List of (leg_key, person_id) tuples. person_id is empty string if not present.
            leg_key format: "Banks|R|IN" or "Torian||VA" (state from jurisdiction)
        """
        legislators: list[tuple[str, str]] = []

        # First check for new format with inline person IDs: [ocd-person/uuid]Name (Party-State)
        # Pattern matches: [ocd-person/uuid]Name (Party-State) or [ocd-person/uuid]Name
        # Also handles malformed double suffix like [id]Name (R-FL) (R-FL)
        person_id_pattern = r'\[([^\]]+)\]([A-Za-z\'\-\.\s]+?)(?:\s*\(([RDI])-([A-Z]{2})\))+(?=,|\s*$|\.\.\.)'
        person_id_matches = list(re.finditer(person_id_pattern, text))

        if person_id_matches:
            for match in person_id_matches:
                person_id = match.group(1)  # ocd-person/uuid
                name = match.group(2).strip()
                party = match.group(3) or ""  # R, D, I
                state = match.group(4) or ""  # State code

                # Validate person_id format to prevent chunk boundary corruption
                # Malformed IDs from chunk boundaries look like:
                # "ocd-person/cb582ab6-6a" (truncated) or "44-620c9a6a1f4c" (partial)
                if not self.is_valid_person_id(person_id):
                    # Skip malformed entries - they're artifacts of chunk splitting
                    continue

                # Use jurisdiction as fallback for state
                if not state and jurisdiction and jurisdiction.upper() not in ["US", "UNITED STATES"]:
                    state = jurisdiction.upper()

                leg_key = f"{name}|{party}|{state}"
                legislators.append((leg_key, person_id))
            return legislators

        # Try US Senate format: Name (Party-State)
        senate_pattern = r'([A-Za-z\'\-\.\s]+?)\s*\(([RDI])-([A-Z]{2})\)'
        senate_matches = list(re.finditer(senate_pattern, text))

        if senate_matches:
            for match in senate_matches:
                name = match.group(1).strip()
                party = match.group(2)
                state = match.group(3)
                leg_key = f"{name}|{party}|{state}"
                legislators.append((leg_key, ""))  # No person_id available
            return legislators

        # For state-level bills (FL, VA, etc.), use jurisdiction as default state
        # For US bills, leave state empty (we can't know which state House members represent)
        default_state = ""
        if jurisdiction and jurisdiction.upper() not in ["US", "UNITED STATES"]:
            default_state = jurisdiction.upper()

        # Handle patterns like "...and 170 others" at the end
        text = re.sub(r'\.\.\.and \d+ others', '', text)

        # Check if this looks like full names (state legislature format)
        # State legislatures often use full names with middle initials
        has_full_names = bool(re.search(r'[A-Z][a-z]+\s+[A-Z]\.\s+[A-Z][a-z]+', text))

        if has_full_names:
            # State legislature format with full names
            # Handle "Jr.", "Sr.", "III" suffixes - temporarily replace comma before suffix
            text = re.sub(r',\s*(Jr\.|Sr\.|III|IV|II)\b', r' \1', text)

            # Split by comma
            entries = text.split(',')
            for entry in entries:
                entry = entry.strip()
                if not entry or len(entry) < 2:
                    continue

                # Skip junk entries
                if entry.lower() in ['and', 'others', 'voted', 'yes', 'no']:
                    continue

                # Extract the last name (usually last word after removing suffixes)
                # For "Luke E. Torian" -> "Torian"
                # For "Robert S. Bloxom Jr." -> "Bloxom"
                name_parts = entry.split()
                if not name_parts:
                    continue

                # Remove suffixes and find last name
                suffixes = {'jr.', 'jr', 'sr.', 'sr', 'ii', 'iii', 'iv'}
                name_parts = [p for p in name_parts if p.lower() not in suffixes]

                if not name_parts:
                    continue

                # Last name is typically the last part after removing suffixes
                # But handle "Buddy" nicknames in quotes
                last_name = name_parts[-1].strip('"').strip("'")

                if last_name and len(last_name) > 1 and last_name[0].isupper():
                    leg_key = f"{last_name}||{default_state}"
                    legislators.append((leg_key, ""))  # No person_id available
        else:
            # US House/simple format: short names, optionally with (State)
            entries = text.split(',')
            for entry in entries:
                entry = entry.strip()
                if not entry or len(entry) < 2:
                    continue

                # Check if it has state: "Bean (FL)"
                state_match = re.match(r'([A-Za-z\'\-\.]+)\s*\(([A-Z]{2})\)', entry)
                if state_match:
                    name = state_match.group(1).strip()
                    state = state_match.group(2)
                    leg_key = f"{name}||{state}"
                    if name and len(name) > 1:
                        legislators.append((leg_key, ""))  # No person_id available
                else:
                    # Just a name
                    name = re.sub(r'[^A-Za-z\'\-\.\s]', '', entry).strip()
                    if name and len(name) > 1:
                        leg_key = f"{name}||{default_state}"
                        legislators.append((leg_key, ""))  # No person_id available

        return legislators

    def _parse_legislator_key(self, key: str) -> tuple[str, str, str]:
        """Parse a legislator key back into name, party, state."""
        parts = key.split("|")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        return key, "", ""

    async def _create_legislator_votes_documents(
        self,
        legislator_votes: dict[str, LegislatorVoteRecord]
    ) -> dict:
        """
        Create legislator-votes documents in Pinecone.

        Args:
            legislator_votes: Dict mapping person_id (or legacy key) to LegislatorVoteRecord

        Returns:
            Stats about created documents
        """
        created = 0
        failed = 0
        total_chunks = 0
        errors = []

        # Load federal legislator cache to get full names
        federal_cache = get_federal_cache()
        name_enrichments = 0

        for leg_key, record in legislator_votes.items():
            try:
                # Enrich with full name from federal legislator cache
                # Vote records only have last names like "Moody", but we need
                # full names like "Ashley Moody" for better search matching
                if record.person_id:
                    cached_info = federal_cache.get_by_person_id(record.person_id)
                    if cached_info and cached_info.get("name"):
                        full_name = cached_info["name"]
                        if full_name != record.name:
                            logger.debug(
                                "Enriching legislator name from cache",
                                original=record.name,
                                full_name=full_name,
                            )
                            record.name = full_name
                            name_enrichments += 1
                            # Also update party/jurisdiction if available
                            if cached_info.get("party") and not record.party:
                                record.party = cached_info["party"]
                            if cached_info.get("state") and not record.jurisdiction:
                                record.jurisdiction = cached_info["state"]

                # Format the document content
                content = self._format_legislator_votes_document(record)

                # Create document ID
                # Prefer person_id (ocd-person/...) for stable, unique IDs
                if record.person_id and record.person_id.startswith("ocd-person/"):
                    # Extract the UUID part from ocd-person/uuid
                    person_uuid = record.person_id.replace("ocd-person/", "")
                    doc_id = f"legislator-votes-{person_uuid}"
                else:
                    # Fall back to name-based ID for legacy data
                    safe_name = re.sub(r'[^a-zA-Z0-9]', '-', record.name.lower())
                    jurisdiction_part = record.jurisdiction.lower() if record.jurisdiction else "unknown"
                    doc_id = f"legislator-votes-{safe_name}-{jurisdiction_part}"

                # Normalize party
                party_full = {
                    "R": "Republican",
                    "D": "Democratic",
                    "I": "Independent",
                    "Republican": "Republican",
                    "Democratic": "Democratic",
                    "Independent": "Independent",
                }.get(record.party, record.party) if record.party else ""

                # Build title based on available info
                if party_full and record.jurisdiction:
                    title = f"{record.name} ({party_full}-{record.jurisdiction}) - Voting Record"
                elif record.jurisdiction:
                    title = f"{record.name} ({record.jurisdiction}) - Voting Record"
                else:
                    title = f"{record.name} - Voting Record"

                metadata = DocumentMetadata(
                    document_id=doc_id,
                    document_type="legislator-votes",
                    source="Digital Democracy Project",
                    title=title,
                    jurisdiction=record.jurisdiction if record.jurisdiction else "US",
                    extra={
                        "openstates_person_id": record.person_id,
                        "legislator_name": record.name,
                        "party": party_full,
                        "jurisdiction": record.jurisdiction or "",
                        "total_votes": len(record.votes),
                        "yes_votes": len([v for v in record.votes if v.vote_option == "yes"]),
                        "no_votes": len([v for v in record.votes if v.vote_option == "no"]),
                        "built_from": "bill-votes-reverse-index",
                        "last_built": date.today().isoformat(),
                    },
                )

                # Ingest the document
                result = await self.pipeline.ingest_document(
                    content=content,
                    metadata=metadata,
                    skip_duplicates=False,  # Always update
                )

                created += 1
                total_chunks += result.chunks_created

                if created % 50 == 0:
                    logger.info(f"Progress: created {created} legislator-votes documents")

            except Exception as e:
                failed += 1
                errors.append(f"{record.name}: {str(e)}")
                logger.error(f"Failed to create document for {record.name}: {e}")

        logger.info(
            "Legislator votes build complete",
            created=created,
            failed=failed,
            total_chunks=total_chunks,
            name_enrichments=name_enrichments,
        )

        return {
            "success": failed == 0 or created > 0,
            "legislators_processed": created + failed,
            "documents_created": created,
            "documents_failed": failed,
            "chunks_created": total_chunks,
            "name_enrichments": name_enrichments,
            "errors": errors[:10] if errors else [],  # Limit error list
        }

    def _format_legislator_votes_document(self, record: LegislatorVoteRecord) -> str:
        """
        Format a legislator's votes into a document for embedding.

        Args:
            record: LegislatorVoteRecord with all their votes

        Returns:
            Formatted markdown content
        """
        party_full = {
            "R": "Republican",
            "D": "Democratic",
            "I": "Independent",
            "Republican": "Republican",
            "Democratic": "Democratic",
            "Independent": "Independent",
        }.get(record.party, record.party) if record.party else ""

        parts = []
        parts.append(f"# {record.name} - Voting Record")
        if record.person_id:
            parts.append(f"**OpenStates ID:** {record.person_id}")
        if party_full:
            parts.append(f"**Party:** {party_full}")
        if record.jurisdiction:
            parts.append(f"**Jurisdiction:** {record.jurisdiction}")
        parts.append("")

        # Summary stats
        yes_votes = [v for v in record.votes if v.vote_option == "yes"]
        no_votes = [v for v in record.votes if v.vote_option == "no"]
        other_votes = [v for v in record.votes if v.vote_option not in ("yes", "no")]

        parts.append("## Voting Summary")
        parts.append(f"- **Total Votes:** {len(record.votes)}")
        parts.append(f"- **Voted Yes:** {len(yes_votes)}")
        parts.append(f"- **Voted No:** {len(no_votes)}")
        if other_votes:
            parts.append(f"- **Other (not voting/abstain):** {len(other_votes)}")
        parts.append("")

        # Sort votes by date (most recent first)
        sorted_votes = sorted(record.votes, key=lambda v: v.vote_date or "", reverse=True)

        # Group by bill to avoid duplicates (same bill may have multiple votes)
        votes_by_bill: dict[str, list[ExtractedVote]] = defaultdict(list)
        for vote in sorted_votes:
            votes_by_bill[vote.bill_id].append(vote)

        # Yes votes section
        if yes_votes:
            parts.append("## Bills Voted YES")
            seen_bills = set()
            for vote in sorted_votes:
                if vote.vote_option == "yes" and vote.bill_id not in seen_bills:
                    seen_bills.add(vote.bill_id)
                    line = f"- **{vote.bill_id}**"
                    if vote.bill_title and vote.bill_title != vote.bill_id:
                        title = vote.bill_title[:60] + "..." if len(vote.bill_title) > 60 else vote.bill_title
                        line += f": {title}"
                    if vote.vote_date:
                        line += f" ({vote.vote_date})"
                    if vote.chamber:
                        line += f" [{vote.chamber}]"
                    parts.append(line)
                    if len(seen_bills) >= 50:  # Limit to prevent huge documents
                        remaining = len([v for v in yes_votes if v.bill_id not in seen_bills])
                        if remaining > 0:
                            parts.append(f"- ...and {remaining} more YES votes")
                        break
            parts.append("")

        # No votes section
        if no_votes:
            parts.append("## Bills Voted NO")
            seen_bills = set()
            for vote in sorted_votes:
                if vote.vote_option == "no" and vote.bill_id not in seen_bills:
                    seen_bills.add(vote.bill_id)
                    line = f"- **{vote.bill_id}**"
                    if vote.bill_title and vote.bill_title != vote.bill_id:
                        title = vote.bill_title[:60] + "..." if len(vote.bill_title) > 60 else vote.bill_title
                        line += f": {title}"
                    if vote.vote_date:
                        line += f" ({vote.vote_date})"
                    if vote.chamber:
                        line += f" [{vote.chamber}]"
                    parts.append(line)
                    if len(seen_bills) >= 50:
                        remaining = len([v for v in no_votes if v.bill_id not in seen_bills])
                        if remaining > 0:
                            parts.append(f"- ...and {remaining} more NO votes")
                        break
            parts.append("")

        # Other votes (limited)
        if other_votes:
            parts.append("## Other Votes (Not Voting/Abstain)")
            seen_bills = set()
            for vote in sorted_votes:
                if vote.vote_option not in ("yes", "no") and vote.bill_id not in seen_bills:
                    seen_bills.add(vote.bill_id)
                    line = f"- **{vote.bill_id}** [{vote.vote_option}]"
                    if vote.vote_date:
                        line += f" ({vote.vote_date})"
                    parts.append(line)
                    if len(seen_bills) >= 20:
                        break

        return "\n".join(parts)

    async def cleanup_corrupted_documents(self, dry_run: bool = False) -> dict:
        """
        Remove corrupted legislator-votes documents from Pinecone.

        Corrupted documents are identified by malformed document IDs that don't
        follow the pattern: legislator-votes-{valid-uuid}

        These are typically caused by chunk boundary parsing issues where
        person IDs get split across chunks.

        Args:
            dry_run: If True, only report what would be deleted

        Returns:
            Dict with cleanup stats
        """
        logger.info("Starting cleanup of corrupted legislator-votes documents")

        # Valid document ID pattern: legislator-votes-{uuid} or legislator-votes-{name}-{jurisdiction}
        # UUID format: 8-4-4-4-12 hex chars
        valid_uuid_pattern = re.compile(
            r'^legislator-votes-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}(-chunk-\d+)?$'
        )
        # Name-based pattern for legacy docs: legislator-votes-{name}-{jurisdiction}
        valid_name_pattern = re.compile(
            r'^legislator-votes-[a-z\-]+(-chunk-\d+)?$'
        )

        # List all legislator-votes vector IDs
        all_ids = []
        for ids in self.vector_store.index.list(
            namespace=self.vector_store.namespace,
            prefix="legislator-votes-"
        ):
            all_ids.extend(ids)

        logger.info(f"Found {len(all_ids)} total legislator-votes vector IDs")

        # Identify corrupted IDs
        corrupted_ids = []
        valid_ids = []

        for vec_id in all_ids:
            if valid_uuid_pattern.match(vec_id) or valid_name_pattern.match(vec_id):
                valid_ids.append(vec_id)
            else:
                corrupted_ids.append(vec_id)

        logger.info(
            f"Identified {len(corrupted_ids)} corrupted documents, "
            f"{len(valid_ids)} valid documents"
        )

        if dry_run:
            # Report what would be deleted
            sample_corrupted = corrupted_ids[:20]
            return {
                "success": True,
                "dry_run": True,
                "total_docs": len(all_ids),
                "corrupted_docs": len(corrupted_ids),
                "valid_docs": len(valid_ids),
                "sample_corrupted": sample_corrupted,
            }

        # Delete corrupted documents in batches
        deleted = 0
        batch_size = 100

        for i in range(0, len(corrupted_ids), batch_size):
            batch = corrupted_ids[i:i + batch_size]
            try:
                self.vector_store.index.delete(
                    ids=batch,
                    namespace=self.vector_store.namespace
                )
                deleted += len(batch)
                if deleted % 500 == 0:
                    logger.info(f"Deleted {deleted}/{len(corrupted_ids)} corrupted documents")
            except Exception as e:
                logger.error(f"Failed to delete batch: {e}")

        logger.info(
            "Cleanup complete",
            deleted=deleted,
            remaining_valid=len(valid_ids),
        )

        return {
            "success": True,
            "deleted": deleted,
            "remaining_valid": len(valid_ids),
            "corrupted_ids_sample": corrupted_ids[:10] if corrupted_ids else [],
        }


async def main():
    """CLI entry point for building legislator votes."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Build legislator-votes documents from bill-votes data"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write to Pinecone, just report stats",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Clean up corrupted legislator-votes documents instead of building",
    )

    args = parser.parse_args()

    builder = LegislatorVotesBuilder()

    if args.cleanup:
        results = await builder.cleanup_corrupted_documents(dry_run=args.dry_run)
        print("\n=== Cleanup Results ===")
    else:
        results = await builder.build_all(dry_run=args.dry_run)
        print("\n=== Build Results ===")

    for key, value in results.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
