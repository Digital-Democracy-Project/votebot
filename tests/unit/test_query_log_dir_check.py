"""A query-log directory the app user cannot write is reported at startup, not discovered later."""

import os
import stat

import pytest

from votebot import main


@pytest.fixture
def settings(monkeypatch, tmp_path):
    monkeypatch.setattr(main.settings, "query_log_enabled", True)
    monkeypatch.setattr(main.settings, "query_log_dir", str(tmp_path / "logs" / "queries"))
    return main.settings


def test_a_writable_directory_is_fine_and_created_when_missing(settings, tmp_path):
    assert main.check_query_log_dir() is True
    assert (tmp_path / "logs" / "queries").is_dir()
    assert list((tmp_path / "logs" / "queries").iterdir()) == []  # the probe file is removed


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_directory_the_user_cannot_write_is_reported(settings, tmp_path, capsys):
    target = tmp_path / "logs" / "queries"
    target.mkdir(parents=True)
    target.chmod(stat.S_IRUSR | stat.S_IXUSR)  # read-only, like a root-owned bind mount seen by uid 1000
    try:
        assert main.check_query_log_dir() is False
        assert "NOT writable" in capsys.readouterr().out
    finally:
        target.chmod(stat.S_IRWXU)


def test_nothing_is_checked_when_query_logging_is_off(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(main.settings, "query_log_enabled", False)
    assert main.check_query_log_dir() is True
    assert not (tmp_path / "logs").exists()
