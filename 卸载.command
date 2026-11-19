#!/usr/bin/env bash
# Doctor Bug 卸载（macOS / Linux）
# 把这台电脑恢复到刚下载源码时的样子：启动脚本和程序运行产生的一切都删干净。
# 源码文件夹本身不删（想彻底不要了，卸载后把这个文件夹拖进废纸篓即可）。
REPO="$(cd "$(dirname "$0")" && pwd)"

WRAP="$HOME/.local/bin/drbug"
WRAP_OURS=0
if [ -f "$WRAP" ] && grep -q "doctor-bug-launcher" "$WRAP" 2>/dev/null; then WRAP_OURS=1; fi

RCS=""
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
  if [ -f "$rc" ] && grep -q ">>> drbug >>>" "$rc" 2>/dev/null; then RCS="$RCS $rc"; fi
done

echo "将删除以下内容（删完就跟刚下载源码时一样）："
FOUND=0
[ "$WRAP_OURS" = "1" ] && { echo "  - 全局命令路牌：$WRAP"; FOUND=1; }
[ -n "$RCS" ] && { echo "  - 终端配置里我们加的那一段（PATH）：$RCS"; FOUND=1; }
[ -d "$REPO/.venv" ] && { echo "  - 本文件夹里的环境：$REPO/.venv"; FOUND=1; }
[ -d "$REPO/.tools" ] && { echo "  - 本文件夹里的工具目录：$REPO/.tools"; FOUND=1; }
[ -d "$HOME/.drbug" ] && { echo "  - 程序数据（工作副本、运行记录）：$HOME/.drbug"; FOUND=1; }
[ -d "$HOME/.config/drbug" ] && { echo "  - 配置和密钥：$HOME/.config/drbug"; FOUND=1; }
if [ -f "$WRAP" ] && [ "$WRAP_OURS" != "1" ]; then
  echo "  （$WRAP 不是我们放的文件，不会动它。）"
fi
if [ "$FOUND" != "1" ]; then
  echo "没有找到要删的东西——这台电脑本来就是干净的。"
  exit 0
fi

echo ""
printf "确定全删吗？[y/N] "
read -r ANS || ANS=""
case "$ANS" in
  y|Y) ;;
  *) echo "没删，什么都没动。"; exit 0 ;;
esac

[ "$WRAP_OURS" = "1" ] && rm -f "$WRAP" && echo "已删：$WRAP"
for rc in $RCS; do
  awk '/# >>> drbug >>>/{skip=1} !skip{print} /# <<< drbug <<</{skip=0}' "$rc" > "$rc.drbug-tmp" \
    && mv "$rc.drbug-tmp" "$rc" && echo "已清：$rc 里我们加的那一段"
done
[ -d "$REPO/.venv" ] && rm -rf "$REPO/.venv" && echo "已删：$REPO/.venv"
[ -d "$REPO/.tools" ] && rm -rf "$REPO/.tools" && echo "已删：$REPO/.tools"
[ -d "$HOME/.drbug" ] && rm -rf "$HOME/.drbug" && echo "已删：$HOME/.drbug"
[ -d "$HOME/.config/drbug" ] && rm -rf "$HOME/.config/drbug" && echo "已删：$HOME/.config/drbug"

echo ""
echo "卸载完成，已经恢复到刚下载源码时的样子。"
echo "源码文件夹本身还在（$REPO），想彻底不要了，把它拖进废纸篓就行。"
