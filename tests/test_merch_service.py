"""商品化合作管理服务的端到端单元测试。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError)
from creative_program_foundation.merch_service import MerchService
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database

START = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)


def terms(**overrides):
    base = {
        "currency": "CNY", "exclusive": True, "territories": ["CN"],
        "channels": ["museum_shop"], "effective_date": "2026-10-10",
        "end_date": "2027-10-09", "price": "99.00",
        "minimum_purchase": {"quantity": "1000", "unit": "件"},
        "royalty": {"mode": "rate", "rate": "0.2", "deduct_costs": True},
        "sample_required": True,
    }
    base.update(overrides)
    return base


class World:
    """搭建包含三家合作方和一个团队的测试环境。"""

    def __init__(self, path: str = ":memory:", clock: FixedClock | None = None):
        self.db = Database(path)
        self.clock = clock or FixedClock(START)
        self.base = DomainService(self.db, self.clock)
        self.merch = MerchService(self.db, self.clock)
        self.R = self.merch.registry
        self.N = self.merch.negotiation
        self.P = self.merch.performance
        self._seq = 0
        self._bootstrap()

    def rid(self, key: str) -> str:
        return f"req-{key}-{self._seq}"

    def tick(self, minutes: int = 1) -> None:
        self.clock = FixedClock(START + timedelta(minutes=minutes))
        for service in (self.merch, self.R, self.N, self.P):
            service.clock = self.clock
        self.base.clock = self.clock

    def _bootstrap(self):
        b = self.base
        b.register_organization(request_id="r-org", actor_id="bootstrap",
                                organization_id="o1", name="赛事机构")
        b.register_actor(request_id="r-admin", actor_id="bootstrap", new_actor_id="admin",
                         display_name="管理员", role="admin", organization_id="o1")
        b.register_actor(request_id="r-op", actor_id="admin", new_actor_id="op",
                         display_name="运营", role="operator", organization_id="o1")
        for suffix, role, name in (("design", "team_member", "设计师"),
                                   ("legal", "team_member", "法务"),
                                   ("sign", "team_member", "签约代表")):
            b.register_actor(request_id=f"r-u-{suffix}", actor_id="admin",
                             new_actor_id=f"u_{suffix}", display_name=name, role=role,
                             organization_id="o1")
        self.partners = {}
        for index, ptype in enumerate(("museum_shop", "tea_brand", "ecommerce"), start=1):
            org = f"po{index}"
            b.register_organization(request_id=f"r-po{index}", actor_id="admin",
                                    organization_id=org, name=f"机构{index}")
            b.register_actor(request_id=f"r-pa{index}", actor_id="admin",
                             new_actor_id=f"prep{index}", display_name=f"代表{index}",
                             role="partner", organization_id=org)
            self.R.register_partner(request_id=f"r-p{index}", actor_id="op",
                                    partner_id=f"partner{index}", organization_id=org,
                                    partner_type=ptype, name=f"合作方{index}")
            self.R.review_qualification(request_id=f"r-q{index}", actor_id="op",
                                        partner_id=f"partner{index}", decision="approved",
                                        valid_until="2027-12-31")
            self.R.bind_representative(request_id=f"r-b{index}", actor_id="op",
                                       partner_id=f"partner{index}",
                                       representative_actor_id=f"prep{index}", can_sign=True)
        self.R.register_team(request_id="r-team", actor_id="op", team_id="team1",
                             organization_id="o1", name="获奖团队",
                             required_countersign_roles=["designer", "legal"])
        for actor_id, member_role in (("u_design", "designer"), ("u_legal", "legal"),
                                      ("u_sign", "signer")):
            self.R.add_team_member(request_id=f"r-tm-{actor_id}", actor_id="op",
                                   team_id="team1", member_actor_id=actor_id,
                                   member_role=member_role)
        self.R.grant_signing(request_id="r-grant", actor_id="op", team_id="team1",
                             member_actor_id="u_sign")
        self.R.register_work(request_id="r-work", actor_id="op", work_id="work1",
                             team_id="team1", title="获奖作品", award_name="金奖")
        self.R.add_design_version(request_id="r-v1", actor_id="u_design", work_id="work1",
                                  version_code="v1", content_hash="hash-v1",
                                  prerequisites=[{"code": "ipr_clear", "label": "权属核查"}])
        self.version_id = self.db.connection.execute(
            "SELECT version_id FROM design_versions WHERE version_code='v1'").fetchone()["version_id"]
        self.R.countersign_version(request_id="r-sig-d", actor_id="u_design",
                                   version_id=self.version_id)
        self.R.countersign_version(request_id="r-sig-l", actor_id="u_legal",
                                   version_id=self.version_id)
        self.R.complete_version_prerequisite(request_id="r-ipr", actor_id="op",
                                             version_id=self.version_id, code="ipr_clear")
        self.R.register_cost_sheet(
            request_id="r-cost1", actor_id="op", work_id="work1",
            lines=[{"label": "打样", "amount": "1000.00", "basis": "fixed"},
                   {"label": "包装", "amount": "2.00", "basis": "unit"}])
        self.cost_id = self.db.connection.execute(
            "SELECT cost_sheet_id FROM cost_sheets").fetchone()["cost_sheet_id"]
        self.R.register_share_sheet(
            request_id="r-share1", actor_id="op", team_id="team1",
            lines=[{"actor_id": "u_design", "ratio": "0.6"},
                   {"actor_id": "u_legal", "ratio": "0.4"}])
        self.share_id = self.db.connection.execute(
            "SELECT share_sheet_id FROM share_sheets").fetchone()["share_sheet_id"]

    def close(self):
        self.db.close()

    # -- 便捷流程 ---------------------------------------------------------------

    def offer(self, *, key, partner="partner1", rep="prep1", direction="inbound",
              actor=None, payload=None, prev=None, valid_until="2026-10-20T00:00:00Z",
              intention=None, channels=None):
        if intention is None:
            intention = self.intention(key, partner=partner, rep=rep, channels=channels)
        payload = payload or terms()
        receipt = self.N.make_offer(
            request_id=self.rid(f"offer-{key}"), actor_id=actor or rep,
            work_id="work1", partner_id=partner, direction=direction,
            intention_id=intention, design_version_id=self.version_id,
            cost_sheet_id=self.cost_id, share_sheet_id=self.share_id, terms=payload,
            valid_until=valid_until, prev_offer_id=prev)
        return receipt.resource_id

    def intention(self, key, *, partner="partner1", rep="prep1", channels=None):
        row = self.db.connection.execute(
            "SELECT intention_id FROM intentions WHERE partner_id=? AND work_id='work1'",
            (partner,)).fetchone()
        if row:
            return row["intention_id"]
        receipt = self.N.create_intention(
            request_id=self.rid(f"int-{key}"), actor_id=rep, work_id="work1",
            partner_id=partner, channels=channels or ["museum_shop"])
        return receipt.resource_id

    def sign_and_accept(self, offer_id, *, team="u_sign", partner_rep="prep1", key="acc"):
        self.N.sign_offer(request_id=self.rid(f"st-{key}"), actor_id=team,
                          offer_id=offer_id, party="team")
        self.N.sign_offer(request_id=self.rid(f"sp-{key}"), actor_id=partner_rep,
                          offer_id=offer_id, party="partner")
        receipt = self.N.accept_offer(request_id=self.rid(f"accept-{key}"),
                                      actor_id="op", offer_id=offer_id)
        return receipt.resource_id


class NegotiationStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()

    def test_counter_offer_chains_thread_and_closes_previous(self):
        w = self.w
        first = w.offer(key="a")
        w.tick(1)
        counter = w.offer(key="b", direction="outbound", actor="u_sign", prev=first,
                          payload=terms(royalty={"mode": "rate", "rate": "0.3"}))
        self.assertEqual(
            self.w.db.connection.execute("SELECT status FROM offers WHERE offer_id=?",
                                         (first,)).fetchone()["status"], "countered")
        rows = self.w.db.connection.execute(
            "SELECT sequence_no FROM offers WHERE offer_id IN (?,?) ORDER BY sequence_no",
            (first, counter)).fetchall()
        self.assertEqual([1, 2], [r["sequence_no"] for r in rows])

    def test_counter_offer_must_come_from_opposite_party(self):
        w = self.w
        first = w.offer(key="a")
        with self.assertRaises(ValidationError):
            w.offer(key="b", rep="prep1", direction="inbound", prev=first)

    def test_signed_offer_cannot_be_countered(self):
        w = self.w
        offer_id = w.offer(key="a")
        w.N.sign_offer(request_id="signed-t", actor_id="u_sign",
                       offer_id=offer_id, party="team")
        with self.assertRaises(ConflictError):
            w.offer(key="b", direction="outbound", actor="u_sign", prev=offer_id,
                    payload=terms(royalty={"mode": "rate", "rate": "0.3"}))

    def test_accepted_offer_cannot_be_withdrawn(self):
        w = self.w
        offer_id = w.offer(key="a")
        w.sign_and_accept(offer_id)
        with self.assertRaises(ConflictError):
            w.N.withdraw_offer(request_id=w.rid("wd"), actor_id="op", offer_id=offer_id)

    def test_terminate_intention_releases_open_offers(self):
        w = self.w
        offer_id = w.offer(key="a")
        intention_id = w.db.connection.execute(
            "SELECT intention_id FROM offers WHERE offer_id=?", (offer_id,)).fetchone()["intention_id"]
        w.N.terminate_intention(request_id=w.rid("term"), actor_id="op",
                                intention_id=intention_id, reason="谈判终止")
        self.assertEqual(
            self.w.db.connection.execute("SELECT status FROM offers WHERE offer_id=?",
                                         (offer_id,)).fetchone()["status"], "terminated")

    def test_accept_requires_both_signatures(self):
        w = self.w
        offer_id = w.offer(key="a")
        w.N.sign_offer(request_id=w.rid("s1"), actor_id="u_sign", offer_id=offer_id,
                       party="team")
        with self.assertRaises(ConflictError):
            w.N.accept_offer(request_id=w.rid("acc"), actor_id="op", offer_id=offer_id)

    def test_team_signer_without_grant_is_rejected(self):
        w = self.w
        offer_id = w.offer(key="a")
        with self.assertRaises(PermissionDenied):
            w.N.sign_offer(request_id=w.rid("sx"), actor_id="u_design",
                           offer_id=offer_id, party="team")

    def test_unqualified_partner_cannot_complete_contract(self):
        w = self.w
        w.R.review_qualification(request_id=w.rid("susp"), actor_id="op",
                                 partner_id="partner1", decision="suspended")
        offer_id = w.offer(key="a")
        w.N.sign_offer(request_id=w.rid("t"), actor_id="u_sign", offer_id=offer_id,
                       party="team")
        w.N.sign_offer(request_id=w.rid("p"), actor_id="prep1", offer_id=offer_id,
                       party="partner")
        with self.assertRaises(PermissionDenied):
            w.N.accept_offer(request_id=w.rid("acc"), actor_id="op", offer_id=offer_id)

    def test_expired_offer_is_rejected(self):
        w = self.w
        offer_id = w.offer(key="a", valid_until="2026-10-04T00:00:00Z")
        w.tick(24 * 60)
        w.N.expire_stale_offers(request_id=w.rid("exp"), actor_id="op")
        status = self.w.db.connection.execute(
            "SELECT status FROM offers WHERE offer_id=?", (offer_id,)).fetchone()["status"]
        self.assertEqual("expired", status)


class ExclusivityTest(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()

    def test_reservation_blocks_overlapping_scope(self):
        w = self.w
        first = w.offer(key="a")
        w.N.reserve_offer(request_id=w.rid("r1"), actor_id="op", offer_id=first,
                          reserve_until="2026-10-09T00:00:00Z")
        second = w.offer(key="b", partner="partner2", rep="prep2",
                         payload=terms(channels=["museum_shop", "ecommerce"]))
        with self.assertRaises(ConflictError):
            w.N.reserve_offer(request_id=w.rid("r2"), actor_id="prep2", offer_id=second,
                              reserve_until="2026-10-08T00:00:00Z")

    def test_distinct_territories_coexist(self):
        w = self.w
        first = w.offer(key="a")
        w.sign_and_accept(first)
        w.tick(1)
        second = w.offer(key="b", partner="partner3", rep="prep3",
                         payload=terms(territories=["US"], channels=["ecommerce"]))
        contract = w.sign_and_accept(second, partner_rep="prep3", key="acc2")
        self.assertTrue(contract)

    def test_same_scope_with_non_overlapping_windows_can_follow(self):
        w = self.w
        first = w.offer(key="a")  # 授权期 2026-10-10 ~ 2027-10-09
        w.sign_and_accept(first)
        w.tick(1)
        second = w.offer(key="b", partner="partner3", rep="prep3",
                         payload=terms(territories=["CN"], channels=["museum_shop"],
                                       effective_date="2028-01-01",
                                       end_date="2028-12-31"))
        contract2 = w.sign_and_accept(second, partner_rep="prep3", key="acc2")
        self.assertTrue(contract2)
        # 期间重叠（起点仍在第一份合同期内）则必须被拒绝。
        w.tick(2)
        overlapping = w.offer(key="c", partner="partner2", rep="prep2",
                              payload=terms(channels=["museum_shop"],
                                            effective_date="2027-05-01",
                                            end_date="2027-12-31"))
        w.N.sign_offer(request_id="t3", actor_id="u_sign", offer_id=overlapping,
                       party="team")
        w.N.sign_offer(request_id="p3", actor_id="prep2", offer_id=overlapping,
                       party="partner")
        with self.assertRaises(ConflictError):
            w.N.accept_offer(request_id="acc3", actor_id="op", offer_id=overlapping)

    def test_cannot_accept_conflicting_contract_after_first_signed(self):
        w = self.w
        first = w.offer(key="a")
        w.sign_and_accept(first)
        w.tick(1)
        second = w.offer(key="b", partner="partner2", rep="prep2",
                         payload=terms(channels=["museum_shop"]))
        w.N.sign_offer(request_id=w.rid("t2"), actor_id="u_sign", offer_id=second,
                       party="team")
        w.N.sign_offer(request_id=w.rid("p2"), actor_id="prep2", offer_id=second,
                       party="partner")
        with self.assertRaises(ConflictError):
            w.N.accept_offer(request_id=w.rid("acc2"), actor_id="op", offer_id=second)

    def test_occupancy_historical_point_excludes_later_contract(self):
        w = self.w
        offer_id = w.offer(key="a")
        w.sign_and_accept(offer_id)
        before = w.N.rights_occupancy("work1", as_of="2026-10-03T07:59:00Z")
        self.assertEqual([], before["occupancy"])
        after = w.N.rights_occupancy("work1")
        self.assertEqual(1, len(after["occupancy"]))


class VersionGateTest(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()

    def test_version_without_countersign_cannot_enter_contract(self):
        w = self.w
        w.R.add_design_version(request_id=w.rid("v2"), actor_id="u_design",
                               work_id="work1", version_code="v2", content_hash="hash-v2")
        v2 = self.w.db.connection.execute(
            "SELECT version_id FROM design_versions WHERE version_code='v2'").fetchone()["version_id"]
        intention = w.intention("x")
        receipt = w.N.make_offer(
            request_id=w.rid("o"), actor_id="prep1", work_id="work1",
            partner_id="partner1", direction="inbound", intention_id=intention,
            design_version_id=v2, cost_sheet_id=w.cost_id, share_sheet_id=w.share_id,
            terms=terms(), valid_until="2026-10-20T00:00:00Z")
        w.N.sign_offer(request_id=w.rid("t"), actor_id="u_sign",
                       offer_id=receipt.resource_id, party="team")
        w.N.sign_offer(request_id=w.rid("p"), actor_id="prep1",
                       offer_id=receipt.resource_id, party="partner")
        with self.assertRaises(ConflictError):
            w.N.accept_offer(request_id=w.rid("acc"), actor_id="op",
                             offer_id=receipt.resource_id)

    def test_delivery_before_sample_is_blocked(self):
        w = self.w
        offer_id = w.offer(key="a")
        contract_id = w.sign_and_accept(offer_id)
        with self.assertRaises(ConflictError):
            w.P.record_delivery(request_id=w.rid("d"), actor_id="prep1",
                                contract_id=contract_id, milestone_code="delivery",
                                quantity_value="100")

    def test_partial_deliveries_accumulate_and_complete_milestone(self):
        w = self.w
        offer_id = w.offer(key="a")
        contract_id = w.sign_and_accept(offer_id)
        w.P.confirm_sample(request_id=w.rid("s"), actor_id="prep1",
                           contract_id=contract_id, approved=True)
        w.P.record_delivery(request_id=w.rid("d1"), actor_id="prep1",
                            contract_id=contract_id, milestone_code="delivery",
                            quantity_value="600")
        view = w.P.get_contract_view(contract_id)
        delivery = next(m for m in view["milestones"] if m["code"] == "delivery")
        self.assertFalse(delivery["completed"])
        w.P.record_delivery(request_id=w.rid("d2"), actor_id="prep1",
                            contract_id=contract_id, milestone_code="delivery",
                            quantity_value="400")
        view = w.P.get_contract_view(contract_id)
        delivery = next(m for m in view["milestones"] if m["code"] == "delivery")
        self.assertTrue(delivery["completed"])


class ChangeOrderTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        offer_id = self.w.offer(key="a")
        self.contract_id = self.w.sign_and_accept(offer_id)

    def tearDown(self):
        self.w.close()

    def _contract(self):
        return self.w.P.get_contract_view(self.contract_id)

    def test_change_requires_dual_signature_and_keeps_original(self):
        w = self.w
        w.P.confirm_sample(request_id=w.rid("s"), actor_id="prep1",
                           contract_id=self.contract_id, approved=True)
        w.R.register_cost_sheet(
            request_id=w.rid("c2"), actor_id="op", work_id="work1",
            lines=[{"label": "新包材", "amount": "0.50", "basis": "unit"}])
        cost2 = self.w.db.connection.execute(
            "SELECT cost_sheet_id FROM cost_sheets ORDER BY version_no DESC LIMIT 1"
        ).fetchone()["cost_sheet_id"]
        change = w.P.propose_change_order(
            request_id=w.rid("co"), actor_id="op", contract_id=self.contract_id,
            reason="换包材", new_cost_sheet_id=cost2)
        with self.assertRaises(ConflictError):
            w.P.approve_change_order(request_id=w.rid("appr"), actor_id="op",
                                     change_order_id=change.resource_id)
        w.P.sign_change_order(request_id=w.rid("ct"), actor_id="u_sign",
                              change_order_id=change.resource_id, party="team")
        w.P.sign_change_order(request_id=w.rid("cp"), actor_id="prep1",
                              change_order_id=change.resource_id, party="partner")
        w.P.approve_change_order(request_id=w.rid("appr2"), actor_id="op",
                                 change_order_id=change.resource_id)
        view = self._contract()
        self.assertEqual(cost2, view["current_cost_sheet_id"])
        self.assertNotEqual(cost2, view["original_cost_sheet_id"])
        facts = [f["fact_type"] for f in w.P.list_facts(self.contract_id)]
        self.assertIn("change_order_effective", facts)


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        offer_id = self.w.offer(key="a")
        self.contract_id = self.w.sign_and_accept(offer_id)
        self.w.P.confirm_sample(request_id="r-sample", actor_id="prep1",
                                contract_id=self.contract_id, approved=True)
        self.w.P.record_delivery(request_id="r-del", actor_id="prep1",
                                 contract_id=self.contract_id, milestone_code="delivery",
                                 quantity_value="1000")

    def tearDown(self):
        self.w.close()

    def test_settlement_amounts_and_idempotent_entries(self):
        w = self.w
        receipt = w.P.generate_settlement(
            request_id="settle-1", actor_id="op", contract_id=self.contract_id,
            gross_revenue="50000", period_start="2026-10-01", period_end="2026-10-31")
        replay = w.P.generate_settlement(
            request_id="settle-1", actor_id="op", contract_id=self.contract_id,
            gross_revenue="50000", period_start="2026-10-01", period_end="2026-10-31")
        self.assertTrue(replay.replayed)
        view = w.P.get_settlement(receipt.resource_id)
        # 成本 1000 + 2*1000 = 3000，净额 47000，团队 20% = 9400。
        self.assertEqual("9400.00", view["team_amount"])
        self.assertEqual("40600.00", view["partner_amount"])
        entries = self.w.db.connection.execute(
            "SELECT COUNT(*) AS c FROM settlement_entries").fetchone()["c"]
        self.assertEqual(3, entries)
        members = {e["recipient_id"]: e["amount"] for e in view["entries"]
                   if e["entry_kind"] == "team_member"}
        self.assertEqual({"u_design": "5640.00", "u_legal": "3760.00"}, members)

    def test_dispute_correction_uses_same_cost_version_and_marks_original(self):
        w = self.w
        receipt = w.P.generate_settlement(
            request_id="rq-s", actor_id="op", contract_id=self.contract_id,
            gross_revenue="50000")
        w.P.dispute_settlement(request_id="rq-dp", actor_id="prep1",
                               settlement_id=receipt.resource_id, description="收入有误")
        dispute_id = self.w.db.connection.execute(
            "SELECT dispute_id FROM settlement_disputes").fetchone()["dispute_id"]
        resolved = w.P.resolve_dispute(
            request_id="rd", actor_id="op", dispute_id=dispute_id,
            resolution="调整为 48000", action="correct", adjusted_gross_revenue="48000")
        self.assertTrue(resolved.resource_id)
        correction = self.w.db.connection.execute(
            "SELECT * FROM settlements WHERE kind='correction'").fetchone()
        self.assertIsNotNone(correction)
        explain = w.merch.explain_settlement(receipt.resource_id)
        self.assertTrue(explain["cost_hash_matches"])
        self.assertTrue(explain["share_hash_matches"])
        original = self.w.db.connection.execute(
            "SELECT status FROM settlements WHERE kind='regular'").fetchone()["status"]
        self.assertEqual("corrected", original)

    def test_unit_costs_apply_per_settlement_period(self):
        w = self.w
        # 第一期：已交付 1000 件（setUp），成本 1000 + 2*1000 = 3000。
        first = w.P.generate_settlement(
            request_id="p1", actor_id="op", contract_id=self.contract_id,
            gross_revenue="50000", period_start="2026-10-01", period_end="2026-10-15")
        self.assertEqual("9400.00", w.P.get_settlement(first.resource_id)["team_amount"])
        # 第二期：跨到 20 天后再交付 500 件。
        w.tick(60 * 24 * 20)
        w.P.record_delivery(request_id="p2-del", actor_id="prep1",
                            contract_id=self.contract_id, milestone_code="delivery",
                            quantity_value="500")
        second = w.P.generate_settlement(
            request_id="p2", actor_id="op", contract_id=self.contract_id,
            gross_revenue="20000", period_start="2026-10-16", period_end="2026-10-31")
        view = w.P.get_settlement(second.resource_id)
        # 单位成本只按当期 500 件：成本 1000 + 2*500 = 2000，净额 18000，团队 20% = 3600。
        self.assertEqual("2000.00", view["detail"]["costs_total"])
        self.assertEqual("3600.00", view["team_amount"])
        self.assertEqual("500.000", view["detail"]["period_quantity"])

    def test_final_settlement_triggers_minimum_guarantee(self):
        w = self.w
        # 新合同仅交付 100 件，低于 1000 件最低量。
        offer_id = w.offer(key="b", partner="partner3", rep="prep3",
                           payload=terms(territories=["US"], channels=["ecommerce"],
                                         royalty={"mode": "rate", "rate": "0.2",
                                                  "guarantee_amount": "5000.00"}))
        contract2 = w.sign_and_accept(offer_id, partner_rep="prep3", key="acc2")
        w.P.confirm_sample(request_id="rq-s2", actor_id="prep3", contract_id=contract2,
                           approved=True)
        w.P.record_delivery(request_id="rq-d2", actor_id="prep3", contract_id=contract2,
                            milestone_code="delivery", quantity_value="100")
        receipt = w.P.generate_settlement(
            request_id="final", actor_id="op", contract_id=contract2,
            gross_revenue="10000", final_settlement=True)
        view = w.P.get_settlement(receipt.resource_id)
        self.assertTrue(view["shortfall"])
        self.assertEqual("5000.00", view["team_amount"])


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        offer_id = self.w.offer(key="a")
        self.contract_id = self.w.sign_and_accept(offer_id)
        self.w.P.confirm_sample(request_id="rq-s", actor_id="prep1",
                                contract_id=self.contract_id, approved=True)
        self.w.P.record_delivery(request_id="rq-d", actor_id="prep1",
                                 contract_id=self.contract_id, milestone_code="delivery",
                                 quantity_value="1000")
        self.settlement = self.w.P.generate_settlement(
            request_id="sv", actor_id="op", contract_id=self.contract_id,
            gross_revenue="50000").resource_id

    def tearDown(self):
        self.w.close()

    def test_partner_sees_totals_but_not_member_share_lines(self):
        view = self.w.merch.settlement_view("prep1", self.settlement)
        self.assertEqual("9400.00", view["team_amount"])
        self.assertTrue(all(e["entry_kind"] != "team_member" for e in view["entries"]))

    def test_team_sees_member_share_lines(self):
        view = self.w.merch.settlement_view("u_design", self.settlement)
        self.assertTrue(any(e["entry_kind"] == "team_member" for e in view["entries"]))

    def test_unrelated_partner_cannot_view_contract(self):
        with self.assertRaises(PermissionDenied):
            self.w.merch.contract_view("prep2", self.contract_id)


class RecoveryTest(unittest.TestCase):
    def test_pending_offer_order_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.sqlite3"
            w = World(path)
            first = w.offer(key="a")
            w.tick(1)
            counter = w.offer(key="b", direction="outbound", actor="u_sign", prev=first,
                              payload=terms(royalty={"mode": "rate", "rate": "0.25"}))
            w.N.reserve_offer(request_id="resv", actor_id="op", offer_id=counter,
                              reserve_until="2026-10-19T00:00:00Z")
            w.close()

            reopened = World.__new__(World)
            reopened.db = Database(path)
            reopened.clock = FixedClock(START + timedelta(minutes=2))
            reopened.merch = MerchService(reopened.db, reopened.clock)
            reopened.R = reopened.merch.registry
            reopened.N = reopened.merch.negotiation
            reopened.P = reopened.merch.performance
            rows = reopened.db.connection.execute(
                "SELECT offer_id, sequence_no, status FROM offers ORDER BY sequence_no"
            ).fetchall()
            self.assertEqual([1, 2], [r["sequence_no"] for r in rows])
            reserved = [r for r in rows if r["status"] == "reserved"]
            self.assertEqual(1, len(reserved))
            reopened.N.sign_offer(request_id="t2", actor_id="u_sign",
                                  offer_id=counter, party="team")
            reopened.N.sign_offer(request_id="p2", actor_id="prep1",
                                  offer_id=counter, party="partner")
            receipt = reopened.N.accept_offer(request_id="acc2", actor_id="op",
                                              offer_id=counter)
            self.assertTrue(receipt.resource_id)
            valid, _ = DomainService(reopened.db, reopened.clock).verify_audit()
            self.assertTrue(valid)
            reopened.db.close()


if __name__ == "__main__":
    unittest.main()
