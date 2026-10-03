"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .merch_service import MerchService
from .service import DomainService
from .storage import Database


def merch_for(service: DomainService) -> MerchService:
    return MerchService(service.database, service.clock)


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        merch_status, merch_payload = merch_route(
            merch_for(service), method, parsed.path, body, headers, parse_qs(parsed.query))
        if merch_status is not None:
            return merch_status, merch_payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


MERCH_POST_ROUTES = {
    "/partners": ("registry", "register_partner"),
    "/partner-qualifications": ("registry", "review_qualification"),
    "/partner-representatives": ("registry", "bind_representative"),
    "/teams": ("registry", "register_team"),
    "/team-members": ("registry", "add_team_member"),
    "/signing-grants": ("registry", "grant_signing"),
    "/signing-grants/revoke": ("registry", "revoke_signing"),
    "/works": ("registry", "register_work"),
    "/design-versions": ("registry", "add_design_version"),
    "/design-versions/countersign": ("registry", "countersign_version"),
    "/design-versions/prerequisite": ("registry", "complete_version_prerequisite"),
    "/cost-sheets": ("registry", "register_cost_sheet"),
    "/share-sheets": ("registry", "register_share_sheet"),
    "/intentions": ("negotiation", "create_intention"),
    "/intentions/terminate": ("negotiation", "terminate_intention"),
    "/offers": ("negotiation", "make_offer"),
    "/offers/withdraw": ("negotiation", "withdraw_offer"),
    "/offers/expire": ("negotiation", "expire_stale_offers"),
    "/offers/reserve": ("negotiation", "reserve_offer"),
    "/offers/reservation/release": ("negotiation", "release_reservation"),
    "/offers/sign": ("negotiation", "sign_offer"),
    "/offers/accept": ("negotiation", "accept_offer"),
    "/samples": ("performance", "confirm_sample"),
    "/milestones/approve": ("performance", "approve_milestone"),
    "/deliveries": ("performance", "record_delivery"),
    "/change-orders": ("performance", "propose_change_order"),
    "/change-orders/sign": ("performance", "sign_change_order"),
    "/change-orders/approve": ("performance", "approve_change_order"),
    "/change-orders/reject": ("performance", "reject_change_order"),
    "/breaches": ("performance", "report_breach"),
    "/breaches/remediation": ("performance", "submit_remediation"),
    "/breaches/resolve": ("performance", "resolve_breach"),
    "/contracts/terminate": ("performance", "terminate_contract"),
    "/settlements": ("performance", "generate_settlement"),
    "/settlements/dispute": ("performance", "dispute_settlement"),
    "/settlements/disputes/resolve": ("performance", "resolve_dispute"),
}


def _one(query: dict[str, str], key: str, default: str | None = None) -> str | None:
    return query.get(key, [default])[0]


def merch_route(merch: MerchService, method: str, path: str, body: dict[str, Any],
                headers: dict[str, str], query) -> tuple[int | None, dict[str, Any] | None]:
    """分派商品化合作管理接口；未命中返回 (None, None)。"""

    actor_id = headers.get("X-Actor-Id", "")
    segments = [segment for segment in path.split("/") if segment]
    if method == "POST" and path in MERCH_POST_ROUTES:
        if path == "/deliveries" and "quantity" in body:
            quantity_value = body["quantity"]
            body = {key: value for key, value in body.items() if key != "quantity"}
            body["quantity_value"] = quantity_value
        group_name, method_name = MERCH_POST_ROUTES[path]
        group = getattr(merch, group_name)
        receipt = getattr(group, method_name)(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method != "GET":
        return None, None
    if path == "/offers":
        return 200, {"items": merch.list_offers(
            actor_id=actor_id, work_id=_one(query, "work_id"),
            thread_id=_one(query, "thread_id"), status=_one(query, "status"))}
    if path == "/contracts":
        return 200, {"items": merch.list_contracts(
            actor_id=actor_id, work_id=_one(query, "work_id"),
            status=_one(query, "status"))}
    if len(segments) == 3 and segments[0] == "works" and segments[2] == "occupancy":
        merch.authorize_work_occupancy(actor_id, segments[1])
        return 200, merch.negotiation.rights_occupancy(
            segments[1], as_of=_one(query, "as_of"))
    if len(segments) == 3 and segments[0] == "design-versions" and segments[2] == "readiness":
        merch.authorize_version_readiness(actor_id, segments[1])
        return 200, merch.registry.get_version_readiness(segments[1])
    if len(segments) == 2 and segments[0] == "offers":
        return 200, merch.offer_view(actor_id, segments[1])
    if len(segments) == 2 and segments[0] == "contracts":
        return 200, merch.contract_view(actor_id, segments[1])
    if len(segments) == 3 and segments[0] == "contracts":
        contract_id = segments[1]
        merch.authorize_contract(actor_id, contract_id)
        if segments[2] == "facts":
            after = int(_one(query, "after_sequence", "0") or "0")
            return 200, {"items": merch.performance.list_facts(contract_id, after)}
        if segments[2] == "deliveries":
            return 200, {"items": merch.performance.list_deliveries(contract_id)}
        if segments[2] == "settlements":
            return 200, {"items": merch.performance.list_settlements(contract_id)}
    if len(segments) == 2 and segments[0] == "settlements":
        return 200, merch.settlement_view(actor_id, segments[1])
    if len(segments) == 3 and segments[0] == "settlements" and segments[2] == "explain":
        merch.authorize_settlement_explain(actor_id, segments[1])
        return 200, merch.explain_settlement(segments[1])
    return None, None


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
