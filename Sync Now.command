#!/bin/zsh

APP_DIR="$HOME/Library/Application Support/CanvasObsidianSync"
clear
if [[ ! -f "$APP_DIR/canvas_sync.py" || ! -f "$APP_DIR/canvas_courses.json" ]]; then
  echo "尚未完成安装，请先双击 Install.command。"
  RESULT=1
else
  echo "正在立即同步全部课程……"
  echo
  python3 "$APP_DIR/canvas_sync.py" --config "$APP_DIR/canvas_courses.json"
  RESULT=$?
fi
echo
read -k 1 "?按任意键关闭窗口……"
echo
exit $RESULT
