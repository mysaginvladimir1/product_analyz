from data_manager import load_excel_data
import pandas as pd
import inspect
import sys
import argparse
from pathlib import Path
from datetime import datetime
from ml_core import ProductionAnalytics
import re
from llm import GoRouterAnalyst
import argparse


# ==================== Функции для работы с датами и сменами ====================
def parse_date_with_shift(date_str):
    """
    Разбирает строку вида '2025-12-27' или '2025-12-27-День' / '2025-12-27-Ночь'.
    Возвращает (date, shift_type), где shift_type может быть None, 'День' или 'Ночь'.
    """
    match = re.match(r'(\d{4}-\d{2}-\d{2})(?:-(День|Ночь))?', date_str)
    if not match:
      raise ValueError(f"Неверный формат даты: {date_str}. Ожидается YYYY-MM-DD или YYYY-MM-DD-День/Ночь")
    date_part = match.group(1)
    shift_type = match.group(2) if match.group(2) else None
    return datetime.strptime(date_part, '%Y-%m-%d').date(), shift_type

def get_shifts_from_range(df, start_date_str, end_date_str,start_shift_type=None, end_shift_type=None):
    """
    Возвращает список идентификаторов смен (строки 'Дата_смены_День/ночь')
    на основе диапазона, где start_date_str и end_date_str могут быть в форматах:
    - 'YYYY-MM-DD' (тогда включаются все смены за эту дату)
    - 'YYYY-MM-DD-День' (только дневная смена)
    - 'YYYY-MM-DD-Ночь' (только ночная смена)
    """
    start_date, start_shift = parse_date_with_shift(start_date_str)
    end_date, end_shift = parse_date_with_shift(end_date_str)

    # Получаем все уникальные даты смен из DataFrame
    all_dates = sorted({d.date() for d in df['Дата'].unique()})
    # Фильтруем даты в диапазоне
    dates_in_range = [d for d in all_dates if start_date <= d <= end_date]

    shift_list = []
    for d in dates_in_range:
        # Для каждой даты определяем, какие типы смен включать
        shifts_for_date = []
        if d == start_date and d == end_date:
            types = ['День', 'Ночь'] if start_shift_type is None else [start_shift_type]
        elif d == start_date:
            types = ['День', 'Ночь'] if start_shift_type is None else [start_shift_type]
        elif d == end_date:
            types = ['День', 'Ночь'] if end_shift_type is None else [end_shift_type]
        else:
            types = ['День', 'Ночь']
        for st in types:
            shift_id = f"{d}_{st}"
            if shift_id in df['Смена'].unique():
                shift_list.append(shift_id)
    return shift_list

# ==================== Функции экспорта (классический режим) ====================

def export_all_metrics_to_excel(analytics_instance, output_path=None, shift_list=None, start_date=None, end_date=None):
    """
    Автоматически вызывает все публичные методы экземпляра ProductionAnalytics,
    которые не требуют обязательных аргументов (кроме self), и сохраняет результаты в Excel.
    """
    if output_path is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"all_metrics_{timestamp}.xlsx"

    metrics = {}
    # Получаем все методы класса
    for name, method in inspect.getmembers(analytics_instance, predicate=inspect.ismethod):
        # Исключаем приватные/защищённые методы и специальные методы
        if name.startswith('_'):
            continue
        sig = inspect.signature(method)
        params = sig.parameters

      # Пропускаем методы, у которых есть обязательные параметры, кроме shift_list
        required = [p for p in sig.parameters.values() 
                    if p.default == inspect.Parameter.empty and p.name != 'self']
        if required and not (len(required) == 1 and required[0].name == 'shift_list'):
            # Методы типа *_date_range сюда не попадут, так как у них два обязательных параметра
            continue

        # Пытаемся вызвать метод с подходящими аргументами
        try:
            if 'shift_list' in sig.parameters:
                print(f"Вызов {name}(shift_list={shift_list})...")
                result = method(shift_list=shift_list)
            else:
                print(f"Вызов {name}()...")
                result = method()
        except Exception as e:
            print(f"  Ошибка в {name}: {e}")
            metrics[name] = pd.DataFrame({"Error": [str(e)]})
            continue
        
        


            # Сохраняем результат (даже если это не DataFrame)
        if isinstance(result, pd.DataFrame):
                df_out = result
        elif isinstance(result, pd.Series):
                # Series -> DataFrame с колонками "Индекс" и "Значение"
                df_out = result.reset_index()
        else:
            df_out = pd.DataFrame({"Result": [result]})
        metrics[name] = df_out
      

    if not metrics:
        print("Не найдено ни одного вызываемого метода.")
        return None

    # Запись в Excel
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        for sheet_name, df in metrics.items():
            # Имя листа не длиннее 31 символа
            sheet = sheet_name[:31]
            df.to_excel(writer, sheet_name=sheet, index=True)
    print(f"✅ Все метрики сохранены в {output_path}")
    return output_path

def main_classic(df, shift_duration=720, shift_list=None):
    analytics = ProductionAnalytics(df, shift_duration=shift_duration)
    export_all_metrics_to_excel(analytics, shift_list=shift_list)



    
# ==================== Режим LLM ====================

