#!/bin/bash
# 双击启动：优先打开已打包的 macOS 应用，找不到时回退到 Python 源码。
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/标准规范下载器.app"

#if [ -d "$APP" ]; then
#  open "$APP"
#  exit 0
#fi

PY="$(command -v python3 2>/dev/null || true)"
if [ -n "$PY" ] && "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  cd "$DIR"
  exec "$PY" -m standards_fetcher.cli serve
fi

/usr/bin/osascript -e 'display dialog "未找到标准规范下载器.app，也没有可用的 Python 3.9+。\n\n请直接双击项目中的“标准规范下载器.app”。" buttons {"好"} default button "好" with icon caution'
exit 1
