"""作品商品化合作管理服务的离线端到端验收。

在临时 SQLite 数据库中走完登记、谈判、独家冲突、会签、履约、结算与争议，
并核对幂等重放、审计链和权利占用时点解释，成功输出一行 status=ok 的 JSON。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .merch_service import MerchService
from .service import DomainService
from .storage import Database

START = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)


def _terms(**overrides):
    base = {
        "currency": "CNY", "exclusive": True, "territories": ["CN"],
        "channels": ["museum_shop"], "effective_date": "2026-10-10",
        "end_date": "2027-10-09", "price": "99.00",
        "minimum_purchase": {"quantity": "1000", "unit": "件"},
        "royalty": {"mode": "rate", "rate": "0.2", "deduct_costs": True,
                    "guarantee_amount": "5000.00"},
        "sample_required": True,
    }
    base.update(overrides)
    return base


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "merch_acceptance.sqlite3")
        clock = FixedClock(START)
        base = DomainService(db, clock)
        merch = MerchService(db, clock)
        R, N, P = merch.registry, merch.negotiation, merch.performance

        def tick(minutes: int) -> None:
            advanced = FixedClock(START + timedelta(minutes=minutes))
            base.clock = advanced
            for service in (merch, R, N, P):
                service.clock = advanced

        base.register_organization(request_id="org", actor_id="bootstrap",
                                    organization_id="o1", name="赛事转化机构")
        base.register_actor(request_id="admin1", actor_id="bootstrap", new_actor_id="admin1",
                            display_name="系统管理员", role="admin", organization_id="o1")
        base.register_actor(request_id="op-actor", actor_id="admin1", new_actor_id="op1",
                            display_name="运营专员", role="operator", organization_id="o1")
        for suffix, role, name in (("d", "team_member", "设计师"),
                                   ("l", "team_member", "法务"),
                                   ("s", "team_member", "签约代表")):
            base.register_actor(request_id=f"u-{suffix}", actor_id="admin1",
                                new_actor_id=f"u_{suffix}", display_name=name, role=role,
                                organization_id="o1")
        for index, ptype in enumerate(("museum_shop", "tea_brand", "ecommerce"), start=1):
            org = f"po{index}"
            base.register_organization(request_id=f"org-{index}", actor_id="admin1",
                                       organization_id=org, name=f"合作机构{index}")
            base.register_actor(request_id=f"pa-{index}", actor_id="admin1",
                                new_actor_id=f"rep{index}", display_name=f"代表{index}",
                                role="partner", organization_id=org)
            R.register_partner(request_id=f"p-{index}", actor_id="op1",
                               partner_id=f"partner{index}", organization_id=org,
                               partner_type=ptype, name=f"合作方{index}")
            R.review_qualification(request_id=f"q-{index}", actor_id="op1",
                                   partner_id=f"partner{index}", decision="approved",
                                   valid_until="2027-12-31")
            R.bind_representative(request_id=f"b-{index}", actor_id="op1",
                                  partner_id=f"partner{index}",
                                  representative_actor_id=f"rep{index}", can_sign=True)

        R.register_team(request_id="team", actor_id="op1", team_id="team1",
                        organization_id="o1", name="金奖团队",
                        required_countersign_roles=["designer", "legal"])
        R.add_team_member(request_id="tm-d", actor_id="op1", team_id="team1",
                          member_actor_id="u_d", member_role="designer")
        R.add_team_member(request_id="tm-l", actor_id="op1", team_id="team1",
                          member_actor_id="u_l", member_role="legal")
        R.add_team_member(request_id="tm-s", actor_id="op1", team_id="team1",
                          member_actor_id="u_s", member_role="signer")
        R.grant_signing(request_id="grant", actor_id="op1", team_id="team1",
                        member_actor_id="u_s")
        R.register_work(request_id="work", actor_id="op1", work_id="work1",
                        team_id="team1", title="金奖纹样", award_name="文创金奖")
        R.add_design_version(request_id="v1", actor_id="u_d", work_id="work1",
                             version_code="v1", content_hash="sha256:v1",
                             prerequisites=[{"code": "ipr", "label": "权属核查"}])
        version_id = db.connection.execute(
            "SELECT version_id FROM design_versions").fetchone()["version_id"]
        R.countersign_version(request_id="sig-d", actor_id="u_d", version_id=version_id)
        R.countersign_version(request_id="sig-l", actor_id="u_l", version_id=version_id)
        R.complete_version_prerequisite(request_id="ipr", actor_id="op1",
                                        version_id=version_id, code="ipr")
        R.register_cost_sheet(
            request_id="cost", actor_id="op1", work_id="work1",
            lines=[{"label": "打样", "amount": "1000.00", "basis": "fixed"},
                   {"label": "单件包装", "amount": "2.00", "basis": "unit"}])
        cost_id = db.connection.execute(
            "SELECT cost_sheet_id FROM cost_sheets").fetchone()["cost_sheet_id"]
        R.register_share_sheet(
            request_id="share", actor_id="op1", team_id="team1",
            lines=[{"actor_id": "u_d", "ratio": "0.6"},
                   {"actor_id": "u_l", "ratio": "0.4"}])
        share_id = db.connection.execute(
            "SELECT share_sheet_id FROM share_sheets").fetchone()["share_sheet_id"]

        N.create_intention(request_id="int1", actor_id="rep1", work_id="work1",
                           partner_id="partner1", channels=["museum_shop"])
        intention1 = db.connection.execute(
            "SELECT intention_id FROM intentions WHERE partner_id='partner1'").fetchone()["intention_id"]
        tick(1)
        N.make_offer(request_id="offer1", actor_id="rep1", work_id="work1",
                     partner_id="partner1", direction="inbound", intention_id=intention1,
                     design_version_id=version_id, cost_sheet_id=cost_id,
                     share_sheet_id=share_id, terms=_terms(),
                     valid_until="2026-10-20T00:00:00Z")
        offer1 = db.connection.execute(
            "SELECT offer_id FROM offers ORDER BY sequence_no LIMIT 1").fetchone()["offer_id"]
        tick(2)
        N.reserve_offer(request_id="res1", actor_id="op1", offer_id=offer1,
                        reserve_until="2026-10-09T00:00:00Z")

        # 第二家在同一地域/渠道上保留，必须被独家冲突拦截。
        N.create_intention(request_id="int2", actor_id="rep2", work_id="work1",
                           partner_id="partner2", channels=["museum_shop"])
        intention2 = db.connection.execute(
            "SELECT intention_id FROM intentions WHERE partner_id='partner2'").fetchone()["intention_id"]
        tick(3)
        N.make_offer(request_id="offer2", actor_id="rep2", work_id="work1",
                     partner_id="partner2", direction="inbound", intention_id=intention2,
                     design_version_id=version_id, cost_sheet_id=cost_id,
                     share_sheet_id=share_id, terms=_terms(),
                     valid_until="2026-10-20T00:00:00Z")
        offer2 = db.connection.execute(
            "SELECT offer_id FROM offers WHERE partner_id='partner2'").fetchone()["offer_id"]
        conflict_blocked = False
        try:
            N.reserve_offer(request_id="res2", actor_id="rep2", offer_id=offer2,
                            reserve_until="2026-10-08T00:00:00Z")
        except Exception:
            conflict_blocked = True
        assert conflict_blocked, "独家冲突必须被拦截"

        N.release_reservation(request_id="rel1", actor_id="op1", offer_id=offer1)
        tick(4)
        N.make_offer(request_id="offer1b", actor_id="op1", work_id="work1",
                     partner_id="partner1", direction="outbound", intention_id=intention1,
                     design_version_id=version_id, cost_sheet_id=cost_id,
                     share_sheet_id=share_id,
                     terms=_terms(royalty={"mode": "rate", "rate": "0.25",
                                           "deduct_costs": True,
                                           "guarantee_amount": "5000.00"}),
                     valid_until="2026-10-21T00:00:00Z", prev_offer_id=offer1)
        offer1b = db.connection.execute(
            "SELECT offer_id FROM offers ORDER BY sequence_no DESC LIMIT 1").fetchone()["offer_id"]
        tick(5)
        N.sign_offer(request_id="sign-t", actor_id="u_s", offer_id=offer1b, party="team")
        N.sign_offer(request_id="sign-p", actor_id="rep1", offer_id=offer1b, party="partner")
        tick(6)
        N.accept_offer(request_id="accept", actor_id="op1", offer_id=offer1b)
        contract_id = db.connection.execute(
            "SELECT contract_id FROM contracts").fetchone()["contract_id"]

        occupancy = N.rights_occupancy("work1")
        assert len(occupancy["occupancy"]) == 1, "生效合同应占用独家权利"
        before = N.rights_occupancy("work1", as_of="2026-10-03T08:00:30Z")
        assert before["occupancy"] == [], "历史时点不应有占用"

        tick(7)
        P.confirm_sample(request_id="sample", actor_id="rep1", contract_id=contract_id,
                         approved=True)
        tick(8)
        P.record_delivery(request_id="del1", actor_id="rep1", contract_id=contract_id,
                          milestone_code="delivery", quantity_value="600")
        tick(9)
        replay_delivery = P.record_delivery(
            request_id="del1", actor_id="rep1", contract_id=contract_id,
            milestone_code="delivery", quantity_value="600")
        assert replay_delivery.replayed, "重复交付回调必须重放"
        delivered = db.connection.execute(
            "SELECT COALESCE(SUM(CAST(quantity AS REAL)),0) AS total FROM deliveries"
        ).fetchone()["total"]
        assert delivered == 600, f"重复回调不得重复记账，实际 {delivered}"
        P.record_delivery(request_id="del2", actor_id="rep1", contract_id=contract_id,
                          milestone_code="delivery", quantity_value="400")

        tick(10)
        P.generate_settlement(request_id="settle", actor_id="op1",
                              contract_id=contract_id, gross_revenue="50000",
                              period_start="2026-10-01", period_end="2026-10-31")
        replay_settle = P.generate_settlement(
            request_id="settle", actor_id="op1", contract_id=contract_id,
            gross_revenue="50000", period_start="2026-10-01", period_end="2026-10-31")
        assert replay_settle.replayed, "重复结算回调必须重放"
        settlement_count = db.connection.execute(
            "SELECT COUNT(*) AS c FROM settlements").fetchone()["c"]
        entry_count = db.connection.execute(
            "SELECT COUNT(*) AS c FROM settlement_entries").fetchone()["c"]
        assert settlement_count == 1 and entry_count == 3, "重复回调不得重复生成结算分录"
        settlement_id = db.connection.execute(
            "SELECT settlement_id FROM settlements").fetchone()["settlement_id"]
        explanation = merch.explain_settlement(settlement_id)
        assert explanation["cost_hash_matches"] and explanation["share_hash_matches"], \
            "结算必须能解释所用成本与份额版本"
        settlement = P.get_settlement(settlement_id)
        # 成本 1000 + 2*1000 = 3000，净额 47000，团队 25% = 11750。
        assert settlement["team_amount"] == "11750.00", settlement["team_amount"]

        P.dispute_settlement(request_id="dispute", actor_id="rep1",
                             settlement_id=settlement_id, description="收入口径待核")
        dispute_id = db.connection.execute(
            "SELECT dispute_id FROM settlement_disputes").fetchone()["dispute_id"]
        tick(11)
        P.resolve_dispute(request_id="resolve", actor_id="op1", dispute_id=dispute_id,
                          resolution="维持原口径", action="uphold")

        tick(12)
        P.report_breach(request_id="breach", actor_id="op1", contract_id=contract_id,
                        party="partner", description="包装延误",
                        remedy_deadline="2026-11-10")
        breach_id = db.connection.execute(
            "SELECT breach_id FROM breaches").fetchone()["breach_id"]
        P.submit_remediation(request_id="remedy", actor_id="rep1", breach_id=breach_id,
                             note="已补发并整改")
        P.resolve_breach(request_id="breach-ok", actor_id="op1", breach_id=breach_id,
                         accepted=True)
        tick(13)
        P.terminate_contract(request_id="terminate", actor_id="op1",
                             contract_id=contract_id, reason="到期结清")
        assert N.rights_occupancy("work1")["occupancy"] == [], "终止后权利应释放"

        facts = P.list_facts(contract_id)
        assert len(facts) >= 10, "履约事实应完整追加"
        valid, audit_count = base.verify_audit()
        partner_view = merch.settlement_view("rep1", settlement_id)
        assert all(e["entry_kind"] != "team_member" for e in partner_view["entries"]), \
            "合作方视角不得暴露团队成员份额明细"

        result = {
            "status": "ok", "audit_valid": valid, "audit_events": audit_count,
            "contract_facts": len(facts),
            "settlement_team_amount": settlement["team_amount"],
            "settlements": settlement_count, "settlement_entries": entry_count,
            "occupancy_after_termination": 0,
            "conflict_blocked": conflict_blocked,
            "partner_entries_minimized": True,
        }
        db.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
