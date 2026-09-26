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
                if path == "/health":
                    self._json(200, {"status": "ok"})
                    return
                if path == "/":
                    self._html(root / "index.html")
                    return
                actor, role = self._identity()
                del actor
                if path == "/api/items":
                    self._json(200, {"items": service.list_items(role)})
                    return
                if path == "/api/audit":
                    self._json(200, {"events": service.audit(role)})
                    return
                segments = [s for s in path.split("/") if s]
                if len(segments) >= 3 and segments[:2] == ["api", "items"]:
                    item_id = int(segments[2])
                    if len(segments) == 3:
                        self._json(200, service.get_item(item_id, role))
                        return
                    if len(segments) == 4 and segments[3] == "records":
                        self._json(200, {"records": service.list_records(item_id, role)})
                        return
                    if len(segments) == 4 and segments[3] == "readings":
                        self._json(200, {"readings": service.list_readings(item_id, role)})
                        return
                    if len(segments) == 4 and segments[3] == "recommendations":
                        self._json(200, {"recommendations": service.list_recommendations(item_id, role)})
                        return
                self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                    return
                segments = [s for s in path.split("/") if s]
                if len(segments) >= 4 and segments[:2] == ["api", "items"]:
                    item_id = int(segments[2])
                    action = segments[3]
                    if len(segments) == 4 and action == "records":
                        self._json(201, service.add_record(item_id, body, actor, role))
                        return
                    if len(segments) == 4 and action == "transition":
                        self._json(200, service.transition(
                            item_id, body.get("target"),
                            body.get("expected_version"), actor, role))
                        return
                    if len(segments) == 4 and action == "readings":
                        self._json(201, service.register_reading(item_id, body, actor, role))
                        return
                    if len(segments) == 4 and action == "notice":
                        self._json(201, service.issue_notice(item_id, body, actor, role))
                        return
                    if len(segments) == 4 and action == "withdraw-notice":
                        self._json(201, service.withdraw_notice(item_id, body, actor, role))
                        return
                    if len(segments) == 6 and action == "readings" and segments[5] == "review":
                        self._json(200, service.review_reading(
                            item_id, int(segments[4]), actor, role))
                        return
                    if len(segments) == 6 and action == "readings" and segments[5] == "close":
                        self._json(200, service.close_reading(
                            item_id, int(segments[4]), actor, role))
                        return
                    if len(segments) == 6 and action == "recommendations" and segments[5] == "sign":
                        self._json(200, service.sign_recommendation(
                            item_id, int(segments[4]), body.get("level"), actor, role))
                        return
                    if len(segments) == 6 and action == "recommendations" and segments[5] == "release":
                        self._json(200, service.release_recommendation(
                            item_id, int(segments[4]), actor, role))
                        return
                self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
