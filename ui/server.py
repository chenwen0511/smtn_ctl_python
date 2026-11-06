#!/usr/bin/env python3
"""点灯自检页面。

只读本机串口信息，点灯请求转发给已运行的 shelf_api，避免再打开串口。

    python ui/server.py
    python ui/server.py --port 8090 --api http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "index.html")
UPSTREAM = "http://127.0.0.1:8000"


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _usb_identity(tty_path: str) -> dict:
    tty = os.path.basename(tty_path)
    cur = os.path.realpath(os.path.join("/sys/class/tty", tty, "device"))
    for _ in range(8):
        if os.path.isfile(os.path.join(cur, "idVendor")):
            return {
                "vid": _read_text(os.path.join(cur, "idVendor")),
                "pid": _read_text(os.path.join(cur, "idProduct")),
                "manufacturer": _read_text(os.path.join(cur, "manufacturer")),
                "product": _read_text(os.path.join(cur, "product")),
                "serial": _read_text(os.path.join(cur, "serial")),
            }
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return {"vid": "", "pid": "", "manufacturer": "", "product": "", "serial": ""}


def list_serial_ports() -> list[dict]:
    by_id = "/dev/serial/by-id"
    found: list[dict] = []
    names = []
    if os.path.isdir(by_id):
        names = sorted(os.path.join(by_id, name) for name in os.listdir(by_id))
    if not names:
        names = sorted(
            os.path.join("/dev", name)
            for name in os.listdir("/dev")
            if name.startswith("ttyUSB") or name.startswith("ttyACM")
        )
    for path in names:
        real = os.path.realpath(path)
        item = {
            "path": path,
            "tty": real,
            "exists": os.path.exists(real),
        }
        item.update(_usb_identity(real))
        found.append(item)
    return found


def fetch_health() -> tuple[int, dict]:
    url = UPSTREAM.rstrip("/") + "/api/health"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {"code": 1, "msg": raw or str(exc), "data": {}}
        return exc.code, body
    except Exception as exc:
        return 0, {"code": 1, "msg": "点灯接口不可用: %s" % exc, "data": {}}


def hardware_payload() -> dict:
    status, health = fetch_health()
    data = health.get("data") if isinstance(health, dict) else {}
    if not isinstance(data, dict):
        data = {}
    serial = data.get("serial") if isinstance(data.get("serial"), dict) else {}
    configured = str(serial.get("port") or "")
    baud = serial.get("baud")
    ports = list_serial_ports()
    active = None
    if configured:
        real = os.path.realpath(configured) if os.path.exists(configured) else configured
        for port in ports:
            if port["path"] == configured or port["tty"] == real or port["path"] == real:
                active = dict(port)
                break
        if active is None:
            active = {"path": configured, "tty": real, "exists": os.path.exists(configured)}
            if os.path.exists(real):
                active.update(_usb_identity(real))
    return {
        "code": 0 if status == 200 and health.get("code") == 0 else 1,
        "msg": health.get("msg") or "点灯接口无响应",
        "data": {
            "api": UPSTREAM,
            "api_http": status,
            "serial_exists": bool(data.get("serial_exists")),
            "serial_open": bool(data.get("serial_open")),
            "baud": baud,
            "parity": "8N1",
            "active": active,
            "ports": ports,
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ShelfLightUI/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _send(self, http: int, raw: bytes, content_type: str) -> None:
        self.send_response(http)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, http: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(http, raw, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            try:
                with open(INDEX, "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                self._json(500, {"code": 1, "msg": "页面文件缺失: %s" % exc, "data": {}})
                return
            self._send(200, raw, "text/html; charset=utf-8")
            return
        if path == "/api/hardware":
            payload = hardware_payload()
            self._json(200 if payload["code"] == 0 else 503, payload)
            return
        if path == "/api/health":
            status, body = fetch_health()
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self._send(status or 502, raw, "application/json; charset=utf-8")
            return
        self._json(404, {"code": 404, "msg": "未知路径: %s" % path, "data": {}})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            self._json(404, {"code": 404, "msg": "未知路径: %s" % path, "data": {}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        req = urllib.request.Request(
            UPSTREAM.rstrip("/") + path,
            data=raw,
            method="POST",
            headers={"Content-Type": self.headers.get("Content-Type") or "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
                self._send(resp.status, body, "application/json; charset=utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self._send(exc.code, body or b"{}", "application/json; charset=utf-8")
        except Exception as exc:
            self._json(502, {"code": 1, "msg": "点灯接口不可用: %s" % exc, "data": {}})


def main(argv: list[str] | None = None) -> int:
    global UPSTREAM
    parser = argparse.ArgumentParser(description="货架点灯自检页面")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--api", default=UPSTREAM, help="shelf_api 地址")
    args = parser.parse_args(argv)
    UPSTREAM = args.api.rstrip("/")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("点灯自检页面: http://%s:%d" % (args.host, args.port))
    print("转发点灯接口: %s" % UPSTREAM)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
