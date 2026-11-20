"""On-demand retrieval. Operator questions never fetch equipment states."""
import math
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from winnum_client import WinnumClient, WinnumConfig, WinnumError
from winnum_plan import Clarification, build_plan

PRODUCTIVE = {'Станок под нагрузкой', 'Программа выполняется'}
logger = logging.getLogger(__name__)


def calendar_spans(plan):
    """Only fetch calendar days touched by selected windows, including sparse selections."""
    days = set()
    for window in plan.windows:
        end = min(window.end, plan.now)
        if end <= window.start:
            continue
        day = window.start.date()
        last = (end - timedelta(microseconds=1)).date()
        while day <= last:
            days.add(day)
            day += timedelta(days=1)
    spans = []
    for day in sorted(days):
        if spans and day == spans[-1][1] + timedelta(days=1):
            spans[-1] = (spans[-1][0], day)
        else:
            spans.append((day, day))
    return spans


def timestamp(value):
    for fmt in ('%d.%m.%Y %H:%M:%S.%f', '%d.%m.%Y %H:%M:%S',
                '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            pass
    raise WinnumError('Winnum вернул неизвестный формат времени.')


def select_products(products, names):
    if not names:
        return products
    selected = {}
    for name in names:
        exact = [p for p in products if p['name'].casefold() == name.casefold() or p['id'] == name]
        matches = exact or [p for p in products if re.match(re.escape(name) + r'(?:[_\s]|$)', p['name'], re.I)]
        if len(matches) != 1:
            raise Clarification(f'Станок «{name}» не найден однозначно. Укажите полное название из Winnum.')
        selected[matches[0]['id']] = matches[0]
    return list(selected.values())


def select_operators(rows, names):
    if not names:
        return rows
    available = set(r['Оператор'] for r in rows)
    selected = set()
    for name in names:
        matches = [s for s in available if s.casefold() == name.casefold()]
        if not matches:
            tokens = set(name.casefold().split())
            matches = [s for s in available if tokens <= set(s.casefold().split())]
        if len(matches) > 1:
            raise Clarification(f'Найдено несколько операторов для «{name}». Укажите полное ФИО.')
        selected.update(matches)
    return [r for r in rows if r['Оператор'] in selected]


class WinnumService:
    def __init__(self, client=None):
        self.client = client

    def clear_cache(self):
        if self.client:
            self.client.clear_cache()

    def execute(self, params, now=None):
        plan = build_plan(params, now)
        if self.client is None:
            self.client = WinnumClient(WinnumConfig.from_env())
        client = self.client
        started = time.monotonic()
        calls = 0
        def fetch(fn, *args):
            nonlocal calls
            if time.monotonic() - started > 120:
                raise WinnumError('Истекло общее время загрузки. Сократите период или список станков.')
            calls += 1
            return fn(*args)

        products = select_products(fetch(client.products), plan.machines)
        if not products:
            raise WinnumError('Winnum не вернул доступное оборудование приложения.')
        days = (plan.end - plan.start).days + 1
        per_product = (days if plan.intent == 'operator_days' else 1) if plan.intent.startswith('operator_') else (
            len(calendar_spans(plan)) + (len(plan.windows) if plan.intent == 'downtime' else 0))
        estimated = len(products) * per_product
        try:
            max_calls = int(os.getenv('WINNUM_MAX_REQUESTS', '200'))
            if not 1 <= max_calls <= 1000:
                raise ValueError()
        except ValueError:
            raise WinnumError('WINNUM_MAX_REQUESTS должен быть от 1 до 1000.') from None
        if estimated > max_calls:
            raise Clarification(f'Запрос потребует до {estimated} вызовов Winnum. Сократите период или укажите станки (лимит {max_calls}).')
        logger.info('Winnum plan intent=%s start=%s end=%s machines=%d estimated_calls=%d',
                    plan.intent, plan.start, plan.end, len(products), estimated)
        result = {'source': 'Winnum API', 'intent': plan.intent,
                  'period': {'start': str(plan.start), 'end': str(plan.end)},
                  'warnings': [], 'rows': []}
        if plan.intent.startswith('operator_'):
            self._operators(plan, products, fetch, result)
        else:
            self._equipment(plan, products, fetch, result)
        result['api_calls_including_catalog'] = calls
        result['row_count'] = len(result['rows'])
        return result

    def _operators(self, plan, products, fetch, result):
        # These are calendar-date summaries, not 07:00 shift summaries.
        result['period_basis'] = 'Календарные даты, не производственные смены'
        result['meaning'] = 'Время регистрации на станке, не продуктивное время и не табель посещения'
        rows = []
        periods = [(plan.start, plan.end)]
        if plan.intent == 'operator_days':
            periods = [(plan.start + timedelta(days=i), plan.start + timedelta(days=i))
                       for i in range((plan.end - plan.start).days + 1)]
        for product in products:
            for start, end in periods:
                for row in fetch(self.client.operators, product, start, end):
                    try:
                        minutes = float(row['value']) / 60000
                        operator = row['operator'].strip()
                        if not math.isfinite(minutes) or minutes < 0 or not operator:
                            raise ValueError()
                    except (KeyError, TypeError, ValueError):
                        raise WinnumError('Некорректная сводка регистраций операторов Winnum.') from None
                    if minutes > 0:
                        item = {'Оператор': operator, 'Станок': product['name'],
                                'Минуты регистрации': round(minutes, 2)}
                        if plan.intent == 'operator_days':
                            item['Дата'] = str(start)
                        rows.append(item)
        result['rows'] = select_operators(rows, plan.operators)
        if plan.end == plan.now.date():
            result['warnings'].append('Текущий календарный день ещё не завершён.')
        if not result['rows']:
            result['warnings'].append('В ответах Winnum нет регистраций по указанным условиям; это не доказательство отсутствия человека на работе.')

    def _equipment(self, plan, products, fetch, result):
        stats, events, comments = [], [], []
        result['period_basis'] = 'Производственные смены 07:00–19:00 / 19:00–07:00; дата смены — дата начала'
        result['selected_shifts'] = [f'{w.day}_{w.kind}' for w in plan.windows]
        active = [w for w in plan.windows if w.start < plan.now]
        start = min(w.start for w in active)
        end = min(max(w.end for w in active), plan.now)
        for product in products:
            intervals = []
            seen = set()
            # Priority durations accepts calendar dates, not arbitrary timestamps.
            raw = []
            for first_day, last_day in calendar_spans(plan):
                raw.extend(fetch(self.client.states, product, first_day, last_day))
            for row in raw:
                try:
                    a, b, tag = timestamp(row['START']), timestamp(row['END']), row['TAG']
                    if not tag or b < a:
                        raise ValueError()
                except (KeyError, ValueError):
                    raise WinnumError('Некорректный интервал состояний Winnum.') from None
                a, b = max(a, start), min(b, end)
                if b <= a or (a, b, tag) in seen:
                    continue
                seen.add((a, b, tag))
                intervals.append((a, b, tag))
            intervals.sort()
            if any(b > c for (_, b, _), (c, _, _) in zip(intervals, intervals[1:])):
                raise WinnumError('Winnum вернул пересекающиеся приоритетные состояния. Расчёт остановлен, чтобы не удвоить время.')
            for window in plan.windows:
                covered = productive = 0.0
                window_events = []
                for a, b, tag in intervals:
                    left, right = max(a, window.start), min(b, window.end, plan.now)
                    if right <= left:
                        continue
                    minutes = (right - left).total_seconds() / 60
                    covered += minutes
                    if tag in PRODUCTIVE:
                        productive += minutes
                    event = {'Станок': product['name'], 'Смена': f'{window.day}_{window.kind}',
                             'Тип смены': window.kind, 'Состояние': tag,
                             'Начало': left, 'Конец': right, 'Минуты': minutes}
                    events.append(event)
                    window_events.append(event)
                elapsed = max(0, (min(window.end, plan.now) - window.start).total_seconds() / 60)
                missing = max(0, elapsed - covered)
                complete = window.end <= plan.now and missing < 1 / 60
                stats.append({'Станок': product['name'], 'Смена': f'{window.day}_{window.kind}',
                              'Тип смены': window.kind, 'Продуктивные минуты': productive,
                              'Минуты с данными': covered, 'Нет данных, мин': missing,
                              'Фонд, мин': 720, 'Полная смена': complete})
                if not complete:
                    result['warnings'].append(f'{product["name"]}, {window.day}_{window.kind}: '
                                              'смена не завершена или данные неполны; КИМ полной смены не рассчитан.')
                if plan.intent == 'downtime' and any(e['Состояние'] not in PRODUCTIVE for e in window_events):
                    for row in fetch(self.client.signals, product, os.getenv('WINNUM_COMMENT_SIGNAL', 'A215'),
                                     window.start, min(window.end, plan.now)):
                        try:
                            moment = timestamp(row['event_time'])
                            text = row['value'].strip()
                        except KeyError:
                            raise WinnumError('В ответе комментариев отсутствуют время или значение.') from None
                        if not text or text.upper() == 'НЕТ СООБЩЕНИЯ':
                            continue
                        # Only associate a comment with a downtime interval containing its time.
                        matching = [e for e in window_events if e['Состояние'] not in PRODUCTIVE
                                    and e['Начало'] <= moment < e['Конец']]
                        if matching:
                            comments.append({'Станок': product['name'], 'Время': str(moment),
                                             'Состояние': matching[0]['Состояние'], 'Комментарий': text[:2000]})
        if plan.intent in ('kim', 'compare_shifts'):
            keys = ['Станок']
            if plan.intent == 'compare_shifts':
                keys.append('Тип смены')
            elif plan.aggregation == 'detailed':
                keys.append('Смена')
            groups = defaultdict(list)
            for row in stats:
                groups[tuple(row[k] for k in keys)].append(row)
            for key, rows in groups.items():
                total = sum(r['Фонд, мин'] for r in rows)
                prod = sum(r['Продуктивные минуты'] for r in rows)
                complete = all(r['Полная смена'] for r in rows)
                result['rows'].append(dict(zip(keys, key)) | {
                    'Продуктивные минуты': round(prod, 2), 'Фонд, мин': total,
                    'Минуты с данными': round(sum(r['Минуты с данными'] for r in rows), 2),
                    'КИМ, %': round(prod / total * 100, 2) if complete else None})
            if plan.intent == 'compare_shifts':
                result['differences'] = []
                for product in products:
                    grouped = {r['Тип смены']: r['КИМ, %'] for r in result['rows'] if r['Станок'] == product['name']}
                    day, night = grouped.get('День'), grouped.get('Ночь')
                    result['differences'].append({'Станок': product['name'], 'День минус ночь, п.п.':
                                                 round(day - night, 2) if day is not None and night is not None else None})
        elif plan.intent == 'downtime':
            groups = defaultdict(float)
            for event in events:
                if event['Состояние'] not in PRODUCTIVE:
                    groups[event['Станок'], event['Состояние']] += event['Минуты']
            result['rows'] = [{'Станок': machine, 'Вид простоя': tag, 'Минуты': round(minutes, 2)}
                              for (machine, tag), minutes in sorted(groups.items(), key=lambda x: -x[1])]
            result['comments'] = comments
            result['warnings'].append('Комментарии сопоставлены по времени записи и не доказывают причину простоя. При отсутствии причины не выдвигать догадок.')
        elif plan.intent == 'hourly':
            groups = defaultdict(lambda: [0.0, 0.0])
            for event in events:
                cursor = event['Начало']
                while cursor < event['Конец']:
                    hour = cursor.replace(minute=0, second=0, microsecond=0)
                    boundary = min(hour + timedelta(hours=1), event['Конец'])
                    minutes = (boundary - cursor).total_seconds() / 60
                    bucket = groups[event['Станок'], str(hour)]
                    bucket[0] += minutes
                    if event['Состояние'] in PRODUCTIVE:
                        bucket[1] += minutes
                    cursor = boundary
            result['rows'] = [{'Станок': machine, 'Час': hour, 'Минуты с данными': round(values[0], 2),
                               'Продуктивные минуты': round(values[1], 2),
                               'Загрузка полного часа, %': round(values[1] / 60 * 100, 2) if abs(values[0] - 60) < 1 / 60 else None}
                              for (machine, hour), values in sorted(groups.items())]
        result['coverage'] = [{k: round(v, 2) if isinstance(v, float) else v for k, v in r.items()} for r in stats]
        result['productive_states'] = sorted(PRODUCTIVE)


def process_winnum_query(service, query, model, history=None):
    try:
        params = model.parse_winnum_query(query, history=history)
        if 'error' in params:
            return 'ОШИБКА: не удалось разобрать вопрос с помощью AI.', None
        result = service.execute(params)
        answer = model.generate_winnum_response(query, result, history=history)
        # Always show limitations, even if the language model omits them.
        warnings = result.get('warnings', [])
        if warnings:
            answer += '\n\nОграничения данных: ' + ' '.join(warnings[:5])
            if len(warnings) > 5:
                answer += f' Ещё предупреждений: {len(warnings) - 5}.'
        return answer, result
    except Clarification as exc:
        return str(exc), None
    except WinnumError as exc:
        return 'ОШИБКА: ' + str(exc), None
