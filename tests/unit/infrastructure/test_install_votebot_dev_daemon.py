"""infrastructure/install-votebot-dev-daemon.sh: the admin script that installs the dev VoteBot's LaunchDaemon (VOTEBOT-32).

It runs as root on the real machine, which a test cannot be, so launchctl, lsof and curl are stand-ins on PATH in a
scratch project folder, and the plist directory is a temp directory. macOS only (the script uses stat -f, PlistBuddy).

    python3 -m unittest tests.unit.infrastructure.test_install_votebot_dev_daemon
"""
import getpass
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "infrastructure" / "install-votebot-dev-daemon.sh"
PLIST = REPO / "infrastructure" / "launchd" / "com.ddp.votebot-dev.plist"

STUBS = {
    # `launchctl print` succeeds only while the service is "loaded"; bootstrap loads it (and makes the health check pass)
    "launchctl": """#!/bin/bash
echo "launchctl $*" >> "$STUB_DIR/calls.log"
case "$1" in
  print) [ -f "$STUB_DIR/loaded" ] && { echo "state = running"; echo "pid = 4242"; exit 0; } || exit 113 ;;
  bootstrap) touch "$STUB_DIR/loaded"; [ -f "$STUB_DIR/never_healthy" ] || touch "$STUB_DIR/healthy"; exit 0 ;;
  bootout) rm -f "$STUB_DIR/loaded" "$STUB_DIR/healthy"; exit 0 ;;
esac
""",
    "curl": """#!/bin/bash
[ -f "$STUB_DIR/healthy" ] && { echo '{"status":"healthy","dependencies":{"redis":"healthy"}}'; exit 0; } || exit 22
""",
    # the pid in $STUB_DIR/listener is "listening on the port" while that process is alive and not a zombie
    "lsof": """#!/bin/bash
pid=$(cat "$STUB_DIR/listener" 2>/dev/null)
[ -n "$pid" ] && ps -o stat= -p "$pid" 2>/dev/null | grep -qv Z && echo "$pid"
exit 0
""",
}


