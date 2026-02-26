#!/usr/bin/env python3
"""
Shared infrastructure for RAG test suite.

Provides unified TestResult, TestReport, VoteBotTestClient, validation,
ground truth fetching, and reporting used by all focused test modules.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv


@dataclass
class TestResult:
    """Unified result of a single test case across all test types."""

    # Core identification
    test_id: str = ""
    category: str = ""  # bills, legislators, organizations, ddp, out_of_system_votes
    entity_type: str = ""  # bill, legislator, organization, ddp, out_of_system_vote
    entity_name: str = ""
    entity_slug: str = ""

    # Question and response
    prompt: str = ""
    response_text: str = ""
    response_preview: str = ""  # First 500 chars

    # Metrics
    confidence: float = 0.0
    has_citations: bool = False
    citation_count: int = 0
    latency: float = 0.0

    # Ground truth validation
    passed: bool | None = None  # None = not validated
    expected_data: list[str] = field(default_factory=list)
    found_data: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    validation_mode: str = ""  # contains, contains_any, keywords
    data_source: str = ""  # webflow_cms, openstates, static, none

    # Multi-turn support
    turn_index: int = 0
    session_id: str = ""

    # Context
    jurisdiction: str = ""
    success: bool = True
    error: str | None = None
    mode: str = "single"  # single or multi


@dataclass
class TestReport:
    """Aggregate test report with breakdowns."""

    timestamp: str = ""
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    not_validated: int = 0
    errors: int = 0
    pass_rate: float = 0.0

    # Breakdowns
    results_by_category: dict[str, dict] = field(default_factory=dict)
    results_by_entity_type: dict[str, dict] = field(default_factory=dict)
    results_by_jurisdiction: dict[str, dict] = field(default_factory=dict)
    results_by_mode: dict[str, dict] = field(default_factory=dict)

    # Aggregate metrics
    avg_confidence: float = 0.0
    avg_latency: float = 0.0
    p95_latency: float = 0.0
    citation_rate: float = 0.0

    all_results: list[TestResult] = field(default_factory=list)


class VoteBotTestClient:
    """Thin httpx wrapper for sending messages to VoteBot API."""

    def __init__(self, api_url: str = "http://localhost:8000", api_key: str | None = None):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key or self._load_api_key()

    def _load_api_key(self) -> str:
        load_dotenv()
        return os.environ.get("VOTEBOT_API_KEY") or os.environ.get("API_KEY", "test-key")

    async def send_message(
        self,
        message: str,
        session_id: str | None = None,
        page_context: dict | None = None,
    ) -> dict:
        """Send a message and return structured response.

        Returns dict with keys:
            response, confidence, citations, citation_count, latency, success, error
        """
        session_id = session_id or f"rag-test-{uuid.uuid4().hex[:8]}"
        page_context = page_context or {"type": "general"}

        start_time = time.time()
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self.api_url}/votebot/v1/chat",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "message": message,
                        "session_id": session_id,
                        "human_active": False,
                        "page_context": page_context,
                    },
                )

                latency = time.time() - start_time

                if resp.status_code != 200:
                    return {
                        "response": "",
                        "confidence": 0.0,
                        "citations": [],
                        "citation_count": 0,
                        "latency": latency,
                        "success": False,
                        "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                    }

                data = resp.json()
                citations = data.get("citations", [])
                return {
                    "response": data.get("response", ""),
                    "confidence": data.get("confidence", 0.0),
                    "citations": citations,
                    "citation_count": len(citations),
                    "latency": latency,
                    "success": True,
                    "error": None,
                }

        except Exception as e:
            return {
                "response": "",
                "confidence": 0.0,
                "citations": [],
                "citation_count": 0,
                "latency": time.time() - start_time,
                "success": False,
                "error": str(e),
            }


def validate_response(
    response_text: str,
    expected_data: list[str],
    validation: str = "contains",
    min_matches: int = 1,
) -> tuple[bool, list[str], list[str]]:
    """Validate response against expected data.

    Args:
        response_text: The response text (will be lowercased).
        expected_data: List of expected strings.
        validation: Mode - "contains", "contains_any", or "keywords".
        min_matches: Minimum matches for contains_any/keywords modes.

    Returns:
        (passed, found_data, missing_data) tuple.
    """
    response_lower = response_text.lower()
    found_data = []
    missing_data = []

    for expected in expected_data:
        if expected.lower() in response_lower:
            found_data.append(expected)
        else:
            missing_data.append(expected)

    if validation == "contains":
        passed = len(missing_data) == 0
    elif validation in ("contains_any", "keywords"):
        passed = len(found_data) >= min_matches
    else:
        passed = len(missing_data) == 0

    return passed, found_data, missing_data


async def fetch_ground_truth(
    limit: int = 0,
    jurisdiction: str | None = None,
    entity_types: list[str] | None = None,
    with_openstates: bool = False,
) -> tuple[list, list, list]:
    """Fetch ground truth data from Webflow CMS.

    Returns:
        (bills_gt, legislators_gt, organizations_gt) tuple.
    """
    # Import here to avoid circular deps and keep rag_ground_truth optional
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))

    from rag_ground_truth import GroundTruthFetcher

    load_dotenv()

    fetcher = GroundTruthFetcher(
        webflow_api_key=os.environ["WEBFLOW_VOTEBOT_API_KEY"],
        bills_collection_id=os.environ["WEBFLOW_BILLS_COLLECTION_ID"],
        legislators_collection_id=os.environ["WEBFLOW_LEGISLATORS_COLLECTION_ID"],
        organizations_collection_id=os.environ["WEBFLOW_ORGANIZATIONS_COLLECTION_ID"],
        jurisdiction_collection_id=os.environ["WEBFLOW_JURISDICTION_COLLECTION_ID"],
        openstates_api_key=os.environ.get("OPENSTATES_API_KEY") if with_openstates else None,
    )

    entity_types = entity_types or ["bills", "legislators", "organizations"]

    bills = []
    legislators = []
    organizations = []

    if "bills" in entity_types:
        print("Fetching bills from Webflow CMS...")
        bills = await fetcher.fetch_all_bills(limit=limit, jurisdiction=jurisdiction)
        print(f"  Fetched {len(bills)} bills")

    if "legislators" in entity_types:
        print("Fetching legislators from Webflow CMS...")
        legislators = await fetcher.fetch_all_legislators(limit=limit, jurisdiction=jurisdiction)
        print(f"  Fetched {len(legislators)} legislators")

    if "organizations" in entity_types:
        print("Fetching organizations from Webflow CMS...")
        organizations = await fetcher.fetch_all_organizations(limit=limit)
        print(f"  Fetched {len(organizations)} organizations")

    if with_openstates and (bills or legislators):
        print("Enriching with OpenStates data...")
        await fetcher.enrich_with_openstates(bills, legislators)

    return bills, legislators, organizations


def generate_report(results: list[TestResult]) -> TestReport:
    """Generate aggregate report from test results."""
    if not results:
        return TestReport(timestamp=datetime.now().isoformat())

    # Count validated results
    validated = [r for r in results if r.passed is not None]
    passed = sum(1 for r in validated if r.passed)
    failed = sum(1 for r in validated if not r.passed)
    not_validated = sum(1 for r in results if r.passed is None)
    errors = sum(1 for r in results if r.error)
    pass_rate = passed / len(validated) * 100 if validated else 0.0

    # Breakdowns helper
    def _breakdown(results_list: list[TestResult], key_fn) -> dict[str, dict]:
        breakdown: dict[str, dict] = {}
        for r in results_list:
            key = key_fn(r) or "unknown"
            if key not in breakdown:
                breakdown[key] = {"total": 0, "passed": 0, "failed": 0, "not_validated": 0, "errors": 0}
            breakdown[key]["total"] += 1
            if r.error:
                breakdown[key]["errors"] += 1
            if r.passed is None:
                breakdown[key]["not_validated"] += 1
            elif r.passed:
                breakdown[key]["passed"] += 1
            else:
                breakdown[key]["failed"] += 1
        return breakdown

    by_category = _breakdown(results, lambda r: r.category)
    by_entity = _breakdown(results, lambda r: r.entity_type)
    by_jurisdiction = _breakdown(results, lambda r: r.jurisdiction)
    by_mode = _breakdown(results, lambda r: r.mode)

    # Aggregate metrics
    successful = [r for r in results if r.success]
    confidences = [r.confidence for r in successful]
    latencies = [r.latency for r in successful if r.latency > 0]

    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    p95_latency = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0.0
    citation_rate = sum(1 for r in successful if r.has_citations) / len(successful) * 100 if successful else 0.0

    return TestReport(
        timestamp=datetime.now().isoformat(),
        total_tests=len(results),
        passed=passed,
        failed=failed,
        not_validated=not_validated,
        errors=errors,
        pass_rate=pass_rate,
        results_by_category=by_category,
        results_by_entity_type=by_entity,
        results_by_jurisdiction=by_jurisdiction,
        results_by_mode=by_mode,
        avg_confidence=avg_confidence,
        avg_latency=avg_latency,
        p95_latency=p95_latency,
        citation_rate=citation_rate,
        all_results=results,
    )


def print_report(report: TestReport, verbose: bool = False) -> None:
    """Print test report to console."""
    print("\n" + "=" * 70)
    print("RAG TEST REPORT")
    print("=" * 70)
    print(f"  Timestamp: {report.timestamp}")
    print(f"  Total Tests: {report.total_tests}")
    print(f"  Passed: {report.passed}")
    print(f"  Failed: {report.failed}")
    print(f"  Not Validated: {report.not_validated}")
    print(f"  Errors: {report.errors}")
    print(f"  Pass Rate: {report.pass_rate:.1f}%")

    print(f"\n--- Aggregate Metrics ---")
    print(f"  Avg Confidence: {report.avg_confidence:.2f}")
    print(f"  Avg Latency: {report.avg_latency:.2f}s")
    print(f"  P95 Latency: {report.p95_latency:.2f}s")
    print(f"  Citation Rate: {report.citation_rate:.1f}%")

    print(f"\n--- Results by Category ---")
    for cat, stats in sorted(report.results_by_category.items()):
        validated_total = stats["passed"] + stats["failed"]
        rate = stats["passed"] / validated_total * 100 if validated_total else 0
        print(f"  {cat}: {stats['passed']}/{validated_total} validated pass "
              f"({rate:.0f}%), {stats['not_validated']} unvalidated, "
              f"{stats['errors']} errors [total: {stats['total']}]")

    print(f"\n--- Results by Entity Type ---")
    for entity, stats in sorted(report.results_by_entity_type.items()):
        validated_total = stats["passed"] + stats["failed"]
        rate = stats["passed"] / validated_total * 100 if validated_total else 0
        print(f"  {entity}: {stats['passed']}/{validated_total} ({rate:.0f}%)")

    if report.results_by_jurisdiction:
        print(f"\n--- Results by Jurisdiction ---")
        for j, stats in sorted(report.results_by_jurisdiction.items()):
            if j and j != "unknown":
                validated_total = stats["passed"] + stats["failed"]
                rate = stats["passed"] / validated_total * 100 if validated_total else 0
                print(f"  {j}: {stats['total']} tests, {stats['passed']}/{validated_total} pass ({rate:.0f}%)")

    if report.results_by_mode:
        print(f"\n--- Results by Mode ---")
        for mode, stats in sorted(report.results_by_mode.items()):
            print(f"  {mode}: {stats['total']} tests")

    # Show failed tests
    if verbose:
        failed_tests = [r for r in report.all_results if r.passed is False]
        if failed_tests:
            print(f"\n--- Failed Tests (first 15) ---")
            for r in failed_tests[:15]:
                print(f"  [{r.test_id}] {r.prompt[:60]}...")
                if r.missing_data:
                    print(f"       Missing: {r.missing_data[:5]}")
                if r.error:
                    print(f"       Error: {r.error[:100]}")
            if len(failed_tests) > 15:
                print(f"  ... and {len(failed_tests) - 15} more")

    print("=" * 70)


def save_report(report: TestReport, output_path: str) -> None:
    """Save report to JSON file."""
    # Convert to dict, handling TestResult objects
    report_dict = {
        "timestamp": report.timestamp,
        "total_tests": report.total_tests,
        "passed": report.passed,
        "failed": report.failed,
        "not_validated": report.not_validated,
        "errors": report.errors,
        "pass_rate": report.pass_rate,
        "avg_confidence": report.avg_confidence,
        "avg_latency": report.avg_latency,
        "p95_latency": report.p95_latency,
        "citation_rate": report.citation_rate,
        "results_by_category": report.results_by_category,
        "results_by_entity_type": report.results_by_entity_type,
        "results_by_jurisdiction": report.results_by_jurisdiction,
        "results_by_mode": report.results_by_mode,
        "all_results": [asdict(r) for r in report.all_results],
    }
    with open(output_path, "w") as f:
        json.dump(report_dict, f, indent=2, default=str)
    print(f"\nReport saved to: {output_path}")
