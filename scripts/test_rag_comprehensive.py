#!/usr/bin/env python3
"""Comprehensive RAG test with multi-turn conversations."""

import asyncio
import json
import random
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx
from votebot.config import get_settings

# Clear settings cache
get_settings.cache_clear()
settings = get_settings()

# Jurisdiction ID to name mapping
JURISDICTIONS = {
    "655288ef928edb128306745f": "Florida",
    "65810f6b889af86635a71b49": "US Federal",
    "691294466973f77ba7924c9b": "Washington",
    "6912910d68fa6adb1b2b630f": "Virginia",
    "6912929f5ec63fd925b99c10": "Michigan",
    "6912928fd6eec8ac6bccb2c8": "Massachusetts",
    "69129425a577496525c8e52a": "Utah",
    "6912916752bfa901425f1e76": "Arizona",
    "69129146d6eec8ac6bcc8280": "Alabama",
}


@dataclass
class TestResult:
    """Result of a single test."""
    test_id: str
    jurisdiction: str
    bill_title: str
    prompts: list[str]
    responses: list[str]
    has_citations: list[bool]
    confidence_scores: list[float]
    latencies: list[float]
    success: bool
    error: Optional[str] = None


# DDP General Knowledge Questions with follow-ups
DDP_QUESTIONS = [
    {
        "initial": "What is the Digital Democracy Project?",
        "follow_ups": [
            "How do voters participate in the platform?",
            "What makes DDP different from regular elections?",
        ]
    },
    {
        "initial": "How do I sign up for Digital Democracy Project?",
        "follow_ups": [
            "What ID do I need to verify my identity?",
            "Is my vote anonymous?",
        ]
    },
    {
        "initial": "What states does Digital Democracy Project operate in?",
        "follow_ups": [
            "Can I vote on federal legislation?",
            "When will DDP expand to more states?",
        ]
    },
    {
        "initial": "How does DDP score legislators?",
        "follow_ups": [
            "Where can I see my representative's scorecard?",
            "What happens when a legislator votes against their constituents?",
        ]
    },
    {
        "initial": "Is Digital Democracy Project partisan?",
        "follow_ups": [
            "Does DDP endorse candidates?",
            "How does DDP choose which bills to feature?",
        ]
    },
    {
        "initial": "What is the Voatz app?",
        "follow_ups": [
            "Is the Voatz app secure?",
            "Can I vote without downloading an app?",
        ]
    },
    {
        "initial": "How are votes tallied on DDP?",
        "follow_ups": [
            "Can I see how other people in my district voted?",
            "Are the results shared with legislators?",
        ]
    },
    {
        "initial": "What's the DDP tagline?",
        "follow_ups": [
            "What does 'You vote. We track it. So you know the score.' mean?",
        ]
    },
]

# Bill-specific question templates
BILL_QUESTION_TEMPLATES = [
    {
        "initial": "What is {bill_title} about?",
        "follow_ups": [
            "What are the main arguments in support of this bill?",
            "What are the arguments against it?",
            "What is the current status of this bill?",
        ]
    },
    {
        "initial": "Can you summarize {bill_title}?",
        "follow_ups": [
            "Who sponsored this legislation?",
            "What committee is it assigned to?",
        ]
    },
    {
        "initial": "What would change if {bill_title} passes?",
        "follow_ups": [
            "Who would be most affected by this bill?",
            "Are there similar bills in other states?",
        ]
    },
    {
        "initial": "Tell me about the {jurisdiction} bill on {topic}",
        "follow_ups": [
            "What's the bill number?",
            "Has there been any public debate on this?",
        ]
    },
]


