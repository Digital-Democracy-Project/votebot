"""infrastructure/render-env.sh: writes the production .env from Secrets Manager (stubbed `aws`), never prints a value.

Standard library only, so it also runs on a host without the project's dependencies:
    python3 -m unittest tests.unit.infrastructure.test_render_env
"""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "infrastructure" / "render-env.sh"

VOTEBOT_SECRET = {
    "api_key": "vb-api-SECRETVALUE-1",
    "openai_api_key": "sk-openai-SECRETVALUE-2",
    "pinecone_api_key": "pc-SECRETVALUE-3",
    "ignored_extra_key": "never-written-SECRETVALUE-9",
}
SYNC_SECRET = {"rds_openstates_api_key": "api3-SECRETVALUE-4", "openai_api_key": "ddp-sync-openai-must-not-be-used"}
ALL_VALUES = ["SECRETVALUE-1", "SECRETVALUE-2", "SECRETVALUE-3", "SECRETVALUE-4", "SECRETVALUE-9"]

FAKE_AWS = """#!/usr/bin/env bash
# stub: prints the JSON for the requested --secret-id from $FAKE_SECRETS_DIR/<id with / replaced by _>.json
while [ $# -gt 0 ]; do [ "$1" = "--secret-id" ] && { id="$2"; break; }; shift; done
f="$FAKE_SECRETS_DIR/${id//\\//_}.json"
[ -f "$f" ] || { echo "ResourceNotFoundException: $id" >&2; exit 254; }
cat "$f"
"""


class RenderEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.bin = t / "bin"
        self.secrets = t / "secrets"
        self.bin.mkdir()
        self.secrets.mkdir()
        aws = self.bin / "aws"
        aws.write_text(FAKE_AWS)
        aws.chmod(0o755)
        self.put("votebot/credentials", VOTEBOT_SECRET)
        self.put("ddp-sync/credentials", SYNC_SECRET)
        self.out = t / "out.env"

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, secret_id, doc):
        (self.secrets / (secret_id.replace("/", "_") + ".json")).write_text(json.dumps(doc))

    def run_script(self, *args, extra_env=None):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_SECRETS_DIR=str(self.secrets))
        env.pop("API_SOURCE_SECRET_ID", None)
        env.update(extra_env or {})
        return subprocess.run(["bash", str(SCRIPT), "--out", str(self.out), *args], env=env, capture_output=True, text=True)

    def test_writes_secrets_and_defaults_with_mode_600_and_prints_no_value(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        for v in ALL_VALUES:
            self.assertNotIn(v, r.stdout + r.stderr)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        text = self.out.read_text()
        self.assertIn("API_KEY=vb-api-SECRETVALUE-1\n", text)
        self.assertIn("OPENAI_API_KEY=sk-openai-SECRETVALUE-2\n", text)
        self.assertIn("PINECONE_API_KEY=pc-SECRETVALUE-3\n", text)
        self.assertIn("DDP_OPENSTATES_BEARER_TOKEN=api3-SECRETVALUE-4\n", text)
        self.assertIn("PINECONE_INDEX_NAME=ddp-knowledge-base\n", text)
        self.assertIn("DDP_OPENSTATES_AUTH_HEADER=x-api-key\n", text)
        self.assertNotIn("SECRETVALUE-9", text)  # keys that are not mapped are never written
        self.assertNotIn("ddp-sync-openai-must-not-be-used", text)  # ddp-sync's OpenAI key is never reused

    def test_missing_secret_key_fails_and_leaves_existing_file_untouched(self):
        self.out.write_text("SENTINEL=1\n")
        self.put("votebot/credentials", {k: v for k, v in VOTEBOT_SECRET.items() if k != "openai_api_key"})
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("OPENAI_API_KEY", r.stderr)
        self.assertEqual(self.out.read_text(), "SENTINEL=1\n")

    def test_empty_value_counts_as_missing(self):
        self.put("votebot/credentials", dict(VOTEBOT_SECRET, pinecone_api_key=""))
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("PINECONE_API_KEY", r.stderr)
        self.assertFalse(self.out.exists())

    def test_fetch_failure_fails_and_writes_nothing(self):
        (self.secrets / "votebot_credentials.json").unlink()
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.out.exists())

    def test_check_writes_nothing_and_prints_names_only(self):
        r = self.run_script("--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.out.exists())
        self.assertIn("OPENAI_API_KEY", r.stdout)
        for v in ALL_VALUES:
            self.assertNotIn(v, r.stdout + r.stderr)

    def test_api_key_can_come_from_the_votebot_secret_when_source_is_empty(self):
        self.put("votebot/credentials", dict(VOTEBOT_SECRET, ddp_openstates_api_key="own-SECRETVALUE-5"))
        r = self.run_script(extra_env={"API_SOURCE_SECRET_ID": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DDP_OPENSTATES_BEARER_TOKEN=own-SECRETVALUE-5\n", self.out.read_text())

    def test_defaults_file_may_not_define_a_secret_name(self):
        bad = Path(self.tmp.name) / "bad.defaults"
        bad.write_text("OPENAI_API_KEY=oops\n")
        r = self.run_script(extra_env={"DEFAULTS_FILE": str(bad)})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must not define secret names", r.stderr)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
