import os
from pathlib import Path

import asyncio
import logging
import re
import time

import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from data_manager import load_excel_data, get_shifts_from_range
from llm import AIAnalyst
from ml_core import ProductionAnalytics

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("analyst.log", encoding='utf-8'),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Производственный аналитик")

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = os.environ.get("DATA_PATH", str(BASE_DIR / "data_test.xlsx"))
SHIFT_DURATION = 720

_state = {"df": pd.DataFrame()}
llm = AIAnalyst()


def _load_data():
    t0 = time.time()
    df = load_excel_data(DATA_PATH, save_processed=False)
    if df.empty:
        logger.error("Данные не загружены")
    else:
        logger.info(f"Данные загружены за {time.time() - t0:.2f} сек, строк: {len(df)}")
    _state["df"] = df


_load_data()


class QueryRequest(BaseModel):
    query: str
    shift_duration: int = SHIFT_DURATION


# ---------------------------------------------------------------
# Основная логика запроса

def process_query(df: pd.DataFrame, query: str, model: AIAnalyst, shift_duration: int, history=None):
    try:
        logger.info(f"Запрос: {query}")

        # Парсинг
        step = time.time()
        all_shifts = sorted(df['Смена'].unique())
        params = model.parse_user_query(query, available_shifts=all_shifts, history=history)
        if 'error' in params:
            logger.error(f"Ошибка парсинга: {params['error']}")
            return f"Ошибка: {params['error']}", None
        logger.info(f"Параметры: {params} | {time.time() - step:.2f} сек")

        metrics_type = params.get('metrics_type', 'kim')
        aggregation = params.get('aggregation', 'period')
        machines = params.get('machines', []) or []
        operators = params.get('operators', []) or []

        # Смены
        step = time.time()
        shift_list = None
        if 'start_date' in params and 'end_date' in params:
            shift_list = get_shifts_from_range(
                df,
                params['start_date'].isoformat(),
                params['end_date'].isoformat(),
                params.get('start_shift_type'),
                params.get('end_shift_type'),
            )
            logger.info(f"shift_list из дат: {shift_list}")
        elif params.get('shift_list'):
            shift_list = params['shift_list']
            logger.info(f"shift_list из запроса: {shift_list}")
        else:
            logger.info("Не указаны ни смены, ни даты. Анализируем все смены.")
        logger.info(f"Формирование shift_list: {time.time() - step:.2f} сек")

        # Аналитика
        step = time.time()
        analytics = ProductionAnalytics(df, shift_duration=shift_duration)
        all_machines = analytics.df['Станок'].unique() if not analytics.df.empty else []

        if metrics_type == 'kim':
            result = (analytics.summary_by_machine(shift_list=shift_list)
                      if aggregation == 'detailed'
                      else analytics.summary_by_machine_period(shift_list=shift_list))
        elif metrics_type == 'downtime':
            result = analytics.downtime_analysis(shift_list=shift_list)
        elif metrics_type == 'operator':
            result = analytics.summary_by_operator(shift_list=shift_list)
            if operators:
                missing = [o for o in operators if o not in result.index]
                if missing:
                    logger.warning(f"Операторы не найдены: {missing}")
                result = result[result.index.isin(operators)]
        elif metrics_type == 'hourly':
            if machines:
                logger.warning("Фильтрация по станкам не поддерживается для hourly — игнорирую")
                machines = []
            result = analytics.hourly_load(shift_list=shift_list)
            if result is not None:
                result = result.reset_index()
                result.columns = ['Час', 'Минуты']
        else:
            logger.error(f"Неизвестный тип метрики: {metrics_type}")
            return f"Неизвестная метрика: {metrics_type}", None

        logger.info(f"Аналитика ({metrics_type}): {time.time() - step:.2f} сек")

        if result is None or result.empty:
            logger.warning("Нет данных по запросу")
            return "Нет данных по запросу.", None

        # Фильтрация по станкам
        if machines and isinstance(result, pd.DataFrame) and not result.empty:
            step = time.time()
            pattern = "|".join(re.escape(m) for m in machines)
            if isinstance(result.index, pd.MultiIndex):
                mask = result.index.get_level_values('Станок').str.contains(
                    pattern, case=False, na=False)
                result = result[mask]
            elif 'Станок' in result.columns:
                result = result[result['Станок'].str.contains(pattern, case=False, na=False)]
            else:
                result = result[result.index.astype(str).str.contains(
                    pattern, case=False, na=False)]

            logger.info(f"После фильтрации по станкам: {len(result)} строк "
                        f"({time.time() - step:.2f} сек)")
            if result.empty:
                logger.info(f"Станки {machines} не найдены")
                return (
                    f"Станки {machines} не найдены в данных. Доступные: "
                    f"{', '.join(sorted(all_machines)[:15])}",
                    None,
                )

        # Генерация ответа
        step = time.time()
        answer = model.generate_response(query, result, metrics_type, history=history)
        logger.info(f"Генерация ответа: {time.time() - step:.2f} сек")
        return answer, result

    except Exception as e:
        logger.error(f"Ошибка обработки запроса: {e}", exc_info=True)
        return f"Ошибка обработки запроса: {e}", None


# ---------------------------------------------------------------
# Эндпоинты

@app.get("/ping")
async def ping():
    return {"status": "ok"}


@app.post("/api/analyze2")
async def analyze2(request: QueryRequest):
    if _state["df"].empty:
        raise HTTPException(503, "Данные не загружены")

    t0 = time.time()
    logger.info(f"Получен POST-запрос: {request.query}")
    answer, _ = await asyncio.to_thread(
        process_query, _state["df"], request.query, llm, request.shift_duration
    )
    logger.info(f"Запрос обработан за {time.time() - t0:.2f} сек")
    return {"answer": answer}


@app.post("/api/reload")
async def reload_data():
    await asyncio.to_thread(_load_data)
    if _state["df"].empty:
        return JSONResponse(status_code=503, content={"status": "failed"})
    return {"status": "ok", "rows": len(_state["df"])}


from chat_api import install_chat_api
install_chat_api(app, _state, llm, process_query, SHIFT_DURATION)

# Static — в самом конце, чтобы не перекрыть /docs и /api
app.mount("/", StaticFiles(directory=str(BASE_DIR / "site"), html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)