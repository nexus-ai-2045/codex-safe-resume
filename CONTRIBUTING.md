# 開発への参加

このリポジトリの表示文言・README・運用文書は日本語を正本にします。外部APIの正式な識別子、ソースコード内の関数名、GitHub Actionsのキー名は翻訳しません。

## 変更手順

1. 既存Issueと実コードを確認し、変更する責務と受入条件を定義する。
2. 不具合を再現する失敗テスト（RED）を先に追加する。
3. 最小実装でGREENにし、共通化・リファクタリング後に全テストを実行する。
4. `python -m unittest discover -s tests -v`、`python -m compileall -q app.py core.py desktop_bridge.py watch.py` を実行する。
5. [repo-preflight](https://github.com/nexus-ai-2045/repo-preflight) の `readiness_scan.py` で公開情報・履歴・文書整合性を確認する。`blocked` または `tool_error` のままマージしない。
6. PRのHEADに対するCI全ジョブ・レビューの指摘・未解決スレッドを確認し、マージ後にも再確認する。

## セキュリティ

端末名、認証トークン、メールアドレス、Codex会話本文、絶対ホームパスをIssue・PR・ログ・fixtureへ載せない。作業者の公開名義はローカルの非公開照合で確認し、実名を表示しない。未知状態では再開送信を拒否する。人の承認を自動許可しない。

## 完成条件

Mac/Windowsの実機で利用枠の停止から復帰、次の制限での再登録、完了・承認待ち時の停止、再起動・スリープ復帰、インストールと削除を検証するまで実験版です。