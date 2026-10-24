# 控えから取り込んだ古い版（2026-10-24）

このフォルダには、履歴を作り直す前の非公開の控えにだけ残っていた中身を、消さずに残すために置いています。

- 置き方: `<控えの枝またはタグ>/<元のパス>`。中身は控えのままです（下の「伏せたもの」を除く）。
- ここにあるファイルは実行も読み込みもされません。テストの収集からも外しています。
- 今のコードへ採り入れるかは、ファイルごとに別のPRで決めます。
- 公開側の履歴に同じ中身があるもの、行がすべて公開側にあるもの、ほかの版で行がそろうものは入れていません。
- 意図して外されていたもの（個人の情報を含む、公開側で削除済みなど）は入れていません。

## 入れたファイル

| 元のパス | 控えの枝・タグ | 置き場所 | 備考 |
|---|---|---|---|
| `.github/workflows/preflight.yml` | `ci/bump-preflight-gate` | `ci/bump-preflight-gate/.github/workflows/preflight.yml` |  |
| `PREFLIGHT.md` | `docs/japanese-preflight-gate` | `docs/japanese-preflight-gate/PREFLIGHT.md` |  |
| `SECURITY.md` | `docs/japanese-preflight-gate` ほか2 | `docs/japanese-preflight-gate/SECURITY.md` |  |
| `app.py` | `fix/fail-closed-quota-percentages` | `fix/fail-closed-quota-percentages/app.py` |  |
| `app.py` | `fix/japanese-diagnostics-clean` ほか1 | `fix/japanese-diagnostics-clean/app.py` |  |
| `tests/test_diagnostics.py` | `fix/japanese-diagnostics-clean` ほか1 | `fix/japanese-diagnostics-clean/tests/test_diagnostics.py` |  |
| `tests/test_monitor_reconciliation.py` | `fix/monitoring-terminal-reconciliation` | `fix/monitoring-terminal-reconciliation/tests/test_monitor_reconciliation.py` |  |
| `watch.py` | `fix/monitoring-terminal-reconciliation` | `fix/monitoring-terminal-reconciliation/watch.py` |  |
| `.github/workflows/preflight.yml` | `main` ほか7 | `main/.github/workflows/preflight.yml` |  |
| `diagnose.py` | `test/safe-local-desktop-preflight` | `test/safe-local-desktop-preflight/diagnose.py` |  |
| `tests/test_diagnose.py` | `test/safe-local-desktop-preflight` | `test/safe-local-desktop-preflight/tests/test_diagnose.py` |  |

## 伏せたもの

- なし
