<!-- repo-preflight:review-record -->

# 公開前レビュー記録

- 対象：Codex Safe Resume
- 判定：**blocked（実機検証・完全preflight走査が未完了）**
- 公開範囲：GitHub Public。公開済みの履歴の再確認も必要。
- 正本：GitHub `main`。PRの変更はレビューが済むまで採用前。

## 確認状況

- [x] 日本語README・ライセンス・セキュリティ方針
- [x] Python単体テスト・静的構文チェック（ローカル）
- [ ] repo-preflightの全履歴・個人情報・コミット名義の非公開照合で `pass`
- [ ] 共通CI preflightゲートで `pass`、ラチェットの基準値レビュー
- [ ] MacとWindowsの実機E2E、スリープ・再起動・多重起動
- [ ] 依存関係とインストール・アンインストールの安全性レビュー
- [ ] PR HEADのCI・レビュー・未解決スレッド照合

## 判断境界

preflightが通っても外部公開の無条件承認とはみなしません。検査不能は合格ではなく停止。個人情報や秘密情報が検出された場合は例外で握りつぶさず、根本的な混入経路を修正します。