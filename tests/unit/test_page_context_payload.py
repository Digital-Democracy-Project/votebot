"""VOTEBOT-8: the widget's page_context payload reaches retrieval with ocd_bill_id intact.

The widget stores whatever /content/resolve returned and sends it back on every message, and the
websocket route copies a fixed list of keys into PageContext. A key missing from that list is
silently dropped, so retrieval would never see the id.
"""

from votebot.api.routes.websocket import _page_context_from_payload, _page_identity

BILL = "a3f7c0d1-1111-4222-8333-444455556666"


class TestPageContextFromPayload:
    def test_ocd_bill_id_survives(self):
        # Exactly what /content/resolve returns for a ddp-next bill URL.
        payload = {
            "type": "bill",
            "id": "HB 123",
            "ocd_bill_id": BILL,
            "gov_id": "HB 123",
            "jurisdiction": "FL",
            "session": "2026",
            "url": "https://digitaldemocracyproject.org/bills/fl/2026/HB%20123",
            "ddp_url": "https://digitaldemocracyproject.org/bills/fl/2026/HB%20123",
        }
        ctx = _page_context_from_payload(payload)
        assert (ctx.type, ctx.ocd_bill_id, ctx.jurisdiction, ctx.session, ctx.id) == (
            "bill", BILL, "FL", "2026", "HB 123",
        )

    def test_webflow_payload_is_unchanged(self):
        ctx = _page_context_from_payload(
            {"type": "bill", "webflow_id": "wf1", "slug": "a-bill", "session-code": "119"}
        )
        assert (ctx.webflow_id, ctx.slug, ctx.session, ctx.ocd_bill_id) == ("wf1", "a-bill", "119", None)

    def test_empty_payload_is_a_general_context(self):
        assert _page_context_from_payload({}).type == "general"


class TestPageIdentity:
    def test_ocd_bill_id_distinguishes_bills_that_share_a_gov_id(self):
        # "HB 1" exists in every state: the id alone would not detect a change of page.
        fl = _page_identity({"id": "HB 1", "ocd_bill_id": "fl-uuid"})
        va = _page_identity({"id": "HB 1", "ocd_bill_id": "va-uuid"})
        assert fl != va

    def test_legacy_order_is_preserved(self):
        assert _page_identity({"slug": "s", "webflow_id": "w", "ocd_bill_id": "o", "id": "i"}) == "s"
        assert _page_identity({"webflow_id": "w", "ocd_bill_id": "o", "id": "i"}) == "w"
        assert _page_identity({"id": "i"}) == "i"
