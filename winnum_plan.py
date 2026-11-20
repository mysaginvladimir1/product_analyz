"""Validate model output before any network request; build bounded shift windows."""
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Clarification(Exception):
    pass


def local_now():
    try:
        return datetime.now(ZoneInfo(os.getenv('WINNUM_TIMEZONE', 'Europe/Moscow'))).replace(tzinfo=None)
    except ZoneInfoNotFoundError:
        raise Clarification('Не найдена часовая зона WINNUM_TIMEZONE. Установите зависимости проекта (tzdata).') from None


@dataclass(frozen=True)
class Window:
    day: date
    kind: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Plan:
    intent: str
    start: date
    end: date
    machines: tuple
    operators: tuple
    windows: tuple
    aggregation: str
    now: datetime


def _names(params, key):
    values = params.get(key, [])
    if not isinstance(values, list) or len(values) > 100 or any(
            not isinstance(s, str) or not s.strip() or len(s) > 200 for s in values):
        raise Clarification('Уточните список станков и операторов.')
    return tuple(dict.fromkeys(s.strip() for s in values))


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def build_plan(params, now=None):
    if not isinstance(params, dict):
        raise Clarification('Не удалось разобрать запрос. Укажите показатель, период и станок или оператора.')
    if params.get('clarification'):
        raise Clarification(str(params['clarification'])[:500])
    intent = params.get('intent', params.get('metrics_type', 'kim'))
    allowed = {'kim', 'downtime', 'hourly', 'compare_shifts', 'operator_machines', 'operator_days'}
    if not isinstance(intent, str) or intent not in allowed:
        raise Clarification('Сейчас доступны КИМ, загрузка, простои, сравнение смен, дни и станки работы оператора. Уточните нужный показатель.')
    now = now or local_now()
    try:
        known_fields = {'intent', 'metrics_type', 'start_date', 'end_date', 'shift_list',
                        'shift_type', 'machines', 'operators', 'aggregation', 'clarification'}
        if set(params) - known_fields:
            raise Clarification('Не удалось применить все ограничения вопроса. Укажите период по датам и, при необходимости, дневные или ночные смены.')
        shifts = params.get('shift_list') or []
        if not isinstance(shifts, list) or len(shifts) > 732:
            raise ValueError()
        selected = []
        for shift in shifts:
            day, kind = shift.rsplit('_', 1)
            if kind not in ('День', 'Ночь'):
                raise ValueError()
            selected.append((_date(day), kind))
        if selected:
            start, end = min(d for d, _ in selected), max(d for d, _ in selected)
        else:
            if not params.get('start_date') or not params.get('end_date'):
                raise Clarification('За какой период выполнить анализ? Укажите начальную и конечную дату.')
            start, end = _date(params['start_date']), _date(params['end_date'])
        max_days = int(os.getenv('WINNUM_MAX_DAYS', '31'))
        if not 1 <= max_days <= 366:
            raise ValueError()
        if end < start or (end - start).days + 1 > max_days:
            raise Clarification(f'Укажите период от 1 до {max_days} дней. Вся история автоматически не загружается.')
        if start > now.date():
            raise Clarification('Указанный период ещё не наступил.')
        if end > now.date():
            raise Clarification('Конец периода находится в будущем. Укажите дату не позднее сегодняшней.')
        kind = params.get('shift_type')
        if kind not in (None, 'День', 'Ночь'):
            raise ValueError()
        if not selected:
            for offset in range((end - start).days + 1):
                day = start + timedelta(days=offset)
                for typ in ('День', 'Ночь'):
                    if kind is None or kind == typ:
                        selected.append((day, typ))
        windows = tuple(Window(day, typ, datetime.combine(day, time(7 if typ == 'День' else 19)),
                               datetime.combine(day, time(7 if typ == 'День' else 19)) + timedelta(hours=12))
                        for day, typ in sorted(set(selected)))
        if intent.startswith('operator_') and (shifts or kind or params.get('start_shift_type') or params.get('end_shift_type')):
            raise Clarification('Сводка регистраций операторов доступна по календарным дням. Укажите период без ограничения на смену.')
        if intent == 'compare_shifts' and {w.kind for w in windows} != {'День', 'Ночь'}:
            raise Clarification('Для сравнения нужны дневные и ночные смены. Укажите период без ограничения на один тип смен.')
        if not intent.startswith('operator_') and not any(w.start < now for w in windows):
            raise Clarification('Выбранная смена ещё не началась.')
        aggregation = params.get('aggregation', 'period')
        if aggregation not in ('period', 'detailed'):
            raise ValueError()
        operators = _names(params, 'operators')
        if operators and not intent.startswith('operator_'):
            raise Clarification('Расчёт показателей оборудования с фильтром по оператору пока не поддерживается. Можно запросить дни или станки его работы.')
        return Plan(intent, start, end, _names(params, 'machines'), operators, windows, aggregation, now)
    except (ValueError, TypeError, AttributeError):
        raise Clarification('Не удалось определить период и смены. Укажите даты в формате ГГГГ-ММ-ДД и тип смены.') from None
