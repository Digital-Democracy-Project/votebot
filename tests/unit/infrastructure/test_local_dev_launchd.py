"""The local dev VoteBot LaunchDaemon: its plist and start script (static checks; nothing is started).

An admin installs the plist with sudo (agentsmith has none), so a mistake in it is found only at the next
reboot. These checks catch the mistakes that do not need launchd: a plist that does not parse, a missing key the
other daemons have, a script that is not executable or has a syntax error.

Standard library only:  python3 -m unittest tests.unit.infrastructure.test_local_dev_launchd
"""
import os
import plistlib
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PLIST = REPO / "infrastructure" / "launchd" / "com.ddp.votebot-dev.plist"
SCRIPT = REPO / "infrastructure" / "start-votebot-dev.sh"


class LocalDevLaunchdTest(unittest.TestCase):
    def setUp(self):
        self.plist = plistlib.loads(PLIST.read_bytes())

    def test_it_is_a_system_daemon_run_as_agentsmith_like_the_other_dev_services(self):
        self.assertEqual(self.plist["Label"], "com.ddp.votebot-dev")
        self.assertEqual(self.plist["UserName"], "agentsmith")  # keeps access to the repo, .env and the Colima socket
        self.assertTrue(self.plist["RunAtLoad"])  # starts at boot
        self.assertTrue(self.plist["KeepAlive"])  # and again when it exits (the script exits when a dependency is down)
        self.assertGreaterEqual(self.plist["ThrottleInterval"], 10)
        env = self.plist["EnvironmentVariables"]
        self.assertEqual(env["HOME"], "/Users/agentsmith")  # a daemon has no HOME: the script finds docker.sock under it
        self.assertIn("/opt/homebrew/bin", env["PATH"])  # docker, curl
        self.assertEqual(self.plist["StandardOutPath"], self.plist["StandardErrorPath"])

    def test_it_runs_the_script_in_this_repo_from_the_repo_root(self):
        shell, script = self.plist["ProgramArguments"]
        self.assertEqual(shell, "/bin/bash")
        self.assertTrue(script.endswith("infrastructure/start-votebot-dev.sh"))
        self.assertTrue(self.plist["WorkingDirectory"].endswith("/votebot"))  # VoteBot reads .env from here
        self.assertTrue(script.startswith(self.plist["WorkingDirectory"]))
        self.assertTrue(self.plist["StandardOutPath"].startswith(self.plist["WorkingDirectory"] + "/logs/"))

    def test_the_script_is_executable_has_a_bash_shebang_and_parses(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self.assertEqual(SCRIPT.read_text().splitlines()[0], "#!/usr/bin/env bash")
        done = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_script_binds_localhost_only_on_the_registered_port_and_never_uses_the_cams_redis(self):
        text = SCRIPT.read_text()
        self.assertIn("--host 127.0.0.1", text)
        self.assertIn('PORT="${PORT:-8010}"', text)  # 8000 is CAMS on this machine; 8010 is in the ddp-infra port registry
        self.assertIn('REDIS_PORT="${REDIS_PORT:-6380}"', text)  # 6379 is the CAMS Redis
        self.assertIn('"127.0.0.1:${REDIS_PORT}:6379"', text)  # a Redis without a password is never published on the network


if __name__ == "__main__":
    unittest.main()
