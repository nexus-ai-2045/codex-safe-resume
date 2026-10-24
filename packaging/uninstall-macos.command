#!/bin/bash
set -euo pipefail
umask 077
source "$(dirname "$0")/macos-common.sh" 2>/dev/null || { echo '共通の配置検査が見つかりません。' >&2; exit 1; }
[ "$#" = 0 ] || { [ "$#" = 1 ] && [ "$1" = '--remove-settings' ]; } || { csr_error '指定できる追加操作は --remove-settings だけです。'; exit 1; }
APP_DIR="$HOME/Applications"
APP="$APP_DIR/Codex Safe Resume.app"
LINK="$HOME/Desktop/Codex Safe Resume.app"
OWNER="$APP_DIR/.codex-safe-resume-owner"
LOCK="$APP_DIR/.codex-safe-resume-install.lock"
csr_safe_path "$APP" && csr_safe_path "$(dirname "$LINK")" && csr_safe_path "$OWNER" || exit 1
if pgrep -f '/Codex Safe Resume.app/Contents/MacOS/' >/dev/null; then csr_error 'Safe Resumeと、接続しているChatGPTを通常終了してから再実行してください。'; exit 1; fi
if [ -e "$APP" ] || [ -L "$APP" ]; then
  mkdir "$LOCK" 2>/dev/null || { csr_error '別の配置処理または未確認のロックがあります。'; exit 1; }
  trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
  csr_verify_owner "$APP" "$OWNER" || exit 1
  if [ -e "$LINK" ] || [ -L "$LINK" ]; then
    [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$APP" ] || { csr_error '別の起動アイコンを保持して停止しました。'; exit 1; }
    rm -- "$LINK"
  fi
  rm -rf -- "$APP"; rm -- "$OWNER"
fi
if [ "${1:-}" = '--remove-settings' ]; then csr_remove_settings "$HOME/.codex-safe-resume" || exit 1; fi
echo 'アンインストールを確認しました。Codex本体と会話データは保持しました。'
