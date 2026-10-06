#!/bin/zsh

SCRIPT_DIR=${0:A:h}
cd -- "$SCRIPT_DIR" || exit 1
clear
echo "Canvas → Obsidian Sync 修改设置"
echo "================================"
echo
python3 setup.py --configure
RESULT=$?
echo
read -k 1 "?按任意键关闭窗口……"
echo
exit $RESULT
