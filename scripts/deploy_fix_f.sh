#!/usr/bin/env bash
# Release sequencing for Fix F2 (PLAN-quick-action-buttons).
#
# Codifies the F1 -> verify -> F2 sequence with fail-fast checks at each step.
# Manual sequencing remains possible (Ramon's preferred ops style); this script
# is a guardrail that turns "did I remember to verify ddp-sync first?" into a
# fail-fast exit code.
#
# Prerequisites:
#   - F1 (ddp-sync producer removal) is committed AND deployed to ddp-sync.
#   - ddp-sync service has been restarted (e.g., systemctl restart ddp-sync).
#   - Required env vars: DDP_SYNC_URL, API_KEY, TEST_BILL_SLUG.
#
# Usage:
#   export DDP_SYNC_URL=https://api.digitaldemocracyproject.org
#   export API_KEY=<your votebot api key>
#   export TEST_BILL_SLUG=<a known-good bill slug, e.g. one-big-beautiful-bill-act-hr1-2025>
#   bash scripts/deploy_fix_f.sh

set -euo pipefail

# --- Sanity check env ---
: "${DDP_SYNC_URL:?DDP_SYNC_URL must be set (e.g. https://api.digitaldemocracyproject.org)}"
: "${API_KEY:?API_KEY must be set (votebot api key)}"
: "${TEST_BILL_SLUG:?TEST_BILL_SLUG must be set (a known-good bill slug)}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== Fix F deploy guardrail ==="
echo "DDP-Sync URL: $DDP_SYNC_URL"
echo "Test bill:    $TEST_BILL_SLUG"
echo

# --- Step 1: Probe ddp-sync for bill-history-free response ---
echo "Step 1: Probing ddp-sync /votebot/v1/sync/unified to confirm F1 is live..."
PROBE_RESPONSE=$(curl -sS --fail \
  -X POST "$DDP_SYNC_URL/votebot/v1/sync/unified" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"content_type\":\"bill\",\"mode\":\"single\",\"slug\":\"$TEST_BILL_SLUG\",\"include_openstates\":true}")

if echo "$PROBE_RESPONSE" | grep -q "bill-history-"; then
  echo "FAIL: ddp-sync probe response still contains 'bill-history-' doc IDs."
  echo "      F1 is not yet live. Verify ddp-sync is on the new commit and restarted."
  echo
  echo "Probe response excerpt:"
  echo "$PROBE_RESPONSE" | head -c 500
  echo
  exit 1
fi
echo "OK: ddp-sync probe is clean (no bill-history-* doc IDs in response)."
echo

# --- Step 2: Run the Pinecone flush ---
echo "Step 2: Running Pinecone flush..."
PYTHONPATH=src .venv/bin/python scripts/flush_bill_history.py --yes
echo

# --- Step 3: Post-flush sanity check from the persisted record ---
RECORD_PATH="logs/eval/flush_bill_history.json"
if [ ! -f "$RECORD_PATH" ]; then
  echo "FAIL: $RECORD_PATH not found. Flush script did not complete normally."
  exit 1
fi

POST_COUNT=$(.venv/bin/python -c "
import json, sys
with open('$RECORD_PATH') as f:
    print(json.load(f).get('post_count', -1))
")

if [ "$POST_COUNT" != "0" ]; then
  echo "FAIL: $POST_COUNT bill-history vectors remain after flush."
  echo "      The Pinecone metadata-filter delete is idempotent — re-running the"
  echo "      flush script with --confirm should drive the count to zero. If a"
  echo "      second run still leaves vectors, escalate to Pinecone support."
  exit 1
fi

echo "OK: Pinecone bill-history vector count is 0."
echo
echo "=== Fix F deploy complete ==="
echo "Next steps:"
echo "  - Run 'PYTHONPATH=src .venv/bin/python scripts/evaluate_production.py --days 7'"
echo "    after some traffic accumulates. The Bill-history leak canary should report"
echo "    bill_history_leak_count: 0."
echo "  - Re-run the eval at days 14 and 30 post-deploy to confirm sustained zero."
