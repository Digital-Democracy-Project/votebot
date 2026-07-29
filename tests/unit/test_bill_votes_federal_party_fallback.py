"""Tests for the federal vote-party fallback matching fix.

OpenStates' federal vote records carry disambiguated Clerk/LIS roll-call names
("Scott (VA)", "Green, Al (TX)") that never exact-match BillVotesService's
per-jurisdiction full-name-only party lookup. These tests cover the fix: folding
diacritics for the existing lookup, and a second-tier fallback through
FederalLegislatorCache's name-variant index (extended with a state-only variant
matching that exact format) for names the full-name lookup still misses.
"""

from unittest.mock import MagicMock, patch

from votebot.services.bill_votes import (
    BillVotesService,
    _federal_cache_lookup_candidates,
    _normalize_name_key,
)
from votebot.utils.federal_legislator_cache import FederalLegislatorCache


class TestNormalizeNameKey:
    def test_folds_diacritics(self):
        assert _normalize_name_key("Velázquez") == _normalize_name_key("Velazquez")

    def test_lowercases_and_strips(self):
        assert _normalize_name_key("  Bobby Scott  ") == "bobby scott"


class TestFederalCacheLookupCandidates:
    def test_bare_state_suffix_passes_through(self):
        assert _federal_cache_lookup_candidates("Scott (VA)") == ["Scott (VA)"]

    def test_last_first_state_strips_first_name(self):
        candidates = _federal_cache_lookup_candidates("Green, Al (TX)")
        assert candidates == ["Green, Al (TX)", "Green (TX)"]

    def test_no_comma_no_extra_candidate(self):
        assert _federal_cache_lookup_candidates("Velazquez") == ["Velazquez"]


class TestFederalLegislatorCacheStateOnlyVariant:
    def test_generates_state_only_variant(self):
        cache = FederalLegislatorCache.__new__(FederalLegislatorCache)
        variants = cache._generate_name_variants("Robert C. Scott", "D", "VA")
        assert "Scott (VA)" in variants
        # Existing variants still present
        assert "Scott (D-VA)" in variants
        assert "Scott (D)" in variants
        assert "Scott" in variants

    def test_state_only_variant_disambiguates_same_surname(self):
        cache = FederalLegislatorCache.__new__(FederalLegislatorCache)
        cache._loaded = True
        cache._cache = {
            "ocd-person/scott-va": {"name": "Robert C. Scott", "party": "Democratic", "state": "VA"},
            "ocd-person/scott-ga": {"name": "Austin Scott", "party": "Republican", "state": "GA"},
        }
        cache._build_name_index()

        assert cache.lookup("Scott (VA)") == "ocd-person/scott-va"
        assert cache.lookup("Scott (GA)") == "ocd-person/scott-ga"

    def test_lookup_folds_diacritics(self):
        cache = FederalLegislatorCache.__new__(FederalLegislatorCache)
        cache._loaded = True
        cache._cache = {
            "ocd-person/velazquez": {"name": "Nydia Velázquez", "party": "Democratic", "state": "NY"},
        }
        cache._build_name_index()

        assert cache.lookup("Velazquez") == "ocd-person/velazquez"
        assert cache.lookup("Velazquez (NY)") == "ocd-person/velazquez"


class TestParseVotesFederalFallback:
    def _votes_payload(self, voter_name: str) -> list[dict]:
        return [
            {
                "id": "ocd-vote/1",
                "motion_text": "On Passage",
                "result": "pass",
                "start_date": "2026-06-04",
                "organization": {"classification": "lower"},
                "counts": [{"option": "yes", "value": 1}],
                "votes": [{"voter_name": voter_name, "option": "yes", "voter": {}}],
            }
        ]

    def test_full_name_miss_falls_through_to_federal_cache(self):
        service = BillVotesService.__new__(BillVotesService)
        mock_cache = MagicMock()
        mock_cache.lookup_with_info.side_effect = lambda name: (
            {"party": "Democratic", "person_id": "ocd-person/scott-va"}
            if name == "Scott (VA)"
            else None
        )

        with patch(
            "votebot.services.bill_votes.get_federal_cache", return_value=mock_cache
        ):
            votes = service._parse_votes(
                self._votes_payload("Scott (VA)"),
                legislator_parties={},  # full-name lookup has nothing for "Scott (VA)"
                jurisdiction="us",
            )

        assert votes[0].votes[0].party == "Democratic"
        assert votes[0].votes[0].legislator_id == "ocd-person/scott-va"

    def test_last_first_state_form_resolves_via_stripped_candidate(self):
        service = BillVotesService.__new__(BillVotesService)
        mock_cache = MagicMock()
        mock_cache.lookup_with_info.side_effect = lambda name: (
            {"party": "Democratic", "person_id": "ocd-person/green-tx"}
            if name == "Green (TX)"
            else None
        )

        with patch(
            "votebot.services.bill_votes.get_federal_cache", return_value=mock_cache
        ):
            votes = service._parse_votes(
                self._votes_payload("Green, Al (TX)"),
                legislator_parties={},
                jurisdiction="us",
            )

        assert votes[0].votes[0].party == "Democratic"

    def test_federal_cache_not_consulted_for_state_jurisdictions(self):
        service = BillVotesService.__new__(BillVotesService)
        mock_cache = MagicMock()

        with patch(
            "votebot.services.bill_votes.get_federal_cache", return_value=mock_cache
        ):
            votes = service._parse_votes(
                self._votes_payload("Smith (VA)"),
                legislator_parties={},
                jurisdiction="va",
            )

        mock_cache.lookup_with_info.assert_not_called()
        assert votes[0].votes[0].party == ""

    def test_voter_object_party_takes_priority_over_fallback(self):
        service = BillVotesService.__new__(BillVotesService)
        payload = [
            {
                "id": "ocd-vote/1",
                "motion_text": "On Passage",
                "result": "pass",
                "start_date": "2026-06-04",
                "organization": {"classification": "lower"},
                "counts": [{"option": "yes", "value": 1}],
                "votes": [
                    {
                        "voter_name": "Scott (VA)",
                        "option": "yes",
                        "voter": {"id": "ocd-person/scott-va", "party": "Democratic"},
                    }
                ],
            }
        ]
        mock_cache = MagicMock()

        with patch(
            "votebot.services.bill_votes.get_federal_cache", return_value=mock_cache
        ):
            votes = service._parse_votes(payload, legislator_parties={}, jurisdiction="us")

        assert votes[0].votes[0].party == "Democratic"
        mock_cache.lookup_with_info.assert_not_called()
