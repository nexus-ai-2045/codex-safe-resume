"""契約・接続の表示は本人確認、接続成功、取得元一致を混同しない。"""
import json
from datetime import date
import unittest
from unittest.mock import Mock, patch

import app
from usage_client import UsageReadError
from tests import test_usage_client as fixture


def response(plan='pro', account='private-account'):
    value = fixture.reply(accountId=account)
    value['rateLimits']['planType'] = plan
    return value


class PlanDisplay(unittest.TestCase):
    def setUp(self):
        validation = patch('app.validate_executable', return_value='fixture')
        validation.start()
        self.addCleanup(validation.stop)

    def read(self, primary, peer=None):
        return fixture.UsageClientTests().read(result=primary, peer_reply=peer)[0]

    def test_plan_and_same_account_are_anonymized(self):
        result = self.read(response(), response('plus'))
        self.assertEqual(result['plan_type'], 'pro')
        self.assertEqual(result['peer_limits']['plan_type'], 'plus')
        self.assertIs(result['account_match'], True)
        self.assertEqual(result['peer_status'], 'read')
        self.assertNotIn('private-account', json.dumps(result))

    def test_different_accounts_are_reported_without_identifiers(self):
        result = self.read(response(), response(account='private-other'))
        self.assertIs(result['account_match'], False)
        self.assertNotIn('private-other', json.dumps(result))

    def test_missing_ids_never_imply_same_account(self):
        for missing in (None, '', ' ', True):
            with self.subTest(missing=missing):
                result = self.read(response(), response(account=missing))
                self.assertIsNone(result['account_match'])

    def test_missing_or_unknown_plan_never_invents_pro_or_leaks_text(self):
        for plan in (None, True, {}, 'unknown', 'private-secret-value'):
            with self.subTest(plan=plan):
                result = self.read(response(plan))
                self.assertIsNone(result['plan_type'])
                self.assertNotIn('private-secret-value', json.dumps(result))
                self.assertIsNone(result['account_match'])

    def test_invalid_peer_does_not_break_valid_cli_usage_or_claim_a_match(self):
        result = self.read(response(), {'rateLimitsByLimitId': None, 'primary': {}})
        self.assertEqual(result['plan_type'], 'pro')
        self.assertEqual(result['peer_status'], 'invalid')
        self.assertIsNone(result['peer_limits'])
        self.assertIsNone(result['account_match'])

    def test_no_peer_means_unverified_not_no_desktop(self):
        result = self.read(response())
        self.assertEqual(result['peer_status'], 'not_provided')
        self.assertIsNone(result['account_match'])

    def test_missing_plan_never_infers_no_five_hour_limit_from_absence(self):
        result = self.read(response(None))
        text = app.usage_text(result)
        self.assertIn('5時間枠: 未確認（APIから未提供）', text)
        self.assertNotIn('無制限', text)

    def test_pro_policy_and_real_weekly_usage_are_separate(self):
        text = app.usage_text(self.read(response('pro')), today=date(2026, 10, 20))
        self.assertIn('5時間枠: プラン案内では制限なし', text)
        self.assertIn('APIの枠情報は未提供', text)
        self.assertIn('1週間枠: 制限あり・残り25%', text)
        self.assertIn('2026-10-20', text)

    def test_unverified_pro_variants_do_not_inherit_public_plan_policy(self):
        for plan in ('prolite', 'promax'):
            with self.subTest(plan=plan):
                text = app.usage_text(self.read(response(plan)), today=date(2026, 10, 20))
                self.assertIn('5時間枠: 未確認（APIから未提供）', text)
                self.assertNotIn('制限なし', text)

    def test_plus_missing_quota_still_shows_plan_limit_and_unknown_amount(self):
        text = app.usage_text(self.read(response('plus')), today=date(2026, 10, 20))
        self.assertIn('5時間枠: 制限あり（プラン案内・残量と回復時刻は未取得）', text)
        self.assertIn('1週間枠:', text)

    def test_api_five_hour_window_is_not_hidden_by_pro_policy(self):
        result = self.read(response())
        result['windows'].append(dict(duration_minutes=300, remaining_percent=40, resets_at=1900000000))
        text = app.usage_text(result, today=date(2026, 10, 20))
        self.assertIn('5時間枠: 制限あり・残り40%', text)
        self.assertIn('公式案内と相違', text)

    def test_expired_or_future_policy_does_not_claim_no_limit(self):
        for today in (date(2026, 12, 1), date(2026, 10, 19)):
            with self.subTest(today=today):
                text = app.usage_text(self.read(response()), today=today)
                self.assertIn('5時間枠: 未確認（APIから未提供）', text)
                self.assertNotIn('対象外', text)

    def test_unknown_api_period_prevents_a_definitive_not_applicable_label(self):
        result = self.read(response())
        result['windows'][0]['duration_minutes'] = None
        text = app.usage_text(result, today=date(2026, 10, 20))
        self.assertIn('5時間枠: 未確認（APIの期間情報が不足）', text)
        self.assertIn('1週間枠: 未確認（APIの期間情報が不足）', text)
        self.assertNotIn('対象外', text)

    def test_operator_confirmation_never_becomes_a_live_connection(self):
        config = {'mode': 'guarded', 'desktop_confirmed': True,
                  'manual_exclusion_acknowledged': True}
        text = app.connection_text(config)
        self.assertIn('本人確認: 済', text)
        self.assertIn('共有接続: 未検査', text)
        self.assertIn('アカウント: 未照合', text)

    def test_connection_and_account_match_do_not_imply_operator_confirmation(self):
        reading = dict(plan_type='pro', peer_limits={'plan_type': 'plus'}, peer_status='read',
                       desktop_connection='read', account_match=True)
        text = app.connection_text({'mode': 'prepared'}, reading)
        self.assertIn('Pro', text)
        self.assertIn('Plus', text)
        self.assertIn('本人確認: 未', text)
        self.assertIn('共有接続: 読取り成功', text)
        self.assertIn('アカウント: 一致', text)
        self.assertIn('自動送信: 停止', text)

    def test_mismatch_is_explicit_without_claiming_identity_or_disconnection(self):
        text = app.connection_text({'mode': 'prepared'},
                    dict(desktop_connection='read', peer_status='read', account_match=False))
        self.assertIn('アカウント: 不一致', text)
        self.assertIn('共有接続: 読取り成功', text)

    def test_invalid_plan_metadata_is_unknown_without_rendering_raw_values(self):
        for value in ([], {}, True, 'private-plan-value'):
            with self.subTest(value=value):
                text = app.connection_text({}, dict(plan_type=value, peer_limits={'plan_type': value}))
                self.assertIn('不明', text)
                self.assertNotIn('private-plan-value', text)

    def test_malformed_peer_usage_does_not_discard_valid_cli_result(self):
        peer = response()
        peer['rateLimits']['primary']['usedPercent'] = 10 ** 1000
        result = self.read(response(), peer)
        self.assertEqual(result['peer_status'], 'invalid')
        self.assertEqual(result['plan_type'], 'pro')
        self.assertIsNone(result['account_match'])

    def test_read_only_probe_does_not_load_threads_or_send(self):
        probe = Mock()
        probe.__enter__ = Mock(return_value=probe)
        probe.__exit__ = Mock(return_value=False)
        probe.quota.return_value = response()
        result = dict(plan_type='pro', peer_limits={'plan_type': 'pro'}, peer_status='read', account_match=True)
        with patch('app.usage_executable', return_value='fixture'), \
                patch('app.DesktopBridge', return_value=probe) as bridge, \
                patch('app.read_limits', return_value=result) as read, \
                patch('app.monitor_once') as monitor:
            actual = app.read_usage_status({'mode': 'prepared'})
        self.assertEqual(actual['desktop_connection'], 'read')
        self.assertTrue(bridge.call_args.kwargs['transport_only'])
        read.assert_called_once_with('fixture', peer_reply=probe.quota.return_value, cancel=None)
        probe.prepare_thread.assert_not_called()
        probe.send_once.assert_not_called()
        monitor.assert_not_called()

    def test_shared_connection_failure_still_displays_cli_plan(self):
        with patch('app.usage_executable', return_value='fixture'), \
                patch('app.DesktopBridge', side_effect=ValueError('private-path')), \
                patch('app.read_limits', return_value={'plan_type': 'pro'}) as read:
            actual = app.read_usage_status({'mode': 'prepared'})
        self.assertEqual(actual['desktop_connection'], 'unavailable')
        self.assertEqual(actual['plan_type'], 'pro')
        self.assertNotIn('private-path', json.dumps(actual))
        read.assert_called_once_with('fixture', peer_reply=None, cancel=None)

    def test_changed_cli_is_rejected_before_either_connection(self):
        with patch('app.validate_executable', side_effect=ValueError), \
                patch('app.usage_executable', side_effect=ValueError), \
                patch('app.DesktopBridge') as bridge, patch('app.read_limits') as read:
            with self.assertRaises(ValueError): app.read_usage_status({'mode': 'prepared'})
        bridge.assert_not_called()
        read.assert_not_called()

    def test_cli_replaced_during_shared_probe_is_not_started(self):
        # 共有接続の確認中にpinが変わった場合、最初の検証を使い回さない。
        for shared_error in (False, True):
            with self.subTest(shared_error=shared_error):
                probe = Mock()
                probe.__enter__ = Mock(return_value=probe)
                probe.__exit__ = Mock(return_value=False)
                probe.quota.return_value = response()
                bridge_value = ValueError('pin changed') if shared_error else None
                with patch('app.usage_executable', side_effect=ValueError('pin changed')), \
                        patch('app.DesktopBridge', return_value=probe, side_effect=bridge_value), \
                        patch('app.read_limits') as read:
                    with self.assertRaises(ValueError):
                        app.read_usage_status({'mode': 'prepared'})
                read.assert_not_called()

    def test_verified_shared_read_is_the_quota_display_source(self):
        peer = response('plus')
        peer['rateLimits']['primary'].update(windowDurationMins=300, usedPercent=20)
        result = self.read(response(), peer)
        result['desktop_connection'] = 'read'
        text = app.usage_text(result, today=date(2026, 10, 20))
        self.assertIn('5時間枠: 制限あり・残り80%', text)
        self.assertIn('1週間枠: 未確認', text)
        self.assertIn('共有接続', text)
        self.assertNotIn('対象外', text)

    def test_cli_read_failure_does_not_discard_valid_shared_limits(self):
        probe = Mock()
        probe.__enter__ = Mock(return_value=probe)
        probe.__exit__ = Mock(return_value=False)
        probe.quota.return_value = response('plus')
        with patch('app.usage_executable', return_value='fixture'), \
                patch('app.DesktopBridge', return_value=probe), \
                patch('app.read_limits', side_effect=UsageReadError('private-failure')):
            result = app.read_usage_status({'mode': 'prepared'})
        self.assertIsNone(result['plan_type'])
        self.assertIsNone(result['account_match'])
        self.assertEqual(result['peer_limits']['plan_type'], 'plus')
        self.assertIn('共有接続', app.usage_text(result))
        self.assertNotIn('private-failure', json.dumps(result))


if __name__ == '__main__': unittest.main()
