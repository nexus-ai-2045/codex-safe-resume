# 差分と必要環境の検証

テストの選択処理は固定版repo-preflightの `scripts/local_check.py` を再利用し、このrepoの `.test-selection.json` がsource/test roots、非Python資料の対応、native環境と配布検査の責務を持ちます。product source rootsはroot直下の実製品Pythonファイル32件を明示し、CI coordinatorは別のmapped契約で `test_ci_selection.py` に結び付けます。製品のimport調査ではproduction子dirへの依存は確認されませんでした。ローカル/CIとも同じ設定と薄い `scripts/run_ci_tests.py` を使います。

## 公開前レビュー中の状態

このpatchは上流semantic修正の固定版 `0d220f19256bd3edcca014d14091f66cd64ccfb1` の実blobへpinとsource hashを同期しています。Core固定先は現時点ではlocal commitであり、公開・配送は別のレビューと承認が必要です。`SemanticBoundaryFixtures` はusage_client変更から無関係なplatform/installer検査を選ばないことと、新しい未宣言productファイルをunknownで止めることを要求し、この固定sourceに対してローカル合格しました。

## planを先に確認する

固定版のCore checkoutを別途用意し、CIと同じselector fileを指定します。実際の比較元SHAが必要で、比較元が取得できなければ停止します。親workspaceのvenvを変更せず、ローカルでuvを使う場合は `uv run --no-project` を指定してください。

```bash
python scripts/run_ci_tests.py plan --base-ref origin/main \
  --selector-script /path/to/fixed-core/scripts/local_check.py \
  --receipt /path/to/local-receipts/plan.json
```

receiptのselected_tests/reasons/unresolvedとmatrixを読みます。未知の変更・未対応check・固定版不一致は失敗です。空の選択はテスト起動ゼロとして記録し、unittestの既定discoverへ倒しません。receiptは追跡する製品本文へ入れず、既存taskの検証置き場へ保存します。

```bash
SAFE_SELECTOR_SCRIPT=/path/to/fixed-core/scripts/local_check.py \
python scripts/run_ci_tests.py run --base-ref origin/main \
  --selector-script /path/to/fixed-core/scripts/local_check.py \
  --environment portable --receipt /path/to/local-receipts/run.json
```

選択を同じ版から再計算し、明示された `tests/test_name.py` の完全なfilename patternだけを標準unittest loaderへ渡します。ファイル消失、loader error、0件のtest、unsupported filenameは失敗です。`test_*.py`の一括discoverは行いません。

## 環境と配布検査

portableは選択されたtestをUbuntuのPython3.11/3.13へ渡し、言語版の互換を検証します。macOS/Windowsは、その選択とrepo-owned native test一覧の積集合だけをprimaryのPython3.13へ渡します。実OSのAPI契約をLinuxで代替合格にしません。packagingが必要な場合も対象OSのPython3.13へ結び付けます。

runtime lockとbuild lockのhash固定導入は維持します。CI接続自体などstdlib-onlyの選択ではruntime依存導入を起動しません。製品moduleの変更は配布物へ入るため、同じsource_rootsの宣言からmacOS/Windowsの既存buildを必要と判定します。別の製品module一覧は持ちません。packaging scriptは対象OSだけに結び付け、Windows installerをusage_client変更に追加しません。

app/usage/queue/desktop等の実startup境界には、生成したfixtureから公式stdioへ接続する `test_desktop_startup_stdio.py` と、relayを検査する `test_desktop_stdio.py` をowner契約で含めます。選択時はWindowsのWinDLL/pipeとMacのPOSIX select/pipe双方で実行します。runtimeにwebsocketをimportするため、両stdio検査のruntime lock導入は省略しません。動的fixtureのため静的graphだけで関係を解決できないことを、テスト除外の理由にしません。新しいproductファイルを追加したときはsource_rootsへの所有宣言も必要です。

CI coordinatorのstdlib-only fixtureもWindows/Macのprimaryで検証します。runnerまたはそのfixtureだけの変更ならLinux3.11/3.13と合わせて4環境ですが、runtime/build依存は導入しません。workflow自体の変更は配布/候補保存条件のintegrationを検証するため、既存のmacOS/Windows packaging checkを追加します。全unitへ戻すことはしません。OS対応は別actorのpackage smokeへ委ねず、runner自身のpath/loader/subprocess境界で確認します。

CIは最初にstdlibだけでplanを作り、必要なmatrixを決めてから依存導入を行います。packagingが必要なWindows3.13で、main pushまたは同一repo PRの候補ZIP/版/hashを上流契約のまま保存します。fork PRとmerge_groupには保存しません。新しい `選択した検査の結果` は、planの不要判定とjob失敗・取消・欠測を区別します。既存preflight workflowの名義・公開情報等のgateは変更しません。

## 検証と限界

CI接続は `tests/test_ci_selection.py` の合成fixtureで、空/明示ファイル/loader error/0件/不正filename/OS範囲/hash固定を検査します。APIのplan検証と製品test実走は別です。CI coordinatorとそのfixtureは同じmapped componentが所有し、製品moduleの自動依存graphへ混ぜません。実製品変更でstdioのstartup検査を省く例外は作りません。

実OSのpackaging、Tk、Windows PowerShell/native APIは、該当OSのCI/実機で別途確認が必要です。ローカルの合成fixture成功をnative CI成功や公開承認へ読み替えません。branch protectionが旧固定matrix名をrequiredにしているかは未確認なので、公開前に必要check名も人間レビューします。
