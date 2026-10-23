# server.py
import pandas as pd
import logging
import time
import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from data_manager import load_excel_data
from ml_core import ProductionAnalytics
from llm import GoRouterAnalyst
from main import get_shifts_from_range  # если она вынесена отдельно
import datetime
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Производственный аналитик")

#Настрйока логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("analyst.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)





# Глобально загружаем данные и модель (один раз при старте)
DATA_PATH = r"\\fpsa.ad.instrument52.ru\Папки отделов\Цифровизация производства\winnum\Архив.xlsx"
#DATA_PATH = "C:\ВладимирАКСИС\Владимир\цифровизация_ГК_Инструмент\python\production_analyst\src\data_test.xlsx"          # путь к вашему Excel-файлу
#MODEL_PATH = "Qwen/Qwen2-1.5B-Instruct"   # или локальный путь
SHIFT_DURATION = 720
CACHE_TTL = 300 #        секунд — минимальный интервал между перечитываниями файла
                          # даже если файл не менялся
LOCAL_COPY = "data_local.xlsx"   # локальная копия — защита от блокировки Excel
# --- Кэш данных ---
DATA_CACHE = {
    "df": None,
    "all_shifts": None,
    "mtime": 0,
    "loaded_at": 0,
}

logger.info("Загрузка данных...")
start=time.time()


df = load_excel_data(DATA_PATH, save_processed=False)
if df.empty:
    logger.error("Данные не загружены")
    raise RuntimeError("Данные не загружены")
logger.info(f"Данные загружены за {time.time() - start:.2f} сек, строк: {len(df)}")


logger.info("Загрузка модели...")
start = time.time()
llm = GoRouterAnalyst()   # ключ берётся из GOROUTER_API_KEY
logger.info(f"Модель загружена за {time.time() - start:.2f} сек")

# --- Определяем класс запроса ---
class QueryRequest(BaseModel):
    query: str
    shift_duration: int = SHIFT_DURATION  # можно переопределить

# --- Вспомогательная функция для обработки запроса (копия из main_query_mode) ---
def process_query(df, query, model, shift_duration):
    try:
        # Здесь почти весь код из main_query_mode, но без Excel-сохранения и без print
        # Используем переданный df, модель и shift_duration
        logger.info(f"Запрос: {query}")
        step_start = time.time()
        all_shifts = sorted(df['Смена'].unique())
        logger.debug(f"Всего смен: {len(all_shifts)}")
        params = model.parse_user_query(query, available_shifts=all_shifts)
        if 'error' in params:
            logger.error(f"Ошибка парсинга: {params['error']}")
            return f"Ошибка: {params['error']}", None

        logger.info(f"Параметры: {params}")
        logger.info(f"Парсинг запроса занял {time.time() - step_start:.2f} сек")


        step_start = time.time()
        metrics_type = params.get('metrics_type', 'kim')
        logger.info(f"metrics_type: {metrics_type}")
        aggregation = params.get('aggregation', 'period')
        logger.info(f"aggregation: {aggregation}")
        machines = params.get('machines', [])
        logger.info(f"machines: {machines}")
        operators = params.get('operators', [])
        logger.info(f"operators: {operators}")






        shift_list = None
        if 'start_date' in params and 'end_date' in params:
            start_date = params['start_date']
            end_date = params['end_date']
            start_shift_type = params.get('start_shift_type')
            end_shift_type = params.get('end_shift_type')
            shift_list = get_shifts_from_range(df, start_date.isoformat(), end_date.isoformat(), start_shift_type, end_shift_type)
            logger.info(f"Сформирован shift_list из дат: {shift_list}")
        elif 'shift_list' in params and params['shift_list']:
            shift_list = params['shift_list']
            logger.info(f"Используем shift_list из запроса: {shift_list}")
        else:
            logger.info("Не указаны ни смены, ни даты. Анализируем все смены.")
            shift_list = None

        logger.info(f"Формирование shift_list заняло {time.time() - step_start:.2f} сек")
        step_start = time.time()

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
            logger.error(f"Неизвестный тип метрики: {metrics_type}")
            return f"Неизвестная метрика: {metrics_type}", None


        logger.info(f"Вызов аналитики ({metrics_type}) занял {time.time() - step_start:.2f} сек")
        

        if result is None or result.empty:
            logger.warning("Нет данных по запросу")
            return "Нет данных по запросу.", None



        step_start = time.time()
        
# Принудительная фильтрация по станкам (если указаны)
        if machines and isinstance(result, pd.DataFrame) and not result.empty:
                logger.info(f"Применяем фильтрацию по станкам: {machines}")
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
                logger.info(f"После фильтрации осталось {len(result)} строк")
        
                if result.empty:
                    logger.info(f"Станки {machines} не найдены. Доступные станки:")
                    for m in all_machines:
                        print(f"  - {m}")
                    return
        logger.info(f"Фильтрация по станкам заняла {time.time() - step_start:.2f} сек")
        # Генерируем ответ
        step_start = time.time()
        answer = model.generate_response(query, result, metrics_type)
        logger.info(f"Генерация ответа заняла {time.time() - step_start:.2f} сек")
        return answer, result
    except Exception as e:
        logger.error(f"Ошибка обработки запроса: {str(e)}")
        return f"Ошибка обработки запроса: {str(e)}", None



    
# --- Эндпоинт ---
@app.get("/ping")
async def ping():
    return {"status": "ok"}

@app.post("/api/analyze2")

async def analyze2(request: QueryRequest):
    try:
        start_all = time.time()
        logger.info(f"Получен POST-запрос: {request.query}")
        answer = await asyncio.to_thread(process_query, df, request.query, llm, request.shift_duration)
        logger.info(f"Запрос обработан за {time.time() - start_all:.2f} сек")
        return {"answer": answer[0]}
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
app.mount("/", StaticFiles(directory="site", html=True), name="static")
# --- Запуск (если файл запускается напрямую) ---
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
