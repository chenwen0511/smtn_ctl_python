#!/usr/bin/env bash
# 货架灯 HTTP 服务：创建虚拟环境，并启停 shelf_api.py
#
# 用法:
#   ./start.sh start        # 串口 FT232 by-id，HTTP 8000
#   ./start.sh stop
#   ./start.sh restart
#   ./start.sh status
#   ./start.sh setup        # 只创建 .venv 并安装依赖
#   ./start.sh enable-boot  # 安装 systemd 并开机自启
#   ./start.sh disable-boot # 取消开机自启

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
LOG_DIR="$ROOT/log"
PID_FILE="$LOG_DIR/.shelf_api.pid"
LOG_FILE="$LOG_DIR/shelf_api.log"
PYTHON="${PYTHON:-python3}"

# 910B 上货架灯接 FT232，用 by-id 固定指向同一块转接器（ttyUSB 编号会变）
SERIAL_PORT="/dev/serial/by-id/usb-FTDI_USB_Serial_Converter_FTB6SPL3-if00-port0"
SERIAL_BAUD="38400"
HTTP_HOST="0.0.0.0"
HTTP_PORT="8000"
UNIT_NAME="shelf-light.service"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"

usage() {
  cat <<EOF
用法: $(basename "$0") {setup|start|stop|restart|status|enable-boot|disable-boot}

默认参数: -p /dev/serial/by-id/usb-FTDI_USB_Serial_Converter_FTB6SPL3-if00-port0 --http-port 8000

命令:
  start         确保环境后启动（已 enable-boot 则走 systemd）
  stop          停止服务
  restart       重启
  status        查看运行状态
  setup         只创建 .venv 并安装依赖
  enable-boot   安装 systemd 单元并开机自启
  disable-boot  取消开机自启

示例:
  $0 start
  $0 enable-boot
  $0 stop
EOF
}

setup() {
  if [[ ! -x "$VENV/bin/python" ]]; then
    echo "创建虚拟环境: $VENV"
    "$PYTHON" -m venv "$VENV"
  else
    echo "虚拟环境已存在: $VENV"
  fi
  echo "安装依赖..."
  "$VENV/bin/pip" install -r "$ROOT/requirements.txt"
  echo "环境就绪"
}

ensure_env() {
  local need_install=0
  if [[ ! -x "$VENV/bin/python" ]]; then
    echo "创建虚拟环境: $VENV"
    "$PYTHON" -m venv "$VENV"
    need_install=1
  fi
  if [[ "$need_install" -eq 1 ]] || ! "$VENV/bin/python" -c "import serial" 2>/dev/null; then
    echo "安装依赖..."
    "$VENV/bin/pip" install -r "$ROOT/requirements.txt"
  fi
}

is_running() {
  if systemd_active; then
    return 0
  fi
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    rm -f "$PID_FILE"
  fi
  return 1
}

systemd_active() {
  systemctl is-active --quiet "$UNIT_NAME" 2>/dev/null
}

systemd_enabled() {
  systemctl is-enabled --quiet "$UNIT_NAME" 2>/dev/null
}

stop_pidfile() {
  if [[ ! -f "$PID_FILE" ]]; then
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "停止旧进程 pid=$pid"
    kill "$pid" 2>/dev/null || true
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$PID_FILE"
}

write_unit() {
  local user
  user="$(id -un)"
  cat <<EOF
[Unit]
Description=货架灯 HTTP 接口服务 (ctl_python)
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=${user}
SupplementaryGroups=dialout
WorkingDirectory=${ROOT}
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/bin/mkdir -p ${LOG_DIR}
ExecStart=${VENV}/bin/python ${ROOT}/shelf_api.py -p ${SERIAL_PORT} -b ${SERIAL_BAUD} --host ${HTTP_HOST} --http-port ${HTTP_PORT}
Restart=always
RestartSec=5
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=multi-user.target
EOF
}

enable_boot() {
  ensure_env
  mkdir -p "$LOG_DIR"
  echo "安装 systemd 单元 $UNIT_NAME ..."
  write_unit | sudo tee "$UNIT_PATH" >/dev/null
  sudo systemctl daemon-reload
  stop_pidfile
  sudo systemctl enable --now "$UNIT_NAME"
  echo "已设置开机自启，并立即启动"
  systemctl --no-pager --full status "$UNIT_NAME" || true
}

disable_boot() {
  if [[ -f "$UNIT_PATH" ]] || systemd_enabled || systemd_active; then
    sudo systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
    echo "已取消开机自启并停止 systemd 服务"
  else
    echo "未安装 systemd 开机自启"
  fi
}

start() {
  ensure_env
  if systemd_enabled || systemd_active; then
    sudo systemctl start "$UNIT_NAME"
    echo "已通过 systemd 启动 $UNIT_NAME"
    return 0
  fi
  if is_running; then
    echo "服务已在运行 (pid $(cat "$PID_FILE"))"
    return 0
  fi

  mkdir -p "$LOG_DIR"
  cd "$ROOT"
  echo "启动货架灯 HTTP 服务..."
  PYTHONUNBUFFERED=1 nohup "$VENV/bin/python" "$ROOT/shelf_api.py" \
    -p "$SERIAL_PORT" \
    -b "$SERIAL_BAUD" \
    --host "$HTTP_HOST" \
    --http-port "$HTTP_PORT" \
    >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 0.3
  if ! is_running; then
    echo "启动失败，查看日志: $LOG_FILE" >&2
    tail -n 20 "$LOG_FILE" >&2 || true
    rm -f "$PID_FILE"
    return 1
  fi
  echo "已启动 pid=$(cat "$PID_FILE")"
  echo "  HTTP  http://${HTTP_HOST}:${HTTP_PORT}"
  echo "  串口  ${SERIAL_PORT} @ ${SERIAL_BAUD}"
  echo "  健康  curl http://127.0.0.1:${HTTP_PORT}/api/health"
  echo "  日志  $LOG_FILE"
}

stop() {
  if systemd_enabled || systemd_active; then
    sudo systemctl stop "$UNIT_NAME"
    echo "已通过 systemd 停止 $UNIT_NAME"
    stop_pidfile
    return 0
  fi
  if ! is_running; then
    echo "服务未运行"
    return 0
  fi
  stop_pidfile
  echo "已停止"
}

status() {
  if systemd_enabled || systemd_active; then
    systemctl --no-pager --full status "$UNIT_NAME" || true
    return 0
  fi
  if is_running; then
    echo "运行中 pid=$(cat "$PID_FILE")"
    echo "  日志 $LOG_FILE"
    return 0
  fi
  echo "未运行"
  return 1
}

cmd="${1:-start}"

case "$cmd" in
  setup)        setup ;;
  start)        start ;;
  stop)         stop ;;
  restart)      stop; start ;;
  status)       status ;;
  enable-boot)  enable_boot ;;
  disable-boot) disable_boot ;;
  -h|--help|help) usage ;;
  *)
    echo "未知命令: $cmd" >&2
    usage >&2
    exit 1
    ;;
esac
