#!/usr/bin/env bash
# Doctor Bug 一键启动（macOS / Linux）
# 第一次运行：自动建环境、装依赖；以后运行：直接打开，不再重复装。
# 做法参考 AUTOMATIC1111 的 webui.sh（同类项目通行做法）。
#
# 运行之前需要先准备好：
#   1) Python 3.12（没有的话脚本会停下来告诉你怎么装，不会自己下载——太慢）
#   2) 联网（第一次装依赖要用）
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO"

fail() {
  echo ""
  echo "出问题了：$1"
  echo "把上面这段文字拍下来发给我们就行。"
  echo "也可以先试试自救：删掉本文件夹里的 .venv 再重新运行这个脚本。"
  exit 1
}

# ---------- 1) 第一次运行才装环境 ----------
if [ ! -f ".venv/.drbug_setup_ok" ]; then
  PY=""
  for c in python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
  done
  if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    echo "这台电脑上还没有可用的 Python（需要 3.12），先装好再回来运行这个脚本："
    echo "  Mac：终端里运行  brew install python@3.12"
    echo "       或者去 python.org 下载 3.12 安装包，装的时候勾选加入 PATH。"
    echo "  Linux：用系统包管理器装 python3.12 和 python3.12-venv。"
    echo "装好以后，重新运行这个脚本就行。"
    exit 1
  fi
  echo "第一次运行：正在建环境、装依赖，大约几分钟，别关窗口…"
  rm -rf .venv
  "$PY" -m venv .venv || fail "虚拟环境没建起来。"
  .venv/bin/pip install -r requirements.txt || fail "依赖没装成功，多半是网络不通（装依赖要联网）。"
  touch ".venv/.drbug_setup_ok"
  echo "环境装好了，以后再打开就直接进。"
fi

# ---------- 2) 问要修哪个项目 ----------
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "把要修的项目文件夹拖到这个窗口里（或粘贴它的路径），再按回车："
  read -r TARGET
  TARGET="$(printf '%s' "$TARGET" | tr -d "'\"")"
fi
[ -d "$TARGET" ] || fail "这个文件夹不存在：$TARGET"

# ---------- 3) 进项目，打开 Doctor Bug ----------
cd "$TARGET"
exec "$REPO/drbug"
