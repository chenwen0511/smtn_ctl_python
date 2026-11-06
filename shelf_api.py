"""
货架灯 HTTP REST 接口服务。

基于 shelf_light.py 的组帧逻辑，对外提供 HTTP 接口，
方便 MES / 上位机 / 网页等系统通过 HTTP 调用灯光控制。

启动（在 ctl_python 目录下）：
    python shelf_api.py                     # 默认 COM4 38400，监听 0.0.0.0:8000
    python shelf_api.py -p COM3 -b 38400 --host 0.0.0.0 --port 8000

接口一览（POST，Content-Type: application/json）：
    /api/light/on        {"location": "3520890", "color": "R"}
    /api/light/off       {"location": "3520890"}
    /api/light/multi-on  {"locations": ["3520890", "3520891"], "color": "B"}
    /api/light/multi-off {"locations": ["3520890", "3520891"]}
    /api/board/on        {"board": 352, "color": "G"}
    /api/board/off       {"board": 352}
    /api/tower           {"board": 352, "color": "G", "action": "on|off|flash"}

GET /api/health        健康检查，返回串口状态

通用参数：
    color   R/G/B，默认 R
    dry_run true 时只生成报文不发串口（调试用）

返回格式统一为：
    {"code": 0, "msg": "ok", "data": {"frames": ["AA 55 ...", ...]}}
    code != 0 表示失败，msg 为错误原因
"""
from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from shelf_light import (
    DEFAULT_BAUD,
    DEFAULT_BOARD,
    DEFAULT_PORT,
    ShelfPort,
    frame_all,
    frame_single,
    frame_tower,
    parse_location,
)

VALID_COLORS = ("R", "G", "B")

# 全局串口连接 + 锁（串口是独占资源，所有请求串行发送）
_port_lock = threading.Lock()
_shelf: ShelfPort | None = None
_serial_cfg = {"port": DEFAULT_PORT, "baud": DEFAULT_BAUD}


def _get_port() -> ShelfPort:
    """打开串口并复用；设备消失或断线后下次调用自动重开。"""
    global _shelf
    port = _serial_cfg["port"]
    if not os.path.exists(port):
        if _shelf is not None:
            try:
                _shelf.close()
            except Exception:
                pass
            _shelf = None
        raise RuntimeError("串口设备不存在: %s" % port)
    if _shelf is None or _shelf._ser is None or not _shelf._ser.is_open:
        _shelf = ShelfPort(port, _serial_cfg["baud"])
        _shelf.open()
    return _shelf


def _close_port() -> None:
    global _shelf
    if _shelf is not None:
        try:
            _shelf.close()
        except Exception:
            pass
        _shelf = None


def _probe_serial() -> tuple[bool, str, dict]:
    """实际探测串口：节点是否存在、能否打开、句柄是否仍可用。"""
    port = _serial_cfg["port"]
    exists = os.path.exists(port)
    data = {
        "serial": dict(_serial_cfg),
        "serial_exists": exists,
        "serial_open": False,
    }
    if not exists:
        _close_port()
        return False, "串口设备不存在: %s" % port, data
    with _port_lock:
        try:
            sp = _get_port()
            ser = sp._ser
            if ser is None or not ser.is_open:
                _close_port()
                return False, "串口未能打开: %s" % port, data
            ser.reset_output_buffer()
            _ = ser.in_waiting
            data["serial_open"] = True
            return True, "ok", data
        except Exception as exc:
            _close_port()
            data["serial_open"] = False
            return False, "串口不可用: %s" % exc, data


def _send_frames(frames: list[bytes], dry_run: bool) -> list[str]:
    hex_frames = [" ".join("%02X" % b for b in f) for f in frames]
    if dry_run:
        return hex_frames
    with _port_lock:
        sp = _get_port()
        for f in frames:
            sp.send(f)
    return hex_frames


def _hex(frames: list[bytes]) -> list[str]:
    return [" ".join("%02X" % b for b in f) for f in frames]


def _norm_color(body: dict) -> str:
    color = str(body.get("color", "R")).strip().upper()
    if color not in VALID_COLORS:
        raise ValueError("color 必须是 R/G/B，收到: %s" % color)
    return color


def _norm_board(body: dict) -> int:
    board = body.get("board", DEFAULT_BOARD)
    try:
        board = int(board)
    except (TypeError, ValueError):
        raise ValueError("board 必须是数字，收到: %s" % board)
    if not 0 <= board <= 0xFFFF:
        raise ValueError("board 超出范围: %s" % board)
    return board


def _norm_location(body: dict) -> tuple[int, int]:
    loc = str(body.get("location", "")).strip()
    board, led = parse_location(loc)  # 可能抛 ValueError
    if led < 1:
        raise ValueError("需要 7 位储位，例如 3520890，收到: %s" % loc)
    return board, led


