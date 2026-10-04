"""作品商品化合作管理服务。

在基础服务的组织、操作者、幂等回执和哈希审计能力之上，把合作方资质、作品与
设计版本、报价要约、独家地域与渠道、样品确认、最低承诺、成本口径、收益分配、
交付里程碑和签署权限组织成可追踪的生命周期：

    意向(协商) → 要约 → 反要约 → 保留/解除保留 → 接受 → 会签 → 前置里程碑 → 履约

撤回与终止在各自阶段追加事实。所有状态迁移按顺序校验：反要约只能针对最新的
未决要约，保留中的要约必须先解除才能接受或反要约，接受会校验独家范围不与
既有有效合同冲突，只有会签齐全且前置里程碑完成的合同才能进入履约。

合同签署后只追加事实（设计变更、部分交付、违约整改、成本与分成修订、结算
争议），原合同条款不被篡改；审计查询可以解释某时点哪些权利仍被占用、某笔
分成采用了哪一版成本和成员份额。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit import append_event, canonical_json
from .clock import Clock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .service import IDENTIFIER, DomainService
from .storage import Database

TEAM_ROLES = ("admin", "operator")
READ_ROLES = ("admin", "operator", "reviewer", "auditor")
LIVE_OFFER_STATUS = ("open", "reserved")
PARTNER_MEMBER = "partner"


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{field} 必须是对象")
    return value


def _positive_int(value: Any, field: str, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是整数")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValidationError(f"{field} 必须是正整数")
    return value


def _str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{field} 必须是非空数组")
    result = []
    for item in value:
        item = str(item).strip()
        if not item or len(item) > 80:
            raise ValidationError(f"{field} 含有无效条目")
        result.append(item)
    if len(set(result)) != len(result):
        raise ValidationError(f"{field} 含有重复条目")
    return result


def _validate_quote(value: Any) -> dict[str, Any]:
    value = _require_dict(value, "quote")
    currency = str(value.get("currency", "")).strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValidationError("quote.currency 必须是三字母货币代码")
    price = _positive_int(value.get("unit_price_cents"), "quote.unit_price_cents", allow_zero=True)
    return {"currency": currency, "unit_price_cents": price}


def _validate_min_commitment(value: Any) -> dict[str, Any]:
    value = _require_dict(value, "min_commitment")
    quantity = _positive_int(value.get("quantity"), "min_commitment.quantity")
    period = str(value.get("period", "")).strip()
    if not period or len(period) > 40:
        raise ValidationError("min_commitment.period 不能为空且不能超过 40 个字符")
    return {"quantity": quantity, "period": period}


def _validate_cost_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValidationError("cost_items 必须是数组")
    result = []
    for item in value:
        item = _require_dict(item, "cost_items[]")
        name = str(item.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ValidationError("成本条目名称不能为空")
        kind = item.get("kind")
        if kind == "fixed":
            amount = _positive_int(item.get("amount_cents"), "cost_items.amount_cents", allow_zero=True)
            result.append({"name": name, "kind": "fixed", "amount_cents": amount})
        elif kind == "rate":
            bps = _positive_int(item.get("rate_bps"), "cost_items.rate_bps", allow_zero=True)
            if bps > 10000:
                raise ValidationError("成本比例不能超过 10000 个基点")
            result.append({"name": name, "kind": "rate", "rate_bps": bps})
        else:
            raise ValidationError("成本条目 kind 必须是 fixed 或 rate")
    return result


def _validate_shares(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValidationError("shares 必须是非空数组")
    total = 0
    members: set[str] = set()
    result = []
    for item in value:
        item = _require_dict(item, "shares[]")
        member = str(item.get("member", "")).strip()
        if not member or len(member) > 80 or member in members:
            raise ValidationError("分成成员无效或重复")
        bps = _positive_int(item.get("bps"), "shares.bps")
        if bps > 10000:
            raise ValidationError("单个分成比例不能超过 10000 个基点")
        members.add(member)
        total += bps
        result.append({"member": member, "bps": bps})
    if total != 10000:
        raise ValidationError("分成比例合计必须等于 10000 个基点")
    return result


def _validate_milestones(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValidationError("milestones 必须是非空数组")
    seen: set[str] = set()
    result = []
    for position, item in enumerate(value):
        item = _require_dict(item, "milestones[]")
        key = str(item.get("key", "")).strip()
        if not IDENTIFIER.fullmatch(key):
            raise ValidationError("milestone key 格式无效")
        if key in seen:
            raise ValidationError("milestone key 重复")
        title = str(item.get("title", "")).strip()
        if not title or len(title) > 120:
            raise ValidationError("milestone title 不能为空")
        depends_on = item.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise ValidationError("milestone depends_on 必须是数组")
        deps = []
        for dep in depends_on:
            dep = str(dep).strip()
            if dep not in seen:
                raise ValidationError("milestone 依赖必须指向更早声明的里程碑")
            deps.append(dep)
        seen.add(key)
        result.append({"key": key, "title": title, "gate": bool(item.get("gate", False)),
                       "depends_on": deps, "position": position})
    return result


def _parse_instant(value: Any, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValidationError(f"{field} 不能为空")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(f"{field} 必须是 ISO-8601 时间") from None
    if moment.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _scope_cells(territories: list[str], channels: list[str]) -> set[tuple[str, str]]:
    return {(territory, channel) for territory in territories for channel in channels}


def _compute_settlement(gross: int, cost_items: list[dict[str, Any]],
                        shares: list[dict[str, Any]]) -> dict[str, Any]:
    """按指定版本的成本口径和成员份额计算结算分录。"""

    fixed = sum(item["amount_cents"] for item in cost_items if item["kind"] == "fixed")
    rated = sum(gross * item["rate_bps"] // 10000 for item in cost_items if item["kind"] == "rate")
    cost_total = fixed + rated
    net = gross - cost_total
    if net < 0:
        raise ValidationError("结算收入不足以覆盖成本口径")
    lines = []
    allocated = 0
    for share in shares:
        amount = net * share["bps"] // 10000
        lines.append({"member": share["member"], "bps": share["bps"], "amount_cents": amount})
        allocated += amount
    remainder = net - allocated
    if remainder:
        target = sorted(shares, key=lambda item: (-item["bps"], item["member"]))[0]["member"]
        for line in lines:
            if line["member"] == target:
                line["amount_cents"] += remainder
                break
    return {"gross_cents": gross, "cost_total_cents": cost_total, "net_cents": net, "lines": lines}


class CommercializationService:
    """协调作品商品化合作的权限、时序、幂等、事务和审计规则。

    通过 __getattr__ 委托基础服务的登记与审计查询方法，HTTP 层只需持有本类实例。
    """

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.base = DomainService(database, clock)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    def _now(self) -> str:
        return self.base.clock.now().astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    # ------------------------------------------------------------------
    # 登记：合作方资质、联系人、作品、设计版本
    # ------------------------------------------------------------------

    def register_partner(self, *, request_id: str, actor_id: str, partner_id: str,
                         organization_id: str, name: str,
                         qualification: dict[str, Any]) -> WriteReceipt:
        """登记合作方及其资质，资质需包含证照编号。"""

        qualification = _require_dict(qualification, "qualification")
        if not str(qualification.get("license_no", "")).strip():
            raise ValidationError("qualification.license_no 不能为空")
        payload = {"actor_id": actor_id, "partner_id": partner_id, "organization_id": organization_id,
                   "name": name, "qualification": qualification}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                if actor.organization_id != organization_id and actor.role != "admin":
                    raise PermissionDenied("不能为其他组织登记合作方")
                partner_id_checked = self.base._identifier(partner_id, "partner_id")
                name_checked = self.base._text(name, "name")
                if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                      (organization_id,)).fetchone() is None:
                    raise NotFoundError("组织不存在")
                try:
                    connection.execute(
                        "INSERT INTO partners(partner_id,organization_id,name,qualification_json,status,created_at) "
                        "VALUES(?,?,?,?,'active',?)",
                        (partner_id_checked, organization_id, name_checked,
                         canonical_json(qualification), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("合作方编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="partner.registered",
                             resource_type="partner", resource_id=partner_id_checked,
                             detail={"name": name_checked, "organization_id": organization_id,
                                     "qualification": qualification}, occurred_at=self._now())
                return "partner", partner_id_checked, {"partner_id": partner_id_checked}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="register_partner", payload=payload, create=create)

    def link_partner_member(self, *, request_id: str, actor_id: str, partner_id: str,
                            member_actor_id: str) -> WriteReceipt:
        """把合作方联系人（partner 角色的操作者）绑定到合作方。"""

        payload = {"actor_id": actor_id, "partner_id": partner_id, "member_actor_id": member_actor_id}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                if connection.execute("SELECT 1 FROM partners WHERE partner_id=?",
                                      (partner_id,)).fetchone() is None:
                    raise NotFoundError("合作方不存在")
                member = self.base._actor(connection, member_actor_id)
                if member.role != "partner":
                    raise ValidationError("联系人必须是 partner 角色的操作者")
                try:
                    connection.execute(
                        "INSERT INTO partner_members(partner_id,actor_id,created_at) VALUES(?,?,?)",
                        (partner_id, member_actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该联系人已绑定此合作方") from exc
                append_event(connection, actor_id=actor_id, action="partner.member_linked",
                             resource_type="partner", resource_id=partner_id,
                             detail={"member_actor_id": member_actor_id}, occurred_at=self._now())
                return "partner", partner_id, {"partner_id": partner_id, "member_actor_id": member_actor_id}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="link_partner_member", payload=payload, create=create)

    def register_work(self, *, request_id: str, actor_id: str, work_id: str,
                      organization_id: str, title: str,
                      award: dict[str, Any] | None = None) -> WriteReceipt:
        """登记获奖作品。"""

        award = _require_dict(award or {}, "award")
        payload = {"actor_id": actor_id, "work_id": work_id, "organization_id": organization_id,
                   "title": title, "award": award}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                if actor.organization_id != organization_id and actor.role != "admin":
                    raise PermissionDenied("不能为其他组织登记作品")
                work_id_checked = self.base._identifier(work_id, "work_id")
                title_checked = self.base._text(title, "title")
                if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                      (organization_id,)).fetchone() is None:
                    raise NotFoundError("组织不存在")
                try:
                    connection.execute(
                        "INSERT INTO works(work_id,organization_id,title,award_json,created_at) VALUES(?,?,?,?,?)",
                        (work_id_checked, organization_id, title_checked,
                         canonical_json(award), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("作品编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="work.registered",
                             resource_type="work", resource_id=work_id_checked,
                             detail={"title": title_checked, "award": award}, occurred_at=self._now())
                return "work", work_id_checked, {"work_id": work_id_checked}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="register_work", payload=payload, create=create)

    def add_design_version(self, *, request_id: str, actor_id: str, work_id: str,
                           design_version_id: str, version_no: int, summary: str,
                           spec: dict[str, Any] | None = None) -> WriteReceipt:
        """为作品追加一个设计版本，版本号必须按 1、2、3… 顺序递增。"""

        spec = _require_dict(spec or {}, "spec")
        _positive_int(version_no, "version_no")
        payload = {"actor_id": actor_id, "work_id": work_id, "design_version_id": design_version_id,
                   "version_no": version_no, "summary": summary, "spec": spec}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                if connection.execute("SELECT 1 FROM works WHERE work_id=?", (work_id,)).fetchone() is None:
                    raise NotFoundError("作品不存在")
                design_id = self.base._identifier(design_version_id, "design_version_id")
                summary_checked = self.base._text(summary, "summary")
                row = connection.execute(
                    "SELECT MAX(version_no) AS max_no FROM design_versions WHERE work_id=?", (work_id,)
                ).fetchone()
                expected = (row["max_no"] or 0) + 1
                if version_no != expected:
                    raise ValidationError(f"设计版本号必须为 {expected}")
                try:
                    connection.execute(
                        "INSERT INTO design_versions(design_version_id,work_id,version_no,summary,spec_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (design_id, work_id, version_no, summary_checked,
                         canonical_json(spec), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("设计版本编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="design_version.added",
                             resource_type="design_version", resource_id=design_id,
                             detail={"work_id": work_id, "version_no": version_no,
                                     "summary": summary_checked}, occurred_at=self._now())
                return "design_version", design_id, {"design_version_id": design_id}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="add_design_version", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 协商：意向、要约、反要约、保留、撤回、接受
    # ------------------------------------------------------------------

    def open_negotiation(self, *, request_id: str, actor_id: str, negotiation_id: str,
                         work_id: str, partner_id: str, note: str = "") -> WriteReceipt:
        """登记合作意向，开启一段协商。团队或合作方联系人均可发起。"""

        payload = {"actor_id": actor_id, "negotiation_id": negotiation_id, "work_id": work_id,
                   "partner_id": partner_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                partner = connection.execute("SELECT * FROM partners WHERE partner_id=?",
                                             (partner_id,)).fetchone()
                if partner is None:
                    raise NotFoundError("合作方不存在")
                if partner["status"] != "active":
                    raise ValidationError("合作方资质已暂停，不能开启协商")
                if connection.execute("SELECT 1 FROM works WHERE work_id=?", (work_id,)).fetchone() is None:
                    raise NotFoundError("作品不存在")
                if actor.role in TEAM_ROLES:
                    pass
                elif actor.role == "partner":
                    self._require_partner_member(connection, actor.actor_id, partner_id)
                else:
                    raise PermissionDenied("当前角色不能发起合作意向")
                negotiation_id_checked = self.base._identifier(negotiation_id, "negotiation_id")
                try:
                    connection.execute(
                        "INSERT INTO negotiations(negotiation_id,work_id,partner_id,status,created_at) "
                        "VALUES(?,?,?,'open',?)",
                        (negotiation_id_checked, work_id, partner_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("协商编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="negotiation.opened",
                             resource_type="negotiation", resource_id=negotiation_id_checked,
                             detail={"work_id": work_id, "partner_id": partner_id, "note": note},
                             occurred_at=self._now())
                return "negotiation", negotiation_id_checked, {"negotiation_id": negotiation_id_checked}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="open_negotiation", payload=payload, create=create)

    def _validate_terms(self, *, design_version_id: Any, quote: Any, exclusive: Any,
                        territories: Any, channels: Any, min_commitment: Any,
                        cost_items: Any, shares: Any, milestones: Any,
                        required_signers: Any) -> dict[str, Any]:
        return {
            "design_version_id": self.base._identifier(str(design_version_id), "design_version_id"),
            "quote": _validate_quote(quote),
            "exclusive": bool(exclusive),
            "territories": _str_list(territories, "territories"),
            "channels": _str_list(channels, "channels"),
            "min_commitment": _validate_min_commitment(min_commitment),
            "cost_items": _validate_cost_items(cost_items),
            "shares": _validate_shares(shares),
            "milestones": _validate_milestones(milestones),
            "required_signers": _str_list(required_signers, "required_signers"),
        }

    def _check_side(self, connection, actor: Actor, partner_id: str, side: str) -> None:
        if side == "team":
            self.base._require(actor, *TEAM_ROLES)
        elif side == "partner":
            if actor.role != "partner":
                raise PermissionDenied("只有合作方联系人能以合作方身份行动")
            self._require_partner_member(connection, actor.actor_id, partner_id)
        else:
            raise ValidationError("side 必须是 team 或 partner")

    def _require_partner_member(self, connection, actor_id: str, partner_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM partner_members WHERE partner_id=? AND actor_id=?",
            (partner_id, actor_id),
        ).fetchone()
        if row is None:
            raise PermissionDenied("操作者不属于该合作方")

    def _check_offer_terms(self, connection, negotiation, terms: dict[str, Any]) -> None:
        design = connection.execute("SELECT * FROM design_versions WHERE design_version_id=?",
                                    (terms["design_version_id"],)).fetchone()
        if design is None or design["work_id"] != negotiation["work_id"]:
            raise ValidationError("设计版本不存在或不属于该作品")
        for signer in terms["required_signers"]:
            self.base._actor(connection, signer)

    def _insert_offer(self, connection, *, negotiation_id: str, parent_offer_id: str | None,
                      side: str, terms: dict[str, Any], actor_id: str) -> tuple[str, int]:
        row = connection.execute(
            "SELECT COALESCE(MAX(offer_no), 0) AS max_no FROM offers WHERE negotiation_id=?",
            (negotiation_id,),
        ).fetchone()
        offer_no = row["max_no"] + 1
        offer_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO offers(offer_id,negotiation_id,offer_no,parent_offer_id,side,status,"
            "terms_json,created_by,created_at) VALUES(?,?,?,?,?,'open',?,?,?)",
            (offer_id, negotiation_id, offer_no, parent_offer_id, side,
             canonical_json(terms), actor_id, self._now()),
        )
        return offer_id, offer_no

    def make_offer(self, *, request_id: str, actor_id: str, negotiation_id: str, side: str,
                   design_version_id: Any, quote: Any, exclusive: Any, territories: Any,
                   channels: Any, min_commitment: Any, cost_items: Any, shares: Any,
                   milestones: Any, required_signers: Any) -> WriteReceipt:
        """在未决要约不存在时发起新要约；存在未决要约时必须走反要约。"""

        terms = self._validate_terms(design_version_id=design_version_id, quote=quote,
                                     exclusive=exclusive, territories=territories, channels=channels,
                                     min_commitment=min_commitment, cost_items=cost_items,
                                     shares=shares, milestones=milestones,
                                     required_signers=required_signers)
        payload = {"actor_id": actor_id, "negotiation_id": negotiation_id, "side": side, "terms": terms}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                negotiation = self._fetch_negotiation(connection, negotiation_id)
                if negotiation["status"] != "open":
                    raise ConflictError("协商已关闭，不能再发起要约")
                self._check_side(connection, actor, negotiation["partner_id"], side)
                live = connection.execute(
                    "SELECT 1 FROM offers WHERE negotiation_id=? AND status IN ('open','reserved')",
                    (negotiation_id,),
                ).fetchone()
                if live is not None:
                    raise ConflictError("存在未决要约，请先反要约、撤回或解除保留")
                self._check_offer_terms(connection, negotiation, terms)
                offer_id, offer_no = self._insert_offer(
                    connection, negotiation_id=negotiation_id, parent_offer_id=None,
                    side=side, terms=terms, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="offer.made",
                             resource_type="offer", resource_id=offer_id,
                             detail={"negotiation_id": negotiation_id, "offer_no": offer_no,
                                     "side": side, "terms": terms}, occurred_at=self._now())
                return "offer", offer_id, {"offer_id": offer_id, "offer_no": offer_no}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="make_offer", payload=payload, create=create)

    def counter_offer(self, *, request_id: str, actor_id: str, offer_id: str, side: str,
                      design_version_id: Any, quote: Any, exclusive: Any, territories: Any,
                      channels: Any, min_commitment: Any, cost_items: Any, shares: Any,
                      milestones: Any, required_signers: Any) -> WriteReceipt:
        """针对最新未决要约提出反要约，原要约随即被取代。"""

        terms = self._validate_terms(design_version_id=design_version_id, quote=quote,
                                     exclusive=exclusive, territories=territories, channels=channels,
                                     min_commitment=min_commitment, cost_items=cost_items,
                                     shares=shares, milestones=milestones,
                                     required_signers=required_signers)
        payload = {"actor_id": actor_id, "offer_id": offer_id, "side": side, "terms": terms}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                offer = self._fetch_offer(connection, offer_id)
                negotiation = self._fetch_negotiation(connection, offer["negotiation_id"])
                if negotiation["status"] != "open":
                    raise ConflictError("协商已关闭，不能再反要约")
                self._check_side(connection, actor, negotiation["partner_id"], side)
                latest = connection.execute(
                    "SELECT offer_id FROM offers WHERE negotiation_id=? ORDER BY offer_no DESC LIMIT 1",
                    (offer["negotiation_id"],),
                ).fetchone()
                if latest["offer_id"] != offer_id:
                    raise ConflictError("只能针对最新要约发起反要约")
                if offer["status"] == "reserved":
                    raise ConflictError("要约处于保留状态，请先解除保留")
                if offer["status"] != "open":
                    raise ConflictError("要约已不在可协商状态")
                self._check_offer_terms(connection, negotiation, terms)
                connection.execute("UPDATE offers SET status='superseded' WHERE offer_id=?", (offer_id,))
                new_offer_id, offer_no = self._insert_offer(
                    connection, negotiation_id=offer["negotiation_id"], parent_offer_id=offer_id,
                    side=side, terms=terms, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="offer.countered",
                             resource_type="offer", resource_id=new_offer_id,
                             detail={"negotiation_id": offer["negotiation_id"], "offer_no": offer_no,
                                     "superseded_offer_id": offer_id, "side": side, "terms": terms},
                             occurred_at=self._now())
                return "offer", new_offer_id, {"offer_id": new_offer_id, "offer_no": offer_no}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="counter_offer", payload=payload, create=create)

    def reserve_offer(self, *, request_id: str, actor_id: str, offer_id: str) -> WriteReceipt:
        """团队保留要约：冻结协商窗口，期间不能接受或反要约。"""

        return self._offer_transition(request_id=request_id, actor_id=actor_id, offer_id=offer_id,
                                      action="reserve_offer", event="offer.reserved",
                                      from_status="open", to_status="reserved", team_only=True)

    def release_offer(self, *, request_id: str, actor_id: str, offer_id: str) -> WriteReceipt:
        """解除要约保留，恢复为可协商状态。"""

        return self._offer_transition(request_id=request_id, actor_id=actor_id, offer_id=offer_id,
                                      action="release_offer", event="offer.released",
                                      from_status="reserved", to_status="open", team_only=True)

    def _offer_transition(self, *, request_id: str, actor_id: str, offer_id: str, action: str,
                          event: str, from_status: str, to_status: str,
                          team_only: bool) -> WriteReceipt:
        payload = {"actor_id": actor_id, "offer_id": offer_id}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                if team_only:
                    self.base._require(actor, *TEAM_ROLES)
                offer = self._fetch_offer(connection, offer_id)
                if offer["status"] != from_status:
                    raise ConflictError(f"要约当前状态不允许该操作（{offer['status']}）")
                connection.execute("UPDATE offers SET status=? WHERE offer_id=?", (to_status, offer_id))
                append_event(connection, actor_id=actor_id, action=event,
                             resource_type="offer", resource_id=offer_id,
                             detail={"negotiation_id": offer["negotiation_id"],
                                     "from": from_status, "to": to_status}, occurred_at=self._now())
                return "offer", offer_id, {"offer_id": offer_id, "status": to_status}

            return self.base._idempotent(connection, request_id=request_id,
                                         action=action, payload=payload, create=create)

    def withdraw_offer(self, *, request_id: str, actor_id: str, offer_id: str) -> WriteReceipt:
        """要约方在接受前撤回自己的要约。"""

        payload = {"actor_id": actor_id, "offer_id": offer_id}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                offer = self._fetch_offer(connection, offer_id)
                negotiation = self._fetch_negotiation(connection, offer["negotiation_id"])
                self._check_side(connection, actor, negotiation["partner_id"], offer["side"])
                if offer["status"] not in LIVE_OFFER_STATUS:
                    raise ConflictError("要约已不在可撤回状态")
                connection.execute("UPDATE offers SET status='withdrawn' WHERE offer_id=?", (offer_id,))
                append_event(connection, actor_id=actor_id, action="offer.withdrawn",
                             resource_type="offer", resource_id=offer_id,
                             detail={"negotiation_id": offer["negotiation_id"], "side": offer["side"]},
                             occurred_at=self._now())
                return "offer", offer_id, {"offer_id": offer_id, "status": "withdrawn"}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="withdraw_offer", payload=payload, create=create)

    def accept_offer(self, *, request_id: str, actor_id: str, offer_id: str) -> WriteReceipt:
        """对方接受最新未决要约，生成待会签合同并占用独家权利。"""

        payload = {"actor_id": actor_id, "offer_id": offer_id}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                offer = self._fetch_offer(connection, offer_id)
                negotiation = self._fetch_negotiation(connection, offer["negotiation_id"])
                if negotiation["status"] != "open":
                    raise ConflictError("协商已关闭，不能再接受要约")
                accepting_side = "partner" if offer["side"] == "team" else "team"
                self._check_side(connection, actor, negotiation["partner_id"], accepting_side)
                if offer["status"] == "reserved":
                    raise ConflictError("保留中的要约需先解除保留才能接受")
                if offer["status"] != "open":
                    raise ConflictError("要约已不在可接受状态")
                terms = json.loads(offer["terms_json"])
                self._ensure_scope_available(connection, negotiation["work_id"], terms)
                contract_id = uuid.uuid4().hex
                snapshot = {"quote": terms["quote"], "exclusive": terms["exclusive"],
                            "territories": terms["territories"], "channels": terms["channels"],
                            "min_commitment": terms["min_commitment"]}
                connection.execute(
                    "INSERT INTO contracts(contract_id,negotiation_id,offer_id,work_id,partner_id,"
                    "design_version_id,terms_json,required_signers_json,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?, 'pending', ?)",
                    (contract_id, offer["negotiation_id"], offer_id, negotiation["work_id"],
                     negotiation["partner_id"], terms["design_version_id"], canonical_json(snapshot),
                     canonical_json(terms["required_signers"]), self._now()),
                )
                for milestone in terms["milestones"]:
                    connection.execute(
                        "INSERT INTO milestones(contract_id,milestone_key,position,title,gate,"
                        "depends_on_json,status) VALUES(?,?,?,?,?,?,'pending')",
                        (contract_id, milestone["key"], milestone["position"], milestone["title"],
                         1 if milestone["gate"] else 0, canonical_json(milestone["depends_on"])),
                    )
                connection.execute(
                    "INSERT INTO cost_versions(contract_id,version_no,items_json,created_at) VALUES(?,1,?,?)",
                    (contract_id, canonical_json(terms["cost_items"]), self._now()),
                )
                connection.execute(
                    "INSERT INTO share_versions(contract_id,version_no,shares_json,created_at) VALUES(?,1,?,?)",
                    (contract_id, canonical_json(terms["shares"]), self._now()),
                )
                connection.execute("UPDATE offers SET status='accepted' WHERE offer_id=?", (offer_id,))
                connection.execute("UPDATE negotiations SET status='accepted' WHERE negotiation_id=?",
                                   (offer["negotiation_id"],))
                append_event(connection, actor_id=actor_id, action="offer.accepted",
                             resource_type="offer", resource_id=offer_id,
                             detail={"negotiation_id": offer["negotiation_id"],
                                     "contract_id": contract_id}, occurred_at=self._now())
                append_event(connection, actor_id=actor_id, action="contract.created",
                             resource_type="contract", resource_id=contract_id,
                             detail={"negotiation_id": offer["negotiation_id"], "offer_id": offer_id,
                                     "work_id": negotiation["work_id"],
                                     "partner_id": negotiation["partner_id"],
                                     "terms": snapshot,
                                     "required_signers": terms["required_signers"]},
                             occurred_at=self._now())
                return "contract", contract_id, {"contract_id": contract_id}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="accept_offer", payload=payload, create=create)

    def _ensure_scope_available(self, connection, work_id: str, terms: dict[str, Any]) -> None:
        new_cells = _scope_cells(terms["territories"], terms["channels"])
        rows = connection.execute(
            "SELECT contract_id, terms_json FROM contracts WHERE work_id=? AND status IN ('pending','active')",
            (work_id,),
        ).fetchall()
        for row in rows:
            existing = json.loads(row["terms_json"])
            overlap = new_cells & _scope_cells(existing["territories"], existing["channels"])
            if overlap and (terms["exclusive"] or existing["exclusive"]):
                cells = sorted(f"{territory}/{channel}" for territory, channel in overlap)
                raise ConflictError(f"独家范围与有效合同 {row['contract_id']} 冲突: {', '.join(cells)}")

    # ------------------------------------------------------------------
    # 合同：会签、里程碑、激活、终止
    # ------------------------------------------------------------------

    def sign_contract(self, *, request_id: str, actor_id: str, contract_id: str) -> WriteReceipt:
        """会签人签署合同，重复签署会被拒绝。"""

        payload = {"actor_id": actor_id, "contract_id": contract_id}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                contract = self._fetch_contract(connection, contract_id)
                if contract["status"] != "pending":
                    raise ConflictError("合同不在待会签状态")
                signers = json.loads(contract["required_signers_json"])
                if actor_id not in signers:
                    raise PermissionDenied("操作者不是该合同的会签人")
                existing = connection.execute(
                    "SELECT 1 FROM signatures WHERE contract_id=? AND actor_id=?",
                    (contract_id, actor_id),
                ).fetchone()
                if existing is not None:
                    raise ConflictError("该会签人已签署，重复签署无效")
                connection.execute(
                    "INSERT INTO signatures(contract_id,actor_id,signed_at) VALUES(?,?,?)",
                    (contract_id, actor_id, self._now()),
                )
                signed = {row["actor_id"] for row in connection.execute(
                    "SELECT actor_id FROM signatures WHERE contract_id=?", (contract_id,))}
                remaining = [signer for signer in signers if signer not in signed]
                append_event(connection, actor_id=actor_id, action="contract.signed",
                             resource_type="contract", resource_id=contract_id,
                             detail={"signer": actor_id, "remaining_signers": remaining},
                             occurred_at=self._now())
                return "contract", contract_id, {"contract_id": contract_id,
                                                 "remaining_signers": remaining}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="sign_contract", payload=payload, create=create)

    def complete_milestone(self, *, request_id: str, actor_id: str, contract_id: str,
                           milestone_key: str) -> WriteReceipt:
        """确认里程碑完成（如样品确认），前置依赖必须先完成。"""

        payload = {"actor_id": actor_id, "contract_id": contract_id, "milestone_key": milestone_key}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                contract = self._fetch_contract(connection, contract_id)
                if contract["status"] not in ("pending", "active"):
                    raise ConflictError("已终止合同不能再确认里程碑")
                milestone = connection.execute(
                    "SELECT * FROM milestones WHERE contract_id=? AND milestone_key=?",
                    (contract_id, milestone_key),
                ).fetchone()
                if milestone is None:
                    raise NotFoundError("里程碑不存在")
                if milestone["status"] == "done":
                    raise ConflictError("里程碑已完成，重复确认无效")
                blocking = []
                for dep in json.loads(milestone["depends_on_json"]):
                    row = connection.execute(
                        "SELECT status FROM milestones WHERE contract_id=? AND milestone_key=?",
                        (contract_id, dep),
                    ).fetchone()
                    if row is None or row["status"] != "done":
                        blocking.append(dep)
                if blocking:
                    raise ConflictError(f"前置里程碑未完成: {', '.join(blocking)}")
                connection.execute(
                    "UPDATE milestones SET status='done', completed_at=? "
                    "WHERE contract_id=? AND milestone_key=?",
                    (self._now(), contract_id, milestone_key),
                )
                append_event(connection, actor_id=actor_id, action="milestone.completed",
                             resource_type="contract", resource_id=contract_id,
                             detail={"milestone_key": milestone_key,
                                     "gate": bool(milestone["gate"])}, occurred_at=self._now())
                return "milestone", f"{contract_id}/{milestone_key}", {"contract_id": contract_id,
                                                                       "milestone_key": milestone_key}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="complete_milestone", payload=payload, create=create)

    def activate_contract(self, *, request_id: str, actor_id: str, contract_id: str) -> WriteReceipt:
        """会签齐全且前置里程碑全部完成后，合同才能进入履约。"""

        payload = {"actor_id": actor_id, "contract_id": contract_id}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                contract = self._fetch_contract(connection, contract_id)
                if contract["status"] == "active":
                    raise ConflictError("合同已在履约中")
                if contract["status"] != "pending":
                    raise ConflictError("已终止合同不能进入履约")
                signers = json.loads(contract["required_signers_json"])
                signed = {row["actor_id"] for row in connection.execute(
                    "SELECT actor_id FROM signatures WHERE contract_id=?", (contract_id,))}
                missing = [signer for signer in signers if signer not in signed]
                if missing:
                    raise ConflictError(f"会签未完成: {', '.join(missing)}")
                gates = connection.execute(
                    "SELECT milestone_key FROM milestones WHERE contract_id=? AND gate=1 AND status!='done' "
                    "ORDER BY position", (contract_id,),
                ).fetchall()
                if gates:
                    pending = [row["milestone_key"] for row in gates]
                    raise ConflictError(f"前置里程碑未完成: {', '.join(pending)}")
                connection.execute(
                    "UPDATE contracts SET status='active', activated_at=? WHERE contract_id=?",
                    (self._now(), contract_id),
                )
                append_event(connection, actor_id=actor_id, action="contract.activated",
                             resource_type="contract", resource_id=contract_id,
                             detail={"signers": signers}, occurred_at=self._now())
                return "contract", contract_id, {"contract_id": contract_id, "status": "active"}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="activate_contract", payload=payload, create=create)

    def terminate_contract(self, *, request_id: str, actor_id: str, contract_id: str,
                           reason: str) -> WriteReceipt:
        """终止合同并释放其占用的独家权利，终止本身也是追加的事实。"""

        reason = str(reason).strip()
        if not reason:
            raise ValidationError("终止原因不能为空")
        payload = {"actor_id": actor_id, "contract_id": contract_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, "admin")
                contract = self._fetch_contract(connection, contract_id)
                if contract["status"] == "terminated":
                    raise ConflictError("合同已终止")
                connection.execute(
                    "UPDATE contracts SET status='terminated', terminated_at=?, termination_reason=? "
                    "WHERE contract_id=?",
                    (self._now(), reason, contract_id),
                )
                append_event(connection, actor_id=actor_id, action="contract.terminated",
                             resource_type="contract", resource_id=contract_id,
                             detail={"reason": reason, "previous_status": contract["status"]},
                             occurred_at=self._now())
                return "contract", contract_id, {"contract_id": contract_id, "status": "terminated"}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="terminate_contract", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 履约事实：部分交付、设计变更、成本与分成修订、违约整改
    # ------------------------------------------------------------------

    def _append_fact(self, connection, *, contract_id: str, kind: str,
                     payload: dict[str, Any], actor_id: str) -> str:
        fact_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO contract_facts(fact_id,contract_id,kind,payload_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (fact_id, contract_id, kind, canonical_json(payload), actor_id, self._now()),
        )
        return fact_id

    def record_delivery(self, *, request_id: str, actor_id: str, contract_id: str,
                        milestone_key: str, quantity: int, note: str = "") -> WriteReceipt:
        """记录一次部分交付，只追加事实，不改写合同。"""

        _positive_int(quantity, "quantity")
        payload = {"actor_id": actor_id, "contract_id": contract_id,
                   "milestone_key": milestone_key, "quantity": quantity, "note": note}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                contract = self._fetch_contract(connection, contract_id)
                if contract["status"] != "active":
                    raise ConflictError("合同未处于履约状态，不能登记交付")
                if connection.execute(
                        "SELECT 1 FROM milestones WHERE contract_id=? AND milestone_key=?",
                        (contract_id, milestone_key)).fetchone() is None:
                    raise NotFoundError("里程碑不存在")
                fact_payload = {"milestone_key": milestone_key, "quantity": quantity, "note": note}
                fact_id = self._append_fact(connection, contract_id=contract_id, kind="delivery",
                                            payload=fact_payload, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="contract.delivery_recorded",
                             resource_type="contract", resource_id=contract_id,
                             detail={"fact_id": fact_id, **fact_payload}, occurred_at=self._now())
                return "contract_fact", fact_id, {"fact_id": fact_id}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="record_delivery", payload=payload, create=create)

    def record_design_change(self, *, request_id: str, actor_id: str, contract_id: str,
                             design_version_id: str, reason: str) -> WriteReceipt:
        """登记设计变更：指向新设计版本，原合同签署版本保持不变。"""

        reason = str(reason).strip()
        if not reason:
            raise ValidationError("变更原因不能为空")
        payload = {"actor_id": actor_id, "contract_id": contract_id,
                   "design_version_id": design_version_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                contract = self._fetch_contract(connection, contract_id)
                if contract["status"] == "terminated":
                    raise ConflictError("已终止合同不能变更设计")
                design = connection.execute("SELECT * FROM design_versions WHERE design_version_id=?",
                                            (design_version_id,)).fetchone()
                if design is None or design["work_id"] != contract["work_id"]:
                    raise ValidationError("设计版本不存在或不属于该作品")
                current = self._current_design_version(connection, contract)
                if current == design_version_id:
                    raise ValidationError("设计版本未发生变化")
                fact_payload = {"from": current, "to": design_version_id, "reason": reason}
                fact_id = self._append_fact(connection, contract_id=contract_id, kind="design_change",
                                            payload=fact_payload, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="contract.design_changed",
                             resource_type="contract", resource_id=contract_id,
                             detail={"fact_id": fact_id, **fact_payload}, occurred_at=self._now())
                return "contract_fact", fact_id, {"fact_id": fact_id}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="record_design_change", payload=payload, create=create)

    def revise_cost_basis(self, *, request_id: str, actor_id: str, contract_id: str,
                          cost_items: Any) -> WriteReceipt:
        """追加新一版成本口径，历史版本保留供结算解释。"""

        items = _validate_cost_items(cost_items)
        payload = {"actor_id": actor_id, "contract_id": contract_id, "cost_items": items}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                contract = self._fetch_contract(connection, contract_id)
                if contract["status"] == "terminated":
                    raise ConflictError("已终止合同不能修订成本口径")
                row = connection.execute(
                    "SELECT COALESCE(MAX(version_no), 0) AS max_no FROM cost_versions WHERE contract_id=?",
                    (contract_id,),
                ).fetchone()
                version_no = row["max_no"] + 1
                connection.execute(
                    "INSERT INTO cost_versions(contract_id,version_no,items_json,created_at) VALUES(?,?,?,?)",
                    (contract_id, version_no, canonical_json(items), self._now()),
                )
                fact_id = self._append_fact(connection, contract_id=contract_id, kind="cost_revision",
                                            payload={"version_no": version_no, "items": items},
                                            actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="contract.cost_revised",
                             resource_type="contract", resource_id=contract_id,
                             detail={"fact_id": fact_id, "version_no": version_no},
                             occurred_at=self._now())
                return "contract_fact", fact_id, {"fact_id": fact_id, "version_no": version_no}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="revise_cost_basis", payload=payload, create=create)

    def revise_shares(self, *, request_id: str, actor_id: str, contract_id: str,
                      shares: Any) -> WriteReceipt:
        """追加新一版成员份额，历史版本保留供结算解释。"""

        shares_checked = _validate_shares(shares)
        payload = {"actor_id": actor_id, "contract_id": contract_id, "shares": shares_checked}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                contract = self._fetch_contract(connection, contract_id)
                if contract["status"] == "terminated":
                    raise ConflictError("已终止合同不能修订成员份额")
                row = connection.execute(
                    "SELECT COALESCE(MAX(version_no), 0) AS max_no FROM share_versions WHERE contract_id=?",
                    (contract_id,),
                ).fetchone()
                version_no = row["max_no"] + 1
                connection.execute(
                    "INSERT INTO share_versions(contract_id,version_no,shares_json,created_at) VALUES(?,?,?,?)",
                    (contract_id, version_no, canonical_json(shares_checked), self._now()),
                )
                fact_id = self._append_fact(connection, contract_id=contract_id, kind="share_revision",
                                            payload={"version_no": version_no, "shares": shares_checked},
                                            actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="contract.shares_revised",
                             resource_type="contract", resource_id=contract_id,
                             detail={"fact_id": fact_id, "version_no": version_no},
                             occurred_at=self._now())
                return "contract_fact", fact_id, {"fact_id": fact_id, "version_no": version_no}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="revise_shares", payload=payload, create=create)

    def record_breach(self, *, request_id: str, actor_id: str, contract_id: str,
                      description: str) -> WriteReceipt:
        """登记违约事实（如未达到最低承诺）。"""

        description = str(description).strip()
        if not description:
            raise ValidationError("违约描述不能为空")
        payload = {"actor_id": actor_id, "contract_id": contract_id, "description": description}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                contract = self._fetch_contract(connection, contract_id)
                if contract["status"] == "terminated":
                    raise ConflictError("已终止合同不能再登记违约")
                fact_id = self._append_fact(connection, contract_id=contract_id, kind="breach",
                                            payload={"description": description}, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="contract.breach_recorded",
                             resource_type="contract", resource_id=contract_id,
                             detail={"fact_id": fact_id, "description": description},
                             occurred_at=self._now())
                return "contract_fact", fact_id, {"fact_id": fact_id}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="record_breach", payload=payload, create=create)

    def record_rectification(self, *, request_id: str, actor_id: str, contract_id: str,
                             breach_fact_id: str, description: str) -> WriteReceipt:
        """针对已登记的违约追加整改事实。"""

        description = str(description).strip()
        if not description:
            raise ValidationError("整改描述不能为空")
        payload = {"actor_id": actor_id, "contract_id": contract_id,
                   "breach_fact_id": breach_fact_id, "description": description}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                self._fetch_contract(connection, contract_id)
                breach = connection.execute(
                    "SELECT kind FROM contract_facts WHERE fact_id=? AND contract_id=?",
                    (breach_fact_id, contract_id),
                ).fetchone()
                if breach is None or breach["kind"] != "breach":
                    raise NotFoundError("对应的违约事实不存在")
                fact_payload = {"breach_fact_id": breach_fact_id, "description": description}
                fact_id = self._append_fact(connection, contract_id=contract_id, kind="rectification",
                                            payload=fact_payload, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="contract.rectification_recorded",
                             resource_type="contract", resource_id=contract_id,
                             detail={"fact_id": fact_id, **fact_payload}, occurred_at=self._now())
                return "contract_fact", fact_id, {"fact_id": fact_id}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="record_rectification", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 结算与争议
    # ------------------------------------------------------------------

    def create_settlement(self, *, request_id: str, actor_id: str, contract_id: str,
                          period: str, gross_amount_cents: int) -> WriteReceipt:
        """按当前成本口径与成员份额版本生成结算分录，同一周期只能结算一次。"""

        period = str(period).strip()
        if not period or len(period) > 40:
            raise ValidationError("period 不能为空且不能超过 40 个字符")
        _positive_int(gross_amount_cents, "gross_amount_cents", allow_zero=True)
        payload = {"actor_id": actor_id, "contract_id": contract_id,
                   "period": period, "gross_amount_cents": gross_amount_cents}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                self.base._require(actor, *TEAM_ROLES)
                contract = self._fetch_contract(connection, contract_id)
                if contract["status"] == "pending":
                    raise ConflictError("合同尚未进入履约，不能结算")
                existing = connection.execute(
                    "SELECT settlement_id FROM settlements WHERE contract_id=? AND period=?",
                    (contract_id, period),
                ).fetchone()
                if existing is not None:
                    raise ConflictError("该结算周期已生成结算，重复回调不会重复入账")
                cost = connection.execute(
                    "SELECT version_no, items_json FROM cost_versions WHERE contract_id=? "
                    "ORDER BY version_no DESC LIMIT 1", (contract_id,),
                ).fetchone()
                share = connection.execute(
                    "SELECT version_no, shares_json FROM share_versions WHERE contract_id=? "
                    "ORDER BY version_no DESC LIMIT 1", (contract_id,),
                ).fetchone()
                cost_items = json.loads(cost["items_json"])
                shares = json.loads(share["shares_json"])
                computed = _compute_settlement(gross_amount_cents, cost_items, shares)
                settlement_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO settlements(settlement_id,contract_id,period,gross_amount_cents,"
                    "cost_version_no,share_version_no,lines_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (settlement_id, contract_id, period, gross_amount_cents,
                     cost["version_no"], share["version_no"], canonical_json(computed), self._now()),
                )
                append_event(connection, actor_id=actor_id, action="settlement.created",
                             resource_type="settlement", resource_id=settlement_id,
                             detail={"contract_id": contract_id, "period": period,
                                     "cost_version_no": cost["version_no"],
                                     "share_version_no": share["version_no"],
                                     "net_cents": computed["net_cents"]}, occurred_at=self._now())
                return "settlement", settlement_id, {"settlement_id": settlement_id}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="create_settlement", payload=payload, create=create)

    def dispute_settlement(self, *, request_id: str, actor_id: str, settlement_id: str,
                           reason: str) -> WriteReceipt:
        """对结算提出争议，争议是追加的事实，结算分录本身不被修改。"""

        reason = str(reason).strip()
        if not reason:
            raise ValidationError("争议原因不能为空")
        payload = {"actor_id": actor_id, "settlement_id": settlement_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            def create() -> tuple[str, str, dict[str, Any]]:
                actor = self.base._actor(connection, actor_id)
                settlement = connection.execute("SELECT * FROM settlements WHERE settlement_id=?",
                                                (settlement_id,)).fetchone()
                if settlement is None:
                    raise NotFoundError("结算不存在")
                contract = self._fetch_contract(connection, settlement["contract_id"])
                if actor.role in TEAM_ROLES:
                    pass
                elif actor.role == "partner":
                    self._require_partner_member(connection, actor.actor_id, contract["partner_id"])
                else:
                    raise PermissionDenied("当前角色不能发起结算争议")
                fact_payload = {"settlement_id": settlement_id, "reason": reason}
                fact_id = self._append_fact(connection, contract_id=contract["contract_id"],
                                            kind="dispute", payload=fact_payload, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="settlement.disputed",
                             resource_type="settlement", resource_id=settlement_id,
                             detail={"fact_id": fact_id, "contract_id": contract["contract_id"],
                                     "reason": reason}, occurred_at=self._now())
                return "contract_fact", fact_id, {"fact_id": fact_id}

            return self.base._idempotent(connection, request_id=request_id,
                                         action="dispute_settlement", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询：最小必要视图、权利占用、结算解释
    # ------------------------------------------------------------------

    def _fetch_negotiation(self, connection, negotiation_id: str):
        row = connection.execute("SELECT * FROM negotiations WHERE negotiation_id=?",
                                 (negotiation_id,)).fetchone()
        if row is None:
            raise NotFoundError("协商不存在")
        return row

    def _fetch_offer(self, connection, offer_id: str):
        row = connection.execute("SELECT * FROM offers WHERE offer_id=?", (offer_id,)).fetchone()
        if row is None:
            raise NotFoundError("要约不存在")
        return row

    def _fetch_contract(self, connection, contract_id: str):
        row = connection.execute("SELECT * FROM contracts WHERE contract_id=?", (contract_id,)).fetchone()
        if row is None:
            raise NotFoundError("合同不存在")
        return row

    def _current_design_version(self, connection, contract) -> str:
        row = connection.execute(
            "SELECT payload_json FROM contract_facts WHERE contract_id=? AND kind='design_change' "
            "ORDER BY seq DESC LIMIT 1", (contract["contract_id"],),
        ).fetchone()
        if row is not None:
            return json.loads(row["payload_json"])["to"]
        return contract["design_version_id"]

    def _access_level(self, connection, actor: Actor, partner_id: str) -> str:
        if actor.role in READ_ROLES:
            return "full"
        if actor.role == "partner":
            self._require_partner_member(connection, actor.actor_id, partner_id)
            return "partner"
        raise PermissionDenied("当前角色不能查看该资源")

    def _offer_dict(self, row, level: str) -> dict[str, Any]:
        terms = json.loads(row["terms_json"])
        data = {"offer_id": row["offer_id"], "negotiation_id": row["negotiation_id"],
                "offer_no": row["offer_no"], "parent_offer_id": row["parent_offer_id"],
                "side": row["side"], "status": row["status"],
                "design_version_id": terms["design_version_id"], "quote": terms["quote"],
                "exclusive": terms["exclusive"], "territories": terms["territories"],
                "channels": terms["channels"], "min_commitment": terms["min_commitment"],
                "milestones": terms["milestones"], "created_at": row["created_at"]}
        if level == "full":
            data["cost_items"] = terms["cost_items"]
            data["shares"] = terms["shares"]
            data["required_signers"] = terms["required_signers"]
        else:
            data["partner_share"] = next(
                (share for share in terms["shares"] if share["member"] == PARTNER_MEMBER), None)
        return data

    def get_negotiation(self, *, actor_id: str, negotiation_id: str) -> dict[str, Any]:
        """查看协商及其要约时序；合作方只能看自己的协商且隐藏团队内部条款。"""

        connection = self.database.connection
        actor = self.base._actor(connection, actor_id)
        negotiation = self._fetch_negotiation(connection, negotiation_id)
        level = self._access_level(connection, actor, negotiation["partner_id"])
        offers = connection.execute(
            "SELECT * FROM offers WHERE negotiation_id=? ORDER BY offer_no", (negotiation_id,),
        ).fetchall()
        contract = connection.execute(
            "SELECT contract_id FROM contracts WHERE negotiation_id=?", (negotiation_id,),
        ).fetchone()
        return {"negotiation_id": negotiation_id, "work_id": negotiation["work_id"],
                "partner_id": negotiation["partner_id"], "status": negotiation["status"],
                "created_at": negotiation["created_at"],
                "contract_id": contract["contract_id"] if contract else None,
                "offers": [self._offer_dict(row, level) for row in offers]}

    def list_pending_offers(self, *, actor_id: str) -> list[dict[str, Any]]:
        """按时间序列出全部待确认要约，服务恢复后顺序不变。"""

        connection = self.database.connection
        actor = self.base._actor(connection, actor_id)
        self.base._require(actor, *READ_ROLES)
        rows = connection.execute(
            "SELECT * FROM offers WHERE status IN ('open','reserved') "
            "ORDER BY created_at, negotiation_id, offer_no",
        ).fetchall()
        return [self._offer_dict(row, "full") for row in rows]

    def get_contract_view(self, *, actor_id: str, contract_id: str) -> dict[str, Any]:
        """按角色返回合同的最小必要视图。"""

        connection = self.database.connection
        actor = self.base._actor(connection, actor_id)
        contract = self._fetch_contract(connection, contract_id)
        level = self._access_level(connection, actor, contract["partner_id"])
        terms = json.loads(contract["terms_json"])
        milestones = connection.execute(
            "SELECT * FROM milestones WHERE contract_id=? ORDER BY position", (contract_id,),
        ).fetchall()
        data = {"contract_id": contract_id, "status": contract["status"],
                "work_id": contract["work_id"], "partner_id": contract["partner_id"],
                "design_version_id": self._current_design_version(connection, contract),
                "quote": terms["quote"], "exclusive": terms["exclusive"],
                "territories": terms["territories"], "channels": terms["channels"],
                "min_commitment": terms["min_commitment"],
                "milestones": [{"key": row["milestone_key"], "title": row["title"],
                                "gate": bool(row["gate"]),
                                "depends_on": json.loads(row["depends_on_json"]),
                                "status": row["status"], "completed_at": row["completed_at"]}
                               for row in milestones],
                "created_at": contract["created_at"], "activated_at": contract["activated_at"],
                "terminated_at": contract["terminated_at"]}
        signers = json.loads(contract["required_signers_json"])
        signed = [row["actor_id"] for row in connection.execute(
            "SELECT actor_id FROM signatures WHERE contract_id=? ORDER BY signed_at, actor_id",
            (contract_id,)).fetchall()]
        if level == "full":
            data.update({
                "negotiation_id": contract["negotiation_id"], "offer_id": contract["offer_id"],
                "signed_design_version_id": contract["design_version_id"],
                "required_signers": signers, "signatures": signed,
                "cost_version_no": self._latest_version(connection, "cost_versions", contract_id),
                "share_version_no": self._latest_version(connection, "share_versions", contract_id),
                "termination_reason": contract["termination_reason"],
            })
        else:
            share_version = self._latest_version(connection, "share_versions", contract_id)
            row = connection.execute(
                "SELECT shares_json FROM share_versions WHERE contract_id=? AND version_no=?",
                (contract_id, share_version)).fetchone()
            shares = json.loads(row["shares_json"])
            data.update({
                "partner_share": next((share for share in shares
                                       if share["member"] == PARTNER_MEMBER), None),
                "signature_complete": len(signed) == len(signers),
                "awaiting_my_signature": actor.actor_id in signers and actor.actor_id not in signed,
            })
        return data

    def _latest_version(self, connection, table: str, contract_id: str) -> int:
        row = connection.execute(
            f"SELECT COALESCE(MAX(version_no), 0) AS max_no FROM {table} WHERE contract_id=?",
            (contract_id,),
        ).fetchone()
        return row["max_no"]

    def contract_timeline(self, *, actor_id: str, contract_id: str) -> list[dict[str, Any]]:
        """按追加顺序返回合同事实，原始条款与后续事实分离。"""

        connection = self.database.connection
        actor = self.base._actor(connection, actor_id)
        contract = self._fetch_contract(connection, contract_id)
        self._access_level(connection, actor, contract["partner_id"])
        rows = connection.execute(
            "SELECT * FROM contract_facts WHERE contract_id=? ORDER BY seq", (contract_id,),
        ).fetchall()
        return [{"seq": row["seq"], "fact_id": row["fact_id"], "kind": row["kind"],
                 "payload": json.loads(row["payload_json"]), "created_by": row["created_by"],
                 "created_at": row["created_at"]} for row in rows]

    def rights_at(self, *, actor_id: str, work_id: str, at: str) -> dict[str, Any]:
        """解释某时点该作品哪些地域×渠道权利仍被哪些合同占用。"""

        connection = self.database.connection
        actor = self.base._actor(connection, actor_id)
        self.base._require(actor, *READ_ROLES)
        moment = _parse_instant(at, "at")
        if connection.execute("SELECT 1 FROM works WHERE work_id=?", (work_id,)).fetchone() is None:
            raise NotFoundError("作品不存在")
        occupied = []
        rows = connection.execute("SELECT * FROM contracts WHERE work_id=?", (work_id,)).fetchall()
        for row in rows:
            if row["created_at"] > moment:
                continue
            if row["terminated_at"] is not None and row["terminated_at"] <= moment:
                continue
            terms = json.loads(row["terms_json"])
            for territory, channel in sorted(_scope_cells(terms["territories"], terms["channels"])):
                occupied.append({"contract_id": row["contract_id"], "territory": territory,
                                 "channel": channel, "exclusive": terms["exclusive"]})
        return {"work_id": work_id, "at": moment, "occupied": occupied}

    def get_settlement_view(self, *, actor_id: str, settlement_id: str) -> dict[str, Any]:
        """查看结算；合作方只能看到自己一方的分录。"""

        connection = self.database.connection
        actor = self.base._actor(connection, actor_id)
        settlement = connection.execute("SELECT * FROM settlements WHERE settlement_id=?",
                                        (settlement_id,)).fetchone()
        if settlement is None:
            raise NotFoundError("结算不存在")
        contract = self._fetch_contract(connection, settlement["contract_id"])
        level = self._access_level(connection, actor, contract["partner_id"])
        computed = json.loads(settlement["lines_json"])
        data = {"settlement_id": settlement_id, "contract_id": settlement["contract_id"],
                "period": settlement["period"], "created_at": settlement["created_at"]}
        if level == "full":
            data.update(computed)
            data["cost_version_no"] = settlement["cost_version_no"]
            data["share_version_no"] = settlement["share_version_no"]
        else:
            data["own_line"] = next((line for line in computed["lines"]
                                     if line["member"] == PARTNER_MEMBER), None)
        return data

    def explain_settlement(self, *, actor_id: str, settlement_id: str) -> dict[str, Any]:
        """解释某笔分成采用了哪一版成本口径和哪一版成员份额。"""

        connection = self.database.connection
        actor = self.base._actor(connection, actor_id)
        settlement = connection.execute("SELECT * FROM settlements WHERE settlement_id=?",
                                        (settlement_id,)).fetchone()
        if settlement is None:
            raise NotFoundError("结算不存在")
        contract = self._fetch_contract(connection, settlement["contract_id"])
        level = self._access_level(connection, actor, contract["partner_id"])
        view = self.get_settlement_view(actor_id=actor_id, settlement_id=settlement_id)
        view["cost_version_no"] = settlement["cost_version_no"]
        view["share_version_no"] = settlement["share_version_no"]
        if level == "full":
            cost = connection.execute(
                "SELECT items_json FROM cost_versions WHERE contract_id=? AND version_no=?",
                (settlement["contract_id"], settlement["cost_version_no"])).fetchone()
            share = connection.execute(
                "SELECT shares_json FROM share_versions WHERE contract_id=? AND version_no=?",
                (settlement["contract_id"], settlement["share_version_no"])).fetchone()
            view["cost_items"] = json.loads(cost["items_json"])
            view["shares"] = json.loads(share["shares_json"])
        return view
