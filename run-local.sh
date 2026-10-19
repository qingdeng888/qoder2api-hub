#!/usr/bin/env bash
# ===========================================================
#  Qoder2API Hub - 宿主机原生启动（调试用）
#
#  前台运行，Ctrl+C 即停；systemd 也用本脚本（见 qoder-proxy.service）。
#  配置优先级：命令行参数 > .env.local > 脚本默认值。
#
#  用法:
#    ./run-local.sh                 # 前台启动（37006）
#    PORT=38000 ./run-local.sh      # 临时换端口调试
# ===========================================================
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# 本地私密配置（0600，已被 .gitignore 忽略）：
#   API_KEY / PANEL_PASSWORD / HOST / PORT / TZ
if [ -f "$HERE/.env.local" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$HERE/.env.local"
    set +a
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-37006}"
PANEL_PASSWORD="${PANEL_PASSWORD:-admin}"
TZ="${TZ:-Asia/Shanghai}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
export HOST PORT TZ PYTHONUNBUFFERED=1

echo "[run-local] qoder-proxy starting on ${HOST}:${PORT} (python: $("$PYTHON_BIN" --version 2>&1))"
exec "$PYTHON_BIN" "$HERE/qoder_proxy.py" \
    --lan \
    --host "$HOST" \
    --port "$PORT" \
    --panel-password "$PANEL_PASSWORD" \
    "$@"
