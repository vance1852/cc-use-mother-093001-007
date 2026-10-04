import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from creative_program_foundation.api import route
from creative_program_foundation.clock import FixedClock
from creative_program_foundation.commercialization import CommercializationService
from creative_program_foundation.errors import ConflictError, PermissionDenied, ValidationError
from creative_program_foundation.storage import Database


class StepClock:
    def __init__(self):
        self.moment = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)

    def now(self):
        return self.moment

    def advance(self, days=1):
        self.moment += timedelta(days=days)


class CommercializationTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = StepClock()
        self.service = CommercializationService(self.database, self.clock)
        self._seq = 0
        s = self.service
        s.register_organization(request_id="req-org", actor_id="bootstrap",
                                organization_id="o1", name="赛事机构")
        s.register_actor(request_id="req-a1", actor_id="bootstrap", new_actor_id="a1",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="req-op1", actor_id="a1", new_actor_id="op1",
                         display_name="运营专员", role="operator", organization_id="o1")
        s.register_actor(request_id="req-au1", actor_id="a1", new_actor_id="au1",
                         display_name="审计员", role="auditor", organization_id="o1")
        s.register_actor(request_id="req-pa1", actor_id="a1", new_actor_id="pa1",
                         display_name="合作方A联系人", role="partner", organization_id="o1")
        s.register_actor(request_id="req-pb1", actor_id="a1", new_actor_id="pb1",
                         display_name="合作方B联系人", role="partner", organization_id="o1")
        s.register_partner(request_id="req-pta", actor_id="op1", partner_id="partner-a",
                           organization_id="o1", name="博物馆商店",
                           qualification={"license_no": "LIC-A"})
        s.register_partner(request_id="req-ptb", actor_id="op1", partner_id="partner-b",
                           organization_id="o1", name="茶品牌",
                           qualification={"license_no": "LIC-B"})
        s.link_partner_member(request_id="req-lma", actor_id="a1",
                              partner_id="partner-a", member_actor_id="pa1")
        s.link_partner_member(request_id="req-lmb", actor_id="a1",
                              partner_id="partner-b", member_actor_id="pb1")
        s.register_work(request_id="req-w1", actor_id="op1", work_id="w1",
                        organization_id="o1", title="获奖作品", award={"level": "金奖"})
        s.add_design_version(request_id="req-dv1", actor_id="op1", work_id="w1",
                             design_version_id="dv-1", version_no=1, summary="首发设计", spec={})

    def tearDown(self):
        self.database.close()

    def rid(self):
        self._seq += 1
        return f"req-x{self._seq}"

    def terms(self, **overrides):
        base = {
            "design_version_id": "dv-1",
            "quote": {"currency": "CNY", "unit_price_cents": 10000},
            "exclusive": True,
            "territories": ["CN-East"],
            "channels": ["museum-store"],
            "min_commitment": {"quantity": 100, "period": "yearly"},
            "cost_items": [{"name": "生产", "kind": "fixed", "amount_cents": 100000}],
            "shares": [{"member": "partner", "bps": 6000}, {"member": "team", "bps": 4000}],
            "milestones": [
                {"key": "sample", "title": "样品确认", "gate": True, "depends_on": []},
                {"key": "delivery", "title": "首批交付", "gate": False, "depends_on": ["sample"]},
            ],
            "required_signers": ["op1", "pa1"],
        }
        base.update(overrides)
        return base

    def open_neg(self, nid="neg-1", partner="partner-a"):
        return self.service.open_negotiation(request_id=self.rid(), actor_id="op1",
                                             negotiation_id=nid, work_id="w1", partner_id=partner)

    def make_offer(self, nid="neg-1", side="team", actor="op1", **overrides):
        return self.service.make_offer(request_id=self.rid(), actor_id=actor,
                                       negotiation_id=nid, side=side, **self.terms(**overrides))

    def accepted_contract(self, nid="neg-1", partner="partner-a", accept_actor="pa1",
                          **overrides):
        self.open_neg(nid, partner)
        offer = self.make_offer(nid, **overrides)
        accepted = self.service.accept_offer(request_id=self.rid(), actor_id=accept_actor,
                                             offer_id=offer.resource_id)
        return accepted.resource_id

    def active_contract(self, nid="neg-1", partner="partner-a", **overrides):
        contract_id = self.accepted_contract(nid, partner, **overrides)
        for signer in overrides.get("required_signers", ["op1", "pa1"]):
            self.service.sign_contract(request_id=self.rid(), actor_id=signer,
                                       contract_id=contract_id)
        self.service.complete_milestone(request_id=self.rid(), actor_id="op1",
                                        contract_id=contract_id, milestone_key="sample")
        self.service.activate_contract(request_id=self.rid(), actor_id="op1",
                                       contract_id=contract_id)
        return contract_id

    # ------------------------------------------------------------------
    # 生命周期与时序校验
    # ------------------------------------------------------------------

    def test_full_lifecycle_reaches_fulfillment(self):
        contract_id = self.active_contract()
        view = self.service.get_contract_view(actor_id="op1", contract_id=contract_id)
        self.assertEqual("active", view["status"])
        self.assertEqual(["op1", "pa1"], view["signatures"])
        self.assertEqual(["sample", "delivery"],
                         [item["key"] for item in view["milestones"]])
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)

    def test_reserved_offer_blocks_accept_and_counter(self):
        self.open_neg()
        offer = self.make_offer()
        self.service.reserve_offer(request_id=self.rid(), actor_id="op1",
                                   offer_id=offer.resource_id)
        with self.assertRaises(ConflictError):
            self.service.accept_offer(request_id=self.rid(), actor_id="pa1",
                                      offer_id=offer.resource_id)
        with self.assertRaises(ConflictError):
            self.service.counter_offer(request_id=self.rid(), actor_id="pa1",
                                       offer_id=offer.resource_id, side="partner",
                                       **self.terms())
        self.service.release_offer(request_id=self.rid(), actor_id="op1",
                                   offer_id=offer.resource_id)
        accepted = self.service.accept_offer(request_id=self.rid(), actor_id="pa1",
                                             offer_id=offer.resource_id)
        self.assertFalse(accepted.replayed)

    def test_counter_supersedes_and_requires_latest(self):
        self.open_neg()
        first = self.make_offer()
        counter = self.service.counter_offer(request_id=self.rid(), actor_id="pa1",
                                             offer_id=first.resource_id, side="partner",
                                             **self.terms())
        with self.assertRaises(ConflictError):
            self.service.counter_offer(request_id=self.rid(), actor_id="op1",
                                       offer_id=first.resource_id, side="team", **self.terms())
        with self.assertRaises(ConflictError):
            self.service.accept_offer(request_id=self.rid(), actor_id="pa1",
                                      offer_id=first.resource_id)
        accepted = self.service.accept_offer(request_id=self.rid(), actor_id="op1",
                                             offer_id=counter.resource_id)
        self.assertEqual("contract", accepted.resource_type)

    def test_withdraw_only_by_offering_side_and_before_accept(self):
        self.open_neg()
        offer = self.make_offer()
        with self.assertRaises(PermissionDenied):
            self.service.withdraw_offer(request_id=self.rid(), actor_id="pa1",
                                        offer_id=offer.resource_id)
        self.service.withdraw_offer(request_id=self.rid(), actor_id="op1",
                                    offer_id=offer.resource_id)
        with self.assertRaises(ConflictError):
            self.service.accept_offer(request_id=self.rid(), actor_id="pa1",
                                      offer_id=offer.resource_id)
        renewed = self.make_offer()
        self.assertEqual(2, self.service.get_negotiation(actor_id="op1",
                                                         negotiation_id="neg-1")["offers"][-1]["offer_no"])
        self.assertFalse(renewed.replayed)

    def test_new_offer_blocked_while_live_offer_exists(self):
        self.open_neg()
        self.make_offer()
        with self.assertRaises(ConflictError):
            self.make_offer()

    def test_accept_must_come_from_opposite_side(self):
        self.open_neg()
        offer = self.make_offer()
        with self.assertRaises(PermissionDenied):
            self.service.accept_offer(request_id=self.rid(), actor_id="op1",
                                      offer_id=offer.resource_id)

    def test_negotiation_closed_after_accept(self):
        self.accepted_contract()
        with self.assertRaises(ConflictError):
            self.make_offer()

    # ------------------------------------------------------------------
    # 独家范围冲突与权利占用
    # ------------------------------------------------------------------

    def test_conflicting_exclusive_scope_rejected(self):
        self.active_contract(nid="neg-1", partner="partner-a")
        self.open_neg("neg-2", "partner-b")
        offer = self.make_offer("neg-2", required_signers=["op1", "pb1"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.accept_offer(request_id=self.rid(), actor_id="pb1",
                                      offer_id=offer.resource_id)
        self.assertIn("独家范围", str(ctx.exception))

    def test_non_overlapping_scope_allowed(self):
        self.active_contract(nid="neg-1", partner="partner-a")
        contract_b = self.accepted_contract("neg-2", "partner-b", accept_actor="pb1",
                                            territories=["CN-North"],
                                            required_signers=["op1", "pb1"])
        self.assertIsNotNone(contract_b)

    def test_termination_releases_rights_and_failed_request_id_reusable(self):
        contract_a = self.active_contract(nid="neg-1", partner="partner-a")
        self.open_neg("neg-2", "partner-b")
        offer = self.make_offer("neg-2", required_signers=["op1", "pb1"])
        request_id = self.rid()
        with self.assertRaises(ConflictError):
            self.service.accept_offer(request_id=request_id, actor_id="pb1",
                                      offer_id=offer.resource_id)
        self.service.terminate_contract(request_id=self.rid(), actor_id="a1",
                                        contract_id=contract_a, reason="合作方违约")
        replay = self.service.accept_offer(request_id=request_id, actor_id="pb1",
                                           offer_id=offer.resource_id)
        self.assertFalse(replay.replayed)
        self.assertEqual("contract", replay.resource_type)

    def test_rights_at_explains_occupancy_over_time(self):
        contract_id = self.active_contract(nid="neg-1", partner="partner-a")
        created = self.service.get_contract_view(actor_id="a1", contract_id=contract_id)["created_at"]
        before = self.service.rights_at(actor_id="au1", work_id="w1", at="2025-12-31T00:00:00Z")
        self.assertEqual([], before["occupied"])
        during = self.service.rights_at(actor_id="au1", work_id="w1", at=created)
        self.assertEqual(1, len(during["occupied"]))
        self.assertEqual("CN-East", during["occupied"][0]["territory"])
        self.assertEqual("museum-store", during["occupied"][0]["channel"])
        self.assertTrue(during["occupied"][0]["exclusive"])
        self.clock.advance(days=2)
        self.service.terminate_contract(request_id=self.rid(), actor_id="a1",
                                        contract_id=contract_id, reason="协商终止")
        terminated = self.service.get_contract_view(actor_id="a1",
                                                    contract_id=contract_id)["terminated_at"]
        after = self.service.rights_at(actor_id="au1", work_id="w1", at=terminated)
        self.assertEqual([], after["occupied"])
        between = self.service.rights_at(actor_id="au1", work_id="w1", at=created)
        self.assertEqual(1, len(between["occupied"]))

    # ------------------------------------------------------------------
    # 会签与前置里程碑门禁
    # ------------------------------------------------------------------

    def test_activation_requires_full_signatures_and_gate_milestones(self):
        contract_id = self.accepted_contract()
        with self.assertRaises(ConflictError) as ctx:
            self.service.activate_contract(request_id=self.rid(), actor_id="op1",
                                           contract_id=contract_id)
        self.assertIn("会签未完成", str(ctx.exception))
        self.service.sign_contract(request_id=self.rid(), actor_id="op1", contract_id=contract_id)
        self.service.sign_contract(request_id=self.rid(), actor_id="pa1", contract_id=contract_id)
        with self.assertRaises(ConflictError) as ctx:
            self.service.activate_contract(request_id=self.rid(), actor_id="op1",
                                           contract_id=contract_id)
        self.assertIn("前置里程碑未完成", str(ctx.exception))
        self.service.complete_milestone(request_id=self.rid(), actor_id="op1",
                                        contract_id=contract_id, milestone_key="sample")
        self.service.activate_contract(request_id=self.rid(), actor_id="op1",
                                       contract_id=contract_id)
        view = self.service.get_contract_view(actor_id="op1", contract_id=contract_id)
        self.assertEqual("active", view["status"])

    def test_milestone_dependency_order_enforced(self):
        contract_id = self.accepted_contract()
        with self.assertRaises(ConflictError) as ctx:
            self.service.complete_milestone(request_id=self.rid(), actor_id="op1",
                                            contract_id=contract_id, milestone_key="delivery")
        self.assertIn("前置里程碑未完成", str(ctx.exception))

    def test_delivery_blocked_before_activation(self):
        contract_id = self.accepted_contract()
        with self.assertRaises(ConflictError):
            self.service.record_delivery(request_id=self.rid(), actor_id="op1",
                                         contract_id=contract_id, milestone_key="delivery",
                                         quantity=10)

    def test_sign_rules(self):
        contract_id = self.accepted_contract()
        with self.assertRaises(PermissionDenied):
            self.service.sign_contract(request_id=self.rid(), actor_id="au1",
                                       contract_id=contract_id)
        request_id = self.rid()
        first = self.service.sign_contract(request_id=request_id, actor_id="op1",
                                           contract_id=contract_id)
        replay = self.service.sign_contract(request_id=request_id, actor_id="op1",
                                            contract_id=contract_id)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        with self.assertRaises(ConflictError):
            self.service.sign_contract(request_id=self.rid(), actor_id="op1",
                                       contract_id=contract_id)

    # ------------------------------------------------------------------
    # 只追加事实
    # ------------------------------------------------------------------

    def test_design_change_appends_fact_without_rewriting_contract(self):
        contract_id = self.active_contract()
        self.service.add_design_version(request_id=self.rid(), actor_id="op1", work_id="w1",
                                        design_version_id="dv-2", version_no=2,
                                        summary="改稿", spec={})
        self.service.record_design_change(request_id=self.rid(), actor_id="op1",
                                          contract_id=contract_id, design_version_id="dv-2",
                                          reason="客户要求")
        view = self.service.get_contract_view(actor_id="op1", contract_id=contract_id)
        self.assertEqual("dv-2", view["design_version_id"])
        self.assertEqual("dv-1", view["signed_design_version_id"])
        timeline = self.service.contract_timeline(actor_id="op1", contract_id=contract_id)
        changes = [fact for fact in timeline if fact["kind"] == "design_change"]
        self.assertEqual(1, len(changes))
        self.assertEqual({"from": "dv-1", "to": "dv-2", "reason": "客户要求"},
                         changes[0]["payload"])
        with self.assertRaises(ValidationError):
            self.service.record_design_change(request_id=self.rid(), actor_id="op1",
                                              contract_id=contract_id, design_version_id="dv-2",
                                              reason="无变化")

    def test_breach_rectification_and_dispute_are_appended_facts(self):
        contract_id = self.active_contract()
        breach = self.service.record_breach(request_id=self.rid(), actor_id="op1",
                                            contract_id=contract_id,
                                            description="未达到最低采购量")
        self.service.record_rectification(request_id=self.rid(), actor_id="op1",
                                          contract_id=contract_id,
                                          breach_fact_id=breach.resource_id,
                                          description="补单整改")
        settlement = self.service.create_settlement(request_id=self.rid(), actor_id="op1",
                                                    contract_id=contract_id, period="2026-Q1",
                                                    gross_amount_cents=500000)
        before = self.service.get_settlement_view(actor_id="op1",
                                                  settlement_id=settlement.resource_id)
        self.service.dispute_settlement(request_id=self.rid(), actor_id="pa1",
                                        settlement_id=settlement.resource_id,
                                        reason="成本口径争议")
        after = self.service.get_settlement_view(actor_id="op1",
                                                 settlement_id=settlement.resource_id)
        self.assertEqual(before, after)
        kinds = [fact["kind"] for fact in
                 self.service.contract_timeline(actor_id="op1", contract_id=contract_id)]
        self.assertEqual(["breach", "rectification", "dispute"], kinds)

    def test_cost_and_share_revisions_are_versioned(self):
        contract_id = self.active_contract()
        self.service.revise_cost_basis(request_id=self.rid(), actor_id="op1",
                                       contract_id=contract_id,
                                       cost_items=[{"name": "生产", "kind": "fixed",
                                                    "amount_cents": 80000}])
        self.service.revise_shares(request_id=self.rid(), actor_id="op1",
                                   contract_id=contract_id,
                                   shares=[{"member": "partner", "bps": 7000},
                                           {"member": "team", "bps": 3000}])
        settlement = self.service.create_settlement(request_id=self.rid(), actor_id="op1",
                                                    contract_id=contract_id, period="2026-Q1",
                                                    gross_amount_cents=1000000)
        explanation = self.service.explain_settlement(actor_id="au1",
                                                      settlement_id=settlement.resource_id)
        self.assertEqual(2, explanation["cost_version_no"])
        self.assertEqual(2, explanation["share_version_no"])
        self.assertEqual([{"name": "生产", "kind": "fixed", "amount_cents": 80000}],
                         explanation["cost_items"])
        self.assertEqual(7000, explanation["shares"][0]["bps"])
        self.assertEqual(920000, explanation["net_cents"])

    # ------------------------------------------------------------------
    # 结算
    # ------------------------------------------------------------------

    def test_settlement_computation_uses_quote_cost_and_shares(self):
        contract_id = self.active_contract(
            cost_items=[{"name": "生产", "kind": "fixed", "amount_cents": 100000},
                        {"name": "扣点", "kind": "rate", "rate_bps": 500}])
        settlement = self.service.create_settlement(request_id=self.rid(), actor_id="op1",
                                                    contract_id=contract_id, period="2026-Q1",
                                                    gross_amount_cents=1000000)
        view = self.service.get_settlement_view(actor_id="op1",
                                                settlement_id=settlement.resource_id)
        self.assertEqual(150000, view["cost_total_cents"])
        self.assertEqual(850000, view["net_cents"])
        lines = {line["member"]: line["amount_cents"] for line in view["lines"]}
        self.assertEqual({"partner": 510000, "team": 340000}, lines)

    def test_settlement_remainder_goes_to_largest_share(self):
        contract_id = self.active_contract(
            cost_items=[],
            shares=[{"member": "a", "bps": 3333}, {"member": "b", "bps": 3333},
                    {"member": "partner", "bps": 3334}])
        settlement = self.service.create_settlement(request_id=self.rid(), actor_id="op1",
                                                    contract_id=contract_id, period="2026-Q1",
                                                    gross_amount_cents=100001)
        view = self.service.get_settlement_view(actor_id="op1",
                                                settlement_id=settlement.resource_id)
        lines = {line["member"]: line["amount_cents"] for line in view["lines"]}
        self.assertEqual({"a": 33330, "b": 33330, "partner": 33341}, lines)
        self.assertEqual(100001, sum(lines.values()))

    def test_duplicate_settlement_callback_does_not_duplicate_entries(self):
        contract_id = self.active_contract()
        request_id = self.rid()
        first = self.service.create_settlement(request_id=request_id, actor_id="op1",
                                               contract_id=contract_id, period="2026-Q1",
                                               gross_amount_cents=1000000)
        replay = self.service.create_settlement(request_id=request_id, actor_id="op1",
                                                contract_id=contract_id, period="2026-Q1",
                                                gross_amount_cents=1000000)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        count = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM settlements").fetchone()["c"]
        self.assertEqual(1, count)
        with self.assertRaises(ConflictError):
            self.service.create_settlement(request_id=self.rid(), actor_id="op1",
                                           contract_id=contract_id, period="2026-Q1",
                                           gross_amount_cents=1000000)

    def test_settlement_requires_active_contract(self):
        contract_id = self.accepted_contract()
        with self.assertRaises(ConflictError):
            self.service.create_settlement(request_id=self.rid(), actor_id="op1",
                                           contract_id=contract_id, period="2026-Q1",
                                           gross_amount_cents=100)

    # ------------------------------------------------------------------
    # 最小必要视图
    # ------------------------------------------------------------------

    def test_partner_view_hides_team_internals(self):
        contract_id = self.active_contract()
        partner_view = self.service.get_contract_view(actor_id="pa1", contract_id=contract_id)
        self.assertNotIn("shares", partner_view)
        self.assertNotIn("required_signers", partner_view)
        self.assertNotIn("cost_version_no", partner_view)
        self.assertEqual(6000, partner_view["partner_share"]["bps"])
        self.assertTrue(partner_view["signature_complete"])
        team_view = self.service.get_contract_view(actor_id="op1", contract_id=contract_id)
        self.assertIn("required_signers", team_view)
        self.assertIn("cost_version_no", team_view)

    def test_partner_cannot_see_other_partners_contract(self):
        contract_id = self.active_contract(nid="neg-1", partner="partner-a")
        with self.assertRaises(PermissionDenied):
            self.service.get_contract_view(actor_id="pb1", contract_id=contract_id)
        with self.assertRaises(PermissionDenied):
            self.service.contract_timeline(actor_id="pb1", contract_id=contract_id)

    def test_partner_settlement_view_shows_only_own_line(self):
        contract_id = self.active_contract()
        settlement = self.service.create_settlement(request_id=self.rid(), actor_id="op1",
                                                    contract_id=contract_id, period="2026-Q1",
                                                    gross_amount_cents=1000000)
        view = self.service.get_settlement_view(actor_id="pa1",
                                                settlement_id=settlement.resource_id)
        self.assertNotIn("lines", view)
        self.assertEqual(540000, view["own_line"]["amount_cents"])
        explanation = self.service.explain_settlement(actor_id="pa1",
                                                      settlement_id=settlement.resource_id)
        self.assertNotIn("cost_items", explanation)
        self.assertEqual(1, explanation["cost_version_no"])

    def test_partner_cannot_perform_team_actions(self):
        self.open_neg()
        with self.assertRaises(PermissionDenied):
            self.service.make_offer(request_id=self.rid(), actor_id="pa1",
                                    negotiation_id="neg-1", side="team", **self.terms())
        offer = self.make_offer()
        with self.assertRaises(PermissionDenied):
            self.service.reserve_offer(request_id=self.rid(), actor_id="pa1",
                                       offer_id=offer.resource_id)
        contract_id = self.accepted_contract(nid="neg-9")
        with self.assertRaises(PermissionDenied):
            self.service.complete_milestone(request_id=self.rid(), actor_id="pa1",
                                            contract_id=contract_id, milestone_key="sample")
        with self.assertRaises(PermissionDenied):
            self.service.record_delivery(request_id=self.rid(), actor_id="pa1",
                                         contract_id=contract_id, milestone_key="delivery",
                                         quantity=1)

    def test_partner_can_open_negotiation_and_counter_for_self(self):
        self.service.open_negotiation(request_id=self.rid(), actor_id="pa1",
                                      negotiation_id="neg-p", work_id="w1",
                                      partner_id="partner-a")
        offer = self.make_offer("neg-p")
        counter = self.service.counter_offer(request_id=self.rid(), actor_id="pa1",
                                             offer_id=offer.resource_id, side="partner",
                                             **self.terms())
        self.assertFalse(counter.replayed)
        with self.assertRaises(PermissionDenied):
            self.service.open_negotiation(request_id=self.rid(), actor_id="pa1",
                                          negotiation_id="neg-q", work_id="w1",
                                          partner_id="partner-b")

    # ------------------------------------------------------------------
    # 幂等与审计
    # ------------------------------------------------------------------

    def test_request_id_rejects_changed_payload(self):
        self.open_neg()
        request_id = self.rid()
        self.service.make_offer(request_id=request_id, actor_id="op1", negotiation_id="neg-1",
                                side="team", **self.terms())
        with self.assertRaises(ConflictError):
            self.service.make_offer(request_id=request_id, actor_id="op1",
                                    negotiation_id="neg-1", side="team",
                                    **self.terms(exclusive=False))

    def test_audit_chain_records_ordered_lifecycle(self):
        self.active_contract()
        valid, _ = self.service.verify_audit()
        self.assertTrue(valid)
        actions = [event["action"] for event in self.service.audit_events()]
        sequence = ["negotiation.opened", "offer.made", "offer.accepted", "contract.created",
                    "contract.signed", "milestone.completed", "contract.activated"]
        positions = [actions.index(action) for action in sequence]
        self.assertEqual(positions, sorted(positions))

    def test_pending_offers_listing(self):
        self.open_neg("neg-1")
        self.make_offer("neg-1")
        self.open_neg("neg-2", "partner-b")
        self.make_offer("neg-2", required_signers=["op1", "pb1"])
        pending = self.service.list_pending_offers(actor_id="au1")
        self.assertEqual(2, len(pending))
        self.assertEqual(["neg-1", "neg-2"],
                         [offer["negotiation_id"] for offer in pending])
        with self.assertRaises(PermissionDenied):
            self.service.list_pending_offers(actor_id="pa1")


class RecoveryTest(unittest.TestCase):
    def test_recovery_preserves_pending_offers_and_milestone_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.sqlite3"
            clock = FixedClock(datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc))
            database = Database(path)
            service = CommercializationService(database, clock)
            service.register_organization(request_id="req-org", actor_id="bootstrap",
                                          organization_id="o1", name="机构")
            service.register_actor(request_id="req-a1", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
            service.register_actor(request_id="req-op1", actor_id="a1", new_actor_id="op1",
                                   display_name="运营", role="operator", organization_id="o1")
            service.register_actor(request_id="req-pa1", actor_id="a1", new_actor_id="pa1",
                                   display_name="联系人", role="partner", organization_id="o1")
            service.register_partner(request_id="req-pt", actor_id="op1", partner_id="partner-a",
                                     organization_id="o1", name="商店",
                                     qualification={"license_no": "LIC"})
            service.link_partner_member(request_id="req-lm", actor_id="a1",
                                        partner_id="partner-a", member_actor_id="pa1")
            service.register_work(request_id="req-w", actor_id="op1", work_id="w1",
                                  organization_id="o1", title="作品", award={})
            service.add_design_version(request_id="req-dv", actor_id="op1", work_id="w1",
                                       design_version_id="dv-1", version_no=1,
                                       summary="首发", spec={})
            terms = {
                "design_version_id": "dv-1",
                "quote": {"currency": "CNY", "unit_price_cents": 100},
                "exclusive": False,
                "territories": ["CN"],
                "channels": ["online"],
                "min_commitment": {"quantity": 10, "period": "yearly"},
                "cost_items": [],
                "shares": [{"member": "partner", "bps": 5000}, {"member": "team", "bps": 5000}],
                "milestones": [
                    {"key": "sample", "title": "样品确认", "gate": True, "depends_on": []},
                    {"key": "make", "title": "生产", "gate": False, "depends_on": ["sample"]},
                    {"key": "delivery", "title": "交付", "gate": False, "depends_on": ["make"]},
                ],
                "required_signers": ["op1"],
            }
            service.open_negotiation(request_id="req-n1", actor_id="op1",
                                     negotiation_id="neg-1", work_id="w1", partner_id="partner-a")
            service.make_offer(request_id="req-o1", actor_id="op1", negotiation_id="neg-1",
                               side="team", **terms)
            service.open_negotiation(request_id="req-n2", actor_id="op1",
                                     negotiation_id="neg-2", work_id="w1", partner_id="partner-a")
            offer2 = service.make_offer(request_id="req-o2", actor_id="op1",
                                        negotiation_id="neg-2", side="team", **terms)
            accepted = service.accept_offer(request_id="req-ac", actor_id="pa1",
                                            offer_id=offer2.resource_id)
            contract_id = accepted.resource_id
            pending_before = [item["offer_id"]
                              for item in service.list_pending_offers(actor_id="a1")]
            milestones_before = [item["key"] for item in
                                 service.get_contract_view(actor_id="a1",
                                                           contract_id=contract_id)["milestones"]]
            database.close()

            restored = Database(path)
            service2 = CommercializationService(restored, clock)
            pending_after = [item["offer_id"]
                             for item in service2.list_pending_offers(actor_id="a1")]
            milestones_after = [item["key"] for item in
                                service2.get_contract_view(actor_id="a1",
                                                           contract_id=contract_id)["milestones"]]
            self.assertEqual(pending_before, pending_after)
            self.assertEqual(["sample", "make", "delivery"], milestones_before)
            self.assertEqual(milestones_before, milestones_after)
            negotiation = service2.get_negotiation(actor_id="a1", negotiation_id="neg-1")
            self.assertEqual("open", negotiation["offers"][0]["status"])
            valid, _ = service2.verify_audit()
            self.assertTrue(valid)
            restored.close()


class ApiRouteTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = CommercializationService(
            self.database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        self._seq = 0
        self.service.register_organization(request_id="req-org", actor_id="bootstrap",
                                           organization_id="o1", name="机构")
        self.service.register_actor(request_id="req-a1", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="req-op1", actor_id="a1", new_actor_id="op1",
                                    display_name="运营", role="operator", organization_id="o1")
        self.service.register_actor(request_id="req-pa1", actor_id="a1", new_actor_id="pa1",
                                    display_name="联系人", role="partner", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def rid(self):
        self._seq += 1
        return f"req-h{self._seq}"

    def post(self, path, body, actor="op1"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def get(self, path, actor="op1"):
        return route(self.service, "GET", path, None, {"X-Actor-Id": actor})

    def test_full_flow_over_http(self):
        status, _ = self.post("/partners", {
            "request_id": self.rid(), "partner_id": "partner-a", "organization_id": "o1",
            "name": "博物馆商店", "qualification": {"license_no": "LIC-A"}})
        self.assertEqual(201, status)
        status, _ = self.post("/partner-members", {
            "request_id": self.rid(), "partner_id": "partner-a", "member_actor_id": "pa1"},
            actor="a1")
        self.assertEqual(201, status)
        status, _ = self.post("/works", {
            "request_id": self.rid(), "work_id": "w1", "organization_id": "o1",
            "title": "获奖作品", "award": {}})
        self.assertEqual(201, status)
        status, _ = self.post("/design-versions", {
            "request_id": self.rid(), "work_id": "w1", "design_version_id": "dv-1",
            "version_no": 1, "summary": "首发", "spec": {}})
        self.assertEqual(201, status)
        status, _ = self.post("/negotiations", {
            "request_id": self.rid(), "negotiation_id": "neg-1", "work_id": "w1",
            "partner_id": "partner-a"})
        self.assertEqual(201, status)
        terms = {
            "design_version_id": "dv-1",
            "quote": {"currency": "CNY", "unit_price_cents": 100},
            "exclusive": True,
            "territories": ["CN-East"],
            "channels": ["museum-store"],
            "min_commitment": {"quantity": 10, "period": "yearly"},
            "cost_items": [],
            "shares": [{"member": "partner", "bps": 6000}, {"member": "team", "bps": 4000}],
            "milestones": [{"key": "sample", "title": "样品确认", "gate": True, "depends_on": []}],
            "required_signers": ["op1", "pa1"],
        }
        offer_body = dict(terms, request_id=self.rid(), negotiation_id="neg-1", side="team")
        status, offer = self.post("/offers", offer_body)
        self.assertEqual(201, status)
        offer_id = offer["resource_id"]
        status, replay = self.post("/offers", offer_body)
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        status, pending = self.get("/pending-offers", actor="a1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(pending["items"]))
        status, accepted = self.post(f"/offers/{offer_id}/accept",
                                     {"request_id": self.rid()}, actor="pa1")
        self.assertEqual(201, status)
        contract_id = accepted["resource_id"]
        self.post(f"/contracts/{contract_id}/sign", {"request_id": self.rid()}, actor="op1")
        self.post(f"/contracts/{contract_id}/sign", {"request_id": self.rid()}, actor="pa1")
        status, _ = self.post(f"/contracts/{contract_id}/activate", {"request_id": self.rid()})
        self.assertEqual(409, status)
        status, _ = self.post(f"/contracts/{contract_id}/milestones/sample/complete",
                              {"request_id": self.rid()})
        self.assertEqual(201, status)
        status, _ = self.post(f"/contracts/{contract_id}/activate", {"request_id": self.rid()})
        self.assertEqual(201, status)
        status, settlement = self.post(f"/contracts/{contract_id}/settlements", {
            "request_id": self.rid(), "period": "2026-Q4", "gross_amount_cents": 100000})
        self.assertEqual(201, status)
        settlement_id = settlement["resource_id"]
        status, explanation = self.get(f"/settlements/{settlement_id}/explanation", actor="a1")
        self.assertEqual(200, status)
        self.assertEqual(1, explanation["cost_version_no"])
        status, partner_view = self.get(f"/contracts/{contract_id}", actor="pa1")
        self.assertEqual(200, status)
        self.assertNotIn("required_signers", partner_view)
        status, team_view = self.get(f"/contracts/{contract_id}", actor="op1")
        self.assertEqual(200, status)
        self.assertIn("required_signers", team_view)
        status, rights = self.get("/works/w1/rights?at=2026-09-25T09:00:00Z", actor="a1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(rights["occupied"]))
        status, timeline = self.get(f"/contracts/{contract_id}/timeline", actor="pa1")
        self.assertEqual(200, status)
        self.assertEqual([], timeline["items"])
        status, _ = self.post(f"/contracts/{contract_id}/terminate",
                              {"request_id": self.rid(), "reason": "到期"}, actor="a1")
        self.assertEqual(201, status)
        status, rights = self.get("/works/w1/rights?at=2026-09-25T09:00:01Z", actor="a1")
        self.assertEqual(200, status)
        self.assertEqual([], rights["occupied"])

    def test_unknown_commercialization_route_returns_404(self):
        status, payload = self.post("/offers/unknown/dance", {"request_id": "req-z1"})
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_missing_fields_return_400(self):
        status, payload = self.post("/partners", {"request_id": "req-z2"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])


if __name__ == "__main__":
    unittest.main()
