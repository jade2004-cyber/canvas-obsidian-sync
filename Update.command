#!/bin/zsh

set -u
SCRIPT_DIR=${0:A:h}
cd -- "$SCRIPT_DIR" || exit 1
clear
echo "Canvas → Obsidian Sync 更新"
echo "============================"
echo

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git pull --ff-only
  RESULT=$?
else
  TEMP_DIR=$(mktemp -d -t canvas-obsidian-sync-update)
  trap '[[ -n "${TEMP_DIR:-}" && -d "$TEMP_DIR" ]] && rm -rf -- "$TEMP_DIR"' EXIT
  echo "正在从 GitHub 下载最新版……"
  curl --fail --location --silent --show-error \
    "https://github.com/jade2004-cyber/canvas-obsidian-sync/archive/refs/heads/main.zip" \
    --output "$TEMP_DIR/update.zip"
  RESULT=$?
  if (( RESULT == 0 )); then
    ditto -x -k "$TEMP_DIR/update.zip" "$TEMP_DIR/unpacked"
    ditto "$TEMP_DIR/unpacked/canvas-obsidian-sync-main" "$SCRIPT_DIR"
    RESULT=$?
  fi
fi

if (( RESULT == 0 )); then
  python3 setup.py --refresh
  RESULT=$?
fi
echo
if (( RESULT == 0 )); then
  echo "更新完成。"
else
  echo "更新失败，现有安装未被卸载。"
fi
read -k 1 "?按任意键关闭窗口……"
echo
exit $RESULT
