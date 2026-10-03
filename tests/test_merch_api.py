"""商品化合作管理接口的 HTTP 路由测试。"""

from __future__ import annotations

import json
import unittest

from creative_program_foundation.api import route
from creative_program_foundation.merch_service import MerchService
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database

from tests.test_merch_service import World


def qs(**kwargs):
    return {key: [str(value)] for key, value in kwargs.items() if value is not None}


class MerchApiTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.service = DomainService(self.w.db, self.w.clock)

    def tearDown(self):
        self.w.close()

    def post(self, path, body, actor="op"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def get(self, path, actor="op", query=None):
        suffix = ""
        if query:
            suffix = "?" + "&".join(f"{k}={v[0]}" for k, v in query.items())
        return route(self.service, "GET", path + suffix, None, {"X-Actor-Id": actor})

    def _signed_offer(self):
        offer_id = self.w.offer(key="api")
        self.post("/offers/sign", {"request_id": "sig-t", "offer_id": offer_id,
                                   "party": "team"}, actor="u_sign")
        self.post("/offers/sign", {"request_id": "sig-p", "offer_id": offer_id,
                                   "party": "partner"}, actor="prep1")
        status, body = self.post("/offers/accept", {"request_id": "acc",
                                                    "offer_id": offer_id})
        self.assertEqual(201, status)
        return offer_id, body["resource_id"]

    def test_full_lifecycle_routes(self):
        offer_id, contract_id = self._signed_offer()
        status, body = self.get(f"/offers/{offer_id}")
        self.assertEqual(200, status)
        self.assertEqual("accepted", body["status"])
        status, body = self.get("/works/work1/occupancy")
        self.assertEqual(200, status)
        self.assertEqual(1, len(body["occupancy"]))
        status, body = self.post("/samples", {"request_id": "sm", "contract_id": contract_id,
                                              "approved": True}, actor="prep1")
        self.assertEqual(201, status)
        status, body = self.post("/deliveries", {"request_id": "dl", "contract_id": contract_id,
                                                 "milestone_code": "delivery",
                                                 "quantity": "1000"}, actor="prep1")
        self.assertEqual(201, status)
        status, body = self.post("/settlements", {"request_id": "st", "contract_id": contract_id,
                                                  "gross_revenue": "50000"})
        self.assertEqual(201, status)
        settlement_id = body["resource_id"]
        status, body = self.get(f"/settlements/{settlement_id}")
        self.assertEqual(200, status)
        self.assertEqual("9400.00", body["team_amount"])

    def test_duplicate_signature_callback_replays_without_second_signature(self):
        offer_id = self.w.offer(key="dup")
        first = self.post("/offers/sign", {"request_id": "same-sig", "offer_id": offer_id,
                                           "party": "team"}, actor="u_sign")
        second = self.post("/offers/sign", {"request_id": "same-sig", "offer_id": offer_id,
                                            "party": "team"}, actor="u_sign")
        self.assertEqual(201, first[0])
        self.assertEqual(200, second[0])
        self.assertTrue(second[1]["replayed"])
        count = self.w.db.connection.execute(
            "SELECT COUNT(*) AS c FROM offer_signatures WHERE offer_id=?", (offer_id,)
        ).fetchone()["c"]
        self.assertEqual(1, count)

    def test_duplicate_settlement_callback_does_not_add_entries(self):
        _, contract_id = self._signed_offer()
        self.post("/samples", {"request_id": "sm", "contract_id": contract_id,
                               "approved": True}, actor="prep1")
        self.post("/deliveries", {"request_id": "dl", "contract_id": contract_id,
                                  "milestone_code": "delivery", "quantity": "1000"},
                  actor="prep1")
        body = {"request_id": "once", "contract_id": contract_id, "gross_revenue": "50000"}
        self.post("/settlements", body)
        self.post("/settlements", body)
        settlements = self.w.db.connection.execute(
            "SELECT COUNT(*) AS c FROM settlements").fetchone()["c"]
        entries = self.w.db.connection.execute(
            "SELECT COUNT(*) AS c FROM settlement_entries").fetchone()["c"]
        self.assertEqual((1, 3), (settlements, entries))

    def test_conflicting_exclusivity_returns_409(self):
        first = self.w.offer(key="a")
        self.post("/offers/reserve", {"request_id": "r1", "offer_id": first,
                                      "reserve_until": "2026-10-09T00:00:00Z"})
        second = self.w.offer(key="b", partner="partner2", rep="prep2")
        status, body = self.post("/offers/reserve",
                                 {"request_id": "r2", "offer_id": second,
                                  "reserve_until": "2026-10-08T00:00:00Z"}, actor="prep2")
        self.assertEqual(409, status)
        self.assertEqual("conflict", body["error"])

    def test_partner_visibility_is_minimized(self):
        _, contract_id = self._signed_offer()
        self.post("/samples", {"request_id": "sm", "contract_id": contract_id,
                               "approved": True}, actor="prep1")
        self.post("/deliveries", {"request_id": "dl", "contract_id": contract_id,
                                  "milestone_code": "delivery", "quantity": "1000"},
                  actor="prep1")
        status, body = self.post("/settlements", {"request_id": "st",
                                                  "contract_id": contract_id,
                                                  "gross_revenue": "50000"})
        settlement_id = body["resource_id"]
        status, partner_body = self.get(f"/settlements/{settlement_id}", actor="prep1")
        self.assertEqual(200, status)
        self.assertTrue(all(e["entry_kind"] != "team_member"
                            for e in partner_body["entries"]))
        status, body = self.get(f"/contracts/{contract_id}", actor="prep2")
        self.assertEqual(403, status)

    def test_unrelated_party_cannot_read_contract_subresources(self):
        _, contract_id = self._signed_offer()
        for suffix in ("facts", "deliveries", "settlements"):
            status, _ = self.get(f"/contracts/{contract_id}/{suffix}", actor="prep2")
            self.assertEqual(403, status)

    def test_settlement_explain_hidden_from_partner(self):
        _, contract_id = self._signed_offer()
        self.post("/samples", {"request_id": "sm", "contract_id": contract_id,
                               "approved": True}, actor="prep1")
        self.post("/deliveries", {"request_id": "dl", "contract_id": contract_id,
                                  "milestone_code": "delivery", "quantity": "1000"},
                  actor="prep1")
        status, body = self.post("/settlements", {"request_id": "st",
                                                  "contract_id": contract_id,
                                                  "gross_revenue": "50000"})
        settlement_id = body["resource_id"]
        status, _ = self.get(f"/settlements/{settlement_id}/explain", actor="prep1")
        self.assertEqual(403, status)
        status, body = self.get(f"/settlements/{settlement_id}/explain", actor="u_sign")
        self.assertEqual(200, status)
        self.assertTrue(body["cost_hash_matches"])

    def test_occupancy_restricted_to_team_and_platform(self):
        self._signed_offer()
        status, body = self.get("/works/work1/occupancy", actor="u_design")
        self.assertEqual(200, status)
        self.assertEqual(1, len(body["occupancy"]))
        status, _ = self.get("/works/work1/occupancy", actor="prep2")
        self.assertEqual(403, status)

    def test_unknown_route_returns_404(self):
        status, body = self.get("/nope")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", body["error"])


if __name__ == "__main__":
    unittest.main()
