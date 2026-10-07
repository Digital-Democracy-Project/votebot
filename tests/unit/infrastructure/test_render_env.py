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

    def run_script(self, *args, extra_env=None, shared=False):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_SECRETS_DIR=str(self.secrets))
        env.pop("API_SOURCE_SECRET_ID", None)
        env.pop("VOTEBOT_SECRET_ID", None)
        if not shared:  # the layout with a secret of VoteBot's own (the default is the shared one)
            env.update(VOTEBOT_SECRET_ID="votebot/credentials", API_SOURCE_SECRET_ID="ddp-sync/credentials")
        env.update(extra_env or {})
        return subprocess.run(["bash", str(SCRIPT), "--out", str(self.out), *args], env=env, capture_output=True, text=True)

    def test_the_default_is_the_one_shared_secret(self):
        # decided 2026-10-07: VoteBot reads ddp-sync/credentials, and the single secret serves all four names
        shared_doc = {"api_key": "shared-api-SECRETVALUE-6", "openai_api_key": "shared-oa-SECRETVALUE-7",
                      "pinecone_api_key": "shared-pc-SECRETVALUE-8", "rds_openstates_api_key": "shared-v3-SECRETVALUE-4"}
        self.put("ddp-sync/credentials", shared_doc)
        r = self.run_script(shared=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.out.read_text()
        self.assertIn("API_KEY=shared-api-SECRETVALUE-6\n", text)
        self.assertIn("OPENAI_API_KEY=shared-oa-SECRETVALUE-7\n", text)
        self.assertIn("PINECONE_API_KEY=shared-pc-SECRETVALUE-8\n", text)
        self.assertIn("DDP_OPENSTATES_BEARER_TOKEN=shared-v3-SECRETVALUE-4\n", text)
        self.assertNotIn("SLACK_", text)  # Slack is off unless asked for

    def test_with_slack_writes_the_two_tokens_and_without_it_they_are_never_written(self):
        shared_doc = {"api_key": "a-SECRETVALUE-6", "openai_api_key": "o-SECRETVALUE-7", "pinecone_api_key": "p-SECRETVALUE-8",
                      "rds_openstates_api_key": "v-SECRETVALUE-4", "slack_bot_token": "xoxb-SECRETVALUE-10",
                      "slack_app_token": "xapp-SECRETVALUE-11"}
        self.put("ddp-sync/credentials", shared_doc)
        r = self.run_script("--with-slack", shared=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.out.read_text()
        self.assertIn("SLACK_BOT_TOKEN=xoxb-SECRETVALUE-10\n", text)
        self.assertIn("SLACK_APP_TOKEN=xapp-SECRETVALUE-11\n", text)
        self.assertNotIn("SECRETVALUE-1", r.stdout + r.stderr)
        self.out.unlink()
        self.assertEqual(self.run_script(shared=True).returncode, 0)
        self.assertNotIn("xoxb", self.out.read_text())  # tokens in the secret are not copied unless asked

    def test_with_slack_fails_before_touching_the_file_when_a_token_is_missing(self):
        self.put("ddp-sync/credentials", {"api_key": "a", "openai_api_key": "o", "pinecone_api_key": "p",
                                          "rds_openstates_api_key": "v", "slack_bot_token": "xoxb-SECRETVALUE-10"})
        self.out.write_text("SENTINEL=1\n")
        r = self.run_script("--with-slack", shared=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SLACK_APP_TOKEN", r.stderr)
        self.assertEqual(self.out.read_text(), "SENTINEL=1\n")

    def test_the_shipped_defaults_hold_no_secret_names_and_no_slack(self):
        names = [line.split("=", 1)[0] for line in (REPO / "infrastructure/docker/prod.env.defaults").read_text().splitlines()
                 if "=" in line and not line.lstrip().startswith("#")]
        for secret in ["API_KEY", "OPENAI_API_KEY", "PINECONE_API_KEY", "DDP_OPENSTATES_BEARER_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"]:
            self.assertNotIn(secret, names)

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

    def test_a_value_with_a_character_an_env_file_cannot_carry_losslessly_is_refused_by_name(self):
        for bad in ["has space", 'has"quote', "has$dollar", "has#hash", "has\\backslash", "has\nnewline", "tab\there", "ünïcode"]:
            self.out.write_text("SENTINEL=1\n")
            self.put("votebot/credentials", dict(VOTEBOT_SECRET, openai_api_key=bad))
            r = self.run_script()
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("OPENAI_API_KEY", r.stderr)
            self.assertNotIn(bad, r.stdout + r.stderr)  # the name is reported, never the value
            self.assertEqual(self.out.read_text(), "SENTINEL=1\n")

    def test_the_characters_real_api_keys_use_are_written_as_they_are(self):
        keys = {"api_key": "Ab-1_2.3~4", "openai_api_key": "sk-proj-AbC_123-xyz", "pinecone_api_key": "pcsk_AbC+/=:9@z"}
        self.put("votebot/credentials", dict(VOTEBOT_SECRET, **keys))
        self.assertEqual(self.run_script().returncode, 0)
        text = self.out.read_text()
        self.assertIn("PINECONE_API_KEY=pcsk_AbC+/=:9@z\n", text)
        self.assertIn("OPENAI_API_KEY=sk-proj-AbC_123-xyz\n", text)

    def test_an_invalid_secret_never_echoes_its_content(self):
        (self.secrets / "votebot_credentials.json").write_text("not json SECRETVALUE-12")
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a JSON object", r.stderr)
        self.assertNotIn("SECRETVALUE-12", r.stdout + r.stderr)
        self.assertFalse(self.out.exists())

    def test_a_traced_run_does_not_show_any_value(self):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_SECRETS_DIR=str(self.secrets),
                   VOTEBOT_SECRET_ID="votebot/credentials", API_SOURCE_SECRET_ID="ddp-sync/credentials")
        r = subprocess.run(["bash", "-x", str(SCRIPT), "--out", str(self.out)], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for v in ALL_VALUES:
            self.assertNotIn(v, r.stdout + r.stderr)  # secrets never pass through bash, so a trace cannot print them

    def test_a_failed_replace_leaves_no_temp_file_and_nothing_changed(self):
        self.out.mkdir()  # os.replace of a file onto a directory fails after the temp file was written
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([p.name for p in self.out.parent.iterdir() if p.name.startswith(".env.")], [])
        self.assertTrue(self.out.is_dir())

    def test_defaults_without_a_trailing_newline_do_not_join_the_first_secret(self):
        no_newline = Path(self.tmp.name) / "nonl.defaults"
        no_newline.write_text("ENVIRONMENT=production")
        self.assertEqual(self.run_script(extra_env={"DEFAULTS_FILE": str(no_newline)}).returncode, 0)
        lines = self.out.read_text().splitlines()
        self.assertIn("ENVIRONMENT=production", lines)
        self.assertIn("API_KEY=vb-api-SECRETVALUE-1", lines)

    def test_defaults_file_may_not_define_a_secret_name(self):
        bad = Path(self.tmp.name) / "bad.defaults"
        bad.write_text("OPENAI_API_KEY=oops\n")
        r = self.run_script(extra_env={"DEFAULTS_FILE": str(bad)})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must not define secret names", r.stderr)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
