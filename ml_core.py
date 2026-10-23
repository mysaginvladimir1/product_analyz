import pandas as pd
from datetime import datetime
#from sklearn.linear_model import LinearRegression
#from statsmodels.tsa.seasonal import seasonal_decompose

class ProductionAnalytics:
    "Определяем группы состояний:"
    "self.productive_states - это "
    "self.df['is_productive'] - это "
    "self.df['is_included_or_jog'] - это"

    def __init__(self, df: pd.DataFrame, shift_duration: int = 720):
        #    shift_duration: нормативная длительность смены в минутах (по умолчанию 12 ч = 720 мин)
        self.df = df.copy()
        self.shift_duration = shift_duration
        # Определяем группы состояний
        self.productive_states = ['Станок под нагрузкой', 'Программа выполняется']
        self.df['is_productive'] = self.df['Вид простоя'].isin(self.productive_states)
        self.df['is_included_or_jog'] = self.df['Вид простоя'].isin(['Станок включен', 'Режим jog'])

    def _get_filtered_df(self, shift_list=None):
        "То есть тут надо вместо shift_list=None писать переменную, на которую буду ссылаться в неявном виде??? "
        "(указывает постановщик задачи в тексте задачи)"
        """Возвращает отфильтрованный DataFrame по списку смен. Если shift_list=None, возвращает полный df."""
        if shift_list is None:
            return self.df
        return self.df[self.df['Смена'].isin(shift_list)]
    
    

    def _split_event_by_hour(self,row):
        """Разбивает событие (строку) на части по часам.
        Возвращает список словарей с ключами 'Час' и 'Время,мин."""
        start = row['Время от']
        end= row['Время до']
        duration = row['Время,мин.']
        if pd.isna(start) or pd.isna(end) or duration <= 0:
            return[]
        intervals = []
        current = start
        while current < end:
            next_hour = current.replace(minute=0, second=0, microsecond=0) + pd.Timedelta(hours=1)
            if next_hour > end:
                next_hour = end
            minutes = (next_hour - current).total_seconds() / 60.0
            if minutes > 0:
                intervals.append({
                    'Час': current.hour,
                    'Время,мин.': minutes
                })
            current = next_hour
        return intervals

    def summary_by_machine(self, shift_list=None):
        """Детальная сводка по станкам и сменам. Если передан shift_list, возвращает только указанные смены.
        Возвращает DataFrame с мультииндексом (Смена, Станок) и столбцами по видам простоев,
        плюс общее время (норматив), продуктивное время, КИМ."""
        df_filtered = self._get_filtered_df(shift_list)
        grouped = self.df.groupby(['Смена','Станок', 'Вид простоя'])['Время,мин.'].sum().reset_index()
        pivot = grouped.pivot(index=['Смена','Станок'], columns='Вид простоя', values='Время,мин.').fillna(0)
          # Используем норматив вместо фактической суммы
        pivot['Всего_минут'] = self.shift_duration
        prod_time = df_filtered[df_filtered['is_productive']].groupby(['Смена', 'Станок'])['Время,мин.'].sum()
        pivot['Продуктивное_время'] = prod_time.reindex(pivot.index, fill_value=0)
        pivot['КИМ_%'] = (pivot['Продуктивное_время'] / pivot['Всего_минут'] * 100).round(1)
        return pivot
    "КИМ - коэффициент ипользования машин (оборудования)"




    #расчёт сводных показателей по каждому станку за произвольный период, состоящий из нескольких смен
    """
        Сводка по станкам за произвольный период (несколько смен).
        shift_list: список идентификаторов смен. Если None, берутся все смены.
        Возвращает DataFrame с индексом Станок, столбцами:
        - Продуктивное_время (сумма за период)
        - Всего_минут_период (количество смен * shift_duration)
        - КИМ
        """

    def summary_by_machine_period(self, shift_list=None):
        df_filtered = self._get_filtered_df(shift_list)
        if shift_list is None:
            num_shifts = self.df['Смена'].nunique()
        else:
            num_shifts = len(shift_list)
        
        prod_per_machine = df_filtered[df_filtered['is_productive']].groupby('Станок')['Время,мин.'].sum()
        total_fund = num_shifts * self.shift_duration

        
        result = pd.DataFrame({
            'Продуктивное_время': prod_per_machine,
            'Всего_минут_период': total_fund
        }).fillna(0)
        result['КИМ_%'] = (result['Продуктивное_время'] / result['Всего_минут_период'] * 100).round(1)
        return result





    #сводная по операторам
    def summary_by_operator(self,shift_list=None):
        """
        Агрегация по операторам:
        - общее время работы (на всех станках)
        - время в состояниях "станок включен" и "режим jog" (суммарно)
        - процент этих состояний от общего времени (неотмеченный простой)
        - также можно добавить продуктивное время
        """
        df_filtered = self._get_filtered_df(shift_list)
        # Общее время по оператору
        total_time = df_filtered.groupby('ФИО')['Время,мин.'].sum().rename('Всего_минут')

        # Время в состояниях "станок включен" и "режим jog"
        mask = df_filtered['is_included_or_jog'] & (df_filtered['Время,мин.'] > 3)
        incl_jog = df_filtered[mask].groupby('ФИО')['Время,мин.'].sum().rename('Вкл+Jog_минут')
        

        # Продуктивное время
        prod_time = df_filtered[df_filtered['is_productive']].groupby('ФИО')['Время,мин.'].sum().rename('Продуктивное_время_мин')
        

        # Список уникальных станков для каждого оператора (через запятую), где работал больше 30 минут
        station_time = df_filtered.groupby(['ФИО', 'Станок'])['Время,мин.'].sum().reset_index()
        long_stations = station_time[station_time['Время,мин.'] > 30]
        stations = long_stations.groupby('ФИО')['Станок'].unique().apply(lambda x: ', '.join(x)).rename('Обслуживал_станки')

        # Список уникальных смен, где оператор работал более 30 минут
        shift_time = df_filtered.groupby(['ФИО', 'Смена'])['Время,мин.'].sum().reset_index()
        long_shifts = shift_time[shift_time['Время,мин.'] > 30]
        shifts_series = long_shifts.groupby('ФИО')['Смена'].apply(lambda x: ', '.join(x)).rename('работал_в_смены')
        # Объединяем
        op_stats = pd.concat([total_time, incl_jog, prod_time, stations, shifts_series], axis=1).fillna(0)
        "axis=1 - объединяем параметры total_time, incl_jog, prod_time в датафрейм постолбцам, если бы был axis=0, объединяли бы по строкам"
        "fillna(0) меняет все данные NaN на 0"
        op_stats['%_неотмеченного_простоя'] = (op_stats['Вкл+Jog_минут'] / op_stats['Всего_минут'] * 100).round(1).fillna(0)
        #Продуктивное время в процентах
        op_stats['%_продуктивного_времени']=(op_stats['Продуктивное_время_мин'] / op_stats['Всего_минут'] * 100).round(1).fillna(0)


        # Сортируем по общему времени
        op_stats = op_stats.sort_values('Всего_минут', ascending=False)
        "Сортировка ascending=False - это сортировка по убыванию (от большего к меньшего)"
        return op_stats
    







    
    def downtime_analysis(self, shift_list=None):
        df_filtered = self._get_filtered_df(shift_list)
        downtime = df_filtered[~df_filtered['is_productive']].groupby('Вид простоя')['Время,мин.'].sum().sort_values(ascending=False)
        total_time = df_filtered['Время,мин.'].sum()
        if total_time == 0:
            return pd.DataFrame({'Минуты': [], 'Доля_%': []})
        
        pct = (downtime / total_time * 100).round(1)

        result = pd.DataFrame({'Минуты': downtime, 'Доля_%': pct})

    # Итоговая строка
        total_minutes = result['Минуты'].sum()
        total_pct = (total_minutes / total_time * 100).round(1)
        summary_row = pd.DataFrame({'Минуты': [total_minutes], 'Доля_%': [total_pct]}, index=['Общий простой'])

    # Сначала итог, затем детали
        result = pd.concat([summary_row, result])
        result.index.name = 'Вид простоя'
        return result
   



   



    def hourly_load(self, shift_list=None):
        """Продуктивная загрузка по часам за указанный период.
        Возвращает Series с индексом час."""
        df_filtered = self._get_filtered_df(shift_list)
        prod_df = df_filtered[df_filtered['is_productive']].copy()
        if prod_df.empty:
            return pd.Series(index=range(24), data=0.0)
        all_parts = []
        for idx, row in prod_df.iterrows():
            parts = self._split_event_by_hour(row)
            all_parts.extend(parts)
        if not all_parts:
            return pd.Series(index=range(24), data=0.0)
        parts_df = pd.DataFrame(all_parts)
        hourly = parts_df.groupby('Час')['Время,мин.'].sum()
        hourly = hourly.reindex(range(24), fill_value=0)
        return hourly

     
    

    def hourly_load_by_shift(self, shift_list=None):
        """
        Продуктивная загрузка по часам в разрезе смен (только указанные смены, если передан shift_list).
        Возвращает DataFrame: строки = часы, столбцы = смены.
        """
        df_filtered = self._get_filtered_df(shift_list)
        prod_df = df_filtered[df_filtered['is_productive']].copy()
        if prod_df.empty:
            return pd.DataFrame()
        all_parts = []
        for idx, row in prod_df.iterrows():
            start = row['Время от']
            end = row['Время до']
            current = start
            while current < end:
                next_hour = current.replace(minute=0, second=0, microsecond=0) + pd.Timedelta(hours=1)
                if next_hour > end:
                    next_hour = end
                minutes = (next_hour - current).total_seconds() / 60.0
                if minutes > 0:
                    all_parts.append({
                        'Смена': row['Смена'],
                        'Час': current.hour,
                        'Время,мин.': minutes
                    })
                current = next_hour
        if not all_parts:
            return pd.DataFrame()
        parts_df = pd.DataFrame(all_parts)
        grouped = parts_df.groupby(['Смена', 'Час'])['Время,мин.'].sum().unstack(fill_value=0)
        grouped = grouped.reindex(columns=range(24), fill_value=0)
        return grouped


   
    def get_shifts_in_range(self, start_date, end_date):
        """
        Возвращает список идентификаторов смен, попадающих в диапазон дат (включительно).
        start_date, end_date: строки в формате 'YYYY-MM-DD' или объекты date/datetime.
        """
        # Приведение к типу date
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        
        mask = (self.df['Дата_смены'] >= start_date) & (self.df['Дата_смены'] <= end_date)
        shifts = self.df.loc[mask, 'Смена'].unique().tolist()
        return shifts
    def hourly_load_by_shift_date_range(self, start_date, end_date):
        """
        Продуктивная загрузка по часам в разрезе смен за диапазон дат.
        """
        shifts = self.get_shifts_in_range(start_date, end_date)
        return self.hourly_load_by_shift(shift_list=shifts)
    def hourly_load_date_range(self, start_date, end_date):
        """
        Продуктивная загрузка по часам за диапазон дат.
        """
        shifts = self.get_shifts_in_range(start_date, end_date)
        return self.hourly_load(shift_list=shifts)
    def downtime_analysis_date_range(self, start_date, end_date):
        """
        Структура простоев за диапазон дат.
        """
        shifts = self.get_shifts_in_range(start_date, end_date)
        return self.downtime_analysis(shift_list=shifts)
        "def summary_by_operator_date_range позволяет пользователю задать период датами (например, с 1 по 7 февраля), "
    "а не списком конкретных смен"
    def summary_by_operator_date_range(self, start_date, end_date):
        """
        Аналитика по операторам за диапазон дат.
        """
        shifts = self.get_shifts_in_range(start_date, end_date)
        return self.summary_by_operator(shift_list=shifts)
    def summary_by_machine_period_date_range(self, start_date, end_date):
        """
        Сводка по станкам за диапазон дат (агрегированная).
        """
        shifts = self.get_shifts_in_range(start_date, end_date)
        return self.summary_by_machine_period(shift_list=shifts)
    def summary_by_machine_date_range(self, start_date, end_date):
        """
        Детальная сводка по станкам за диапазон дат (включительно).
        """
        shifts = self.get_shifts_in_range(start_date, end_date)
        return self.summary_by_machine(shift_list=shifts)