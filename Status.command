#!/bin/zsh

SCRIPT_DIR=${0:A:h}
cd -- "$SCRIPT_DIR" || exit 1
clear
python3 manager.py status
RESULT=$?
echo
read -k 1 "?按任意键关闭窗口……"
echo
exit $RESULT
