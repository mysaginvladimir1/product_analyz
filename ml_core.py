import pandas as pd
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class ProductionAnalytics:
    """
    Аналитический слой.
    Использует столбцы: Смена, Станок, Вид простоя, Время,мин., ФИО, Дата_смены.
    """

    PRODUCTIVE_STATES = ['Станок под нагрузкой', 'Программа выполняется']
    INACTIVE_STATES = ['Станок включен', 'Режим jog']

    def __init__(self, df: pd.DataFrame, shift_duration: int = 720):
        # .assign() возвращает копию — оригинальный df не мутируется
        self.df = df.assign(
            is_productive=df['Вид простоя'].isin(self.PRODUCTIVE_STATES),
            is_included_or_jog=df['Вид простоя'].isin(self.INACTIVE_STATES),
        )
        self.shift_duration = shift_duration

    # ---------------------------------------------------------------

    def _get_filtered_df(self, shift_list=None):
        if shift_list is None:
            return self.df
        if not shift_list:
            return self.df.iloc[0:0]
        return self.df[self.df['Смена'].isin(shift_list)]

    def _split_event_by_hour(self, row):
        start, end = row['Время от'], row['Время до']
        duration = row['Время,мин.']
        if pd.isna(start) or pd.isna(end) or duration <= 0:
            return []
        intervals = []
        current = start
        while current < end:
            next_hour = current.replace(minute=0, second=0, microsecond=0) + pd.Timedelta(hours=1)
            if next_hour > end:
                next_hour = end
            minutes = (next_hour - current).total_seconds() / 60.0
            if minutes > 0:
                intervals.append({'Час': current.hour, 'Время,мин.': minutes})
            current = next_hour
        return intervals

    # ---------------------------------------------------------------
    # КИМ по станкам

    def summary_by_machine(self, shift_list=None):
        """Детальная сводка по станкам и сменам (MultiIndex: Смена, Станок)."""
        df_filtered = self._get_filtered_df(shift_list)
        if df_filtered.empty:
            return pd.DataFrame(columns=['Всего_минут', 'Продуктивное_время', 'КИМ_%'])

        grouped = (df_filtered.groupby(['Смена', 'Станок', 'Вид простоя'])['Время,мин.']
                   .sum().reset_index())
        pivot = (grouped.pivot(index=['Смена', 'Станок'], columns='Вид простоя',
                                values='Время,мин.')
                 .fillna(0))
        pivot['Всего_минут'] = self.shift_duration
        prod_time = (df_filtered[df_filtered['is_productive']]
                     .groupby(['Смена', 'Станок'])['Время,мин.'].sum())
        pivot['Продуктивное_время'] = prod_time.reindex(pivot.index, fill_value=0)
        pivot['КИМ_%'] = (pivot['Продуктивное_время'] / pivot['Всего_минут'] * 100).round(1)
        return pivot

    def summary_by_machine_period(self, shift_list=None):
        """Агрегированная сводка по станкам за период (индекс: Станок)."""
        if shift_list is not None and len(shift_list) == 0:
            return pd.DataFrame(
                columns=['Продуктивное_время', 'Всего_минут_период', 'КИМ_%']
            )

        df_filtered = self._get_filtered_df(shift_list)
        num_shifts = self.df['Смена'].nunique() if shift_list is None else len(shift_list)
        if num_shifts == 0:
            return pd.DataFrame(
                columns=['Продуктивное_время', 'Всего_минут_период', 'КИМ_%']
            )

        prod = (df_filtered[df_filtered['is_productive']]
                .groupby('Станок')['Время,мин.'].sum())
        result = pd.DataFrame({
            'Продуктивное_время': prod,
            'Всего_минут_период': num_shifts * self.shift_duration,
        }).fillna(0)
        result['КИМ_%'] = (result['Продуктивное_время'] / result['Всего_минут_период'] * 100).round(1)
        return result

    # ---------------------------------------------------------------
    # Операторы

    def summary_by_operator(self, shift_list=None):
        """Сводка по операторам (индекс: ФИО)."""
        df_filtered = self._get_filtered_df(shift_list)
        if df_filtered.empty:
            return pd.DataFrame()

        total_time = df_filtered.groupby('ФИО')['Время,мин.'].sum().rename('Всего_минут')

        mask = df_filtered['is_included_or_jog'] & (df_filtered['Время,мин.'] > 3)
        incl_jog = df_filtered[mask].groupby('ФИО')['Время,мин.'].sum().rename('Вкл+Jog_минут')

        prod_time = (df_filtered[df_filtered['is_productive']]
                     .groupby('ФИО')['Время,мин.'].sum()
                     .rename('Продуктивное_время_мин'))

        # Станки, где оператор работал > 30 мин
        station_time = df_filtered.groupby(['ФИО', 'Станок'])['Время,мин.'].sum()
        long_stations = station_time[station_time > 30]
        if long_stations.empty:
            stations = pd.Series(dtype=str, name='Обслуживал_станки')
        else:
            stations = (long_stations.reset_index()
                        .groupby('ФИО')['Станок']
                        .agg(lambda s: ', '.join(s.astype(str).unique()))
                        .rename('Обслуживал_станки'))

        # Смены, где оператор работал > 30 мин
        shift_time = df_filtered.groupby(['ФИО', 'Смена'])['Время,мин.'].sum()
        long_shifts = shift_time[shift_time > 30]
        if long_shifts.empty:
            shifts_series = pd.Series(dtype=str, name='работал_в_смены')
        else:
            shifts_series = (long_shifts.reset_index()
                             .groupby('ФИО')['Смена']
                             .agg(lambda s: ', '.join(s.astype(str).unique()))
                             .rename('работал_в_смены'))

        parts = [total_time, incl_jog, prod_time, stations, shifts_series]
        for i, p in enumerate(parts):
            if not isinstance(p, pd.Series):
                logger.error(f"summary_by_operator: элемент {i} не Series, а {type(p)}: {p!r}")

        op_stats = pd.concat(parts, axis=1)

        for col in ('Обслуживал_станки', 'работал_в_смены'):
            if col in op_stats.columns:
                op_stats[col] = op_stats[col].fillna('—')
        op_stats = op_stats.fillna(0)

        op_stats['%_неотмеченного_простоя'] = (
            op_stats['Вкл+Jog_минут'] / op_stats['Всего_минут'] * 100
        ).round(1).fillna(0)
        op_stats['%_продуктивного_времени'] = (
            op_stats['Продуктивное_время_мин'] / op_stats['Всего_минут'] * 100
        ).round(1).fillna(0)

        return op_stats.sort_values('Всего_минут', ascending=False)

    # ---------------------------------------------------------------
    # Простои

    def downtime_analysis(self, shift_list=None):
        df_filtered = self._get_filtered_df(shift_list)
        total_time = df_filtered['Время,мин.'].sum()
        if total_time == 0:
            return pd.DataFrame({'Минуты': [0], 'Доля_%': [0]}, index=['Общий простой'])

        downtime = (df_filtered[~df_filtered['is_productive']]
                    .groupby('Вид простоя')['Время,мин.'].sum()
                    .sort_values(ascending=False))
        if downtime.empty:
            return pd.DataFrame({'Минуты': [0], 'Доля_%': [0]}, index=['Общий простой'])

        pct = (downtime / total_time * 100).round(1)
        result = pd.DataFrame({'Минуты': downtime, 'Доля_%': pct})
        total_minutes = result['Минуты'].sum()
        total_pct = (total_minutes / total_time * 100).round(1)
        summary_row = pd.DataFrame(
            {'Минуты': [total_minutes], 'Доля_%': [total_pct]},
            index=['Общий простой'],
        )
        result = pd.concat([summary_row, result])
        result.index.name = 'Вид простоя'
        return result

    # ---------------------------------------------------------------
    # Почасовая загрузка

    def hourly_load(self, shift_list=None):
        df_filtered = self._get_filtered_df(shift_list)
        prod_df = df_filtered[df_filtered['is_productive']].copy()
        if prod_df.empty:
            return pd.Series(index=range(24), data=0.0, name='Минуты')
        all_parts = []
        for _, row in prod_df.iterrows():
            all_parts.extend(self._split_event_by_hour(row))
        if not all_parts:
            return pd.Series(index=range(24), data=0.0, name='Минуты')
        parts_df = pd.DataFrame(all_parts)
        hourly = parts_df.groupby('Час')['Время,мин.'].sum().reindex(range(24), fill_value=0)
        return hourly.rename('Минуты')

    def hourly_load_by_shift(self, shift_list=None):
        df_filtered = self._get_filtered_df(shift_list)
        prod_df = df_filtered[df_filtered['is_productive']].copy()
        if prod_df.empty:
            return pd.DataFrame()
        all_parts = []
        for _, row in prod_df.iterrows():
            start, end = row['Время от'], row['Время до']
            if pd.isna(start) or pd.isna(end):
                continue
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
                        'Время,мин.': minutes,
                    })
                current = next_hour
        if not all_parts:
            return pd.DataFrame()
        parts_df = pd.DataFrame(all_parts)
        grouped = parts_df.groupby(['Смена', 'Час'])['Время,мин.'].sum().unstack(fill_value=0)
        return grouped.reindex(columns=range(24), fill_value=0)

    # ---------------------------------------------------------------
    # Диапазон дат

    def get_shifts_in_range(self, start_date, end_date):
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        mask = (self.df['Дата_смены'] >= start_date) & (self.df['Дата_смены'] <= end_date)
        return self.df.loc[mask, 'Смена'].unique().tolist()

    def hourly_load_by_shift_date_range(self, start_date, end_date):
        return self.hourly_load_by_shift(self.get_shifts_in_range(start_date, end_date))

    def hourly_load_date_range(self, start_date, end_date):
        return self.hourly_load(self.get_shifts_in_range(start_date, end_date))

    def downtime_analysis_date_range(self, start_date, end_date):
        return self.downtime_analysis(self.get_shifts_in_range(start_date, end_date))

    def summary_by_operator_date_range(self, start_date, end_date):
        return self.summary_by_operator(self.get_shifts_in_range(start_date, end_date))

    def summary_by_machine_period_date_range(self, start_date, end_date):
        return self.summary_by_machine_period(self.get_shifts_in_range(start_date, end_date))

    def summary_by_machine_date_range(self, start_date, end_date):
        return self.summary_by_machine(self.get_shifts_in_range(start_date, end_date))