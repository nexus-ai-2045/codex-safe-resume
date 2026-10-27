#!/bin/bash
set -euo pipefail
umask 077
[ "$(uname -s)" = 'Darwin' ] || { echo 'macOSでビルドしてください' >&2; exit 1; }
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
mkdir -p build
BUILD_LOG="$PWD/build/build-macos.log"
"$PYTHON" -c 'import tkinter; tkinter.Tcl()' > "$BUILD_LOG" 2>&1 || { echo '画面ライブラリを読み込めません。ビルド用PythonとTkを確認してください。' >&2; exit 1; }
"$PYTHON" -m pip install --require-hashes -r packaging/requirements-build.lock >> "$BUILD_LOG" 2>&1 || { echo 'ビルド依存を導入できません。' >&2; exit 1; }
"$PYTHON" packaging/build-icons.py --output build/icons >> "$BUILD_LOG" 2>&1 || { echo '配布アイコンを生成できません。'; exit 1; }
"$PYTHON" -m PyInstaller --noconfirm --clean --windowed --icon "$PWD/build/icons/SafeResume.icns" --osx-bundle-identifier com.codexsaferesume.app --name 'Codex Safe Resume' --distpath dist --workpath build --specpath build app.py >> "$BUILD_LOG" 2>&1 || { echo 'アプリをビルドできません。' >&2; exit 1; }
APP="$PWD/dist/Codex Safe Resume.app"
[ -d "$APP" ] || { echo 'アプリが生成されませんでした' >&2; exit 1; }
# 配布物内のTcl/Tkを検査。既存ユーザー領域は診断に使わない。
"$PYTHON" - "$APP/Contents/MacOS/Codex Safe Resume" >> "$BUILD_LOG" 2>&1 <<'PY_SMOKE' || { echo '配布アプリの起動環境を確認できません。' >&2; exit 1; }
import os, pathlib, subprocess, sys, tempfile
with tempfile.TemporaryDirectory() as directory:
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG')
           if key in os.environ}
    env.update(CODEX_HOME=str(pathlib.Path(directory)/'codex'),
               CODEX_RESUME_HOME=str(pathlib.Path(directory)/'state'))
    result = subprocess.run([sys.argv[1], 'diagnose'], env=env, timeout=60)
    raise SystemExit(result.returncode)
PY_SMOKE
# .app内では既存のContents/Frameworksを共有し、起動時の自己展開を避ける。
"$PYTHON" -m PyInstaller --noconfirm --clean --console --onedir --contents-directory Frameworks --name CodexSafeResumeBridge --distpath dist/bridge --workpath build/bridge --specpath build desktop_launcher.py >> "$BUILD_LOG" 2>&1 || { echo 'Desktop接続用プログラムをビルドできません。' >&2; exit 1; }
cp dist/bridge/CodexSafeResumeBridge/CodexSafeResumeBridge "$APP/Contents/MacOS/CodexSafeResumeBridge"
chmod 755 "$APP/Contents/MacOS/CodexSafeResumeBridge"
# macOS 26向けの一時的な旧UI互換設定。性能・クラッシュ対策とは別。
# Appleの仕様上、SDK 27以降でビルドするとこのキーは無視される。
"$PYTHON" - "$APP/Contents/Info.plist" >> "$BUILD_LOG" 2>&1 <<'PY_UI_COMPAT' || { echo '旧UI互換設定を適用できません。'; exit 1; }
from pathlib import Path
import plistlib
import sys
path = Path(sys.argv[1])
value = plistlib.loads(path.read_bytes())
value['UIDesignRequiresCompatibility'] = True
path.write_bytes(plistlib.dumps(value))
PY_UI_COMPAT
# No trusted publisher identity or notarization; ad-hoc only.
codesign --force --deep --sign - "$APP" >> "$BUILD_LOG" 2>&1 || { echo '署名を付けられません。' >&2; exit 1; }
codesign --verify --deep --strict "$APP" >> "$BUILD_LOG" 2>&1 || { echo '署名を検査できません。' >&2; exit 1; }
"$PYTHON" - "$APP/Contents/MacOS/CodexSafeResumeBridge" >> "$BUILD_LOG" 2>&1 <<'PY_HELPER_SMOKE' || { echo '補助プログラムを3秒以内に起動できません。' >&2; exit 1; }
import os, pathlib, subprocess, sys, tempfile
with tempfile.TemporaryDirectory() as directory:
    state = pathlib.Path(directory) / 'unused-state'
    env = dict(os.environ, CODEX_RESUME_HOME=str(state),
               CODEX_HOME=str(pathlib.Path(directory) / 'unused-codex'),
               CSR_WINDOWS_ORDER_FREE='1')
    result = subprocess.run([sys.argv[1], '--open-chat',
        '11111111-1111-4111-8111-111111111111', 'nan'], env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
    assert result.returncode == 0 and result.stdout == b'failed\n' and not result.stderr
    assert not state.exists()
PY_HELPER_SMOKE
source packaging/macos-common.sh
csr_safe_path "$PWD/release" || exit 1
mkdir -p release
STAGING="$(mktemp -d "$PWD/release/CodexSafeResume-Mac-Portable.XXXXXXXX")"
ditto "$APP" "$STAGING/Codex Safe Resume.app"
cp packaging/install-macos.command packaging/uninstall-macos.command packaging/macos-common.sh "$STAGING/"
cp LICENSE "$STAGING/LICENSE"
chmod 755 "$STAGING/install-macos.command" "$STAGING/uninstall-macos.command"
hdiutil create -volname 'Codex Safe Resume' -srcfolder "$STAGING" -ov -format UDZO 'release/CodexSafeResume-Mac-Portable.dmg' >> "$BUILD_LOG" 2>&1 || { echo '配布イメージを生成できません。' >&2; exit 1; }
echo '配布物: release/CodexSafeResume-Mac-Portable.dmg (未公証)'
