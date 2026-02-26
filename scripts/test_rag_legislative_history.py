#!/usr/bin/env python3
"""RAG test focused on legislative history from OpenStates (sponsors, votes, status)."""

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
    test_category: str
    prompts: list[str]
    responses: list[str]
    has_citations: list[bool]
    confidence_scores: list[float]
    latencies: list[float]
    success: bool
    error: Optional[str] = None


# Legislative history question templates - focused on OpenStates data
BILL_DESCRIPTION_TEMPLATES = [
    {
        "initial": "What is {bill_title} about?",
        "follow_ups": [
            "Who sponsored this bill?",
            "What is the current status of this bill?",
            "Has this bill had any votes yet?",
        ]
    },
    {
        "initial": "Can you summarize {bill_title}?",
        "follow_ups": [
            "Who are the primary sponsors?",
            "What actions have been taken on this bill?",
            "Which committee is reviewing this bill?",
        ]
    },
    {
        "initial": "Tell me about {bill_title} in {jurisdiction}.",
        "follow_ups": [
            "Who introduced this legislation?",
            "Has it passed any chamber votes?",
            "What was the most recent action on this bill?",
        ]
    },
]

# Sponsor-focused questions
SPONSOR_TEMPLATES = [
    {
        "initial": "Who sponsored {bill_title}?",
        "follow_ups": [
            "Are there any co-sponsors?",
            "Which party do the sponsors belong to?",
            "Have these sponsors introduced similar bills before?",
        ]
    },
    {
        "initial": "Who introduced {bill_title} in {jurisdiction}?",
        "follow_ups": [
            "How many co-sponsors does this bill have?",
            "Is this a bipartisan bill?",
        ]
    },
]

# Vote-focused questions
VOTE_TEMPLATES = [
    {
        "initial": "What were the vote results for {bill_title}?",
        "follow_ups": [
            "How many legislators voted yes?",
            "How many voted no?",
            "Was this a close vote or a landslide?",
        ]
    },
    {
        "initial": "Has {bill_title} been voted on yet?",
        "follow_ups": [
            "Which chamber voted on it?",
            "Did it pass or fail?",
            "When was the vote held?",
        ]
    },
    {
        "initial": "Did {bill_title} pass in {jurisdiction}?",
        "follow_ups": [
            "What was the final vote count?",
            "Has it moved to the other chamber?",
        ]
    },
]

# Status-focused questions
STATUS_TEMPLATES = [
    {
        "initial": "What is the current status of {bill_title}?",
        "follow_ups": [
            "Has it passed out of committee?",
            "Is it still active or has it died?",
            "What's the next step for this bill?",
        ]
    },
    {
        "initial": "Where is {bill_title} in the legislative process?",
        "follow_ups": [
            "Has the governor signed it yet?",
            "Which committee is it assigned to?",
            "When was the last action taken?",
        ]
    },
    {
        "initial": "Has {bill_title} been signed into law?",
        "follow_ups": [
            "Did it pass both chambers?",
            "Were there any amendments?",
            "When did it become law?",
        ]
    },
    {
        "initial": "Did {bill_title} die in committee?",
        "follow_ups": [
            "What was the last action before it stalled?",
            "Can it be reintroduced?",
        ]
    },
]

# Action history questions
ACTION_HISTORY_TEMPLATES = [
    {
        "initial": "What actions have been taken on {bill_title}?",
        "follow_ups": [
            "When was it first introduced?",
            "Has it been referred to any committees?",
            "What happened most recently?",
        ]
    },
    {
        "initial": "Give me the legislative history of {bill_title}.",
        "follow_ups": [
            "How long has this bill been in the legislature?",
            "Has it faced any obstacles?",
        ]
    },
]


