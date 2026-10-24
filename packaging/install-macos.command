#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")" 2>/dev/null || { echo '配布フォルダーを読み取れません。' >&2; exit 1; }
source './macos-common.sh' 2>/dev/null || { echo '共通の配置検査が見つかりません。' >&2; exit 1; }
APP_SRC="$PWD/Codex Safe Resume.app"
APP_DIR="$HOME/Applications"
APP_DST="$APP_DIR/Codex Safe Resume.app"
LINK="$HOME/Desktop/Codex Safe Resume.app"
OWNER="$APP_DIR/.codex-safe-resume-owner"
LOCK="$APP_DIR/.codex-safe-resume-install.lock"
csr_safe_path "$APP_SRC" && csr_safe_path "$APP_DIR" && csr_safe_path "$(dirname "$LINK")" || exit 1
[ -d "$APP_SRC" ] || { csr_error 'アプリ本体が見つかりません。'; exit 1; }
for entry in "$APP_DST" "$LINK" "$OWNER"; do
  [ ! -e "$entry" ] && [ ! -L "$entry" ] || { csr_error '同名の配置先または所有記録を保持して停止しました。'; exit 1; }
done
mkdir -p "$APP_DIR" 2>/dev/null || { csr_error '配置先を作成できません。'; exit 1; }
mkdir "$LOCK" 2>/dev/null || { csr_error '別の配置処理または未確認のロックがあります。'; exit 1; }
stage=''; installed=0; linked=0; owned=0; success=0
cleanup() {
  if [ "$success" = 0 ]; then
    [ "$linked" = 0 ] || rm -- "$LINK" 2>/dev/null
    [ "$owned" = 0 ] || rm -- "$OWNER" 2>/dev/null
    [ "$installed" = 0 ] || rm -rf -- "$APP_DST" 2>/dev/null
  fi
  [ -z "$stage" ] || rm -rf -- "$stage" 2>/dev/null
  rmdir "$LOCK" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 1' INT TERM
stage="$(mktemp -d "$APP_DIR/.codex-safe-resume-stage.XXXXXXXX" 2>/dev/null)" || { csr_error '一時配置先を作成できません。'; exit 1; }
ditto "$APP_SRC" "$stage/application" 2>/dev/null || { csr_error 'アプリのコピーに失敗しました。'; exit 1; }
[ ! -e "$APP_DST" ] && [ ! -L "$APP_DST" ] || { csr_error '配置先が変更されました。'; exit 1; }
mv "$stage/application" "$APP_DST" 2>/dev/null || { csr_error '配置を確定できません。'; exit 1; }; installed=1
csr_write_owner "$APP_DST" "$OWNER" || { csr_error '配置の所有記録を作成できません。'; exit 1; }; owned=1
ln -s "$APP_DST" "$LINK" 2>/dev/null || { csr_error '起動アイコンを作成できません。配置を戻します。'; exit 1; }; linked=1
csr_verify_owner "$APP_DST" "$OWNER" || exit 1
success=1
echo 'ユーザー領域への配置と起動アイコン作成を確認しました。'
echo 'ログイン時の自動起動は登録していません。'
