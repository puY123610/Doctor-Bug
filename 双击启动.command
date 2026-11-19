#!/usr/bin/env bash
# Doctor Bug 一键启动（macOS / Linux）
# 第一次运行：自动建环境、装依赖，并可顺手装一个全局命令（任何文件夹里敲 drbug 就能打开）；
# 以后运行：直接打开，不再重复装。
# 不想要了：运行同文件夹里的卸载脚本，能恢复到刚下载源码时的样子。
# 做法参考 AUTOMATIC1111 的 webui.sh 与 OpenCode 的安装器（往 ~/.local/bin 放命令、给 PATH 加一行）。
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

env_ok() {
  # 光有「装好」记号不算数，还要验一遍环境里的 python 真能跑（Jack 把关意见）
  [ -f ".venv/.drbug_setup_ok" ] && [ -x ".venv/bin/python" ] \
    && .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}

install_global() {
  local bindir="$HOME/.local/bin"
  local wrap="$bindir/drbug"
  if [ -e "$wrap" ] && ! grep -q "doctor-bug-launcher" "$wrap" 2>/dev/null; then
    echo "提醒：$wrap 已经有一个不是我们放的文件，这次不装全局命令，怕覆盖你的东西。"
    return 0
  fi
  mkdir -p "$bindir"
  cat > "$wrap" <<EOF
#!/bin/sh
# doctor-bug-launcher —— Doctor Bug 一键启动脚本放的路牌，卸载脚本会删它
TARGET="$REPO/drbug"
if [ ! -x "\$TARGET" ]; then
  echo "Doctor Bug 的文件夹好像被移动或删掉了（原位置：$REPO）。重新下载源码、再跑一次启动脚本就好。"
  exit 1
fi
exec "\$TARGET" "\$@"
EOF
  chmod +x "$wrap"
  local rc
  for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
    case "$rc" in
      "$HOME/.zshrc") [ "${SHELL##*/}" = "zsh" ] || [ -f "$rc" ] || continue ;;
      *) [ -f "$rc" ] || continue ;;
    esac
    if ! grep -q ">>> drbug >>>" "$rc" 2>/dev/null; then
      {
        echo ""
        echo "# >>> drbug >>> （Doctor Bug 启动脚本加的，卸载脚本会删这一段）"
        echo 'export PATH="$HOME/.local/bin:$PATH"'
        echo "# <<< drbug <<<"
      } >> "$rc"
    fi
  done
  echo "全局命令装好了：以后新开一个终端窗口，在任何文件夹里敲 drbug 就能打开。"
}

# ---------- 1) 第一次运行才装环境 ----------
if ! env_ok; then
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
  if ! .venv/bin/pip install -r requirements.txt; then
    fail "依赖没装成功，多半是网络问题。可以换国内镜像源再试：在终端运行 .venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple ，装好后再运行这个脚本。"
  fi
  touch ".venv/.drbug_setup_ok"
  echo "环境装好了，以后再打开就直接进。"
  # 顺手装全局命令（只在这一次问）
  echo ""
  echo "要不要顺手装一个全局命令？以后在任何文件夹打开终端，敲 drbug 就能打开。"
  echo "（会在 ~/.local/bin 放一个小文件，并在终端配置里加一行；想删的时候运行同文件夹里的卸载脚本，能删干净。）"
  printf "装全局命令？[Y/n] "
  read -r ANS || ANS=""
  case "$ANS" in
    n|N) echo "好，不装。以后想装，删掉 .venv 再跑一次这个脚本就行。" ;;
    *) install_global ;;
  esac
fi

# ---------- 2) 问要修哪个项目 ----------
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "把要修的项目文件夹拖到这个窗口里（或粘贴它的路径），再按回车："
  read -r TARGET || TARGET=""
  TARGET="$(printf '%s' "$TARGET" | tr -d "'\"")"
fi
[ -d "$TARGET" ] || fail "这个文件夹不存在：$TARGET"

# ---------- 3) 进项目，打开 Doctor Bug ----------
cd "$TARGET"
exec "$REPO/drbug"
