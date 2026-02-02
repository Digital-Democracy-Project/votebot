#!/usr/bin/env python3
"""
RAG Quality Test Runner for VoteBot.

Runs a suite of test prompts against VoteBot and validates that expected
data is present in responses. Supports both:
1. Static tests from YAML files (hand-crafted edge cases, DDP info)
2. Dynamic tests generated from Webflow CMS ground truth

Usage:
    # Run static tests only
    python scripts/test_rag_quality.py --static

    # Run dynamic tests for all entity types
    python scripts/test_rag_quality.py --dynamic

    # Run dynamic tests for specific entity type
    python scripts/test_rag_quality.py --dynamic --entity-type bills --limit 50

    # Run both static and dynamic tests
    python scripts/test_rag_quality.py --all

    # Filter by jurisdiction
    python scripts/test_rag_quality.py --dynamic --jurisdiction FL

    # Include OpenStates enrichment
    python scripts/test_rag_quality.py --dynamic --with-openstates

Options:
    --api-url URL           VoteBot API URL (default: http://localhost:8000)
    --static                Run static YAML tests only
    --dynamic               Run dynamic tests from Webflow ground truth
    --all                   Run both static and dynamic tests
    --entity-type TYPE      Filter dynamic tests: bills, legislators, organizations
    --jurisdiction CODE     Filter by state (e.g., FL, VA, WA)
    --limit N               Limit dynamic tests per entity type
    --with-openstates       Enrich ground truth with OpenStates data
    --category CAT          Run only tests in this category
    --output FILE           Write results to JSON file
    --verbose               Show detailed output
    --dry-run               Show test cases without running
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

# Add src and scripts to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from rag_ground_truth import (
    GroundTruthFetcher,
    BillGroundTruth,
    LegislatorGroundTruth,
    OrganizationGroundTruth,
)


@dataclass
class TestResult:
    """Result of a single test case."""

    test_id: str
    prompt: str
    category: str
    data_source: str
    passed: bool
    expected_data: list[str]
    found_data: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    response_preview: str = ""
    error: str | None = None
    confidence: float = 0.0
    sources: list[str] = field(default_factory=list)
    entity_type: str = ""
    entity_slug: str = ""


@dataclass
class TestReport:
    """Aggregate test report."""

    timestamp: str
    total_tests: int
    passed: int
    failed: int
    errors: int
    pass_rate: float
    results_by_category: dict[str, dict] = field(default_factory=dict)
    results_by_source: dict[str, dict] = field(default_factory=dict)
    results_by_entity_type: dict[str, dict] = field(default_factory=dict)
    all_results: list[TestResult] = field(default_factory=list)


class DynamicTestGenerator:
    """Generates test cases from Webflow CMS ground truth."""

    def __init__(self, templates_path: str | None = None):
        """Initialize with optional templates file."""
        self.templates_path = templates_path
        self.templates = self._load_templates()

    def _load_templates(self) -> dict:
        """Load test templates from YAML."""
        if self.templates_path and Path(self.templates_path).exists():
            with open(self.templates_path, "r") as f:
                return yaml.safe_load(f) or {}
        return self._default_templates()

    def _default_templates(self) -> dict:
        """Default test templates if no file provided."""
        return {
            "bill_templates": [
                {
                    "id": "{slug}_summary",
                    "prompt": "What is {jurisdiction_name} {bill_id} about?",
                    "category": "bill_summary",
                    "validation": "keywords",
                    "ground_truth_field": "description_keywords",
                    "min_matches": 2,
                },
                {
                    "id": "{slug}_title",
                    "prompt": "What is the title of {jurisdiction_name} {bill_id}?",
                    "category": "bill_title",
                    "validation": "contains",
                    "ground_truth_field": "name",
                },
                {
                    "id": "{slug}_support_orgs",
                    "prompt": "Which organizations support {jurisdiction_name} {bill_id}?",
                    "category": "org_positions",
                    "condition": "has_support_orgs",
                    "validation": "contains_any",
                    "ground_truth_field": "support_org_names",
                    "min_matches": 1,
                },
                {
                    "id": "{slug}_oppose_orgs",
                    "prompt": "Which organizations oppose {jurisdiction_name} {bill_id}?",
                    "category": "org_positions",
                    "condition": "has_oppose_orgs",
                    "validation": "contains_any",
                    "ground_truth_field": "oppose_org_names",
                    "min_matches": 1,
                },
            ],
            "legislator_templates": [
                {
                    "id": "{slug}_profile",
                    "prompt": "Tell me about {jurisdiction_name} {chamber_title} {name}",
                    "category": "legislator_profile",
                    "validation": "contains",
                    "ground_truth_field": "name",
                },
                {
                    "id": "{slug}_district",
                    "prompt": "What district does {name} represent?",
                    "category": "legislator_district",
                    "validation": "contains",
                    "ground_truth_field": "district",
                },
                {
                    "id": "{slug}_party",
                    "prompt": "What party is {name} affiliated with?",
                    "category": "legislator_party",
                    "validation": "contains",
                    "ground_truth_field": "party",
                },
            ],
            "organization_templates": [
                {
                    "id": "{slug}_profile",
                    "prompt": "Tell me about {name}",
                    "category": "org_profile",
                    "validation": "contains",
                    "ground_truth_field": "name",
                },
                {
                    "id": "{slug}_type",
                    "prompt": "What type of organization is {name}?",
                    "category": "org_profile",
                    "condition": "has_type",
                    "validation": "contains",
                    "ground_truth_field": "org_type",
                },
                {
                    "id": "{slug}_supported_bills",
                    "prompt": "What bills does {name} support?",
                    "category": "org_positions",
                    "condition": "has_supported_bills",
                    "validation": "contains_any",
                    "ground_truth_field": "bills_support_names",
                    "min_matches": 1,
                },
            ],
        }

    def generate_bill_tests(self, bills: list[BillGroundTruth]) -> list[dict]:
        """Generate test cases for bills."""
        test_cases = []
        templates = self.templates.get("bill_templates", [])

        for bill in bills:
            for template in templates:
                # Check condition
                condition = template.get("condition")
                if condition and not getattr(bill, condition, False):
                    continue

                # Build test case
                test_id = template["id"].format(slug=bill.slug)
                prompt = template["prompt"].format(
                    jurisdiction=bill.jurisdiction,
                    jurisdiction_name=bill.jurisdiction_name,
                    bill_id=bill.bill_id,
                    name=bill.name,
                    slug=bill.slug,
                )

                # Get ground truth data
                ground_truth_field = template.get("ground_truth_field", "")
                expected_data = self._get_ground_truth_value(bill, ground_truth_field)

                test_cases.append({
                    "id": test_id,
                    "prompt": prompt,
                    "category": template.get("category", "bill"),
                    "data_source": "webflow_cms",
                    "expected_data": expected_data,
                    "validation": template.get("validation", "contains"),
                    "min_matches": template.get("min_matches", 1),
                    "entity_type": "bill",
                    "entity_slug": bill.slug,
                })

        return test_cases

    def generate_legislator_tests(self, legislators: list[LegislatorGroundTruth]) -> list[dict]:
        """Generate test cases for legislators."""
        test_cases = []
        templates = self.templates.get("legislator_templates", [])

        for legislator in legislators:
            for template in templates:
                condition = template.get("condition")
                if condition and not getattr(legislator, condition, False):
                    continue

                test_id = template["id"].format(slug=legislator.slug)
                prompt = template["prompt"].format(
                    jurisdiction=legislator.jurisdiction,
                    jurisdiction_name=legislator.jurisdiction_name,
                    name=legislator.name,
                    chamber=legislator.chamber,
                    chamber_title=legislator.chamber_title,
                    district=legislator.district,
                    party=legislator.party,
                )

                ground_truth_field = template.get("ground_truth_field", "")
                expected_data = self._get_ground_truth_value(legislator, ground_truth_field)

                test_cases.append({
                    "id": test_id,
                    "prompt": prompt,
                    "category": template.get("category", "legislator"),
                    "data_source": "webflow_cms",
                    "expected_data": expected_data,
                    "validation": template.get("validation", "contains"),
                    "min_matches": template.get("min_matches", 1),
                    "entity_type": "legislator",
                    "entity_slug": legislator.slug,
                })

        return test_cases

    def generate_organization_tests(self, organizations: list[OrganizationGroundTruth]) -> list[dict]:
        """Generate test cases for organizations."""
        test_cases = []
        templates = self.templates.get("organization_templates", [])

        for org in organizations:
            for template in templates:
                condition = template.get("condition")
                if condition and not getattr(org, condition, False):
                    continue

                test_id = template["id"].format(slug=org.slug)
                prompt = template["prompt"].format(
                    name=org.name,
                    org_type=org.org_type,
                )

                ground_truth_field = template.get("ground_truth_field", "")
                expected_data = self._get_ground_truth_value(org, ground_truth_field)

                test_cases.append({
                    "id": test_id,
                    "prompt": prompt,
                    "category": template.get("category", "organization"),
                    "data_source": "webflow_cms",
                    "expected_data": expected_data,
                    "validation": template.get("validation", "contains"),
                    "min_matches": template.get("min_matches", 1),
                    "entity_type": "organization",
                    "entity_slug": org.slug,
                })

        return test_cases

    def _get_ground_truth_value(self, entity: Any, field_name: str) -> list[str]:
        """Extract ground truth value from entity."""
        if not field_name:
            return []

        # Handle method calls (e.g., description_keywords)
        if field_name.endswith("_keywords"):
            method = getattr(entity, field_name, None)
            if callable(method):
                return method()

        value = getattr(entity, field_name, None)
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value if v]
        return [str(value)] if value else []


class RAGQualityTester:
    """Test runner for RAG quality validation."""

    def __init__(
        self,
        api_url: str = "http://localhost:8000",
        api_key: str | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key or self._load_api_key()
        self.results: list[TestResult] = []

    def _load_api_key(self) -> str:
        """Load API key from environment or .env file."""
        load_dotenv()
        return os.environ.get("VOTEBOT_API_KEY") or os.environ.get("API_KEY", "test-key")

    def load_test_cases(self, yaml_path: str) -> list[dict]:
        """Load test cases from YAML file."""
        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)

        test_cases = []
        for category_name, tests in data.items():
            if isinstance(tests, list):
                for test in tests:
                    test["category_group"] = category_name
                    test["entity_type"] = "static"
                    test["entity_slug"] = ""
                    test_cases.append(test)

        return test_cases

    async def run_single_test(
        self,
        test_case: dict,
        verbose: bool = False,
    ) -> TestResult:
        """Run a single test case against the API."""
        test_id = test_case.get("id", "unknown")
        prompt = test_case.get("prompt", "")
        expected_data = test_case.get("expected_data", [])
        category = test_case.get("category", "unknown")
        data_source = test_case.get("data_source", "unknown")
        validation = test_case.get("validation", "contains")
        min_matches = test_case.get("min_matches", 1)

        result = TestResult(
            test_id=test_id,
            prompt=prompt,
            category=category,
            data_source=data_source,
            passed=False,
            expected_data=expected_data,
            entity_type=test_case.get("entity_type", ""),
            entity_slug=test_case.get("entity_slug", ""),
        )

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                session_id = f"rag-test-{uuid.uuid4().hex[:8]}"

                response = await client.post(
                    f"{self.api_url}/votebot/v1/chat",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "message": prompt,
                        "session_id": session_id,
                        "page_context": {"type": "general"},
                    },
                )

                if response.status_code != 200:
                    result.error = f"API error: {response.status_code} - {response.text[:200]}"
                    return result

                data = response.json()
                response_text = data.get("response", "").lower()
                result.response_preview = response_text[:500]
                result.confidence = data.get("confidence", 0.0)
                result.sources = [s.get("source", "") for s in data.get("sources", [])]

                # Validate based on validation type
                result.passed = self._validate_response(
                    response_text,
                    expected_data,
                    validation,
                    min_matches,
                    result,
                )

        except Exception as e:
            result.error = str(e)

        if verbose:
            status = "PASS" if result.passed else "FAIL"
            print(f"  [{status}] {test_id}: {prompt[:50]}...")
            if result.missing_data:
                print(f"       Missing: {result.missing_data}")
            if result.error:
                print(f"       Error: {result.error}")

        return result

    def _validate_response(
        self,
        response_text: str,
        expected_data: list[str],
        validation: str,
        min_matches: int,
        result: TestResult,
    ) -> bool:
        """Validate response against expected data."""
        if validation == "contains":
            # All expected items must be in response
            for expected in expected_data:
                expected_lower = expected.lower()
                if expected_lower in response_text:
                    result.found_data.append(expected)
                else:
                    result.missing_data.append(expected)
            return len(result.missing_data) == 0

        elif validation == "contains_any":
            # At least min_matches items must be in response
            for expected in expected_data:
                expected_lower = expected.lower()
                if expected_lower in response_text:
                    result.found_data.append(expected)
                else:
                    result.missing_data.append(expected)
            return len(result.found_data) >= min_matches

        elif validation == "keywords":
            # At least min_matches keywords must be in response
            for expected in expected_data:
                expected_lower = expected.lower()
                if expected_lower in response_text:
                    result.found_data.append(expected)
                else:
                    result.missing_data.append(expected)
            return len(result.found_data) >= min_matches

        else:
            # Default: all must match
            for expected in expected_data:
                if expected.lower() in response_text:
                    result.found_data.append(expected)
                else:
                    result.missing_data.append(expected)
            return len(result.missing_data) == 0

    async def run_all_tests(
        self,
        test_cases: list[dict],
        category_filter: str | None = None,
        verbose: bool = False,
    ) -> TestReport:
        """Run all test cases and generate report."""
        if category_filter:
            test_cases = [
                t for t in test_cases
                if t.get("category") == category_filter
                or t.get("category_group") == category_filter
            ]

        print(f"\nRunning {len(test_cases)} RAG quality tests...")
        print("=" * 60)

        results = []
        for i, test_case in enumerate(test_cases):
            result = await self.run_single_test(test_case, verbose)
            results.append(result)

            # Progress indicator every 10 tests
            if (i + 1) % 10 == 0:
                print(f"  Progress: {i + 1}/{len(test_cases)} tests completed")

            # Rate limiting
            await asyncio.sleep(0.5)

        report = self._generate_report(results)
        self._print_summary(report)
        return report

    def _generate_report(self, results: list[TestResult]) -> TestReport:
        """Generate aggregate report from results."""
        passed = sum(1 for r in results if r.passed)
        failed = sum(1 for r in results if not r.passed and not r.error)
        errors = sum(1 for r in results if r.error)

        # Group by category
        by_category: dict[str, dict] = {}
        for r in results:
            if r.category not in by_category:
                by_category[r.category] = {"total": 0, "passed": 0, "failed": 0}
            by_category[r.category]["total"] += 1
            if r.passed:
                by_category[r.category]["passed"] += 1
            else:
                by_category[r.category]["failed"] += 1

        # Group by data source
        by_source: dict[str, dict] = {}
        for r in results:
            if r.data_source not in by_source:
                by_source[r.data_source] = {"total": 0, "passed": 0, "failed": 0}
            by_source[r.data_source]["total"] += 1
            if r.passed:
                by_source[r.data_source]["passed"] += 1
            else:
                by_source[r.data_source]["failed"] += 1

        # Group by entity type
        by_entity: dict[str, dict] = {}
        for r in results:
            if r.entity_type not in by_entity:
                by_entity[r.entity_type] = {"total": 0, "passed": 0, "failed": 0}
            by_entity[r.entity_type]["total"] += 1
            if r.passed:
                by_entity[r.entity_type]["passed"] += 1
            else:
                by_entity[r.entity_type]["failed"] += 1

        return TestReport(
            timestamp=datetime.now().isoformat(),
            total_tests=len(results),
            passed=passed,
            failed=failed,
            errors=errors,
            pass_rate=passed / len(results) * 100 if results else 0,
            results_by_category=by_category,
            results_by_source=by_source,
            results_by_entity_type=by_entity,
            all_results=results,
        )

    def _print_summary(self, report: TestReport) -> None:
        """Print test summary to console."""
        print("\n" + "=" * 60)
        print("RAG QUALITY TEST SUMMARY")
        print("=" * 60)
        print(f"  Timestamp: {report.timestamp}")
        print(f"  Total Tests: {report.total_tests}")
        print(f"  Passed: {report.passed}")
        print(f"  Failed: {report.failed}")
        print(f"  Errors: {report.errors}")
        print(f"  Pass Rate: {report.pass_rate:.1f}%")

        print("\n--- Results by Category ---")
        for cat, stats in sorted(report.results_by_category.items()):
            rate = stats["passed"] / stats["total"] * 100 if stats["total"] else 0
            print(f"  {cat}: {stats['passed']}/{stats['total']} ({rate:.0f}%)")

        print("\n--- Results by Entity Type ---")
        for entity, stats in sorted(report.results_by_entity_type.items()):
            rate = stats["passed"] / stats["total"] * 100 if stats["total"] else 0
            print(f"  {entity}: {stats['passed']}/{stats['total']} ({rate:.0f}%)")

        print("\n--- Results by Data Source ---")
        for src, stats in sorted(report.results_by_source.items()):
            rate = stats["passed"] / stats["total"] * 100 if stats["total"] else 0
            print(f"  {src}: {stats['passed']}/{stats['total']} ({rate:.0f}%)")

        # Show failed tests
        failed_tests = [r for r in report.all_results if not r.passed]
        if failed_tests:
            print("\n--- Failed Tests (first 15) ---")
            for r in failed_tests[:15]:
                print(f"  [{r.test_id}] {r.prompt[:50]}...")
                if r.missing_data:
                    print(f"    Missing: {r.missing_data[:5]}")
                if r.error:
                    print(f"    Error: {r.error[:100]}")
            if len(failed_tests) > 15:
                print(f"  ... and {len(failed_tests) - 15} more")

        print("=" * 60)

    def save_report(self, report: TestReport, output_path: str) -> None:
        """Save report to JSON file."""
        report_dict = asdict(report)
        with open(output_path, "w") as f:
            json.dump(report_dict, f, indent=2, default=str)
        print(f"\nReport saved to: {output_path}")


def print_test_cases(test_cases: list[dict]) -> None:
    """Print test cases without running them (dry run)."""
    print("\n" + "=" * 60)
    print("RAG QUALITY TEST CASES (Dry Run)")
    print("=" * 60)

    for i, test in enumerate(test_cases, 1):
        print(f"\n[{i}] {test.get('id', 'unknown')}")
        print(f"    Entity: {test.get('entity_type', 'unknown')}")
        print(f"    Category: {test.get('category', 'unknown')}")
        print(f"    Prompt: {test.get('prompt', '')[:60]}...")
        expected = test.get('expected_data', [])
        print(f"    Expected: {expected[:5]}{'...' if len(expected) > 5 else ''}")

    print("\n" + "=" * 60)
    print(f"Total: {len(test_cases)} test cases")


async def fetch_ground_truth(
    limit: int = 0,
    jurisdiction: str | None = None,
    entity_types: list[str] | None = None,
    with_openstates: bool = False,
) -> tuple[list[BillGroundTruth], list[LegislatorGroundTruth], list[OrganizationGroundTruth]]:
    """Fetch ground truth data from Webflow CMS."""
    load_dotenv()

    fetcher = GroundTruthFetcher(
        webflow_api_key=os.environ["WEBFLOW_API_KEY"],
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


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="RAG Quality Test Runner for VoteBot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Mode selection
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--static",
        action="store_true",
        help="Run static YAML tests only",
    )
    mode_group.add_argument(
        "--dynamic",
        action="store_true",
        help="Run dynamic tests from Webflow ground truth",
    )
    mode_group.add_argument(
        "--all",
        action="store_true",
        help="Run both static and dynamic tests",
    )

    # Dynamic test options
    parser.add_argument(
        "--entity-type",
        choices=["bills", "legislators", "organizations"],
        action="append",
        dest="entity_types",
        help="Filter dynamic tests by entity type (can specify multiple)",
    )
    parser.add_argument(
        "--jurisdiction",
        help="Filter by jurisdiction (e.g., FL, VA, WA)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit entities per type for dynamic tests (0 = unlimited)",
    )
    parser.add_argument(
        "--with-openstates",
        action="store_true",
        help="Enrich ground truth with OpenStates data",
    )

    # Common options
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="VoteBot API URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--category",
        help="Run only tests in this category",
    )
    parser.add_argument(
        "--output",
        help="Write results to JSON file",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show test cases without running",
    )
    parser.add_argument(
        "--prompts-file",
        default="tests/rag_test_prompts.yaml",
        help="Path to static test prompts YAML file",
    )
    parser.add_argument(
        "--templates-file",
        default="tests/rag_test_templates.yaml",
        help="Path to dynamic test templates YAML file",
    )

    args = parser.parse_args()

    # Default to static tests if no mode specified
    if not args.static and not args.dynamic and not args.all:
        args.static = True

    all_test_cases = []

    # Load static tests
    if args.static or args.all:
        prompts_path = Path(args.prompts_file)
        if not prompts_path.is_absolute():
            prompts_path = Path(__file__).parent.parent / prompts_path

        if prompts_path.exists():
            tester = RAGQualityTester(api_url=args.api_url)
            static_tests = tester.load_test_cases(str(prompts_path))
            all_test_cases.extend(static_tests)
            print(f"Loaded {len(static_tests)} static test cases")
        else:
            print(f"Warning: Static prompts file not found: {prompts_path}")

    # Generate dynamic tests
    if args.dynamic or args.all:
        templates_path = Path(args.templates_file)
        if not templates_path.is_absolute():
            templates_path = Path(__file__).parent.parent / templates_path

        generator = DynamicTestGenerator(
            templates_path=str(templates_path) if templates_path.exists() else None
        )

        bills, legislators, organizations = await fetch_ground_truth(
            limit=args.limit,
            jurisdiction=args.jurisdiction,
            entity_types=args.entity_types,
            with_openstates=args.with_openstates,
        )

        entity_types = args.entity_types or ["bills", "legislators", "organizations"]

        if bills and "bills" in entity_types:
            bill_tests = generator.generate_bill_tests(bills)
            all_test_cases.extend(bill_tests)
            print(f"Generated {len(bill_tests)} bill test cases")

        if legislators and "legislators" in entity_types:
            legislator_tests = generator.generate_legislator_tests(legislators)
            all_test_cases.extend(legislator_tests)
            print(f"Generated {len(legislator_tests)} legislator test cases")

        if organizations and "organizations" in entity_types:
            org_tests = generator.generate_organization_tests(organizations)
            all_test_cases.extend(org_tests)
            print(f"Generated {len(org_tests)} organization test cases")

    if not all_test_cases:
        print("Error: No test cases loaded")
        return 1

    if args.dry_run:
        print_test_cases(all_test_cases)
        return 0

    # Run tests
    tester = RAGQualityTester(api_url=args.api_url)
    report = await tester.run_all_tests(
        all_test_cases,
        category_filter=args.category,
        verbose=args.verbose,
    )

    # Save report if output specified
    if args.output:
        tester.save_report(report, args.output)

    # Exit with appropriate code
    return 0 if report.pass_rate >= 80 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
