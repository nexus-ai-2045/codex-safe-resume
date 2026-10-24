"""利用枠カードの表示契約。実サービスやDesktopへは接続しない。"""
import datetime
import unittest
from unittest.mock import patch

from quota_presentation import build_usage_view

NOW = 1800000000
TODAY = datetime.date(2026, 10, 21)


def reading(windows=None, **values):
    return dict(checked_at=NOW, windows=windows or [], **values)


def window(duration=300, remaining=75, reset=NOW + 90061, slot='primary'):
    return dict(duration_minutes=duration, remaining_percent=remaining,
                resets_at=reset, slot=slot)


class QuotaPresentation(unittest.TestCase):
    def view(self, value=None, **kwargs):
        return build_usage_view(value, now=NOW, today=TODAY, **kwargs)

    def test_service_denial_and_unknown_are_visible_without_changing_remaining(self):
        from tests.test_usage_ui import sample
        for value, expected in ((False, '制限中'), (None, '未確認'), (True, '利用可能')):
            result = sample()
            result['ordinary_usage_allowed'] = value
            view = build_usage_view(result, now=result['checked_at'])
            self.assertIn(expected, view['availability_text'])
            weekly = next(c for c in view['cards'] if c['name'] == '1週間枠')
            self.assertEqual(weekly['remaining_percent'], 75)

    def test_initial_and_failed_reads_do_not_claim_api_omitted_a_window(self):
        initial = self.view()
        failed = self.view(failed=True)
        self.assertTrue(all('読取り待ち' in c['summary'] for c in initial['cards']))
        self.assertTrue(all('読取り失敗' in c['summary'] for c in failed['cards']))
        self.assertNotIn('APIから未提供', str(initial))
        self.assertFalse(failed['stale'])
        self.assertNotIn('前回取得値', str(failed))
        last = self.view(reading([window()]), failed=True)
        self.assertIn('更新失敗', last['updated_text'])
        self.assertEqual(last['cards'][0]['remaining_percent'], 75)

    def test_unread_has_both_unknown_cards(self):
        view = self.view()
        self.assertEqual(view['plan_label'], '未確認')
        self.assertIn('未取得', view['updated_text'])
        self.assertEqual([c['name'] for c in view['cards']], ['5時間枠', '1週間枠'])
        self.assertTrue(all(c['remaining_percent'] is None for c in view['cards']))

    def test_observed_countdown_and_local_timestamp(self):
        card = self.view(reading([window()]))['cards'][0]
        self.assertEqual(card['kind'], 'observed')
        self.assertEqual(card['remaining_percent'], 75)
        self.assertIn('75%', card['summary'])
        self.assertIn('1日1時間2分', card['reset_text'])
        stamp = datetime.datetime.fromtimestamp(NOW + 90061).astimezone().strftime('%m/%d %H:%M')
        self.assertIn(stamp, card['reset_text'])

    def test_observed_missing_reset_stays_unknown_time(self):
        card = self.view(reading([window(remaining=100, reset=None)]))['cards'][0]
        self.assertEqual(card['kind'], 'observed')
        self.assertEqual(card['remaining_percent'], 100)
        self.assertEqual(card['reset_text'], '回復 時刻不明')

    def test_same_period_is_never_combined(self):
        cards = self.view(reading([window(), window(remaining=10, slot='secondary'), window(60)]))['cards']
        self.assertEqual(len(cards), 4)
        self.assertEqual(len(set(c['key'] for c in cards)), 4)
        self.assertEqual([c['remaining_percent'] for c in cards if c['kind'] == 'observed'], [75, 10, 75])

    def test_staleness_boundaries_and_failure(self):
        for checked, failed, expected in [(NOW - 120, False, False),
                                         (NOW - 120.001, False, True),
                                         (NOW - 121, False, True),
                                         (NOW + 0.001, False, False),
                                         (NOW + 1, False, False),
                                         (NOW + 5, False, False),
                                         (NOW + 5.001, False, True),
                                         (NOW, True, True)]:
            with self.subTest(checked=checked, failed=failed):
                view = self.view(reading([window()], checked_marker=True) | {'checked_at': checked}, failed=failed)
                self.assertEqual(view['stale'], expected)
                if expected:
                    self.assertIn('前回取得値', view['updated_text'])
                    self.assertIn('更新待ち', view['updated_text'])
                else:
                    self.assertNotIn('前回取得値', view['updated_text'])
                    self.assertNotIn('更新待ち', view['updated_text'])

    def test_default_clock_and_explicit_now(self):
        with patch('quota_presentation.time.time', return_value=NOW + 121) as clock:
            self.assertFalse(build_usage_view(reading(), now=NOW, today=TODAY)['stale'])
            clock.assert_not_called()
            self.assertTrue(build_usage_view(reading(), today=TODAY)['stale'])
            clock.assert_called_once_with()

    def test_past_reset_does_not_refill(self):
        card = self.view(reading([window(remaining=0, reset=NOW - 1)]))['cards'][0]
        self.assertEqual(card['remaining_percent'], 0)
        self.assertIn('回復時刻を経過', card['reset_text'])
        self.assertIn('再確認待ち', card['reset_text'])

    def test_policy_none_missing_and_unknown_duration_precedence(self):
        view = self.view(reading(plan_type='pro'))
        self.assertEqual(view['cards'][0]['kind'], 'policy_none')
        self.assertIn('API', view['cards'][0]['details'])
        self.assertEqual(view['cards'][1]['kind'], 'unknown')
        view = self.view(reading([window(None)], plan_type='pro'))
        self.assertEqual(view['cards'][0]['kind'], 'unknown')
        self.assertEqual(view['cards'][-1]['kind'], 'observed')

    def test_observation_overrides_policy_and_expired_policy_is_unknown(self):
        card = self.view(reading([window()], plan_type='pro'))['cards'][0]
        self.assertEqual(card['kind'], 'observed')
        self.assertIn('相違', card['details'])
        view = build_usage_view(reading(plan_type='pro'), now=NOW, today=datetime.date(2027, 1, 1))
        self.assertEqual(view['cards'][0]['kind'], 'unknown')

    def test_peer_selection_matches_usage_text(self):
        result = reading([window(remaining=3)], plan_type='pro', desktop_connection='read',
                         peer_status='read', peer_limits=reading([window(remaining=42)], plan_type='plus'))
        view = self.view(result)
        self.assertEqual(view['source'], '共有接続')
        self.assertEqual(view['plan_label'], 'Plus')
        self.assertEqual(view['cards'][0]['remaining_percent'], 42)
        result['peer_status'] = 'unread'
        self.assertEqual(self.view(result)['source'], '公式CLI')

    def test_missing_values_and_arbitrary_strings_are_not_invented_or_exposed(self):
        card = self.view(reading([window(remaining=None, reset=None, slot='private-value')],
                                 plan_type='private-value', email='private-value'))['cards'][0]
        self.assertEqual(card['kind'], 'unknown')
        self.assertIsNone(card['remaining_percent'])
        self.assertNotIn('private-value', str(card))


if __name__ == '__main__':
    unittest.main()
