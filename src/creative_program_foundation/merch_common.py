"""商品化各模块共享的辅助：事务、幂等、权限与快照读取。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .storage import Database

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def new_id() -> str:
    return uuid.uuid4().hex


def thread_key(work_id: str, partner_id: str) -> str:
    return digest({"work_id": work_id, "partner_id": partner_id})[:32]


class MerchBase:
    """共享数据库、时钟、权限与校验。"""

    database: Database
    clock: Clock

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    def now(self) -> datetime:
        return self.clock.now()

    def now_text(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    # -- 校验 ---------------------------------------------------------------

    def id_value(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def code_value(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not CODE.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def text(self, value: Any, field: str, limit: int = 200) -> str:
        value = str(value if value is not None else "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def opt_text(self, value: Any, field: str, limit: int = 500) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        if len(value) > limit:
            raise ValidationError(f"{field} 不能超过 {limit} 个字符")
        return value

    def day(self, value: Any, field: str) -> str:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 日期或时间") from exc
        if len(text) == 10:
            return text
        return parsed.date().isoformat()

    def stamp(self, value: Any, field: str) -> str:
        text = str(value).strip()
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 时间") from exc
        if text.endswith("Z") or "+" in text:
            return text.replace("+00:00", "Z")
        if "T" in text:
            return text + "Z"
        return text

    # -- 操作者与权限 ---------------------------------------------------------

    def actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def require_roles(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def team_member(self, connection, actor: Actor, team_id: str):
        return connection.execute(
            "SELECT * FROM team_members WHERE team_id=? AND actor_id=? AND active=1",
            (team_id, actor.actor_id),
        ).fetchone()

    def partner_rep(self, connection, actor: Actor, partner_id: str):
        return connection.execute(
            "SELECT pr.* FROM partner_representatives pr JOIN partners p ON p.partner_id=pr.partner_id "
            "WHERE pr.partner_id=? AND pr.actor_id=? AND pr.active=1 AND p.organization_id=?",
            (partner_id, actor.actor_id, actor.organization_id),
        ).fetchone()

    def team_signer(self, connection, actor: Actor, team_id: str, work_id: str):
        now_value = self.now_text()
        return connection.execute(
            "SELECT * FROM signing_grants WHERE team_id=? AND actor_id=? AND status='active' "
            "AND (scope_type='team' OR (scope_type='work' AND scope_id=?)) "
            "AND valid_from<=? AND (valid_until IS NULL OR valid_until>?)",
            (team_id, actor.actor_id, work_id, now_value, now_value),
        ).fetchone()

    # -- 行读取 ---------------------------------------------------------------

    def require_row(self, connection, table: str, key_field: str, key_value: str,
                    message: str):
        row = connection.execute(
            f"SELECT * FROM {table} WHERE {key_field}=?", (key_value,)
        ).fetchone()
        if row is None:
            raise NotFoundError(message)
        return row

    def qualification_ok(self, connection, partner_id: str) -> bool:
        row = connection.execute(
            "SELECT decision, valid_until FROM partner_qualifications WHERE partner_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (partner_id,),
        ).fetchone()
        if row is None or row["decision"] != "approved":
            return False
        if row["valid_until"] and row["valid_until"] < self.now_text()[:10]:
            return False
        return True

    # -- 幂等与审计 ------------------------------------------------------------

    def idempotent(self, connection, *, request_id: str, action: str,
                   payload: dict[str, Any],
                   create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        replayed = self.replay_if_seen(connection, request_id=request_id,
                                       action=action, payload=payload)
        if replayed is not None:
            return replayed
        request_id = self.id_value(request_id, "request_id")
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload and digest(payload), resource_type, resource_id,
             canonical_json(response), self.now_text()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def replay_if_seen(self, connection, *, request_id: str, action: str,
                       payload: dict[str, Any]) -> WriteReceipt | None:
        """若 request_id 已处理则返回原回执；重复回调不再做任何状态校验。"""

        request_id = self.id_value(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    def audit(self, connection, *, actor_id: str, action: str, resource_type: str,
              resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     detail=detail, occurred_at=self.now_text())
