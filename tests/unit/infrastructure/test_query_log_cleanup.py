"""infrastructure/query-log-cleanup.sh: compress query logs older than a week, delete those older than a year.

Standard library only, so it also runs on a host without the project's dependencies:
    python3 -m unittest tests.unit.infrastructure.test_query_log_cleanup
"""
import gzip
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "infrastructure" / "query-log-cleanup.sh"
DAY = 86400


class QueryLogCleanupTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def make(self, name, age_days, content="{\"event_type\": \"query_processed\"}\n"):
        path = self.dir / name
        if name.endswith(".gz"):
            path.write_bytes(gzip.compress(content.encode()))
        else:
            path.write_text(content)
        stamp = time.time() - age_days * DAY - 3600  # an hour past the whole day, so "older than N days" is unambiguous
        os.utime(path, (stamp, stamp))
        return path

    def run_script(self, *args, **env):
        full = dict(os.environ, **{"LOG_DIR": str(self.dir), **env})
        return subprocess.run(["bash", str(SCRIPT), *args], env=full, capture_output=True, text=True)

    def test_a_file_older_than_a_week_is_compressed_and_keeps_its_content(self):
        self.make("2026-09-20.jsonl", 17, content="hello\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.dir / "2026-09-20.jsonl").exists())
        self.assertEqual(gzip.decompress((self.dir / "2026-09-20.jsonl.gz").read_bytes()), b"hello\n")

    def test_the_last_week_and_today_are_left_uncompressed_for_the_weekly_report(self):
        for age in (0, 1, 6):
            self.make(f"day-{age}.jsonl", age)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["day-0.jsonl", "day-1.jsonl", "day-6.jsonl"])

    def test_a_file_older_than_a_year_is_deleted_compressed_or_not(self):
        self.make("old.jsonl", 400)
        self.make("older.jsonl.gz", 500)
        self.make("keep.jsonl.gz", 300)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["keep.jsonl.gz"])

    def test_a_dry_run_says_what_it_would_do_and_changes_nothing(self):
        self.make("a.jsonl", 20)
        self.make("b.jsonl", 400)
        before = sorted(p.name for p in self.dir.iterdir())
        for r in (self.run_script("--dry-run"), self.run_script(DRY_RUN="1")):
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("would compress", r.stdout)
            self.assertIn("would delete", r.stdout)
            self.assertIn("1 to compress, 1 to delete", r.stdout)
            self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)

    def test_other_files_and_subdirectories_are_never_touched(self):
        self.make("notes.txt", 500)
        sub = self.dir / "sub"
        sub.mkdir()
        stamp = time.time() - 500 * DAY
        (sub / "x.jsonl").write_text("x")
        os.utime(sub / "x.jsonl", (stamp, stamp))
        self.assertEqual(self.run_script().returncode, 0)
        self.assertTrue((self.dir / "notes.txt").exists())
        self.assertTrue((sub / "x.jsonl").exists())

    def test_an_existing_gz_is_not_overwritten(self):
        self.make("d.jsonl", 20, content="new\n")
        self.make("d.jsonl.gz", 20, content="old\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already exists", r.stderr)
        self.assertEqual((self.dir / "d.jsonl").read_text(), "new\n")
        self.assertEqual(gzip.decompress((self.dir / "d.jsonl.gz").read_bytes()), b"old\n")

    def test_bad_settings_stop_before_touching_anything(self):
        self.make("old.jsonl", 400)
        for env in ({"COMPRESS_DAYS": "abc"}, {"COMPRESS_DAYS": "400", "DELETE_DAYS": "365"}, {"LOG_DIR": str(self.dir / "missing")}, {"LOG_DIR": "/"}):
            r = self.run_script(**env)
            self.assertEqual(r.returncode, 2, (env, r.stdout, r.stderr))
        self.assertTrue((self.dir / "old.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
