"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .commercialization import CommercializationService
from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


def _receipt(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def _route_commercialization(service, method: str, segments: list[str],
                             query: dict[str, list[str]], body: dict[str, Any],
                             actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """把作品商品化合作管理的请求分派到领域服务，未命中时返回 None。"""

    if method == "POST":
        if segments == ["partners"]:
            return _receipt(service.register_partner(actor_id=actor_id, **body))
        if segments == ["partner-members"]:
            return _receipt(service.link_partner_member(actor_id=actor_id, **body))
        if segments == ["works"]:
            return _receipt(service.register_work(actor_id=actor_id, **body))
        if segments == ["design-versions"]:
            return _receipt(service.add_design_version(actor_id=actor_id, **body))
        if segments == ["negotiations"]:
            return _receipt(service.open_negotiation(actor_id=actor_id, **body))
        if segments == ["offers"]:
            return _receipt(service.make_offer(actor_id=actor_id, **body))
        if len(segments) == 3 and segments[0] == "offers":
            offer_id = segments[1]
            action = segments[2]
            if action == "counter":
                return _receipt(service.counter_offer(actor_id=actor_id, offer_id=offer_id, **body))
            if action == "reserve":
                return _receipt(service.reserve_offer(actor_id=actor_id, offer_id=offer_id, **body))
            if action == "release":
                return _receipt(service.release_offer(actor_id=actor_id, offer_id=offer_id, **body))
            if action == "withdraw":
                return _receipt(service.withdraw_offer(actor_id=actor_id, offer_id=offer_id, **body))
            if action == "accept":
                return _receipt(service.accept_offer(actor_id=actor_id, offer_id=offer_id, **body))
        if len(segments) >= 3 and segments[0] == "contracts":
            contract_id = segments[1]
            action = segments[2]
            if len(segments) == 3:
                handlers = {
                    "sign": service.sign_contract,
                    "activate": service.activate_contract,
                    "terminate": service.terminate_contract,
                    "deliveries": service.record_delivery,
                    "design-changes": service.record_design_change,
                    "cost-revisions": service.revise_cost_basis,
                    "share-revisions": service.revise_shares,
                    "breaches": service.record_breach,
                    "rectifications": service.record_rectification,
                    "settlements": service.create_settlement,
                }
                handler = handlers.get(action)
                if handler is not None:
                    return _receipt(handler(actor_id=actor_id, contract_id=contract_id, **body))
            if len(segments) == 5 and action == "milestones" and segments[4] == "complete":
                return _receipt(service.complete_milestone(actor_id=actor_id, contract_id=contract_id,
                                                           milestone_key=segments[3], **body))
        if len(segments) == 3 and segments[0] == "settlements" and segments[2] == "disputes":
            return _receipt(service.dispute_settlement(actor_id=actor_id,
                                                       settlement_id=segments[1], **body))
    if method == "GET":
        if segments == ["pending-offers"]:
            return 200, {"items": service.list_pending_offers(actor_id=actor_id)}
        if len(segments) == 2 and segments[0] == "negotiations":
            return 200, service.get_negotiation(actor_id=actor_id, negotiation_id=segments[1])
        if len(segments) == 2 and segments[0] == "contracts":
            return 200, service.get_contract_view(actor_id=actor_id, contract_id=segments[1])
        if len(segments) == 3 and segments[0] == "contracts" and segments[2] == "timeline":
            return 200, {"items": service.contract_timeline(actor_id=actor_id,
                                                            contract_id=segments[1])}
        if len(segments) == 3 and segments[0] == "works" and segments[2] == "rights":
            at = query.get("at", [""])[0]
            return 200, service.rights_at(actor_id=actor_id, work_id=segments[1], at=at)
        if len(segments) == 2 and segments[0] == "settlements":
            return 200, service.get_settlement_view(actor_id=actor_id, settlement_id=segments[1])
        if len(segments) == 3 and segments[0] == "settlements" and segments[2] == "explanation":
            return 200, service.explain_settlement(actor_id=actor_id, settlement_id=segments[1])
    return None


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
        segments = [segment for segment in parsed.path.split("/") if segment]
        result = _route_commercialization(service, method, segments,
                                          parse_qs(parsed.query), body, actor_id)
        if result is not None:
            return result
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: CommercializationService

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

    parser = argparse.ArgumentParser(description="启动作品商品化合作管理服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = CommercializationService(database)
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
