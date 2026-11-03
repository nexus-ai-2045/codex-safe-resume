# エージェント向け入口

`Codex Safe Resume` は、`Codex Desktop` の利用枠による中断を検出し、対象チャットの再開を目指す実験版の補助ツールです。対象は `macOS` / `Windows` です。両 OS の実機受入は未完了です。詳細は [README.md](README.md) を読んでください。

このファイルはエージェント作業の入口です。手順・方針の正本は下記の既存文書にあり、ここでは重複させません。

## 正本ドキュメント

- 概要と使い方: [README.md](README.md)
- 変更手順とテスト命令: [CONTRIBUTING.md](CONTRIBUTING.md)
- 公開前検査と受入の境界: [PREFLIGHT.md](PREFLIGHT.md)
- 公開判断の境界: [PUBLIC_READY.md](PUBLIC_READY.md)
- セキュリティ: [SECURITY.md](SECURITY.md)
- 開発・検証の正本ルール: [docs/開発と検証.md](docs/開発と検証.md)
- 接続の準備: [docs/接続設定.md](docs/接続設定.md)
- 配布・配置: [packaging/README-portable-ja.md](packaging/README-portable-ja.md)

`.cursor/rules` は、このリポジトリにはありません。

## セットアップ・テスト・ビルド

`Python 3.11` 以降と `Tk` が必要です。`Windows` では `python3` を `py` に置き換えてください。

依存の導入（版とハッシュ固定）:

```bash
python3 -m pip install --require-hashes -r packaging/requirements-runtime.lock
```

開発・ビルド用の追加依存:

```bash
python3 -m pip install --require-hashes -r packaging/requirements-build.lock
```

診断（送信しない）:

```bash
python3 diagnose.py --json
```

GUI 起動:

```bash
python3 app.py gui
```

テストとバイトコード検査（[CONTRIBUTING.md](CONTRIBUTING.md) / [README.md](README.md)）:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py core.py desktop_bridge.py watch.py
```

README の `compileall` 例は `tests` も含みます。CI（`.github/workflows/test.yml`）は追加モジュールも `compileall` します。

このリポジトリに、別系統の lint 専用コマンドはありません。回帰は上記の `unittest` と `compileall` です。

配布ビルド（各 OS 向け。詳細は packaging 文書）:

- macOS: `bash packaging/build-macos.sh`
- Windows: `./packaging/build-windows.ps1`（PowerShell）

## ディレクトリ地図

- ルートの `*.py`: アプリ本体（`app.py` GUI、`core.py`、接続・キュー・監視・Windows 固有など）
- `tests/`: 単体・回帰テストと fixture
- `docs/`: 接続・受入・検証の運用文書
- `packaging/`: 依存 lock、両 OS のビルド／配置スクリプト
- `scripts/`: 接続境界などの検証スクリプト
- `.github/workflows/`: `test.yml`（回帰と配布ビルド）、`preflight.yml`（上流 gate）
- `.repo-preflight/`: ラチェット基準と共通フック（検査ロジック本体は複製しない）

## 守る線

- 個人名・個人アカウント名・個人メール・実ホームパス・鍵・認証トークン・Codex 会話本文を、Issue・PR・ログ・fixture・コミット名義／本文へ書かない。
- コミットの作者・コミッターは公開用名義だけにする。個人名義を入れない。
- [repo-preflight](https://github.com/nexus-ai-2045/repo-preflight) の必須検査を飛ばさない。`--no-verify` や hooks 差し替えで回避しない。利用版は `.github/workflows/preflight.yml` を正本とする（[PREFLIGHT.md](PREFLIGHT.md)）。
- `.repo-preflight/ratchet-baseline.json` の基準値 0 を緩めない。
- 手元では `readiness_scan.py` などで公開情報・履歴・文書整合を確認する。`blocked` または `tool_error` のまま進めない（[CONTRIBUTING.md](CONTRIBUTING.md)）。
- マージ・タグ・公開・visibility 変更は人が押す。検査成功を人の承認の代わりにしない。
- 進行中の無関係な PR の枝や内容には触れない。

## 変更の進め方

[CONTRIBUTING.md](CONTRIBUTING.md) に従う。失敗するテストを先に足し、最小実装で通し、全テストと preflight を回してから PR する。表示文言と運用文書は日本語を正本にする。
