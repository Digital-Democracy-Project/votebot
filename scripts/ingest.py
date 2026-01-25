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
from votebot.ingestion.sources.webflow import WebflowSource
from votebot.utils.logging import setup_logging

# Default paths
RAG_TRAINING_DOCS_DIR = Path(__file__).parent.parent / "RAG training docs"
WEBSITE_PAGES_FILE = RAG_TRAINING_DOCS_DIR / "website_pages.txt"


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


async def ingest_webflow(pipeline: IngestionPipeline, args: argparse.Namespace) -> None:
    """Ingest from Webflow CMS."""
    print(f"Ingesting from Webflow CMS (limit {args.limit}, include_pdfs={args.include_pdfs})...")

    result = await pipeline.ingest_from_source(
        "webflow",
        {
            "limit": args.limit,
            "include_pdfs": args.include_pdfs,
        },
    )

    print(f"  Documents processed: {result.documents_processed}")
    print(f"  Chunks created: {result.chunks_created}")
    print(f"  Chunks upserted: {result.chunks_upserted}")
    if result.errors:
        print(f"  Errors: {len(result.errors)}")
        for error in result.errors[:5]:
            print(f"    - {error}")


async def ingest_website(pipeline: IngestionPipeline, args: argparse.Namespace) -> None:
    """Ingest website pages from the website_pages.txt file."""
    pages_file = Path(args.file) if args.file else WEBSITE_PAGES_FILE

    if not pages_file.exists():
        print(f"Error: Website pages file not found: {pages_file}")
        sys.exit(1)

    # Read URLs from file
    urls = []
    with open(pages_file, "r") as f:
        for line in f:
            line = line.strip()
            # Skip empty lines and comments
            if line and not line.startswith("#"):
                urls.append(line)

    if not urls:
        print("No URLs found in website pages file.")
        return

    print(f"Ingesting {len(urls)} website pages from {pages_file}...")

    result = await pipeline.ingest_from_source(
        "website",
        {"urls": urls},
    )

    print(f"  Pages processed: {result.documents_processed}")
    print(f"  Chunks created: {result.chunks_created}")
    print(f"  Chunks upserted: {result.chunks_upserted}")
    if result.errors:
        print(f"  Errors: {len(result.errors)}")
        for error in result.errors[:5]:
            print(f"    - {error}")


async def ingest_training_docs(pipeline: IngestionPipeline, args: argparse.Namespace) -> None:
    """Ingest RAG training documents from the training docs folder."""
    docs_dir = Path(args.path) if args.path else RAG_TRAINING_DOCS_DIR

    if not docs_dir.exists():
        print(f"Error: Training docs directory not found: {docs_dir}")
        sys.exit(1)

    # Find all text files (excluding website_pages.txt which is a config file)
    doc_files = []
    for ext in ["*.txt", "*.md"]:
        for f in docs_dir.glob(ext):
            if f.name != "website_pages.txt":
                doc_files.append(f)

    if not doc_files:
        print("No training documents found.")
        return

    print(f"Ingesting {len(doc_files)} training documents from {docs_dir}...")

    result = await pipeline.ingest_from_source(
        "training_docs",
        {"files": [str(f) for f in doc_files]},
    )

    print(f"  Documents processed: {result.documents_processed}")
    print(f"  Chunks created: {result.chunks_created}")
    print(f"  Chunks upserted: {result.chunks_upserted}")
    if result.errors:
        print(f"  Errors: {len(result.errors)}")
        for error in result.errors[:5]:
            print(f"    - {error}")


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

    # Webflow CMS subcommand
    webflow_parser = subparsers.add_parser("webflow", help="Ingest from Webflow CMS")
    webflow_parser.add_argument(
        "--limit", type=int, default=0, help="Maximum items to fetch (default: 0 = unlimited)"
    )
    webflow_parser.add_argument(
        "--no-pdfs", dest="include_pdfs", action="store_false",
        help="Skip downloading PDFs from gov-url"
    )

    # Website pages subcommand
    website_parser = subparsers.add_parser("website", help="Ingest website pages")
    website_parser.add_argument(
        "--file", type=str, help=f"Path to URL list file (default: {WEBSITE_PAGES_FILE})"
    )

    # Training docs subcommand
    training_parser = subparsers.add_parser("training", help="Ingest RAG training documents")
    training_parser.add_argument(
        "--path", type=str, help=f"Path to training docs folder (default: {RAG_TRAINING_DOCS_DIR})"
    )

    # All sources subcommand
    all_parser = subparsers.add_parser("all", help="Ingest from all configured sources")
    all_parser.add_argument(
        "--webflow-limit", type=int, default=0, help="Max Webflow items (default: 0 = unlimited)"
    )
    all_parser.add_argument(
        "--no-pdfs", dest="include_pdfs", action="store_false",
        help="Skip downloading PDFs from gov-url"
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
        elif args.source == "webflow":
            await ingest_webflow(pipeline, args)
        elif args.source == "website":
            await ingest_website(pipeline, args)
        elif args.source == "training":
            await ingest_training_docs(pipeline, args)
        elif args.source == "all":
            print("=" * 60)
            print("INGESTING ALL SOURCES")
            print("=" * 60)

            # Create namespace objects for each source
            print("\n[1/3] Training Documents")
            training_args = argparse.Namespace(path=None)
            await ingest_training_docs(pipeline, training_args)

            print("\n[2/3] Website Pages")
            website_args = argparse.Namespace(file=None)
            await ingest_website(pipeline, website_args)

            print("\n[3/3] Webflow CMS")
            webflow_args = argparse.Namespace(
                limit=args.webflow_limit,
                include_pdfs=args.include_pdfs,
            )
            await ingest_webflow(pipeline, webflow_args)

            print("\n" + "=" * 60)
        else:
            print(f"Unknown source: {args.source}")
            sys.exit(1)

        print("\nIngestion completed successfully!")

    except Exception as e:
        print(f"\nIngestion failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
