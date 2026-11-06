"""
立宇智能货架「新版测试 / 无写号 _IC」串口控制。

对应厂家 reelshelfTEST.exe 的「新版测试」页：
COM3, 38400, 8N1, 主板 352，储位 7 位直接用（如 3520890）。

点灯前请先在「料架测试」里点「关闭」，否则 COM3 会被占用。
"""
from __future__ import annotations

import argparse
import sys
import time

import serial

from led_map import encode_led_word

DEFAULT_PORT = "COM4"
DEFAULT_BAUD = 38400
DEFAULT_BOARD = 352

COLOR_BYTE = {
    "R": 0x21,
    "G": 0x11,
    "B": 0x01,
}

# 灯塔颜色编码与储位灯不同（来自厂家 DLL LightHouse_IC 实测报文）
TOWER_COLOR_BYTE = {
    "R": 0x00,
    "G": 0x01,
    "B": 0x02,
}


def _board_bytes(board: int) -> bytes:
    if not 0 <= board <= 0xFFFF:
        raise ValueError("主板号超出范围: %s" % board)
    return bytes((board >> 8, board & 0xFF))


def _color_byte(color: str) -> int:
    key = color.strip().upper()
    if key not in COLOR_BYTE:
        raise ValueError("颜色必须是 R/G/B，收到: %s" % color)
    return COLOR_BYTE[key]


def parse_location(text: str) -> tuple[int, int]:
    """7 位储位 -> (主板号, 灯序号)。主板号也可直接传 1~3 位数字。"""
    s = text.strip()
    if s.isdigit() and len(s) == 7:
        return int(s[:3]), int(s[3:])
    if s.isdigit() and 1 <= len(s) <= 3:
        return int(s), 0
    raise ValueError("请输入 7 位储位（如 3520890）或主板号（如 352）: %s" % text)


def frame_single(board: int, led: int, color: str = "R", on: bool = True) -> bytes:
    """单灯。AA 55 + 主板 + 颜色 + 灯号 + 00 + 亮度 + 88"""
    word = encode_led_word(led)
    bright = 0xFF if on else 0x00
    return (
        bytes((0xAA, 0x55))
        + _board_bytes(board)
        + bytes((_color_byte(color), word >> 8, word & 0xFF, 0x00, bright, 0x88))
    )


def frame_all(board: int, color: str = "R", on: bool = True) -> bytes:
    """整板。55 AA + 主板 + 颜色 + 201 字节 FF(亮) 或 00(灭)。"""
    fill = 0xFF if on else 0x00
    return bytes((0x55, 0xAA)) + _board_bytes(board) + bytes((_color_byte(color),)) + bytes([fill] * 201)


def frame_tower(board: int, color: str = "R", on: bool = True, flash: bool = False) -> bytes:
    """灯塔。AA 54 + 主板 + 00 00 + 颜色(00=R/01=G/02=B) + 状态(00灭/01亮/11闪) + 88。"""
    if flash:
        mode = 0x11
    elif on:
        mode = 0x01
    else:
        mode = 0x00
    key = color.strip().upper()
    if key not in TOWER_COLOR_BYTE:
        raise ValueError("颜色必须是 R/G/B，收到: %s" % color)
    return bytes((0xAA, 0x54)) + _board_bytes(board) + bytes((0x00, 0x00, TOWER_COLOR_BYTE[key], mode, 0x88))


class ShelfPort:
    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD, timeout: float = 0.2):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser: serial.Serial | None = None

    def open(self) -> None:
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=2,
            )
        except serial.SerialException as exc:
            raise RuntimeError(
                "无法打开 %s（%s）。确认设备存在且没有其它程序占用该口。"
                % (self.port, exc)
            ) from exc

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    def send(self, data: bytes, gap: float = 0.05) -> None:
        if self._ser is None or not self._ser.is_open:
            raise RuntimeError("串口未打开")
        self._ser.write(data)
        self._ser.flush()
        time.sleep(gap)

    def __enter__(self) -> "ShelfPort":
        self.open()
        return self

    def __exit__(self, *args) -> None:
        self.close()


