import pandas as pd
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

REQUIRED_COLS = [
    'Дата', 'День/ночь', 'Время от', 'Время до', 'Станок',
    'Вид простоя', 'Время,мин.', 'Время комментария',
    'Комментарий', 'ФИО',
]


def _preprocess_sheet(df: pd.DataFrame, sheet_name: str) -> pd.DataFrame:
    """Предобработка одного листа."""
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"Лист '{sheet_name}': отсутствуют колонки {missing}")

    df = df.copy()

    # Времена -> datetime
    df['Время от'] = pd.to_datetime(df['Время от'], errors='coerce')
    df['Время до'] = pd.to_datetime(df['Время до'], errors='coerce')
    n_bad = int(df['Время от'].isna().sum() + df['Время до'].isna().sum())
    if n_bad:
        logger.warning(f"  [{sheet_name}] некорректных времён: {n_bad}")

    df['Час'] = df['Время от'].dt.hour

    df['Дата'] = pd.to_datetime(df['Дата'], errors='coerce')
    # ВАЖНО: date-колонка для сравнений (используется в get_shifts_in_range)
    df['Дата_смены'] = df['Дата'].dt.date
    df['Смена'] = df['Дата'].dt.date.astype(str) + '_' + df['День/ночь'].astype(str)

    df['Лист'] = sheet_name
    df = df.sort_values('Время от').reset_index(drop=True)
    return df


def load_excel_data(file_path: str, save_processed: bool = False) -> pd.DataFrame:
    """Загружает ВСЕ листы Excel-файла и возвращает единый DataFrame."""
    try:
        sheets = pd.read_excel(file_path, sheet_name=None, engine='openpyxl')
        logger.info(
            f"Успешно загружен файл: {file_path} "
            f"(листов: {len(sheets)} -> {list(sheets.keys())})"
        )
    except FileNotFoundError:
        logger.error(f"Файл '{file_path}' не найден.")
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"Критическая ошибка при чтении {file_path}: {e}")
        return pd.DataFrame()

    processed = []
    for name, df in sheets.items():
        if df.empty:
            logger.info(f"  лист '{name}' пустой — пропускаю")
            continue
        try:
            processed.append(_preprocess_sheet(df, name))
        except ValueError as e:
            logger.warning(f"  {e} — лист пропущен")

    if not processed:
        logger.error("Не удалось обработать ни один лист.")
        return pd.DataFrame()

    df_all = pd.concat(processed, ignore_index=True)

    if save_processed:
        try:
            base = os.path.splitext(file_path)[0]
            output_path = f"{base}_processed.csv"
            df_all.to_csv(output_path, index=False, decimal=',', encoding='utf-8-sig')
            logger.info(f"Обработанные данные сохранены в {output_path}")
        except Exception as e:
            logger.warning(f"Не удалось сохранить: {e}")

    return df_all


def get_shifts_from_range(df: pd.DataFrame, start_date, end_date,
                          start_shift_type: str | None = None,
                          end_shift_type: str | None = None) -> list[str]:
    """
    Возвращает отсортированный список смен между двумя датами (включительно).
    start_date/end_date: 'YYYY-MM-DD' или date/datetime.
    start_shift_type / end_shift_type: 'День'/'Ночь' — фильтр для крайних дней.
    """
    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    elif isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, str):
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
    elif isinstance(end_date, datetime):
        end_date = end_date.date()

    if start_date > end_date:
        logger.warning(f"start_date ({start_date}) > end_date ({end_date})")
        return []

    mask = (df['Дата_смены'] >= start_date) & (df['Дата_смены'] <= end_date)
    shifts = sorted(df.loc[mask, 'Смена'].unique().tolist())

    if start_shift_type:
        first = start_date.isoformat()
        shifts = [s for s in shifts
                  if not s.startswith(f"{first}_") or s.endswith(f"_{start_shift_type}")]
    if end_shift_type:
        last = end_date.isoformat()
        shifts = [s for s in shifts
                  if not s.startswith(f"{last}_") or s.endswith(f"_{end_shift_type}")]

    return shifts