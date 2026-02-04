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


@dataclass
class LegislatorVoteRecord:
    """Accumulated votes for a legislator."""
    name: str
    party: str
    state: str  # e.g., "FL", "TX"
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
        """Fetch all bill-votes documents from Pinecone."""
        all_docs = []

        # Query with a broad search to get bill-votes documents
        # We'll need to paginate through results
        queries = [
            "vote voting record senate house passed failed",
            "voted yes no legislator senator representative",
            "roll call vote tally passage",
        ]

        seen_ids = set()

        for query in queries:
            results = await self.vector_store.query(
                query=query,
                top_k=100,  # Max allowed
                filter={"document_type": "bill-votes"},
            )

            for r in results:
                if r.id not in seen_ids:
                    seen_ids.add(r.id)
                    all_docs.append({
                        "id": r.id,
                        "content": r.content,
                        "metadata": r.metadata,
                    })

        # Also try to get documents by listing (if the vector store supports it)
        # For now, we rely on the query approach

        return all_docs

    def _extract_votes_by_legislator(
        self,
        bill_votes_docs: list[dict]
    ) -> dict[str, LegislatorVoteRecord]:
        """
        Parse bill-votes documents and extract votes grouped by legislator.

        Args:
            bill_votes_docs: List of bill-votes documents with content

        Returns:
            Dict mapping legislator key to their vote record
        """
        legislator_votes: dict[str, LegislatorVoteRecord] = {}

        for doc in bill_votes_docs:
            content = doc.get("content", "")
            metadata = doc.get("metadata", {})

            # Extract bill info from document
            bill_id = metadata.get("bill_id", "")
            bill_title = metadata.get("title", "")

            # Parse the content to find vote sections and extract individual votes
            votes_extracted = self._parse_bill_votes_content(content, bill_id, bill_title)

            # Add to legislator records
            for leg_key, vote in votes_extracted:
                if leg_key not in legislator_votes:
                    # Parse the key to get name, party, state
                    name, party, state = self._parse_legislator_key(leg_key)
                    legislator_votes[leg_key] = LegislatorVoteRecord(
                        name=name,
                        party=party,
                        state=state,
                        votes=[],
                    )
                legislator_votes[leg_key].votes.append(vote)

        return legislator_votes

    def _parse_bill_votes_content(
        self,
        content: str,
        bill_id: str,
        bill_title: str,
    ) -> list[tuple[str, ExtractedVote]]:
        """
        Parse a bill-votes document content to extract individual votes.

        The content format is typically:
        ### Senate Vote - 2025-07-01
        **Motion:** On Passage of the Bill
        **Result:** PASS (Yes: 50, No: 50, Other: 0)
        **Voted Yes:** Banks (R-IN), Moody (R-FL), Scott (R-FL), ...
        **Voted No:** Alsobrooks (D-MD), ...

        Returns:
            List of (legislator_key, ExtractedVote) tuples
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
                legislators = self._parse_legislator_list(yes_text)
                for leg_key in legislators:
                    vote = ExtractedVote(
                        bill_id=bill_id,
                        bill_title=bill_title,
                        vote_date=vote_date,
                        chamber=chamber,
                        motion=motion,
                        result=result,
                        vote_option="yes",
                    )
                    results.append((leg_key, vote))

            # Extract voted no
            no_match = re.search(r'\*\*Voted No[^:]*:\*\*\s*(.+?)(?:\n\*\*|\n###|$)', section, re.DOTALL)
            if no_match:
                no_text = no_match.group(1)
                legislators = self._parse_legislator_list(no_text)
                for leg_key in legislators:
                    vote = ExtractedVote(
                        bill_id=bill_id,
                        bill_title=bill_title,
                        vote_date=vote_date,
                        chamber=chamber,
                        motion=motion,
                        result=result,
                        vote_option="no",
                    )
                    results.append((leg_key, vote))

            # Extract not voting / other
            other_match = re.search(r'\*\*Not Voting[^:]*:\*\*\s*(.+?)(?:\n\*\*|\n###|$)', section, re.DOTALL)
            if other_match:
                other_text = other_match.group(1)
                legislators = self._parse_legislator_list(other_text)
                for leg_key in legislators:
                    vote = ExtractedVote(
                        bill_id=bill_id,
                        bill_title=bill_title,
                        vote_date=vote_date,
                        chamber=chamber,
                        motion=motion,
                        result=result,
                        vote_option="not voting",
                    )
                    results.append((leg_key, vote))

        return results

    def _parse_legislator_list(self, text: str) -> list[str]:
        """
        Parse a comma-separated list of legislators.

        Format: "Banks (R-IN), Moody (R-FL), Scott (R-FL), ..."

        Returns:
            List of legislator keys like "Banks|R|IN"
        """
        legislators = []

        # Pattern: Name (Party-State)
        pattern = r'([A-Za-z\'\-\.\s]+?)\s*\(([RDI])-([A-Z]{2})\)'

        for match in re.finditer(pattern, text):
            name = match.group(1).strip()
            party = match.group(2)
            state = match.group(3)

            # Create a unique key
            leg_key = f"{name}|{party}|{state}"
            legislators.append(leg_key)

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
            legislator_votes: Dict of legislator records

        Returns:
            Stats about created documents
        """
        created = 0
        failed = 0
        total_chunks = 0
        errors = []

        for leg_key, record in legislator_votes.items():
            try:
                # Format the document content
                content = self._format_legislator_votes_document(record)

                # Create document ID
                # Use name and state to create a somewhat stable ID
                safe_name = re.sub(r'[^a-zA-Z0-9]', '-', record.name.lower())
                doc_id = f"legislator-votes-{safe_name}-{record.state.lower()}"

                # Create metadata
                party_full = {
                    "R": "Republican",
                    "D": "Democratic",
                    "I": "Independent",
                }.get(record.party, record.party)

                metadata = DocumentMetadata(
                    document_id=doc_id,
                    document_type="legislator-votes",
                    source="Digital Democracy Project",
                    title=f"{record.name} ({party_full}-{record.state}) - Voting Record",
                    jurisdiction=record.state if record.state != "US" else "US",
                    extra={
                        "legislator_name": record.name,
                        "party": party_full,
                        "state": record.state,
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
        )

        return {
            "success": failed == 0 or created > 0,
            "legislators_processed": created + failed,
            "documents_created": created,
            "documents_failed": failed,
            "chunks_created": total_chunks,
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
        }.get(record.party, record.party)

        parts = []
        parts.append(f"# {record.name} - Voting Record")
        parts.append(f"**Party:** {party_full}")
        parts.append(f"**State:** {record.state}")
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

    args = parser.parse_args()

    builder = LegislatorVotesBuilder()
    results = await builder.build_all(dry_run=args.dry_run)

    print("\n=== Build Results ===")
    for key, value in results.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
