#!/bin/zsh

SCRIPT_DIR=${0:A:h}
cd -- "$SCRIPT_DIR" || exit 1
clear
echo "Canvas → Obsidian Sync 卸载定时任务"
echo "===================================="
echo
python3 setup.py --uninstall
RESULT=$?
echo
read -k 1 "?按任意键关闭窗口……"
echo
exit $RESULT