def _norm_locations(body: dict) -> list[tuple[int, int]]:
    locs = body.get("locations")
    if not isinstance(locs, list) or not locs:
        raise ValueError("locations 必须是非空数组，例如 [\"3520890\", \"3520891\"]")
    result = []
    for loc in locs:
        board, led = parse_location(str(loc).strip())
        if led < 1:
            raise ValueError("需要 7 位储位，例如 3520890，收到: %s" % loc)
        result.append((board, led))
    return result


# ---------- 各接口处理函数，返回帧列表 ----------

def handle_light_on(body: dict) -> list[bytes]:
    board, led = _norm_location(body)
    return [frame_single(board, led, _norm_color(body), on=True)]


def handle_light_off(body: dict) -> list[bytes]:
    board, led = _norm_location(body)
    return [frame_single(board, led, _norm_color(body), on=False)]


def handle_multi_on(body: dict) -> list[bytes]:
    color = _norm_color(body)
    return [frame_single(b, l, color, on=True) for b, l in _norm_locations(body)]


def handle_multi_off(body: dict) -> list[bytes]:
    color = _norm_color(body)
    return [frame_single(b, l, color, on=False) for b, l in _norm_locations(body)]


def handle_board_on(body: dict) -> list[bytes]:
    return [frame_all(_norm_board(body), _norm_color(body), on=True)]


def handle_board_off(body: dict) -> list[bytes]:
    return [frame_all(_norm_board(body), _norm_color(body), on=False)]


def handle_tower(body: dict) -> list[bytes]:
    action = str(body.get("action", "on")).strip().lower()
    if action not in ("on", "off", "flash"):
        raise ValueError("action 必须是 on/off/flash，收到: %s" % action)
    board = _norm_board(body)
    color = _norm_color(body)
    return [frame_tower(board, color, on=(action != "off"), flash=(action == "flash"))]


ROUTES = {
    "/api/light/on": handle_light_on,
    "/api/light/off": handle_light_off,
    "/api/light/multi-on": handle_multi_on,
    "/api/light/multi-off": handle_multi_off,
    "/api/board/on": handle_board_on,
    "/api/board/off": handle_board_off,
    "/api/tower": handle_tower,
}


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "ShelfLightAPI/1.0"

    def _reply(self, code: int, msg: str, data: dict | None = None, http: int = 200):
        payload = {"code": code, "msg": msg, "data": data or {}}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(http)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):  # 简化日志
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def do_GET(self):
        if self.path.rstrip("/") == "/api/health":
            ok, msg, data = _probe_serial()
            data["routes"] = sorted(ROUTES)
            self._reply(0 if ok else 1, msg, data, http=200 if ok else 503)
        else:
            self._reply(404, "未知接口: %s" % self.path, http=404)

    def do_POST(self):
        path = self.path.rstrip("/")
        handler = ROUTES.get(path)
        if handler is None:
            self._reply(404, "未知接口: %s" % path, http=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except (ValueError, json.JSONDecodeError) as exc:
            self._reply(400, "JSON 解析失败: %s" % exc, http=400)
            return

        try:
            frames = handler(body)
            dry_run = bool(body.get("dry_run", False))
            hex_frames = _send_frames(frames, dry_run)
            self._reply(0, "ok" if not dry_run else "ok (dry-run 未发串口)", {
                "frames": hex_frames,
                "count": len(hex_frames),
            })
        except ValueError as exc:
            self._reply(400, str(exc), http=400)
        except Exception as exc:  # 串口错误等
            self._reply(500, "发送失败: %s" % exc, http=500)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="货架灯 HTTP REST 接口服务")
    parser.add_argument("-p", "--port", default=DEFAULT_PORT, help="串口，默认 %s" % DEFAULT_PORT)
    parser.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUD, help="波特率，默认 %d" % DEFAULT_BAUD)
    parser.add_argument("--host", default="0.0.0.0", help="HTTP 监听地址，默认 0.0.0.0")
    parser.add_argument("--http-port", type=int, default=8000, help="HTTP 端口，默认 8000")
    args = parser.parse_args(argv)

    _serial_cfg["port"] = args.port
    _serial_cfg["baud"] = args.baud

    server = ThreadingHTTPServer((args.host, args.http_port), ApiHandler)
    print("货架灯接口服务已启动: http://%s:%d" % (args.host, args.http_port))
    print("串口: %s @ %d（首次请求时打开）" % (args.port, args.baud))
    print("健康检查: curl http://127.0.0.1:%d/api/health" % args.http_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if _shelf is not None:
            _shelf.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
