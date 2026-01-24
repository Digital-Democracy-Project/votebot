#!/usr/bin/env python3
"""Manual ingestion script for VoteBot."""

import argparse
import asyncio
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from votebot.config import get_settings

# Clear settings cache to ensure fresh env vars are loaded
get_settings.cache_clear()

from votebot.ingestion.pipeline import IngestionPipeline
from votebot.utils.logging import setup_logging


async def ingest_congress(pipeline: IngestionPipeline, args: argparse.Namespace) -> None:
    """Ingest from Congress.gov."""
    print(f"Ingesting from Congress.gov (Congress {args.congress}, limit {args.limit})...")

    result = await pipeline.ingest_from_source(
        "congress",
        {
            "congress": args.congress,
            "bill_type": args.bill_type,
            "limit": args.limit,
        },
    )

    print(f"  Documents processed: {result.documents_processed}")
    print(f"  Chunks created: {result.chunks_created}")
    print(f"  Chunks upserted: {result.chunks_upserted}")
    if result.errors:
        print(f"  Errors: {len(result.errors)}")
        for error in result.errors[:5]:
            print(f"    - {error}")


async def ingest_openstates(pipeline: IngestionPipeline, args: argparse.Namespace) -> None:
    """Ingest from OpenStates."""
    print(f"Ingesting from OpenStates (jurisdiction: {args.jurisdiction}, limit {args.limit})...")

    result = await pipeline.ingest_from_source(
        "openstates",
        {
            "jurisdiction": args.jurisdiction,
            "limit": args.limit,
        },
    )

    print(f"  Documents processed: {result.documents_processed}")
    print(f"  Chunks created: {result.chunks_created}")
    print(f"  Chunks upserted: {result.chunks_upserted}")
    if result.errors:
        print(f"  Errors: {len(result.errors)}")


async def ingest_pdf(pipeline: IngestionPipeline, args: argparse.Namespace) -> None:
    """Ingest PDF files."""
    print(f"Ingesting PDFs from {args.path}...")

    path = Path(args.path)
    if path.is_file():
        config = {"files": [str(path)]}
    else:
        config = {"directory": str(path), "recursive": args.recursive}

    result = await pipeline.ingest_from_source("pdf", config)

    print(f"  Documents processed: {result.documents_processed}")
    print(f"  Chunks created: {result.chunks_created}")
    print(f"  Chunks upserted: {result.chunks_upserted}")
    if result.errors:
        print(f"  Errors: {len(result.errors)}")


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="VoteBot Manual Ingestion Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="source", help="Data source to ingest from")

    # Congress.gov subcommand
    congress_parser = subparsers.add_parser("congress", help="Ingest from Congress.gov")
    congress_parser.add_argument(
        "--congress", type=int, default=118, help="Congress number (default: 118)"
    )
    congress_parser.add_argument(
        "--bill-type", type=str, help="Bill type (hr, s, hjres, sjres)"
    )
    congress_parser.add_argument(
        "--limit", type=int, default=50, help="Maximum bills to fetch"
    )

    # OpenStates subcommand
    openstates_parser = subparsers.add_parser("openstates", help="Ingest from OpenStates")
    openstates_parser.add_argument(
        "--jurisdiction", type=str, help="State abbreviation (e.g., ca, ny)"
    )
    openstates_parser.add_argument(
        "--limit", type=int, default=50, help="Maximum bills to fetch"
    )

    # PDF subcommand
    pdf_parser = subparsers.add_parser("pdf", help="Ingest PDF files")
    pdf_parser.add_argument("path", help="Path to PDF file or directory")
    pdf_parser.add_argument(
        "--recursive", action="store_true", help="Search directories recursively"
    )

    # Common arguments
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )

    args = parser.parse_args()

    if not args.source:
        parser.print_help()
        sys.exit(1)

    # Setup logging
    setup_logging(args.log_level)

    # Initialize pipeline
    settings = get_settings()
    pipeline = IngestionPipeline(settings)

    # Run appropriate ingestion
    try:
        if args.source == "congress":
            await ingest_congress(pipeline, args)
        elif args.source == "openstates":
            await ingest_openstates(pipeline, args)
        elif args.source == "pdf":
            await ingest_pdf(pipeline, args)
        else:
            print(f"Unknown source: {args.source}")
            sys.exit(1)

        print("\nIngestion completed successfully!")

    except Exception as e:
        print(f"\nIngestion failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