def main_query_mode(file_path, user_query, model_path, shift_duration=720):
    print("Загрузка данных...")
    df = load_excel_data(file_path, save_processed=False)
    if df.empty:
        print("Нет данных.")
        return

    print("Загрузка локальной LLM (может занять минуту)...")
    llm = GoRouterAnalyst()   # ключ берётся из GOROUTER_API_KEY
    print("Модель загружена.")

    all_shifts = sorted(df['Смена'].unique())
    params = llm.parse_user_query(user_query, available_shifts=all_shifts)
    if 'error' in params:
        print(f"Ошибка интерпретации: {params['error']}")
        if 'raw_response' in params:
            print("Ответ модели:", params['raw_response'])
        return

    # Формируем shift_list с приоритетом дат
    shift_list = None
    if 'start_date' in params and 'end_date' in params:
        start_date = params['start_date']
        end_date = params['end_date']
        start_shift_type = params.get('start_shift_type')
        end_shift_type = params.get('end_shift_type')
        shift_list = get_shifts_from_range(df, start_date.isoformat(), end_date.isoformat(), start_shift_type, end_shift_type)
        print(f"Сформирован shift_list из дат: {shift_list}")
    elif 'shift_list' in params and params['shift_list']:
        shift_list = params['shift_list']
        print(f"Используем shift_list из запроса: {shift_list}")
    else:
        print("Не указаны ни смены, ни даты. Анализируем все смены.")
        shift_list = None

    

    metrics_type = params.get('metrics_type', 'kim')
    aggregation = params.get('aggregation', 'period')
    machines = params.get('machines', [])
    operators = params.get('operators', [])


      # Логируем извлечённые параметры
    print(f"Метрика: {metrics_type}, агрегация: {aggregation}")
    if machines:
        print(f"Станки: {machines}")
    if operators:
        print(f"Операторы: {operators}")

    analytics = ProductionAnalytics(df, shift_duration=shift_duration)




    result = None

    all_machines = analytics.df['Станок'].unique() if not analytics.df.empty else []
    
    if metrics_type == 'kim':
        if aggregation == 'detailed':
            result = analytics.summary_by_machine(shift_list=shift_list)
        else:
            result = analytics.summary_by_machine_period(shift_list=shift_list)
    elif metrics_type == 'downtime':
        result = analytics.downtime_analysis(shift_list=shift_list)
    elif metrics_type == 'operator':
        result = analytics.summary_by_operator(shift_list=shift_list)
        if operators:
            result = result[result.index.isin(operators)]
    elif metrics_type == 'hourly':
        result = analytics.hourly_load(shift_list=shift_list)
        if result is not None:
            result = result.reset_index()
            result.columns = ['Час', 'Минуты']
    else:
        print(f"Неизвестный тип метрики: {metrics_type}")
        return

    if result is None or result.empty:
        print("Нет данных по вашему запросу.")
        return

    # Принудительная фильтрация по станкам (если указаны)
    if machines and isinstance(result, pd.DataFrame) and not result.empty:
        print(f"Применяем фильтрацию по станкам: {machines}")
        if aggregation != 'detailed':
            if 'Станок' in result.columns:
                mask = result['Станок'].str.contains('|'.join(machines), case=False, na=False)
                result = result[mask]
            else:
                try:
                    result = result[result.index.isin(machines)]
                except:
                    result = result[result.index.str.contains('|'.join(machines), case=False, na=False)]
        else:
            try:
                result = result[result.index.get_level_values('Станок').str.contains('|'.join(machines), case=False, na=False)]
            except:
                pass
        print(f"После фильтрации осталось {len(result)} строк")

        if result.empty:
            print(f"Станки {machines} не найдены. Доступные станки:")
            for m in all_machines:
                print(f"  - {m}")
            return

    answer = llm.generate_response(user_query, result, metrics_type)
    
    print("\n=== ОТВЕТ АНАЛИТИКА ===\n")
    print(answer)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = f"query_result_{timestamp}.xlsx"
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        if isinstance(result, pd.DataFrame):
            result.to_excel(writer, sheet_name="Результат", index=False if 'index' in result.columns else True)
        else:
            pd.DataFrame({"Result": [result]}).to_excel(writer, sheet_name="Результат", index=False)
    print(f"\nРезультат сохранён в {output_path}")




# ==================== Точка входа ====================



if __name__ == "__main__":
    

    parser = argparse.ArgumentParser(description='Производственный аналитик')
    parser.add_argument('file_path', type=str, help='Путь к Excel-файлу')
    parser.add_argument('--query', type=str, help='Естественно-языковой запрос (включает режим LLM)')
    parser.add_argument('--model', type=str, default='phi-3-mini.gguf', help='Путь к GGUF-файлу модели')
    parser.add_argument('--shift-duration', type=int, default=720, help='Длительность смены в минутах')
    # Параметры классического режима (оставлены для обратной совместимости)
    parser.add_argument('--shifts', type=str, default=None, help='Список смен через запятую')
    parser.add_argument('--start-date', type=str, default=None, help='Начальная дата')
    parser.add_argument('--end-date', type=str, default=None, help='Конечная дата')

    args = parser.parse_args()

    if args.query:
        # Режим LLM
        main_query_mode(args.file_path, args.query, args.model, args.shift_duration)
    else:
        # Классический режим (сохранение всех метрик)
        df = load_excel_data(args.file_path, save_processed=True)
        if df.empty:
            print("Нет данных для анализа.")
            sys.exit(1)

        shift_list = None
        if args.start_date and args.end_date:
            shift_list = get_shifts_from_range(df, args.start_date, args.end_date)
            print(f"Смены за период {args.start_date} – {args.end_date}: {shift_list}")
        elif args.shifts:
            shift_list = [s.strip() for s in args.shifts.split(',')]

        main_classic(df, shift_duration=args.shift_duration, shift_list=shift_list)














 