async def fetch_sample_bills():
    """Fetch sample bills from Webflow CMS."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = {
            "Authorization": f"Bearer {settings.webflow_api_key.get_secret_value()}",
            "accept": "application/json",
        }

        all_bills = []
        offset = 0

        # Fetch all bills
        while True:
            response = await client.get(
                f"https://api.webflow.com/v2/collections/{settings.webflow_bills_collection_id}/items",
                headers=headers,
                params={"limit": 100, "offset": offset}
            )

            if response.status_code != 200:
                break

            data = response.json()
            items = data.get("items", [])

            if not items:
                break

            all_bills.extend(items)
            offset += 100

            if len(items) < 100:
                break

        return all_bills


def select_bills_by_jurisdiction(bills: list, sample_size: int = 50) -> list:
    """Select a stratified sample of bills across jurisdictions."""
    by_jurisdiction = {}

    for bill in bills:
        fields = bill.get("fieldData", {})
        jurisdiction_id = fields.get("jurisdiction")

        if jurisdiction_id not in by_jurisdiction:
            by_jurisdiction[jurisdiction_id] = []
        by_jurisdiction[jurisdiction_id].append(bill)

    # Calculate how many to sample from each jurisdiction
    selected = []
    jurisdictions_with_bills = [j for j in by_jurisdiction if by_jurisdiction[j]]

    if not jurisdictions_with_bills:
        return []

    # Prioritize jurisdictions we have names for
    priority_jurisdictions = [j for j in jurisdictions_with_bills if j in JURISDICTIONS]
    other_jurisdictions = [j for j in jurisdictions_with_bills if j not in JURISDICTIONS]

    # Allocate samples
    per_priority = sample_size // (len(priority_jurisdictions) + 1) if priority_jurisdictions else 0

    for jurisdiction_id in priority_jurisdictions:
        bills_in_j = by_jurisdiction[jurisdiction_id]
        n_sample = min(per_priority, len(bills_in_j))
        selected.extend(random.sample(bills_in_j, n_sample))

    # Fill remaining with other jurisdictions
    remaining = sample_size - len(selected)
    if remaining > 0 and other_jurisdictions:
        other_bills = []
        for j in other_jurisdictions:
            other_bills.extend(by_jurisdiction[j])
        if other_bills:
            selected.extend(random.sample(other_bills, min(remaining, len(other_bills))))

    return selected


def generate_bill_tests(bills: list) -> list[dict]:
    """Generate test cases for bills."""
    tests = []

    for bill in bills:
        fields = bill.get("fieldData", {})
        title = fields.get("name", "Unknown Bill")
        jurisdiction_id = fields.get("jurisdiction", "")
        jurisdiction_name = JURISDICTIONS.get(jurisdiction_id, jurisdiction_id[:8] + "...")
        description = fields.get("description", "")

        # Extract topic from title or description
        topic_words = title.split()[:3]
        topic = " ".join(topic_words).lower()

        # Select a random question template
        template = random.choice(BILL_QUESTION_TEMPLATES)

        # Format the questions
        initial = template["initial"].format(
            bill_title=title,
            jurisdiction=jurisdiction_name,
            topic=topic
        )

        follow_ups = [
            q.format(bill_title=title, jurisdiction=jurisdiction_name, topic=topic)
            for q in template["follow_ups"]
        ]

        # Randomly select 1-3 follow-ups
        n_follow_ups = random.randint(1, min(3, len(follow_ups)))
        selected_follow_ups = random.sample(follow_ups, n_follow_ups)

        tests.append({
            "type": "bill",
            "jurisdiction": jurisdiction_name,
            "bill_title": title,
            "initial": initial,
            "follow_ups": selected_follow_ups,
        })

    return tests


def generate_ddp_tests() -> list[dict]:
    """Generate DDP general knowledge tests."""
    tests = []

    for q in DDP_QUESTIONS:
        n_follow_ups = random.randint(1, min(3, len(q["follow_ups"])))
        selected_follow_ups = random.sample(q["follow_ups"], n_follow_ups)

        tests.append({
            "type": "ddp",
            "jurisdiction": "General",
            "bill_title": "DDP Knowledge",
            "initial": q["initial"],
            "follow_ups": selected_follow_ups,
        })

    return tests


async def run_chat_test(test: dict, api_url: str, api_key: str) -> TestResult:
    """Run a multi-turn chat test."""
    import time

    session_id = f"test-{random.randint(10000, 99999)}"
    prompts = [test["initial"]] + test["follow_ups"]
    responses = []
    has_citations = []
    confidence_scores = []
    latencies = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        for i, prompt in enumerate(prompts):
            start_time = time.time()

            try:
                response = await client.post(
                    f"{api_url}/votebot/v1/chat",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "message": prompt,
                        "session_id": session_id,
                        "human_active": False,
                        "page_context": {
                            "type": "general" if test["type"] == "ddp" else "bill",
                        }
                    }
                )

                latency = time.time() - start_time
                latencies.append(latency)

                if response.status_code != 200:
                    return TestResult(
                        test_id=session_id,
                        jurisdiction=test["jurisdiction"],
                        bill_title=test["bill_title"],
                        prompts=prompts[:i+1],
                        responses=responses,
                        has_citations=has_citations,
                        confidence_scores=confidence_scores,
                        latencies=latencies,
                        success=False,
                        error=f"HTTP {response.status_code}: {response.text[:200]}"
                    )

                data = response.json()
                responses.append(data.get("response", ""))
                has_citations.append(len(data.get("citations", [])) > 0)
                confidence_scores.append(data.get("confidence", 0.0))

            except Exception as e:
                return TestResult(
                    test_id=session_id,
                    jurisdiction=test["jurisdiction"],
                    bill_title=test["bill_title"],
                    prompts=prompts[:i+1],
                    responses=responses,
                    has_citations=has_citations,
                    confidence_scores=confidence_scores,
                    latencies=latencies,
                    success=False,
                    error=str(e)
                )

    return TestResult(
        test_id=session_id,
        jurisdiction=test["jurisdiction"],
        bill_title=test["bill_title"],
        prompts=prompts,
        responses=responses,
        has_citations=has_citations,
        confidence_scores=confidence_scores,
        latencies=latencies,
        success=True
    )


async def main():
    """Run comprehensive RAG tests."""
    print("=" * 70)
    print("VOTEBOT COMPREHENSIVE RAG TEST")
    print("=" * 70)

    # Configuration
    api_url = "http://localhost:8000"
    api_key = settings.api_key.get_secret_value()

    # Fetch bills
    print("\nFetching bills from Webflow CMS...")
    all_bills = await fetch_sample_bills()
    print(f"Total bills available: {len(all_bills)}")

    # Select stratified sample
    selected_bills = select_bills_by_jurisdiction(all_bills, sample_size=50)
    print(f"Selected {len(selected_bills)} bills for testing")

    # Generate tests
    bill_tests = generate_bill_tests(selected_bills)
    ddp_tests = generate_ddp_tests()

    all_tests = bill_tests + ddp_tests
    random.shuffle(all_tests)

    print(f"\nTotal test cases: {len(all_tests)}")
    print(f"  - Bill questions: {len(bill_tests)}")
    print(f"  - DDP questions: {len(ddp_tests)}")

    # Run tests
    print("\n" + "=" * 70)
    print("RUNNING TESTS")
    print("=" * 70)

    results = []
    for i, test in enumerate(all_tests):
        print(f"\n[{i+1}/{len(all_tests)}] {test['type'].upper()}: {test['bill_title'][:50]}...")
        print(f"  Initial: {test['initial'][:60]}...")

        result = await run_chat_test(test, api_url, api_key)
        results.append(result)

        if result.success:
            avg_confidence = sum(result.confidence_scores) / len(result.confidence_scores) if result.confidence_scores else 0
            avg_latency = sum(result.latencies) / len(result.latencies) if result.latencies else 0
            citations_pct = sum(result.has_citations) / len(result.has_citations) * 100 if result.has_citations else 0

            print(f"  ✓ {len(result.prompts)} turns, avg confidence: {avg_confidence:.2f}, "
                  f"citations: {citations_pct:.0f}%, avg latency: {avg_latency:.1f}s")
        else:
            print(f"  ✗ FAILED: {result.error[:60]}...")

    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)

    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    print(f"\nTotal tests: {len(results)}")
    print(f"Successful: {len(successful)} ({len(successful)/len(results)*100:.1f}%)")
    print(f"Failed: {len(failed)} ({len(failed)/len(results)*100:.1f}%)")

    if successful:
        all_confidences = [c for r in successful for c in r.confidence_scores]
        all_latencies = [l for r in successful for l in r.latencies]
        all_citations = [c for r in successful for c in r.has_citations]

        print(f"\nPerformance Metrics:")
        print(f"  Avg confidence: {sum(all_confidences)/len(all_confidences):.2f}")
        print(f"  High confidence (>0.7): {sum(1 for c in all_confidences if c > 0.7)/len(all_confidences)*100:.1f}%")
        print(f"  With citations: {sum(all_citations)/len(all_citations)*100:.1f}%")
        print(f"  Avg latency: {sum(all_latencies)/len(all_latencies):.2f}s")
        print(f"  P95 latency: {sorted(all_latencies)[int(len(all_latencies)*0.95)]:.2f}s")

    # Results by jurisdiction
    print(f"\nResults by Jurisdiction:")
    by_jurisdiction = {}
    for r in successful:
        j = r.jurisdiction
        if j not in by_jurisdiction:
            by_jurisdiction[j] = []
        by_jurisdiction[j].append(r)

    for j, j_results in sorted(by_jurisdiction.items()):
        j_confidences = [c for r in j_results for c in r.confidence_scores]
        avg_conf = sum(j_confidences) / len(j_confidences) if j_confidences else 0
        print(f"  {j}: {len(j_results)} tests, avg confidence: {avg_conf:.2f}")

    # Save detailed results
    output_file = Path(__file__).parent / "test_results.json"
    with open(output_file, "w") as f:
        json.dump([{
            "test_id": r.test_id,
            "jurisdiction": r.jurisdiction,
            "bill_title": r.bill_title,
            "prompts": r.prompts,
            "responses": r.responses,
            "has_citations": r.has_citations,
            "confidence_scores": r.confidence_scores,
            "latencies": r.latencies,
            "success": r.success,
            "error": r.error,
        } for r in results], f, indent=2)

    print(f"\nDetailed results saved to: {output_file}")

    # Show sample conversations
    print("\n" + "=" * 70)
    print("SAMPLE CONVERSATIONS")
    print("=" * 70)

    samples = random.sample(successful, min(3, len(successful)))
    for r in samples:
        print(f"\n--- {r.jurisdiction}: {r.bill_title[:50]} ---")
        for i, (prompt, response) in enumerate(zip(r.prompts, r.responses)):
            print(f"\nUser: {prompt}")
            print(f"Bot: {response[:300]}..." if len(response) > 300 else f"Bot: {response}")
            print(f"[Confidence: {r.confidence_scores[i]:.2f}, Citations: {r.has_citations[i]}]")


if __name__ == "__main__":
    asyncio.run(main())
