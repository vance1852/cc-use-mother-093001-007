"""登记合作方、团队、作品版本以及成本与分成口径。"""

from __future__ import annotations

import json
from typing import Any

from .audit import canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .merch_common import MerchBase, new_id
from .merch_money import validate_share_lines
from .models import WriteReceipt

PARTNER_TYPES = ("museum_shop", "tea_brand", "ecommerce", "other")
DECISIONS = ("approved", "rejected", "suspended")


class RegistryService(MerchBase):
    """管理资质、主体、版本和口径表的登记。"""

    # -- 合作方 ---------------------------------------------------------------

    def register_partner(self, *, request_id: str, actor_id: str, partner_id: str,
                         organization_id: str, partner_type: str, name: str,
                         license_no: str | None = None) -> WriteReceipt:
        payload = locals_snapshot(**{k: v for k, v in locals().items() if k != "self"})
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="register_partner", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator")
            partner_id = self.id_value(partner_id, "partner_id")
            organization_id = self.id_value(organization_id, "organization_id")
            name = self.text(name, "name")
            license_no = self.opt_text(license_no, "license_no", 120)
            if partner_type not in PARTNER_TYPES:
                raise ValidationError("partner_type 不在允许范围内")
            if conn.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                            (organization_id,)).fetchone() is None:
                raise NotFoundError("合作方所属机构必须先登记")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO partners(partner_id,organization_id,partner_type,name,"
                        "license_no,created_at) VALUES(?,?,?,?,?,?)",
                        (partner_id, organization_id, partner_type, name, license_no, self.now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("合作方编号已经存在或机构已绑定其他合作方") from exc
                self.audit(conn, actor_id=actor_id, action="partner.registered",
                           resource_type="partner", resource_id=partner_id,
                           detail={"organization_id": organization_id, "partner_type": partner_type,
                                   "name": name})
                return "partner", partner_id, {"partner_id": partner_id}

            return self.idempotent(conn, request_id=request_id, action="register_partner",
                                   payload=payload, create=create)

    def review_qualification(self, *, request_id: str, actor_id: str, partner_id: str,
                             decision: str, valid_until: str | None = None,
                             note: str | None = None) -> WriteReceipt:
        payload = locals_snapshot(request_id=request_id, actor_id=actor_id, partner_id=partner_id,
                                  decision=decision, valid_until=valid_until, note=note)
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="review_qualification", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator", "reviewer")
            self.require_row(conn, "partners", "partner_id", partner_id, "合作方不存在")
            if decision not in DECISIONS:
                raise ValidationError("decision 只能是 approved、rejected 或 suspended")
            valid_until_text = self.day(valid_until, "valid_until") if valid_until else None
            if decision == "approved" and not valid_until_text:
                raise ValidationError("资质通过必须给出有效期")
            note_text = self.opt_text(note, "note", 1000)

            def create():
                qid = new_id()
                conn.execute(
                    "INSERT INTO partner_qualifications(qualification_id,partner_id,decision,"
                    "valid_until,note,reviewed_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (qid, partner_id, decision, valid_until_text, note_text, actor_id, self.now_text()),
                )
                self.audit(conn, actor_id=actor_id, action="partner.qualification_reviewed",
                           resource_type="partner", resource_id=partner_id,
                           detail={"qualification_id": qid, "decision": decision,
                                   "valid_until": valid_until_text})
                return "qualification", qid, {"qualification_id": qid, "partner_id": partner_id,
                                              "decision": decision}

            return self.idempotent(conn, request_id=request_id, action="review_qualification",
                                   payload=payload, create=create)

    def bind_representative(self, *, request_id: str, actor_id: str, partner_id: str,
                            representative_actor_id: str, can_sign: bool) -> WriteReceipt:
        payload = locals_snapshot(request_id=request_id, actor_id=actor_id, partner_id=partner_id,
                                  representative_actor_id=representative_actor_id, can_sign=can_sign)
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="bind_representative", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator")
            partner = self.require_row(conn, "partners", "partner_id", partner_id, "合作方不存在")
            rep = self.actor(conn, representative_actor_id)
            if rep.organization_id != partner["organization_id"]:
                raise PermissionDenied("代表必须属于合作方所属机构")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO partner_representatives(partner_id,actor_id,can_sign,active,created_at)"
                        " VALUES(?,?,?,1,?)",
                        (partner_id, representative_actor_id, 1 if can_sign else 0, self.now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("该操作者已经绑定为合作方代表") from exc
                self.audit(conn, actor_id=actor_id, action="partner.representative_bound",
                           resource_type="partner", resource_id=partner_id,
                           detail={"representative_actor_id": representative_actor_id,
                                   "can_sign": bool(can_sign)})
                return "representative", representative_actor_id, {
                    "partner_id": partner_id, "actor_id": representative_actor_id}

            return self.idempotent(conn, request_id=request_id, action="bind_representative",
                                   payload=payload, create=create)

    # -- 团队 -----------------------------------------------------------------

    def register_team(self, *, request_id: str, actor_id: str, team_id: str,
                      organization_id: str, name: str,
                      required_countersign_roles: list[str]) -> WriteReceipt:
        payload = locals_snapshot(request_id=request_id, actor_id=actor_id, team_id=team_id,
                                  organization_id=organization_id, name=name,
                                  required_countersign_roles=required_countersign_roles)
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="register_team", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator")
            team_id = self.id_value(team_id, "team_id")
            organization_id = self.id_value(organization_id, "organization_id")
            name = self.text(name, "name")
            roles = self._role_list(required_countersign_roles)
            if conn.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                            (organization_id,)).fetchone() is None:
                raise NotFoundError("团队所属机构不存在")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO teams(team_id,organization_id,name,"
                        "required_countersign_roles_json,created_at) VALUES(?,?,?,?,?)",
                        (team_id, organization_id, name, canonical_json(roles), self.now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("团队编号已经存在") from exc
                self.audit(conn, actor_id=actor_id, action="team.registered",
                           resource_type="team", resource_id=team_id,
                           detail={"organization_id": organization_id, "name": name,
                                   "required_countersign_roles": roles})
                return "team", team_id, {"team_id": team_id}

            return self.idempotent(conn, request_id=request_id, action="register_team",
                                   payload=payload, create=create)

    def add_team_member(self, *, request_id: str, actor_id: str, team_id: str,
                        member_actor_id: str, member_role: str) -> WriteReceipt:
        payload = locals_snapshot(request_id=request_id, actor_id=actor_id, team_id=team_id,
                                  member_actor_id=member_actor_id, member_role=member_role)
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="add_team_member", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator")
            team = self.require_row(conn, "teams", "team_id", team_id, "团队不存在")
            member = self.actor(conn, member_actor_id)
            member_role = self.code_value(member_role, "member_role")
            if member.organization_id != team["organization_id"]:
                raise PermissionDenied("成员必须属于团队所属机构")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO team_members(team_id,actor_id,member_role,active,created_at)"
                        " VALUES(?,?,?,1,?)",
                        (team_id, member_actor_id, member_role, self.now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("该成员已经加入团队") from exc
                self.audit(conn, actor_id=actor_id, action="team.member_added",
                           resource_type="team", resource_id=team_id,
                           detail={"member_actor_id": member_actor_id, "member_role": member_role})
                return "team_member", member_actor_id, {"team_id": team_id,
                                                        "actor_id": member_actor_id}

            return self.idempotent(conn, request_id=request_id, action="add_team_member",
                                   payload=payload, create=create)

    def grant_signing(self, *, request_id: str, actor_id: str, team_id: str,
                      member_actor_id: str, scope_type: str = "team",
                      scope_id: str | None = None, valid_until: str | None = None,
                      note: str | None = None) -> WriteReceipt:
        payload = locals_snapshot(request_id=request_id, actor_id=actor_id, team_id=team_id,
                                  member_actor_id=member_actor_id, scope_type=scope_type,
                                  scope_id=scope_id, valid_until=valid_until, note=note)
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="grant_signing", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator")
            self.require_row(conn, "teams", "team_id", team_id, "团队不存在")
            self.actor(conn, member_actor_id)
            if conn.execute("SELECT 1 FROM team_members WHERE team_id=? AND actor_id=? AND active=1",
                            (team_id, member_actor_id)).fetchone() is None:
                raise ValidationError("被授权人必须是团队在职成员")
            if scope_type not in ("team", "work"):
                raise ValidationError("scope_type 只能是 team 或 work")
            if scope_type == "work":
                scope_id = self.id_value(scope_id or "", "scope_id")
                if conn.execute("SELECT 1 FROM works WHERE work_id=? AND team_id=?",
                                (scope_id, team_id)).fetchone() is None:
                    raise NotFoundError("授权指向的作品不存在")
            else:
                scope_id = ""
            valid_until_text = self.stamp(valid_until, "valid_until") if valid_until else None
            note_text = self.opt_text(note, "note", 500)

            def create():
                grant_id = new_id()
                conn.execute(
                    "INSERT INTO signing_grants(grant_id,team_id,actor_id,scope_type,scope_id,"
                    "status,valid_from,valid_until,granted_by,note,created_at)"
                    " VALUES(?,?,?,?,?,'active',?,?,?,?,?)",
                    (grant_id, team_id, member_actor_id, scope_type, scope_id,
                     self.now_text(), valid_until_text, actor_id, note_text, self.now_text()),
                )
                self.audit(conn, actor_id=actor_id, action="signing.granted",
                           resource_type="signing_grant", resource_id=grant_id,
                           detail={"team_id": team_id, "member_actor_id": member_actor_id,
                                   "scope_type": scope_type, "scope_id": scope_id,
                                   "valid_until": valid_until_text})
                return "signing_grant", grant_id, {"grant_id": grant_id}

            return self.idempotent(conn, request_id=request_id, action="grant_signing",
                                   payload=payload, create=create)

    def revoke_signing(self, *, request_id: str, actor_id: str, grant_id: str) -> WriteReceipt:
        payload = locals_snapshot(request_id=request_id, actor_id=actor_id, grant_id=grant_id)
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="revoke_signing", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator")
            grant = self.require_row(conn, "signing_grants", "grant_id", grant_id, "授权不存在")
            if grant["status"] != "active":
                raise ConflictError("授权已经失效")

            def create():
                conn.execute("UPDATE signing_grants SET status='revoked' WHERE grant_id=?", (grant_id,))
                self.audit(conn, actor_id=actor_id, action="signing.revoked",
                           resource_type="signing_grant", resource_id=grant_id,
                           detail={"team_id": grant["team_id"], "actor_id": grant["actor_id"]})
                return "signing_grant", grant_id, {"grant_id": grant_id, "status": "revoked"}

            return self.idempotent(conn, request_id=request_id, action="revoke_signing",
                                   payload=payload, create=create)

    # -- 作品与版本 ------------------------------------------------------------

    def register_work(self, *, request_id: str, actor_id: str, work_id: str, team_id: str,
                      title: str, award_name: str | None = None,
                      metadata: dict[str, Any] | None = None) -> WriteReceipt:
        metadata = metadata or {}
        payload = locals_snapshot(request_id=request_id, actor_id=actor_id, work_id=work_id,
                                  team_id=team_id, title=title, award_name=award_name,
                                  metadata=metadata)
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="register_work", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator", "reviewer", "team_member")
            team = self.require_row(conn, "teams", "team_id", team_id, "团队不存在")
            work_id = self.id_value(work_id, "work_id")
            title = self.text(title, "title")
            award_text = self.opt_text(award_name, "award_name")
            if not isinstance(metadata, dict):
                raise ValidationError("metadata 必须是对象")
            if actor.role == "team_member" and self.team_member(conn, actor, team_id) is None:
                raise PermissionDenied("只能为自己所在团队登记作品")
            if actor.organization_id != team["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能为其他组织的团队登记作品")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO works(work_id,team_id,title,award_name,metadata_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (work_id, team_id, title, award_text, canonical_json(metadata),
                         actor_id, self.now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("作品编号已经存在") from exc
                self.audit(conn, actor_id=actor_id, action="work.registered",
                           resource_type="work", resource_id=work_id,
                           detail={"team_id": team_id, "title": title, "award_name": award_text})
                return "work", work_id, {"work_id": work_id}

            return self.idempotent(conn, request_id=request_id, action="register_work",
                                   payload=payload, create=create)

    def add_design_version(self, *, request_id: str, actor_id: str, work_id: str,
                           version_code: str, content_hash: str,
                           note: str | None = None,
                           prerequisites: list[dict[str, str]] | None = None) -> WriteReceipt:
        prerequisites = prerequisites or []
        payload = locals_snapshot(request_id=request_id, actor_id=actor_id, work_id=work_id,
                                  version_code=version_code, content_hash=content_hash,
                                  note=note, prerequisites=prerequisites)
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="add_design_version", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator", "reviewer", "team_member")
            work = self.require_row(conn, "works", "work_id", work_id, "作品不存在")
            if actor.role == "team_member" and self.team_member(conn, actor, work["team_id"]) is None:
                raise PermissionDenied("只能为自己团队的作品新增设计版本")
            version_code = self.code_value(version_code, "version_code")
            content_hash = self.text(content_hash, "content_hash", 128)
            note_text = self.opt_text(note, "note", 1000)
            gates: list[tuple[str, str]] = []
            seen: set[str] = set()
            for gate in prerequisites:
                code = self.code_value(gate.get("code", ""), "prerequisites.code")
                label = self.text(gate.get("label", ""), "prerequisites.label")
                if code in seen:
                    raise ValidationError(f"前置条件 {code} 重复")
                seen.add(code)
                gates.append((code, label))

            def create():
                version_id = new_id()
                try:
                    conn.execute(
                        "INSERT INTO design_versions(version_id,work_id,version_code,content_hash,"
                        "note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (version_id, work_id, version_code, content_hash, note_text,
                         actor_id, self.now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("作品下的版本号已经存在") from exc
                for code, label in gates:
                    conn.execute(
                        "INSERT INTO version_prerequisites(version_id,code,label) VALUES(?,?,?)",
                        (version_id, code, label),
                    )
                self.audit(conn, actor_id=actor_id, action="design_version.added",
                           resource_type="design_version", resource_id=version_id,
                           detail={"work_id": work_id, "version_code": version_code,
                                   "content_hash": content_hash,
                                   "prerequisites": [c for c, _ in gates]})
                return "design_version", version_id, {"version_id": version_id,
                                                      "version_code": version_code}

            return self.idempotent(conn, request_id=request_id, action="add_design_version",
                                   payload=payload, create=create)

    def countersign_version(self, *, request_id: str, actor_id: str, version_id: str) -> WriteReceipt:
        payload = locals_snapshot(request_id=request_id, actor_id=actor_id, version_id=version_id)
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="countersign_version", payload=payload)
            if _replay is not None:
                return _replay
            version = self.require_row(conn, "design_versions", "version_id", version_id,
                                       "设计版本不存在")
            work = self.require_row(conn, "works", "work_id", version["work_id"], "作品不存在")
            membership = self.team_member(conn, actor, work["team_id"])
            if membership is None and actor.role != "admin":
                raise PermissionDenied("只有团队成员可以会签设计版本")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO design_signatures(version_id,actor_id,created_at) VALUES(?,?,?)",
                        (version_id, actor_id, self.now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("该成员已经会签此版本") from exc
                self.audit(conn, actor_id=actor_id, action="design_version.countersigned",
                           resource_type="design_version", resource_id=version_id,
                           detail={"work_id": version["work_id"],
                                   "member_role": membership["member_role"] if membership else "admin"})
                return "design_version", version_id, {"version_id": version_id,
                                                      "signed_by": actor_id}

            return self.idempotent(conn, request_id=request_id, action="countersign_version",
                                   payload=payload, create=create)

    def complete_version_prerequisite(self, *, request_id: str, actor_id: str, version_id: str,
                                      code: str, note: str | None = None) -> WriteReceipt:
        payload = locals_snapshot(request_id=request_id, actor_id=actor_id, version_id=version_id,
                                  code=code, note=note)
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="complete_version_prerequisite", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator", "reviewer")
            version = self.require_row(conn, "design_versions", "version_id", version_id,
                                       "设计版本不存在")
            code = self.code_value(code, "code")
            gate = conn.execute(
                "SELECT 1 FROM version_prerequisites WHERE version_id=? AND code=?",
                (version_id, code),
            ).fetchone()
            if gate is None:
                raise NotFoundError("该版本没有此前置条件")
            note_text = self.opt_text(note, "note", 1000)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO version_prerequisite_facts(version_id,code,completed_by,"
                        "note,created_at) VALUES(?,?,?,?,?)",
                        (version_id, code, actor_id, note_text, self.now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("该前置条件已经完成") from exc
                self.audit(conn, actor_id=actor_id, action="design_version.prerequisite_completed",
                           resource_type="design_version", resource_id=version_id,
                           detail={"work_id": version["work_id"], "code": code})
                return ("prerequisite_fact", f"{version_id}:{code}",
                        {"version_id": version_id, "code": code})

            return self.idempotent(conn, request_id=request_id,
                                   action="complete_version_prerequisite",
                                   payload=payload, create=create)

    # -- 口径表 ----------------------------------------------------------------

    def register_cost_sheet(self, *, request_id: str, actor_id: str, work_id: str,
                            lines: list[dict[str, Any]]) -> WriteReceipt:
        payload = locals_snapshot(request_id=request_id, actor_id=actor_id, work_id=work_id,
                                  lines=lines)
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="register_cost_sheet", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator")
            self.require_row(conn, "works", "work_id", work_id, "作品不存在")
            normalized = self._cost_lines(lines)

            def create():
                version_no = (conn.execute(
                    "SELECT COALESCE(MAX(version_no),0)+1 AS next FROM cost_sheets WHERE work_id=?",
                    (work_id,)).fetchone()["next"])
                sheet_id = new_id()
                conn.execute(
                    "INSERT INTO cost_sheets(cost_sheet_id,work_id,version_no,lines_json,"
                    "sheet_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (sheet_id, work_id, version_no, canonical_json(normalized),
                     digest(normalized), actor_id, self.now_text()),
                )
                self.audit(conn, actor_id=actor_id, action="cost_sheet.registered",
                           resource_type="cost_sheet", resource_id=sheet_id,
                           detail={"work_id": work_id, "version_no": version_no,
                                   "sheet_hash": digest(normalized)})
                return "cost_sheet", sheet_id, {"cost_sheet_id": sheet_id,
                                                "version_no": version_no}

            return self.idempotent(conn, request_id=request_id, action="register_cost_sheet",
                                   payload=payload, create=create)

    def register_share_sheet(self, *, request_id: str, actor_id: str, team_id: str,
                             lines: list[dict[str, Any]]) -> WriteReceipt:
        payload = locals_snapshot(request_id=request_id, actor_id=actor_id, team_id=team_id,
                                  lines=lines)
        with self.database.transaction(immediate=True) as conn:
            actor = self.actor(conn, actor_id)
            _replay = self.replay_if_seen(
                conn, request_id=request_id, action="register_share_sheet", payload=payload)
            if _replay is not None:
                return _replay
            self.require_roles(actor, "admin", "operator")
            self.require_row(conn, "teams", "team_id", team_id, "团队不存在")
            try:
                normalized = validate_share_lines(lines)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            for line in normalized:
                if conn.execute("SELECT 1 FROM team_members WHERE team_id=? AND actor_id=? AND active=1",
                                (team_id, line["actor_id"])).fetchone() is None:
                    raise ValidationError(f"分成成员 {line['actor_id']} 不是团队在职成员")

            def create():
                version_no = (conn.execute(
                    "SELECT COALESCE(MAX(version_no),0)+1 AS next FROM share_sheets WHERE team_id=?",
                    (team_id,)).fetchone()["next"])
                sheet_id = new_id()
                conn.execute(
                    "INSERT INTO share_sheets(share_sheet_id,team_id,version_no,lines_json,"
                    "sheet_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (sheet_id, team_id, version_no, canonical_json(normalized),
                     digest(normalized), actor_id, self.now_text()),
                )
                self.audit(conn, actor_id=actor_id, action="share_sheet.registered",
                           resource_type="share_sheet", resource_id=sheet_id,
                           detail={"team_id": team_id, "version_no": version_no,
                                   "sheet_hash": digest(normalized)})
                return "share_sheet", sheet_id, {"share_sheet_id": sheet_id,
                                                 "version_no": version_no}

            return self.idempotent(conn, request_id=request_id, action="register_share_sheet",
                                   payload=payload, create=create)

    # -- 读取 ------------------------------------------------------------------

    def get_version_readiness(self, version_id: str) -> dict[str, Any]:
        conn = self.database.connection
        version = self.require_row(conn, "design_versions", "version_id", version_id,
                                   "设计版本不存在")
        work = self.require_row(conn, "works", "work_id", version["work_id"], "作品不存在")
        team = self.require_row(conn, "teams", "team_id", work["team_id"], "团队不存在")
        required_roles = set(json.loads(team["required_countersign_roles_json"]))
        signature_rows = conn.execute(
            "SELECT tm.member_role FROM design_signatures ds JOIN team_members tm "
            "ON tm.actor_id=ds.actor_id AND tm.team_id=? WHERE ds.version_id=?",
            (work["team_id"], version_id),
        ).fetchall()
        signed_roles = {row["member_role"] for row in signature_rows}
        gates = [dict(row) for row in conn.execute(
            "SELECT vp.code,vp.label, CASE WHEN vf.code IS NULL THEN 0 ELSE 1 END AS completed "
            "FROM version_prerequisites vp LEFT JOIN version_prerequisite_facts vf "
            "ON vf.version_id=vp.version_id AND vf.code=vp.code WHERE vp.version_id=?",
            (version_id,))]
        missing_roles = sorted(required_roles - signed_roles)
        missing_gates = [g["code"] for g in gates if not g["completed"]]
        return {
            "version_id": version_id, "work_id": version["work_id"],
            "version_code": version["version_code"], "content_hash": version["content_hash"],
            "required_roles": sorted(required_roles), "signed_roles": sorted(signed_roles),
            "missing_roles": missing_roles, "prerequisites": gates,
            "missing_prerequisites": missing_gates,
            "approved": not missing_roles and not missing_gates,
        }

    # -- 内部 ------------------------------------------------------------------

    def _role_list(self, values: Any) -> list[str]:
        if not isinstance(values, list) or not values:
            raise ValidationError("required_countersign_roles 必须是非空数组")
        result = []
        seen: set[str] = set()
        for value in values:
            code = self.code_value(value, "required_countersign_roles")
            if code in seen:
                raise ValidationError(f"会签角色 {code} 重复")
            seen.add(code)
            result.append(code)
        return result

    def _cost_lines(self, lines: Any) -> list[dict[str, Any]]:
        from .merch_money import money

        if not isinstance(lines, list) or not lines:
            raise ValidationError("成本行必须是非空数组")
        normalized: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                raise ValidationError(f"成本行{index + 1}必须是对象")
            label = self.text(line.get("label"), f"成本行{index + 1}.label")
            basis = str(line.get("basis", "fixed"))
            if basis not in ("fixed", "unit"):
                raise ValidationError(f"成本行{index + 1}.basis 只能是 fixed 或 unit")
            try:
                amount = money(line.get("amount"), f"成本行{index + 1}.amount")
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            normalized.append({"label": label, "basis": basis, "amount": str(amount)})
        return normalized


def locals_snapshot(**kwargs: Any) -> dict[str, Any]:
    """构造幂等载荷时的显式参数快照。"""

    return {key: value for key, value in kwargs.items() if value is not None}
