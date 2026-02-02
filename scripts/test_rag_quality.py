#!/usr/bin/env python3
"""
RAG Quality Test Runner for VoteBot.

Runs a suite of test prompts against VoteBot and validates that expected
data is present in responses. Generates a diagnostic report showing:
- Which queries pass/fail
- What expected data is missing
- Data source coverage gaps

Usage:
    python scripts/test_rag_quality.py [options]

Options:
    --api-url URL       VoteBot API URL (default: http://localhost:8000)
    --category CAT      Run only tests in this category
    --output FILE       Write results to JSON file
    --verbose           Show full responses
    --dry-run           Show test cases without running
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


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
    all_results: list[TestResult] = field(default_factory=list)


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
        import os
        from dotenv import load_dotenv
        load_dotenv()
        # Try VOTEBOT_API_KEY first, then fall back to API_KEY
        return os.environ.get("VOTEBOT_API_KEY") or os.environ.get("API_KEY", "test-key")

    def load_test_cases(self, yaml_path: str) -> list[dict]:
        """Load test cases from YAML file."""
        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)

        # Flatten all test categories into a list
        test_cases = []
        for category_name, tests in data.items():
            if isinstance(tests, list):
                for test in tests:
                    test["category_group"] = category_name
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

        result = TestResult(
            test_id=test_id,
            prompt=prompt,
            category=category,
            data_source=data_source,
            passed=False,
            expected_data=expected_data,
        )

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                # Generate a unique session ID for each test
                import uuid
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
                        "page_context": {
                            "type": "general",
                        },
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

                # Check for expected data in response
                for expected in expected_data:
                    expected_lower = expected.lower()
                    if expected_lower in response_text:
                        result.found_data.append(expected)
                    else:
                        result.missing_data.append(expected)

                # Test passes if all expected data is found
                result.passed = len(result.missing_data) == 0

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

    async def run_all_tests(
        self,
        test_cases: list[dict],
        category_filter: str | None = None,
        verbose: bool = False,
    ) -> TestReport:
        """Run all test cases and generate report."""
        # Filter by category if specified
        if category_filter:
            test_cases = [
                t for t in test_cases
                if t.get("category") == category_filter
                or t.get("category_group") == category_filter
            ]

        print(f"\nRunning {len(test_cases)} RAG quality tests...")
        print("=" * 60)

        results = []
        for test_case in test_cases:
            result = await self.run_single_test(test_case, verbose)
            results.append(result)

            # Rate limiting - don't overwhelm the API
            await asyncio.sleep(0.5)

        # Generate report
        report = self._generate_report(results)

        # Print summary
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

        return TestReport(
            timestamp=datetime.now().isoformat(),
            total_tests=len(results),
            passed=passed,
            failed=failed,
            errors=errors,
            pass_rate=passed / len(results) * 100 if results else 0,
            results_by_category=by_category,
            results_by_source=by_source,
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

        print("\n--- Results by Data Source ---")
        for src, stats in sorted(report.results_by_source.items()):
            rate = stats["passed"] / stats["total"] * 100 if stats["total"] else 0
            print(f"  {src}: {stats['passed']}/{stats['total']} ({rate:.0f}%)")

        # Show failed tests
        failed_tests = [r for r in report.all_results if not r.passed]
        if failed_tests:
            print("\n--- Failed Tests ---")
            for r in failed_tests[:10]:
                print(f"  [{r.test_id}] {r.prompt[:40]}...")
                if r.missing_data:
                    print(f"    Missing: {r.missing_data}")
                if r.error:
                    print(f"    Error: {r.error}")
            if len(failed_tests) > 10:
                print(f"  ... and {len(failed_tests) - 10} more")

        print("=" * 60)

    def save_report(self, report: TestReport, output_path: str) -> None:
        """Save report to JSON file."""
        # Convert dataclasses to dicts
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
        print(f"    Category: {test.get('category', 'unknown')}")
        print(f"    Source: {test.get('data_source', 'unknown')}")
        print(f"    Prompt: {test.get('prompt', '')[:60]}...")
        print(f"    Expected: {test.get('expected_data', [])}")

    print("\n" + "=" * 60)
    print(f"Total: {len(test_cases)} test cases")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="RAG Quality Test Runner for VoteBot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

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
        help="Show full responses",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show test cases without running",
    )
    parser.add_argument(
        "--prompts-file",
        default="tests/rag_test_prompts.yaml",
        help="Path to test prompts YAML file",
    )

    args = parser.parse_args()

    # Find prompts file
    prompts_path = Path(args.prompts_file)
    if not prompts_path.is_absolute():
        prompts_path = Path(__file__).parent.parent / prompts_path

    if not prompts_path.exists():
        print(f"Error: Prompts file not found: {prompts_path}")
        return 1

    # Load test cases
    tester = RAGQualityTester(api_url=args.api_url)
    test_cases = tester.load_test_cases(str(prompts_path))

    if args.dry_run:
        print_test_cases(test_cases)
        return 0

    # Run tests
    report = await tester.run_all_tests(
        test_cases,
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