def _print_frame(label: str, data: bytes) -> None:
    hexstr = " ".join("{:02X}".format(b) for b in data)
    print("%s (%d B): %s" % (label, len(data), hexstr))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="新版测试协议：用串口点亮立宇货架灯（主板 352 / 储位 3520890）"
    )
    parser.add_argument("-p", "--port", default=DEFAULT_PORT, help="串口，默认 COM3")
    parser.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUD, help="波特率，默认 38400")
    parser.add_argument("-c", "--color", default="R", help="颜色 R/G/B，默认 R")
    parser.add_argument("-n", "--dry-run", action="store_true", help="只打印报文，不发串口")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_on = sub.add_parser("on", help="单灯常亮，如 on 3520890")
    p_on.add_argument("location", help="7 位储位")
    p_off = sub.add_parser("off", help="单灯灭")
    p_off.add_argument("location", help="7 位储位")
    p_allon = sub.add_parser("allon", help="整板全亮，如 allon 352")
    p_allon.add_argument("board", nargs="?", default=str(DEFAULT_BOARD))
    p_alloff = sub.add_parser("alloff", help="整板全灭")
    p_alloff.add_argument("board", nargs="?", default=str(DEFAULT_BOARD))
    p_tw = sub.add_parser("tower", help="灯塔亮")
    p_tw.add_argument("board", nargs="?", default=str(DEFAULT_BOARD))
    p_twoff = sub.add_parser("tower-off", help="灯塔灭")
    p_twoff.add_argument("board", nargs="?", default=str(DEFAULT_BOARD))

    p_multi_on = sub.add_parser("multi-on", help="批量点亮多个灯，如 multi-on 3520890 3520891 3520892")
    p_multi_on.add_argument("locations", nargs="+", help="多个 7 位储位，空格分隔")
    p_multi_off = sub.add_parser("multi-off", help="批量熄灭多个灯")
    p_multi_off.add_argument("locations", nargs="+", help="多个 7 位储位，空格分隔")

    args = parser.parse_args(argv)
    color = args.color

    # 批量多灯操作
    if args.cmd in ("multi-on", "multi-off"):
        frames = []
        for loc in args.locations:
            board, led = parse_location(loc)
            if led < 1:
                raise SystemExit("批量操作需要 7 位储位，例如 3520890，错误输入: %s" % loc)
            frame = frame_single(board, led, color, on=(args.cmd == "multi-on"))
            frames.append((frame, "%03d%04d" % (board, led)))

        print("批量%s %d 个灯 %s:" % ("亮" if args.cmd == "multi-on" else "灭", len(frames), color))
        for i, (frame, loc_label) in enumerate(frames, 1):
            _print_frame("  [%d] %s" % (i, loc_label), frame)

        if not args.dry_run:
            with ShelfPort(args.port, args.baud) as sp:
                for frame, _ in frames:
                    sp.send(frame)
                print("已发送 %d 条指令 -> %s %d" % (len(frames), args.port, args.baud))
        return 0

    if args.cmd in ("on", "off"):
        board, led = parse_location(args.location)
        if led < 1:
            raise SystemExit("单灯需要 7 位储位，例如 3520890")
        frame = frame_single(board, led, color, on=(args.cmd == "on"))
        label = "单灯%s %03d%04d %s" % ("亮" if args.cmd == "on" else "灭", board, led, color)
    elif args.cmd in ("allon", "alloff"):
        board, _ = parse_location(args.board) if len(args.board) == 7 else (int(args.board), 0)
        frame = frame_all(board, color, on=(args.cmd == "allon"))
        label = "整板%s %s %s" % ("亮" if args.cmd == "allon" else "灭", board, color)
    elif args.cmd == "tower":
        board, _ = parse_location(args.board) if len(args.board) == 7 else (int(args.board), 0)
        frame = frame_tower(board, color, on=True)
        label = "灯塔亮 %s %s" % (board, color)
    else:
        board, _ = parse_location(args.board) if len(args.board) == 7 else (int(args.board), 0)
        frame = frame_tower(board, color, on=False)
        label = "灯塔灭 %s" % board

    _print_frame(label, frame)
    if args.dry_run:
        return 0

    with ShelfPort(args.port, args.baud) as sp:
        sp.send(frame)
        print("已发送 -> %s %d" % (args.port, args.baud))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