@unittest.skipUnless(sys.platform == "darwin", "the script is macOS-only (stat -f, PlistBuddy, launchctl)")
class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.project = self.tmp / "votebot"
        self.stub = self.tmp / "stub"
        self.dest = self.tmp / "LaunchDaemons"
        for d in (self.project / "infrastructure" / "launchd", self.project / ".venv" / "bin", self.project / "logs", self.stub, self.dest):
            d.mkdir(parents=True)
        shutil.copy(SCRIPT, self.project / "infrastructure" / "install-votebot-dev-daemon.sh")
        start = self.project / "infrastructure" / "start-votebot-dev.sh"
        start.write_text("#!/bin/bash\nexit 0\n")
        start.chmod(0o755)
        (self.project / ".env").write_text("API_KEY=x\n")
        py = self.project / ".venv" / "bin" / "python"
        py.write_text("#!/bin/bash\nexit 0\n")
        py.chmod(0o755)
        plist = self.project / "infrastructure" / "launchd" / "com.ddp.votebot-dev.plist"
        shutil.copy(PLIST, plist)  # point it at the scratch start script, the way the real one points at the repo's
        subprocess.run(["/usr/libexec/PlistBuddy", "-c", f"Set :ProgramArguments:1 {start}", str(plist)], check=True)
        for name, body in STUBS.items():
            f = self.stub / name
            f.write_text(body)
            f.chmod(0o755)
        self.procs = []
        self.addCleanup(lambda: [p.kill() for p in self.procs if p.poll() is None])

    def run_script(self, *args, test_mode=True, **extra):
        env = dict(os.environ, PATH=f"{self.stub}:{os.environ['PATH']}", STUB_DIR=str(self.stub), DEST_DIR=str(self.dest),
                   SERVICE_USER=getpass.getuser(), WAIT_SECONDS="3", **extra)
        if test_mode:
            env["VOTEBOT_INSTALL_TEST"] = "1"
        else:
            env.pop("VOTEBOT_INSTALL_TEST", None)
        return subprocess.run(["bash", str(self.project / "infrastructure" / "install-votebot-dev-daemon.sh"), *args],
                              env=env, capture_output=True, text=True)

    def calls(self):
        log = self.stub / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def listener(self, argv0):
        """A process that looks like it holds the port: `ps` shows argv0 as its command."""
        proc = subprocess.Popen(["bash", "-c", f'exec -a "{argv0}" sleep 60'])
        self.procs.append(proc)
        time.sleep(0.3)
        (self.stub / "listener").write_text(str(proc.pid))
        return proc

    def test_a_fresh_install_copies_the_plist_loads_it_and_waits_for_health(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        installed = self.dest / "com.ddp.votebot-dev.plist"
        self.assertTrue(installed.exists())
        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o644)
        self.assertTrue(any(c.startswith("launchctl bootstrap system ") and c.endswith("com.ddp.votebot-dev.plist") for c in self.calls()))
        self.assertIn("healthy", r.stdout)

    def test_running_it_again_unloads_the_loaded_copy_before_loading(self):
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.run_script().returncode, 0)
        verbs = [c.split()[1] for c in self.calls() if c.split()[1] in ("bootstrap", "bootout")]
        self.assertEqual(verbs, ["bootstrap", "bootout", "bootstrap"])  # never two loads in a row (that fails on a real Mac)

    def test_a_hand_started_votebot_on_the_port_is_stopped_first(self):
        proc = self.listener("uvicorn votebot.main:app")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.returncode)
        self.assertIn("stopping hand-started VoteBot", r.stdout)

    def test_something_else_on_the_port_stops_the_script_before_any_change(self):
        proc = self.listener("python3 -m http.server")
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn("not VoteBot", r.stderr)
        self.assertIsNone(proc.poll())  # not killed
        self.assertFalse((self.dest / "com.ddp.votebot-dev.plist").exists())
        self.assertEqual(self.calls(), [])

    def test_never_healthy_exits_1_with_a_hint_and_leaves_the_daemon_installed(self):
        (self.stub / "never_healthy").touch()
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn("not healthy", r.stderr)
        self.assertIn("--uninstall", r.stderr)
        self.assertTrue((self.dest / "com.ddp.votebot-dev.plist").exists())

    def test_check_changes_nothing(self):
        proc = self.listener("uvicorn votebot.main:app")
        r = self.run_script("--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nothing was changed", r.stdout)
        self.assertIsNone(proc.poll())  # the hand-started copy is left running
        self.assertFalse((self.dest / "com.ddp.votebot-dev.plist").exists())
        # it may ask launchctl whether the service is loaded (print only reads); it must never load or unload
        self.assertEqual([c for c in self.calls() if c.split()[1] in ("bootstrap", "bootout")], [])

    def test_uninstall_unloads_and_removes_the_plist(self):
        self.assertEqual(self.run_script().returncode, 0)
        r = self.run_script("--uninstall")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.dest / "com.ddp.votebot-dev.plist").exists())
        self.assertIn("launchctl bootout system/com.ddp.votebot-dev", self.calls())

    def test_a_missing_env_file_fails_before_any_change(self):
        (self.project / ".env").unlink()
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn(".env is missing", r.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.dest / "com.ddp.votebot-dev.plist").exists())

    @unittest.skipIf(os.geteuid() == 0, "root passes the root check")
    def test_without_sudo_an_install_is_refused(self):
        r = self.run_script(test_mode=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("sudo", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_the_real_script_and_plist_are_executable_and_valid(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self.assertEqual(SCRIPT.read_text().splitlines()[0], "#!/usr/bin/env bash")
        self.assertEqual(subprocess.run(["bash", "-n", str(SCRIPT)]).returncode, 0)
        self.assertEqual(subprocess.run(["plutil", "-lint", str(PLIST)], capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
