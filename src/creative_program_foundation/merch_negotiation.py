"""意向、要约、反要约、保留、会签、接受、撤回与终止的时序状态机。"""

from __future__ import annotations

import json
from typing import Any

from .audit import canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .merch_common import MerchBase, new_id, thread_key
from .merch_money import money, quantity, rate
from .models import WriteReceipt

OFFER_OPEN = frozenset({"proposed", "countered", "reserved"})
OFFER_TERMINAL = frozenset({"accepted", "withdrawn", "expired", "terminated"})
STANDARD_CHANNELS = ("museum_shop", "tea_brand", "ecommerce", "other")


def scope_overlap(a: str, b: str, *separators: str) -> bool:
    if a == b:
        return True
    for sep in separators:
        if a.startswith(b + sep) or b.startswith(a + sep):
            return True
    return False


class NegotiationService(MerchBase):
    """管理合作谈判线程与合同生效。"""

    # -- 意向 -----------------------------------------------------------------

    def create_intention(self, *, request_id: str, actor_id: str, work_id: str,
                         partner_id: str, channels: list[str] | None = None,
                         note: str | None = None) -> WriteReceipt:
        channels = channels or []
        payload = {"request_id": request_id, "actor_id": actor_id, "work_id": work_id,
                   "partner_id": partner_id, "channels": channels, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="create_intention", payload=payload)
            if _replay is not None:
                return _replay
            work = self.require_row(conn, "works", "work_id", work_id, "作品不存在")
            partner = self.require_row(conn, "partners", "partner_id", partner_id, "合作方不存在")
            channel_list = self._channel_list(channels)
            note_text = self.opt_text(note, "note", 1000)
            is_team = self.team_member(conn, actor, work["team_id"]) is not None
            is_partner = self.partner_rep(conn, actor, partner_id) is not None
            if actor.role not in ("admin", "operator") and not is_team and not is_partner:
                raise PermissionDenied("只有团队成员、合作方代表或运营可以登记意向")
            tid = thread_key(work_id, partner_id)
            if conn.execute("SELECT 1 FROM intentions WHERE work_id=? AND partner_id=? AND status='open'",
                            (work_id, partner_id)).fetchone():
                raise ConflictError("该作品与合作方已经存在进行中的意向")

            def create():
                intention_id = new_id()
                conn.execute(
                    "INSERT INTO intentions(intention_id,work_id,partner_id,channels_json,note,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?, 'open', ?,?)",
                    (intention_id, work_id, partner_id, canonical_json(channel_list),
                     note_text, actor_id, self.now_text()),
                )
                self.audit(conn, actor_id=actor_id, action="intention.created",
                           resource_type="intention", resource_id=intention_id,
                           detail={"thread_id": tid, "work_id": work_id, "partner_id": partner_id,
                                   "channels": channel_list})
                return "intention", intention_id, {"intention_id": intention_id,
                                                   "thread_id": tid}

            return self.idempotent(conn, request_id=request_id, action="create_intention",
                                   payload=payload, create=create)

    def terminate_intention(self, *, request_id: str, actor_id: str, intention_id: str,
                            reason: str) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "intention_id": intention_id,
                   "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="terminate_intention", payload=payload)
            if _replay is not None:
                return _replay
            intention = self.require_row(conn, "intentions", "intention_id", intention_id,
                                         "意向不存在")
            if intention["status"] != "open":
                raise ConflictError("意向已经终止")
            accepted = conn.execute(
                "SELECT 1 FROM offers WHERE intention_id=? AND status='accepted'", (intention_id,)
            ).fetchone()
            if accepted:
                raise ConflictError("意向已经产生合同，应通过合同终止流程结束")
            reason_text = self.text(reason, "reason", 500)

            def create():
                now_value = self.now_text()
                conn.execute(
                    "UPDATE intentions SET status='terminated', terminated_at=?, terminate_reason=? "
                    "WHERE intention_id=?", (now_value, reason_text, intention_id))
                open_offers = conn.execute(
                    "SELECT offer_id FROM offers WHERE intention_id=? AND status IN ('proposed','countered','reserved')",
                    (intention_id,)).fetchall()
                for row in open_offers:
                    conn.execute(
                        "UPDATE offer_reservations SET released_at=? WHERE offer_id=? AND released_at IS NULL",
                        (now_value, row["offer_id"]))
                    conn.execute("UPDATE offers SET status='terminated' WHERE offer_id=?",
                                 (row["offer_id"],))
                    self.audit(conn, actor_id=actor_id, action="offer.terminated_with_intention",
                               resource_type="offer", resource_id=row["offer_id"],
                               detail={"intention_id": intention_id, "reason": reason_text})
                self.audit(conn, actor_id=actor_id, action="intention.terminated",
                           resource_type="intention", resource_id=intention_id,
                           detail={"reason": reason_text, "offers_terminated": len(open_offers)})
                return "intention", intention_id, {"intention_id": intention_id,
                                                   "status": "terminated"}

            return self.idempotent(conn, request_id=request_id, action="terminate_intention",
                                   payload=payload, create=create)

    # -- 要约与反要约 -----------------------------------------------------------

    def make_offer(self, *, request_id: str, actor_id: str, work_id: str, partner_id: str,
                   direction: str, intention_id: str, design_version_id: str,
                   cost_sheet_id: str, share_sheet_id: str, terms: dict[str, Any],
                   valid_until: str, note: str | None = None,
                   prev_offer_id: str | None = None) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "work_id": work_id,
                   "partner_id": partner_id, "direction": direction,
                   "intention_id": intention_id, "design_version_id": design_version_id,
                   "cost_sheet_id": cost_sheet_id, "share_sheet_id": share_sheet_id,
                   "terms": terms, "valid_until": valid_until, "note": note,
                   "prev_offer_id": prev_offer_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="counter_offer" if prev_offer_id else "make_offer", payload=payload)
            if _replay is not None:
                return _replay
            work = self.require_row(conn, "works", "work_id", work_id, "作品不存在")
            partner = self.require_row(conn, "partners", "partner_id", partner_id, "合作方不存在")
            if direction not in ("inbound", "outbound"):
                raise ValidationError("direction 只能是 inbound 或 outbound")
            is_team = self.team_member(conn, actor, work["team_id"]) is not None
            is_partner = self.partner_rep(conn, actor, partner_id) is not None
            if actor.role not in ("admin", "operator"):
                if direction == "inbound" and not is_partner:
                    raise PermissionDenied("合作方要约只能由合作方代表或运营发起")
                if direction == "outbound" and not is_team:
                    raise PermissionDenied("团队要约只能由团队成员或运营发起")
            intention = self.require_row(conn, "intentions", "intention_id", intention_id,
                                         "意向不存在")
            if intention["work_id"] != work_id or intention["partner_id"] != partner_id:
                raise ValidationError("要约与意向的作品或合作方不一致")
            if intention["status"] != "open":
                raise ConflictError("意向已经终止，不能再发要约")
            tid = thread_key(work_id, partner_id)
            previous = None
            if prev_offer_id:
                previous = self.require_row(conn, "offers", "offer_id", prev_offer_id,
                                            "前序要约不存在")
                if previous["thread_id"] != tid:
                    raise ValidationError("反要约必须在同一谈判线程内")
                if previous["status"] not in ("proposed", "countered"):
                    raise ConflictError("只有未被保留或终局的要约才能被反要约")
                signed = conn.execute(
                    "SELECT COUNT(*) AS c FROM offer_signatures WHERE offer_id=?",
                    (prev_offer_id,)).fetchone()["c"]
                if signed:
                    raise ConflictError("要约已经被会签，不能再反要约；应先撤回再重新发起")
                if previous["direction"] == direction:
                    raise ValidationError("反要约必须由另一方提出")
            else:
                existing = conn.execute(
                    "SELECT 1 FROM offers WHERE thread_id=? LIMIT 1", (tid,)).fetchone()
                if existing:
                    raise ConflictError("线程已经存在要约，继续谈判必须使用 prev_offer_id")
            self._require_version_refs(conn, work, design_version_id, cost_sheet_id,
                                       share_sheet_id)
            normalized_terms = self.validate_terms(terms)
            valid_until_text = self.stamp(valid_until, "valid_until")
            if valid_until_text <= self.now_text():
                raise ValidationError("要约有效期必须晚于当前时间")
            note_text = self.opt_text(note, "note", 1000)

            def create():
                next_seq = conn.execute(
                    "SELECT COALESCE(MAX(sequence_no),0)+1 AS next FROM offers WHERE thread_id=?",
                    (tid,)).fetchone()["next"]
                offer_id = new_id()
                conn.execute(
                    "INSERT INTO offers(offer_id,thread_id,sequence_no,prev_offer_id,intention_id,"
                    "work_id,partner_id,direction,design_version_id,cost_sheet_id,share_sheet_id,"
                    "terms_json,terms_hash,status,valid_until,note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'proposed', ?,?,?,?)",
                    (offer_id, tid, next_seq, prev_offer_id, intention_id, work_id, partner_id,
                     direction, design_version_id, cost_sheet_id, share_sheet_id,
                     canonical_json(normalized_terms), digest(normalized_terms),
                     valid_until_text, note_text, actor_id, self.now_text()),
                )
                if previous is not None:
                    conn.execute("UPDATE offers SET status='countered' WHERE offer_id=?",
                                 (prev_offer_id,))
                action = "offer.countered" if prev_offer_id else "offer.proposed"
                self.audit(conn, actor_id=actor_id, action=action,
                           resource_type="offer", resource_id=offer_id,
                           detail={"thread_id": tid, "sequence_no": next_seq,
                                   "work_id": work_id, "partner_id": partner_id,
                                   "direction": direction,
                                   "design_version_id": design_version_id,
                                   "exclusive": normalized_terms["exclusive"],
                                   "territories": normalized_terms["territories"],
                                   "channels": normalized_terms["channels"],
                                   "terms_hash": digest(normalized_terms),
                                   "prev_offer_id": prev_offer_id})
                result = {"offer_id": offer_id, "thread_id": tid, "sequence_no": next_seq}
                return "offer", offer_id, result

            return self.idempotent(conn, request_id=request_id,
                                   action="counter_offer" if prev_offer_id else "make_offer",
                                   payload=payload, create=create)

    def withdraw_offer(self, *, request_id: str, actor_id: str, offer_id: str,
                       reason: str | None = None) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "offer_id": offer_id,
                   "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="withdraw_offer", payload=payload)
            if _replay is not None:
                return _replay
            offer = self.require_row(conn, "offers", "offer_id", offer_id, "要约不存在")
            if offer["status"] not in ("proposed", "countered", "reserved"):
                raise ConflictError("当前状态的要约不能撤回")
            self._assert_offer_party(conn, actor, offer)
            reason_text = self.opt_text(reason, "reason", 500)

            def create():
                conn.execute(
                    "UPDATE offer_reservations SET released_at=? WHERE offer_id=? AND released_at IS NULL",
                    (self.now_text(), offer_id))
                conn.execute("UPDATE offers SET status='withdrawn' WHERE offer_id=?", (offer_id,))
                self.audit(conn, actor_id=actor_id, action="offer.withdrawn",
                           resource_type="offer", resource_id=offer_id,
                           detail={"thread_id": offer["thread_id"], "reason": reason_text})
                return "offer", offer_id, {"offer_id": offer_id, "status": "withdrawn"}

            return self.idempotent(conn, request_id=request_id, action="withdraw_offer",
                                   payload=payload, create=create)

    def expire_stale_offers(self, *, request_id: str, actor_id: str) -> WriteReceipt:
        """把所有已过有效期但尚未终局的要约标记为过期。"""

        payload = {"request_id": request_id, "actor_id": actor_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="expire_stale_offers", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator")

            def create():
                now_value = self.now_text()
                rows = conn.execute(
                    "SELECT offer_id FROM offers WHERE status IN ('proposed','countered','reserved') "
                    "AND valid_until<=?", (now_value,)).fetchall()
                for row in rows:
                    conn.execute(
                        "UPDATE offer_reservations SET released_at=? WHERE offer_id=? AND released_at IS NULL",
                        (now_value, row["offer_id"]))
                    conn.execute("UPDATE offers SET status='expired' WHERE offer_id=?",
                                 (row["offer_id"],))
                    self.audit(conn, actor_id=actor_id, action="offer.expired",
                               resource_type="offer", resource_id=row["offer_id"],
                               detail={"expired_at": now_value})
                rid = new_id()
                return "offer_expiry_batch", rid, {"expired": len(rows)}

            return self.idempotent(conn, request_id=request_id, action="expire_stale_offers",
                                   payload=payload, create=create)

    # -- 保留 -----------------------------------------------------------------

    def reserve_offer(self, *, request_id: str, actor_id: str, offer_id: str,
                      reserve_until: str) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "offer_id": offer_id,
                   "reserve_until": reserve_until}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="reserve_offer", payload=payload)
            if _replay is not None:
                return _replay
            offer = self.require_row(conn, "offers", "offer_id", offer_id, "要约不存在")
            if offer["status"] not in ("proposed", "countered"):
                raise ConflictError("只有进行中的要约可以保留")
            if offer["valid_until"] <= self.now_text():
                raise ConflictError("要约已经过了有效期")
            partner = self.require_row(conn, "partners", "partner_id", offer["partner_id"],
                                       "合作方不存在")
            is_partner = self.partner_rep(conn, actor, offer["partner_id"]) is not None
            if actor.role not in ("admin", "operator") and not is_partner:
                raise PermissionDenied("只有合作方代表或运营可以保留要约")
            reserve_until_text = self.stamp(reserve_until, "reserve_until")
            if reserve_until_text <= self.now_text():
                raise ValidationError("保留期限必须晚于当前时间")
            if reserve_until_text > offer["valid_until"]:
                raise ValidationError("保留期限不能超过要约有效期")
            conflict = self._scope_conflict(conn, offer, exclude_partner=offer["partner_id"])
            if conflict:
                raise ConflictError(f"独家范围冲突：{conflict}")

            def create():
                reservation_id = new_id()
                conn.execute(
                    "INSERT INTO offer_reservations(reservation_id,offer_id,prior_status,"
                    "reserved_by,reserved_at,reserve_until) VALUES(?,?,?,?,?,?)",
                    (reservation_id, offer_id, offer["status"], actor_id,
                     self.now_text(), reserve_until_text))
                conn.execute("UPDATE offers SET status='reserved' WHERE offer_id=?", (offer_id,))
                self.audit(conn, actor_id=actor_id, action="offer.reserved",
                           resource_type="offer", resource_id=offer_id,
                           detail={"reservation_id": reservation_id,
                                   "reserve_until": reserve_until_text,
                                   "thread_id": offer["thread_id"]})
                return "reservation", reservation_id, {"reservation_id": reservation_id,
                                                       "offer_id": offer_id}

            return self.idempotent(conn, request_id=request_id, action="reserve_offer",
                                   payload=payload, create=create)

    def release_reservation(self, *, request_id: str, actor_id: str, offer_id: str) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "offer_id": offer_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="release_reservation", payload=payload)
            if _replay is not None:
                return _replay
            offer = self.require_row(conn, "offers", "offer_id", offer_id, "要约不存在")
            if offer["status"] != "reserved":
                raise ConflictError("要约当前不在保留状态")
            reservation = conn.execute(
                "SELECT * FROM offer_reservations WHERE offer_id=? AND released_at IS NULL",
                (offer_id,)).fetchone()
            if reservation is None:
                raise ConflictError("保留已经释放")
            is_partner = self.partner_rep(conn, actor, offer["partner_id"]) is not None
            if actor.role not in ("admin", "operator") and not is_partner:
                raise PermissionDenied("只有运营或合作方可以释放保留")

            def create():
                conn.execute(
                    "UPDATE offer_reservations SET released_at=? WHERE reservation_id=?",
                    (self.now_text(), reservation["reservation_id"]))
                conn.execute("UPDATE offers SET status=? WHERE offer_id=?",
                             (reservation["prior_status"], offer_id))
                self.audit(conn, actor_id=actor_id, action="offer.reservation_released",
                           resource_type="offer", resource_id=offer_id,
                           detail={"reservation_id": reservation["reservation_id"],
                                   "restored_status": reservation["prior_status"]})
                return "reservation", reservation["reservation_id"], {
                    "reservation_id": reservation["reservation_id"], "offer_id": offer_id,
                    "status": reservation["prior_status"]}

            return self.idempotent(conn, request_id=request_id, action="release_reservation",
                                   payload=payload, create=create)

    # -- 会签与接受 -------------------------------------------------------------

    def sign_offer(self, *, request_id: str, actor_id: str, offer_id: str, party: str) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "offer_id": offer_id,
                   "party": party}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="sign_offer", payload=payload)
            if _replay is not None:
                return _replay
            offer = self.require_row(conn, "offers", "offer_id", offer_id, "要约不存在")
            if offer["status"] not in OFFER_OPEN:
                raise ConflictError("只有进行中的要约可以签署")
            if offer["valid_until"] <= self.now_text():
                raise ConflictError("要约已经过了有效期")
            work = self.require_row(conn, "works", "work_id", offer["work_id"], "作品不存在")
            if party == "team":
                grant = self.team_signer(conn, actor, work["team_id"], offer["work_id"])
                if grant is None and actor.role != "admin":
                    raise PermissionDenied("团队签署人缺少有效签署授权")
            elif party == "partner":
                rep = self.partner_rep(conn, actor, offer["partner_id"])
                if rep is None or not rep["can_sign"]:
                    raise PermissionDenied("合作方签署人必须是有签约权的代表")
            else:
                raise ValidationError("party 只能是 team 或 partner")
            existing = conn.execute(
                "SELECT 1 FROM offer_signatures WHERE offer_id=? AND party=?", (offer_id, party)
            ).fetchone()
            if existing:
                raise ConflictError(f"{party} 方已经完成签署")

            def create():
                conn.execute(
                    "INSERT INTO offer_signatures(offer_id,party,actor_id,created_at) VALUES(?,?,?,?)",
                    (offer_id, party, actor_id, self.now_text()))
                self.audit(conn, actor_id=actor_id, action="offer.signed",
                           resource_type="offer", resource_id=offer_id,
                           detail={"party": party, "thread_id": offer["thread_id"]})
                return ("offer_signature", f"{offer_id}:{party}",
                        {"offer_id": offer_id, "party": party, "actor_id": actor_id})

            return self.idempotent(conn, request_id=request_id, action="sign_offer",
                                   payload=payload, create=create)

    def accept_offer(self, *, request_id: str, actor_id: str, offer_id: str) -> WriteReceipt:
        payload = {"request_id": request_id, "actor_id": actor_id, "offer_id": offer_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="accept_offer", payload=payload)
            if _replay is not None:
                return _replay
            offer = self.require_row(conn, "offers", "offer_id", offer_id, "要约不存在")
            if offer["status"] not in OFFER_OPEN:
                raise ConflictError("当前状态的要约不能接受")
            if offer["valid_until"] <= self.now_text():
                raise ConflictError("要约已经过了有效期")
            work = self.require_row(conn, "works", "work_id", offer["work_id"], "作品不存在")
            partner = self.require_row(conn, "partners", "partner_id", offer["partner_id"],
                                       "合作方不存在")
            is_team = self.team_member(conn, actor, work["team_id"]) is not None
            is_partner = self.partner_rep(conn, actor, offer["partner_id"]) is not None
            if actor.role not in ("admin", "operator") and not is_team and not is_partner:
                raise PermissionDenied("只有谈判双方或运营可以接受要约")
            sigs = conn.execute(
                "SELECT party,actor_id FROM offer_signatures WHERE offer_id=?", (offer_id,)
            ).fetchall()
            parties = {row["party"]: row["actor_id"] for row in sigs}
            if "team" not in parties or "partner" not in parties:
                raise ConflictError("接受前必须完成双方会签")
            # 签署授权在接受时点必须仍然有效。
            team_actor = _light_actor(conn, parties["team"])
            if team_actor.role != "admin" and self.team_signer(
                    conn, team_actor, work["team_id"], offer["work_id"]) is None:
                raise PermissionDenied("团队签署授权在接受前已失效")
            partner_actor = _light_actor(conn, parties["partner"])
            rep = self.partner_rep(conn, partner_actor, offer["partner_id"])
            if rep is None or not rep["can_sign"]:
                raise PermissionDenied("合作方签约代表资格在接受前已失效")
            if not self.qualification_ok(conn, offer["partner_id"]):
                raise PermissionDenied("合作方资质未通过或已过期")
            readiness = self._version_readiness(conn, offer["design_version_id"])
            if not readiness["approved"]:
                raise ConflictError(
                    "设计版本尚未满足会签与前置条件："
                    f"缺角色 {readiness['missing_roles']}，缺前置 {readiness['missing_prerequisites']}")
            terms = json.loads(offer["terms_json"])
            if terms["exclusive"]:
                conflict = self._scope_conflict(conn, offer, exclude_partner=offer["partner_id"])
                if conflict:
                    raise ConflictError(f"独家范围冲突，不能接受：{conflict}")
            cost = self.require_row(conn, "cost_sheets", "cost_sheet_id",
                                    offer["cost_sheet_id"], "成本口径不存在")
            share = self.require_row(conn, "share_sheets", "share_sheet_id",
                                     offer["share_sheet_id"], "分成口径不存在")
            contract_id = new_id()
            now_value = self.now_text()

            def create():
                conn.execute(
                    "INSERT INTO contracts(contract_id,offer_id,thread_id,work_id,partner_id,team_id,"
                    "status,design_version_id,cost_sheet_id,cost_sheet_hash,share_sheet_id,"
                    "share_sheet_hash,terms_json,terms_hash,signed_by_team,signed_by_partner,"
                    "accepted_at,effective_date,end_date) "
                    "VALUES(?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?,?,?,?)",
                    (contract_id, offer_id, offer["thread_id"], offer["work_id"],
                     offer["partner_id"], work["team_id"], offer["design_version_id"],
                     offer["cost_sheet_id"], cost["sheet_hash"], offer["share_sheet_id"],
                     share["sheet_hash"], offer["terms_json"], offer["terms_hash"],
                     parties["team"], parties["partner"], now_value,
                     terms["effective_date"], terms.get("end_date")))
                for milestone in terms["milestones"]:
                    conn.execute(
                        "INSERT INTO contract_milestones(milestone_id,contract_id,code,label,"
                        "sequence_no,kind,planned_qty,depends_on_json) VALUES(?,?,?,?,?,?,?,?)",
                        (new_id(), contract_id, milestone["code"], milestone["label"],
                         milestone["sequence_no"], milestone["kind"],
                         milestone["planned_qty"],
                         canonical_json(milestone["depends_on"])))
                conn.execute(
                    "UPDATE offer_reservations SET released_at=? WHERE offer_id=? AND released_at IS NULL",
                    (now_value, offer_id))
                conn.execute("UPDATE offers SET status='accepted' WHERE offer_id=?", (offer_id,))
                conn.execute(
                    "INSERT INTO contract_facts(fact_id,contract_id,sequence_no,fact_type,"
                    "payload_json,created_by,created_at) VALUES(?,?,?, 'contract_signed', ?,?,?)",
                    (new_id(), contract_id, 1,
                     canonical_json({"offer_id": offer_id, "signed_by_team": parties["team"],
                                     "signed_by_partner": parties["partner"],
                                     "design_version_id": offer["design_version_id"],
                                     "cost_sheet_hash": cost["sheet_hash"],
                                     "share_sheet_hash": share["sheet_hash"]}),
                     actor_id, now_value))
                self.audit(conn, actor_id=actor_id, action="offer.accepted",
                           resource_type="offer", resource_id=offer_id,
                           detail={"contract_id": contract_id, "thread_id": offer["thread_id"]})
                self.audit(conn, actor_id=actor_id, action="contract.created",
                           resource_type="contract", resource_id=contract_id,
                           detail={"offer_id": offer_id, "work_id": offer["work_id"],
                                   "partner_id": offer["partner_id"],
                                   "design_version_id": offer["design_version_id"],
                                   "cost_sheet_id": offer["cost_sheet_id"],
                                   "share_sheet_id": offer["share_sheet_id"],
                                   "exclusive": terms["exclusive"],
                                   "territories": terms["territories"],
                                   "channels": terms["channels"],
                                   "terms_hash": offer["terms_hash"]})
                return "contract", contract_id, {"contract_id": contract_id,
                                                 "offer_id": offer_id}

            return self.idempotent(conn, request_id=request_id, action="accept_offer",
                                   payload=payload, create=create)

    # -- 占用查询 ---------------------------------------------------------------

    def rights_occupancy(self, work_id: str, as_of: str | None = None) -> dict[str, Any]:
        """解释某时点作品上仍被占用的独家地域与渠道。"""

        conn = self.database.connection
        point = self.stamp(as_of, "as_of") if as_of else self.now_text()
        point_date = point[:10]
        occupied: list[dict[str, Any]] = []
        contracts = conn.execute(
            "SELECT * FROM contracts WHERE work_id=? AND status='active' "
            "AND accepted_at<=? AND (terminated_at IS NULL OR terminated_at>?)",
            (work_id, point, point)).fetchall()
        for contract in contracts:
            if contract["end_date"] and contract["end_date"] < point_date:
                continue
            terms = json.loads(contract["terms_json"])
            if not terms["exclusive"]:
                continue
            occupied.append({
                "source": "contract", "source_id": contract["contract_id"],
                "partner_id": contract["partner_id"],
                "territories": terms["territories"], "channels": terms["channels"],
                "effective_date": contract["effective_date"], "end_date": contract["end_date"],
                "from": contract["accepted_at"],
                "until": contract["terminated_at"] or contract["end_date"],
            })
        reservations = conn.execute(
            "SELECT r.*, o.terms_json, o.partner_id, o.work_id, o.thread_id, o.status AS offer_status "
            "FROM offer_reservations r JOIN offers o ON o.offer_id=r.offer_id "
            "WHERE o.work_id=? AND r.reserved_at<=? AND r.reserve_until>?",
            (work_id, point, point)).fetchall()
        for row in reservations:
            if row["released_at"] and row["released_at"] <= point:
                continue
            terms = json.loads(row["terms_json"])
            if not terms["exclusive"]:
                continue
            occupied.append({
                "source": "reservation", "source_id": row["reservation_id"],
                "offer_id": row["offer_id"], "partner_id": row["partner_id"],
                "territories": terms["territories"], "channels": terms["channels"],
                "from": row["reserved_at"], "until": row["reserve_until"],
            })
        return {"work_id": work_id, "as_of": point, "occupancy": occupied}

    # -- 条款与校验 --------------------------------------------------------------

    def validate_terms(self, terms: Any) -> dict[str, Any]:
        if not isinstance(terms, dict):
            raise ValidationError("terms 必须是对象")
        currency = str(terms.get("currency", "CNY")).strip().upper()
        if not (3 <= len(currency) <= 5) or not currency.isalpha():
            raise ValidationError("currency 必须是 3-5 位字母")
        exclusive = bool(terms.get("exclusive", False))
        territories = self._code_list(terms.get("territories"), "territories")
        channels = self._channel_list(terms.get("channels"))
        effective_date = self.day(terms.get("effective_date"), "effective_date")
        end_date = None
        if terms.get("end_date"):
            end_date = self.day(terms.get("end_date"), "end_date")
            if end_date <= effective_date:
                raise ValidationError("end_date 必须晚于 effective_date")
        normalized: dict[str, Any] = {
            "currency": currency, "exclusive": exclusive,
            "territories": territories, "channels": channels,
            "effective_date": effective_date, "end_date": end_date,
        }
        if terms.get("price") is not None:
            try:
                normalized["price"] = str(money(terms.get("price"), "price"))
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        minimum = terms.get("minimum_purchase")
        if minimum is not None:
            if not isinstance(minimum, dict):
                raise ValidationError("minimum_purchase 必须是对象")
            try:
                qty = str(quantity(minimum.get("quantity"), "minimum_purchase.quantity"))
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            unit = self.text(minimum.get("unit"), "minimum_purchase.unit", 40)
            normalized["minimum_purchase"] = {"quantity": qty, "unit": unit}
        royalty = terms.get("royalty")
        if royalty is None or not isinstance(royalty, dict):
            raise ValidationError("royalty 分成口径为必填对象")
        normalized["royalty"] = self._validate_royalty(royalty)
        sample_required = bool(terms.get("sample_required", False))
        normalized["sample_required"] = sample_required
        normalized["milestones"] = self._validate_milestones(
            terms.get("milestones"), sample_required,
            normalized.get("minimum_purchase", {}).get("quantity"))
        return normalized

    def _validate_royalty(self, royalty: dict[str, Any]) -> dict[str, Any]:
        mode = str(royalty.get("mode", "")).strip()
        deduct_costs = bool(royalty.get("deduct_costs", True))
        if mode == "rate":
            try:
                value = str(rate(royalty.get("rate"), "royalty.rate"))
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            result = {"mode": "rate", "rate": value, "deduct_costs": deduct_costs}
        elif mode == "fixed":
            try:
                value = str(money(royalty.get("amount"), "royalty.amount"))
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            result = {"mode": "fixed", "amount": value, "deduct_costs": deduct_costs}
        else:
            raise ValidationError("royalty.mode 只能是 rate 或 fixed")
        if royalty.get("guarantee_amount") not in (None, ""):
            try:
                result["guarantee_amount"] = str(money(royalty.get("guarantee_amount"),
                                                       "royalty.guarantee_amount"))
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        return result

    def _validate_milestones(self, raw: Any, sample_required: bool,
                             minimum_qty: str | None) -> list[dict[str, Any]]:
        if raw is None:
            milestones: list[dict[str, Any]] = []
            if sample_required:
                milestones.append({"code": "sample", "label": "样品确认", "kind": "sample",
                                   "planned_qty": "0", "depends_on": []})
            milestones.append({
                "code": "delivery", "label": "最低采购量交付", "kind": "delivery",
                "planned_qty": minimum_qty or "0",
                "depends_on": ["sample"] if sample_required else []})
            return [dict(item, sequence_no=i + 1) for i, item in enumerate(milestones)]
        if not isinstance(raw, list) or not raw:
            raise ValidationError("milestones 必须是非空数组")
        result = []
        codes: set[str] = set()
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValidationError(f"里程碑{index + 1}必须是对象")
            code = self.code_value(item.get("code", ""), f"里程碑{index + 1}.code")
            if code in codes:
                raise ValidationError(f"里程碑编码 {code} 重复")
            codes.add(code)
            label = self.text(item.get("label"), f"里程碑{index + 1}.label")
            kind = str(item.get("kind", "")).strip()
            if kind not in ("sample", "delivery", "approval"):
                raise ValidationError(f"里程碑{index + 1}.kind 非法")
            try:
                planned = str(quantity(item.get("planned_qty", "0"),
                                       f"里程碑{index + 1}.planned_qty"))
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            depends = item.get("depends_on", [])
            if not isinstance(depends, list):
                raise ValidationError(f"里程碑{code}.depends_on 必须是数组")
            depends = [self.code_value(d, f"里程碑{code}.depends_on") for d in depends]
            result.append({"code": code, "label": label, "kind": kind,
                           "planned_qty": planned, "depends_on": depends,
                           "sequence_no": len(result) + 1})
        for item in result:
            for dep in item["depends_on"]:
                if dep not in codes:
                    raise ValidationError(f"里程碑 {item['code']} 依赖了不存在的 {dep}")
        if not any(item["kind"] == "delivery" for item in result):
            raise ValidationError("至少需要一个 delivery 交付里程碑")
        return result

    def _code_list(self, values: Any, field: str) -> list[str]:
        if not isinstance(values, list) or not values:
            raise ValidationError(f"{field} 必须是非空数组")
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            code = self.code_value(value, field)
            if code in seen:
                raise ValidationError(f"{field} 中 {code} 重复")
            seen.add(code)
            result.append(code)
        return result

    def _channel_list(self, values: Any) -> list[str]:
        result = self._code_list(values, "channels")
        for code in result:
            if code not in STANDARD_CHANNELS and not code.startswith("other"):
                raise ValidationError(f"渠道 {code} 不在标准范围 {STANDARD_CHANNELS}")
        return result

    # -- 内部查询 ---------------------------------------------------------------

    def _assert_offer_party(self, conn, actor, offer) -> None:
        work = self.require_row(conn, "works", "work_id", offer["work_id"], "作品不存在")
        is_team = self.team_member(conn, actor, work["team_id"]) is not None
        is_partner = self.partner_rep(conn, actor, offer["partner_id"]) is not None
        if actor.role not in ("admin", "operator") and not is_team and not is_partner:
            raise PermissionDenied("只有谈判双方或运营可以操作该要约")

    def _require_version_refs(self, conn, work, design_version_id: str,
                              cost_sheet_id: str, share_sheet_id: str) -> None:
        version = self.require_row(conn, "design_versions", "version_id",
                                   design_version_id, "设计版本不存在")
        if version["work_id"] != work["work_id"]:
            raise ValidationError("设计版本不属于该作品")
        cost = self.require_row(conn, "cost_sheets", "cost_sheet_id", cost_sheet_id,
                                "成本口径不存在")
        if cost["work_id"] != work["work_id"]:
            raise ValidationError("成本口径不属于该作品")
        share = self.require_row(conn, "share_sheets", "share_sheet_id", share_sheet_id,
                                 "分成口径不存在")
        if share["team_id"] != work["team_id"]:
            raise ValidationError("分成口径不属于作品所属团队")

    def _version_readiness(self, conn, version_id: str) -> dict[str, Any]:
        version = self.require_row(conn, "design_versions", "version_id", version_id,
                                   "设计版本不存在")
        work = self.require_row(conn, "works", "work_id", version["work_id"], "作品不存在")
        team = self.require_row(conn, "teams", "team_id", work["team_id"], "团队不存在")
        required_roles = set(json.loads(team["required_countersign_roles_json"]))
        signed = {row["member_role"] for row in conn.execute(
            "SELECT tm.member_role FROM design_signatures ds JOIN team_members tm "
            "ON tm.actor_id=ds.actor_id AND tm.team_id=? WHERE ds.version_id=?",
            (work["team_id"], version_id))}
        missing_gates = [row["code"] for row in conn.execute(
            "SELECT vp.code FROM version_prerequisites vp LEFT JOIN version_prerequisite_facts vf "
            "ON vf.version_id=vp.version_id AND vf.code=vp.code "
            "WHERE vp.version_id=? AND vf.code IS NULL", (version_id,))]
        return {"approved": not (required_roles - signed) and not missing_gates,
                "missing_roles": sorted(required_roles - signed),
                "missing_prerequisites": missing_gates}

    def _scope_conflict(self, conn, offer, *, exclude_partner: str | None) -> str | None:
        """返回首个冲突描述；无冲突返回 None。

        - 在效合同按授权期间 [effective_date, end_date] 是否区间重叠判定，
          因此顺序衔接（不重叠期间）的两份授权可以共存；
        - 保留锁是短期独家选择权，在其有效期内一律阻断同范围的他方锁定。
        """

        terms = json.loads(offer["terms_json"])
        candidates = self.rights_occupancy(offer["work_id"])["occupancy"]
        win_start = terms["effective_date"]
        win_end = terms.get("end_date")
        for item in candidates:
            if exclude_partner and item["partner_id"] == exclude_partner:
                continue
            if item["source"] == "contract" and not self._windows_overlap(
                    win_start, win_end, item.get("effective_date"), item.get("end_date")):
                continue
            for territory in terms["territories"]:
                for other_t in item["territories"]:
                    if not scope_overlap(territory, other_t, "-"):
                        continue
                    for channel in terms["channels"]:
                        for other_c in item["channels"]:
                            if scope_overlap(channel, other_c, ":", "."):
                                return (f"{territory}/{channel} 与 {item['source']} "
                                        f"{item['source_id']} 的 {other_t}/{other_c} 重叠")
        return None

    @staticmethod
    def _windows_overlap(start_a: str, end_a: str | None,
                         start_b: str | None, end_b: str | None) -> bool:
        """闭区间重叠判定；None 表示开放端点。"""

        if end_a and start_b and end_a < start_b:
            return False
        if end_b and start_a and end_b < start_a:
            return False
        return True


def _light_actor(conn, actor_id: str):
    from .models import Actor

    row = conn.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
    if row is None:
        raise NotFoundError("签署操作者不存在")
    return Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"],
                 bool(row["active"]))
