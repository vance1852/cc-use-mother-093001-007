"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .commercialization import CommercializationService
from .service import DomainService
from .storage import Database


def run_commercialization(service: CommercializationService) -> dict[str, object]:
    """演练一条从意向到结算的完整合作链并返回摘要。"""

    service.register_actor(request_id="req-partner-user", actor_id="admin-001",
                           new_actor_id="partner-001", display_name="合作方联系人",
                           role="partner", organization_id="org-001")
    service.register_partner(request_id="req-partner", actor_id="operator-001",
                             partner_id="partner-museum", organization_id="org-001",
                             name="博物馆商店",
                             qualification={"license_no": "LS-2026-001", "scope": "文创零售"})
    service.link_partner_member(request_id="req-link", actor_id="admin-001",
                                partner_id="partner-museum", member_actor_id="partner-001")
    service.register_work(request_id="req-work", actor_id="operator-001", work_id="work-001",
                          organization_id="org-001", title="获奖作品《山河》",
                          award={"contest": "2026 文创赛", "level": "金奖"})
    service.add_design_version(request_id="req-dv1", actor_id="operator-001", work_id="work-001",
                               design_version_id="dv-1", version_no=1, summary="首发设计",
                               spec={"colors": ["青", "金"]})
    service.open_negotiation(request_id="req-neg", actor_id="operator-001",
                             negotiation_id="neg-001", work_id="work-001",
                             partner_id="partner-museum", note="博物馆商店合作意向")
    terms = {
        "design_version_id": "dv-1",
        "quote": {"currency": "CNY", "unit_price_cents": 19900},
        "exclusive": True,
        "territories": ["CN-East"],
        "channels": ["museum-store"],
        "min_commitment": {"quantity": 500, "period": "yearly"},
        "cost_items": [{"name": "生产成本", "kind": "fixed", "amount_cents": 200000},
                       {"name": "渠道扣点", "kind": "rate", "rate_bps": 500}],
        "shares": [{"member": "partner", "bps": 6000}, {"member": "op-team", "bps": 4000}],
        "milestones": [{"key": "sample-confirm", "title": "样品确认", "gate": True, "depends_on": []},
                       {"key": "first-delivery", "title": "首批交付", "gate": False,
                        "depends_on": ["sample-confirm"]}],
        "required_signers": ["operator-001", "partner-001"],
    }
    offer = service.make_offer(request_id="req-offer-1", actor_id="operator-001",
                               negotiation_id="neg-001", side="team", **terms)
    counter_terms = dict(terms, shares=[{"member": "partner", "bps": 6500},
                                        {"member": "op-team", "bps": 3500}])
    counter = service.counter_offer(request_id="req-offer-2", actor_id="partner-001",
                                    offer_id=offer.resource_id, side="partner", **counter_terms)
    service.reserve_offer(request_id="req-reserve", actor_id="operator-001",
                          offer_id=counter.resource_id)
    service.release_offer(request_id="req-release", actor_id="operator-001",
                          offer_id=counter.resource_id)
    accepted = service.accept_offer(request_id="req-accept", actor_id="operator-001",
                                    offer_id=counter.resource_id)
    contract_id = accepted.resource_id
    service.sign_contract(request_id="req-sign-1", actor_id="operator-001", contract_id=contract_id)
    service.sign_contract(request_id="req-sign-2", actor_id="partner-001", contract_id=contract_id)
    service.complete_milestone(request_id="req-ms-1", actor_id="operator-001",
                               contract_id=contract_id, milestone_key="sample-confirm")
    service.activate_contract(request_id="req-activate", actor_id="operator-001",
                              contract_id=contract_id)
    service.record_delivery(request_id="req-delivery", actor_id="operator-001",
                            contract_id=contract_id, milestone_key="first-delivery",
                            quantity=200, note="首批部分交付")
    service.add_design_version(request_id="req-dv2", actor_id="operator-001", work_id="work-001",
                               design_version_id="dv-2", version_no=2, summary="节庆改稿",
                               spec={"colors": ["红", "金"]})
    service.record_design_change(request_id="req-change", actor_id="operator-001",
                                 contract_id=contract_id, design_version_id="dv-2",
                                 reason="节庆限定包装")
    settlement = service.create_settlement(request_id="req-settle", actor_id="operator-001",
                                           contract_id=contract_id, period="2026-Q4",
                                           gross_amount_cents=1000000)
    service.dispute_settlement(request_id="req-dispute", actor_id="partner-001",
                               settlement_id=settlement.resource_id, reason="渠道扣点口径待复核")
    rights = service.rights_at(actor_id="admin-001", work_id="work-001", at="2026-09-25T09:00:00Z")
    explanation = service.explain_settlement(actor_id="admin-001",
                                             settlement_id=settlement.resource_id)
    view = service.get_contract_view(actor_id="operator-001", contract_id=contract_id)
    return {"contract_status": view["status"], "current_design": view["design_version_id"],
            "signed_design": view["signed_design_version_id"],
            "settlement_net_cents": explanation["net_cents"],
            "settlement_cost_version": explanation["cost_version_no"],
            "rights_cells": len(rights["occupied"])}


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = CommercializationService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范项目机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="项目负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号项目节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="program_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="program_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        commercialization = run_commercialization(service)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed, "commercialization": commercialization}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
