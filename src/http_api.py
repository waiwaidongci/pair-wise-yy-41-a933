from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                seg = [part for part in path.split("/") if part]
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif seg == ["api", "items"]:
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"items": service.list_items(role)})
                elif seg == ["api", "audit"]:
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"events": service.audit(role)})
                elif len(seg) >= 3 and seg[:2] == ["api", "items"]:
                    item_id = int(seg[2])
                    rest = seg[3:]
                    actor, role = self._identity()
                    del actor
                    if not rest:
                        self._json(200, service.get_item(item_id, role))
                    elif rest == ["records"]:
                        self._json(200, {"records": service.list_records(item_id, role)})
                    elif rest == ["readings"]:
                        self._json(200, {"readings": service.list_readings(item_id, role)})
                    elif rest == ["advisory"]:
                        self._json(200, service.current_advisory(item_id, role))
                    elif rest == ["advisories"]:
                        self._json(200, {"advisories": service.list_advisories(item_id, role)})
                    else:
                        self._json(404, {"error": "not_found"})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                seg = [part for part in path.split("/") if part]
                actor, role = self._identity()
                body = self._body()
                if seg == ["api", "items"]:
                    self._json(201, service.create_item(body, actor, role))
                elif len(seg) >= 3 and seg[:2] == ["api", "items"]:
                    item_id = int(seg[2])
                    rest = seg[3:]
                    if rest == ["records"]:
                        self._json(201, service.add_record(item_id, body, actor, role))
                    elif rest == ["transition"]:
                        target = body.get("target")
                        expected = body.get("expected_version")
                        self._json(200, service.transition(
                            item_id, target, expected, actor, role))
                    elif rest == ["readings"]:
                        self._json(201, service.register_reading(item_id, body, actor, role))
                    elif len(rest) == 3 and rest[0] == "readings" and rest[2] == "review":
                        self._json(200, service.review_reading(
                            item_id, int(rest[1]), body, actor, role))
                    elif len(rest) == 3 and rest[0] == "readings" and rest[2] == "close":
                        self._json(200, service.close_defect(
                            item_id, int(rest[1]), actor, role))
                    elif rest == ["advisory", "sign"]:
                        self._json(200, service.sign_advisory(item_id, body, actor, role))
                    elif rest == ["advisory", "restore"]:
                        self._json(200, service.restore(item_id, body, actor, role))
                    else:
                        self._json(404, {"error": "not_found"})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
