"""VOTEBOT-10: which version of a bill is current, asked of api-v3 and never stored on vectors."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from votebot.config import Settings
from votebot.services import bill_versions
from votebot.services.bill_versions import BillVersion, BillVersionService, current_version

BILL = "a3f7c0d1-1111-4222-8333-444455556666"
ROOT = "https://ddp.test/openstates"


def _v(doc, stage="amendment", note="Substitute", date="2026-02-01", ordinal=1):
    return BillVersion(document_id=doc, note=note, date=date, stage=stage, ordinal=ordinal)


def _api_version(doc, stage, ordinal, note="X", date="2026-01-01"):
    """One entry of api-v3's `versions` (single-bill detail, include=versions)."""
    return {"note": note, "date": date, "archived_document_id": doc, "version_stage": stage, "version_ordinal": ordinal}


def _service(replica: bool = True) -> BillVersionService:
    settings = Settings(
        use_ddp_openstates_replica=replica,
        ddp_openstates_api_root=ROOT,
        ddp_openstates_bearer_token=SecretStr("tok"),
        _env_file=None,
    )
    return BillVersionService(settings)


class TestCurrentVersion:
    def test_the_latest_classifiable_version_with_a_document_is_current(self):
        versions = [_v("1", "introduced", ordinal=0), _v("2", "chamber_passage"), _v("3", "final_passage", ordinal=2)]
        assert current_version(versions).document_id == "3"

    def test_a_stage_unknown_version_is_never_current_even_when_it_is_last(self):
        versions = [_v("1", "introduced", ordinal=0), _v("2", "unknown", ordinal=None)]
        assert current_version(versions).document_id == "1"

    def test_a_latest_version_not_archived_yet_leaves_no_current_version(self):
        # Its text is not in the index, so filtering on it would return nothing, and promoting the
        # previous version would label stale text "current" while a newer one exists.
        assert current_version([_v("1", "introduced", ordinal=0), _v(None, "amendment")]) is None

    def test_an_unarchived_latest_classifiable_version_is_not_skipped_in_favour_of_an_older_one(self):
        versions = [_v("1", "introduced", ordinal=0), _v(None, "amendment"), _v("9", "unknown", ordinal=None)]
        assert current_version(versions) is None  # the unknown one is skipped; the unarchived one is the latest

    def test_an_older_unarchived_version_does_not_matter(self):
        versions = [_v(None, "introduced", ordinal=0), _v("2", "amendment")]
        assert current_version(versions).document_id == "2"

    def test_no_usable_version_means_no_current_version(self):
        assert current_version([_v("1", "unknown", ordinal=None)]) is None
        assert current_version([]) is None
        assert current_version(None) is None

    def test_api_order_is_trusted_not_re_sorted(self):
        # api-v3 already returns latest last; dates here would sort the other way and must not matter.
        versions = [_v("1", "introduced", date="2026-09-09", ordinal=0), _v("2", "amendment", date="2026-01-01")]
        assert current_version(versions).document_id == "2"


class TestGetVersions:
    async def test_skipped_unless_openstates_is_routed_to_the_ddp_replica(self):
        svc = _service(replica=False)
        svc._fetch = AsyncMock(return_value={"versions": []})
        assert await svc.get_versions(BILL) is None
        svc._fetch.assert_not_called()  # the public API has none of the DDP version fields

    async def test_parses_api_v3_versions_in_order(self):
        svc = _service()
        svc._fetch = AsyncMock(
            return_value={
                "versions": [
                    _api_version(101, "introduced", 0, "Introduced", "2026-01-10"),
                    _api_version(None, "unknown", None, "Odd note"),
                    {"note": "Bare"},  # an entry missing every DDP field must not break parsing
                ]
            }
        )
        got = await svc.get_versions(BILL)
        assert [(v.document_id, v.stage, v.ordinal) for v in got] == [
            ("101", "introduced", 0),
            (None, "unknown", None),
            (None, "unknown", None),
        ]
        assert got[0].note == "Introduced" and got[0].date == "2026-01-10"

    async def test_a_failed_lookup_is_none_and_is_retried_next_time(self):
        svc = _service()
        svc._fetch = AsyncMock(side_effect=[None, {"versions": [_api_version(5, "introduced", 0)]}])
        assert await svc.get_versions(BILL) is None
        assert (await svc.get_versions(BILL))[0].document_id == "5"  # None was not cached

    async def test_cached_briefly_then_refreshed(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(bill_versions.time, "monotonic", lambda: clock["now"])
        svc = _service()
        svc._fetch = AsyncMock(return_value={"versions": [_api_version(5, "introduced", 0)]})

        await svc.get_versions(BILL)
        clock["now"] += bill_versions.CACHE_TTL_SECONDS - 1
        await svc.get_versions(BILL)
        assert svc._fetch.await_count == 1  # a new version can lag by at most the TTL

        clock["now"] += 2
        await svc.get_versions(BILL)
        assert svc._fetch.await_count == 2


class TestFetch:
    async def test_asks_the_ddp_proxy_for_the_bill_by_ocd_id_with_versions(self, monkeypatch):
        seen = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"versions": []}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None, params=None):
                seen.update(url=url, headers=headers, params=params)
                return FakeResponse()

        monkeypatch.setattr(bill_versions.httpx, "AsyncClient", FakeClient)
        assert await _service()._fetch(BILL) == {"versions": []}
        assert seen["url"] == f"{ROOT}/bills/ocd-bill/{BILL}"
        assert seen["params"] == [("include", "versions")]
        assert seen["headers"]["Authorization"] == "Bearer tok"

    async def test_any_failure_degrades_to_none(self, monkeypatch):
        class Boom:
            def __init__(self, **kwargs):
                raise RuntimeError("down")

        monkeypatch.setattr(bill_versions.httpx, "AsyncClient", Boom)
        assert await _service()._fetch(BILL) is None


@pytest.mark.parametrize("stage", ["introduced", "amendment", "chamber_passage", "final_passage", "enacted"])
def test_every_api_v3_stage_label_can_be_current(stage):
    assert current_version([_v("9", stage)]).document_id == "9"
