"""匿名の取得結果を表示カードへ変換する。I/Oや利用権限の判定は行わない。"""
import datetime
import math
import time

from plan_metadata import PLAN_LABELS, POLICY_REVIEWED_ON, policy_is_current, window_policy


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _local_time(value, pattern):
    if not _number(value):
        return None
    try:
        return datetime.datetime.fromtimestamp(value).astimezone().strftime(pattern)
    except (OverflowError, OSError, ValueError):
        return None


def build_usage_view(result, *, now=None, today=None, failed=False):
    """5時間・週次と追加枠を個別表示し、未取得値と古い値を区別する。"""
    current = time.time() if now is None else now
    original = result if isinstance(result, dict) else None
    shared = bool(original and original.get('desktop_connection') == 'read'
                  and original.get('peer_status') == 'read'
                  and isinstance(original.get('peer_limits'), dict))
    data = (original['peer_limits'] if shared else original) or {}
    source = '共有接続' if shared else ('公式CLI' if original is not None else '未取得')
    plan = data.get('plan_type')
    plan_label = PLAN_LABELS.get(plan, '未確認') if isinstance(plan, str) else '未確認'
    checked = data.get('checked_at')
    checked_text = _local_time(checked, '%m/%d %H:%M:%S')
    # 時計の分解能などによる小さな未来へのずれは5秒まで許容する。
    stale = bool(original is not None and (failed or
                 checked_text is None or checked - current > 5 or current - checked > 120))
    if original is None:
        updated = '未取得・読取り失敗・更新待ち' if failed else '未取得'
    else:
        updated = f'取得 {checked_text or "時刻不明"}・{source}'
        if failed:
            updated += '・更新失敗'
        if stale:
            updated += '・前回取得値・更新待ち'
    policy_note = f'案内確認 {POLICY_REVIEWED_ON.isoformat()}'
    if not policy_is_current(today):
        policy_note += '・再確認が必要'
    windows = [w for w in data.get('windows', []) if isinstance(w, dict)]
    cards = []

    def add(name, duration, window=None, index=None):
        policy = window_policy(plan, duration, today=today)
        details = [policy_note]
        remaining = window.get('remaining_percent') if window is not None else None
        valid = _number(remaining) and 0 <= remaining <= 100
        reset_text = ''
        if window is not None and valid:
            kind = 'observed'
            summary = f'制限あり・残り{remaining:g}%'
            reset = window.get('resets_at')
            when = _local_time(reset, '%m/%d %H:%M')
            reset_text = f'回復 {when or "時刻不明"}'
            if when is not None:
                if reset <= current:
                    reset_text += '・回復時刻を経過・再確認待ち'
                else:
                    minutes = math.ceil((reset - current) / 60)
                    days, minutes = divmod(minutes, 1440)
                    hours, minutes = divmod(minutes, 60)
                    reset_text += '・あと' + (f'{days}日' if days else '') + f'{hours}時間{minutes}分'
            if policy is False:
                details.append('公式案内と相違・要確認（実測を優先）')
        else:
            remaining = None
            kind = 'unknown'
            if original is None:
                summary = '未取得（読取り失敗）' if failed else '未取得（読取り待ち）'
            elif window is not None:
                summary = '未確認（APIの残量情報が不足）'
                reset_text = '回復 時刻不明'
            elif any(w.get('duration_minutes') is None for w in windows):
                summary = '未確認（APIの期間情報が不足）'
            elif policy is False:
                kind = 'policy_none'
                summary = 'プラン案内では制限なし（Pro）'
                details.append('APIの枠情報は未提供')
            elif policy is True:
                summary = '制限あり（プラン案内・残量と回復時刻は未取得）'
            else:
                summary = '未確認（APIから未提供）'
        if stale:
            details.append('前回取得値・更新待ち')
        # APIから来た任意文字列やslot値を識別子へ流さない。
        key = f'window-{index}' if index is not None else f'missing-{duration}'
        cards.append(dict(key=key, name=name, kind=kind, remaining_percent=remaining,
                          summary=summary, reset_text=reset_text, details='・'.join(details)))

    for duration, name in ((300, '5時間枠'), (10080, '1週間枠')):
        matching = [(i, w) for i, w in enumerate(windows) if w.get('duration_minutes') == duration]
        if matching:
            for index, window in matching:
                add(name, duration, window, index)
        else:
            add(name, duration)
    for index, window in enumerate(windows):
        duration = window.get('duration_minutes')
        if duration not in (300, 10080):
            name = f'{duration:g}分枠' if _number(duration) and duration > 0 else '長さ不明の枠'
            add(name, duration, window, index)
    ordinary = data.get('ordinary_usage_allowed')
    availability = ('利用可否: 利用可能（取得時点）' if ordinary is True else
                    '利用可否: 制限中' if ordinary is False else '利用可否: 未確認')
    if not data.get('account_bound'):
        availability += '・アカウント照合情報なし'
    return dict(cards=cards, updated_text=updated, stale=stale,
                plan_label=plan_label, source=source, availability_text=availability)
