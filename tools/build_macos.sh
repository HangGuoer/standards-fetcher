#!/bin/bash
# 在 Apple Silicon macOS 上重新打包。需要 Python 3.9+、PyInstaller、Pillow，
# 以及 Xcode Command Line Tools（提供 lipo，用于把 PyInstaller bootloader 裁剪为 arm64）。
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "需要 Python 3.9+。可通过 python.org 安装，或设置 PYTHON=/path/to/python3。" >&2
  exit 1
fi

if ! command -v lipo >/dev/null 2>&1 || ! lipo -info /bin/ls >/dev/null 2>&1; then
  echo "未找到可用的 lipo。请先安装 Xcode Command Line Tools：xcode-select --install" >&2
  exit 1
fi

"$PY" -m pip install --upgrade pyinstaller pillow

rm -rf build/pyinstaller dist
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$PWD/build/pyinstaller-cache}"

"$PY" -m PyInstaller \
  --noconfirm --clean --windowed \
  --name "标准规范下载器" \
  --icon build/icon.icns \
  --add-data "standards_fetcher/data:data" \
  --add-data "standards_fetcher/static:static" \
  --osx-bundle-identifier cn.standardsfetcher.app \
  run.py

codesign --force --deep --sign - "dist/标准规范下载器.app"
ditto "dist/标准规范下载器.app" "标准规范下载器.app"
echo "完成：$PWD/标准规范下载器.app"
