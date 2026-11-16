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
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

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
COLOR_NAME = {"R": "红", "G": "绿", "B": "蓝"}
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")
_BIZ_LOG = os.path.join(_LOG_DIR, "shelf_light.log")

# 全局串口连接 + 锁（串口是独占资源，所有请求串行发送）
_port_lock = threading.Lock()
_shelf: ShelfPort | None = None
_serial_cfg = {"port": DEFAULT_PORT, "baud": DEFAULT_BAUD}
_biz = logging.getLogger("shelf.light")
_health_lock = threading.Lock()
_health_state: str | None = None


class _BizFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        ct = self.converter(record.created)
        return "%s.%03d" % (time.strftime("%Y-%m-%d %H:%M:%S", ct), record.msecs)


def _setup_biz_log() -> str:
    """业务日志单独落盘，不跟访问日志混在一起。"""
    os.makedirs(_LOG_DIR, exist_ok=True)
    _biz.setLevel(logging.INFO)
    _biz.propagate = False
    if not _biz.handlers:
        handler = RotatingFileHandler(
            _BIZ_LOG, maxBytes=5 * 1024 * 1024, backupCount=10, encoding="utf-8"
        )
        handler.setFormatter(_BizFormatter("%(asctime)s %(levelname)s %(message)s"))
        _biz.addHandler(handler)
    return _BIZ_LOG


def _color_label(body: dict) -> str:
    raw = body.get("color", "R")
    color = str(raw).strip().upper() if raw is not None else ""
    if not color:
        color = "R"
    name = COLOR_NAME.get(color)
    return "%s(%s)" % (name, color) if name else color


def _location_bits(text: str) -> str:
    loc = str(text).strip()
    if not loc:
        return "盘=(空)"
    try:
        board, led = parse_location(loc)
    except ValueError:
        return "盘=%s" % loc
    if led < 1:
        return "盘=%s 货架=%s" % (loc, board)
    return "盘=%s 货架=%d 灯号=%04d" % (loc, board, led)


def _describe(path: str, body: dict) -> str:
    """把请求整理成可落盘的业务描述。解析失败也尽量记下调用方传来的盘号。"""
    color = _color_label(body)
    if path in ("/api/light/on", "/api/light/off"):
        action = "单灯点亮" if path.endswith("/on") else "单灯熄灭"
        return "动作=%s 颜色=%s %s" % (action, color, _location_bits(body.get("location", "")))
    if path in ("/api/light/multi-on", "/api/light/multi-off"):
        action = "批量点亮" if path.endswith("/on") else "批量熄灭"
        locs = body.get("locations")
        if isinstance(locs, list):
            shown = ",".join(str(item).strip() for item in locs) or "(空)"
            count = len(locs)
        else:
            shown = str(locs)
            count = 0
        return "动作=%s 颜色=%s 数量=%d 盘=%s" % (action, color, count, shown)
    if path in ("/api/board/on", "/api/board/off"):
        action = "整架点亮" if path.endswith("/on") else "整架熄灭"
        return "动作=%s 颜色=%s 货架=%s 范围=整架" % (action, color, body.get("board", ""))
    if path == "/api/tower":
        action = str(body.get("action", "on")).strip().lower() or "on"
        action_name = {"on": "灯塔点亮", "off": "灯塔熄灭", "flash": "灯塔闪烁"}.get(action, "灯塔%s" % action)
        return "动作=%s 颜色=%s 货架=%s 范围=灯塔" % (action_name, color, body.get("board", ""))
    return "动作=未知 路径=%s" % path


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

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _audit(self, ok: bool, action: str, reason: str = "", sent: bool = True, count: int = 0) -> None:
        if ok and not sent:
            result = "结果=成功 下发=未发串口"
        elif ok:
            result = "结果=成功 下发=已发串口 帧数=%d" % count
        else:
            result = "结果=失败 原因=%s" % reason
        line = "来源=%s %s %s" % (self._client_ip(), action, result)
        self._detail = line
        if ok:
            _biz.info(line)
        else:
            _biz.warning(line)

    def log_message(self, fmt, *args):
        base = "[%s] %s" % (self.log_date_time_string(), fmt % args)
        detail = getattr(self, "_detail", "")
        print("%s | %s" % (base, detail) if detail else base, flush=True)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/api/health":
            ok, msg, data = _probe_serial()
            data["routes"] = sorted(ROUTES)
            self._note_health(ok, msg, data)
            self._reply(0 if ok else 1, msg, data, http=200 if ok else 503)
        else:
            self._audit(False, "动作=未知接口 路径=%s" % self.path, reason="未知接口")
            self._reply(404, "未知接口: %s" % self.path, http=404)

    def _note_health(self, ok: bool, msg: str, data: dict) -> None:
        global _health_state
        state = "ok" if ok else "fail:%s" % msg
        with _health_lock:
            if state == _health_state:
                return
            _health_state = state
        serial = data.get("serial") if isinstance(data.get("serial"), dict) else {}
        port = serial.get("port", _serial_cfg["port"])
        baud = serial.get("baud", _serial_cfg["baud"])
        if ok:
            _biz.info("来源=%s 动作=串口检查 串口=%s 波特率=%s 结果=正常" % (self._client_ip(), port, baud))
        else:
            _biz.warning("来源=%s 动作=串口检查 串口=%s 波特率=%s 结果=异常 原因=%s" % (self._client_ip(), port, baud, msg))

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        handler = ROUTES.get(path)
        if handler is None:
            self._audit(False, "动作=未知接口 路径=%s" % path, reason="未知接口")
            self._reply(404, "未知接口: %s" % path, http=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except (ValueError, json.JSONDecodeError) as exc:
            self._audit(False, "动作=请求无效 路径=%s" % path, reason="JSON 解析失败: %s" % exc)
            self._reply(400, "JSON 解析失败: %s" % exc, http=400)
            return

        action = _describe(path, body)
        try:
            frames = handler(body)
            dry_run = bool(body.get("dry_run", False))
            hex_frames = _send_frames(frames, dry_run)
            self._audit(True, action, sent=not dry_run, count=len(hex_frames))
            self._reply(0, "ok" if not dry_run else "ok (dry-run 未发串口)", {
                "frames": hex_frames,
                "count": len(hex_frames),
            })
        except ValueError as exc:
            self._audit(False, action, reason=str(exc))
            self._reply(400, str(exc), http=400)
        except Exception as exc:  # 串口错误等
            self._audit(False, action, reason="发送失败: %s" % exc)
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
    biz_log = _setup_biz_log()

    server = ThreadingHTTPServer((args.host, args.http_port), ApiHandler)
    print("货架灯接口服务已启动: http://%s:%d" % (args.host, args.http_port))
    print("串口: %s @ %d（首次请求时打开）" % (args.port, args.baud))
    print("健康检查: curl http://127.0.0.1:%d/api/health" % args.http_port)
    print("业务日志: %s" % biz_log)
    _biz.info(
        "动作=服务启动 监听=%s:%d 串口=%s 波特率=%d 业务日志=%s"
        % (args.host, args.http_port, args.port, args.baud, biz_log)
    )
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
