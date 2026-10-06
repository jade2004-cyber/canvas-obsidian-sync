#!/bin/zsh

SCRIPT_DIR=${0:A:h}
cd -- "$SCRIPT_DIR" || exit 1
clear
echo "Canvas → Obsidian Sync 安装"
echo "============================"
echo
python3 setup.py
RESULT=$?
echo
if (( RESULT == 0 )); then
  echo "安装流程已完成。"
else
  echo "安装没有完成，请查看上方提示。"
fi
read -k 1 "?按任意键关闭窗口……"
echo
exit $RESULT
