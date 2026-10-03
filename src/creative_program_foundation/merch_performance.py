"""合同履约：样品、部分交付、里程碑、变更、违约、终止与结算。"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .audit import canonical_json
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .merch_common import MerchBase, new_id
from .merch_money import compute_settlement, money, quantity
from .models import WriteReceipt


class PerformanceService(MerchBase):
    """在不可变合同快照上追加履约事实并生成可解释结算。"""

    # -- 内部装载 ---------------------------------------------------------------

    def _active_contract(self, conn, contract_id: str):
        contract = self.require_row(conn, "contracts", "contract_id", contract_id, "合同不存在")
        if contract["status"] != "active":
            raise ConflictError("合同已经终止")
        return contract

    def _party_check(self, conn, actor, contract, *, team: bool = True, partner: bool = True) -> None:
        is_team = self.team_member(conn, actor, contract["team_id"]) is not None
        is_partner = self.partner_rep(conn, actor, contract["partner_id"]) is not None
        allowed = actor.role in ("admin", "operator") or (team and is_team) or (partner and is_partner)
        if not allowed:
            raise PermissionDenied("当前操作者与该合同无关")

    def _terms(self, contract) -> dict[str, Any]:
        return json.loads(contract["terms_json"])

    def _effective_pointers(self, conn, contract_id: str) -> dict[str, str]:
        """依据已批准变更单推导当前生效的版本与口径指针。"""

        contract = self.require_row(conn, "contracts", "contract_id", contract_id, "合同不存在")
        pointers = {"design_version_id": contract["design_version_id"],
                    "cost_sheet_id": contract["cost_sheet_id"],
                    "share_sheet_id": contract["share_sheet_id"]}
        for row in conn.execute(
                "SELECT * FROM change_orders WHERE contract_id=? AND status='approved' "
                "ORDER BY created_at, rowid", (contract_id,)):
            if row["new_design_version_id"]:
                pointers["design_version_id"] = row["new_design_version_id"]
            if row["new_cost_sheet_id"]:
                pointers["cost_sheet_id"] = row["new_cost_sheet_id"]
            if row["new_share_sheet_id"]:
                pointers["share_sheet_id"] = row["new_share_sheet_id"]
        return pointers

    def _effective_version_set(self, conn, contract) -> set[str]:
        versions = {contract["design_version_id"]}
        for row in conn.execute(
                "SELECT new_design_version_id FROM change_orders "
                "WHERE contract_id=? AND status='approved' AND new_design_version_id IS NOT NULL",
                (contract["contract_id"],)):
            versions.add(row["new_design_version_id"])
        return versions

    def _append_fact(self, conn, *, contract_id: str, fact_type: str, payload: dict[str, Any],
                     created_by: str) -> str:
        next_seq = conn.execute(
            "SELECT COALESCE(MAX(sequence_no),0)+1 AS next FROM contract_facts WHERE contract_id=?",
            (contract_id,)).fetchone()["next"]
        fact_id = new_id()
        now_value = self.now_text()
        conn.execute(
            "INSERT INTO contract_facts(fact_id,contract_id,sequence_no,fact_type,payload_json,"
            "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (fact_id, contract_id, next_seq, fact_type, canonical_json(payload), created_by, now_value))
        return fact_id

    # -- 里程碑状态 --------------------------------------------------------------

    def _delivered_range(self, conn, contract_id: str, *, start: str | None,
                         end: str | None) -> Decimal:
        """统计某结算周期 [start, end] 内的交付量。"""

        clauses = ["contract_id=?"]
        params: list[Any] = [contract_id]
        if start:
            clauses.append("created_at>=?")
            params.append(start + "T00:00:00Z")
        if end:
            clauses.append("created_at<=?")
            params.append(end + "T23:59:59Z")
        rows = conn.execute(
            f"SELECT quantity FROM deliveries WHERE {' AND '.join(clauses)}", params).fetchall()
        return sum((Decimal(row["quantity"]) for row in rows), Decimal("0"))

    def _milestone_rows(self, conn, contract_id: str):
        return conn.execute(
            "SELECT * FROM contract_milestones WHERE contract_id=? ORDER BY sequence_no",
            (contract_id,)).fetchall()

    def _delivered_total(self, conn, contract_id: str, up_to: str | None = None) -> Decimal:
        if up_to:
            rows = conn.execute(
                "SELECT quantity FROM deliveries WHERE contract_id=? AND created_at<=?",
                (contract_id, up_to)).fetchall()
        else:
            rows = conn.execute("SELECT quantity FROM deliveries WHERE contract_id=?",
                                (contract_id,)).fetchall()
        return sum((Decimal(row["quantity"]) for row in rows), Decimal("0"))

    def _milestone_status(self, conn, contract, milestone) -> dict[str, Any]:
        deps = json.loads(milestone["depends_on_json"])
        rows_by_code = {row["code"]: row for row in self._milestone_rows(conn, contract["contract_id"])}
        completed_deps: list[str] = []
        for dep in deps:
            dep_row = rows_by_code.get(dep)
            if dep_row is None:
                continue
            dep_status = self._milestone_status(conn, contract, dep_row)
            if dep_status["completed"]:
                completed_deps.append(dep)
        deps_ready = len(completed_deps) == len(deps)
        completed = False
        detail: dict[str, Any] = {}
        if milestone["kind"] == "sample":
            row = conn.execute(
                "SELECT 1 FROM sample_confirmations WHERE contract_id=? AND approved=1 LIMIT 1",
                (contract["contract_id"],)).fetchone()
            completed = row is not None
        elif milestone["kind"] == "delivery":
            total = self._delivered_total(conn, contract["contract_id"])
            planned = Decimal(milestone["planned_qty"])
            detail["delivered_qty"] = str(total)
            detail["planned_qty"] = str(planned)
            completed = deps_ready and total >= planned
        elif milestone["kind"] == "approval":
            row = conn.execute(
                "SELECT payload_json FROM contract_facts WHERE contract_id=? AND fact_type='milestone_approval' "
                "AND json_extract(payload_json,'$.code')=? ORDER BY sequence_no DESC LIMIT 1",
                (contract["contract_id"], milestone["code"])).fetchone()
            completed = row is not None and deps_ready
            if row:
                detail["approval"] = json.loads(row["payload_json"])
        fact = conn.execute(
            "SELECT 1 FROM contract_facts WHERE contract_id=? AND fact_type='milestone_completed' "
            "AND json_extract(payload_json,'$.code')=? LIMIT 1",
            (contract["contract_id"], milestone["code"])).fetchone()
        return {"code": milestone["code"], "label": milestone["label"], "kind": milestone["kind"],
                "sequence_no": milestone["sequence_no"], "planned_qty": milestone["planned_qty"],
                "depends_on": deps, "deps_ready": deps_ready,
                "completed": bool(completed or fact is not None),
                "recorded_completed": fact is not None, "detail": detail}

    def _completed_codes(self, conn, contract) -> set[str]:
        """返回当前已满足完成条件的里程碑编码集合。"""

        result: set[str] = set()
        for row in self._milestone_rows(conn, contract["contract_id"]):
            if self._milestone_status(conn, contract, row)["completed"]:
                result.add(row["code"])
        return result

    def _refresh_milestones(self, conn, contract, *, created_by: str) -> list[str]:
        """为新满足条件的里程碑追加完成事实。"""

        newly: list[str] = []
        for row in self._milestone_rows(conn, contract["contract_id"]):
            status = self._milestone_status(conn, contract, row)
            if status["completed"] and not status["recorded_completed"]:
                self._append_fact(conn, contract_id=contract["contract_id"],
                                  fact_type="milestone_completed",
                                  payload={"code": status["code"], "kind": status["kind"],
                                           "detail": status["detail"]},
                                  created_by=created_by)
                newly.append(status["code"])
        return newly

    # -- 样品 -------------------------------------------------------------------

    def confirm_sample(self, *, request_id: str, actor_id: str, contract_id: str,
                       approved: bool, design_version_id: str | None = None,
                       note: str | None = None) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "contract_id": contract_id,
                   "approved": approved, "design_version_id": design_version_id, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="confirm_sample", payload=payload)
            if _replay is not None:
                return _replay
            contract = self._active_contract(conn, contract_id)
            self._party_check(conn, actor, contract)
            pointers = self._effective_pointers(conn, contract_id)
            version_id = design_version_id or pointers["design_version_id"]
            if version_id not in self._effective_version_set(conn, contract):
                raise ValidationError("样品版本必须是合同当前生效的设计版本")
            if not self._version_ready(conn, version_id):
                raise ConflictError("该设计版本尚未满足会签与前置条件，不能进入履约")
            note_text = self.opt_text(note, "note", 1000)

            def create():
                sample_id = new_id()
                now_value = self.now_text()
                conn.execute(
                    "INSERT INTO sample_confirmations(sample_id,contract_id,design_version_id,"
                    "approved,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (sample_id, contract_id, version_id, 1 if approved else 0, note_text,
                     actor_id, now_value))
                self._append_fact(conn, contract_id=contract_id,
                                  fact_type="sample_confirmed",
                                  payload={"sample_id": sample_id, "approved": bool(approved),
                                           "design_version_id": version_id, "note": note_text},
                                  created_by=actor_id)
                newly = self._refresh_milestones(conn, contract, created_by=actor_id)
                self.audit(conn, actor_id=actor_id, action="sample.confirmed",
                           resource_type="contract", resource_id=contract_id,
                           detail={"sample_id": sample_id, "approved": bool(approved),
                                   "design_version_id": version_id,
                                   "milestones_completed": newly})
                return "sample", sample_id, {"sample_id": sample_id, "approved": bool(approved),
                                             "milestones_completed": newly}

            return self.idempotent(conn, request_id=request_id, action="confirm_sample",
                                   payload=payload, create=create)

    def approve_milestone(self, *, request_id: str, actor_id: str, contract_id: str,
                          code: str, note: str | None = None) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "contract_id": contract_id,
                   "code": code, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="approve_milestone", payload=payload)
            if _replay is not None:
                return _replay
            contract = self._active_contract(conn, contract_id)
            self._party_check(conn, actor, contract)
            milestone = conn.execute(
                "SELECT * FROM contract_milestones WHERE contract_id=? AND code=?",
                (contract_id, code)).fetchone()
            if milestone is None:
                raise NotFoundError("里程碑不存在")
            if milestone["kind"] != "approval":
                raise ValidationError("只有审批类里程碑需要手动确认")
            status = self._milestone_status(conn, contract, milestone)
            if not status["deps_ready"]:
                raise ConflictError("前置里程碑尚未全部完成")
            note_text = self.opt_text(note, "note", 1000)

            def create():
                fact_id = self._append_fact(conn, contract_id=contract_id,
                                            fact_type="milestone_approval",
                                            payload={"code": code, "note": note_text},
                                            created_by=actor_id)
                newly = self._refresh_milestones(conn, contract, created_by=actor_id)
                self.audit(conn, actor_id=actor_id, action="milestone.approved",
                           resource_type="contract", resource_id=contract_id,
                           detail={"code": code, "fact_id": fact_id,
                                   "milestones_completed": newly})
                return "milestone_approval", fact_id, {"fact_id": fact_id, "code": code,
                                                       "milestones_completed": newly}

            return self.idempotent(conn, request_id=request_id, action="approve_milestone",
                                   payload=payload, create=create)

    # -- 部分交付 ----------------------------------------------------------------

    def record_delivery(self, *, request_id: str, actor_id: str, contract_id: str,
                        milestone_code: str, quantity_value: Any,
                        design_version_id: str | None = None) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "contract_id": contract_id,
                   "milestone_code": milestone_code, "quantity": str(quantity_value),
                   "design_version_id": design_version_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="record_delivery", payload=payload)
            if _replay is not None:
                return _replay
            contract = self._active_contract(conn, contract_id)
            self._party_check(conn, actor, contract)
            milestone = conn.execute(
                "SELECT * FROM contract_milestones WHERE contract_id=? AND code=?",
                (contract_id, milestone_code)).fetchone()
            if milestone is None:
                raise NotFoundError("交付里程碑不存在")
            if milestone["kind"] != "delivery":
                raise ValidationError("只能向交付类里程碑登记交付")
            status = self._milestone_status(conn, contract, milestone)
            if not status["deps_ready"]:
                unmet = [dep for dep in status["depends_on"]
                         if dep not in self._completed_codes(conn, contract)]
                raise ConflictError(f"前置里程碑 {unmet} 尚未全部完成")
            try:
                qty = quantity(str(quantity_value), "quantity")
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            if qty <= 0:
                raise ValidationError("交付数量必须大于 0")
            pointers = self._effective_pointers(conn, contract_id)
            version_id = design_version_id or pointers["design_version_id"]
            if version_id not in self._effective_version_set(conn, contract):
                raise ValidationError("交付版本必须是合同生效过的设计版本")
            if not self._version_ready(conn, version_id):
                raise ConflictError("该设计版本尚未满足会签与前置条件，不能进入履约")

            def create():
                before_total = self._delivered_total(conn, contract_id)
                delivery_id = new_id()
                now_value = self.now_text()
                conn.execute(
                    "INSERT INTO deliveries(delivery_id,contract_id,milestone_code,"
                    "design_version_id,quantity,request_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (delivery_id, contract_id, milestone_code, version_id, str(qty),
                     request_id, actor_id, now_value))
                self._append_fact(conn, contract_id=contract_id, fact_type="delivery_recorded",
                                  payload={"delivery_id": delivery_id, "code": milestone_code,
                                           "quantity": str(qty),
                                           "cumulative_qty": str(before_total + qty),
                                           "design_version_id": version_id},
                                  created_by=actor_id)
                newly = self._refresh_milestones(conn, contract, created_by=actor_id)
                self.audit(conn, actor_id=actor_id, action="delivery.recorded",
                           resource_type="contract", resource_id=contract_id,
                           detail={"delivery_id": delivery_id, "milestone_code": milestone_code,
                                   "quantity": str(qty),
                                   "cumulative_qty": str(before_total + qty),
                                   "milestones_completed": newly})
                return "delivery", delivery_id, {"delivery_id": delivery_id,
                                                 "cumulative_qty": str(before_total + qty),
                                                 "milestones_completed": newly}

            return self.idempotent(conn, request_id=request_id, action="record_delivery",
                                   payload=payload, create=create)

    # -- 设计变更（只追加）--------------------------------------------------------

    def propose_change_order(self, *, request_id: str, actor_id: str, contract_id: str,
                             reason: str, new_design_version_id: str | None = None,
                             new_cost_sheet_id: str | None = None,
                             new_share_sheet_id: str | None = None) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "contract_id": contract_id,
                   "reason": reason, "new_design_version_id": new_design_version_id,
                   "new_cost_sheet_id": new_cost_sheet_id,
                   "new_share_sheet_id": new_share_sheet_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="propose_change_order", payload=payload)
            if _replay is not None:
                return _replay
            contract = self._active_contract(conn, contract_id)
            self._party_check(conn, actor, contract)
            reason_text = self.text(reason, "reason", 1000)
            if not any((new_design_version_id, new_cost_sheet_id, new_share_sheet_id)):
                raise ValidationError("变更单至少替换一项版本或口径")
            work = self.require_row(conn, "works", "work_id", contract["work_id"], "作品不存在")
            if new_design_version_id:
                version = self.require_row(conn, "design_versions", "version_id",
                                           new_design_version_id, "新设计版本不存在")
                if version["work_id"] != contract["work_id"]:
                    raise ValidationError("新设计版本不属于该作品")
                if not self._version_ready(conn, new_design_version_id):
                    raise ConflictError("新设计版本尚未满足会签与前置条件")
            if new_cost_sheet_id:
                sheet = self.require_row(conn, "cost_sheets", "cost_sheet_id",
                                         new_cost_sheet_id, "新成本口径不存在")
                if sheet["work_id"] != contract["work_id"]:
                    raise ValidationError("新成本口径不属于该作品")
            if new_share_sheet_id:
                sheet = self.require_row(conn, "share_sheets", "share_sheet_id",
                                         new_share_sheet_id, "新分成口径不存在")
                if sheet["team_id"] != contract["team_id"]:
                    raise ValidationError("新分成口径不属于该团队")

            def create():
                change_id = new_id()
                conn.execute(
                    "INSERT INTO change_orders(change_order_id,contract_id,new_design_version_id,"
                    "new_cost_sheet_id,new_share_sheet_id,reason,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,'proposed',?,?)",
                    (change_id, contract_id, new_design_version_id, new_cost_sheet_id,
                     new_share_sheet_id, reason_text, actor_id, self.now_text()))
                self.audit(conn, actor_id=actor_id, action="change_order.proposed",
                           resource_type="change_order", resource_id=change_id,
                           detail={"contract_id": contract_id, "reason": reason_text,
                                   "new_design_version_id": new_design_version_id,
                                   "new_cost_sheet_id": new_cost_sheet_id,
                                   "new_share_sheet_id": new_share_sheet_id})
                return "change_order", change_id, {"change_order_id": change_id}

            return self.idempotent(conn, request_id=request_id, action="propose_change_order",
                                   payload=payload, create=create)

    def sign_change_order(self, *, request_id: str, actor_id: str, change_order_id: str,
                          party: str) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id,
                   "change_order_id": change_order_id, "party": party}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="sign_change_order", payload=payload)
            if _replay is not None:
                return _replay
            change = self.require_row(conn, "change_orders", "change_order_id", change_order_id,
                                      "变更单不存在")
            if change["status"] != "proposed":
                raise ConflictError("变更单已经批复")
            contract = self._active_contract(conn, change["contract_id"])
            if party == "team":
                if actor.role != "admin" and self.team_signer(
                        conn, actor, contract["team_id"], contract["work_id"]) is None:
                    raise PermissionDenied("团队方签署需要有效签署授权")
                if change["team_signature"]:
                    raise ConflictError("团队已经签署")
            elif party == "partner":
                rep = self.partner_rep(conn, actor, contract["partner_id"])
                if rep is None or not rep["can_sign"]:
                    raise PermissionDenied("合作方签署人必须有签约权")
                if change["partner_signature"]:
                    raise ConflictError("合作方已经签署")
            else:
                raise ValidationError("party 只能是 team 或 partner")

            def create():
                now_value = self.now_text()
                if party == "team":
                    conn.execute(
                        "UPDATE change_orders SET team_signature=?, team_signed_at=? "
                        "WHERE change_order_id=?", (actor_id, now_value, change_order_id))
                else:
                    conn.execute(
                        "UPDATE change_orders SET partner_signature=?, partner_signed_at=? "
                        "WHERE change_order_id=?", (actor_id, now_value, change_order_id))
                self.audit(conn, actor_id=actor_id, action="change_order.signed",
                           resource_type="change_order", resource_id=change_order_id,
                           detail={"contract_id": contract["contract_id"], "party": party})
                return ("change_order_signature", f"{change_order_id}:{party}",
                        {"change_order_id": change_order_id, "party": party})

            return self.idempotent(conn, request_id=request_id, action="sign_change_order",
                                   payload=payload, create=create)

    def approve_change_order(self, *, request_id: str, actor_id: str,
                             change_order_id: str) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id,
                   "change_order_id": change_order_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="approve_change_order", payload=payload)
            if _replay is not None:
                return _replay
            change = self.require_row(conn, "change_orders", "change_order_id", change_order_id,
                                      "变更单不存在")
            if change["status"] != "proposed":
                raise ConflictError("变更单已经批复")
            contract = self._active_contract(conn, change["contract_id"])
            self._party_check(conn, actor, contract)
            if not change["team_signature"] or not change["partner_signature"]:
                raise ConflictError("变更单必须经双方签署后才能生效")
            pointers_before = self._effective_pointers(conn, contract["contract_id"])

            def create():
                now_value = self.now_text()
                conn.execute("UPDATE change_orders SET status='approved' WHERE change_order_id=?",
                             (change_order_id,))
                payload_detail = {
                    "previous_design_version_id": pointers_before["design_version_id"],
                    "previous_cost_sheet_id": pointers_before["cost_sheet_id"],
                    "previous_share_sheet_id": pointers_before["share_sheet_id"],
                    "new_design_version_id": change["new_design_version_id"],
                    "new_cost_sheet_id": change["new_cost_sheet_id"],
                    "new_share_sheet_id": change["new_share_sheet_id"],
                    "team_signature": change["team_signature"],
                    "partner_signature": change["partner_signature"],
                    "reason": change["reason"], "effective_at": now_value}
                self._append_fact(conn, contract_id=contract["contract_id"],
                                  fact_type="change_order_effective",
                                  payload=payload_detail, created_by=actor_id)
                self.audit(conn, actor_id=actor_id, action="change_order.approved",
                           resource_type="change_order", resource_id=change_order_id,
                           detail=payload_detail)
                return "change_order", change_order_id, {"change_order_id": change_order_id,
                                                         "status": "approved"}

            return self.idempotent(conn, request_id=request_id, action="approve_change_order",
                                   payload=payload, create=create)

    def reject_change_order(self, *, request_id: str, actor_id: str,
                            change_order_id: str) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id,
                   "change_order_id": change_order_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="reject_change_order", payload=payload)
            if _replay is not None:
                return _replay
            change = self.require_row(conn, "change_orders", "change_order_id", change_order_id,
                                      "变更单不存在")
            contract = self.require_row(conn, "contracts", "contract_id",
                                        change["contract_id"], "合同不存在")
            self._party_check(conn, actor, contract)
            if change["status"] != "proposed":
                raise ConflictError("变更单已经批复")

            def create():
                conn.execute("UPDATE change_orders SET status='rejected' WHERE change_order_id=?",
                             (change_order_id,))
                self._append_fact(conn, contract_id=contract["contract_id"],
                                  fact_type="change_order_rejected",
                                  payload={"change_order_id": change_order_id},
                                  created_by=actor_id)
                self.audit(conn, actor_id=actor_id, action="change_order.rejected",
                           resource_type="change_order", resource_id=change_order_id,
                           detail={"contract_id": contract["contract_id"]})
                return "change_order", change_order_id, {"change_order_id": change_order_id,
                                                         "status": "rejected"}

            return self.idempotent(conn, request_id=request_id, action="reject_change_order",
                                   payload=payload, create=create)

    # -- 违约与整改 ---------------------------------------------------------------

    def report_breach(self, *, request_id: str, actor_id: str, contract_id: str, party: str,
                      description: str, remedy_deadline: str | None = None) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "contract_id": contract_id,
                   "party": party, "description": description, "remedy_deadline": remedy_deadline}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="report_breach", payload=payload)
            if _replay is not None:
                return _replay
            contract = self._active_contract(conn, contract_id)
            self._party_check(conn, actor, contract)
            if party not in ("team", "partner"):
                raise ValidationError("party 只能是 team 或 partner")
            description_text = self.text(description, "description", 1000)
            deadline = self.day(remedy_deadline, "remedy_deadline") if remedy_deadline else None

            def create():
                seq = conn.execute(
                    "SELECT COALESCE(MAX(sequence_no),0)+1 AS next FROM breaches WHERE contract_id=?",
                    (contract_id,)).fetchone()["next"]
                breach_id = new_id()
                conn.execute(
                    "INSERT INTO breaches(breach_id,contract_id,sequence_no,party,description,"
                    "remedy_deadline,status,created_by,created_at) VALUES(?,?,?,?,?,?,'open',?,?)",
                    (breach_id, contract_id, seq, party, description_text, deadline,
                     actor_id, self.now_text()))
                self._append_fact(conn, contract_id=contract_id, fact_type="breach_reported",
                                  payload={"breach_id": breach_id, "sequence_no": seq,
                                           "party": party, "description": description_text,
                                           "remedy_deadline": deadline},
                                  created_by=actor_id)
                self.audit(conn, actor_id=actor_id, action="breach.reported",
                           resource_type="breach", resource_id=breach_id,
                           detail={"contract_id": contract_id, "party": party, "sequence_no": seq})
                return "breach", breach_id, {"breach_id": breach_id, "sequence_no": seq}

            return self.idempotent(conn, request_id=request_id, action="report_breach",
                                   payload=payload, create=create)

    def submit_remediation(self, *, request_id: str, actor_id: str, breach_id: str,
                           note: str) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "breach_id": breach_id,
                   "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="submit_remediation", payload=payload)
            if _replay is not None:
                return _replay
            breach = self.require_row(conn, "breaches", "breach_id", breach_id, "违约记录不存在")
            contract = self._active_contract(conn, breach["contract_id"])
            self._party_check(conn, actor, contract)
            if breach["status"] != "open":
                raise ConflictError("违约已经闭合")
            note_text = self.text(note, "note", 1000)

            def create():
                remediation_id = new_id()
                conn.execute(
                    "INSERT INTO breach_remediations(remediation_id,breach_id,note,accepted,"
                    "created_by,created_at) VALUES(?,?,?,0,?,?)",
                    (remediation_id, breach_id, note_text, actor_id, self.now_text()))
                self._append_fact(conn, contract_id=contract["contract_id"],
                                  fact_type="remediation_submitted",
                                  payload={"breach_id": breach_id,
                                           "remediation_id": remediation_id, "note": note_text},
                                  created_by=actor_id)
                self.audit(conn, actor_id=actor_id, action="breach.remediation_submitted",
                           resource_type="breach", resource_id=breach_id,
                           detail={"contract_id": contract["contract_id"],
                                   "remediation_id": remediation_id})
                return "remediation", remediation_id, {"remediation_id": remediation_id}

            return self.idempotent(conn, request_id=request_id, action="submit_remediation",
                                   payload=payload, create=create)

    def resolve_breach(self, *, request_id: str, actor_id: str, breach_id: str,
                       accepted: bool, note: str | None = None) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "breach_id": breach_id,
                   "accepted": accepted, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="resolve_breach", payload=payload)
            if _replay is not None:
                return _replay
            breach = self.require_row(conn, "breaches", "breach_id", breach_id, "违约记录不存在")
            contract = conn.execute("SELECT * FROM contracts WHERE contract_id=?",
                                    (breach["contract_id"],)).fetchone()
            if contract is None:
                raise NotFoundError("合同不存在")
            self._party_check(conn, actor, contract)
            if breach["status"] != "open":
                raise ConflictError("违约已经闭合")
            note_text = self.opt_text(note, "note", 1000)

            def create():
                new_status = "remedied" if accepted else "rejected"
                conn.execute("UPDATE breaches SET status=? WHERE breach_id=?",
                             (new_status, breach_id))
                self._append_fact(conn, contract_id=contract["contract_id"],
                                  fact_type="breach_resolved",
                                  payload={"breach_id": breach_id, "accepted": bool(accepted),
                                           "status": new_status, "note": note_text},
                                  created_by=actor_id)
                self.audit(conn, actor_id=actor_id,
                           action="breach.remedied" if accepted else "breach.rejected",
                           resource_type="breach", resource_id=breach_id,
                           detail={"contract_id": contract["contract_id"], "status": new_status})
                return "breach", breach_id, {"breach_id": breach_id, "status": new_status}

            return self.idempotent(conn, request_id=request_id, action="resolve_breach",
                                   payload=payload, create=create)

    # -- 终止 --------------------------------------------------------------------

    def terminate_contract(self, *, request_id: str, actor_id: str, contract_id: str,
                           reason: str) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "contract_id": contract_id,
                   "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="terminate_contract", payload=payload)
            if _replay is not None:
                return _replay
            contract = self.require_row(conn, "contracts", "contract_id", contract_id, "合同不存在")
            if contract["status"] != "active":
                raise ConflictError("合同已经终止")
            self._party_check(conn, actor, contract)
            reason_text = self.text(reason, "reason", 1000)

            def create():
                now_value = self.now_text()
                conn.execute(
                    "UPDATE contracts SET status='terminated', terminated_at=?, terminated_by=?, "
                    "terminate_reason=? WHERE contract_id=?",
                    (now_value, actor_id, reason_text, contract_id))
                self._append_fact(conn, contract_id=contract_id, fact_type="contract_terminated",
                                  payload={"reason": reason_text, "terminated_at": now_value},
                                  created_by=actor_id)
                self.audit(conn, actor_id=actor_id, action="contract.terminated",
                           resource_type="contract", resource_id=contract_id,
                           detail={"reason": reason_text})
                return "contract", contract_id, {"contract_id": contract_id,
                                                 "status": "terminated"}

            return self.idempotent(conn, request_id=request_id, action="terminate_contract",
                                   payload=payload, create=create)

    # -- 结算 --------------------------------------------------------------------

    def generate_settlement(self, *, request_id: str, actor_id: str, contract_id: str,
                            gross_revenue: Any, period_start: str | None = None,
                            period_end: str | None = None, final_settlement: bool = False,
                            kind: str = "regular",
                            corrects_settlement_id: str | None = None) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "contract_id": contract_id,
                   "gross_revenue": str(gross_revenue), "period_start": period_start,
                   "period_end": period_end, "final_settlement": final_settlement, "kind": kind,
                   "corrects_settlement_id": corrects_settlement_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="generate_settlement", payload=payload)
            if _replay is not None:
                return _replay
            contract = self.require_row(conn, "contracts", "contract_id", contract_id, "合同不存在")
            if contract["status"] != "active" and not final_settlement and kind != "correction":
                raise ConflictError("非结算期不能为已终止合同生成常规结算")
            self._party_check(conn, actor, contract)
            if kind not in ("regular", "correction"):
                raise ValidationError("kind 只能是 regular 或 correction")
            start_text = self.day(period_start, "period_start") if period_start else None
            end_text = self.day(period_end, "period_end") if period_end else None
            if start_text and end_text and end_text < start_text:
                raise ValidationError("period_end 不能早于 period_start")
            try:
                gross = money(str(gross_revenue), "gross_revenue")
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            pointers = self._effective_pointers(conn, contract_id)
            cost_sheet = self.require_row(conn, "cost_sheets", "cost_sheet_id",
                                          pointers["cost_sheet_id"], "成本口径不存在")
            share_sheet = self.require_row(conn, "share_sheets", "share_sheet_id",
                                           pointers["share_sheet_id"], "分成口径不存在")
            cost_lines = json.loads(cost_sheet["lines_json"])
            share_lines = json.loads(share_sheet["lines_json"])
            terms = self._terms(contract)
            delivered_qty = self._delivered_total(conn, contract_id,
                                                  up_to=end_text + "T23:59:59Z" if end_text else None)
            period_qty = self._delivered_range(conn, contract_id, start=start_text, end=end_text)
            shortfall = False
            minimum = terms.get("minimum_purchase")
            if final_settlement and minimum:
                shortfall = delivered_qty < Decimal(minimum["quantity"])

            result = compute_settlement(terms=terms, cost_lines=cost_lines,
                                        share_lines=share_lines, gross_revenue=gross,
                                        delivered_qty=delivered_qty, shortfall=shortfall,
                                        cost_quantity=period_qty)
            original = None
            if kind == "correction":
                if not corrects_settlement_id:
                    raise ValidationError("纠正结算必须指定 corrects_settlement_id")
                original = self.require_row(conn, "settlements", "settlement_id",
                                            corrects_settlement_id, "被纠正的结算不存在")
                if original["contract_id"] != contract_id:
                    raise ValidationError("纠正结算与原结算不属于同一合同")
                if original["status"] not in ("disputed", "upheld"):
                    raise ConflictError("只能纠正处于争议中的结算")

            def create():
                settlement_id = new_id()
                now_value = self.now_text()
                detail = {**result,
                          "cost_sheet_version_no": cost_sheet["version_no"],
                          "share_sheet_version_no": share_sheet["version_no"],
                          "period_start": start_text, "period_end": end_text}
                conn.execute(
                    "INSERT INTO settlements(settlement_id,contract_id,kind,"
                    "corrects_settlement_id,period_start,period_end,final_settlement,currency,"
                    "gross_revenue,period_quantity,delivered_qty,cost_sheet_id,cost_sheet_hash,"
                    "share_sheet_id,share_sheet_hash,minimum_quantity,shortfall,partner_amount,"
                    "team_amount,status,detail_json,request_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'confirmed', ?,?,?,?)",
                    (settlement_id, contract_id, kind, corrects_settlement_id, start_text,
                     end_text, 1 if final_settlement else 0, result["currency"],
                     result["gross_revenue"], str(period_qty), str(delivered_qty),
                     pointers["cost_sheet_id"], cost_sheet["sheet_hash"],
                     pointers["share_sheet_id"], share_sheet["sheet_hash"],
                     minimum["quantity"] if minimum else None, 1 if shortfall else 0,
                     result["partner_amount"], result["team_amount"],
                     canonical_json(detail), request_id, actor_id, now_value))
                conn.execute(
                    "INSERT INTO settlement_entries(entry_id,settlement_id,entry_kind,"
                    "recipient_id,amount,note) VALUES(?,?, 'partner', ?,?,?)",
                    (new_id(), settlement_id, contract["partner_id"], result["partner_amount"],
                     "合作方分成"))
                for entry in result["member_entries"]:
                    conn.execute(
                        "INSERT INTO settlement_entries(entry_id,settlement_id,entry_kind,"
                        "recipient_id,amount,note) VALUES(?,?, 'team_member', ?,?,?)",
                        (new_id(), settlement_id, entry["recipient_id"], entry["amount"],
                                         f"成员份额 {entry['ratio']}"))
                self._append_fact(conn, contract_id=contract_id, fact_type="settlement_generated",
                                  payload={"settlement_id": settlement_id, "kind": kind,
                                           "gross_revenue": result["gross_revenue"],
                                           "team_amount": result["team_amount"],
                                           "partner_amount": result["partner_amount"],
                                           "cost_sheet_id": pointers["cost_sheet_id"],
                                           "cost_sheet_hash": cost_sheet["sheet_hash"],
                                           "share_sheet_id": pointers["share_sheet_id"],
                                           "share_sheet_hash": share_sheet["sheet_hash"],
                                           "corrects_settlement_id": corrects_settlement_id,
                                           "shortfall": shortfall},
                                  created_by=actor_id)
                if kind == "correction":
                    conn.execute("UPDATE settlements SET status='corrected' WHERE settlement_id=?",
                                 (corrects_settlement_id,))
                self.audit(conn, actor_id=actor_id, action="settlement.generated",
                           resource_type="settlement", resource_id=settlement_id,
                           detail={"contract_id": contract_id, "kind": kind,
                                   "cost_sheet_hash": cost_sheet["sheet_hash"],
                                   "share_sheet_hash": share_sheet["sheet_hash"],
                                   "team_amount": result["team_amount"],
                                   "partner_amount": result["partner_amount"],
                                   "request_id": request_id})
                return "settlement", settlement_id, {"settlement_id": settlement_id,
                                                      "team_amount": result["team_amount"],
                                                      "partner_amount": result["partner_amount"]}

            return self.idempotent(conn, request_id=request_id, action="generate_settlement",
                                   payload=payload, create=create)

    def dispute_settlement(self, *, request_id: str, actor_id: str, settlement_id: str,
                           description: str) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "settlement_id": settlement_id,
                   "description": description}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="dispute_settlement", payload=payload)
            if _replay is not None:
                return _replay
            settlement = self.require_row(conn, "settlements", "settlement_id", settlement_id,
                                          "结算不存在")
            contract = self.require_row(conn, "contracts", "contract_id",
                                        settlement["contract_id"], "合同不存在")
            self._party_check(conn, actor, contract)
            if settlement["status"] not in ("confirmed", "upheld"):
                raise ConflictError("当前状态的结算不能提出争议")
            description_text = self.text(description, "description", 1000)

            def create():
                dispute_id = new_id()
                conn.execute(
                    "INSERT INTO settlement_disputes(dispute_id,settlement_id,description,status,"
                    "created_by,created_at) VALUES(?,?,?, 'open', ?,?)",
                    (dispute_id, settlement_id, description_text, actor_id, self.now_text()))
                conn.execute("UPDATE settlements SET status='disputed' WHERE settlement_id=?",
                             (settlement_id,))
                self._append_fact(conn, contract_id=contract["contract_id"],
                                  fact_type="settlement_disputed",
                                  payload={"settlement_id": settlement_id,
                                           "dispute_id": dispute_id,
                                           "description": description_text},
                                  created_by=actor_id)
                self.audit(conn, actor_id=actor_id, action="settlement.disputed",
                           resource_type="settlement", resource_id=settlement_id,
                           detail={"contract_id": contract["contract_id"],
                                   "dispute_id": dispute_id})
                return "dispute", dispute_id, {"dispute_id": dispute_id}

            return self.idempotent(conn, request_id=request_id, action="dispute_settlement",
                                   payload=payload, create=create)

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                        resolution: str, action: str,
                        adjusted_gross_revenue: Any | None = None) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "dispute_id": dispute_id,
                   "resolution": resolution, "action": action,
                   "adjusted_gross_revenue": (str(adjusted_gross_revenue)
                                              if adjusted_gross_revenue is not None else None)}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="resolve_dispute", payload=payload)
            if _replay is not None:
                return _replay
            dispute = self.require_row(conn, "settlement_disputes", "dispute_id", dispute_id,
                                       "争议不存在")
            if dispute["status"] != "open":
                raise ConflictError("争议已经处理")
            settlement = self.require_row(conn, "settlements", "settlement_id",
                                          dispute["settlement_id"], "结算不存在")
            contract = self.require_row(conn, "contracts", "contract_id",
                                        settlement["contract_id"], "合同不存在")
            self._party_check(conn, actor, contract)
            resolution_text = self.text(resolution, "resolution", 1000)
            if action not in ("uphold", "correct"):
                raise ValidationError("action 只能是 uphold 或 correct")

            def create():
                now_value = self.now_text()
                correction_id = None
                if action == "uphold":
                    new_status = "upheld"
                    conn.execute("UPDATE settlements SET status='upheld' WHERE settlement_id=?",
                                 (settlement["settlement_id"],))
                else:
                    new_status = "corrected"
                    if adjusted_gross_revenue is None:
                        raise ValidationError("纠正争议必须给出调整后的收入")
                    # 在同一事务内复用结算生成逻辑：插入纠正结算。
                    correction_id = self._create_correction(
                        conn, actor_id=actor_id, contract=contract, original=settlement,
                        adjusted_gross_revenue=adjusted_gross_revenue,
                        resolution=resolution_text, now_value=now_value, request_id=request_id)
                conn.execute(
                    "UPDATE settlement_disputes SET status='resolved', resolution=?, "
                    "resolution_action=?, resolved_by=?, resolved_at=? WHERE dispute_id=?",
                    (resolution_text, action, actor_id, now_value, dispute_id))
                self._append_fact(conn, contract_id=contract["contract_id"],
                                  fact_type="dispute_resolved",
                                  payload={"dispute_id": dispute_id, "action": action,
                                           "settlement_id": settlement["settlement_id"],
                                           "correction_settlement_id": correction_id,
                                           "resolution": resolution_text},
                                  created_by=actor_id)
                self.audit(conn, actor_id=actor_id, action="settlement.dispute_resolved",
                           resource_type="settlement", resource_id=settlement["settlement_id"],
                           detail={"action": action, "correction_settlement_id": correction_id})
                return "dispute", dispute_id, {"dispute_id": dispute_id, "action": action,
                                               "new_status": new_status,
                                               "correction_settlement_id": correction_id}

            return self.idempotent(conn, request_id=request_id, action="resolve_dispute",
                                   payload=payload, create=create)

    def _create_correction(self, conn, *, actor_id: str, contract, original,
                           adjusted_gross_revenue: Any, resolution: str, now_value: str,
                           request_id: str) -> str:
        try:
            gross = money(str(adjusted_gross_revenue), "adjusted_gross_revenue")
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        cost_sheet = self.require_row(conn, "cost_sheets", "cost_sheet_id",
                                      original["cost_sheet_id"], "原成本口径不存在")
        share_sheet = self.require_row(conn, "share_sheets", "share_sheet_id",
                                       original["share_sheet_id"], "原分成口径不存在")
        terms = self._terms(contract)
        delivered_qty = Decimal(original["delivered_qty"])
        period_qty = Decimal(original["period_quantity"])
        result = compute_settlement(terms=terms, cost_lines=json.loads(cost_sheet["lines_json"]),
                                    share_lines=json.loads(share_sheet["lines_json"]),
                                    gross_revenue=gross, delivered_qty=delivered_qty,
                                    shortfall=bool(original["shortfall"]),
                                    cost_quantity=period_qty)
        settlement_id = new_id()
        original_detail = json.loads(original["detail_json"])
        detail = {**result, "correction_of": original["settlement_id"],
                  "resolution": resolution,
                  "deltas": {
                      "gross_revenue": str(gross - Decimal(original["gross_revenue"])),
                      "team_amount": str(Decimal(result["team_amount"])
                                        - Decimal(original["team_amount"])),
                      "partner_amount": str(Decimal(result["partner_amount"])
                                           - Decimal(original["partner_amount"]))},
                  "cost_sheet_version_no": cost_sheet["version_no"],
                  "share_sheet_version_no": share_sheet["version_no"]}
        conn.execute(
            "INSERT INTO settlements(settlement_id,contract_id,kind,corrects_settlement_id,"
            "period_start,period_end,final_settlement,currency,gross_revenue,period_quantity,"
            "delivered_qty,cost_sheet_id,cost_sheet_hash,share_sheet_id,share_sheet_hash,"
            "minimum_quantity,shortfall,partner_amount,team_amount,status,detail_json,"
            "request_id,created_by,created_at) "
            "VALUES(?,?, 'correction', ?,?,?,?,?,?, ?, ?,?,?,?,?,?,?,?,?, 'confirmed', ?,?,?,?)",
            (settlement_id, contract["contract_id"], original["settlement_id"],
             original["period_start"], original["period_end"], original["final_settlement"],
             result["currency"], result["gross_revenue"], str(period_qty), str(delivered_qty),
             original["cost_sheet_id"], original["cost_sheet_hash"],
             original["share_sheet_id"], original["share_sheet_hash"],
             original["minimum_quantity"], original["shortfall"],
             result["partner_amount"], result["team_amount"],
             canonical_json(detail), request_id + ":correction", actor_id, now_value))
        for entry in result["member_entries"]:
            conn.execute(
                "INSERT INTO settlement_entries(entry_id,settlement_id,entry_kind,recipient_id,"
                "amount,note) VALUES(?,?, 'team_member', ?,?,?)",
                (new_id(), settlement_id, entry["recipient_id"], entry["amount"],
                 f"纠正后成员份额 {entry['ratio']}"))
        conn.execute(
            "INSERT INTO settlement_entries(entry_id,settlement_id,entry_kind,recipient_id,"
            "amount,note) VALUES(?,?, 'partner', ?,?,?)",
            (new_id(), settlement_id, contract["partner_id"], result["partner_amount"], "纠正后合作方分成"))
        conn.execute("UPDATE settlements SET status='corrected' WHERE settlement_id=?",
                     (original["settlement_id"],))
        self._append_fact(conn, contract_id=contract["contract_id"],
                          fact_type="settlement_corrected",
                          payload={"original_settlement_id": original["settlement_id"],
                                   "correction_settlement_id": settlement_id,
                                   "deltas": detail["deltas"]},
                          created_by=actor_id)
        return settlement_id

    # -- 版本就绪 ----------------------------------------------------------------

    def _version_ready(self, conn, version_id: str) -> bool:
        version = self.require_row(conn, "design_versions", "version_id", version_id,
                                   "设计版本不存在")
        work = self.require_row(conn, "works", "work_id", version["work_id"], "作品不存在")
        team = self.require_row(conn, "teams", "team_id", work["team_id"], "团队不存在")
        required = set(json.loads(team["required_countersign_roles_json"]))
        signed = {row["member_role"] for row in conn.execute(
            "SELECT tm.member_role FROM design_signatures ds JOIN team_members tm "
            "ON tm.actor_id=ds.actor_id AND tm.team_id=? WHERE ds.version_id=?",
            (work["team_id"], version_id))}
        gates = conn.execute(
            "SELECT 1 FROM version_prerequisites vp LEFT JOIN version_prerequisite_facts vf "
            "ON vf.version_id=vp.version_id AND vf.code=vp.code "
            "WHERE vp.version_id=? AND vf.code IS NULL", (version_id,)).fetchone()
        return not (required - signed) and gates is None

    # -- 查询 --------------------------------------------------------------------

    def get_contract_view(self, contract_id: str) -> dict[str, Any]:
        conn = self.database.connection
        contract = self.require_row(conn, "contracts", "contract_id", contract_id, "合同不存在")
        terms = self._terms(contract)
        pointers = self._effective_pointers(conn, contract_id)
        milestones = [self._milestone_status(conn, contract, row)
                      for row in self._milestone_rows(conn, contract_id)]
        return {
            "contract_id": contract_id, "status": contract["status"],
            "work_id": contract["work_id"], "partner_id": contract["partner_id"],
            "team_id": contract["team_id"],
            "original_design_version_id": contract["design_version_id"],
            "original_cost_sheet_id": contract["cost_sheet_id"],
            "original_share_sheet_id": contract["share_sheet_id"],
            "current_design_version_id": pointers["design_version_id"],
            "current_cost_sheet_id": pointers["cost_sheet_id"],
            "current_share_sheet_id": pointers["share_sheet_id"],
            "terms": terms, "terms_hash": contract["terms_hash"],
            "cost_sheet_hash": contract["cost_sheet_hash"],
            "share_sheet_hash": contract["share_sheet_hash"],
            "accepted_at": contract["accepted_at"],
            "effective_date": contract["effective_date"], "end_date": contract["end_date"],
            "terminated_at": contract["terminated_at"],
            "signed_by_team": contract["signed_by_team"],
            "signed_by_partner": contract["signed_by_partner"],
            "milestones": milestones,
        }

    def list_facts(self, contract_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        conn = self.database.connection
        self.require_row(conn, "contracts", "contract_id", contract_id, "合同不存在")
        rows = conn.execute(
            "SELECT * FROM contract_facts WHERE contract_id=? AND sequence_no>? ORDER BY sequence_no",
            (contract_id, after_sequence)).fetchall()
        return [{"fact_id": row["fact_id"], "sequence_no": row["sequence_no"],
                 "fact_type": row["fact_type"], "payload": json.loads(row["payload_json"]),
                 "created_by": row["created_by"], "created_at": row["created_at"]} for row in rows]

    def list_deliveries(self, contract_id: str) -> list[dict[str, Any]]:
        conn = self.database.connection
        self.require_row(conn, "contracts", "contract_id", contract_id, "合同不存在")
        rows = conn.execute(
            "SELECT * FROM deliveries WHERE contract_id=? ORDER BY created_at, rowid",
            (contract_id,)).fetchall()
        return [{"delivery_id": row["delivery_id"], "milestone_code": row["milestone_code"],
                 "design_version_id": row["design_version_id"], "quantity": row["quantity"],
                 "created_by": row["created_by"], "created_at": row["created_at"]} for row in rows]

    def get_settlement(self, settlement_id: str) -> dict[str, Any]:
        conn = self.database.connection
        row = self.require_row(conn, "settlements", "settlement_id", settlement_id, "结算不存在")
        entries = [dict(r) for r in conn.execute(
            "SELECT entry_kind,recipient_id,amount,note FROM settlement_entries "
            "WHERE settlement_id=?", (settlement_id,))]
        disputes = [dict(r) for r in conn.execute(
            "SELECT dispute_id,description,status,resolution,resolution_action,created_by,created_at "
            "FROM settlement_disputes WHERE settlement_id=? ORDER BY created_at", (settlement_id,))]
        return {
            "settlement_id": settlement_id, "contract_id": row["contract_id"],
            "kind": row["kind"], "corrects_settlement_id": row["corrects_settlement_id"],
            "period_start": row["period_start"], "period_end": row["period_end"],
            "final_settlement": bool(row["final_settlement"]),
            "currency": row["currency"], "gross_revenue": row["gross_revenue"],
            "delivered_qty": row["delivered_qty"],
            "cost_sheet_id": row["cost_sheet_id"], "cost_sheet_hash": row["cost_sheet_hash"],
            "share_sheet_id": row["share_sheet_id"], "share_sheet_hash": row["share_sheet_hash"],
            "minimum_quantity": row["minimum_quantity"], "shortfall": bool(row["shortfall"]),
            "team_amount": row["team_amount"], "partner_amount": row["partner_amount"],
            "status": row["status"], "detail": json.loads(row["detail_json"]),
            "entries": entries, "disputes": disputes,
            "created_by": row["created_by"], "created_at": row["created_at"],
        }

    def list_settlements(self, contract_id: str) -> list[dict[str, Any]]:
        conn = self.database.connection
        self.require_row(conn, "contracts", "contract_id", contract_id, "合同不存在")
        rows = conn.execute(
            "SELECT settlement_id,kind,corrects_settlement_id,period_start,period_end,"
            "final_settlement,gross_revenue,team_amount,partner_amount,status,created_at "
            "FROM settlements WHERE contract_id=? ORDER BY created_at, rowid", (contract_id,))
        return [dict(row) for row in rows]
