<!-- repo-preflight:review-record -->

# 公開前検査と受入の境界

Codex Safe Resumeは、本人の承認に基づき実験版ソースをPublic公開しています。現在のbridgeは安全な送信能力が未確立のため、実際の自動送信を停止します。ソース公開・CI成功・アプリ完成を区別します。

最新の対象SHA、実施結果、証拠、未達、担当と次の操作は[Issue #11](https://github.com/nexus-ai-2045/codex-safe-resume/issues/11)を正本とします。旧repoのPR結果を、新repoの検証結果として流用しません。

## 機械検査

- 上流repo-preflightを使用し、検査ロジックを複製しません。利用する版は `.github/workflows/preflight.yml` を正本とします。
- `.repo-preflight/ratchet-baseline.json` の基準値0を維持します。秘密情報・個人パス・名義の検出を例外で握りつぶしません。
- ローカルの全履歴照合は非公開の個人パターン設定を使います。CIは公開用名義の許可リストでauthor・committer・コミット本文を上流identity_gateで検査し、GitHubが生成するnoreply名義も明示します。個人パターンはCIへ渡しません。
- 今回のコミットと有効なGit設定は、操作の比較元に対するexpected-identity検査でも照合します。GitHubのマージ名義を含む全履歴を、一つの操作者名義と同一視しません。
- PRとmainのCIは、回帰6環境・両OS配布ビルド・上流ゲート・名義と依存の監査を検査します。ジョブ数と成功件数は実際の結果で確認します。

## 人間レビューと実機受入

機械検査の成功は、外部操作の承認や実機E2E成功の代替ではありません。公開範囲変更、安定版の案内、リリースは対象と操作を明示した承認に従います。

本物のCodex Desktopへの接続と安全な送信を確立し、Mac・Windowsの両方で実際の5時間・週次枠解除後の再開を確認するまで、完成・残務ゼロと判定しません。受入手順は `docs/実機受入手順.md`、公開判断の境界は `PUBLIC_READY.md` を参照してください。
