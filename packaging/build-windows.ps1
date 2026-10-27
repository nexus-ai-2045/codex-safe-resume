param([string]$PythonExecutable = 'py')
$ErrorActionPreference='Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Windowsでビルドしてください' }
Set-Location (Join-Path $PSScriptRoot '..')
New-Item -ItemType Directory -Force -Path build | Out-Null
$BuildLog=Join-Path $PWD 'build/build-windows.log'
& $PythonExecutable -c 'import tkinter; tkinter.Tcl()' *> $BuildLog
if ($LASTEXITCODE -ne 0) { throw 'Python / Tkinterが必要です' }
& $PythonExecutable -X utf8 -m pip --isolated install --index-url https://pypi.org/simple --require-hashes -r packaging/requirements-build.lock *>> $BuildLog
if ($LASTEXITCODE -ne 0) { throw 'ビルド依存の導入に失敗しました' }
& $PythonExecutable packaging/build-icons.py --output build/icons *>> $BuildLog
if ($LASTEXITCODE -ne 0) { throw '配布アイコンを生成できません' }
$ApplicationIcon = Join-Path $PWD 'build/icons/SafeResume.ico'
& $PythonExecutable -m PyInstaller --noconfirm --clean --windowed --icon $ApplicationIcon --onedir --name CodexSafeResume --distpath dist --workpath build --specpath build app.py *>> $BuildLog
if ($LASTEXITCODE -ne 0) { throw 'アプリのビルドに失敗しました' }
$exe = 'dist/CodexSafeResume/CodexSafeResume.exe'
if (-not (Test-Path $exe)) { throw 'アプリ本体が生成されませんでした' }
# windowed executableは標準出力を持たないことがある。終了コードで判定する。
$StartupCheck = @'
import os, pathlib, subprocess, sys, tempfile
with tempfile.TemporaryDirectory() as directory:
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG')
           if key in os.environ}
    env.update(CODEX_HOME=str(pathlib.Path(directory)/'codex'),
               CODEX_RESUME_HOME=str(pathlib.Path(directory)/'state'))
    result = subprocess.run([sys.argv[1], 'diagnose'], env=env, timeout=60)
    raise SystemExit(result.returncode)
'@
& $PythonExecutable -c $StartupCheck $exe *>> $BuildLog
if ($LASTEXITCODE -ne 0) { throw '配布アプリの起動環境を確認できません' }
& $PythonExecutable -m PyInstaller --noconfirm --clean --console --onefile --name CodexSafeResumeBridge --distpath dist/bridge --workpath build/bridge --specpath build desktop_launcher.py *>> $BuildLog
if ($LASTEXITCODE -ne 0) { throw 'Desktop接続用プログラムのビルドに失敗しました' }
Copy-Item -LiteralPath 'dist/bridge/CodexSafeResumeBridge.exe' -Destination 'dist/CodexSafeResume/CodexSafeResumeBridge.exe'
New-Item -ItemType Directory -Force -Path release | Out-Null
. (Join-Path $PSScriptRoot 'windows-common.ps1')
Assert-CsrSafePath (Join-Path $PWD 'release')
$staging=Join-Path $PWD ('release/CodexSafeResume-Portable.'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $staging | Out-Null
Copy-Item -LiteralPath 'dist/CodexSafeResume' -Destination $staging -Recurse
Copy-Item packaging/install-windows.ps1,packaging/uninstall-windows.ps1,packaging/windows-common.ps1 -Destination $staging
Copy-Item -LiteralPath 'LICENSE' -Destination (Join-Path $staging 'LICENSE')
Compress-Archive -Path "$staging/*" -DestinationPath 'release/CodexSafeResume-Windows-Portable.zip' -Force
Write-Host '配布物: release/CodexSafeResume-Windows-Portable.zip (レジストリを触らない配置型)'
