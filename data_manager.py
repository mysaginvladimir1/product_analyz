import pandas as pd
import os

DATA_DIR = 'C:/ВладимирАКСИС/Владимир/цифровизация_ГК_Инструмент/python/production_analyst/data/input_excel/data_test'
TRAINING_DIR = 'data/training_data'
filename = 'data_test.xlsx'
file_path = os.path.join(DATA_DIR, filename)
def load_excel_data(file_path: str, save_processed: bool = False) -> pd.DataFrame:
    """
    Загружает данные из указанного файла Excel и выполняет базовую предобработку.
    """
  
    try:
        # 1. Чтение данных (предполагаем, что данные на первом листе)
        df = pd.read_excel(file_path, sheet_name="Сентябрь 2026", engine='openpyxl')
        print(f"Успешно загружен файл: {file_path}")
    except FileNotFoundError:
        print(f"Файл '{file_path}' не найден.")
        return pd.DataFrame()
    except Exception as e:
        print(f"Критическая ошибка при чтении {file_path}: {e}. Проверьте формат Excel.")
        return pd.DataFrame()
        
    required_cols = ['Дата', 'День/ночь', 'Время от', 'Время до',  'Станок', 'Вид простоя', 'Время,мин.', 'Время комментария', 'Комментарий', 'ФИО']
    if not all(col in df.columns for col in required_cols):
        raise ValueError(f"Отсутствуют колонки: {required_cols}")
      
         # Преобразуем время начала в datetime (для извлечения часа)
    df['Время от'] = pd.to_datetime(df['Время от'], errors='coerce')
    # Предупреждение, если много пропусков
    if df['Время от'].isna().sum() > 0:
        print(f"Предупреждение: {df['Время от'].isna().sum()} записей с некорректным временем начала.")
    
    # Извлекаем час (будет NaN для некорректных записей)
    df['Час'] = df['Время от'].dt.hour
       # Создаём идентификатор смены: используем столбец 'Дата' и 'День/ночь'
    df['Дата'] = pd.to_datetime(df['Дата'])   # убеждаемся, что 'Дата' datetime
    df['Смена'] = df['Дата'].dt.date.astype(str) + '_' + df['День/ночь']

    
       # Сортируем по времени начала
    df.sort_values('Время от', inplace=True)
        #сбрасываем индекс и присваиваем новый без сохранения старого
    df.reset_index(drop=True, inplace=True)
     # Сохраняем, если нужно
    if save_processed:
        try:
            base = os.path.splitext(file_path)[0]
            output_path = f"{base}_processed.csv"
            df.to_csv(output_path, index=False, decimal=',', encoding='utf-8-sig')
            print(f"Обработанные данные сохранены в {output_path}")
        except Exception as e:
            print(f"Не удалось сохранить обработанный файл: {e}")

    return df






