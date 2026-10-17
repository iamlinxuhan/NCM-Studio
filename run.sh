#!/usr/bin/env bash
# 启动 NCM Studio。优先使用同目录下的 .venv，没有就退回系统 python3。
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -x "$here/.venv/bin/python" ]]; then
    python="$here/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    python="python3"
else
    echo "找不到 python3，请先安装 Python 3.10 或更高版本。" >&2
    exit 1
fi

exec "$python" "$here/main.py" "$@"
