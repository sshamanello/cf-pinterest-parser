#!/usr/bin/env python3
"""Small VDS-side API for transient pin image uploads and cleanup.

Endpoints:
- GET /healthz
- POST /upload?name=pin.jpg  (accepts raw body or JSON {name,data_base64})
- DELETE /upload?name=pin.jpg

Auth:
- X-Pins-Token header must match PINS_API_TOKEN.
"""

from __future__ import annotations

import hashlib
import json
import os
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT_DIR = Path(os.environ.get("PINS_API_ROOT_DIR", "/opt/caddy/html/pins/ready")).resolve()
TOKEN = os.environ.get("PINS_API_TOKEN", "").strip()
HOST = os.environ.get("PINS_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("PINS_API_PORT", "8877"))


def _safe_name(name: str) -> str:
    candidate = Path(name).name.strip()
    if not candidate:
        return "pin.jpg"
    if candidate in {".", ".."}:
        return "pin.jpg"
    if "/" in candidate or "\\" in candidate:
        return "pin.jpg"
    return candidate


def _ensure_root() -> None:
    ROOT_DIR.mkdir(parents=True, exist_ok=True)


def _auth_ok(headers, params: dict | None = None) -> bool:
    if not TOKEN:
        return True
    got = headers.get("X-Pins-Token", "").strip()
    if not got:
        got = headers.get("Authorization", "").strip()
        if got.lower().startswith("bearer "):
            got = got[7:].strip()
    if not got and params:
        got = (params.get("token") or [""])[0].strip()
    return got == TOKEN


def _json(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    server_version = "pins-api/1.0"

    def log_message(self, format, *args):  # noqa: A003
        return

    def _unauthorized(self) -> None:
        _json(self, 401, {"ok": False, "error": "unauthorized"})

    def _require_auth(self) -> bool:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if not _auth_ok(self.headers, params):
            self._unauthorized()
            return False
        return True

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            _json(self, 200, {"ok": True, "root": str(ROOT_DIR), "exists": ROOT_DIR.exists()})
            return
        if parsed.path == "/upload":
            if not self._require_auth():
                return
            params = parse_qs(parsed.query)
            name = _safe_name((params.get("name") or ["pin.jpg"])[0])
            target = (ROOT_DIR / name).resolve()
            if ROOT_DIR not in target.parents and target != ROOT_DIR:
                self.send_error(400, "Invalid file name")
                return
            if not target.exists():
                self.send_error(404, "Not found")
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404, "Not found")

    def do_POST(self):  # noqa: N802
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path != "/upload":
            self.send_error(404, "Not found")
            return

        params = parse_qs(parsed.query)
        name = _safe_name((params.get("name") or ["pin.jpg"])[0])
        body_len = int(self.headers.get("Content-Length", "0") or "0")
        if body_len <= 0:
            self.send_error(400, "Missing body")
            return

        _ensure_root()
        target = (ROOT_DIR / name).resolve()
        if ROOT_DIR not in target.parents and target != ROOT_DIR:
            self.send_error(400, "Invalid file name")
            return

        raw = self.rfile.read(body_len)
        data = raw
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self.send_error(400, "Invalid JSON body")
                return
            if isinstance(payload, dict) and payload.get("data_base64"):
                try:
                    data = base64.b64decode(str(payload["data_base64"]))
                except Exception:
                    self.send_error(400, "Invalid data_base64")
                    return
                if payload.get("name"):
                    name = _safe_name(str(payload["name"]))
        target.write_bytes(data)
        digest = hashlib.sha1(data).hexdigest()[:10]
        _json(self, 200, {
            "ok": True,
            "name": name,
            "path": str(target),
            "bytes": len(data),
            "version": digest,
        })

    def do_DELETE(self):  # noqa: N802
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path != "/upload":
            self.send_error(404, "Not found")
            return

        params = parse_qs(parsed.query)
        name = _safe_name((params.get("name") or ["pin.jpg"])[0])
        target = (ROOT_DIR / name).resolve()
        if ROOT_DIR not in target.parents and target != ROOT_DIR:
            self.send_error(400, "Invalid file name")
            return

        if target.exists():
            target.unlink()
        _json(self, 200, {"ok": True, "deleted": name, "path": str(target)})


def main() -> None:
    _ensure_root()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"pins-api listening on {HOST}:{PORT}, root={ROOT_DIR}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
