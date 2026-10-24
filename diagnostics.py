import argparse

"""監視状態の日本語表示。生の例外文やローカルパスは表示しない。"""

def monitor_status(result):
    state=result.get('state') if isinstance(result,dict) else None
    if state=='disabled':
        return '自動再開は無効のため、監視を停止しています'
    if state=='busy':
        return '別の監視処理が確認中です'
    if state=='unsupported-transport':
        return '安全なDesktop送信経路が未検証のため、自動送信を停止しています'
    if state=='quota-wait':
        return '利用枠の解除を待っています'
    if state=='offline':
        return 'Codexへの接続を確認できません'
    if state=='unavailable':
        return 'Codexの会話データを読み取れません'
    if state=='empty':
        return '検出されたチャットがありません'
    if state=='monitor-error':
        return '監視中にエラーが発生しました。次回再確認します'
    if state=='checked':
        results=result.get('results') or []
        sent=sum(1 for item in results if isinstance(item,(list,tuple)) and len(item)==2 and item[1]=='dispatched')
        queued=sum(1 for item in results if isinstance(item,(list,tuple)) and len(item)==2 and item[1]=='queued')
        blocked=sum(1 for item in results if isinstance(item,(list,tuple)) and len(item)==2 and item[1]=='blocked')
        if blocked:
            return f'再開を安全に確認できないチャットがあります（{blocked}件）。手動確認が必要です'
        if sent:
            return f'再開要求を送信しました（{sent}件）。結果の確認が必要です'
        if queued:
            return f'再開要求を受け付けました（{queued}件）。会話への到達を確認しています'
        return '監視は正常です。今回の再開対象はありません'
    return '監視状態を判定できません。自動再開せず確認が必要です'


class JapaneseParser(argparse.ArgumentParser):
    """不正な入力やファイルパスをそのままエラーへ再掲しない。"""
    def __init__(self, *args, **kwargs):
        kwargs['add_help'] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = '操作'
        self._optionals.title = '設定'
        self.add_argument('-h', '--help', action='help', help='使い方を表示する')
    def error(self, message):
        self.exit(2, '引数を確認してください。--helpで使い方を表示できます。\n')
