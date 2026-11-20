import json
import os
import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch

import requests

from winnum_client import WinnumClient, WinnumConfig, WinnumError
from winnum_plan import Clarification, build_plan
from winnum_service import WinnumService, process_winnum_query

NOW = datetime(2026, 11, 19, 12)
PRODUCT = {'id': 'winnum.org.product.WNProduct:17', 'name': '3.4_АРМА', 'ProductUUID': 'uuid-17'}


class FakeClient:
    def __init__(self):
        self.products = Mock(return_value=[PRODUCT])
        self.operators = Mock(return_value=[{'operator': 'Иванов Иван', 'value': '3600000'}])
        self.states = Mock(return_value=[])
        self.signals = Mock(return_value=[])


def params(intent='kim', **kwargs):
    return {'intent': intent, 'start_date': '2026-10-01', 'end_date': '2026-10-01',
            'machines': ['3.4'], **kwargs}


def state(start, end, tag='Программа выполняется'):
    return {'START': start, 'END': end, 'TAG': tag}


class PlanningTests(unittest.TestCase):
    def test_missing_dates_and_large_period_do_not_contact_winnum(self):
        client = FakeClient()
        for query in ({'intent': 'kim'}, params(end_date='2026-11-18'), params(start_date='wrong')):
            with self.assertRaises(Clarification):
                WinnumService(client).execute(query, NOW)
        client.products.assert_not_called()

    def test_night_window_crosses_midnight_and_keeps_start_date(self):
        plan = build_plan(params(shift_type='Ночь'), NOW)
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(plan.windows[0].start, datetime(2026, 10, 1, 19))
        self.assertEqual(plan.windows[0].end, datetime(2026, 10, 2, 7))
        self.assertEqual(plan.windows[0].day, date(2026, 10, 1))

    def test_operator_shift_does_not_silently_become_calendar_day(self):
        with self.assertRaises(Clarification):
            build_plan(params('operator_days', shift_type='Ночь'), NOW)

    def test_unknown_intent_and_future_rejected(self):
        for query in (params('sendSignal'), params(start_date='2027-01-01', end_date='2027-01-01')):
            with self.assertRaises(Clarification):
                build_plan(query, NOW)


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.service = WinnumService(self.client)

    def test_operator_machines_uses_only_summary_and_preserves_period(self):
        result = self.service.execute(params('operator_machines', operators=['Иванов'], end_date='2026-10-07'), NOW)
        self.client.operators.assert_called_once_with(PRODUCT, date(2026, 10, 1), date(2026, 10, 7))
        self.client.states.assert_not_called()
        self.client.signals.assert_not_called()
        self.assertEqual(result['rows'][0]['Минуты регистрации'], 60)

    def test_operator_days_uses_daily_summary_without_states(self):
        result = self.service.execute(params('operator_days', end_date='2026-10-02'), NOW)
        self.assertEqual([r['Дата'] for r in result['rows']], ['2026-10-01', '2026-10-02'])
        self.assertEqual(self.client.operators.call_count, 2)
        self.client.states.assert_not_called()

    def test_ambiguous_machine_or_operator_asks_instead_of_merging(self):
        self.client.products.return_value.append(dict(PRODUCT, id='other', name='3.4_ДРУГОЙ'))
        with self.assertRaises(Clarification):
            self.service.execute(params(), NOW)
        self.client.states.assert_not_called()
        self.client.products.return_value = [PRODUCT]
        self.client.operators.return_value.append({'operator': 'Иванов Пётр', 'value': '1000'})
        with self.assertRaises(Clarification):
            self.service.execute(params('operator_machines', operators=['Иванов']), NOW)

    def test_compare_shifts_fetches_states_only_and_includes_zero_production(self):
        self.client.states.return_value = [
            state('2026-10-01 07:00:00', '2026-10-01 19:00:00'),
            state('2026-10-01 19:00:00', '2026-10-02 07:00:00', 'Станок выключен')]
        result = self.service.execute(params('compare_shifts'), NOW)
        self.assertEqual([r['КИМ, %'] for r in result['rows']], [100, 0])
        self.assertEqual(result['differences'][0]['День минус ночь, п.п.'], 100)
        self.client.states.assert_called_once_with(PRODUCT, date(2026, 10, 1), date(2026, 10, 2))
        self.client.operators.assert_not_called()
        self.client.signals.assert_not_called()

    def test_only_selected_night_and_machine_contribute(self):
        self.client.states.return_value = [state('2026-10-01 00:00:00', '2026-10-03 00:00:00')]
        result = self.service.execute(params(shift_type='Ночь'), NOW)
        self.assertEqual(result['rows'][0]['Продуктивные минуты'], 720)
        self.assertEqual(result['rows'][0]['КИМ, %'], 100)

    def test_sparse_shift_selection_does_not_fetch_intervening_days(self):
        query = {'intent': 'kim', 'machines': ['3.4'],
                 'shift_list': ['2026-10-01_День', '2026-10-07_День']}
        self.service.execute(query, NOW)
        bounds = [(c.args[1], c.args[2]) for c in self.client.states.call_args_list]
        self.assertEqual(bounds, [(date(2026, 10, 1), date(2026, 10, 1)),
                                  (date(2026, 10, 7), date(2026, 10, 7))])

    def test_gaps_do_not_become_off_or_zero_kim(self):
        self.client.states.return_value = [state('2026-10-01 07:00:00', '2026-10-01 08:00:00')]
        result = self.service.execute(params(shift_type='День'), NOW)
        self.assertIsNone(result['rows'][0]['КИМ, %'])
        self.assertEqual(result['coverage'][0]['Нет данных, мин'], 660)
        self.assertTrue(result['warnings'])

    def test_in_progress_shift_has_no_final_kim(self):
        self.client.states.return_value = [state('2026-11-19 07:00:00', '2026-11-19 19:00:00')]
        result = self.service.execute(params(start_date='2026-11-19', end_date='2026-11-19', shift_type='День'), NOW)
        self.assertEqual(result['rows'][0]['Продуктивные минуты'], 300)
        self.assertIsNone(result['rows'][0]['КИМ, %'])

    def test_overlapping_states_rejected_and_duplicate_removed(self):
        full = state('2026-10-01 07:00:00', '2026-10-01 19:00:00')
        self.client.states.return_value = [full, full]
        self.assertEqual(self.service.execute(params(shift_type='День'), NOW)['rows'][0]['КИМ, %'], 100)
        self.client.states.return_value.append(state('2026-10-01 08:00:00', '2026-10-01 09:00:00', 'Простой'))
        with self.assertRaises(WinnumError):
            self.service.execute(params(), NOW)

    def test_comments_only_for_downtime_and_matched_by_time(self):
        self.client.states.return_value = [state('2026-10-01 07:00:00', '2026-10-01 19:00:00', 'Ожидание')]
        self.client.signals.return_value = [
            {'event_time': '2026-10-01 08:00:00', 'value': 'Нет заготовки'},
            {'event_time': '2026-10-01 20:00:00', 'value': 'Вне смены'}]
        result = self.service.execute(params('downtime', shift_type='День'), NOW)
        self.assertEqual(result['rows'][0]['Минуты'], 720)
        self.assertEqual(len(result['comments']), 1)
        self.assertEqual(result['comments'][0]['Комментарий'], 'Нет заготовки')
        self.assertEqual(self.client.signals.call_args.args[1], 'A215')

    def test_hourly_keeps_machine_and_does_not_fetch_operator(self):
        self.client.states.return_value = [state('2026-10-01 07:30:00', '2026-10-01 09:00:00')]
        result = self.service.execute(params('hourly', shift_type='День'), NOW)
        self.assertEqual([r['Продуктивные минуты'] for r in result['rows']], [30, 60])
        self.assertEqual([r['Загрузка полного часа, %'] for r in result['rows']], [None, 100])
        self.client.operators.assert_not_called()

    def test_failure_aborts_instead_of_returning_partial_data(self):
        self.client.operators.side_effect = [[{'operator': 'Иванов', 'value': '60000'}], WinnumError('Нет связи')]
        with self.assertRaises(WinnumError):
            self.service.execute(params('operator_days', end_date='2026-10-02'), NOW)

    def test_budget_enforced_before_history_requests(self):
        with patch.dict(os.environ, {'WINNUM_MAX_REQUESTS': '1'}):
            with self.assertRaises(Clarification):
                self.service.execute(params('operator_days', end_date='2026-10-02'), NOW)
        self.client.operators.assert_not_called()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.client = WinnumClient(WinnumConfig('http://localhost/agw.jsp', 'app:9', 'user', 'secret'))

    def response(self, body, status=200):
        response = Mock(status_code=status)
        response.iter_content.return_value = [body.encode('utf-8')]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_documented_operator_xml_and_url_decoding(self):
        xml = '<items status="success"><item operator="%D0%98%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2" value="60000"/></items>'
        with patch('winnum_client.requests.get', return_value=self.response(xml)) as get:
            rows = self.client.operators(PRODUCT, date(2026, 10, 1), date(2026, 10, 2))
        self.assertEqual(rows[0]['operator'], 'Иванов')
        self.assertEqual(get.call_args.kwargs['params']['men'], 'getOperatorsLoading')
        self.assertEqual(get.call_args.kwargs['params']['from'], '2026-10-01')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_http_xml_errors_and_redirects_never_expose_credentials(self):
        for response in [self.response('secret', 302), self.response('secret', 401),
                         self.response('<items status="error">secret</items>'), self.response('secret')]:
            with patch('winnum_client.requests.get', return_value=response):
                with self.assertRaises(WinnumError) as error:
                    self.client.products()
                self.assertNotIn('secret', str(error.exception))
        with patch('winnum_client.requests.get', side_effect=requests.Timeout('url?pwd=secret')):
            with self.assertRaises(WinnumError) as error:
                self.client.products()
            self.assertNotIn('secret', str(error.exception))

    def test_catalog_cache_and_invalidation(self):
        xml = '<items status="success"><item id="17" name="Machine" ProductUUID="uuid"/></items>'
        with patch('winnum_client.requests.get', side_effect=lambda *a, **kw: self.response(xml)) as get:
            self.client.products()[0]['name'] = 'mutated'
            self.assertEqual(self.client.products()[0]['name'], 'Machine')
            self.assertEqual(get.call_count, 1)
            self.client.clear_cache()
            self.client.products()
            self.assertEqual(get.call_count, 2)


class OrchestrationTests(unittest.TestCase):
    def test_clarification_does_not_generate_answer_or_fetch_data(self):
        model = Mock()
        model.parse_winnum_query.return_value = {'intent': 'kim'}
        client = FakeClient()
        answer, data = process_winnum_query(WinnumService(client), 'КИМ?', model)
        self.assertIn('период', answer)
        self.assertIsNone(data)
        client.products.assert_not_called()
        model.generate_winnum_response.assert_not_called()

    def test_llm_parser_uses_history_and_rejects_non_object(self):
        from llm import AIAnalyst
        model = AIAnalyst(api_key='test')
        history = [{'role': 'user', 'content': 'За октябрь 2026'}]
        with patch.object(model, '_call_llm', return_value=json.dumps(params())) as call:
            self.assertEqual(model.parse_winnum_query('А ночью?', history)['intent'], 'kim')
            self.assertEqual(call.call_args.args[0][1], history[0])
        with patch.object(model, '_call_llm', return_value='[]'):
            self.assertIn('error', model.parse_winnum_query('КИМ'))


if __name__ == '__main__':
    unittest.main()