async def fetch_sample_bills():
    """Fetch sample bills from Webflow CMS."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = {
            "Authorization": f"Bearer {settings.webflow_votebot_api_key.get_secret_value()}",
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
    # Filter to bills that have OpenStates URLs (these have legislative history)
    bills_with_openstates = [
        b for b in bills
        if b.get("fieldData", {}).get("open-states-url-2")
    ]

    by_jurisdiction = {}

    for bill in bills_with_openstates:
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

    # Allocate samples evenly across jurisdictions
    per_jurisdiction = max(1, sample_size // len(priority_jurisdictions)) if priority_jurisdictions else sample_size

    for jurisdiction_id in priority_jurisdictions:
        bills_in_j = by_jurisdiction[jurisdiction_id]
        n_sample = min(per_jurisdiction, len(bills_in_j))
        selected.extend(random.sample(bills_in_j, n_sample))

        if len(selected) >= sample_size:
            break

    # If we need more, sample from remaining
    if len(selected) < sample_size:
        remaining_bills = [b for b in bills_with_openstates if b not in selected]
        if remaining_bills:
            n_more = min(sample_size - len(selected), len(remaining_bills))
            selected.extend(random.sample(remaining_bills, n_more))

    return selected[:sample_size]


def generate_tests(bills: list) -> list[dict]:
    """Generate test cases focused on legislative history."""
    tests = []

    # All template categories
    all_templates = {
        "description": BILL_DESCRIPTION_TEMPLATES,
        "sponsor": SPONSOR_TEMPLATES,
        "vote": VOTE_TEMPLATES,
        "status": STATUS_TEMPLATES,
        "action_history": ACTION_HISTORY_TEMPLATES,
    }

    for bill in bills:
        fields = bill.get("fieldData", {})
        title = fields.get("name", "Unknown Bill")
        jurisdiction_id = fields.get("jurisdiction", "")
        jurisdiction_name = JURISDICTIONS.get(jurisdiction_id, "Unknown")

        # Select a random category for this bill
        category = random.choice(list(all_templates.keys()))
        template = random.choice(all_templates[category])

        # Format the questions
        initial = template["initial"].format(
            bill_title=title,
            jurisdiction=jurisdiction_name,
        )

        follow_ups = [
            q.format(bill_title=title, jurisdiction=jurisdiction_name)
            for q in template["follow_ups"]
        ]

        # Select 2-3 follow-ups to probe deeper
        n_follow_ups = random.randint(2, min(3, len(follow_ups)))
        selected_follow_ups = random.sample(follow_ups, n_follow_ups)

        tests.append({
            "type": "bill",
            "category": category,
            "jurisdiction": jurisdiction_name,
            "bill_title": title,
            "initial": initial,
            "follow_ups": selected_follow_ups,
        })

    return tests


async def run_chat_test(test: dict, api_url: str, api_key: str) -> TestResult:
    """Run a multi-turn chat test."""
    import time

    session_id = f"test-leg-{random.randint(10000, 99999)}"
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
                            "type": "bill",
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
                        test_category=test["category"],
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
                    test_category=test["category"],
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
        test_category=test["category"],
        prompts=prompts,
        responses=responses,
        has_citations=has_citations,
        confidence_scores=confidence_scores,
        latencies=latencies,
        success=True
    )


async def main():
    """Run legislative history RAG tests."""
    print("=" * 70)
    print("VOTEBOT LEGISLATIVE HISTORY RAG TEST")
    print("=" * 70)
    print("Testing: Bill descriptions, sponsors, votes, status, action history")
    print("=" * 70)

    # Configuration
    api_url = "http://localhost:8000"
    api_key = settings.api_key.get_secret_value()

    # Fetch bills
    print("\nFetching bills from Webflow CMS...")
    all_bills = await fetch_sample_bills()
    print(f"Total bills available: {len(all_bills)}")

    # Count bills with OpenStates URLs
    bills_with_openstates = [
        b for b in all_bills
        if b.get("fieldData", {}).get("open-states-url-2")
    ]
    print(f"Bills with OpenStates data: {len(bills_with_openstates)}")

    # Select stratified sample
    selected_bills = select_bills_by_jurisdiction(all_bills, sample_size=50)
    print(f"Selected {len(selected_bills)} bills for testing")

    # Generate tests
    all_tests = generate_tests(selected_bills)
    random.shuffle(all_tests)

    # Count by category
    by_category = {}
    for t in all_tests:
        cat = t["category"]
        by_category[cat] = by_category.get(cat, 0) + 1

    print(f"\nTotal test cases: {len(all_tests)}")
    print("Test distribution by category:")
    for cat, count in sorted(by_category.items()):
        print(f"  - {cat}: {count}")

    # Run tests
    print("\n" + "=" * 70)
    print("RUNNING TESTS")
    print("=" * 70)

    results = []
    for i, test in enumerate(all_tests):
        print(f"\n[{i+1}/{len(all_tests)}] {test['category'].upper()}: {test['bill_title'][:45]}...")
        print(f"  Q: {test['initial'][:65]}...")

        result = await run_chat_test(test, api_url, api_key)
        results.append(result)

        if result.success:
            avg_confidence = sum(result.confidence_scores) / len(result.confidence_scores) if result.confidence_scores else 0
            avg_latency = sum(result.latencies) / len(result.latencies) if result.latencies else 0
            citations_pct = sum(result.has_citations) / len(result.has_citations) * 100 if result.has_citations else 0

            print(f"  OK {len(result.prompts)} turns, conf: {avg_confidence:.2f}, "
                  f"citations: {citations_pct:.0f}%, latency: {avg_latency:.1f}s")
        else:
            print(f"  FAIL: {result.error[:60]}...")

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

    # Results by category
    print(f"\nResults by Category:")
    by_category_results = {}
    for r in successful:
        cat = r.test_category
        if cat not in by_category_results:
            by_category_results[cat] = []
        by_category_results[cat].append(r)

    for cat, cat_results in sorted(by_category_results.items()):
        cat_confidences = [c for r in cat_results for c in r.confidence_scores]
        cat_citations = [c for r in cat_results for c in r.has_citations]
        avg_conf = sum(cat_confidences) / len(cat_confidences) if cat_confidences else 0
        citation_rate = sum(cat_citations) / len(cat_citations) * 100 if cat_citations else 0
        print(f"  {cat}: {len(cat_results)} tests, conf: {avg_conf:.2f}, citations: {citation_rate:.0f}%")

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
    output_file = Path(__file__).parent / "test_results_legislative.json"
    with open(output_file, "w") as f:
        json.dump([{
            "test_id": r.test_id,
            "jurisdiction": r.jurisdiction,
            "bill_title": r.bill_title,
            "test_category": r.test_category,
            "prompts": r.prompts,
            "responses": r.responses,
            "has_citations": r.has_citations,
            "confidence_scores": r.confidence_scores,
            "latencies": r.latencies,
            "success": r.success,
            "error": r.error,
        } for r in results], f, indent=2)

    print(f"\nDetailed results saved to: {output_file}")

    # Show sample conversations by category
    print("\n" + "=" * 70)
    print("SAMPLE CONVERSATIONS BY CATEGORY")
    print("=" * 70)

    for category in ["description", "sponsor", "vote", "status", "action_history"]:
        cat_successes = [r for r in successful if r.test_category == category]
        if cat_successes:
            sample = random.choice(cat_successes)
            print(f"\n--- {category.upper()}: {sample.bill_title[:50]} ({sample.jurisdiction}) ---")
            for i, (prompt, response) in enumerate(zip(sample.prompts, sample.responses)):
                print(f"\nUser: {prompt}")
                response_preview = response[:400] + "..." if len(response) > 400 else response
                print(f"Bot: {response_preview}")
                print(f"[Confidence: {sample.confidence_scores[i]:.2f}, Citations: {sample.has_citations[i]}]")


if __name__ == "__main__":
    asyncio.run(main())
