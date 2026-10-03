"""作品商品化合作管理服务的统一门面。

组合登记、谈判、履约三个子服务，并按操作者身份投影最小必要信息。
"""

from __future__ import annotations

import json
from typing import Any

from .clock import Clock
from .errors import NotFoundError, PermissionDenied
from .merch_common import MerchBase
from .merch_negotiation import NegotiationService
from .merch_performance import PerformanceService
from .merch_registry import RegistryService
from .storage import Database


class MerchService(MerchBase):
    """对外暴露的商品化合作服务。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        super().__init__(database, clock)
        self.registry = RegistryService(database, self.clock)
        self.negotiation = NegotiationService(database, self.clock)
        self.performance = PerformanceService(database, self.clock)

    # -- 身份与数据范围 ----------------------------------------------------------

    def _scope(self, conn, actor_id: str) -> dict[str, Any]:
        actor = self.actor(conn, actor_id)
        team_rows = conn.execute(
            "SELECT team_id FROM team_members WHERE actor_id=? AND active=1", (actor_id,)
        ).fetchall()
        partner_rows = conn.execute(
            "SELECT partner_id FROM partner_representatives WHERE actor_id=? AND active=1",
            (actor_id,)).fetchall()
        return {"actor": actor,
                "team_ids": {row["team_id"] for row in team_rows},
                "partner_ids": {row["partner_id"] for row in partner_rows}}

    def _can_see_work(self, scope: dict[str, Any], team_id: str) -> bool:
        return (scope["actor"].role in ("admin", "operator", "auditor")
                or team_id in scope["team_ids"])

    def _can_see_partner(self, scope: dict[str, Any], partner_id: str) -> bool:
        return (scope["actor"].role in ("admin", "operator", "auditor")
                or partner_id in scope["partner_ids"])

    # -- 列表 --------------------------------------------------------------------

    def list_offers(self, *, actor_id: str, work_id: str | None = None,
                    thread_id: str | None = None,
                    status: str | None = None) -> list[dict[str, Any]]:
        conn = self.database.connection
        scope = self._scope(conn, actor_id)
        query = ("SELECT o.*, w.team_id FROM offers o JOIN works w ON w.work_id=o.work_id WHERE 1=1")
        params: list[Any] = []
        if work_id:
            query += " AND o.work_id=?"
            params.append(work_id)
        if thread_id:
            query += " AND o.thread_id=?"
            params.append(thread_id)
        if status:
            query += " AND o.status=?"
            params.append(status)
        query += " ORDER BY o.thread_id, o.sequence_no"
        items: list[dict[str, Any]] = []
        for row in conn.execute(query, params):
            if not self._can_see_work(scope, row["team_id"]) and not self._can_see_partner(
                    scope, row["partner_id"]):
                continue
            items.append(self._offer_projection(scope, row))
        return items

    def list_contracts(self, *, actor_id: str, work_id: str | None = None,
                       status: str | None = None) -> list[dict[str, Any]]:
        conn = self.database.connection
        scope = self._scope(conn, actor_id)
        query = "SELECT * FROM contracts WHERE 1=1"
        params: list[Any] = []
        if work_id:
            query += " AND work_id=?"
            params.append(work_id)
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY accepted_at, contract_id"
        items: list[dict[str, Any]] = []
        for row in conn.execute(query, params):
            if not self._can_see_work(scope, row["team_id"]) and not self._can_see_partner(
                    scope, row["partner_id"]):
                continue
            items.append(self._contract_projection(scope, row))
        return items

    # -- 单对象视图 ---------------------------------------------------------------

    def authorize_contract(self, actor_id: str, contract_id: str):
        """校验操作者能否查看该合同，返回合同行；无关方拒绝。"""

        conn = self.database.connection
        scope = self._scope(conn, actor_id)
        row = conn.execute("SELECT * FROM contracts WHERE contract_id=?",
                           (contract_id,)).fetchone()
        if row is None:
            raise NotFoundError("合同不存在")
        if not self._can_see_work(scope, row["team_id"]) and not self._can_see_partner(
                scope, row["partner_id"]):
            raise PermissionDenied("无权查看该合同")
        return row

    def authorize_settlement_explain(self, actor_id: str, settlement_id: str):
        """结算解释含团队内部成员份额，仅团队与平台/审计可见。"""

        conn = self.database.connection
        scope = self._scope(conn, actor_id)
        row = conn.execute(
            "SELECT c.team_id FROM settlements s JOIN contracts c "
            "ON c.contract_id=s.contract_id WHERE s.settlement_id=?",
            (settlement_id,)).fetchone()
        if row is None:
            raise NotFoundError("结算不存在")
        if not self._can_see_work(scope, row["team_id"]):
            raise PermissionDenied("无权查看结算的成本与成员份额明细")

    def authorize_work_occupancy(self, actor_id: str, work_id: str) -> None:
        """占用明细含第三方独家范围，仅作品团队、平台与审计可见。"""

        conn = self.database.connection
        scope = self._scope(conn, actor_id)
        row = conn.execute("SELECT team_id FROM works WHERE work_id=?", (work_id,)).fetchone()
        if row is None:
            raise NotFoundError("作品不存在")
        if not self._can_see_work(scope, row["team_id"]):
            raise PermissionDenied("无权查看该作品的权利占用")

    def authorize_version_readiness(self, actor_id: str, version_id: str) -> None:
        """版本就绪状态对团队、平台/审计和引用该版本要约的合作方可见。"""

        conn = self.database.connection
        scope = self._scope(conn, actor_id)
        row = conn.execute(
            "SELECT w.team_id FROM design_versions dv JOIN works w ON w.work_id=dv.work_id "
            "WHERE dv.version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("设计版本不存在")
        if self._can_see_work(scope, row["team_id"]):
            return
        linked = conn.execute(
            "SELECT 1 FROM offers o WHERE o.design_version_id=? AND o.partner_id "
            "IN (SELECT partner_id FROM partner_representatives WHERE actor_id=? AND active=1) "
            "LIMIT 1", (version_id, actor_id)).fetchone()
        if linked is None:
            raise PermissionDenied("无权查看该设计版本的就绪状态")

    def offer_view(self, actor_id: str, offer_id: str) -> dict[str, Any]:
        conn = self.database.connection
        scope = self._scope(conn, actor_id)
        row = conn.execute(
            "SELECT o.*, w.team_id FROM offers o JOIN works w ON w.work_id=o.work_id "
            "WHERE o.offer_id=?", (offer_id,)).fetchone()
        if row is None:
            raise NotFoundError("要约不存在")
        if not self._can_see_work(scope, row["team_id"]) and not self._can_see_partner(
                scope, row["partner_id"]):
            raise PermissionDenied("无权查看该要约")
        return self._offer_projection(scope, row)

    def contract_view(self, actor_id: str, contract_id: str) -> dict[str, Any]:
        conn = self.database.connection
        scope = self._scope(conn, actor_id)
        row = conn.execute("SELECT * FROM contracts WHERE contract_id=?",
                           (contract_id,)).fetchone()
        if row is None:
            raise NotFoundError("合同不存在")
        if not self._can_see_work(scope, row["team_id"]) and not self._can_see_partner(
                scope, row["partner_id"]):
            raise PermissionDenied("无权查看该合同")
        return self._contract_projection(scope, row)

    def settlement_view(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        conn = self.database.connection
        scope = self._scope(conn, actor_id)
        row = conn.execute(
            "SELECT s.*, c.team_id, c.partner_id FROM settlements s JOIN contracts c "
            "ON c.contract_id=s.contract_id WHERE s.settlement_id=?",
            (settlement_id,)).fetchone()
        if row is None:
            raise NotFoundError("结算不存在")
        if not self._can_see_work(scope, row["team_id"]) and not self._can_see_partner(
                scope, row["partner_id"]):
            raise PermissionDenied("无权查看该结算")
        view = self.performance.get_settlement(settlement_id)
        if not self._can_see_work(scope, row["team_id"]):
            # 合作方视角：隐去团队内部成员份额明细，仅保留团队合计。
            view["entries"] = [e for e in view["entries"] if e["entry_kind"] != "team_member"]
            view["detail"].pop("member_entries", None)
        return view

    # -- 投影 --------------------------------------------------------------------

    def _offer_projection(self, scope: dict[str, Any], row) -> dict[str, Any]:
        terms = json.loads(row["terms_json"])
        is_team = self._can_see_work(scope, row["team_id"])
        item = {
            "offer_id": row["offer_id"], "thread_id": row["thread_id"],
            "sequence_no": row["sequence_no"], "prev_offer_id": row["prev_offer_id"],
            "intention_id": row["intention_id"], "work_id": row["work_id"],
            "partner_id": row["partner_id"], "direction": row["direction"],
            "design_version_id": row["design_version_id"], "terms": terms,
            "terms_hash": row["terms_hash"], "status": row["status"],
            "valid_until": row["valid_until"], "created_by": row["created_by"],
            "created_at": row["created_at"],
        }
        if is_team or scope["actor"].role in ("admin", "operator", "auditor"):
            item["cost_sheet_id"] = row["cost_sheet_id"]
            item["share_sheet_id"] = row["share_sheet_id"]
        else:
            # 合作方视角：保留商务条款，隐去团队内部口径表编号。
            item["terms"] = {k: v for k, v in terms.items() if k not in ("milestones",)}
        signatures = self.database.connection.execute(
            "SELECT party,actor_id,created_at FROM offer_signatures WHERE offer_id=?",
            (row["offer_id"],)).fetchall()
        item["signatures"] = [dict(s) for s in signatures]
        return item

    def _contract_projection(self, scope: dict[str, Any], row) -> dict[str, Any]:
        terms = json.loads(row["terms_json"])
        is_team = self._can_see_work(scope, row["team_id"])
        pointers = self.performance._effective_pointers(self.database.connection,
                                                        row["contract_id"])
        item = {
            "contract_id": row["contract_id"], "status": row["status"],
            "work_id": row["work_id"], "partner_id": row["partner_id"],
            "terms": terms, "terms_hash": row["terms_hash"],
            "accepted_at": row["accepted_at"], "effective_date": row["effective_date"],
            "end_date": row["end_date"], "terminated_at": row["terminated_at"],
            "current_design_version_id": pointers["design_version_id"],
        }
        if is_team or scope["actor"].role in ("admin", "operator", "auditor"):
            item.update({
                "team_id": row["team_id"],
                "original_design_version_id": row["design_version_id"],
                "current_cost_sheet_id": pointers["cost_sheet_id"],
                "current_share_sheet_id": pointers["share_sheet_id"],
                "cost_sheet_hash": row["cost_sheet_hash"],
                "share_sheet_hash": row["share_sheet_hash"],
                "signed_by_team": row["signed_by_team"],
                "signed_by_partner": row["signed_by_partner"],
            })
        return item

    # -- 审计解释 ----------------------------------------------------------------

    def explain_settlement(self, settlement_id: str) -> dict[str, Any]:
        """解释某笔分成采用了哪一版成本和成员份额。"""

        view = self.performance.get_settlement(settlement_id)
        cost = self.database.connection.execute(
            "SELECT * FROM cost_sheets WHERE cost_sheet_id=?",
            (view["cost_sheet_id"],)).fetchone()
        share = self.database.connection.execute(
            "SELECT * FROM share_sheets WHERE share_sheet_id=?",
            (view["share_sheet_id"],)).fetchone()
        return {
            "settlement_id": settlement_id,
            "used_cost_sheet": {"cost_sheet_id": cost["cost_sheet_id"],
                                "work_id": cost["work_id"],
                                "version_no": cost["version_no"],
                                "sheet_hash": cost["sheet_hash"],
                                "lines": json.loads(cost["lines_json"])},
            "used_share_sheet": {"share_sheet_id": share["share_sheet_id"],
                                 "team_id": share["team_id"],
                                 "version_no": share["version_no"],
                                 "sheet_hash": share["sheet_hash"],
                                 "lines": json.loads(share["lines_json"])},
            "cost_hash_matches": cost["sheet_hash"] == view["cost_sheet_hash"],
            "share_hash_matches": share["sheet_hash"] == view["share_sheet_hash"],
            "computation": view["detail"],
            "entries": view["entries"],
        }
